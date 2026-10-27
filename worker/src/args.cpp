#include "args.hpp"

#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <limits>

#include "frame_v1.h"

namespace k2 {
namespace {

bool parse_u64(const char* s, uint64_t& out) {
    if (s == nullptr || *s == '\0' || *s == '-' || *s == '+' || *s == ' ') return false;
    errno = 0;
    char* end = nullptr;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0') return false;
    out = static_cast<uint64_t>(v);
    return true;
}

bool parse_i64(const char* s, int64_t& out) {
    if (s == nullptr || *s == '\0' || *s == '+' || *s == ' ') return false;
    errno = 0;
    char* end = nullptr;
    long long v = std::strtoll(s, &end, 10);
    if (errno != 0 || end == s || *end != '\0') return false;
    out = static_cast<int64_t>(v);
    return true;
}

template <typename T>
bool parse_bounded(const char* s, T& out, uint64_t lo, uint64_t hi) {
    uint64_t v = 0;
    if (!parse_u64(s, v) || v < lo || v > hi) return false;
    out = static_cast<T>(v);
    return true;
}

bool parse_serial(const char* s, uint64_t& out) {
    // 16 hex digits, optional 0x prefix (as airspyhf_info prints it).
    std::string str(s);
    if (str.size() > 2 && str[0] == '0' && (str[1] == 'x' || str[1] == 'X')) str = str.substr(2);
    if (str.empty() || str.size() > 16) return false;
    uint64_t v = 0;
    for (char c : str) {
        int d = (c >= '0' && c <= '9') ? c - '0' : (c >= 'a' && c <= 'f') ? c - 'a' + 10
              : (c >= 'A' && c <= 'F') ? c - 'A' + 10 : -1;
        if (d < 0) return false;
        v = (v << 4) | static_cast<uint64_t>(d);
    }
    if (v == 0) return false;
    out = v;
    return true;
}

bool parse_onoff(const char* s, uint8_t& out, const char* on, const char* off) {
    if (std::strcmp(s, on) == 0) { out = 1; return true; }
    if (std::strcmp(s, off) == 0) { out = 0; return true; }
    return false;
}

bool valid_device_id(const std::string& id) {
    if (id.empty() || id.size() > 64) return false;
    for (char c : id) {
        bool ok = (c >= 'a' && c <= 'z') || (c >= '0' && c <= '9') || c == '.' || c == '_' || c == '-';
        if (!ok) return false;
    }
    return id != "." && id != "..";
}

bool parse_format(const char* s, uint16_t& out) {
    if (std::strcmp(s, "ci16") == 0) { out = KSDR_FMT_CI16; return true; }
    if (std::strcmp(s, "cf32") == 0) { out = KSDR_FMT_CF32; return true; }
    if (std::strcmp(s, "ri16") == 0) { out = KSDR_FMT_RI16; return true; }
    if (std::strcmp(s, "rf32") == 0) { out = KSDR_FMT_RF32; return true; }
    return false;
}

// "INDEX" (gap unknown) or "INDEX:GAP"
bool parse_discontinuity(const char* s, DiscontinuityArg& out) {
    std::string str(s);
    auto colon = str.find(':');
    uint64_t idx = 0;
    if (colon == std::string::npos) {
        if (!parse_u64(str.c_str(), idx) || idx == 0) return false;
        out = {idx, std::nullopt};
        return true;
    }
    uint64_t gap = 0;
    std::string a = str.substr(0, colon), b = str.substr(colon + 1);
    if (!parse_u64(a.c_str(), idx) || idx == 0 || !parse_u64(b.c_str(), gap) || gap == 0) return false;
    out = {idx, gap};
    return true;
}

}  // namespace

void print_usage(const char* prog) {
    std::fprintf(stderr,
        "usage: %s --device-id ID --lock-dir DIR --receiver-idx N --stream-id N\n"
        "          --ctl-fd FD --data-fd FD --source replay --file PATH.sigmf-data\n"
        "          --format ci16|cf32|ri16|rf32 --rate-hz N --origin-hz N\n"
        "          [--discontinuity INDEX[:GAP]]... [--capture-gen-start N] [--frame-samples N]\n"
        "          [--paced] [--loop] [--start-after-ping]\n"
        "          [--iq-frames on|off] [--spectrum-fft N] [--spectrum-fps N]\n"
        "   or: %s ... --source airspyhf --serial HEX --rate-hz N --center-hz N\n"
        "          [--hf-agc on|off] [--hf-agc-threshold low|high] [--hf-lna on|off] [--hf-att 0..8]\n"
        "          [--capture-gen-start N] [--frame-samples N] [--iq-frames ...] [--spectrum-* ...]\n",
        prog,
        prog);
}

std::optional<Args> parse_args(int argc, char** argv) {
    Args a;
    bool have_rx = false, have_stream = false, have_origin = false, have_center = false;
    bool replay_only = false, live_only = false;
    auto fail = [&](const char* msg, const char* val) -> std::optional<Args> {
        std::fprintf(stderr, "ksdr-worker: %s%s%s\n", msg, val ? ": " : "", val ? val : "");
        return std::nullopt;
    };
    for (int i = 1; i < argc; ++i) {
        std::string k = argv[i];
        if (k == "--paced") { a.paced = true; replay_only = true; continue; }
        if (k == "--loop") { a.loop = true; replay_only = true; continue; }
        if (k == "--start-after-ping") { a.start_after_ping = true; continue; }
        if (i + 1 >= argc) return fail("missing value for", argv[i]);
        const char* v = argv[++i];
        if (k == "--device-id") {
            a.device_id = v;
            if (!valid_device_id(a.device_id)) return fail("invalid --device-id", v);
        } else if (k == "--lock-dir") {
            a.lock_dir = v;
            if (a.lock_dir.empty()) return fail("empty --lock-dir", nullptr);
        } else if (k == "--receiver-idx") {
            if (!parse_bounded(v, a.receiver_idx, 0, std::numeric_limits<uint32_t>::max())) return fail("bad --receiver-idx", v);
            have_rx = true;
        } else if (k == "--stream-id") {
            if (!parse_bounded(v, a.stream_id, 0, std::numeric_limits<uint32_t>::max())) return fail("bad --stream-id", v);
            have_stream = true;
        } else if (k == "--ctl-fd") {
            if (!parse_bounded(v, a.ctl_fd, 3, 1 << 20)) return fail("bad --ctl-fd", v);
        } else if (k == "--data-fd") {
            if (!parse_bounded(v, a.data_fd, 3, 1 << 20)) return fail("bad --data-fd", v);
        } else if (k == "--source") {
            a.source = v;
        } else if (k == "--file") {
            a.file = v;
            replay_only = true;
        } else if (k == "--format") {
            if (!parse_format(v, a.format)) return fail("bad --format", v);
            replay_only = true;
        } else if (k == "--serial") {
            if (!parse_serial(v, a.serial)) return fail("bad --serial (hex)", v);
            a.have_serial = live_only = true;
        } else if (k == "--center-hz") {
            if (!parse_i64(v, a.origin_hz) || a.origin_hz <= 0 || a.origin_hz > std::numeric_limits<uint32_t>::max())
                return fail("bad --center-hz (1..4294967295)", v);
            have_center = live_only = true;
        } else if (k == "--hf-agc") {
            if (!parse_onoff(v, a.hf_agc, "on", "off")) return fail("bad --hf-agc (on|off)", v);
            live_only = true;
        } else if (k == "--hf-agc-threshold") {
            if (!parse_onoff(v, a.hf_agc_threshold, "high", "low")) return fail("bad --hf-agc-threshold (low|high)", v);
            live_only = true;
        } else if (k == "--hf-lna") {
            if (!parse_onoff(v, a.hf_lna, "on", "off")) return fail("bad --hf-lna (on|off)", v);
            live_only = true;
        } else if (k == "--hf-att") {
            if (!parse_bounded(v, a.hf_att, 0, 8)) return fail("bad --hf-att (0..8)", v);
            live_only = true;
        } else if (k == "--rate-hz") {
            if (!parse_bounded(v, a.rate_hz, 1, std::numeric_limits<uint32_t>::max())) return fail("bad --rate-hz", v);
        } else if (k == "--origin-hz") {
            if (!parse_i64(v, a.origin_hz)) return fail("bad --origin-hz", v);
            have_origin = replay_only = true;
        } else if (k == "--discontinuity") {
            replay_only = true;
            DiscontinuityArg d{};
            if (!parse_discontinuity(v, d)) return fail("bad --discontinuity", v);
            if (!a.discontinuities.empty() && d.sample_index <= a.discontinuities.back().sample_index)
                return fail("--discontinuity values must be strictly increasing", v);
            a.discontinuities.push_back(d);
        } else if (k == "--capture-gen-start") {
            if (!parse_bounded(v, a.capture_gen_start, 1, (1ull << 53) - 1)) return fail("bad --capture-gen-start", v);
        } else if (k == "--iq-frames") {
            if (std::strcmp(v, "on") == 0) a.iq_frames = true;
            else if (std::strcmp(v, "off") == 0) a.iq_frames = false;
            else return fail("bad --iq-frames (on|off)", v);
        } else if (k == "--spectrum-fft") {
            if (!parse_bounded(v, a.spectrum_fft, 0, 1u << 16) ||
                (a.spectrum_fft != 0 && (a.spectrum_fft < 64 || (a.spectrum_fft & (a.spectrum_fft - 1)) != 0)))
                return fail("bad --spectrum-fft (0, or a power of two 64..65536)", v);
        } else if (k == "--spectrum-fps") {
            if (!parse_bounded(v, a.spectrum_fps, 1, 100)) return fail("bad --spectrum-fps (1..100)", v);
        } else if (k == "--frame-samples") {
            if (!parse_bounded(v, a.frame_samples, 1, 1u << 20)) return fail("bad --frame-samples", v);
        } else {
            return fail("unknown argument", argv[i - 1]);
        }
    }
    if (a.device_id.empty() || a.lock_dir.empty() || !have_rx || !have_stream || a.ctl_fd < 0 ||
        a.data_fd < 0 || a.rate_hz == 0)
        return fail("missing required arguments", nullptr);
    if (a.ctl_fd == a.data_fd) return fail("--ctl-fd and --data-fd must differ", nullptr);
    if (a.spectrum_fft != 0 && a.spectrum_fps > a.rate_hz) return fail("--spectrum-fps exceeds the sample rate", nullptr);
    if (a.source == "airspyhf") {
#ifndef K2_HAVE_AIRSPYHF
        return fail("this ksdr-worker was built without libairspyhf", nullptr);
#endif
        if (replay_only) return fail("--file/--format/--origin-hz/--discontinuity/--paced/--loop are replay-only", nullptr);
        if (!a.have_serial || !have_center) return fail("airspyhf needs --serial and --center-hz", nullptr);
        if (a.start_after_ping) return fail("--start-after-ping is replay-only", nullptr);
        a.format = KSDR_FMT_CF32;   // libairspyhf delivers complex float32
        return a;
    }
    if (a.source != "replay") return fail("unsupported --source", a.source.c_str());
    if (live_only) return fail("--serial/--center-hz/--hf-* are for --source airspyhf", nullptr);
    if (a.file.empty() || a.format == 0 || !have_origin) return fail("missing required arguments", nullptr);
    bool real = a.format == KSDR_FMT_RI16 || a.format == KSDR_FMT_RF32;
    if (!real && a.origin_hz < 0) return fail("complex --origin-hz (center) must be >= 0", nullptr);
    return a;
}

}  // namespace k2
