// Shared DSP types for the worker (M1 design §3.2). All frequencies are integer Hz.
#pragma once

#include <complex>
#include <cstdint>
#include <cstddef>

namespace k2::dsp {

using cf = std::complex<float>;

// Sample formats as on the frame_v1 wire / in SigMF (core:datatype).
enum class SampleFormat : uint8_t { CI16, CF32, RI16, RF32 };

// Converts `count` samples of raw little-endian `fmt` data to complex float in [-1, 1).
// Real formats become complex with a zero imaginary part.
void to_complex(SampleFormat fmt, const uint8_t* raw, size_t count, cf* out);
size_t bytes_per_sample(SampleFormat fmt);

}  // namespace k2::dsp
