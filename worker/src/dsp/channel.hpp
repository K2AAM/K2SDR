// One listener channel (M1 design §3.2): NCO mix -> multi-stage polyphase resampler ->
// complex passband filter -> demodulator -> AGC -> int16 PCM at 12000 S/s mono.
//
// Integer Hz throughout. `offset_hz` is the tuned (dial/carrier) frequency relative to the
// capture centre. Passband edges are relative to the tuned frequency:
//   USB 300..2700, LSB -2700..-300, CW -250..250 around the carrier (heard at bfo_hz),
//   AM -4500..4500, NFM -6000..6000, WFM -90000..90000 (defaults; settable).
// Channel (complex) rates: 12 kS/s (AM/SSB/CW), 24 kS/s (NFM), 192 kS/s (WFM); NFM/WFM
// audio is resampled to 12 kS/s after the discriminator. Audio is good to ~4.5 kHz
// (measured roll-off of the 12 kS/s resampler, docs/ops/liquid-dsp-1.8.3.md).
#pragma once

#include <cstdint>
#include <memory>
#include <string>
#include <vector>

#include "dsp/agc.hpp"
#include "dsp/types.hpp"

namespace k2::dsp {

constexpr uint32_t kAudioRateHz = 12000;

enum class Mode : uint8_t { AM, USB, LSB, CW, NFM, WFM };

struct ChannelConfig {
    uint32_t in_rate_hz = 0;
    int64_t offset_hz = 0;
    Mode mode = Mode::USB;
    // default_passband: use the mode's default edges and ignore pass_lo/hi_hz. Otherwise the
    // edges are taken as given (every int32 value is an edge; there is no in-band marker).
    bool default_passband = true;
    int32_t pass_lo_hz = 0;
    int32_t pass_hi_hz = 0;
    int32_t bfo_hz = 700;             // CW pitch
    AgcConfig agc = AgcConfig::preset(AgcMode::Medium);
};

struct ChannelStats {
    uint64_t in_samples = 0, out_samples = 0, clipped = 0;
};

class Channel {
  public:
    // Throws std::invalid_argument with a reason if the config cannot be honoured.
    explicit Channel(const ChannelConfig& cfg);
    ~Channel();
    Channel(const Channel&) = delete;
    Channel& operator=(const Channel&) = delete;

    void process(const cf* x, size_t n, std::vector<int16_t>& audio);
    // Phase-continuous retune within the capture span (no generation change for the stream).
    void set_offset(int64_t offset_hz);
    void set_passband(int32_t lo_hz, int32_t hi_hz);
    // Channel power inside the passband, before AGC, averaged over ~100 ms. dBFS: a full-scale
    // tone in the passband reads 0.
    float level_dbfs() const;
    const ChannelConfig& config() const { return cfg_; }
    const ChannelStats& stats() const { return stats_; }
    uint32_t channel_rate_hz() const { return ch_rate_; }

    static void default_passband(Mode m, int32_t& lo, int32_t& hi);
    // True if [offset+lo, offset+hi] lies inside +-in_rate/2. Overflow-free for every int64 offset.
    static bool fits_span(int64_t offset_hz, int32_t lo_hz, int32_t hi_hz, uint32_t in_rate_hz);
    static bool parse_mode(const std::string& s, Mode& m);

  private:
    struct Impl;
    void validate() const;
    void build_filter();
    ChannelConfig cfg_;
    uint32_t ch_rate_ = 0;
    ChannelStats stats_;
    std::unique_ptr<Impl> p_;
};

}  // namespace k2::dsp
