// Listening AGC (outline §3: Fast / Medium / Slow / Off, separate from hardware gain).
//
// Peak AGC with look-ahead: the input is delayed by `lookahead_ms`, and the envelope sees
// the undelayed signal through a sliding maximum, so the gain is already down when a peak
// reaches the output (no blast on a sudden strong signal; measured need: liquid's loop AGC
// overshoots +27..+40 dB, docs/ops/liquid-dsp-1.8.3.md). After a peak the envelope holds
// for `hang_ms`, then decays exponentially with `decay_ms`. Gain = target / envelope,
// limited to `max_gain_db`. Off: a fixed `manual_gain_db`.
#pragma once

#include <cstdint>
#include <deque>
#include <vector>

namespace k2::dsp {

enum class AgcMode : uint8_t { Off, Fast, Medium, Slow };

struct AgcConfig {
    AgcMode mode = AgcMode::Medium;
    float target_dbfs = -6.0f;     // output peak level
    float max_gain_db = 70.0f;
    float manual_gain_db = 0.0f;   // Off only
    float attack_ms = 0.5f;
    float lookahead_ms = 5.0f;
    float hang_ms = -1;            // < 0: mode preset
    float decay_ms = -1;           // < 0: mode preset

    static AgcConfig preset(AgcMode m);
};

class Agc {
  public:
    Agc(const AgcConfig& cfg, uint32_t rate_hz);
    // In-place; output is delayed by lookahead samples (constant latency).
    void process(float* x, size_t n);
    uint32_t latency_samples() const { return lookahead_; }
    float gain_db() const;

  private:
    float next(float x);
    AgcConfig cfg_;
    uint32_t lookahead_ = 0;
    float target_ = 0, max_gain_ = 0, manual_ = 1;
    float k_attack_ = 0, k_decay_ = 0;
    uint32_t hang_samples_ = 0, hang_left_ = 0;
    float env_ = 0;
    uint64_t n_in_ = 0;
    std::vector<float> delay_;
    size_t delay_pos_ = 0;
    std::deque<std::pair<uint64_t, float>> window_;   // sliding max of |x| over the look-ahead
};

}  // namespace k2::dsp
