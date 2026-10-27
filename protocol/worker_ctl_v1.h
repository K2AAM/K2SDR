/*
 * K2AAM WebSDR — worker control messages, version 1.
 *
 * Carried over a Unix stream socket between the control service and one
 * acquisition worker. Every message is a 24-byte header followed by
 * payload_len bytes. Little-endian, naturally aligned, offsets asserted.
 *
 * Command lifecycle (control -> worker):
 *   cmd_seq is assigned by the control service, strictly increasing per
 *   connection, never 0. The worker answers SET_CAPTURE and STOP with exactly
 *   one ACK, then (if accepted) exactly one APPLIED or FAILED with the same
 *   cmd_seq; SET_HW likewise with HW_APPLIED, CHANNEL_* with CHANNEL_APPLIED. PING is answered by exactly one PONG and no ACK. Commands are
 *   processed strictly in order. A malformed message is a control-service
 *   bug: the worker reports STATUS FAILED/E_BAD_MESSAGE and exits.
 *
 *   APPLIED may reach the control service before the last frames of the old
 *   capture generation have been read from the data socket. The capture_gen
 *   in each frame header is authoritative for which generation it belongs to.
 *
 * Unsolicited worker messages (STATUS) use cmd_seq = 0.
 *
 * Copyright (c) 2026 K2AAM. All rights reserved (license pending).
 */
#ifndef K2WEBSDR_WORKER_CTL_V1_H
#define K2WEBSDR_WORKER_CTL_V1_H

#include <stddef.h>
#include <stdint.h>

#if !defined(__BYTE_ORDER__) || __BYTE_ORDER__ != __ORDER_LITTLE_ENDIAN__
#error "worker_ctl_v1 is defined little-endian; big-endian hosts are unsupported"
#endif

#ifdef __cplusplus
extern "C" {
#define KCTL_STATIC_ASSERT(c, m) static_assert(c, m)
#else
#define KCTL_STATIC_ASSERT(c, m) _Static_assert(c, m)
#endif

#define KCTL_MAGIC          0x4C54434Bu /* "KCTL" read as little-endian u32 */
#define KCTL_VERSION        1u
#define KCTL_HEADER_LEN_V1  24u
#define KCTL_MAX_PAYLOAD    256u        /* v1 payloads are tiny; larger is a protocol error */

enum kctl_msg_type {
    /* control -> worker */
    KCTL_MSG_SET_CAPTURE = 0x0001,
    KCTL_MSG_STOP        = 0x0002, /* stop streaming and release the device; worker then exits */
    KCTL_MSG_PING        = 0x0003,
    KCTL_MSG_SET_HW      = 0x0004, /* kctl_set_hw_v1: hardware gain controls (live sources only) */
    /* M1: listener channels (DSP in the worker). Answered by ACK, then CHANNEL_APPLIED or FAILED. */
    KCTL_MSG_CHANNEL_OPEN  = 0x0010, /* kctl_channel_v1; channel_id must not exist */
    KCTL_MSG_CHANNEL_TUNE  = 0x0011, /* kctl_channel_v1: the full desired config of an existing channel */
    KCTL_MSG_CHANNEL_CLOSE = 0x0012, /* kctl_channel_close_v1 */

    /* worker -> control, replies (cmd_seq echoes the command) */
    KCTL_MSG_ACK         = 0x0081,
    KCTL_MSG_APPLIED     = 0x0082,
    KCTL_MSG_FAILED      = 0x0083,
    KCTL_MSG_PONG        = 0x0084,
    KCTL_MSG_CHANNEL_APPLIED = 0x0085, /* kctl_channel_applied_v1, answers CHANNEL_* */
    KCTL_MSG_HW_APPLIED  = 0x0086, /* kctl_hw_applied_v1, answers SET_HW */

    /* worker -> control, unsolicited (cmd_seq = 0) */
    KCTL_MSG_STATUS      = 0x00C1
};

/* Worker-level result codes. The control service maps these to E_* wire codes. */
enum kctl_result {
    KCTL_OK              = 0,
    KCTL_E_UNSUPPORTED   = 1, /* operation or value not supported by this source/device */
    KCTL_E_OUT_OF_RANGE  = 2,
    KCTL_E_DEVICE        = 3, /* driver/device error */
    KCTL_E_BUSY          = 4,
    KCTL_E_BAD_MESSAGE   = 5, /* malformed header/payload, bad sequence */
    KCTL_E_TIMEOUT       = 6, /* device did not settle within the worker's own bound */
    KCTL_E_NOT_FOUND     = 7  /* CHANNEL_TUNE/CLOSE for a channel_id that is not open */
};

/* Worker acquisition states reported by STATUS. */
enum kctl_worker_state {
    KCTL_STATE_STARTING  = 1,
    KCTL_STATE_RUNNING   = 2,
    KCTL_STATE_STOPPING  = 3,
    KCTL_STATE_FAILED    = 4  /* e.g. USB loss; worker exits after reporting */
};

struct kctl_header_v1 {
    uint32_t magic;       /*  0 KCTL_MAGIC */
    uint16_t version;     /*  4 KCTL_VERSION */
    uint16_t header_len;  /*  6 KCTL_HEADER_LEN_V1 */
    uint16_t msg_type;    /*  8 enum kctl_msg_type */
    uint16_t reserved0;   /* 10 must be 0 */
    uint32_t payload_len; /* 12 bytes following the header, <= KCTL_MAX_PAYLOAD */
    uint64_t cmd_seq;     /* 16 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_header_v1) == KCTL_HEADER_LEN_V1, "kctl header must be 24 bytes");
KCTL_STATIC_ASSERT(offsetof(struct kctl_header_v1, cmd_seq) == 16, "cmd_seq offset");

/* SET_CAPTURE flags: which fields the command changes. At least one must be set. */
enum kctl_set_capture_flags {
    KCTL_SC_CENTER = 1u << 0,
    KCTL_SC_RATE   = 1u << 1
};
#define KCTL_SC_DEFINED_MASK 0x00000003u

struct kctl_set_capture_v1 {
    int64_t  center_hz;      /*  0 valid iff KCTL_SC_CENTER */
    uint32_t sample_rate_hz; /*  8 valid iff KCTL_SC_RATE */
    uint32_t flags;          /* 12 enum kctl_set_capture_flags */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_set_capture_v1) == 16, "set_capture payload must be 16 bytes");

struct kctl_ack_v1 {
    uint32_t result;   /* 0 KCTL_OK = accepted, otherwise rejected with enum kctl_result */
    uint32_t reserved; /* 4 must be 0 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_ack_v1) == 8, "ack payload must be 8 bytes");

/* APPLIED reports what the source/driver read back, not what was requested. */
struct kctl_applied_v1 {
    uint64_t capture_gen;    /*  0 generation now in effect */
    int64_t  center_hz;      /*  8 readback */
    uint32_t sample_rate_hz; /* 16 readback */
    uint32_t verified;       /* 20 0 = none, 1 = driver_readback (rf_evidence is never set by a worker) */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_applied_v1) == 24, "applied payload must be 24 bytes");

struct kctl_failed_v1 {
    uint32_t result;   /* 0 enum kctl_result, never KCTL_OK */
    uint32_t reserved; /* 4 must be 0 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_failed_v1) == 8, "failed payload must be 8 bytes");

/* Listener channel modes and AGC (M1 design §3.2; docs/design/M1-dsp.md). */
enum kctl_mode { KCTL_MODE_AM = 1, KCTL_MODE_USB = 2, KCTL_MODE_LSB = 3, KCTL_MODE_CW = 4,
                 KCTL_MODE_NFM = 5, KCTL_MODE_WFM = 6 };
enum kctl_agc { KCTL_AGC_OFF = 1, KCTL_AGC_FAST = 2, KCTL_AGC_MEDIUM = 3, KCTL_AGC_SLOW = 4 };

/* kctl_channel_v1.flags */
enum kctl_channel_flags {
    KCTL_CH_DEFAULT_PASSBAND = 1u << 0, /* ignore pass_lo/hi; use the mode's default */
    KCTL_CH_DEFAULT_BFO      = 1u << 1  /* ignore bfo_hz; 700 Hz */
};
#define KCTL_CH_DEFINED_MASK 0x00000003u
#define KCTL_MAX_CHANNELS    16u        /* per worker; more is refused with KCTL_E_BUSY */

/*
 * CHANNEL_OPEN / CHANNEL_TUNE payload. All frequencies integer Hz.
 * offset_hz is the tuned frequency relative to the capture centre. Passband edges are relative
 * to the tuned frequency. Invalid values are refused at ACK with KCTL_E_OUT_OF_RANGE (passband,
 * span, rate, BFO) or KCTL_E_UNSUPPORTED (unknown mode/agc); the channel is then unchanged.
 * TUNE that changes only offset_hz is a phase-continuous retune: channel_gen is kept. Any other
 * change rebuilds the channel's DSP and bumps channel_gen.
 */
struct kctl_channel_v1 {
    uint32_t channel_id;      /*  0 chosen by the control service */
    uint16_t mode;            /*  4 enum kctl_mode */
    uint16_t agc;             /*  6 enum kctl_agc */
    int64_t  offset_hz;       /*  8 */
    int32_t  pass_lo_hz;      /* 16 */
    int32_t  pass_hi_hz;      /* 20 */
    int32_t  bfo_hz;          /* 24 CW pitch, 100..3000 */
    int32_t  manual_gain_cdb; /* 28 AGC off: fixed gain, centi-dB */
    uint32_t flags;           /* 32 enum kctl_channel_flags */
    uint32_t reserved;        /* 36 must be 0 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_channel_v1) == 40, "channel payload must be 40 bytes");
KCTL_STATIC_ASSERT(offsetof(struct kctl_channel_v1, offset_hz) == 8, "offset_hz offset");

struct kctl_channel_close_v1 {
    uint32_t channel_id; /* 0 */
    uint32_t reserved;   /* 4 must be 0 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_channel_close_v1) == 8, "channel close payload must be 8 bytes");

/* CHANNEL_APPLIED: the channel as the worker now runs it (defaults resolved). */
struct kctl_channel_applied_v1 {
    uint64_t channel_gen;            /*  0 current (for CLOSE: the last) */
    uint64_t capture_gen;            /*  8 */
    struct kctl_channel_v1 channel;  /* 16 resolved config; flags = 0 */
    uint32_t channel_rate_hz;        /* 56 complex rate inside the channel (12000/24000/192000) */
    uint32_t open;                   /* 60 1 = open, 0 = closed by this command */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_channel_applied_v1) == 64, "channel applied payload must be 64 bytes");

/*
 * SET_HW (M1, Airspy HF+): hardware gain controls. flags selects the fields to change; the
 * others keep their current values. ACK (E_UNSUPPORTED from sources without these controls,
 * E_OUT_OF_RANGE for a bad value, E_BAD_MESSAGE for unknown flags/reserved), then HW_APPLIED
 * with every control's value now in effect, or FAILED (E_DEVICE) if the driver refused.
 * Gain changes do not start a new capture generation.
 */
enum kctl_set_hw_flags {
    KCTL_HW_AGC           = 1u << 0, /* hf_agc: 0 off, 1 on */
    KCTL_HW_AGC_THRESHOLD = 1u << 1, /* hf_agc_threshold: 0 low, 1 high (acts with AGC on) */
    KCTL_HW_LNA           = 1u << 2, /* hf_lna: 0 off, 1 on */
    KCTL_HW_ATT           = 1u << 3  /* hf_att: 0..8 attenuator index (acts with AGC off) */
};
#define KCTL_HW_DEFINED_MASK 0x0000000Fu
#define KCTL_HW_ATT_MAX 8u

struct kctl_set_hw_v1 {
    uint32_t flags;            /*  0 enum kctl_set_hw_flags, at least one */
    uint8_t  hf_agc;           /*  4 */
    uint8_t  hf_agc_threshold; /*  5 */
    uint8_t  hf_lna;           /*  6 */
    uint8_t  hf_att;           /*  7 */
    uint64_t reserved;         /*  8 must be 0 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_set_hw_v1) == 16, "set_hw payload must be 16 bytes");

/* HW_APPLIED: the values set in the device. libairspyhf has no getters, so verified is 0
 * ("set and accepted by the driver"), never driver_readback. */
struct kctl_hw_applied_v1 {
    uint64_t capture_gen;      /*  0 */
    uint8_t  hf_agc;           /*  8 */
    uint8_t  hf_agc_threshold; /*  9 */
    uint8_t  hf_lna;           /* 10 */
    uint8_t  hf_att;           /* 11 */
    uint32_t verified;         /* 12 0 = none */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_hw_applied_v1) == 16, "hw applied payload must be 16 bytes");

struct kctl_status_v1 {
    uint32_t state;       /*  0 enum kctl_worker_state */
    uint32_t result;      /*  4 enum kctl_result; KCTL_OK unless state == FAILED */
    uint64_t capture_gen; /*  8 */
};
KCTL_STATIC_ASSERT(sizeof(struct kctl_status_v1) == 16, "status payload must be 16 bytes");

#ifdef __cplusplus
} /* extern "C" */
#endif

#endif /* K2WEBSDR_WORKER_CTL_V1_H */
