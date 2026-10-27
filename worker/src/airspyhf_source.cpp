#include "airspyhf_source.hpp"

#include <airspyhf.h>
#include <sys/eventfd.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <cstring>

namespace k2 {

namespace {
uint64_t mono_ns() {
    timespec ts{};
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
}
}  // namespace

static_assert(sizeof(airspyhf_complex_float_t) == sizeof(dsp::cf), "complex float layouts must match");

namespace {
int on_block(airspyhf_transfer_t* t) {
    static_cast<AirspyhfSource*>(t->ctx)->deliver(t->samples, t->sample_count, t->dropped_samples);
    return 0;
}
}  // namespace

AirspyhfSource::AirspyhfSource() { wake_fd_ = ::eventfd(0, EFD_NONBLOCK | EFD_CLOEXEC); }

AirspyhfSource::~AirspyhfSource() {
    // Owners call stop_and_close() or abandon() first; never block in a destructor.
    if (wake_fd_ >= 0) ::close(wake_fd_);
}

void AirspyhfSource::alloc_ring(uint32_t block_cap) {
    block_cap_ = block_cap;
    data_.assign(static_cast<size_t>(kSlots) * block_cap, dsp::cf());
    count_.assign(kSlots, 0);
    dropped_.assign(kSlots, 0);
    lost_.assign(kSlots, 0);
    epoch_of_.assign(kSlots, 0);
}

uint32_t AirspyhfSource::open(uint64_t serial, uint32_t rate_hz, uint32_t center_hz, const HwSettings& hw,
                              std::string& err) {
    if (wake_fd_ < 0) {
        err = "eventfd failed";
        return KCTL_E_DEVICE;
    }
    airspyhf_device_t* d = nullptr;
    if (airspyhf_open_sn(&d, serial) != AIRSPYHF_SUCCESS || d == nullptr) {
        // Distinguish "held by another process" (listed, cannot open) from "not connected".
        uint64_t serials[16] = {};
        const int n = airspyhf_list_devices(serials, 16);
        const bool present = n > 0 && std::find(serials, serials + std::min(n, 16), serial) != serials + std::min(n, 16);
        err = present ? "device listed but could not be opened (in use by another program, or not authorized)"
                      : "no Airspy HF+ with that serial is connected";
        return present ? KCTL_E_BUSY : KCTL_E_DEVICE;
    }
    dev_ = d;
    uint32_t count = 0;
    if (airspyhf_get_samplerates(dev_, &count, 0) == AIRSPYHF_SUCCESS && count > 0 && count < 64) {
        rates_.resize(count);
        if (airspyhf_get_samplerates(dev_, rates_.data(), count) != AIRSPYHF_SUCCESS) rates_.clear();
    }
    if (std::find(rates_.begin(), rates_.end(), rate_hz) == rates_.end()) {
        err = "sample rate not offered by the device";
        return KCTL_E_UNSUPPORTED;
    }
    // Every control, every time: the device keeps whatever the previous process set.
    if (airspyhf_set_samplerate(dev_, rate_hz) != AIRSPYHF_SUCCESS ||
        airspyhf_set_freq(dev_, center_hz) != AIRSPYHF_SUCCESS ||
        airspyhf_set_lib_dsp(dev_, 1) != AIRSPYHF_SUCCESS || !set_hw(hw)) {
        err = "device refused an initial setting";
        return KCTL_E_DEVICE;
    }
    const int out = airspyhf_get_output_size(dev_);
    if (out <= 0 || out > (1 << 16)) {
        err = "unexpected driver block size";
        return KCTL_E_DEVICE;
    }
    alloc_ring(static_cast<uint32_t>(out));
    return KCTL_OK;
}

bool AirspyhfSource::start(std::string& err) {
    last_block_ns_.store(mono_ns(), std::memory_order_release);   // stall timer starts now
    if (airspyhf_start(dev_, &on_block, this) != AIRSPYHF_SUCCESS) {
        err = "airspyhf_start failed";
        return false;
    }
    return true;
}

// Driver callback thread. Never blocks: a full ring counts the block as overflow.
void AirspyhfSource::deliver(const void* samples, int sample_count, uint64_t dropped) {
    last_block_ns_.store(mono_ns(), std::memory_order_release);
    const uint32_t epoch = epoch_.load(std::memory_order_acquire);
    const uint64_t head = head_.load(std::memory_order_relaxed);
    const uint64_t tail = tail_.load(std::memory_order_acquire);
    const uint32_t n = sample_count < 0 ? 0u : std::min(static_cast<uint32_t>(sample_count), block_cap_);
    if (head - tail >= kSlots) {
        lost_pending_ += n + dropped;   // reported with the next block that fits, where the gap is
    } else {
        const size_t slot = static_cast<size_t>(head % kSlots);
        std::memcpy(&data_[slot * block_cap_], samples, static_cast<size_t>(n) * sizeof(dsp::cf));
        count_[slot] = n;
        dropped_[slot] = dropped;
        lost_[slot] = lost_pending_;
        lost_pending_ = 0;
        epoch_of_[slot] = epoch;
        head_.store(head + 1, std::memory_order_release);
    }
    const uint64_t one = 1;
    ssize_t w = ::write(wake_fd_, &one, sizeof one);
    (void)w;   // EAGAIN only if the counter is saturated: the worker is awake anyway
}

void AirspyhfSource::clear_wake() {
    uint64_t v = 0;
    ssize_t r = ::read(wake_fd_, &v, sizeof v);
    (void)r;
}

bool AirspyhfSource::pop(LiveBlock& out) {
    uint64_t tail = tail_.load(std::memory_order_relaxed);
    if (holding_) {   // release the previous view's slot only now: it was valid until this call
        tail_.store(++tail, std::memory_order_release);
        holding_ = false;
    }
    if (tail == head_.load(std::memory_order_acquire)) return false;
    const size_t slot = static_cast<size_t>(tail % kSlots);
    out.samples = &data_[slot * block_cap_];
    out.count = count_[slot];
    out.dropped = dropped_[slot];
    out.lost = lost_[slot];
    out.epoch = epoch_of_[slot];
    holding_ = true;
    return true;
}

uint32_t AirspyhfSource::new_epoch() { return epoch_.fetch_add(1, std::memory_order_acq_rel) + 1; }

bool AirspyhfSource::set_center(uint32_t hz) { return airspyhf_set_freq(dev_, hz) == AIRSPYHF_SUCCESS; }

bool AirspyhfSource::set_rate(uint32_t hz, std::string& err) {
    // A rate change is a full stop/start: not measured while streaming, so not attempted.
    if (airspyhf_stop(dev_) != AIRSPYHF_SUCCESS) {
        err = "airspyhf_stop failed";
        return false;
    }
    if (airspyhf_set_samplerate(dev_, hz) != AIRSPYHF_SUCCESS) {
        err = "device refused the sample rate";
        return false;
    }
    // Producer stopped and we are the consumer: safe to drop queued blocks (an older epoch anyway)
    // and to grow the slots if the driver's block size changed.
    const int out = airspyhf_get_output_size(dev_);
    if (out <= 0 || out > (1 << 16)) {
        err = "unexpected driver block size";
        return false;
    }
    if (static_cast<uint32_t>(out) > block_cap_) alloc_ring(static_cast<uint32_t>(out));
    tail_.store(head_.load(std::memory_order_acquire), std::memory_order_release);
    holding_ = false;
    lost_pending_ = 0;
    return start(err);
}

bool AirspyhfSource::set_hw(const HwSettings& hw) {
    return airspyhf_set_hf_agc(dev_, hw.hf_agc) == AIRSPYHF_SUCCESS &&
           airspyhf_set_hf_agc_threshold(dev_, hw.hf_agc_threshold) == AIRSPYHF_SUCCESS &&
           airspyhf_set_hf_lna(dev_, hw.hf_lna) == AIRSPYHF_SUCCESS &&
           airspyhf_set_hf_att(dev_, hw.hf_att) == AIRSPYHF_SUCCESS;
}

bool AirspyhfSource::streaming() const { return dev_ != nullptr && airspyhf_is_streaming(dev_) != 0; }

void AirspyhfSource::stop_and_close() {
    if (dev_ == nullptr) return;
    airspyhf_stop(dev_);
    airspyhf_close(dev_);
    dev_ = nullptr;
}

void AirspyhfSource::abandon() { dev_ = nullptr; }

}  // namespace k2
