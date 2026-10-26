/*
 * K2AAM WebSDR — data-plane frame header, version 1.
 *
 * Canonical definition. The Python mirror lives in
 * control/src/k2websdr/protocol/frame.py and is checked against this file
 * by control/tests/test_abi.py (compiled as both C11 and C++17).
 *
 * Wire layout: little-endian, 80-byte header, naturally aligned (no packing
 * attribute needed; offsets are asserted below), immediately followed by
 * sample_count samples in `format`.
 *
 * Copyright (c) 2026 K2AAM. All rights reserved (license pending).
 */
#ifndef K2WEBSDR_FRAME_V1_H
#define K2WEBSDR_FRAME_V1_H

#include <stddef.h>
#include <stdint.h>

#if !defined(__BYTE_ORDER__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "frame_v1 is defined little-endian; big-endian hosts are unsupported"
#endif

#ifdef __cplusplus
extern "C" {
#define KSDR_STATIC_ASSERT(c, m) static_assert(c, m)
#else
#define KSDR_STATIC_ASSERT(c, m) _Static_assert(c, m)
#endif

#define KSDR_FRAME_MAGIC        0x5244534Bu /* "KSDR" read as little-endian u32 */
#define KSDR_FRAME_VERSION      1u
#define KSDR_FRAME_HEADER_LEN_V1 80u

/* Frame kinds. Readers reject unknown kinds. */
enum ksdr_frame_kind {
    KSDR_KIND_SAMPLES       = 1,
    KSDR_KIND_END_OF_STREAM = 2  /* sample_count must be 0 */
};

/*
 * Sample formats. Complex formats are interleaved I,Q.
 * Real formats carry raw ADC samples (e.g. RX888) without forced conversion.
 */
enum ksdr_sample_format {
    KSDR_FMT_CI16 = 1, /* complex int16:  4 bytes/sample */
    KSDR_FMT_CF32 = 2, /* complex float32: 8 bytes/sample */
    KSDR_FMT_RI16 = 3, /* real int16:     2 bytes/sample */
    KSDR_FMT_RF32 = 4  /* real float32:   4 bytes/sample */
};

/* Flag bits. v1 readers reject a frame with any KSDR_F_RESERVED_MASK bit set. */
enum ksdr_frame_flags {
    KSDR_F_DISCONTINUITY = 1u << 0, /* samples before this frame are not contiguous with it */
    KSDR_F_GAP_KNOWN     = 1u << 1, /* gap_samples is meaningful (requires DISCONTINUITY) */
    KSDR_F_OVERFLOW      = 1u << 2, /* driver/USB overflow reported for this frame */
    KSDR_F_NEW_GEN       = 1u << 3  /* first frame of a new capture_gen */
};
#define KSDR_F_DEFINED_MASK  0x0000000Fu
#define KSDR_F_RESERVED_MASK (~KSDR_F_DEFINED_MASK)

struct ksdr_frame_header_v1 {
    uint32_t magic;              /*  0  KSDR_FRAME_MAGIC */
    uint16_t version;            /*  4  KSDR_FRAME_VERSION */
    uint16_t header_len;         /*  6  >= 80; readers skip trailing bytes they do not know */
    uint16_t kind;               /*  8  enum ksdr_frame_kind */
    uint16_t format;             /* 10  enum ksdr_sample_format */
    uint32_t flags;              /* 12  enum ksdr_frame_flags */
    uint64_t capture_gen;        /* 16  bumps on any physical capture change */
    uint64_t seq;                /* 24  per-stream frame counter, starts at 0, never resets */
    uint64_t first_sample_index; /* 32  delivered-sample count within this capture_gen; restarts at
                                        0 with each new generation; never wall clock */
    int64_t  freq_origin_hz;     /* 40  complex: center frequency; real: 0 Hz edge of sampled band */
    uint64_t host_time_ns;       /* 48  CLOCK_REALTIME estimate; informational only, excluded from
                                        determinism comparisons */
    uint64_t gap_samples;        /* 56  samples lost before this frame when GAP_KNOWN, else 0 */
    uint32_t receiver_idx;       /* 64  numeric receiver index; name mapping lives in control state */
    uint32_t stream_id;          /* 68 */
    uint32_t sample_rate_hz;     /* 72  integer Hz */
    uint32_t sample_count;       /* 76  samples (not bytes) following the header */
};

KSDR_STATIC_ASSERT(sizeof(struct ksdr_frame_header_v1) == KSDR_FRAME_HEADER_LEN_V1,
                   "frame header v1 must be 80 bytes");
KSDR_STATIC_ASSERT(offsetof(struct ksdr_frame_header_v1, flags) == 12, "flags offset");
KSDR_STATIC_ASSERT(offsetof(struct ksdr_frame_header_v1, capture_gen) == 16, "capture_gen offset");
KSDR_STATIC_ASSERT(offsetof(struct ksdr_frame_header_v1, freq_origin_hz) == 40, "freq_origin_hz offset");
KSDR_STATIC_ASSERT(offsetof(struct ksdr_frame_header_v1, receiver_idx) == 64, "receiver_idx offset");
KSDR_STATIC_ASSERT(offsetof(struct ksdr_frame_header_v1, sample_count) == 76, "sample_count offset");

/* Bytes per sample for a format, or 0 for an unknown format. */
static inline uint32_t ksdr_bytes_per_sample(uint16_t format)
{
    switch (format) {
    case KSDR_FMT_CI16: return 4u;
    case KSDR_FMT_CF32: return 8u;
    case KSDR_FMT_RI16: return 2u;
    case KSDR_FMT_RF32: return 4u;
    default:            return 0u;
    }
}

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* K2WEBSDR_FRAME_V1_H */
