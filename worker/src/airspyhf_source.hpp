// Airspy HF+ live source through native libairspyhf (M1 design §3.1). Built only with
// K2_HAVE_AIRSPYHF. Tests link the same code against worker/probe/mock_airspyhf.c.
#pragma once

#include <atomic>
#include <cstdint>
#include <string>
#include <vector>

#include "live_source.hpp"

struct airspyhf_device;

namespace k2 {

class AirspyhfSource final : public LiveSource {
public:
    AirspyhfSource();
    ~AirspyhfSource() override;
    AirspyhfSource(const AirspyhfSource&) = delete;
    AirspyhfSource& operator=(const AirspyhfSource&) = delete;

    uint32_t open(uint64_t serial, uint32_t rate_hz, uint32_t center_hz, const HwSettings& hw,
                  std::string& err) override;
    bool start(std::string& err) override;
    int wake_fd() const override { return wake_fd_; }
    void clear_wake() override;
    bool pop(LiveBlock& out) override;

    uint32_t new_epoch() override;
    bool set_center(uint32_t hz) override;
    bool set_rate(uint32_t hz, std::string& err) override;
    bool set_hw(const HwSettings& hw) override;
    const std::vector<uint32_t>& rates() const override { return rates_; }

    bool streaming() const override;
    uint64_t last_block_ns() const override { return last_block_ns_.load(std::memory_order_acquire); }

    void stop_and_close() override;
    void abandon() override;

    // Driver callback thread only: copy one block into the ring (never blocks).
    void deliver(const void* samples, int sample_count, uint64_t dropped);

private:
    void alloc_ring(uint32_t block_cap);

    airspyhf_device* dev_ = nullptr;
    int wake_fd_ = -1;
    std::vector<uint32_t> rates_;

    // SPSC ring of fixed-size slots. Producer: the driver's callback thread; consumer: the worker.
    static constexpr uint32_t kSlots = 512;   // >= 0.5 s at 912 kS/s with 1024-sample blocks
    uint32_t block_cap_ = 0;
    std::vector<dsp::cf> data_;
    std::vector<uint32_t> count_;
    std::vector<uint64_t> dropped_;
    std::vector<uint64_t> lost_;
    std::vector<uint32_t> epoch_of_;
    std::atomic<uint64_t> head_{0};   // written by the producer
    std::atomic<uint64_t> tail_{0};   // written by the consumer
    bool holding_ = false;            // consumer: the last popped slot is still being read
    uint64_t lost_pending_ = 0;       // producer only: ring-full loss not yet attached to a slot
    std::atomic<uint32_t> epoch_{0};
    std::atomic<uint64_t> last_block_ns_{0};
};

}  // namespace k2
