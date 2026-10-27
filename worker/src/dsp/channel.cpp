#include "dsp/channel.hpp"

#include <algorithm>
#include <climits>
#include <cmath>
#include <stdexcept>

#include "liquid_pin.hpp"

namespace k2::dsp {

struct Channel::Impl {
    nco_crcf nco = nullptr;
    msresamp_crcf rs = nullptr;
    firfilt_cccf bp = nullptr;
    iirfilt_rrrf dc = nullptr;
    freqdem fm = nullptr;
    iirfilt_rrrf deemph = nullptr;
    msresamp_rrrf audio_rs = nullptr;
    std::unique_ptr<Agc> agc;
    std::vector<cf> mixed, ch;
    std::vector<float> demod, audio;
    double level_acc = 0;          // one-pole power average of the filtered channel
    float level_k = 0;
    ~Impl() {
        if (nco) nco_crcf_destroy(nco);
        if (rs) msresamp_crcf_destroy(rs);
        if (bp) firfilt_cccf_destroy(bp);
        if (dc) iirfilt_rrrf_destroy(dc);
        if (fm) freqdem_destroy(fm);
        if (deemph) iirfilt_rrrf_destroy(deemph);
        if (audio_rs) msresamp_rrrf_destroy(audio_rs);
    }
};

bool Channel::parse_mode(const std::string& s, Mode& m) {
    static const std::pair<const char*, Mode> names[] = {{"am", Mode::AM},   {"usb", Mode::USB}, {"lsb", Mode::LSB},
                                                         {"cw", Mode::CW},   {"nfm", Mode::NFM}, {"wfm", Mode::WFM}};
    for (const auto& [n, v] : names)
        if (s == n) { m = v; return true; }
    return false;
}

void Channel::default_passband(Mode m, int32_t& lo, int32_t& hi) {
    switch (m) {
        case Mode::USB: lo = 300; hi = 2700; break;
        case Mode::LSB: lo = -2700; hi = -300; break;
        case Mode::CW: lo = -250; hi = 250; break;
        case Mode::AM: lo = -4500; hi = 4500; break;
        case Mode::NFM: lo = -6000; hi = 6000; break;
        case Mode::WFM: lo = -90000; hi = 90000; break;
    }
}

namespace {
uint32_t channel_rate(Mode m) { return m == Mode::WFM ? 192000u : m == Mode::NFM ? 24000u : kAudioRateHz; }
}  // namespace

void Channel::validate() const {
    if (cfg_.in_rate_hz < ch_rate_)
        throw std::invalid_argument("capture rate " + std::to_string(cfg_.in_rate_hz) + " Hz is below the " +
                                    std::to_string(ch_rate_) + " Hz this mode needs");
    if (cfg_.pass_lo_hz >= cfg_.pass_hi_hz) throw std::invalid_argument("passband low edge must be below high edge");
    const int64_t shift = cfg_.mode == Mode::CW ? cfg_.bfo_hz : 0;
    const int64_t edge = static_cast<int64_t>(ch_rate_) / 2 - static_cast<int64_t>(ch_rate_) / 40;   // 5 % guard
    if (cfg_.pass_lo_hz + shift < -edge || cfg_.pass_hi_hz + shift > edge)
        throw std::invalid_argument("passband exceeds the channel bandwidth (+-" + std::to_string(edge) + " Hz)");
    if (cfg_.mode == Mode::CW && (cfg_.bfo_hz < 100 || cfg_.bfo_hz > 3000))
        throw std::invalid_argument("CW BFO must be 100..3000 Hz");
    // The whole channel must sit inside the capture span.
    const int64_t half = static_cast<int64_t>(cfg_.in_rate_hz) / 2;
    if (cfg_.offset_hz + cfg_.pass_lo_hz < -half || cfg_.offset_hz + cfg_.pass_hi_hz > half)
        throw std::invalid_argument("channel passband lies outside the capture span");
}

Channel::Channel(const ChannelConfig& cfg) : cfg_(cfg), p_(std::make_unique<Impl>()) {
    int32_t lo, hi;
    default_passband(cfg_.mode, lo, hi);
    if (cfg_.pass_lo_hz == INT32_MIN) cfg_.pass_lo_hz = lo;
    if (cfg_.pass_hi_hz == INT32_MIN) cfg_.pass_hi_hz = hi;
    ch_rate_ = channel_rate(cfg_.mode);
    validate();
    p_->nco = nco_crcf_create(LIQUID_NCO);
    set_offset(cfg_.offset_hz);
    p_->rs = msresamp_crcf_create(static_cast<float>(static_cast<double>(ch_rate_) / cfg_.in_rate_hz), 70.0f);
    build_filter();
    if (cfg_.mode == Mode::AM) p_->dc = iirfilt_rrrf_create_dc_blocker(1e-3f);
    if (cfg_.mode == Mode::NFM || cfg_.mode == Mode::WFM) {
        const double dev = cfg_.mode == Mode::NFM ? 5000.0 : 75000.0;   // full-scale audio at this deviation
        p_->fm = freqdem_create(static_cast<float>(dev / ch_rate_));
        if (cfg_.mode == Mode::WFM) {   // 75 us de-emphasis, one pole
            const float a = std::exp(-1.0f / (static_cast<float>(ch_rate_) * 75e-6f));
            float b[2] = {1.0f - a, 0.0f}, den[2] = {1.0f, -a};
            p_->deemph = iirfilt_rrrf_create(b, 2, den, 2);
        }
        p_->audio_rs = msresamp_rrrf_create(static_cast<float>(static_cast<double>(kAudioRateHz) / ch_rate_), 70.0f);
    }
    p_->agc = std::make_unique<Agc>(cfg_.agc, kAudioRateHz);
    p_->level_k = 1.0f - std::exp(-1.0f / (0.1f * static_cast<float>(ch_rate_)));
}

Channel::~Channel() = default;

void Channel::set_offset(int64_t offset_hz) {
    const int64_t half = static_cast<int64_t>(cfg_.in_rate_hz) / 2;
    if (offset_hz + cfg_.pass_lo_hz < -half || offset_hz + cfg_.pass_hi_hz > half)
        throw std::invalid_argument("channel passband lies outside the capture span");
    cfg_.offset_hz = offset_hz;
    // CW: the carrier lands at +bfo in the channel so it is heard as a bfo_hz tone.
    const int64_t mix = offset_hz - (cfg_.mode == Mode::CW ? cfg_.bfo_hz : 0);
    nco_crcf_set_frequency(p_->nco, static_cast<float>(2.0 * M_PI * static_cast<double>(mix) / cfg_.in_rate_hz));
}

void Channel::set_passband(int32_t lo_hz, int32_t hi_hz) {
    ChannelConfig old = cfg_;
    cfg_.pass_lo_hz = lo_hz;
    cfg_.pass_hi_hz = hi_hz;
    try {
        validate();
    } catch (...) {
        cfg_ = old;
        throw;
    }
    build_filter();
}

void Channel::build_filter() {
    // Complex band-pass [lo, hi] (+bfo for CW) at the channel rate: Kaiser low-pass of half the
    // width, shifted to the centre. Length from the transition width (10 % of the width, 100..2000 Hz).
    const double shift = cfg_.mode == Mode::CW ? cfg_.bfo_hz : 0;
    const double lo = cfg_.pass_lo_hz + shift, hi = cfg_.pass_hi_hz + shift, fs = ch_rate_;
    const double width = hi - lo;
    const double trans = std::clamp(width * 0.1, 100.0, 2000.0);
    unsigned taps = static_cast<unsigned>(std::ceil((70.0 - 8.0) / (2.285 * 2.0 * M_PI * trans / fs))) | 1u;
    taps = std::clamp(taps, 31u, 1023u);
    std::vector<float> h(taps);
    liquid_firdes_kaiser(taps, static_cast<float>((width / 2.0 + trans / 2.0) / fs), 70.0f, 0.0f, h.data());
    double sum = 0;
    for (float v : h) sum += v;
    const double f0 = (hi + lo) / 2.0 / fs;
    std::vector<liquid_float_complex> hc(taps);
    for (unsigned i = 0; i < taps; ++i) {
        const double t = static_cast<double>(i) - static_cast<double>(taps - 1) / 2.0;
        hc[i] = static_cast<float>(h[i] / sum) * std::polar(1.0f, static_cast<float>(2.0 * M_PI * f0 * t));
    }
    if (p_->bp) firfilt_cccf_destroy(p_->bp);
    p_->bp = firfilt_cccf_create(hc.data(), taps);
}

float Channel::level_dbfs() const { return 10.0f * std::log10(static_cast<float>(p_->level_acc) + 1e-30f); }

void Channel::process(const cf* x, size_t n, std::vector<int16_t>& audio) {
    Impl& p = *p_;
    stats_.in_samples += n;
    p.mixed.resize(n);
    for (size_t i = 0; i < n; ++i) {
        nco_crcf_mix_down(p.nco, x[i], &p.mixed[i]);
        nco_crcf_step(p.nco);
    }
    p.ch.resize(static_cast<size_t>(std::ceil(static_cast<double>(n) * ch_rate_ / cfg_.in_rate_hz)) + 64);
    unsigned ny = 0;
    msresamp_crcf_execute(p.rs, p.mixed.data(), static_cast<unsigned>(n), p.ch.data(), &ny);

    p.demod.resize(ny);
    for (unsigned i = 0; i < ny; ++i) {
        cf z;
        firfilt_cccf_push(p.bp, p.ch[i]);
        firfilt_cccf_execute(p.bp, &z);
        p.level_acc += (static_cast<double>(std::norm(z)) - p.level_acc) * p.level_k;
        float a;
        switch (cfg_.mode) {
            case Mode::AM: iirfilt_rrrf_execute(p.dc, std::abs(z), &a); break;
            case Mode::USB:
            case Mode::LSB:
            case Mode::CW: a = z.real(); break;
            case Mode::NFM:
            case Mode::WFM:
                freqdem_demodulate(p.fm, z, &a);
                if (p.deemph) iirfilt_rrrf_execute(p.deemph, a, &a);
                break;
        }
        p.demod[i] = a;
    }
    std::vector<float>* aud = &p.demod;
    if (p.audio_rs) {
        p.audio.resize(static_cast<size_t>(ny) * kAudioRateHz / ch_rate_ + 64);
        unsigned na = 0;
        msresamp_rrrf_execute(p.audio_rs, p.demod.data(), ny, p.audio.data(), &na);
        p.audio.resize(na);
        aud = &p.audio;
    }
    p.agc->process(aud->data(), aud->size());
    const size_t base = audio.size();
    audio.resize(base + aud->size());
    for (size_t i = 0; i < aud->size(); ++i) {
        float s = (*aud)[i] * 32767.0f;
        if (s > 32767.0f || s < -32768.0f) {
            ++stats_.clipped;
            s = std::clamp(s, -32768.0f, 32767.0f);
        }
        audio[base + i] = static_cast<int16_t>(std::lrint(s));
    }
    stats_.out_samples += aud->size();
}

}  // namespace k2::dsp
