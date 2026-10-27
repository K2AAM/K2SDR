// ksdr-worker — K2AAM WebSDR acquisition worker: replay (M0) or Airspy HF+ (M1).
//
// Started by the control service with two already-connected Unix sockets
// (control and data). Takes the per-device lock first, then opens the source.
// Exits when told to STOP, on SIGTERM/SIGINT, when the control socket closes,
// or when the parent process dies.
//
// Live device exit paths (Window 1b, H7): after device loss libairspyhf's stop/close hang, so
// that path leaves with _exit() without calling them (the OS releases USB with the process).
// The normal path calls them under a watchdog that forces the exit if they do not return.

#include <signal.h>
#include <csignal>
#include <sys/prctl.h>
#include <unistd.h>

#include <chrono>
#include <cstdio>
#include <cstring>
#include <memory>
#include <string>
#include <thread>

#include "args.hpp"
#include "liquid_pin.hpp"
#include "device_lock.hpp"
#include "frame_v1.h"
#include "io.hpp"
#include "replay_source.hpp"
#include "worker.hpp"
#include "worker_ctl_v1.h"
#ifdef K2_HAVE_AIRSPYHF
#include "airspyhf_source.hpp"
#endif

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

#ifdef K2_HAVE_AIRSPYHF
    if (a.source == "airspyhf") {
        // Owned for the rest of the process: on the loss path it is deliberately never destroyed.
        auto* live = new k2::AirspyhfSource();
        k2::HwSettings hw;
        hw.hf_agc = a.hf_agc;
        hw.hf_agc_threshold = a.hf_agc_threshold;
        hw.hf_lna = a.hf_lna;
        hw.hf_att = a.hf_att;
        const uint32_t r = live->open(a.serial, a.rate_hz, static_cast<uint32_t>(a.origin_hz), hw, err);
        if (r != KCTL_OK) {
            std::fprintf(stderr, "ksdr-worker: airspyhf: %s\n", err.c_str());
            report_failure(a.ctl_fd, r);
            live->abandon();   // a half-open handle may be the hanging kind too
            ::_exit(r == KCTL_E_BUSY ? k2::kExitBusy : r == KCTL_E_UNSUPPORTED ? k2::kExitUsage : k2::kExitDeviceLost);
        }
        int code;
        {
            k2::Worker w(a, nullptr, live);
            code = w.run();
        }
        if (code == k2::kExitDeviceLost) {
            live->abandon();
            std::fflush(nullptr);
            ::_exit(code);
        }
        std::thread([] {
            std::this_thread::sleep_for(std::chrono::seconds(3));
            std::fprintf(stderr, "ksdr-worker: airspyhf stop/close did not return; exiting\n");
            ::_exit(k2::kExitStopHung);
        }).detach();
        live->stop_and_close();
        delete live;
        return code;
    }
#endif

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

    k2::Worker w(a, &src, nullptr);
    return w.run();
}
