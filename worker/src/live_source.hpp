// A live acquisition source (M1: Airspy HF+ via libairspyhf). The driver's callback thread
// hands sample blocks to the worker's event loop through a single-producer/single-consumer
// ring and wakes it through an eventfd; everything else runs on the worker thread.
//
// Hardware facts this interface is shaped by (docs/ops/airspyhf-measurements-2026-10-26.md):
// - A retune silently loses samples and the step lands up to about one block after the call:
//   every block is tagged with the tuning epoch it was captured in, and the worker discards
//   blocks of older epochs plus the first blocks of the new one.
// - After device loss, stop/close hang: abandon() is the only safe way out on that path.
// - Device settings persist across opens: open() sets every control explicitly.
#pragma once

#include <cstdint>
#include <string>
#include <vector>

#include "dsp/types.hpp"
#include "worker_ctl_v1.h"

namespace k2 {

struct HwSettings {
    uint8_t hf_agc = 1;            // approved defaults (Adam, 2026-10-26)
    uint8_t hf_agc_threshold = 0;
    uint8_t hf_lna = 1;
    uint8_t hf_att = 0;
};

struct LiveBlock {
    const dsp::cf* samples = nullptr;
    uint32_t count = 0;
    uint64_t dropped = 0;          // driver-reported loss immediately before this block
    uint64_t lost = 0;             // samples the ring had no room for, immediately before this block
    uint32_t epoch = 0;
};

class LiveSource {
public:
    virtual ~LiveSource() = default;

    // Opens the device and sets every control. Returns KCTL_OK or a kctl_result
    // (E_BUSY: present but held elsewhere; E_DEVICE: absent or failed; E_UNSUPPORTED: bad rate).
    virtual uint32_t open(uint64_t serial, uint32_t rate_hz, uint32_t center_hz, const HwSettings& hw,
                          std::string& err) = 0;
    virtual bool start(std::string& err) = 0;
    virtual int wake_fd() const = 0;
    virtual void clear_wake() = 0;
    // Next block, or false if the ring is empty. The view stays valid until the next pop().
    virtual bool pop(LiveBlock& out) = 0;

    // Starts a new tuning epoch: blocks captured from now on carry it. Returns it.
    virtual uint32_t new_epoch() = 0;
    virtual bool set_center(uint32_t hz) = 0;
    virtual bool set_rate(uint32_t hz, std::string& err) = 0;   // stop, set, start
    virtual bool set_hw(const HwSettings& hw) = 0;
    virtual const std::vector<uint32_t>& rates() const = 0;

    virtual bool streaming() const = 0;
    virtual uint64_t last_block_ns() const = 0;   // CLOCK_MONOTONIC of the newest callback (0: none yet)

    // Normal path: stop streaming and release the device (callers bound it with a watchdog).
    virtual void stop_and_close() = 0;
    // Device-loss path: forget the handle without calling into the driver (stop/close hang).
    virtual void abandon() = 0;
};

}  // namespace k2
