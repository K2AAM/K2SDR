// ksdr-dsp-check — exercises the pinned liquid-dsp build (FFT, NCO, resampler, filters,
// demodulators, AGC) on samples piped through it, and benchmarks the M1 sizes.
//
// It is a verification harness for the dependency, not the worker's DSP pipeline: the
// chain below mirrors docs/design/M1-first-live-receiver.md §3.2 so each liquid component
// the worker will use is tested against numpy and against real recordings.
//
//   version
//   fft N                     stdin: frames of N cf32  -> stdout: N cf32 (forward, unnormalized)
//   chain --in-rate R --offset HZ --mode am|usb|lsb|cw|nfm|wfm
//         [--bw HZ] [--bfo HZ] [--agc off|fast|medium|slow] [--gain G]
//                             stdin: cf32 at R      -> stdout: f32 audio at 12000 S/s
//   agc --mode fast|medium|slow                stdin: f32 -> stdout: f32 (AGC applied)
//   bench [--seconds S]       JSON lines: FFT and per-channel chain cost

#include <algorithm>
#include <chrono>
#include <cmath>
#include <complex>
#include <cstdio>
#include <cstdlib>
#include <cstring>
#include <memory>
#include <string>
#include <vector>

#include "liquid_pin.hpp"   // includes <complex> before <liquid/liquid.h>

namespace {

using cf = std::complex<float>;
constexpr float kPi = 3.14159265358979323846f;
constexpr unsigned kAudioRate = 12000;

[[noreturn]] void die(const std::string& m) {
    std::fprintf(stderr, "ksdr-dsp-check: %s\n", m.c_str());
    std::exit(64);
}

template <class T>
size_t read_block(std::vector<T>& buf) {
    return std::fread(buf.data(), sizeof(T), buf.size(), stdin);
}

template <class T>
void write_all(const T* p, size_t n) {
    if (n && std::fwrite(p, sizeof(T), n, stdout) != n) die("write failed");
}

// ----------------------------------------------------------------------------- FFT

int cmd_fft(unsigned n) {
    if (n < 2 || n > (1u << 20)) die("bad FFT size");
    std::vector<cf> x(n), y(n);
    fftplan p = fft_create_plan(n, reinterpret_cast<liquid_float_complex*>(x.data()),
                                reinterpret_cast<liquid_float_complex*>(y.data()), LIQUID_FFT_FORWARD, 0);
    while (read_block(x) == n) {
        fft_execute(p);
        write_all(y.data(), n);
    }
    fft_destroy_plan(p);
    return 0;
}

// --------------------------------------------------------------------------- AGC

struct Agc {
    agc_rrrf q = nullptr;
    float fixed_gain = 1.0f;
    explicit Agc(const std::string& mode, float gain) : fixed_gain(gain) {
        float bt = 0;
        // Loop bandwidth -> measured settling after a +40 dB step at 12 kS/s (stays within 3 dB):
        // 3e-2: 0.04 s, 5e-3: 0.23 s, 1e-3: 1.16 s (a -40 dB step settles ~3x faster). liquid's AGC
        // is one symmetric loop: a sudden loud signal overshoots by +27..+40 dB until it catches up.
        if (mode == "fast") bt = 3e-2f;
        else if (mode == "medium") bt = 5e-3f;
        else if (mode == "slow") bt = 1e-3f;
        else if (mode != "off") die("bad --agc");
        if (bt > 0) {
            q = agc_rrrf_create();
            agc_rrrf_set_bandwidth(q, bt);
            agc_rrrf_set_scale(q, 0.25f);         // output level target (~ -12 dBFS)
        }
    }
    ~Agc() { if (q) agc_rrrf_destroy(q); }
    Agc(const Agc&) = delete;
    Agc& operator=(const Agc&) = delete;
    float operator()(float x) {
        if (!q) return x * fixed_gain;
        float y = 0;
        agc_rrrf_execute(q, x, &y);
        return y;
    }
};

int cmd_agc(const std::string& mode) {
    Agc agc(mode, 1.0f);
    std::vector<float> x(4096), y(4096);
    size_t n;
    while ((n = read_block(x)) > 0) {
        for (size_t i = 0; i < n; ++i) y[i] = agc(x[i]);
        write_all(y.data(), n);
    }
    return 0;
}

// ------------------------------------------------------------------------- chain

struct ChainOpts {
    unsigned in_rate = 0;
    double offset_hz = 0;
    std::string mode;
    double bw_hz = 0;      // 0: mode default
    double bfo_hz = 700;
    std::string agc = "off";
    float gain = 1.0f;
};

// Complex FIR selecting [lo, hi] Hz at rate fs: Kaiser low-pass of half the width, shifted.
firfilt_cccf make_bandpass(double lo, double hi, double fs, unsigned taps) {
    std::vector<float> h(taps);
    float fc = static_cast<float>((hi - lo) / 2.0 / fs);
    liquid_firdes_kaiser(taps, fc, 70.0f, 0.0f, h.data());
    float f0 = static_cast<float>((hi + lo) / 2.0 / fs);
    std::vector<liquid_float_complex> hc(taps);
    for (unsigned i = 0; i < taps; ++i) {
        float t = static_cast<float>(i) - static_cast<float>(taps - 1) / 2.0f;
        hc[i] = h[i] * std::polar(1.0f, 2.0f * kPi * f0 * t);
    }
    return firfilt_cccf_create(hc.data(), taps);
}

class Chain {
  public:
    explicit Chain(const ChainOpts& o) : o_(o), agc_(o.agc, o.gain) {
        const bool wfm = o.mode == "wfm";
        const bool nfm = o.mode == "nfm";
        ch_rate_ = wfm ? 192000.0 : nfm ? 24000.0 : static_cast<double>(kAudioRate);
        if (static_cast<double>(o.in_rate) < ch_rate_) die("input rate below the channel rate");
        double mix = o.offset_hz;
        if (o.mode == "cw") mix -= o.bfo_hz;              // carrier lands at +BFO in the channel
        nco_ = nco_crcf_create(LIQUID_NCO);
        nco_crcf_set_frequency(nco_, static_cast<float>(2.0 * M_PI * mix / o.in_rate));
        rs_ = msresamp_crcf_create(static_cast<float>(ch_rate_ / o.in_rate), 70.0f);
        const double fs = ch_rate_;
        if (o.mode == "usb") bp_ = make_bandpass(300, o.bw_hz > 0 ? 300 + o.bw_hz : 2700, fs, 161);
        else if (o.mode == "lsb") bp_ = make_bandpass(o.bw_hz > 0 ? -300 - o.bw_hz : -2700, -300, fs, 161);
        else if (o.mode == "cw") {
            double w = o.bw_hz > 0 ? o.bw_hz : 500;
            bp_ = make_bandpass(o.bfo_hz - w / 2, o.bfo_hz + w / 2, fs, 241);
        } else if (o.mode == "am") {
            double w = o.bw_hz > 0 ? o.bw_hz : 10000;
            bp_ = make_bandpass(-w / 2, w / 2, fs, 81);
            dc_ = iirfilt_rrrf_create_dc_blocker(1e-3f);
        } else if (nfm || wfm) {
            double dev = nfm ? 5000 : 75000;
            fm_ = freqdem_create(static_cast<float>(dev / fs));
            if (wfm) {                                     // 75 us de-emphasis, one pole at 192 kS/s
                float a = std::exp(-1.0f / (192000.0f * 75e-6f));
                float b[2] = {1.0f - a, 0.0f}, den[2] = {1.0f, -a};
                deemph_ = iirfilt_rrrf_create(b, 2, den, 2);
            }
            audio_rs_ = msresamp_rrrf_create(static_cast<float>(kAudioRate / fs), 70.0f);
        } else {
            die("bad --mode");
        }
    }
    ~Chain() {
        nco_crcf_destroy(nco_);
        msresamp_crcf_destroy(rs_);
        if (bp_) firfilt_cccf_destroy(bp_);
        if (dc_) iirfilt_rrrf_destroy(dc_);
        if (fm_) freqdem_destroy(fm_);
        if (deemph_) iirfilt_rrrf_destroy(deemph_);
        if (audio_rs_) msresamp_rrrf_destroy(audio_rs_);
    }
    Chain(const Chain&) = delete;
    Chain& operator=(const Chain&) = delete;

    // in: n samples at in_rate; appends 12 kS/s audio to out
    void process(const cf* in, size_t n, std::vector<float>& out) {
        mixed_.resize(n);
        for (size_t i = 0; i < n; ++i) {
            liquid_float_complex y;
            nco_crcf_mix_down(nco_, in[i], &y);
            nco_crcf_step(nco_);
            mixed_[i] = y;
        }
        ch_.resize(static_cast<size_t>(std::ceil(static_cast<double>(n) * ch_rate_ / o_.in_rate)) + 64);
        unsigned ny = 0;
        msresamp_crcf_execute(rs_, reinterpret_cast<liquid_float_complex*>(mixed_.data()),
                              static_cast<unsigned>(n), reinterpret_cast<liquid_float_complex*>(ch_.data()), &ny);
        if (fm_) {
            fmout_.resize(ny);
            for (unsigned i = 0; i < ny; ++i) {
                float m = 0;
                freqdem_demodulate(fm_, ch_[i], &m);
                if (deemph_) iirfilt_rrrf_execute(deemph_, m, &m);
                fmout_[i] = m;
            }
            aud_.resize(static_cast<size_t>(ny) * kAudioRate / static_cast<size_t>(ch_rate_) + 64);
            unsigned na = 0;
            msresamp_rrrf_execute(audio_rs_, fmout_.data(), ny, aud_.data(), &na);
            for (unsigned i = 0; i < na; ++i) out.push_back(agc_(aud_[i]));
            return;
        }
        for (unsigned i = 0; i < ny; ++i) {
            liquid_float_complex z;
            firfilt_cccf_push(bp_, ch_[i]);
            firfilt_cccf_execute(bp_, &z);
            float a;
            if (dc_) {                                    // AM: envelope, then DC removal
                iirfilt_rrrf_execute(dc_, std::abs(z), &a);
            } else {
                a = z.real();                              // SSB/CW: sideband already selected
            }
            out.push_back(agc_(a));
        }
    }

  private:
    ChainOpts o_;
    Agc agc_;
    double ch_rate_ = 0;
    nco_crcf nco_ = nullptr;
    msresamp_crcf rs_ = nullptr;
    firfilt_cccf bp_ = nullptr;
    iirfilt_rrrf dc_ = nullptr;
    freqdem fm_ = nullptr;
    iirfilt_rrrf deemph_ = nullptr;
    msresamp_rrrf audio_rs_ = nullptr;
    std::vector<cf> mixed_, ch_;
    std::vector<float> fmout_, aud_;
};

int cmd_chain(const ChainOpts& o) {
    Chain c(o);
    std::vector<cf> x(8192);
    std::vector<float> out;
    size_t n;
    while ((n = read_block(x)) > 0) {
        out.clear();
        c.process(x.data(), n, out);
        write_all(out.data(), out.size());
    }
    return 0;
}

// ------------------------------------------------------------------------- bench

double now_s() {
    return std::chrono::duration<double>(std::chrono::steady_clock::now().time_since_epoch()).count();
}

int cmd_bench(double seconds) {
    std::printf("{\"event\":\"bench_host\",\"liquid\":\"%s\"}\n", liquid_libversion());
    // Spectrum frame: Hann window, FFT, |X|^2, 10 log10, uint8 quantize.
    for (unsigned n : {1024u, 2048u, 4096u, 8192u}) {
        std::vector<cf> x(n), y(n);
        std::vector<float> w(n);
        std::vector<unsigned char> q(n);
        for (unsigned i = 0; i < n; ++i) {
            w[i] = 0.5f - 0.5f * std::cos(2.0f * kPi * static_cast<float>(i) / static_cast<float>(n));
            x[i] = cf(std::sin(0.1f * static_cast<float>(i)), std::cos(0.37f * static_cast<float>(i)));
        }
        std::vector<cf> xw(n);
        fftplan p = fft_create_plan(n, reinterpret_cast<liquid_float_complex*>(xw.data()),
                                    reinterpret_cast<liquid_float_complex*>(y.data()), LIQUID_FFT_FORWARD, 0);
        unsigned iters = 0;
        double t0 = now_s(), fft_only = 0;
        while (now_s() - t0 < seconds / 8) {
            for (unsigned i = 0; i < n; ++i) xw[i] = x[i] * w[i];
            double a = now_s();
            fft_execute(p);
            fft_only += now_s() - a;
            for (unsigned i = 0; i < n; ++i) {
                float db = 10.0f * std::log10(std::norm(y[i]) + 1e-20f);
                q[i] = static_cast<unsigned char>(std::clamp(db + 150.0f, 0.0f, 255.0f));
            }
            ++iters;
        }
        double per_frame = (now_s() - t0) / iters, per_fft = fft_only / iters;
        fft_destroy_plan(p);
        for (unsigned rate : {192000u, 384000u, 912000u}) {
            // Worst case: every input sample transformed (no overlap) = rate/n frames per second.
            double cpu_all = 100.0 * per_frame * rate / n;
            std::printf("{\"event\":\"bench_fft\",\"n\":%u,\"fft_us\":%.2f,\"frame_us\":%.2f,\"rate_hz\":%u,"
                        "\"cpu_pct_every_sample\":%.3f,\"cpu_pct_20fps\":%.4f,\"q\":%u}\n",
                        n, per_fft * 1e6, per_frame * 1e6, rate, cpu_all, 100.0 * per_frame * 20, q[n / 3]);
        }
    }
    // Channel chain: mix + resample + filter/demod + AGC, per channel, at each capture rate.
    for (unsigned rate : {192000u, 384000u, 912000u}) {
        std::vector<cf> x(rate / 10);
        for (size_t i = 0; i < x.size(); ++i)
            x[i] = std::polar(0.3f, 2.0f * kPi * 12345.0f * static_cast<float>(i) / static_cast<float>(rate));
        for (const char* mode : {"am", "usb", "lsb", "cw", "nfm", "wfm"}) {
            ChainOpts o;
            o.in_rate = rate;
            o.offset_hz = 12345;
            o.mode = mode;
            o.agc = "medium";
            Chain c(o);
            std::vector<float> out;
            double t0 = now_s(), sig = 0;
            while (now_s() - t0 < seconds / 18) {
                out.clear();
                c.process(x.data(), x.size(), out);
                sig += 0.1;
            }
            double cpu = 100.0 * (now_s() - t0) / sig;
            std::printf("{\"event\":\"bench_chain\",\"mode\":\"%s\",\"rate_hz\":%u,\"cpu_pct_per_channel\":%.3f}\n",
                        mode, rate, cpu);
        }
    }
    return 0;
}

}  // namespace

int main(int argc, char** argv) {
    if (argc < 2) die("usage: version | fft N | chain ... | agc --mode M | bench [--seconds S]");
    std::string cmd = argv[1];
    auto arg = [&](int& i) -> const char* {
        if (i + 1 >= argc) die(std::string("missing value for ") + argv[i]);
        return argv[++i];
    };
    if (cmd == "version") {
        std::printf("{\"liquid_header\":\"%s\",\"liquid_library\":\"%s\",\"pinned\":\"%s\",\"fft\":\"internal\"}\n",
                    LIQUID_VERSION, liquid_libversion(), k2::kLiquidPinnedVersion);
        return std::strcmp(liquid_libversion(), k2::kLiquidPinnedVersion) == 0 ? 0 : 3;
    }
    if (cmd == "fft") {
        if (argc != 3) die("fft N");
        return cmd_fft(static_cast<unsigned>(std::strtoul(argv[2], nullptr, 10)));
    }
    if (cmd == "agc") {
        std::string mode = "medium";
        for (int i = 2; i < argc; ++i) {
            if (std::string(argv[i]) == "--mode") mode = arg(i);
            else die(std::string("unknown option ") + argv[i]);
        }
        return cmd_agc(mode);
    }
    if (cmd == "chain") {
        ChainOpts o;
        for (int i = 2; i < argc; ++i) {
            std::string k = argv[i];
            if (k == "--in-rate") o.in_rate = static_cast<unsigned>(std::strtoul(arg(i), nullptr, 10));
            else if (k == "--offset") o.offset_hz = std::strtod(arg(i), nullptr);
            else if (k == "--mode") o.mode = arg(i);
            else if (k == "--bw") o.bw_hz = std::strtod(arg(i), nullptr);
            else if (k == "--bfo") o.bfo_hz = std::strtod(arg(i), nullptr);
            else if (k == "--agc") o.agc = arg(i);
            else if (k == "--gain") o.gain = std::strtof(arg(i), nullptr);
            else die("unknown option " + k);
        }
        if (!o.in_rate || o.mode.empty()) die("chain needs --in-rate and --mode");
        return cmd_chain(o);
    }
    if (cmd == "bench") {
        double s = 9;
        for (int i = 2; i < argc; ++i) {
            if (std::string(argv[i]) == "--seconds") s = std::strtod(arg(i), nullptr);
            else die(std::string("unknown option ") + argv[i]);
        }
        return cmd_bench(s);
    }
    die("unknown command " + cmd);
}
