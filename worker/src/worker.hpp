// The worker event loop: one replay source, one control socket, one data socket.
//
// Replay semantics (M0):
// - capture_gen starts at --capture-gen-start (default 1); the first frame
//   carries NEW_GEN. The control service passes last_gen + 1 when it restarts
//   a worker, so a receiver's generation never goes backwards.
// - SET_CAPTURE is accepted only for the recording's own center and rate
//   (replay cannot honestly relabel samples as another frequency). An accepted
//   SET_CAPTURE is a re-acquisition: capture_gen increments, playback restarts
//   at file sample 0, first_sample_index restarts at 0, and the next frame
//   carries NEW_GEN | DISCONTINUITY. Readback is reported with verified=0
//   (no hardware involved).
// - Real-sample recordings have no tunable center: CENTER is unsupported.
// - Recorded discontinuities split frames so the frame starting at the
//   annotated sample carries DISCONTINUITY (+ GAP_KNOWN/gap_samples if known).
// - At end of file: END_OF_STREAM, or with --loop a wrap flagged
//   DISCONTINUITY (gap unknown) within the same generation.
//
// M1 DSP (worker/src/dsp, protocol: frame_v1 SPECTRUM/AUDIO, worker_ctl CHANNEL_*):
// - Every block of capture samples (the same blocks SAMPLES frames carry; --iq-frames off
//   suppresses those frames, not the processing) feeds the receiver's spectrum and every open
//   listener channel, in that order; frames follow in channel_id order. Deterministic on a
//   given build host.
// - A new capture generation rebuilds the spectrum and every channel: each channel gets a new
//   channel_gen and its next AUDIO frame carries NEW_GEN with first_sample_index 0.
// - A block that starts at a discontinuity marks the next SPECTRUM frame and each channel's next
//   AUDIO frame DISCONTINUITY.
// - --start-after-ping: no frames (and no pacing clock) until the first PING is answered; the
//   control service opens channels first, so where audio starts is deterministic.
#pragma once

#include <cstdint>
#include <map>
#include <memory>
#include <string>
#include <vector>

#include "args.hpp"
#include "dsp/channel.hpp"
#include "dsp/spectrum.hpp"
#include "io.hpp"
#include "replay_source.hpp"
#include "worker_ctl_v1.h"

namespace k2 {

// Process exit codes (sysexits.h values).
enum ExitCode : int {
    kExitOk = 0,
    kExitUsage = 64,
    kExitBadMessage = 65,  // control sent a malformed message
    kExitNoInput = 66,     // replay file unusable
    kExitInternal = 70,
    kExitLockError = 73,
    kExitIo = 74,          // data or control socket failed
    kExitBusy = 75,        // device lock held by another process
};

class Worker {
public:
    Worker(const Args& args, ReplaySource& src);
    ~Worker();
    int run();  // returns an ExitCode

private:
    struct ChannelState {
        kctl_channel_v1 cfg{};               // resolved (defaults filled in, flags 0)
        std::unique_ptr<dsp::Channel> dsp;
        uint64_t channel_gen = 0;
        uint64_t audio_index = 0;            // first_sample_index of the next AUDIO frame
        uint32_t pending_flags = 0;
    };

    void build_frame_if_due(uint64_t now_ns);
    void append_frame(uint16_t kind, uint64_t start, uint32_t count);
    void write_header(uint16_t kind, uint16_t format, uint32_t flags, uint64_t first_index, int64_t origin,
                      uint64_t gap, uint32_t stream_id, uint32_t rate, uint32_t count, uint16_t header_len);
    void process_dsp(uint64_t start, uint32_t count, uint32_t block_flags);
    void reset_dsp_for_new_generation();
    void handle_channel(uint16_t type, uint64_t seq, const uint8_t* payload);
    uint32_t resolve_channel(const kctl_channel_v1& in, kctl_channel_v1& out) const;
    std::unique_ptr<dsp::Channel> build_channel(const kctl_channel_v1& cfg) const;  // throws invalid_argument
    void send_channel_applied(uint64_t seq, const ChannelState& ch, bool open);
    bool handle_ctl_input();          // false => fatal protocol error
    void handle_set_capture(uint64_t seq, const uint8_t* payload);
    void send_ctl(uint16_t type, uint64_t seq, const void* payload, uint32_t len);
    void send_status(uint32_t state, uint32_t result);
    void begin_generation();
    uint64_t frame_deadline_ns(uint32_t count) const;
    int finish(int code);             // flush best-effort, return code

    const Args& a_;
    ReplaySource& src_;
    OutBuf ctl_out_, data_out_;
    InBuf ctl_in_;

    uint64_t gen_;
    uint64_t seq_ = 0;                // frame counter, never resets
    uint64_t file_pos_ = 0;           // next file sample to send
    uint64_t delivered_in_gen_ = 0;   // first_sample_index of the next frame
    uint64_t total_delivered_ = 0;    // pacing timebase across generations
    size_t next_disc_ = 0;            // index into a_.discontinuities
    uint32_t pending_flags_ = 0;
    uint64_t pending_gap_ = 0;
    bool eos_sent_ = false;
    bool stopping_ = false;
    uint64_t last_cmd_seq_ = 0;
    uint64_t t0_ns_ = 0;
    bool held_ = false;               // --start-after-ping: waiting for the first PING

    // DSP
    dsp::SampleFormat dsp_fmt_;
    std::unique_ptr<dsp::Spectrum> spectrum_;
    uint32_t spectrum_pending_ = 0;
    std::map<uint32_t, ChannelState> channels_;
    uint64_t next_channel_gen_ = 1;
    std::vector<dsp::cf> cbuf_;
    std::vector<dsp::SpectrumFrame> sframes_;
    std::vector<int16_t> abuf_;
};

}  // namespace k2
