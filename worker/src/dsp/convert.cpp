#include <cstring>

#include "dsp/types.hpp"

namespace k2::dsp {

size_t bytes_per_sample(SampleFormat fmt) {
    switch (fmt) {
        case SampleFormat::CI16: return 4;
        case SampleFormat::CF32: return 8;
        case SampleFormat::RI16: return 2;
        case SampleFormat::RF32: return 4;
    }
    return 0;
}

void to_complex(SampleFormat fmt, const uint8_t* raw, size_t count, cf* out) {
    constexpr float kI16 = 1.0f / 32768.0f;
    for (size_t i = 0; i < count; ++i) {
        switch (fmt) {
            case SampleFormat::CI16: {
                int16_t v[2];
                std::memcpy(v, raw + 4 * i, 4);
                out[i] = cf(static_cast<float>(v[0]) * kI16, static_cast<float>(v[1]) * kI16);
                break;
            }
            case SampleFormat::CF32: {
                float v[2];
                std::memcpy(v, raw + 8 * i, 8);
                out[i] = cf(v[0], v[1]);
                break;
            }
            case SampleFormat::RI16: {
                int16_t v;
                std::memcpy(&v, raw + 2 * i, 2);
                out[i] = cf(static_cast<float>(v) * kI16, 0.0f);
                break;
            }
            case SampleFormat::RF32: {
                float v;
                std::memcpy(&v, raw + 4 * i, 4);
                out[i] = cf(v, 0.0f);
                break;
            }
        }
    }
}

}  // namespace k2::dsp
