// The one liquid-dsp the worker may be built against: the pinned, project-built 1.8.3
// (tools/build_liquid.sh, no FFTW). Debian 13 ships 1.7.0 with FFTW in /usr/include/liquid;
// if that header were ever picked up instead, this fails the build.
#pragma once

// <complex> first: liquid.h then makes liquid_float_complex = std::complex<float>.
#include <complex>

#include <liquid/liquid.h>

#if !defined(LIQUID_VERSION_MAJOR) || LIQUID_VERSION_MAJOR != 1 || LIQUID_VERSION_MINOR != 8 || \
    LIQUID_VERSION_PATCH != 3
#error "liquid-dsp header is not the pinned 1.8.3 (build it with tools/build_liquid.sh)"
#endif

namespace k2 {
inline constexpr const char* kLiquidPinnedVersion = "1.8.3";
}
