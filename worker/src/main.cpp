// ksdr-worker — K2AAM WebSDR acquisition worker (M0: replay only).
//
// Started by the control service with two already-connected Unix sockets
// (control and data). Takes the per-device lock first, then opens the source.
// Exits when told to STOP, on SIGTERM/SIGINT, when the control socket closes,
// or when the parent process dies.

#include <signal.h>
#include <csignal>
#include <sys/prctl.h>
#include <unistd.h>

#include <cstdio>
#include <cstring>
#include <string>

#include "args.hpp"
#include "liquid_pin.hpp"
#include "device_lock.hpp"
#include "frame_v1.h"
#include "io.hpp"
#include "replay_source.hpp"
#include "worker.hpp"
#include "worker_ctl_v1.h"

namespace k2 {
extern volatile std::sig_atomic_t g_stop_signal;
}

namespace {

void on_stop_signal(int) { k2::g_stop_signal = 1; }

// Used before the event loop exists: a single blocking-ish STATUS write.
void report_failure(int ctl_fd, uint32_t result) {
    kctl_header_v1 h{};
    h.magic = KCTL_MAGIC;
    h.version = KCTL_VERSION;
    h.header_len = KCTL_HEADER_LEN_V1;
    h.msg_type = KCTL_MSG_STATUS;
    h.payload_len = sizeof(kctl_status_v1);
    h.cmd_seq = 0;
    kctl_status_v1 s{};
    s.state = KCTL_STATE_FAILED;
    s.result = result;
    s.capture_gen = 0;
    k2::OutBuf out;
    out.append(&h, sizeof h);
    out.append(&s, sizeof s);
    out.flush(ctl_fd);
}

}  // namespace

int main(int argc, char** argv) {
    if (argc == 2 && std::strcmp(argv[1], "--version") == 0) {
        // liquid-dsp is linked statically from the pinned, project-built archive.
        std::printf("ksdr-worker frame_v%u worker_ctl_v%u liquid-dsp %s\n", static_cast<unsigned>(KSDR_FRAME_VERSION),
                    static_cast<unsigned>(KCTL_VERSION), liquid_libversion());
        return std::strcmp(liquid_libversion(), k2::kLiquidPinnedVersion) == 0 ? 0 : k2::kExitInternal;
    }
    // Die with the control service: never leave an orphan holding a device.
    if (::prctl(PR_SET_PDEATHSIG, SIGTERM) != 0) {
        std::perror("ksdr-worker: prctl");
        return k2::kExitInternal;
    }
    pid_t parent_at_start = ::getppid();
    if (parent_at_start == 1) return k2::kExitOk;  // parent already gone

    auto parsed = k2::parse_args(argc, argv);
    if (!parsed) {
        k2::print_usage(argv[0]);
        return k2::kExitUsage;
    }
    const k2::Args& a = *parsed;

    struct sigaction sa {};
    sa.sa_handler = on_stop_signal;
    sigemptyset(&sa.sa_mask);
    sa.sa_flags = 0;  // no SA_RESTART: poll() returns EINTR and the loop notices
    ::sigaction(SIGTERM, &sa, nullptr);
    ::sigaction(SIGINT, &sa, nullptr);
    ::signal(SIGPIPE, SIG_IGN);

    if (!k2::set_nonblocking(a.ctl_fd) || !k2::set_nonblocking(a.data_fd)) {
        std::fprintf(stderr, "ksdr-worker: --ctl-fd/--data-fd are not usable descriptors\n");
        return k2::kExitUsage;
    }

    // Ownership first: nothing touches the source until the lock is ours.
    k2::DeviceLock lock;
    std::string err;
    switch (lock.acquire(a.lock_dir, a.device_id, err)) {
        case k2::DeviceLock::Result::Acquired: break;
        case k2::DeviceLock::Result::Busy:
            std::fprintf(stderr, "ksdr-worker: device busy: %s\n", err.c_str());
            report_failure(a.ctl_fd, KCTL_E_BUSY);
            return k2::kExitBusy;
        case k2::DeviceLock::Result::Error:
            std::fprintf(stderr, "ksdr-worker: device lock: %s\n", err.c_str());
            report_failure(a.ctl_fd, KCTL_E_DEVICE);
            return k2::kExitLockError;
    }

    k2::ReplaySource src;
    if (!src.open(a.file, ksdr_bytes_per_sample(a.format), err)) {
        std::fprintf(stderr, "ksdr-worker: replay: %s\n", err.c_str());
        report_failure(a.ctl_fd, KCTL_E_DEVICE);
        return k2::kExitNoInput;
    }
    for (const auto& d : a.discontinuities) {
        if (d.sample_index >= src.sample_count()) {
            std::fprintf(stderr, "ksdr-worker: --discontinuity %llu beyond %llu samples\n",
                         static_cast<unsigned long long>(d.sample_index),
                         static_cast<unsigned long long>(src.sample_count()));
            report_failure(a.ctl_fd, KCTL_E_OUT_OF_RANGE);
            return k2::kExitUsage;
        }
    }

    k2::Worker w(a, src);
    return w.run();
}
