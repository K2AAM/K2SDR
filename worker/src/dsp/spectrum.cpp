#include "dsp/spectrum.hpp"

#include <algorithm>
#include <cmath>
#include <stdexcept>

#include "liquid_pin.hpp"

namespace k2::dsp {

Spectrum::Spectrum(const SpectrumConfig& cfg) : cfg_(cfg) {
    if (cfg.rate_hz == 0 || cfg.fft_n < 16 || cfg.fft_n > (1u << 16) || cfg.frames_per_s == 0 ||
        cfg.frames_per_s > cfg.rate_hz)
        throw std::invalid_argument("bad spectrum config");
    const uint64_t period = cfg.rate_hz / cfg.frames_per_s;
    blocks_per_frame_ = static_cast<uint32_t>(std::max<uint64_t>(1, period / cfg.fft_n));
    ring_.assign(static_cast<size_t>(blocks_per_frame_) * cfg.fft_n, cf());
    window_.resize(cfg.fft_n);
    double sum = 0;
    for (uint32_t i = 0; i < cfg.fft_n; ++i) {
        // Periodic Hann (DFT-even): a tone centred in a bin has coherent gain sum(w).
        window_[i] = static_cast<float>(0.5 - 0.5 * std::cos(2.0 * M_PI * i / cfg.fft_n));
        sum += window_[i];
    }
    norm_ = static_cast<float>(1.0 / (sum * sum));
    fft_in_.resize(cfg.fft_n);
    fft_out_.resize(cfg.fft_n);
    acc_.resize(cfg.fft_n);
    last_db_.resize(cfg.fft_n);
    plan_ = fft_create_plan(cfg.fft_n, reinterpret_cast<liquid_float_complex*>(fft_in_.data()),
                            reinterpret_cast<liquid_float_complex*>(fft_out_.data()), LIQUID_FFT_FORWARD, 0);
}

Spectrum::~Spectrum() { fft_destroy_plan(static_cast<fftplan>(plan_)); }

uint64_t Spectrum::boundary(uint64_t k) const { return k * cfg_.rate_hz / cfg_.frames_per_s; }

void Spectrum::push(const cf* x, size_t n, std::vector<SpectrumFrame>& out) {
    const size_t rn = ring_.size();
    size_t i = 0;
    while (i < n) {
        // Copy up to the next frame boundary, then emit.
        const uint64_t next = boundary(frame_k_);
        const size_t take = static_cast<size_t>(std::min<uint64_t>(n - i, next - total_));
        for (size_t j = 0; j < take; ++j) ring_[(total_ + j) % rn] = x[i + j];
        total_ += take;
        i += take;
        if (total_ == next) {
            if (total_ >= cfg_.fft_n) emit(out);   // need one full block
            ++frame_k_;
        }
    }
}

void Spectrum::emit(std::vector<SpectrumFrame>& out) {
    const uint32_t n = cfg_.fft_n;
    const size_t rn = ring_.size();
    const uint32_t blocks = static_cast<uint32_t>(std::min<uint64_t>(blocks_per_frame_, total_ / n));
    std::fill(acc_.begin(), acc_.end(), 0.0f);
    for (uint32_t b = 0; b < blocks; ++b) {
        const uint64_t start = total_ - static_cast<uint64_t>(b + 1) * n;
        for (uint32_t k = 0; k < n; ++k) fft_in_[k] = ring_[(start + k) % rn] * window_[k];
        fft_execute(static_cast<fftplan>(plan_));
        for (uint32_t k = 0; k < n; ++k) acc_[k] += std::norm(fft_out_[k]);
    }
    SpectrumFrame f;
    f.end_sample = total_;
    f.blocks = blocks;
    f.bins.resize(n);
    const float scale = norm_ / static_cast<float>(blocks);
    for (uint32_t k = 0; k < n; ++k) {
        const uint32_t src = (k + n / 2) % n;   // FFT shift
        const float db = 10.0f * std::log10(acc_[src] * scale + 1e-30f);
        last_db_[k] = db;
        const float q = std::round((db - cfg_.db_floor) / cfg_.db_step);
        f.bins[k] = static_cast<uint8_t>(std::clamp(q, 0.0f, 255.0f));
    }
    out.push_back(std::move(f));
}

}  // namespace k2::dsp
