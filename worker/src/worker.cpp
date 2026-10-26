#include "worker.hpp"

#include <poll.h>
#include <time.h>

#include <algorithm>
#include <cerrno>
#include <csignal>
#include <cstdio>
#include <cstring>

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

}  // namespace

uint64_t Worker::frame_deadline_ns(uint32_t count) const {
    // Real hardware delivers a frame once its last sample has been captured.
    // samples -> ns without overflow: whole seconds plus remainder (remainder < rate <= 2^32,
    // so remainder * 1e9 < 4.3e18 fits in 64 bits).
    const uint64_t samples = total_delivered_ + count;
    const uint64_t secs = samples / a_.rate_hz;
    const uint64_t rem = samples % a_.rate_hz;
    return t0_ns_ + secs * 1000000000ull + rem * 1000000000ull / a_.rate_hz;
}

void Worker::begin_generation() {
    file_pos_ = 0;
    delivered_in_gen_ = 0;
    next_disc_ = 0;
    total_delivered_ = 0;
    t0_ns_ = now_ns();
    eos_sent_ = false;
}

void Worker::append_frame(uint16_t kind, uint64_t start, uint32_t count) {
    ksdr_frame_header_v1 h{};
    h.magic = KSDR_FRAME_MAGIC;
    h.version = KSDR_FRAME_VERSION;
    h.header_len = KSDR_FRAME_HEADER_LEN_V1;
    h.kind = kind;
    h.format = a_.format;
    h.flags = pending_flags_;
    h.capture_gen = gen_;
    h.seq = seq_++;
    h.first_sample_index = delivered_in_gen_;
    h.freq_origin_hz = a_.origin_hz;
    h.host_time_ns = realtime_ns();
    h.gap_samples = (pending_flags_ & KSDR_F_GAP_KNOWN) ? pending_gap_ : 0;
    h.receiver_idx = a_.receiver_idx;
    h.stream_id = a_.stream_id;
    h.sample_rate_hz = a_.rate_hz;
    h.sample_count = count;
    data_out_.append(&h, sizeof h);
    if (count > 0) {
        data_out_.append(src_.at(start), static_cast<size_t>(count) * ksdr_bytes_per_sample(a_.format));
    }
    pending_flags_ = 0;
    pending_gap_ = 0;
    delivered_in_gen_ += count;
    total_delivered_ += count;
}

void Worker::build_frame_if_due(uint64_t now) {
    if (!data_out_.empty() || eos_sent_ || stopping_) return;
    const uint64_t n = src_.sample_count();
    if (file_pos_ == n) {
        if (!a_.loop) {
            append_frame(KSDR_KIND_END_OF_STREAM, 0, 0);
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
    append_frame(KSDR_KIND_SAMPLES, file_pos_, count);
    file_pos_ += count;
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
    if (sc.flags == 0 || (sc.flags & ~KCTL_SC_DEFINED_MASK)) {
        ack.result = KCTL_E_BAD_MESSAGE;
    } else if ((sc.flags & KCTL_SC_CENTER) && (is_real(a_.format) || sc.center_hz != a_.origin_hz)) {
        ack.result = KCTL_E_UNSUPPORTED;
    } else if ((sc.flags & KCTL_SC_RATE) && sc.sample_rate_hz != a_.rate_hz) {
        ack.result = KCTL_E_UNSUPPORTED;
    } else {
        ack.result = KCTL_OK;
    }
    send_ctl(KCTL_MSG_ACK, seq, &ack, sizeof ack);
    if (ack.result != KCTL_OK) return;

    // Re-acquisition. Any frame already queued belongs to the old generation
    // and is flushed before frames of the new one.
    ++gen_;
    begin_generation();
    pending_flags_ = KSDR_F_NEW_GEN | KSDR_F_DISCONTINUITY;
    pending_gap_ = 0;

    kctl_applied_v1 ap{};
    ap.capture_gen = gen_;
    ap.center_hz = a_.origin_hz;
    ap.sample_rate_hz = a_.rate_hz;
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
            case KCTL_MSG_STOP:
            case KCTL_MSG_PING: expect = 0; break;
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
        } else if (h.msg_type == KCTL_MSG_SET_CAPTURE) {
            handle_set_capture(h.cmd_seq, payload);
        } else {  // STOP
            kctl_ack_v1 ack{KCTL_OK, 0};
            send_ctl(KCTL_MSG_ACK, h.cmd_seq, &ack, sizeof ack);
            if (!eos_sent_) {
                append_frame(KSDR_KIND_END_OF_STREAM, 0, 0);
                eos_sent_ = true;
            }
            kctl_applied_v1 ap{};
            ap.capture_gen = gen_;
            ap.center_hz = a_.origin_hz;
            ap.sample_rate_hz = a_.rate_hz;
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
    begin_generation();
    pending_flags_ = KSDR_F_NEW_GEN;
    send_status(KCTL_STATE_RUNNING, KCTL_OK);

    for (;;) {
        if (g_stop_signal) {
            if (!eos_sent_) {
                append_frame(KSDR_KIND_END_OF_STREAM, 0, 0);
                eos_sent_ = true;
            }
            send_status(KCTL_STATE_STOPPING, KCTL_OK);
            return finish(kExitOk);
        }
        if (stopping_ && ctl_out_.empty() && data_out_.empty()) return kExitOk;

        uint64_t now = now_ns();
        build_frame_if_due(now);

        int timeout_ms = -1;
        if (data_out_.empty() && !eos_sent_ && !stopping_) {
            if (!a_.paced) {
                timeout_ms = 0;
            } else {
                const uint64_t n = src_.sample_count();
                uint64_t remaining = file_pos_ < n ? n - file_pos_ : 0;
                auto count = static_cast<uint32_t>(std::min<uint64_t>(a_.frame_samples, std::max<uint64_t>(remaining, 1)));
                uint64_t dl = frame_deadline_ns(count);
                timeout_ms = dl > now ? static_cast<int>((dl - now + 999999) / 1000000) : 0;
            }
        }

        pollfd fds[2] = {
            {a_.ctl_fd, static_cast<short>(POLLIN | (ctl_out_.empty() ? 0 : POLLOUT)), 0},
            {a_.data_fd, static_cast<short>(data_out_.empty() ? 0 : POLLOUT), 0},
        };
        int pr = ::poll(fds, 2, timeout_ms);
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
        if (!ctl_out_.empty() && !ctl_out_.flush(a_.ctl_fd)) return kExitIo;
        if (!data_out_.empty() && !data_out_.flush(a_.data_fd)) {
            std::fprintf(stderr, "ksdr-worker: data consumer closed\n");
            return kExitIo;
        }
    }
}

}  // namespace k2
