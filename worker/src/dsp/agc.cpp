#include "dsp/agc.hpp"

#include <algorithm>
#include <cmath>

namespace k2::dsp {

AgcConfig AgcConfig::preset(AgcMode m) {
    AgcConfig c;
    c.mode = m;
    switch (m) {
        // Back within 3 dB of target after a 40 dB drop: hang + 5.48 x decay -> fast 0.27 s,
        // medium 1.47 s, slow 4.61 s (measured, control/tests/test_dsp_pipeline.py).
        case AgcMode::Fast: c.hang_ms = 0; c.decay_ms = 50; break;
        case AgcMode::Medium: c.hang_ms = 100; c.decay_ms = 250; break;
        case AgcMode::Slow: c.hang_ms = 500; c.decay_ms = 750; break;
        case AgcMode::Off: c.hang_ms = 0; c.decay_ms = 0; break;
    }
    return c;
}

namespace {
float db_to_amp(float db) { return std::pow(10.0f, db / 20.0f); }
float time_coef(float ms, uint32_t rate) {  // one-pole coefficient reaching 1-1/e after `ms`
    if (ms <= 0) return 1.0f;
    return 1.0f - std::exp(-1.0f / (ms * 1e-3f * static_cast<float>(rate)));
}
}  // namespace

Agc::Agc(const AgcConfig& cfg, uint32_t rate_hz) : cfg_(cfg) {
    AgcConfig p = AgcConfig::preset(cfg.mode);
    if (cfg_.hang_ms < 0) cfg_.hang_ms = p.hang_ms;
    if (cfg_.decay_ms < 0) cfg_.decay_ms = p.decay_ms;
    lookahead_ = cfg_.mode == AgcMode::Off
                     ? 0
                     : static_cast<uint32_t>(std::lround(cfg_.lookahead_ms * 1e-3f * static_cast<float>(rate_hz)));
    target_ = db_to_amp(cfg_.target_dbfs);
    max_gain_ = db_to_amp(cfg_.max_gain_db);
    manual_ = db_to_amp(cfg_.manual_gain_db);
    k_attack_ = time_coef(cfg_.attack_ms, rate_hz);
    k_decay_ = time_coef(cfg_.decay_ms, rate_hz);
    hang_samples_ = static_cast<uint32_t>(std::lround(cfg_.hang_ms * 1e-3f * static_cast<float>(rate_hz)));
    // Delay of exactly `lookahead_` samples: the oldest sample in the sliding-max window is the one
    // leaving the delay line, so the envelope has had lookahead_ samples to attack it.
    delay_.assign(std::max<uint32_t>(lookahead_, 1), 0.0f);
    env_ = target_ / max_gain_;   // start at maximum gain (quiet)
}

float Agc::gain_db() const {
    if (cfg_.mode == AgcMode::Off) return cfg_.manual_gain_db;
    return 20.0f * std::log10(std::min(max_gain_, target_ / std::max(env_, 1e-30f)));
}

float Agc::next(float x) {
    if (cfg_.mode == AgcMode::Off) return x * manual_;
    // Sliding maximum of |x| over the last lookahead+1 input samples.
    const float a = std::fabs(x);
    while (!window_.empty() && window_.back().second <= a) window_.pop_back();
    window_.emplace_back(n_in_, a);
    while (window_.front().first + lookahead_ < n_in_) window_.pop_front();
    const float peak = window_.front().second;
    ++n_in_;

    if (peak > env_) {
        env_ += (peak - env_) * k_attack_;
        // The attack is fast but not instant; never let the delayed sample exceed the target.
        if (peak > env_ && lookahead_ == 0) env_ = peak;
        hang_left_ = hang_samples_;
    } else if (hang_left_ > 0) {
        --hang_left_;
    } else {
        env_ += (peak - env_) * k_decay_;
    }
    const float g = std::min(max_gain_, target_ / std::max(env_, 1e-30f));

    // Delay line: output the sample that entered lookahead samples ago.
    float out = delay_[delay_pos_];
    delay_[delay_pos_] = x;
    delay_pos_ = (delay_pos_ + 1) % delay_.size();
    return out * g;
}

void Agc::process(float* x, size_t n) {
    for (size_t i = 0; i < n; ++i) x[i] = next(x[i]);
}

}  // namespace k2::dsp
