// Replay of a SigMF dataset (the control service has already validated the
// metadata and passes format/rate/origin explicitly). Samples are delivered as
// the file's own bytes: no format conversion, so real ADC data stays real.
#pragma once

#include <cstddef>
#include <cstdint>
#include <string>

namespace k2 {

class ReplaySource {
public:
    ReplaySource() = default;
    ~ReplaySource();
    ReplaySource(const ReplaySource&) = delete;
    ReplaySource& operator=(const ReplaySource&) = delete;

    bool open(const std::string& path, uint32_t bytes_per_sample, std::string& err);
    uint64_t sample_count() const { return samples_; }
    // Pointer to sample `index`; caller guarantees index + count <= sample_count().
    const uint8_t* at(uint64_t index) const { return base_ + index * bps_; }

private:
    const uint8_t* base_ = nullptr;
    size_t len_ = 0;
    uint64_t samples_ = 0;
    uint32_t bps_ = 0;
};

}  // namespace k2
