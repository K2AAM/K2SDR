// The worker event loop: one replay source, one control socket, one data socket.
//
// Replay semantics (M0):
// - capture_gen starts at 1; the first frame carries NEW_GEN.
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
#pragma once

#include <cstdint>
#include <string>

#include "args.hpp"
#include "io.hpp"
#include "replay_source.hpp"

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
    Worker(const Args& args, ReplaySource& src) : a_(args), src_(src) {}
    int run();  // returns an ExitCode

private:
    void build_frame_if_due(uint64_t now_ns);
    void append_frame(uint16_t kind, uint64_t start, uint32_t count);
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

    uint64_t gen_ = 1;
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
};

}  // namespace k2
