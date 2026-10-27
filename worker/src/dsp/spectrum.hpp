// Spectrum for the waterfall (M1 design §3.2): one per receiver.
//
// At every frame boundary (frames_per_s, exact in integer sample arithmetic) the most recent
// max(1, period / fft_n) non-overlapping fft_n-sample blocks are Hann-windowed, transformed and
// power-averaged; bins are FFT-shifted (bin 0 = -rate/2) and quantized to uint8 dB:
//   level_dBFS = db_floor + q * db_step,  q in [0, 255]
// Referenced so that a full-scale complex tone (|x| = 1) centred in a bin reads 0 dBFS.
#pragma once

#include <cstdint>
#include <vector>

#include "dsp/types.hpp"

namespace k2::dsp {

struct SpectrumConfig {
    uint32_t rate_hz = 0;
    uint32_t fft_n = 4096;      // a power of two, 16..65536 (Spectrum::valid_fft_size)
    uint32_t frames_per_s = 15;
    float db_floor = -127.5f;
    float db_step = 0.5f;   // 256 levels: -127.5 .. 0 dBFS
};

struct SpectrumFrame {
    uint64_t end_sample = 0;    // index (in this generation) just past the newest sample used
    uint32_t blocks = 0;        // FFT blocks averaged
    std::vector<uint8_t> bins;  // fft_n values, FFT-shifted
};

class Spectrum {
  public:
    explicit Spectrum(const SpectrumConfig& cfg);
    static constexpr bool valid_fft_size(uint32_t n) { return n >= 16 && n <= (1u << 16) && (n & (n - 1)) == 0; }
    ~Spectrum();
    Spectrum(const Spectrum&) = delete;
    Spectrum& operator=(const Spectrum&) = delete;

    void push(const cf* x, size_t n, std::vector<SpectrumFrame>& out);
    const SpectrumConfig& config() const { return cfg_; }
    // Averaged power in dBFS for the last frame (unquantized, FFT-shifted), for tests/S-meter.
    const std::vector<float>& last_db() const { return last_db_; }

  private:
    void emit(std::vector<SpectrumFrame>& out);
    uint64_t boundary(uint64_t k) const;
    SpectrumConfig cfg_;
    uint32_t blocks_per_frame_ = 1;
    std::vector<cf> ring_;          // last `ring_.size()` samples
    uint64_t total_ = 0;            // samples pushed
    uint64_t frame_k_ = 1;          // next frame number; frame k ends at boundary(k)
    std::vector<float> window_;
    float norm_ = 1;                // 1 / (sum of window)^2
    std::vector<cf> fft_in_, fft_out_;
    std::vector<float> acc_, last_db_;
    void* plan_ = nullptr;
};

}  // namespace k2::dsp
