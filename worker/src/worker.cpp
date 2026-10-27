#include "worker.hpp"

#include <poll.h>
#include <time.h>

#include <algorithm>
#include <cerrno>
#include <climits>
#include <cmath>
#include <csignal>
#include <cstdio>
#include <cstring>
#include <stdexcept>

#include "frame_v1.h"
#include "worker_ctl_v1.h"

namespace k2 {

volatile std::sig_atomic_t g_stop_signal = 0;

namespace {

uint64_t now_ns() {
    timespec ts{};
    ::clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
}

uint64_t realtime_ns() {
    timespec ts{};
    ::clock_gettime(CLOCK_REALTIME, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
}

bool is_real(uint16_t fmt) { return fmt == KSDR_FMT_RI16 || fmt == KSDR_FMT_RF32; }

dsp::SampleFormat to_dsp_format(uint16_t fmt) {
    switch (fmt) {
        case KSDR_FMT_CI16: return dsp::SampleFormat::CI16;
        case KSDR_FMT_CF32: return dsp::SampleFormat::CF32;
        case KSDR_FMT_RI16: return dsp::SampleFormat::RI16;
        default: return dsp::SampleFormat::RF32;
    }
}

bool to_dsp_mode(uint16_t m, dsp::Mode& out) {
    switch (m) {
        case KCTL_MODE_AM: out = dsp::Mode::AM; return true;
        case KCTL_MODE_USB: out = dsp::Mode::USB; return true;
        case KCTL_MODE_LSB: out = dsp::Mode::LSB; return true;
        case KCTL_MODE_CW: out = dsp::Mode::CW; return true;
        case KCTL_MODE_NFM: out = dsp::Mode::NFM; return true;
        case KCTL_MODE_WFM: out = dsp::Mode::WFM; return true;
        default: return false;
    }
}

bool to_dsp_agc(uint16_t a, dsp::AgcMode& out) {
    switch (a) {
        case KCTL_AGC_OFF: out = dsp::AgcMode::Off; return true;
        case KCTL_AGC_FAST: out = dsp::AgcMode::Fast; return true;
        case KCTL_AGC_MEDIUM: out = dsp::AgcMode::Medium; return true;
        case KCTL_AGC_SLOW: out = dsp::AgcMode::Slow; return true;
        default: return false;
    }
}

constexpr int32_t kDefaultBfoHz = 700;
// Live source (docs/ops/airspyhf-measurements-2026-10-26.md, "Consequences for M1"):
constexpr uint32_t kRetuneDiscardBlocks = 2;            // the step lands up to ~1 block after the call
constexpr uint64_t kStallNs = 1000000000ull;            // no driver block for this long: device lost
constexpr int kLiveHealthPollMs = 100;
constexpr size_t kMaxDataBacklog = 8u << 20;            // bytes queued for the data socket
constexpr float kSpectrumFloorDb = -127.5f;
constexpr float kSpectrumStepDb = 0.5f;

}  // namespace

Worker::Worker(const Args& args, ReplaySource* replay, LiveSource* live)
    : a_(args), replay_(replay), live_(live), gen_(args.capture_gen_start), center_hz_(args.origin_hz),
      rate_hz_(args.rate_hz), dsp_fmt_(to_dsp_format(args.format)) {
    hw_.hf_agc = args.hf_agc;
    hw_.hf_agc_threshold = args.hf_agc_threshold;
    hw_.hf_lna = args.hf_lna;
    hw_.hf_att = args.hf_att;
}

Worker::~Worker() = default;

uint64_t Worker::frame_deadline_ns(uint32_t count) const {
    // Real hardware delivers a frame once its last sample has been captured.
    // samples -> ns without overflow: whole seconds plus remainder (remainder < rate <= 2^32,
    // so remainder * 1e9 < 4.3e18 fits in 64 bits).
    const uint64_t samples = total_delivered_ + count;
    const uint64_t secs = samples / rate_hz_;
    const uint64_t rem = samples % rate_hz_;
    return t0_ns_ + secs * 1000000000ull + rem * 1000000000ull / rate_hz_;
}

void Worker::begin_generation() {
    file_pos_ = 0;
    delivered_in_gen_ = 0;
    next_disc_ = 0;
    total_delivered_ = 0;
    t0_ns_ = now_ns();
    eos_sent_ = false;
}

void Worker::write_header(uint16_t kind, uint16_t format, uint32_t flags, uint64_t first_index, int64_t origin,
                          uint64_t gap, uint32_t stream_id, uint32_t rate, uint32_t count, uint16_t header_len) {
    ksdr_frame_header_v1 h{};
    h.magic = KSDR_FRAME_MAGIC;
    h.version = KSDR_FRAME_VERSION;
    h.header_len = header_len;
    h.kind = kind;
    h.format = format;
    h.flags = flags;
    h.capture_gen = gen_;
    h.seq = seq_++;
    h.first_sample_index = first_index;
    h.freq_origin_hz = origin;
    h.host_time_ns = realtime_ns();
    h.gap_samples = gap;
    h.receiver_idx = a_.receiver_idx;
    h.stream_id = stream_id;
    h.sample_rate_hz = rate;
    h.sample_count = count;
    data_out_.append(&h, sizeof h);
}

// One block of capture samples: SAMPLES frame (unless --iq-frames off), then the DSP outputs.
// END_OF_STREAM goes through here with count 0 and no DSP.
void Worker::append_frame(uint16_t kind, const uint8_t* data, uint32_t count) {
    const uint32_t flags = pending_flags_;
    const uint64_t gap = (pending_flags_ & KSDR_F_GAP_KNOWN) ? pending_gap_ : 0;
    if (kind != KSDR_KIND_SAMPLES || a_.iq_frames) {
        write_header(kind, a_.format, flags, delivered_in_gen_, center_hz_, gap, a_.stream_id, rate_hz_, count,
                     KSDR_FRAME_HEADER_LEN_V1);
        if (count > 0) {
            data_out_.append(data, static_cast<size_t>(count) * ksdr_bytes_per_sample(a_.format));
        }
    }
    if (kind == KSDR_KIND_SAMPLES && count > 0) process_dsp(data, count, flags);
    pending_flags_ = 0;
    pending_gap_ = 0;
    delivered_in_gen_ += count;
    total_delivered_ += count;
}

void Worker::reset_dsp_for_new_generation() {
    if (a_.spectrum_fft) {
        dsp::SpectrumConfig sc;
        sc.rate_hz = rate_hz_;
        sc.fft_n = a_.spectrum_fft;
        sc.frames_per_s = a_.spectrum_fps;
        sc.db_floor = kSpectrumFloorDb;
        sc.db_step = kSpectrumStepDb;
        spectrum_ = std::make_unique<dsp::Spectrum>(sc);
        spectrum_pending_ = KSDR_F_NEW_GEN;
    }
    for (auto it = channels_.begin(); it != channels_.end();) {
        ChannelState& ch = it->second;
        try {
            ch.dsp = build_channel(ch.cfg);
        } catch (const std::invalid_argument& e) {
            // Cannot happen for replay (same rate/centre); for hardware the control service re-tunes.
            std::fprintf(stderr, "ksdr-worker: channel %u dropped on re-acquisition: %s\n", it->first, e.what());
            it = channels_.erase(it);
            continue;
        }
        ch.channel_gen = next_channel_gen_++;
        ch.audio_index = 0;
        ch.pending_flags = KSDR_F_NEW_GEN;
        ++it;
    }
}

void Worker::process_dsp(const uint8_t* data, uint32_t count, uint32_t block_flags) {
    if (!spectrum_ && channels_.empty()) return;
    cbuf_.resize(count);
    dsp::to_complex(dsp_fmt_, data, count, cbuf_.data());
    const uint32_t disc = block_flags & KSDR_F_DISCONTINUITY;
    if (spectrum_) {
        spectrum_pending_ |= disc;
        sframes_.clear();
        spectrum_->push(cbuf_.data(), count, sframes_);
        for (const auto& f : sframes_) {
            write_header(KSDR_KIND_SPECTRUM, KSDR_FMT_U8DB, spectrum_pending_, f.end_sample, center_hz_, 0, 0,
                         rate_hz_, static_cast<uint32_t>(f.bins.size()), KSDR_SPECTRUM_HEADER_LEN);
            ksdr_spectrum_ext_v1 ext{};
            ext.db_floor_mdb = static_cast<int32_t>(std::lround(kSpectrumFloorDb * 1000.0f));
            ext.db_step_mdb = static_cast<uint32_t>(std::lround(kSpectrumStepDb * 1000.0f));
            ext.blocks = f.blocks;
            data_out_.append(&ext, sizeof ext);
            data_out_.append(f.bins.data(), f.bins.size());
            spectrum_pending_ = 0;
        }
    }
    for (auto& [id, ch] : channels_) {
        ch.pending_flags |= disc;
        abuf_.clear();
        ch.dsp->process(cbuf_.data(), count, abuf_);
        if (abuf_.empty()) continue;
        write_header(KSDR_KIND_AUDIO, KSDR_FMT_RI16, ch.pending_flags, ch.audio_index,
                     center_hz_ + ch.cfg.offset_hz, 0, id, dsp::kAudioRateHz,
                     static_cast<uint32_t>(abuf_.size()), KSDR_AUDIO_HEADER_LEN);
        ksdr_audio_ext_v1 ext{};
        ext.channel_gen = ch.channel_gen;
        ext.mode = ch.cfg.mode;
        ext.agc = ch.cfg.agc;
        const double lvl = std::clamp(static_cast<double>(ch.dsp->level_dbfs()) * 100.0, -2e9, 2e9);
        ext.level_cdb = static_cast<int32_t>(std::lround(lvl));
        ext.pass_lo_hz = ch.dsp->config().pass_lo_hz;   // the filter that is running
        ext.pass_hi_hz = ch.dsp->config().pass_hi_hz;
        data_out_.append(&ext, sizeof ext);
        data_out_.append(abuf_.data(), abuf_.size() * sizeof(int16_t));
        ch.audio_index += abuf_.size();
        ch.pending_flags = 0;
    }
}

// Validates enums/flags and fills in defaults. Returns KCTL_OK or a result code.
uint32_t Worker::resolve_channel(const kctl_channel_v1& in, kctl_channel_v1& out) const {
    if ((in.flags & ~KCTL_CH_DEFINED_MASK) || in.reserved != 0) return KCTL_E_BAD_MESSAGE;
    dsp::Mode m;
    dsp::AgcMode g;
    if (!to_dsp_mode(in.mode, m) || !to_dsp_agc(in.agc, g)) return KCTL_E_UNSUPPORTED;
    out = in;
    if (in.flags & KCTL_CH_DEFAULT_PASSBAND) dsp::Channel::default_passband(m, out.pass_lo_hz, out.pass_hi_hz);
    if (in.flags & KCTL_CH_DEFAULT_BFO) out.bfo_hz = kDefaultBfoHz;
    out.flags = 0;
    return KCTL_OK;
}

std::unique_ptr<dsp::Channel> Worker::build_channel(const kctl_channel_v1& c) const {
    dsp::ChannelConfig cc;
    cc.in_rate_hz = rate_hz_;
    cc.offset_hz = c.offset_hz;
    to_dsp_mode(c.mode, cc.mode);
    dsp::AgcMode g = dsp::AgcMode::Medium;
    to_dsp_agc(c.agc, g);
    cc.agc = dsp::AgcConfig::preset(g);
    cc.agc.manual_gain_db = static_cast<float>(c.manual_gain_cdb) / 100.0f;
    cc.default_passband = false;   // resolve_channel already applied DEFAULT_PASSBAND
    cc.pass_lo_hz = c.pass_lo_hz;
    cc.pass_hi_hz = c.pass_hi_hz;
    cc.bfo_hz = c.bfo_hz;
    return std::make_unique<dsp::Channel>(cc);
}

void Worker::send_channel_applied(uint64_t seq, const ChannelState& ch, bool open) {
    kctl_channel_applied_v1 ap{};
    ap.channel_gen = ch.channel_gen;
    ap.capture_gen = gen_;
    ap.channel = ch.cfg;
    if (ch.dsp) {   // report the filter that is running
        ap.channel.pass_lo_hz = ch.dsp->config().pass_lo_hz;
        ap.channel.pass_hi_hz = ch.dsp->config().pass_hi_hz;
    }
    ap.channel_rate_hz = ch.dsp ? ch.dsp->channel_rate_hz() : 0;
    ap.open = open ? 1u : 0u;
    send_ctl(KCTL_MSG_CHANNEL_APPLIED, seq, &ap, sizeof ap);
}

void Worker::handle_channel(uint16_t type, uint64_t seq, const uint8_t* payload) {
    kctl_ack_v1 ack{KCTL_OK, 0};
    auto reply_ack = [&] { send_ctl(KCTL_MSG_ACK, seq, &ack, sizeof ack); };

    if (type == KCTL_MSG_CHANNEL_CLOSE) {
        kctl_channel_close_v1 cl{};
        std::memcpy(&cl, payload, sizeof cl);
        auto it = channels_.find(cl.channel_id);
        if (cl.reserved != 0) ack.result = KCTL_E_BAD_MESSAGE;
        else if (it == channels_.end()) ack.result = KCTL_E_NOT_FOUND;
        reply_ack();
        if (ack.result != KCTL_OK) return;
        ChannelState closed = std::move(it->second);
        channels_.erase(it);
        send_channel_applied(seq, closed, false);
        return;
    }

    kctl_channel_v1 req{}, cfg{};
    std::memcpy(&req, payload, sizeof req);
    ack.result = resolve_channel(req, cfg);
    auto it = channels_.find(req.channel_id);
    if (ack.result == KCTL_OK) {
        if (type == KCTL_MSG_CHANNEL_OPEN && it != channels_.end()) ack.result = KCTL_E_BUSY;
        else if (type == KCTL_MSG_CHANNEL_OPEN && channels_.size() >= KCTL_MAX_CHANNELS) ack.result = KCTL_E_BUSY;
        else if (type == KCTL_MSG_CHANNEL_TUNE && it == channels_.end()) ack.result = KCTL_E_NOT_FOUND;
    }
    if (ack.result != KCTL_OK) {
        reply_ack();
        return;
    }

    if (type == KCTL_MSG_CHANNEL_TUNE) {
        ChannelState& ch = it->second;
        kctl_channel_v1 same = cfg;
        same.offset_hz = ch.cfg.offset_hz;
        if (std::memcmp(&same, &ch.cfg, sizeof same) == 0) {
            // Offset-only (or no) change: phase-continuous, same channel_gen.
            try {
                if (cfg.offset_hz != ch.cfg.offset_hz) ch.dsp->set_offset(cfg.offset_hz);
            } catch (const std::invalid_argument&) {
                ack.result = KCTL_E_OUT_OF_RANGE;
                reply_ack();
                return;
            }
            ch.cfg = cfg;
            reply_ack();
            send_channel_applied(seq, ch, true);
            return;
        }
    }

    std::unique_ptr<dsp::Channel> built;
    try {
        built = build_channel(cfg);
    } catch (const std::invalid_argument&) {
        ack.result = KCTL_E_OUT_OF_RANGE;
        reply_ack();
        return;
    }
    reply_ack();
    ChannelState& ch = channels_[cfg.channel_id];
    ch.cfg = cfg;
    ch.dsp = std::move(built);
    ch.channel_gen = next_channel_gen_++;
    ch.audio_index = 0;
    ch.pending_flags = KSDR_F_NEW_GEN;
    send_channel_applied(seq, ch, true);
}

void Worker::build_frame_if_due(uint64_t now) {
    if (live_ != nullptr || held_ || !data_out_.empty() || eos_sent_ || stopping_) return;
    const uint64_t n = replay_->sample_count();
    if (file_pos_ == n) {
        if (!a_.loop) {
            append_frame(KSDR_KIND_END_OF_STREAM, nullptr, 0);
            eos_sent_ = true;
            return;
        }
        file_pos_ = 0;
        next_disc_ = 0;
        pending_flags_ |= KSDR_F_DISCONTINUITY;  // wrap: gap length is not meaningful
    }
    // Work out this frame without changing state, so a paced "not due yet"
    // return leaves everything untouched.
    const auto& discs = a_.discontinuities;
    const bool at_disc = next_disc_ < discs.size() && discs[next_disc_].sample_index == file_pos_;
    const size_t following = at_disc ? next_disc_ + 1 : next_disc_;
    uint64_t limit = n;
    if (following < discs.size()) limit = std::min<uint64_t>(limit, discs[following].sample_index);
    auto count = static_cast<uint32_t>(std::min<uint64_t>(a_.frame_samples, limit - file_pos_));
    if (a_.paced && now < frame_deadline_ns(count)) return;

    if (at_disc) {
        pending_flags_ |= KSDR_F_DISCONTINUITY;
        if (discs[next_disc_].gap) {
            pending_flags_ |= KSDR_F_GAP_KNOWN;
            pending_gap_ = *discs[next_disc_].gap;
        }
        next_disc_ = following;
    }
    append_frame(KSDR_KIND_SAMPLES, replay_->at(file_pos_), count);
    file_pos_ += count;
}

// ------------------------------------------------------------------ live source

// A loss of `samples` before the next frame. Staged samples precede it and go out first, so the
// gap sits exactly at a frame boundary. GAP_KNOWN only while every pending gap is known.
void Worker::mark_gap(uint64_t samples, uint32_t extra_flags) {
    if (!staging_.empty()) emit_live(static_cast<uint32_t>(staging_.size()));
    const bool unknown = (pending_flags_ & KSDR_F_DISCONTINUITY) && !(pending_flags_ & KSDR_F_GAP_KNOWN);
    pending_flags_ |= KSDR_F_DISCONTINUITY | extra_flags;
    if (!unknown) {
        pending_flags_ |= KSDR_F_GAP_KNOWN;
        pending_gap_ += samples;
    }
}

void Worker::emit_live(uint32_t count) {
    if (data_out_.pending() > kMaxDataBacklog) {
        // The control service is not reading: drop this frame (and its DSP) rather than grow.
        staging_.erase(staging_.begin(), staging_.begin() + count);
        mark_gap(count, 0);
        return;
    }
    append_frame(KSDR_KIND_SAMPLES, reinterpret_cast<const uint8_t*>(staging_.data()), count);
    staging_.erase(staging_.begin(), staging_.begin() + count);
}

void Worker::live_block(const LiveBlock& b) {
    if (stopping_ || eos_sent_) return;
    if (b.epoch != epoch_) return;                    // captured before the latest retune
    if (discard_blocks_ > 0) {                        // may still straddle the retune
        --discard_blocks_;
        return;
    }
    if (b.lost) mark_gap(b.lost, 0);                  // the worker fell behind the driver
    if (b.dropped) mark_gap(b.dropped, KSDR_F_OVERFLOW);   // the driver/USB lost samples
    staging_.insert(staging_.end(), b.samples, b.samples + b.count);
    while (staging_.size() >= a_.frame_samples) emit_live(a_.frame_samples);
}

void Worker::drain_live() {
    live_->clear_wake();
    LiveBlock b;
    while (live_->pop(b)) live_block(b);
}

bool Worker::live_healthy(uint64_t now) const {
    if (!live_->streaming()) return false;
    const uint64_t last = live_->last_block_ns();
    return last == 0 || now <= last || now - last < kStallNs;
}

void Worker::handle_set_hw(uint64_t seq, const uint8_t* payload) {
    kctl_set_hw_v1 r{};
    std::memcpy(&r, payload, sizeof r);
    kctl_ack_v1 ack{KCTL_OK, 0};
    if (r.flags == 0 || (r.flags & ~KCTL_HW_DEFINED_MASK) || r.reserved != 0) ack.result = KCTL_E_BAD_MESSAGE;
    else if (live_ == nullptr) ack.result = KCTL_E_UNSUPPORTED;
    else if (((r.flags & KCTL_HW_AGC) && r.hf_agc > 1) || ((r.flags & KCTL_HW_AGC_THRESHOLD) && r.hf_agc_threshold > 1) ||
             ((r.flags & KCTL_HW_LNA) && r.hf_lna > 1) || ((r.flags & KCTL_HW_ATT) && r.hf_att > KCTL_HW_ATT_MAX))
        ack.result = KCTL_E_OUT_OF_RANGE;
    send_ctl(KCTL_MSG_ACK, seq, &ack, sizeof ack);
    if (ack.result != KCTL_OK) return;

    HwSettings next = hw_;
    if (r.flags & KCTL_HW_AGC) next.hf_agc = r.hf_agc;
    if (r.flags & KCTL_HW_AGC_THRESHOLD) next.hf_agc_threshold = r.hf_agc_threshold;
    if (r.flags & KCTL_HW_LNA) next.hf_lna = r.hf_lna;
    if (r.flags & KCTL_HW_ATT) next.hf_att = r.hf_att;
    if (!live_->set_hw(next)) {
        // Device state is now unknown: report, then leave on the device-loss path (the control
        // service restarts the worker, which sets every control again).
        kctl_failed_v1 f{KCTL_E_DEVICE, 0};
        send_ctl(KCTL_MSG_FAILED, seq, &f, sizeof f);
        fatal_exit_ = kExitDeviceLost;
        return;
    }
    hw_ = next;
    kctl_hw_applied_v1 ap{};
    ap.capture_gen = gen_;
    ap.hf_agc = hw_.hf_agc;
    ap.hf_agc_threshold = hw_.hf_agc_threshold;
    ap.hf_lna = hw_.hf_lna;
    ap.hf_att = hw_.hf_att;
    ap.verified = 0;
    send_ctl(KCTL_MSG_HW_APPLIED, seq, &ap, sizeof ap);
}

void Worker::send_ctl(uint16_t type, uint64_t seq, const void* payload, uint32_t len) {
    kctl_header_v1 h{};
    h.magic = KCTL_MAGIC;
    h.version = KCTL_VERSION;
    h.header_len = KCTL_HEADER_LEN_V1;
    h.msg_type = type;
    h.payload_len = len;
    h.cmd_seq = seq;
    ctl_out_.append(&h, sizeof h);
    if (len) ctl_out_.append(payload, len);
}

void Worker::send_status(uint32_t state, uint32_t result) {
    kctl_status_v1 s{};
    s.state = state;
    s.result = result;
    s.capture_gen = gen_;
    send_ctl(KCTL_MSG_STATUS, 0, &s, sizeof s);
}

void Worker::handle_set_capture(uint64_t seq, const uint8_t* payload) {
    kctl_set_capture_v1 sc{};
    std::memcpy(&sc, payload, sizeof sc);
    kctl_ack_v1 ack{};
    const int64_t new_center = (sc.flags & KCTL_SC_CENTER) ? sc.center_hz : center_hz_;
    const uint32_t new_rate = (sc.flags & KCTL_SC_RATE) ? sc.sample_rate_hz : rate_hz_;
    if (sc.flags == 0 || (sc.flags & ~KCTL_SC_DEFINED_MASK)) {
        ack.result = KCTL_E_BAD_MESSAGE;
    } else if (live_ != nullptr) {
        const auto& rates = live_->rates();
        if (new_center <= 0 || new_center > static_cast<int64_t>(UINT32_MAX)) ack.result = KCTL_E_OUT_OF_RANGE;
        else if (std::find(rates.begin(), rates.end(), new_rate) == rates.end()) ack.result = KCTL_E_UNSUPPORTED;
        else ack.result = KCTL_OK;
    } else if ((sc.flags & KCTL_SC_CENTER) && (is_real(a_.format) || sc.center_hz != center_hz_)) {
        ack.result = KCTL_E_UNSUPPORTED;
    } else if ((sc.flags & KCTL_SC_RATE) && sc.sample_rate_hz != rate_hz_) {
        ack.result = KCTL_E_UNSUPPORTED;
    } else {
        ack.result = KCTL_OK;
    }
    send_ctl(KCTL_MSG_ACK, seq, &ack, sizeof ack);
    if (ack.result != KCTL_OK) return;

    if (live_ != nullptr) {
        // A new tuning epoch first: every block captured before or during the change is
        // discarded, plus the first kRetuneDiscardBlocks of the new epoch (the step lands up to
        // about one block after the call, and the driver silently loses samples around it).
        staging_.clear();
        epoch_ = live_->new_epoch();
        discard_blocks_ = kRetuneDiscardBlocks;
        std::string err;
        bool ok = new_rate == rate_hz_ || live_->set_rate(new_rate, err);
        if (ok && (sc.flags & KCTL_SC_CENTER)) ok = live_->set_center(static_cast<uint32_t>(new_center));
        if (!ok) {
            std::fprintf(stderr, "ksdr-worker: retune failed: %s\n", err.empty() ? "driver refused" : err.c_str());
            kctl_failed_v1 f{KCTL_E_DEVICE, 0};
            send_ctl(KCTL_MSG_FAILED, seq, &f, sizeof f);
            fatal_exit_ = kExitDeviceLost;   // device state unknown: restart from scratch
            return;
        }
        center_hz_ = new_center;
        rate_hz_ = new_rate;
    }

    // Re-acquisition. Any frame already queued belongs to the old generation
    // and is flushed before frames of the new one.
    ++gen_;
    begin_generation();
    pending_flags_ = KSDR_F_NEW_GEN | KSDR_F_DISCONTINUITY;
    pending_gap_ = 0;
    reset_dsp_for_new_generation();

    kctl_applied_v1 ap{};
    ap.capture_gen = gen_;
    ap.center_hz = center_hz_;
    ap.sample_rate_hz = rate_hz_;
    ap.verified = 0;
    send_ctl(KCTL_MSG_APPLIED, seq, &ap, sizeof ap);
}

bool Worker::handle_ctl_input() {
    while (ctl_in_.size() >= KCTL_HEADER_LEN_V1) {
        kctl_header_v1 h{};
        std::memcpy(&h, ctl_in_.data(), sizeof h);
        if (h.magic != KCTL_MAGIC || h.version != KCTL_VERSION || h.header_len != KCTL_HEADER_LEN_V1 ||
            h.reserved0 != 0 || h.payload_len > KCTL_MAX_PAYLOAD) {
            return false;
        }
        uint32_t expect = 0;
        switch (h.msg_type) {
            case KCTL_MSG_SET_CAPTURE: expect = sizeof(kctl_set_capture_v1); break;
            case KCTL_MSG_SET_HW: expect = sizeof(kctl_set_hw_v1); break;
            case KCTL_MSG_STOP:
            case KCTL_MSG_PING: expect = 0; break;
            case KCTL_MSG_CHANNEL_OPEN:
            case KCTL_MSG_CHANNEL_TUNE: expect = sizeof(kctl_channel_v1); break;
            case KCTL_MSG_CHANNEL_CLOSE: expect = sizeof(kctl_channel_close_v1); break;
            default: return false;  // worker->control types, or unknown
        }
        if (h.payload_len != expect || h.cmd_seq == 0 || h.cmd_seq <= last_cmd_seq_) return false;
        if (ctl_in_.size() < KCTL_HEADER_LEN_V1 + h.payload_len) return true;  // wait for the rest
        const uint8_t* payload = ctl_in_.data() + KCTL_HEADER_LEN_V1;
        last_cmd_seq_ = h.cmd_seq;

        if (stopping_) {
            // After STOP is accepted no further commands are executed.
            kctl_ack_v1 ack{KCTL_E_BUSY, 0};
            if (h.msg_type == KCTL_MSG_PING) send_ctl(KCTL_MSG_PONG, h.cmd_seq, nullptr, 0);
            else send_ctl(KCTL_MSG_ACK, h.cmd_seq, &ack, sizeof ack);
        } else if (h.msg_type == KCTL_MSG_PING) {
            send_ctl(KCTL_MSG_PONG, h.cmd_seq, nullptr, 0);
            if (held_) {
                held_ = false;
                t0_ns_ = now_ns();   // pacing starts now, not at process start
            }
        } else if (h.msg_type == KCTL_MSG_SET_CAPTURE) {
            handle_set_capture(h.cmd_seq, payload);
        } else if (h.msg_type == KCTL_MSG_SET_HW) {
            handle_set_hw(h.cmd_seq, payload);
        } else if (h.msg_type == KCTL_MSG_CHANNEL_OPEN || h.msg_type == KCTL_MSG_CHANNEL_TUNE ||
                   h.msg_type == KCTL_MSG_CHANNEL_CLOSE) {
            handle_channel(h.msg_type, h.cmd_seq, payload);
        } else {  // STOP
            kctl_ack_v1 ack{KCTL_OK, 0};
            send_ctl(KCTL_MSG_ACK, h.cmd_seq, &ack, sizeof ack);
            if (!eos_sent_) {
                append_frame(KSDR_KIND_END_OF_STREAM, nullptr, 0);
                eos_sent_ = true;
            }
            kctl_applied_v1 ap{};
            ap.capture_gen = gen_;
            ap.center_hz = center_hz_;
            ap.sample_rate_hz = rate_hz_;
            ap.verified = 0;
            send_ctl(KCTL_MSG_APPLIED, h.cmd_seq, &ap, sizeof ap);
            stopping_ = true;
        }
        ctl_in_.consume(KCTL_HEADER_LEN_V1 + h.payload_len);
    }
    return true;
}

int Worker::finish(int code) {
    // Best effort: give queued bytes up to ~1 s to drain, then exit.
    uint64_t deadline = now_ns() + 1000000000ull;
    while ((!ctl_out_.empty() || !data_out_.empty()) && now_ns() < deadline) {
        pollfd fds[2] = {{a_.ctl_fd, POLLOUT, 0}, {a_.data_fd, POLLOUT, 0}};
        if (::poll(fds, 2, 50) < 0 && errno != EINTR) break;
        if (!ctl_out_.empty() && !ctl_out_.flush(a_.ctl_fd)) break;
        if (!data_out_.empty() && !data_out_.flush(a_.data_fd)) break;
    }
    return code;
}

int Worker::run() {
    held_ = a_.start_after_ping;
    begin_generation();
    pending_flags_ = KSDR_F_NEW_GEN;
    reset_dsp_for_new_generation();
    if (live_ != nullptr) {
        epoch_ = live_->new_epoch();
        discard_blocks_ = kRetuneDiscardBlocks;   // the first blocks after start also settle
        std::string err;
        if (!live_->start(err)) {
            std::fprintf(stderr, "ksdr-worker: %s\n", err.c_str());
            send_status(KCTL_STATE_FAILED, KCTL_E_DEVICE);
            return finish(kExitDeviceLost);
        }
    }
    send_status(KCTL_STATE_RUNNING, KCTL_OK);

    for (;;) {
        if (fatal_exit_) {
            send_status(KCTL_STATE_FAILED, KCTL_E_DEVICE);
            return finish(fatal_exit_);
        }
        if (g_stop_signal) {
            if (!eos_sent_) {
                append_frame(KSDR_KIND_END_OF_STREAM, nullptr, 0);
                eos_sent_ = true;
            }
            send_status(KCTL_STATE_STOPPING, KCTL_OK);
            return finish(kExitOk);
        }
        if (stopping_ && ctl_out_.empty() && data_out_.empty()) return kExitOk;

        uint64_t now = now_ns();
        build_frame_if_due(now);

        int timeout_ms = -1;
        if (live_ != nullptr) {
            timeout_ms = kLiveHealthPollMs;      // samples wake us through the source's fd
        } else if (!held_ && data_out_.empty() && !eos_sent_ && !stopping_) {
            if (!a_.paced) {
                timeout_ms = 0;
            } else {
                const uint64_t n = replay_->sample_count();
                uint64_t remaining = file_pos_ < n ? n - file_pos_ : 0;
                auto count = static_cast<uint32_t>(std::min<uint64_t>(a_.frame_samples, std::max<uint64_t>(remaining, 1)));
                uint64_t dl = frame_deadline_ns(count);
                timeout_ms = dl > now ? static_cast<int>((dl - now + 999999) / 1000000) : 0;
            }
        }

        pollfd fds[3] = {
            {a_.ctl_fd, static_cast<short>(POLLIN | (ctl_out_.empty() ? 0 : POLLOUT)), 0},
            {a_.data_fd, static_cast<short>(data_out_.empty() ? 0 : POLLOUT), 0},
            {live_ != nullptr ? live_->wake_fd() : -1, POLLIN, 0},
        };
        int pr = ::poll(fds, live_ != nullptr ? 3 : 2, timeout_ms);
        if (pr < 0) {
            if (errno == EINTR) continue;
            std::perror("ksdr-worker: poll");
            return kExitInternal;
        }

        if (fds[0].revents & (POLLIN | POLLHUP | POLLERR)) {
            auto r = ctl_in_.fill(a_.ctl_fd);
            if (r == InBuf::ReadResult::Error) return kExitIo;
            if (!handle_ctl_input()) {
                std::fprintf(stderr, "ksdr-worker: malformed control message\n");
                send_status(KCTL_STATE_FAILED, KCTL_E_BAD_MESSAGE);
                return finish(kExitBadMessage);
            }
            if (r == InBuf::ReadResult::Eof) {
                // Control service is gone: stop and release the device.
                return kExitOk;
            }
        }
        if (live_ != nullptr) {
            drain_live();
            if (!stopping_ && !live_healthy(now_ns())) {
                // Unplugged or stalled. Report and leave; main() exits without stop/close (they
                // hang after device loss) and the control service restarts the worker.
                std::fprintf(stderr, "ksdr-worker: device stopped streaming\n");
                send_status(KCTL_STATE_FAILED, KCTL_E_DEVICE);
                return finish(kExitDeviceLost);
            }
        }
        if (!ctl_out_.empty() && !ctl_out_.flush(a_.ctl_fd)) return kExitIo;
        if (!data_out_.empty() && !data_out_.flush(a_.data_fd)) {
            std::fprintf(stderr, "ksdr-worker: data consumer closed\n");
            return kExitIo;
        }
    }
}

}  // namespace k2
