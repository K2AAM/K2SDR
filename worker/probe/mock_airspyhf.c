/*
 * Mock libairspyhf for testing ksdr-airspyhf-probe without hardware.
 *
 * Implements only the functions the probe uses, against the real header. It
 * generates a complex tone at (MOCK_TONE_HZ - tuned frequency) offset, paced in
 * real time. Behaviour knobs (environment variables):
 *   MOCK_DEVICES=0|1           number of devices (default 1)
 *   MOCK_BUSY=1                open fails as if another process held the device
 *   MOCK_TONE_HZ=<hz>          absolute tone frequency (default 10000000 + 5000)
 *   MOCK_TONE2_HZ=<hz>         a second absolute tone (default 15000000 + 5000)
 *   MOCK_SETTLE_MS=<ms>        after set_freq, output noise for this long (default 30)
 *   MOCK_DROP_AT_S=<s>         from this time, lose samples before the next callback(s)
 *   MOCK_DROP_SAMPLES=<n>      one loss of n samples (default 1000)
 *   MOCK_DROP_PATTERN=a,b,c    instead: losses on consecutive callbacks (e.g. 1000,1000,500)
 * Like libairspyhf 1.6.8, transfer.dropped_samples is the loss immediately before
 * that block (per callback), not a running total.
 *   MOCK_UNPLUG_AT_S=<s>       stop streaming at this time (is_streaming -> 0)
 *   MOCK_UNPLUG_ONCE_FILE=<p>  ... only if <p> does not exist yet, and create it (one unplug across
 *                              worker restarts: the replugged device then keeps streaming)
 *   MOCK_RETUNE_LOSS=<n>       each set_freq while streaming silently skips n samples
 *                              (time advances, nothing delivered, nothing reported)
 *   MOCK_STOP_HANG=1           after an unplug, airspyhf_stop/close never return (as measured
 *                              in Window 1b, H7); MOCK_STOP_HANG=2: they never return at all
 *   MOCK_STALL_AT_S=<s>        from this time, no more callbacks while is_streaming stays 1
 *   MOCK_FAIL=<call>           that call returns AIRSPYHF_ERROR (e.g. set_hf_att, set_freq),
 *   MOCK_FAIL_SKIP=<n>         ... except its first n calls (default 0)
 *   MOCK_CALL_LOG=<path>       append one line per control call: "<call> <value>"
 * Test-only. Not part of any shipped binary.
 */
#include <airspyhf.h>

#include <math.h>
#include <stdatomic.h>
#include <pthread.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>
#include <unistd.h>

#define MOCK_SERIAL 0xA1B2C3D4E5F60718ULL
#define BLOCK 1024

struct airspyhf_device {
    uint32_t rate;
    double freq;
    atomic_int streaming;
    pthread_mutex_t mu;   /* guards freq, settle_until, t0, skip (set_freq runs on the caller's thread) */
    uint64_t skip;
    pthread_t thread;
    airspyhf_sample_block_cb_fn cb;
    void* ctx;
    double settle_until;  /* seconds since start */
    double t0;
    uint8_t att, lna, agc, agc_thr, lib_dsp;
    int32_t calib;
};

/* The list the real Airspy HF+ (firmware R5.0.1-CD, lib 1.6.8) reported in Window 1. */
static const uint32_t RATES[] = {912000, 768000, 650000, 456000, 384000, 228000, 192000};

static double now_s(void) {
    struct timespec ts;
    clock_gettime(CLOCK_MONOTONIC, &ts);
    return (double)ts.tv_sec + (double)ts.tv_nsec / 1e9;
}

static double env_d(const char* k, double def) {
    const char* v = getenv(k);
    return v ? atof(v) : def;
}

static atomic_int g_fail_calls;

static int mock_fail(const char* call) {
    const char* f = getenv("MOCK_FAIL");
    if (!f || strcmp(f, call) != 0) return 0;
    return atomic_fetch_add(&g_fail_calls, 1) >= (int)env_d("MOCK_FAIL_SKIP", 0);
}

static void call_log(const char* call, long long value) {
    const char* path = getenv("MOCK_CALL_LOG");
    if (!path) return;
    FILE* fp = fopen(path, "a");
    if (!fp) return;
    fprintf(fp, "%s %lld\n", call, value);
    fclose(fp);
}

static atomic_int g_unplugged;

static void maybe_hang(void) {
    int mode = (int)env_d("MOCK_STOP_HANG", 0);
    if (mode == 2 || (mode == 1 && g_unplugged)) for (;;) pause();
}

void ADDCALL airspyhf_lib_version(airspyhf_lib_version_t* v) {
    v->major_version = AIRSPYHF_VER_MAJOR;
    v->minor_version = AIRSPYHF_VER_MINOR;
    v->revision = AIRSPYHF_VER_REVISION;
}

int ADDCALL airspyhf_list_devices(uint64_t* serials, int count) {
    int n = (int)env_d("MOCK_DEVICES", 1);
    if (serials && count > 0 && n > 0) serials[0] = MOCK_SERIAL;
    return n;
}

int ADDCALL airspyhf_open_sn(airspyhf_device_t** device, uint64_t serial) {
    if (env_d("MOCK_DEVICES", 1) < 1 || serial != MOCK_SERIAL || env_d("MOCK_BUSY", 0) > 0) {
        *device = NULL;
        return AIRSPYHF_ERROR;
    }
    struct airspyhf_device* d = calloc(1, sizeof *d);
    d->rate = 768000;
    d->freq = 7000000;
    pthread_mutex_init(&d->mu, NULL);
    *device = d;
    return AIRSPYHF_SUCCESS;
}

static void stop_thread(airspyhf_device_t* d) {
    d->streaming = 0;
    if (d->thread) { pthread_join(d->thread, NULL); d->thread = 0; }
}

int ADDCALL airspyhf_close(airspyhf_device_t* d) {
    call_log("close", 0);
    maybe_hang();
    stop_thread(d);   /* also joins a thread that already ended on simulated unplug */
    pthread_mutex_destroy(&d->mu);
    free(d);
    return AIRSPYHF_SUCCESS;
}

int ADDCALL airspyhf_get_output_size(airspyhf_device_t* d) { (void)d; return BLOCK; }
int ADDCALL airspyhf_is_low_if(airspyhf_device_t* d) { return d->rate <= 650000; }  /* as measured */
int ADDCALL airspyhf_is_streaming(airspyhf_device_t* d) { return d->streaming; }

int ADDCALL airspyhf_get_samplerates(airspyhf_device_t* d, uint32_t* buffer, const uint32_t len) {
    (void)d;
    uint32_t n = sizeof RATES / sizeof RATES[0];
    if (len == 0) { *buffer = n; return AIRSPYHF_SUCCESS; }
    for (uint32_t i = 0; i < len && i < n; i++) buffer[i] = RATES[i];
    return AIRSPYHF_SUCCESS;
}

int ADDCALL airspyhf_set_samplerate(airspyhf_device_t* d, uint32_t rate) {
    call_log("set_samplerate", rate);
    if (mock_fail("set_samplerate")) return AIRSPYHF_ERROR;
    for (size_t i = 0; i < sizeof RATES / sizeof RATES[0]; i++)
        if (RATES[i] == rate) { d->rate = rate; return AIRSPYHF_SUCCESS; }
    return AIRSPYHF_ERROR;
}

int ADDCALL airspyhf_set_freq(airspyhf_device_t* d, const uint32_t f) {
    call_log("set_freq", f);
    if (mock_fail("set_freq")) return AIRSPYHF_ERROR;
    if (f < 9000 || f > 260000000) return AIRSPYHF_ERROR;
    pthread_mutex_lock(&d->mu);
    d->freq = f;
    /* settle_until is wall-clock based; sample time can lag it on a slow machine, so 0 must mean
       no junk at all rather than "until sample time catches up". */
    double settle_ms = env_d("MOCK_SETTLE_MS", 30);
    if (d->t0 > 0) {
        if (settle_ms > 0) d->settle_until = now_s() - d->t0 + settle_ms / 1000.0;
        d->skip += (uint64_t)env_d("MOCK_RETUNE_LOSS", 0);
    }
    pthread_mutex_unlock(&d->mu);
    return AIRSPYHF_SUCCESS;
}

int ADDCALL airspyhf_set_lib_dsp(airspyhf_device_t* d, const uint8_t f) {
    call_log("set_lib_dsp", f);
    d->lib_dsp = f;
    return AIRSPYHF_SUCCESS;
}
int ADDCALL airspyhf_set_hf_agc(airspyhf_device_t* d, uint8_t f) {
    call_log("set_hf_agc", f);
    if (mock_fail("set_hf_agc")) return AIRSPYHF_ERROR;
    d->agc = f;
    return AIRSPYHF_SUCCESS;
}
int ADDCALL airspyhf_set_hf_agc_threshold(airspyhf_device_t* d, uint8_t f) {
    call_log("set_hf_agc_threshold", f);
    d->agc_thr = f;
    return AIRSPYHF_SUCCESS;
}
int ADDCALL airspyhf_set_hf_lna(airspyhf_device_t* d, uint8_t f) {
    call_log("set_hf_lna", f);
    d->lna = f;
    return AIRSPYHF_SUCCESS;
}
int ADDCALL airspyhf_set_hf_att(airspyhf_device_t* d, uint8_t i) {
    call_log("set_hf_att", i);
    if (i > 8 || mock_fail("set_hf_att")) return AIRSPYHF_ERROR;
    d->att = i;
    return AIRSPYHF_SUCCESS;
}
int ADDCALL airspyhf_get_calibration(airspyhf_device_t* d, int32_t* ppb) { *ppb = d->calib; return AIRSPYHF_SUCCESS; }

int ADDCALL airspyhf_version_string_read(airspyhf_device_t* d, char* v, uint8_t len) {
    (void)d;
    strncpy(v, "MOCK firmware 0.0", len);
    return AIRSPYHF_SUCCESS;
}

int ADDCALL airspyhf_board_partid_serialno_read(airspyhf_device_t* d, airspyhf_read_partid_serialno_t* ps) {
    (void)d;
    memset(ps, 0, sizeof *ps);
    ps->part_id = 0xFEED;
    return AIRSPYHF_SUCCESS;
}

static void* stream_thread(void* arg) {
    struct airspyhf_device* d = arg;
    airspyhf_complex_float_t buf[BLOCK];
    double tone1 = env_d("MOCK_TONE_HZ", 10005000), tone2 = env_d("MOCK_TONE2_HZ", 15005000);
    double drop_at = env_d("MOCK_DROP_AT_S", -1), unplug_at = env_d("MOCK_UNPLUG_AT_S", -1);
    double stall_at = env_d("MOCK_STALL_AT_S", -1);
    const char* once = getenv("MOCK_UNPLUG_ONCE_FILE");
    if (once && unplug_at > 0 && access(once, F_OK) == 0) unplug_at = -1;   /* already happened */
    uint64_t drops[32];
    int ndrops = 0, next_drop = 0;
    const char* pat = getenv("MOCK_DROP_PATTERN");
    if (pat && *pat) {
        char* end = NULL;
        while (ndrops < 32) {
            unsigned long long v = strtoull(pat, &end, 10);
            if (end == pat) break;
            drops[ndrops++] = (uint64_t)v;
            if (*end != ',') break;
            pat = end + 1;
        }
    } else {
        drops[ndrops++] = (uint64_t)env_d("MOCK_DROP_SAMPLES", 1000);
    }
    uint64_t n = 0;
    double phase = 0;
    unsigned seed = 1;
    pthread_mutex_lock(&d->mu);
    d->t0 = now_s();
    const double t0 = d->t0;
    pthread_mutex_unlock(&d->mu);
    while (d->streaming) {
        double t = (double)n / d->rate;
        double wait = t0 + t + (double)BLOCK / d->rate - now_s();
        if (wait > 0) usleep((useconds_t)(wait * 1e6));
        if (unplug_at > 0 && t >= unplug_at) {
            if (once) { FILE* f = fopen(once, "w"); if (f) fclose(f); }
            g_unplugged = 1;
            d->streaming = 0;
            break;
        }
        if (stall_at > 0 && t >= stall_at) { while (d->streaming) usleep(10000); break; }
        uint64_t lost = 0;
        if (drop_at > 0 && t >= drop_at && next_drop < ndrops) { lost = drops[next_drop++]; n += lost; }
        pthread_mutex_lock(&d->mu);
        const double freq = d->freq, settle_until = d->settle_until;
        const uint64_t skip = d->skip;
        d->skip = 0;
        pthread_mutex_unlock(&d->mu);
        n += skip;   /* lost silently, like a device that discards on retune */
        int settling = t < settle_until;
        for (int i = 0; i < BLOCK; i++) {
            double off1 = tone1 - freq, off2 = tone2 - freq;
            double off = fabs(off1) < d->rate / 2.0 ? off1 : (fabs(off2) < d->rate / 2.0 ? off2 : 1e9);
            float nr = ((float)(rand_r(&seed) % 2001) - 1000.0f) / 100000.0f;
            float ni = ((float)(rand_r(&seed) % 2001) - 1000.0f) / 100000.0f;
            if (!settling && off < 1e8) {
                phase += 2 * M_PI * off / d->rate;
                buf[i].re = (float)(0.5 * cos(phase)) + nr;
                buf[i].im = (float)(0.5 * sin(phase)) + ni;
            } else {
                buf[i].re = nr * 20; buf[i].im = ni * 20;   /* settling: wideband junk */
            }
            n++;
            t = (double)n / d->rate;
            settling = t < settle_until;
        }
        airspyhf_transfer_t tr = {d, d->ctx, buf, BLOCK, lost};
        d->cb(&tr);
    }
    return NULL;
}

int ADDCALL airspyhf_start(airspyhf_device_t* d, airspyhf_sample_block_cb_fn cb, void* ctx) {
    call_log("start", 0);
    d->cb = cb;
    d->ctx = ctx;
    d->streaming = 1;
    return pthread_create(&d->thread, NULL, stream_thread, d) == 0 ? AIRSPYHF_SUCCESS : AIRSPYHF_ERROR;
}

int ADDCALL airspyhf_stop(airspyhf_device_t* d) {
    call_log("stop", 0);
    maybe_hang();
    stop_thread(d);
    return AIRSPYHF_SUCCESS;
}
