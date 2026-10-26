// Command-line arguments for ksdr-worker. The control service builds these
// from parsed SigMF metadata; the worker never parses JSON.
#pragma once

#include <cstdint>
#include <optional>
#include <string>
#include <vector>

namespace k2 {

struct DiscontinuityArg {
    uint64_t sample_index;           // file sample index of the first sample after the break
    std::optional<uint64_t> gap;     // samples lost, if known
};

struct Args {
    // Identity and ownership
    std::string device_id;           // lock name; [a-z0-9._-]{1,64}
    std::string lock_dir;            // directory holding <device_id>.lock
    uint32_t receiver_idx = 0;
    uint32_t stream_id = 0;

    // Inherited, already-connected sockets (socketpair ends) from the control service
    int ctl_fd = -1;
    int data_fd = -1;

    // Source: replay only in M0
    std::string source;              // "replay"
    std::string file;                // .sigmf-data path
    uint16_t format = 0;             // enum ksdr_sample_format
    uint32_t rate_hz = 0;
    int64_t origin_hz = 0;
    std::vector<DiscontinuityArg> discontinuities;  // strictly increasing

    uint32_t frame_samples = 4096;
    bool paced = false;              // real-time pacing; default is as fast as the consumer reads
    bool loop = false;               // wrap at end of file instead of END_OF_STREAM
};

// Parse argv strictly. On error, prints a message to stderr and returns nullopt.
std::optional<Args> parse_args(int argc, char** argv);

void print_usage(const char* prog);

}  // namespace k2
