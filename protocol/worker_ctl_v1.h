/*
 * K2AAM WebSDR — worker control messages, version 1.
 *
 * Carried over a Unix stream socket between the control service and one
 * acquisition worker. Every message is a 24-byte header followed by
 * payload_len bytes. Little-endian, naturally aligned, offsets asserted.
 *
 * Command lifecycle (control -> worker):
 *   cmd_seq is assigned by the control service, strictly increasing per
 *   connection, never 0. The worker answers every command with exactly one
 *   ACK, then (if accepted) exactly one APPLIED or FAILED with the same
 *   cmd_seq. Commands are processed strictly in order.
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

    /* worker -> control, replies (cmd_seq echoes the command) */
    KCTL_MSG_ACK         = 0x0081,
    KCTL_MSG_APPLIED     = 0x0082,
    KCTL_MSG_FAILED      = 0x0083,
    KCTL_MSG_PONG        = 0x0084,

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
    KCTL_E_TIMEOUT       = 6  /* device did not settle within the worker's own bound */
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
