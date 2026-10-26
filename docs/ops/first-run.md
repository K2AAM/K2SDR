# Trying the M0 service locally

M0 runs **replay receivers only**: it plays the small test recordings in
`fixtures/iq/`. It never opens an SDR and never touches OpenWebRX+. It listens
on `127.0.0.1:8075` only; OWRX stays on 8073.

## On the Optiplex (as `k2dev`)

```bash
cd ~/K2SDR
git pull
make sync          # once, or after dependencies change
make run-dev       # builds the worker and starts the service; Ctrl+C stops it
```

The first start prints a line like:

```
first-run setup pending: open /setup and use the token in /home/k2dev/K2SDR/.dev-state/state/setup-token
```

The token itself is never printed. Read it with:

```bash
cat ~/K2SDR/.dev-state/state/setup-token
```

## Opening the page from your Mac

The service only listens on the Optiplex's own loopback address, so use an
SSH tunnel from the Mac (this is the "SSH/Tailscale tunnel" route; nothing is
exposed to the network):

```bash
ssh -N -L 8075:127.0.0.1:8075 k2aam@192.168.1.17
```

Use your own `k2aam` login for the tunnel (`k2dev` has no password or key from
the Mac). Over Tailscale, `100.79.117.125` works the same way.

Leave that running, then open <http://127.0.0.1:8075> in your Mac's browser.

1. You land on **First-run setup**. Paste the token, choose an admin
   username and a password of 12+ characters.
2. Sign in. The status page lists the two replay receivers, their capture
   generation and lease, and "0 of 74 baseline modes available" (correct for
   M0: no decoders exist yet). **Why?** shows the reason for every mode.

## Resetting

Stop the service and delete `~/K2SDR/.dev-state/` to start over with a new
setup token.

## What is deliberately not possible yet

- Binding to anything other than loopback (refused until admin MFA exists).
- Listening to audio, waterfalls, or any real receiver (M1).
