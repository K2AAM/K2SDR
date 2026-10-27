// ksdr-dsp-run — drives the worker's DSP classes (worker/src/dsp) on raw samples, for tests.
//
//   spectrum --format ci16|cf32|ri16|rf32 --rate HZ [--fft N] [--fps F]
//       stdin: raw samples   stdout: per frame  u64 end_sample | u32 blocks | N bytes (uint8 dB)
//   channel --format F --rate HZ --offset HZ --mode am|usb|lsb|cw|nfm|wfm
//           [--lo HZ --hi HZ] [--bfo HZ] [--agc off|fast|medium|slow] [--manual-gain DB]
//           [--target DBFS] [--retune SAMPLE:OFFSET_HZ]... [--levels PATH]
//       stdin: raw samples   stdout: int16 PCM, 12000 S/s mono
//       stderr: one JSON line with stats at the end; invalid config -> {"error":...}, exit 2
//   --block N (spectrum, channel): input chunk size in samples (default 4096); outputs must not
//   depend on it.
//   agc --mode M [--target DBFS] [--manual-gain DB]
//       stdin: f32 at 12000 S/s   stdout: f32
// All frequencies are integer Hz.

#include <cerrno>
#include <cinttypes>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <stdexcept>
#include <string>
#include <utility>
#include <vector>

#include "dsp/agc.hpp"
#include "dsp/channel.hpp"
#include "dsp/spectrum.hpp"
#include "dsp/types.hpp"

using namespace k2::dsp;

namespace {

[[noreturn]] void usage(const std::string& m) {
    std::fprintf(stderr, "ksdr-dsp-run: %s\n", m.c_str());
    std::exit(64);
}

int64_t parse_int(const char* s, const char* what) {
    errno = 0;
    char* end = nullptr;
    long long v = std::strtoll(s, &end, 10);
    if (errno || end == s || *end) usage(std::string("bad integer for ") + what + ": " + s);
    return v;
}

float parse_float(const char* s, const char* what) {
    char* end = nullptr;
    float v = std::strtof(s, &end);
    if (end == s || *end) usage(std::string("bad number for ") + what + ": " + s);
    return v;
}

SampleFormat parse_format(const std::string& s) {
    if (s == "ci16") return SampleFormat::CI16;
    if (s == "cf32") return SampleFormat::CF32;
    if (s == "ri16") return SampleFormat::RI16;
    if (s == "rf32") return SampleFormat::RF32;
    usage("bad --format");
}

AgcMode parse_agc(const std::string& s) {
    if (s == "off") return AgcMode::Off;
    if (s == "fast") return AgcMode::Fast;
    if (s == "medium") return AgcMode::Medium;
    if (s == "slow") return AgcMode::Slow;
    usage("bad --agc");
}

// Reads stdin in blocks and hands complex samples to `fn(samples, count, first_index)`.
template <class Fn>
void read_samples(SampleFormat fmt, size_t block, Fn&& fn) {
    const size_t bps = bytes_per_sample(fmt);
    std::vector<uint8_t> raw(block * bps);
    std::vector<cf> x(block);
    uint64_t index = 0;
    size_t have = 0;
    for (;;) {
        size_t got = std::fread(raw.data() + have, 1, raw.size() - have, stdin);
        have += got;
        const size_t n = have / bps;
        if (n == 0 && got == 0) break;
        if (n) {
            to_complex(fmt, raw.data(), n, x.data());
            fn(x.data(), n, index);
            index += n;
            const size_t rest = have - n * bps;
            std::memmove(raw.data(), raw.data() + n * bps, rest);
            have = rest;
        }
        if (got == 0) break;
    }
}

int cmd_spectrum(int argc, char** argv) {
    SpectrumConfig c;
    SampleFormat fmt = SampleFormat::CF32;
    size_t block = 4096;
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        if (i + 1 >= argc) usage("missing value for " + k);
        const char* v = argv[++i];
        if (k == "--format") fmt = parse_format(v);
        else if (k == "--rate") c.rate_hz = static_cast<uint32_t>(parse_int(v, "--rate"));
        else if (k == "--fft") c.fft_n = static_cast<uint32_t>(parse_int(v, "--fft"));
        else if (k == "--fps") c.frames_per_s = static_cast<uint32_t>(parse_int(v, "--fps"));
        else if (k == "--block") block = static_cast<size_t>(parse_int(v, "--block"));
        else usage("unknown option " + k);
    }
    Spectrum s(c);
    std::vector<SpectrumFrame> frames;
    read_samples(fmt, block, [&](const cf* x, size_t n, uint64_t) {
        frames.clear();
        s.push(x, n, frames);
        for (const auto& f : frames) {
            std::fwrite(&f.end_sample, 8, 1, stdout);
            std::fwrite(&f.blocks, 4, 1, stdout);
            std::fwrite(f.bins.data(), 1, f.bins.size(), stdout);
        }
    });
    return 0;
}

int cmd_channel(int argc, char** argv) {
    ChannelConfig c;
    SampleFormat fmt = SampleFormat::CF32;
    std::string agc = "medium", levels_path;
    float target = -6.0f, manual = 0.0f;
    bool have_mode = false;
    size_t block = 4096;
    std::vector<std::pair<uint64_t, int64_t>> retunes;
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        if (i + 1 >= argc) usage("missing value for " + k);
        const char* v = argv[++i];
        if (k == "--format") fmt = parse_format(v);
        else if (k == "--rate") c.in_rate_hz = static_cast<uint32_t>(parse_int(v, "--rate"));
        else if (k == "--offset") c.offset_hz = parse_int(v, "--offset");
        else if (k == "--mode") {
            if (!Channel::parse_mode(v, c.mode)) usage("bad --mode");
            have_mode = true;
        } else if (k == "--lo") c.pass_lo_hz = static_cast<int32_t>(parse_int(v, "--lo"));
        else if (k == "--hi") c.pass_hi_hz = static_cast<int32_t>(parse_int(v, "--hi"));
        else if (k == "--bfo") c.bfo_hz = static_cast<int32_t>(parse_int(v, "--bfo"));
        else if (k == "--agc") agc = v;
        else if (k == "--target") target = parse_float(v, "--target");
        else if (k == "--manual-gain") manual = parse_float(v, "--manual-gain");
        else if (k == "--levels") levels_path = v;
        else if (k == "--block") block = static_cast<size_t>(parse_int(v, "--block"));
        else if (k == "--retune") {
            std::string s = v;
            auto colon = s.find(':');
            if (colon == std::string::npos) usage("--retune SAMPLE:OFFSET_HZ");
            retunes.emplace_back(static_cast<uint64_t>(parse_int(s.substr(0, colon).c_str(), "--retune")),
                                 parse_int(s.substr(colon + 1).c_str(), "--retune"));
        } else usage("unknown option " + k);
    }
    if (!c.in_rate_hz || !have_mode) usage("channel needs --rate and --mode");
    c.agc = AgcConfig::preset(parse_agc(agc));
    c.agc.target_dbfs = target;
    c.agc.manual_gain_db = manual;
    Channel* ch = nullptr;
    try {
        ch = new Channel(c);
    } catch (const std::invalid_argument& e) {
        std::fprintf(stderr, "{\"error\":\"%s\"}\n", e.what());
        return 2;
    }
    FILE* lv = levels_path.empty() ? nullptr : std::fopen(levels_path.c_str(), "w");
    std::vector<int16_t> audio;
    size_t next_retune = 0;
    uint64_t next_level = c.in_rate_hz / 10;
    read_samples(fmt, block, [&](const cf* x, size_t n, uint64_t first) {
        size_t done = 0;
        while (done < n) {
            uint64_t idx = first + done;
            size_t take = n - done;
            if (next_retune < retunes.size() && retunes[next_retune].first < idx + take)
                take = static_cast<size_t>(retunes[next_retune].first > idx ? retunes[next_retune].first - idx : 0);
            if (take == 0) {
                ch->set_offset(retunes[next_retune].second);
                ++next_retune;
                continue;
            }
            audio.clear();
            ch->process(x + done, take, audio);
            std::fwrite(audio.data(), 2, audio.size(), stdout);
            done += take;
            if (lv && first + done >= next_level) {
                std::fprintf(lv, "{\"sample\":%" PRIu64 ",\"level_dbfs\":%.2f}\n", first + done, ch->level_dbfs());
                next_level += c.in_rate_hz / 10;
            }
        }
    });
    if (lv) std::fclose(lv);
    const ChannelStats& s = ch->stats();
    std::fprintf(stderr,
                 "{\"in_samples\":%" PRIu64 ",\"out_samples\":%" PRIu64 ",\"clipped\":%" PRIu64
                 ",\"channel_rate_hz\":%u,\"pass_lo_hz\":%d,\"pass_hi_hz\":%d,\"level_dbfs\":%.2f}\n",
                 s.in_samples, s.out_samples, s.clipped, ch->channel_rate_hz(), ch->config().pass_lo_hz,
                 ch->config().pass_hi_hz, ch->level_dbfs());
    delete ch;
    return 0;
}

int cmd_agc(int argc, char** argv) {
    std::string mode = "medium";
    float target = -6.0f, manual = 0.0f;
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        if (i + 1 >= argc) usage("missing value for " + k);
        const char* v = argv[++i];
        if (k == "--mode") mode = v;
        else if (k == "--target") target = parse_float(v, "--target");
        else if (k == "--manual-gain") manual = parse_float(v, "--manual-gain");
        else usage("unknown option " + k);
    }
    AgcConfig c = AgcConfig::preset(parse_agc(mode));
    c.target_dbfs = target;
    c.manual_gain_db = manual;
    Agc agc(c, kAudioRateHz);
    std::vector<float> x(4096);
    size_t n;
    while ((n = std::fread(x.data(), 4, x.size(), stdin)) > 0) {
        agc.process(x.data(), n);
        std::fwrite(x.data(), 4, n, stdout);
    }
    std::fprintf(stderr, "{\"latency_samples\":%u}\n", agc.latency_samples());
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) usage("usage: spectrum|channel|agc ... (see source header)");
    std::string cmd = argv[1];
    try {
        if (cmd == "spectrum") return cmd_spectrum(argc, argv);
        if (cmd == "channel") return cmd_channel(argc, argv);
        if (cmd == "agc") return cmd_agc(argc, argv);
    } catch (const std::invalid_argument& e) {
        std::fprintf(stderr, "{\"error\":\"%s\"}\n", e.what());
        return 2;
    }
    usage("unknown command " + cmd);
}
