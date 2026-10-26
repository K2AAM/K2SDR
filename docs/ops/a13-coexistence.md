# A13 — OpenWebRX+ coexistence trial

Run on the Optiplex as `k2dev`. Read-only for everything except the service
the trial starts itself. Takes about 10 seconds.

```bash
cd ~/K2SDR && git pull && make sync && make worker
python3 tools/coexist_check.py trial
```

The trial checks that, before, during and after our service runs on
127.0.0.1:8075 with a replay receiver:

- OWRX stays `active` with the same start time, `openwebrx` and
  `soapy_connector` PIDs, HTTP response on :8073, and SDR devices;
- our service listens on loopback only;
- none of our processes opens any USB device;
- `k2dev` cannot write any SDR device node;
- our service and worker stop cleanly and leave nothing running.

Pass criterion: `A13 automated checks: N/N passed`. Audio continuity is a
manual check: listen to OWRX while the trial runs and report what you heard.
