// ksdr-airspyhf-probe — Window 1 measurement and recording tool for the Airspy HF+.
//
// Targets the libairspyhf 1.6.x API shipped by Debian 13 (also builds with 1.8.x).
// Every subcommand prints one JSON object per line on stdout; diagnostics go to
// stderr. It never touches OpenWebRX+; it opens the device only when asked and
// closes it before exiting.
//
//   info        [--serial HEX]
//   busy-check                              list devices and try to open each, nothing else
//   stream      --serial HEX --freq HZ --rate HZ --seconds N [gain options]
//   record      --serial HEX --freq HZ --rate HZ --seconds N --out BASE
//               [--retune HZ@SECONDS]... [gain options]
//   stream/record also take --timing-log FILE: one CSV row per callback (host CLOCK_MONOTONIC
//   ns at the callback, cumulative samples incl. that block, block size, library drop count)
//   and one per retune (call and return times). Used to measure samples lost on retune
//   (tools/window1/timing.py). Rows are kept in memory and written after streaming stops.
//
// gain options: --hf-agc 0|1 --hf-agc-threshold 0|1 --hf-att 0..8 --hf-lna 0|1 --lib-dsp 0|1
//
// `record` writes BASE.sigmf-data (cf32_le, the library's native format) and
// BASE.sigmf-meta (SigMF 1.2.6). core:sha512 is added afterwards by
// tools/window1/finalize.py so the hot path never hashes.
//
// Exit codes: 0 ok, 2 open failed, 3 configuration/start failed, 4 device lost
// while streaming, 5 recording failed (a data or metadata write, flush or close
// failed; the summary carries "recording_ok":false and the error). 5 wins over 4.
// Sample loss is annotated as a discontinuity: library drops (dropped_samples is
// per callback in libairspyhf 1.6.8, not cumulative) and blocks this tool had to
// discard because the writer fell behind.

#include <airspyhf.h>

#include <sys/resource.h>
#include <time.h>
#include <unistd.h>

#include <algorithm>
#include <atomic>
#include <cerrno>
#include <chrono>
#include <condition_variable>
#include <csignal>
#include <cstdint>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <deque>
#include <mutex>
#include <string>
#include <thread>
#include <vector>

namespace {

std::atomic<bool> g_interrupted{false};
void on_signal(int) { g_interrupted = true; }

uint64_t mono_ns() {
    timespec ts{};
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return static_cast<uint64_t>(ts.tv_sec) * 1000000000ull + static_cast<uint64_t>(ts.tv_nsec);
}

std::string utc_now_iso() {
    timespec ts{};
    clock_gettime(CLOCK_REALTIME, &ts);
    tm t{};
    gmtime_r(&ts.tv_sec, &t);
    char buf[64];
    std::snprintf(buf, sizeof buf, "%04d-%02d-%02dT%02d:%02d:%02d.%03ldZ", t.tm_year + 1900, t.tm_mon + 1,
                  t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, ts.tv_nsec / 1000000);
    return buf;
}

std::string json_escape(const std::string& s) {
    std::string o;
    for (char c : s) {
        switch (c) {
            case '"': o += "\\\""; break;
            case '\\': o += "\\\\"; break;
            case '\n': o += "\\n"; break;
            default:
                if (static_cast<unsigned char>(c) < 0x20) {
                    char b[8];
                    std::snprintf(b, sizeof b, "\\u%04x", c);
                    o += b;
                } else {
                    o += c;
                }
        }
    }
    return o;
}

[[noreturn]] void die(const char* msg) {
    std::fprintf(stderr, "ksdr-airspyhf-probe: %s\n", msg);
    std::exit(64);
}

bool parse_u64(const char* s, uint64_t& out) {
    if (!s || !*s || *s == '-' || *s == '+') return false;
    char* end = nullptr;
    errno = 0;
    unsigned long long v = std::strtoull(s, &end, 10);
    if (errno || *end) return false;
    out = v;
    return true;
}

struct Retune {
    uint64_t freq_hz;
    double at_s;
};

struct Opts {
    std::string cmd;
    bool have_serial = false;
    uint64_t serial = 0;
    uint64_t freq = 0, rate = 0, seconds = 0;
    std::string out;
    std::string timing_log;
    std::vector<Retune> retunes;
    int hf_agc = -1, hf_agc_threshold = -1, hf_att = -1, hf_lna = -1, lib_dsp = -1;
};

Opts parse(int argc, char** argv) {
    if (argc < 2) die("usage: info|busy-check|stream|record ... (see source header)");
    Opts o;
    o.cmd = argv[1];
    for (int i = 2; i < argc; ++i) {
        std::string k = argv[i];
        if (i + 1 >= argc) die(("missing value for " + k).c_str());
        const char* v = argv[++i];
        uint64_t n = 0;
        auto num = [&](uint64_t lo, uint64_t hi) {
            if (!parse_u64(v, n) || n < lo || n > hi) die(("bad value for " + k).c_str());
            return n;
        };
        if (k == "--serial") {
            char* end = nullptr;
            o.serial = std::strtoull(v, &end, 16);
            if (*end) die("bad --serial (hex)");
            o.have_serial = true;
        } else if (k == "--freq") o.freq = num(1, 400000000);
        else if (k == "--rate") o.rate = num(1, 100000000);
        else if (k == "--seconds") o.seconds = num(1, 86400);
        else if (k == "--out") o.out = v;
        else if (k == "--timing-log") o.timing_log = v;
        else if (k == "--hf-agc") o.hf_agc = static_cast<int>(num(0, 1));
        else if (k == "--hf-agc-threshold") o.hf_agc_threshold = static_cast<int>(num(0, 1));
        else if (k == "--hf-att") o.hf_att = static_cast<int>(num(0, 8));
        else if (k == "--hf-lna") o.hf_lna = static_cast<int>(num(0, 1));
        else if (k == "--lib-dsp") o.lib_dsp = static_cast<int>(num(0, 1));
        else if (k == "--retune") {
            std::string s = v;
            auto at = s.find('@');
            uint64_t f = 0;
            if (at == std::string::npos || !parse_u64(s.substr(0, at).c_str(), f)) die("bad --retune HZ@SECONDS");
            double t = std::atof(s.substr(at + 1).c_str());
            if (t <= 0) die("bad --retune time");
            o.retunes.push_back({f, t});
        } else die(("unknown option " + k).c_str());
    }
    std::sort(o.retunes.begin(), o.retunes.end(), [](const Retune& a, const Retune& b) { return a.at_s < b.at_s; });
    return o;
}

std::string serial_hex(uint64_t s) {
    char b[32];
    std::snprintf(b, sizeof b, "%016llX", static_cast<unsigned long long>(s));
    return b;
}

airspyhf_device_t* open_or_die(const Opts& o) {
    if (!o.have_serial) die("--serial is required (use `info` to list serials)");
    airspyhf_device_t* dev = nullptr;
    int r = airspyhf_open_sn(&dev, o.serial);
    if (r != AIRSPYHF_SUCCESS || !dev) {
        std::printf("{\"event\":\"open_failed\",\"serial\":\"%s\",\"result\":%d}\n", serial_hex(o.serial).c_str(), r);
        std::exit(2);
    }
    return dev;
}

void apply_settings(airspyhf_device_t* dev, const Opts& o) {
    auto report = [](const char* what, int value, int r) {
        std::printf("{\"event\":\"setting\",\"name\":\"%s\",\"value\":%d,\"result\":%d}\n", what, value, r);
    };
    if (o.lib_dsp >= 0) report("lib_dsp", o.lib_dsp, airspyhf_set_lib_dsp(dev, static_cast<uint8_t>(o.lib_dsp)));
    if (o.hf_agc >= 0) report("hf_agc", o.hf_agc, airspyhf_set_hf_agc(dev, static_cast<uint8_t>(o.hf_agc)));
    if (o.hf_agc_threshold >= 0)
        report("hf_agc_threshold", o.hf_agc_threshold,
               airspyhf_set_hf_agc_threshold(dev, static_cast<uint8_t>(o.hf_agc_threshold)));
    if (o.hf_att >= 0) report("hf_att", o.hf_att, airspyhf_set_hf_att(dev, static_cast<uint8_t>(o.hf_att)));
    if (o.hf_lna >= 0) report("hf_lna", o.hf_lna, airspyhf_set_hf_lna(dev, static_cast<uint8_t>(o.hf_lna)));
}

void configure_or_die(airspyhf_device_t* dev, const Opts& o) {
    if (!o.freq || !o.rate || !o.seconds) die("--freq, --rate and --seconds are required");
    int r = airspyhf_set_samplerate(dev, static_cast<uint32_t>(o.rate));
    std::printf("{\"event\":\"set_samplerate\",\"rate_hz\":%llu,\"result\":%d,\"low_if\":%d}\n",
                static_cast<unsigned long long>(o.rate), r, airspyhf_is_low_if(dev));
    if (r != AIRSPYHF_SUCCESS) std::exit(3);
    r = airspyhf_set_freq(dev, static_cast<uint32_t>(o.freq));
    std::printf("{\"event\":\"set_freq\",\"freq_hz\":%llu,\"result\":%d}\n", static_cast<unsigned long long>(o.freq), r);
    if (r != AIRSPYHF_SUCCESS) std::exit(3);
    apply_settings(dev, o);
}

// ------------------------------------------------------------------- info

int cmd_info(const Opts& o) {
    airspyhf_lib_version_t lv{};
    airspyhf_lib_version(&lv);
    std::printf("{\"event\":\"lib\",\"header_version\":\"%s\",\"runtime\":\"%u.%u.%u\"}\n", AIRSPYHF_VERSION,
                lv.major_version, lv.minor_version, lv.revision);
    uint64_t serials[16] = {};
    int n = airspyhf_list_devices(serials, 16);
    std::printf("{\"event\":\"devices\",\"count\":%d,\"serials\":[", n);
    for (int i = 0; i < n && i < 16; ++i) std::printf("%s\"%s\"", i ? "," : "", serial_hex(serials[i]).c_str());
    std::printf("]}\n");
    if (!o.have_serial) return 0;

    airspyhf_device_t* dev = open_or_die(o);
    char ver[255] = {};
    int r = airspyhf_version_string_read(dev, ver, sizeof ver - 1);
    std::printf("{\"event\":\"firmware\",\"result\":%d,\"version\":\"%s\"}\n", r, json_escape(ver).c_str());
    airspyhf_read_partid_serialno_t ps{};
    r = airspyhf_board_partid_serialno_read(dev, &ps);
    std::printf("{\"event\":\"partid\",\"result\":%d,\"part_id\":%u}\n", r, ps.part_id);
    int32_t ppb = 0;
    r = airspyhf_get_calibration(dev, &ppb);
    std::printf("{\"event\":\"calibration\",\"result\":%d,\"ppb\":%d}\n", r, ppb);

    uint32_t count = 0;
    airspyhf_get_samplerates(dev, &count, 0);
    std::vector<uint32_t> rates(count);
    if (count) airspyhf_get_samplerates(dev, rates.data(), count);
    std::printf("{\"event\":\"samplerates\",\"rates_hz\":[");
    for (size_t i = 0; i < rates.size(); ++i) std::printf("%s%u", i ? "," : "", rates[i]);
    std::printf("]}\n");
    for (uint32_t rate : rates) {
        int sr = airspyhf_set_samplerate(dev, rate);
        std::printf("{\"event\":\"samplerate_mode\",\"rate_hz\":%u,\"result\":%d,\"low_if\":%d,\"output_size\":%d}\n",
                    rate, sr, airspyhf_is_low_if(dev), airspyhf_get_output_size(dev));
    }
    airspyhf_close(dev);
    return 0;
}

// -------------------------------------------------------------- busy-check

int cmd_busy_check(const Opts&) {
    uint64_t serials[16] = {};
    int n = airspyhf_list_devices(serials, 16);
    std::printf("{\"event\":\"devices\",\"count\":%d}\n", n);
    for (int i = 0; i < n && i < 16; ++i) {
        airspyhf_device_t* dev = nullptr;
        uint64_t t0 = mono_ns();
        int r = airspyhf_open_sn(&dev, serials[i]);
        uint64_t dt = mono_ns() - t0;
        std::printf("{\"event\":\"open_attempt\",\"serial\":\"%s\",\"result\":%d,\"opened\":%s,\"ms\":%.1f}\n",
                    serial_hex(serials[i]).c_str(), r, dev ? "true" : "false", static_cast<double>(dt) / 1e6);
        if (dev) {
            airspyhf_close(dev);  // opened only to learn the answer; nothing configured or streamed
        }
    }
    return 0;
}

// ---------------------------------------------------------------- streaming

struct Block {
    std::vector<airspyhf_complex_float_t> samples;
    uint64_t dropped_before = 0;  // samples lost (library + queue overflow) just before this block
};

struct CbRec {
    uint64_t t_ns, samples_total, dropped;
    uint32_t count;
};

struct StreamState {
    // Per-callback timing (only when --timing-log): written by the callback thread alone,
    // read by main after airspyhf_stop() has joined that thread.
    std::vector<CbRec> cb_log;
    std::atomic<size_t> cb_n{0};
    std::atomic<uint64_t> callbacks{0}, samples{0}, dropped{0}, max_gap_ns{0}, last_cb_ns{0};
    std::atomic<uint64_t> queue_overflow_blocks{0}, queue_overflow_samples{0};
    bool recording = false;
    std::mutex mu;
    std::condition_variable cv;
    std::deque<Block> queue;
    size_t queue_limit = 512;
    // Guarded by mu. File-space position: samples handed to the writer so far, and
    // loss not yet attached to a block (it is annotated on the next queued block).
    uint64_t enqueued = 0;
    uint64_t pending_gap = 0;
};

int callback(airspyhf_transfer_t* t) {
    auto* st = static_cast<StreamState*>(t->ctx);
    uint64_t now = mono_ns();
    uint64_t last = st->last_cb_ns.exchange(now);
    if (last) {
        uint64_t gap = now - last;
        uint64_t prev = st->max_gap_ns.load();
        while (gap > prev && !st->max_gap_ns.compare_exchange_weak(prev, gap)) {
        }
    }
    st->callbacks++;
    const uint64_t total = st->samples.fetch_add(static_cast<uint64_t>(t->sample_count)) +
                           static_cast<uint64_t>(t->sample_count);
    if (!st->cb_log.empty()) {
        size_t i = st->cb_n.load(std::memory_order_relaxed);
        if (i < st->cb_log.size()) {
            st->cb_log[i] = {now, total, t->dropped_samples, static_cast<uint32_t>(t->sample_count)};
            st->cb_n.store(i + 1, std::memory_order_release);
        }
    }
    // libairspyhf 1.6.8 reports the samples lost immediately before *this* block
    // (dropped_buffers is reset each time a buffer is queued): a per-callback count.
    const uint64_t lost = t->dropped_samples;
    st->dropped += lost;
    if (st->recording) {
        std::lock_guard<std::mutex> lk(st->mu);
        st->pending_gap += lost;
        if (st->queue.size() >= st->queue_limit) {
            // Writer behind: this block is lost too; it becomes part of the next gap.
            st->queue_overflow_blocks++;
            st->queue_overflow_samples += static_cast<uint64_t>(t->sample_count);
            st->pending_gap += static_cast<uint64_t>(t->sample_count);
        } else {
            Block b;
            b.samples.assign(t->samples, t->samples + t->sample_count);
            b.dropped_before = st->pending_gap;
            st->pending_gap = 0;
            st->enqueued += b.samples.size();
            st->queue.push_back(std::move(b));
        }
        st->cv.notify_one();
    }
    return 0;
}

double cpu_seconds() {
    rusage ru{};
    getrusage(RUSAGE_SELF, &ru);
    return static_cast<double>(ru.ru_utime.tv_sec + ru.ru_stime.tv_sec) +
           static_cast<double>(ru.ru_utime.tv_usec + ru.ru_stime.tv_usec) / 1e6;
}

struct Annotation {
    uint64_t sample;
    std::string json;  // body without braces
};

// Records the first write failure (later ones are consequences of it).
struct WriteError {
    std::mutex mu;
    std::string what;
    void set(const std::string& step, int err) {
        std::lock_guard<std::mutex> lk(mu);
        if (what.empty()) what = step + ": " + std::strerror(err ? err : EIO);
    }
    bool failed() {
        std::lock_guard<std::mutex> lk(mu);
        return !what.empty();
    }
};

// Writes the SigMF metadata; returns false (and sets err) on any write, flush or close failure.
template <class Captures, class Annotations>
bool write_meta(const Opts& o, const std::string& started, const Captures& captures, const Annotations& annotations,
                WriteError& err) {
    const std::string path = o.out + ".sigmf-meta";
    FILE* meta = std::fopen(path.c_str(), "w");
    if (!meta) {
        err.set("open " + path, errno);
        return false;
    }
    std::fprintf(meta,
                 "{\n  \"global\": {\n    \"core:datatype\": \"cf32_le\",\n    \"core:sample_rate\": %llu,\n"
                 "    \"core:version\": \"1.2.6\",\n    \"core:recorder\": \"ksdr-airspyhf-probe (libairspyhf %s)\",\n"
                 "    \"core:hw\": \"Airspy HF+ serial %s\",\n"
                 "    \"core:description\": \"Window 1 recording; settings: hf_agc=%d hf_agc_threshold=%d hf_att=%d "
                 "hf_lna=%d lib_dsp=%d (-1 = library default)\",\n"
                 "    \"core:extensions\": [{\"name\": \"k2websdr\", \"version\": \"1.0.0\", \"optional\": false}]\n  },\n"
                 "  \"captures\": [\n",
                 static_cast<unsigned long long>(o.rate), AIRSPYHF_VERSION, serial_hex(o.serial).c_str(), o.hf_agc,
                 o.hf_agc_threshold, o.hf_att, o.hf_lna, o.lib_dsp);
    for (size_t i = 0; i < captures.size(); ++i) {
        std::fprintf(meta, "    {\"core:sample_start\": %llu, \"core:frequency\": %llu, \"core:datetime\": \"%s\"}%s\n",
                     static_cast<unsigned long long>(captures[i].sample),
                     static_cast<unsigned long long>(captures[i].freq),
                     i == 0 ? started.c_str() : captures[i].at.c_str(), i + 1 < captures.size() ? "," : "");
    }
    std::fprintf(meta, "  ],\n  \"annotations\": [\n");
    for (size_t i = 0; i < annotations.size(); ++i)
        std::fprintf(meta, "    {%s}%s\n", annotations[i].json.c_str(), i + 1 < annotations.size() ? "," : "");
    std::fprintf(meta, "  ]\n}\n");
    bool ok = true;
    if (std::ferror(meta)) { err.set("write " + path, errno); ok = false; }
    if (std::fflush(meta) != 0) { err.set("flush " + path, errno); ok = false; }
    if (fsync(fileno(meta)) != 0 && errno != EINVAL) { err.set("fsync " + path, errno); ok = false; }
    if (std::fclose(meta) != 0) { err.set("close " + path, errno); ok = false; }
    return ok;
}

int run_stream(const Opts& o, bool record) {
    airspyhf_device_t* dev = open_or_die(o);
    configure_or_die(dev, o);
    StreamState st;
    st.recording = record;

    FILE* data = nullptr;
    std::string data_path;
    std::vector<Annotation> annotations;
    struct Capture { uint64_t sample; uint64_t freq; std::string at; };
    std::vector<Capture> captures{{0, o.freq, ""}};
    uint64_t written = 0;
    WriteError werr;
    std::thread writer;
    std::atomic<bool> writer_stop{false};
    if (record) {
        if (o.out.empty()) die("--out is required for record");
        data_path = o.out + ".sigmf-data";
        data = std::fopen(data_path.c_str(), "wb");
        if (!data) die("cannot open output file");
        writer = std::thread([&] {
            bool failed = false;
            for (;;) {
                std::unique_lock<std::mutex> lk(st.mu);
                st.cv.wait(lk, [&] { return !st.queue.empty() || writer_stop.load(); });
                if (st.queue.empty() && writer_stop.load()) return;
                Block b = std::move(st.queue.front());
                st.queue.pop_front();
                lk.unlock();
                if (failed) continue;  // keep draining so the callback never stalls
                if (b.dropped_before && written) {
                    char buf[160];
                    std::snprintf(buf, sizeof buf,
                                  "\"core:sample_start\":%llu,\"core:sample_count\":0,\"core:label\":\"discontinuity\","
                                  "\"k2websdr:gap_samples\":%llu",
                                  static_cast<unsigned long long>(written), static_cast<unsigned long long>(b.dropped_before));
                    annotations.push_back({written, buf});
                }
                errno = 0;
                size_t n = std::fwrite(b.samples.data(), sizeof(airspyhf_complex_float_t), b.samples.size(), data);
                written += n;  // only what the C library accepted
                if (n != b.samples.size()) {
                    werr.set("write " + data_path, errno);
                    failed = true;
                    std::printf("{\"event\":\"write_failed\",\"written_samples\":%llu}\n",
                                static_cast<unsigned long long>(written));
                }
            }
        });
    }

    struct RetuneRec { uint64_t call_ns, return_ns, freq; int result; };
    std::vector<RetuneRec> retune_log;
    if (!o.timing_log.empty()) {
        // Room for every callback at the smallest block the library uses (1024) plus 25 %.
        size_t n = static_cast<size_t>(static_cast<double>(o.seconds) * static_cast<double>(o.rate) / 1024.0 * 1.25) + 1024;
        st.cb_log.resize(n);
    }
    std::string started = utc_now_iso();
    double cpu0 = cpu_seconds();
    int r = airspyhf_start(dev, callback, &st);
    std::printf("{\"event\":\"start\",\"result\":%d,\"utc\":\"%s\"}\n", r, started.c_str());
    if (r != AIRSPYHF_SUCCESS) {
        airspyhf_close(dev);
        return 3;
    }
    uint64_t t0 = mono_ns();
    size_t next_retune = 0;
    uint64_t last_report = t0;
    uint64_t prev_samples = 0, prev_dropped = 0;
    bool device_lost = false;
    while (!g_interrupted) {
        std::this_thread::sleep_for(std::chrono::milliseconds(20));
        uint64_t now = mono_ns();
        double elapsed = static_cast<double>(now - t0) / 1e9;
        if (next_retune < o.retunes.size() && elapsed >= o.retunes[next_retune].at_s) {
            uint64_t at_sample = st.samples.load();
            uint64_t tc = mono_ns();
            uint64_t at_file_sample = 0;
            {
                std::lock_guard<std::mutex> lk(st.mu);
                at_file_sample = st.enqueued;  // file-space index: excludes lost samples
            }
            int rr = airspyhf_set_freq(dev, static_cast<uint32_t>(o.retunes[next_retune].freq_hz));
            uint64_t tr = mono_ns();
            double call_ms = static_cast<double>(tr - tc) / 1e6;
            retune_log.push_back({tc, tr, o.retunes[next_retune].freq_hz, rr});
            std::printf("{\"event\":\"retune\",\"freq_hz\":%llu,\"result\":%d,\"at_sample\":%llu,\"call_ms\":%.2f}\n",
                        static_cast<unsigned long long>(o.retunes[next_retune].freq_hz), rr,
                        static_cast<unsigned long long>(at_sample), call_ms);
            if (record && rr == AIRSPYHF_SUCCESS) {
                // A rejected retune leaves the device where it was, so it starts no new capture.
                // Index is the file position when set_freq was called; settling is measured offline.
                captures.push_back({at_file_sample, o.retunes[next_retune].freq_hz, utc_now_iso()});
            }
            ++next_retune;
        }
        if (now - last_report >= 1000000000ull) {
            uint64_t s = st.samples.load(), d = st.dropped.load();
            std::printf("{\"event\":\"tick\",\"t\":%.1f,\"samples\":%llu,\"rate_measured\":%.0f,\"dropped_total\":%llu,"
                        "\"dropped_new\":%llu,\"callbacks\":%llu,\"max_cb_gap_ms\":%.2f,\"streaming\":%d,"
                        "\"queue_overflow_blocks\":%llu}\n",
                        elapsed, static_cast<unsigned long long>(s),
                        static_cast<double>(s - prev_samples) / (static_cast<double>(now - last_report) / 1e9),
                        static_cast<unsigned long long>(d), static_cast<unsigned long long>(d - prev_dropped),
                        static_cast<unsigned long long>(st.callbacks.load()),
                        static_cast<double>(st.max_gap_ns.load()) / 1e6, airspyhf_is_streaming(dev),
                        static_cast<unsigned long long>(st.queue_overflow_blocks.load()));
            std::fflush(stdout);
            prev_samples = s;
            prev_dropped = d;
            last_report = now;
            if (!airspyhf_is_streaming(dev)) {
                device_lost = true;
                std::printf("{\"event\":\"streaming_stopped\",\"t\":%.1f}\n", elapsed);
                break;
            }
        }
        if (elapsed >= static_cast<double>(o.seconds)) break;
    }
    uint64_t t_end = mono_ns();
    int stop_r = airspyhf_stop(dev);
    double cpu = cpu_seconds() - cpu0;
    double wall = static_cast<double>(t_end - t0) / 1e9;
    int close_r = airspyhf_close(dev);
    bool recording_ok = true;
    if (record) {
        writer_stop = true;
        st.cv.notify_all();
        writer.join();
        if (std::fflush(data) != 0) werr.set("flush " + data_path, errno);
        // EINVAL: the target cannot be synced at all (a pipe); that is not lost data.
        if (fsync(fileno(data)) != 0 && errno != EINVAL) werr.set("fsync " + data_path, errno);
        if (std::fclose(data) != 0) werr.set("close " + data_path, errno);
        // Metadata is written only for complete data, so a failed recording can never
        // pass for a valid SigMF pair.
        if (!werr.failed()) write_meta(o, started, captures, annotations, werr);
        recording_ok = !werr.failed();
        if (!recording_ok) {
            std::remove((o.out + ".sigmf-meta").c_str());
            std::printf("{\"event\":\"recording_failed\",\"error\":\"%s\"}\n", json_escape(werr.what).c_str());
        }
    }
    std::printf("{\"event\":\"summary\",\"mode\":\"%s\",\"rate_hz\":%llu,\"freq_hz\":%llu,\"wall_s\":%.2f,"
                "\"samples\":%llu,\"rate_measured\":%.1f,\"dropped_total\":%llu,\"callbacks\":%llu,"
                "\"max_cb_gap_ms\":%.2f,\"cpu_s\":%.2f,\"cpu_pct\":%.1f,\"device_lost\":%s,\"stop_result\":%d,"
                "\"close_result\":%d,\"written_samples\":%llu,\"queue_overflow_blocks\":%llu,"
                "\"queue_overflow_samples\":%llu,\"captures\":%zu,\"recording_ok\":%s}\n",
                record ? "record" : "stream", static_cast<unsigned long long>(o.rate),
                static_cast<unsigned long long>(o.freq), wall, static_cast<unsigned long long>(st.samples.load()),
                static_cast<double>(st.samples.load()) / wall, static_cast<unsigned long long>(st.dropped.load()),
                static_cast<unsigned long long>(st.callbacks.load()), static_cast<double>(st.max_gap_ns.load()) / 1e6,
                cpu, 100.0 * cpu / wall, device_lost ? "true" : "false", stop_r, close_r,
                static_cast<unsigned long long>(written), static_cast<unsigned long long>(st.queue_overflow_blocks.load()),
                static_cast<unsigned long long>(st.queue_overflow_samples.load()), record ? captures.size() : 0,
                record ? (recording_ok ? "true" : "false") : "null");
    if (!o.timing_log.empty()) {
        FILE* f = std::fopen(o.timing_log.c_str(), "w");
        bool ok = f != nullptr;
        if (f) {
            std::fprintf(f, "kind,t_ns,a,b,c\n# cb: a=samples_total b=count c=dropped; retune: a=return_ns b=freq_hz c=result\n");
            const size_t n = st.cb_n.load(std::memory_order_acquire);
            for (size_t i = 0; i < n; ++i) {
                const CbRec& c = st.cb_log[i];
                std::fprintf(f, "cb,%llu,%llu,%u,%llu\n", static_cast<unsigned long long>(c.t_ns),
                             static_cast<unsigned long long>(c.samples_total), c.count,
                             static_cast<unsigned long long>(c.dropped));
            }
            for (const RetuneRec& rt : retune_log)
                std::fprintf(f, "retune,%llu,%llu,%llu,%d\n", static_cast<unsigned long long>(rt.call_ns),
                             static_cast<unsigned long long>(rt.return_ns), static_cast<unsigned long long>(rt.freq),
                             rt.result);
            ok = !std::ferror(f);
            ok = (std::fclose(f) == 0) && ok;
        }
        std::printf("{\"event\":\"timing_log\",\"path\":\"%s\",\"callbacks\":%zu,\"truncated\":%s,\"ok\":%s}\n",
                    json_escape(o.timing_log).c_str(), st.cb_n.load(),
                    st.cb_n.load() >= st.cb_log.size() ? "true" : "false", ok ? "true" : "false");
        if (!ok) return 5;
    }
    if (!recording_ok) return 5;
    return device_lost ? 4 : 0;
}

}  // namespace

int main(int argc, char** argv) {
    std::signal(SIGINT, on_signal);
    std::signal(SIGTERM, on_signal);
    std::signal(SIGXFSZ, SIG_IGN);  // a file-size limit must surface as a write error (EFBIG), not kill us
    std::setvbuf(stdout, nullptr, _IOLBF, 0);
    Opts o = parse(argc, argv);
    if (o.cmd == "info") return cmd_info(o);
    if (o.cmd == "busy-check") return cmd_busy_check(o);
    if (o.cmd == "stream") return run_stream(o, false);
    if (o.cmd == "record") return run_stream(o, true);
    die("unknown command");
}
