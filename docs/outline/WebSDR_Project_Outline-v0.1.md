# K2AAM WebSDR — Project Outline

Version 0.1 · 26 October 2026 · Working title

Status: proposed product and engineering scope. This document does not represent an implemented replacement or a deployment to any receiver.

## 1. Purpose

Build a self-hosted, receive-only web SDR with the useful functionality of OpenWebRX+, a clean interface, dependable mobile behavior, and explicit control of the receiver. Make antennas, decoders, audio, recording, and scanning native parts of the application.

Initial deployments are Adam's Airspy HF+ Discovery, SDRplay RSPdx2, and RX888 MkII, followed by the friend's receiver. Debian on x86-64 is the first supported server platform. Desktop browsers and iPhone Safari are first-class clients. Other operating systems and architectures follow verified driver support.

The application runs on the receiver host independently of ChatGPT. Development can use ChatGPT/Codex; operating the receiver requires no OpenAI connection. Normal radio reception and decoding do not require external cloud services. Online map tiles and optional source updates have their own network requirements.

Use established hardware drivers, DSP libraries, decoder programs, and RNNoise. Build a new application around explicit interfaces and state ownership. Carry forward upstream licenses, attribution, pinned versions, and source provenance for reused components.

## 2. Product requirements

| Area | Required behavior |
| --- | --- |
| Receiver | Click or type to tune, explicit Hz/kHz/MHz, selectable modes and passbands, optional presets and bookmarks |
| Hardware | RX888 MkII, Airspy HF+ Discovery, SDRplay; model-specific capabilities detected and validated |
| Antennas | Runtime port selection and custom labels, independently of profiles |
| Display | Responsive spectrum/waterfall and compact RIG controls, with optional map and decoder drawers |
| Modes | Track the complete current OpenWebRX+ mode/service inventory; integrate engines in tested stages |
| ALE | 2G decoding, fast server scanning, simultaneous monitoring where practical, dated public channel catalog, history/export |
| Audio | Standard demodulation and AGC, optional RNNoise adapted from the working PhantomSDR implementation, real bypass |
| Map | Plot locally received stations when decoded coordinates, grid squares, or clearly labeled location references exist |
| Administration | Receiver setup, access roles, users and blocks, bot deterrence, resource limits, health, backup, updates |
| Reliability | Explicit hardware ownership, mobile reconnection, process recovery, bounded storage, rollback |

Full mode parity is an overall project goal. The first usable build implements a smaller verified set. A mode appears as available only when its adapter, dependencies, hardware bandwidth, and signal format are supported; the admin view explains missing requirements.

## 3. Receiver experience

### Main layout

- Thin header: receiver selector, connection state, Map, Log, Settings.
- Spectrum and waterfall receive most of the screen. Sensible automatic levels, manual overrides, adjustable spectrum height, zoom, speed, colors, and optional band/bookmark overlays.
- Compact bottom RIG: clickable frequency, mode selector, passband, antenna, S-meter, volume/mute, AGC, RNNoise, recording, and step size.
- Secondary controls open deliberately in a drawer. No automatically reopening chat or floating status window.
- Decoder results open above the RIG in a resizable, collapsible tray. Collapse keeps decoding active. Map is an optional tab or side pane. On narrow screens, large views can take focus while the user chooses to retain or hide a waterfall preview.
- Remember layout and display preferences per browser. Preserve the meaningful distinction between a user's listening settings and global hardware settings.

### Tuning and gain

- Store frequencies as integer Hz across APIs, configuration, logs, and bookmarks. Units belong to input and display formatting.
- Accept explicit unit suffixes and display the interpreted frequency before a potentially disruptive retune. Bound all requests to the hardware's supported range and configured policy.
- Tune freely within the capture span. Outside it, an authorized operator can request a hardware retune. Saved profiles are optional convenience presets.
- Separate RF/IF hardware gain from each listener's demodulator AGC and volume. Expose Fast, Medium, Slow, and Off AGC with advanced parameters where supported.
- Show dBFS or a calibrated signal level with its calibration status. An S-meter is appropriate for these receive-only devices; an actual SWR measurement requires separate measurement hardware.

## 4. Hardware and antenna support

| Device | Initial adapter direction | Required validation |
| --- | --- | --- |
| Airspy HF+ Discovery | Evaluate native libairspyhf against the existing working Soapy route | Supported rates, tuning, gain/AGC ownership, calibration, sustained streaming, unplug/reopen |
| SDRplay RSPdx2 | Existing SDRplay API through a tested adapter, initially evaluate SoapySDRPlay3 | A/B/C ports, model/frequency restrictions, gain, notch/HDR controls, retune settling, service recovery |
| RX888 MkII | Benchmark an established Linux RX888/SDDC streaming path; compare Soapy and native routes | Real-sample format, USB throughput, firmware startup, decimation, usable span, sustained loss rate and multi-channel CPU load |

The driver boundary accepts both real ADC samples and complex IQ with an explicit sample format, rate, frequency origin, and discontinuity indication. Do not force the RX888 through an unnecessarily expensive format conversion.

The RX888 manufacturer lists existing multi-signal and streaming projects, and an RX888 Soapy implementation modified for PhantomSDR exists. These establish candidate integration paths, not a performance guarantee for this application. Select the RX888 backend after a measured prototype. [S5–S8]

### Antenna behavior

- Enumerate ports and supported settings from the actual device/adapter; show the switcher only when available.
- Preserve custom labels: **A — VHF**, **B — Experimental**, **C — HF** on Adam's RSPdx2.
- Antenna selection persists independently of band presets. A preset changes it only if explicitly configured to do so.
- Show a pending command and then the acknowledged port. Distinguish a driver's reported state from a hardware-level verification when that distinction exists.
- Antenna changes affect the receiver and its listeners. Enforce operator permissions and serialize changes with retunes.
- Validate live B/C switching with controlled or known RF evidence, in addition to driver readback. Test restart behavior and incompatible frequency/port combinations.

## 5. Audio and RNNoise

Use the RNNoise approach already present in Adam's PhantomSDR-Plus fork. Its wrapper loads **@jitsi/rnnoise-wasm** on demand, and the application includes resampling, frame buffering, aligned mixing, and listening-path integration. The port must include the relevant integration behavior; copying the small wrapper alone would not reproduce the result. [S2–S4]

Requirements:

- Standard demodulation, passband filtering, and AGC form the baseline radio audio path.
- RNNoise is an optional listening enhancement, bypassed by default, with one strength control and a clearly visible state.
- Reuse the established engine and useful integration behavior; avoid adding a second competing noise reduction system to the first release.
- Use a separate recurrent state for each audio stream. Handle rate conversion, framing, scaling, reset, and dry/wet alignment explicitly.
- Keep decoding audio upstream of listening enhancements, volume, mute, and listening squelch. A muted browser must not stop a server decoder.
- Restrict RNNoise to supported speech modes. CW, FT8, ALE, other data, and unsupported modes automatically bypass it.
- Model loading failure or processing failure leaves normal audio available and gives a concise explanation.
- Package the required assets with the release. No microphone permission or audio upload is required.
- On mobile reconnection, recover the audio graph and show an explicit tap-to-resume control if the browser requires user interaction.

Acceptance: listen to the same AM/SSB recordings through the known Phantom implementation and the new integration; compare output, level, delay, artifacts, and bypass. Verify that toggling RNNoise cannot change decoder input.

## 6. Modes and decoder integration

Use the current fork's OpenWebRX+ mode registry as the baseline inventory, frozen and checked into the new project's compatibility ledger. The registry reviewed for this outline contains **74 configured entries, including recording and background services**. This is not a claim of 74 independent waveforms or existing support in the new application. [S1]

| Family | Baseline scope |
| --- | --- |
| Conventional reception | AM, SAM, USB, LSB, CW, narrow FM, wide FM, DATA pass-through |
| Weak-signal amateur | FT8, FT4, JT65, JT9, WSPR, FST4, FST4W, Q65, MSK144, JS8Call |
| HF text and images | BPSK31/63, RTTY variants, SITOR-B, NAVTEX, DSC, ALE 2G, CW decoder/skimmer, RTTY skimmer, SSTV, fax |
| Digital voice | DMR, D-Star, NXDN, YSF, P25, M17, FreeDV, TETRA, RADE lower/upper variants |
| Broadcast digital | DRM, DAB, HDR/HD Radio |
| Transport and positions | Packet/APRS, AIS, HFDL, VDL2, ACARS, ADS-B, UAT |
| Other telemetry | Paging, SelCall, ZVEI, EAS, ISM, WMBus, LoRa variants, Meshtastic, Meshcore, MeshCom, radiosondes |
| Other services | Speech transcription, audio recording, Meteor LRPT and Elektro LRIT capture/processing |

Decoder engines run as supervised adapters with explicit input formats, timestamps, stream identity, sample-rate requirements, health state, resource budgets, and structured output. Reuse mature engines where available. Keep raw text/details alongside normalized fields and confidence/error indicators supplied by the engine. Treat decoded radio text as untrusted display input.

Prioritize conventional reception, FT8/FT4/WSPR, ALE 2G, RTTY, SSTV/fax, and relevant HF position modes. Integrate remaining families through the same adapter interface. Frequency coverage and bandwidth remain device-specific: selecting a mode cannot give an HF receiver an unsupported RF band.

Each compatibility-ledger entry records the adapter/engine version, required dependency, supported hardware/input conditions, reference sample, test result, live-validation status, and any limitation. Speech transcription can use a local optional engine; it need not introduce an external AI dependency.

## 7. Received-station map

The map is driven by actual received decode events. Every marker retains its originating event, receiver, mode, frequency, UTC, location source, and age.

| Available evidence | Map treatment |
| --- | --- |
| Decoded coordinates | Plot the reported position with timestamp and any available quality information |
| Decoded Maidenhead grid | Show the grid area or its clearly labeled center; preserve grid precision |
| Callsign/address with a reference entry | Show an approximate/reference location, with source and date |
| No usable location | Keep the event in the log and heard list; do not invent a marker |

Features: filter by mode, receiver, band and time; marker clustering, aging/fading, selected-event details, optional path from the receiving site, and optional track history for moving stations. Distance and bearing require a configured receiver position.

FT8 messages do not always contain a grid, and an ALE address alone does not establish a current physical position. Reference locations must stay distinguishable from decoded positions. A destination address is not proof that the destination was heard locally.

Bundle the map UI locally. Use a configurable tile provider with proper attribution; an optional offline tile route can follow. External spot reporting remains an explicit opt-in feature. Keep the station map distinct from any admin view of connected users.

## 8. ALE scanning and the 2026 channel catalog

### Receive and scan behavior

- ALE 2G is the first supported ALE waveform. Track 3G as a separate later decoder and validation project.
- The server owns scanning. Closing or suspending a browser does not cancel it. Reconnecting clients request the existing state and history.
- Offer manual hold/resume, channel skip/temporary lockout, network banks, priority channels, per-bank dwell, quiet release and a maximum hold.
- Use ALE-specific detection and valid decoder evidence for holds. Ordinary noise or a high S-meter reading alone must not create endless stops.
- Keep bounded pre-roll where useful, and preserve sample timing and stream generations. Reject output from a previous frequency/session after a retune.
- Show actual cycle time, measured channel visits per second, missed/deferred visits, detection holds and decoder errors.

### Two monitoring strategies

1. **Retuned scan:** visit channels outside one capture span by changing hardware frequency. Aim to validate **2–5 channels per second** where hardware settling and acquisition allow it. This is a measured target, not a guaranteed universal setting. Include slower profiles for weaker signals or slower hardware. HFLINK documents hardware capable of 2–5 channels per second and also recommends slower rates in its amateur operating guidance. [S9–S10]
2. **Simultaneous monitoring:** extract several channel streams from one captured span and monitor them concurrently. This is particularly attractive for a wide RX888 capture and also applies to nearby channels on the other receivers. Establish CPU, bandwidth, and decoder limits through benchmarks. The number of useful simultaneous channels is not assumed in advance.

When several channels are monitored, a detected signal selects what the operator hears without stopping the other permitted decoder streams. When physical retuning is required, communicate its effect to other listeners and follow the configured ownership policy.

### Channel data

Ship a versioned public-source catalog reviewed during 2026. Store frequency in Hz, sideband/dial convention, waveform, network, region, source URL, source publication date when available, review date, evidence type/confidence, optional reported activity date, and notes.

Candidate banks include HFGCS, COTHEN, public SHARES/federal monitoring candidates, amateur HFN/HFL, and user-defined lists. Inclusion depends on evidence; a bank title does not certify every channel's current use.

Distinguish published channel plans, public first-hand monitoring reports, and this receiver's own confirmed decodes. A 2026 retrieval/review date is not a 2026 publication or an activity confirmation. The HFLINK channel page checked for this outline still states an effective date of **15 July 2019**; the UDXF HFGCS PDF checked is revised **17 May 2025**. Retain those dates when using them as references. [S11–S12]

Catalog updates are optional, previewable, and reversible. Keep existing local edits and a last-known-good snapshot. Support validated CSV/JSON import/export, deduplication, and per-user inclusion/exclusion. Research and validate the actual initial catalog during the ALE milestone; this outline does not claim that a complete 2026 list has already been assembled.

### Results and recording

- Preserve raw words/events and assemble higher-level soundings, calls and AMD messages where justified by protocol evidence.
- Identify from/to and confidence without turning an address lookup into a confirmed station identity.
- Deduplicate display repetitions while retaining raw detail and counts.
- Provide heard stations, searchable UTC logs, channel activity, CSV/JSON export, and optional short audio clips around validated detections.
- Bound retention and storage in admin settings. Historical entries remain clearly historical and do not trigger new holds.
- Persist scan configuration; provide an explicit, initially disabled resume-after-restart policy with startup validation and a visible stop control.

## 9. Administration and access control

Build the essential admin controls early so prototype deployments can remain private. Expand the panel as the receiver grows.

| Area | Controls |
| --- | --- |
| Devices | Discover/select by serial, set safe ranges and rates, labels, port names, gain, calibration and supported features |
| Receiver setup | Presets, bookmarks, default modes, server jobs, scanner policy, antenna startup behavior |
| Accounts | Private/invite-only/public operation, listener/operator/admin roles, revoke sessions, disable users |
| Receiver permissions | Listen, decoder access, hardware retune, antenna changes, server scan and scheduling, recording |
| Abuse controls | Kick/ban accounts, temporary/permanent IP/CIDR blocks, login limits, connection and command rate limits, concurrent-session and decoder quotas |
| Bot deterrence | Optional browser challenges at admission, access tokens, edge rules, configurable user-agent blocks as a supplementary measure |
| Health | CPU, memory, stream dropouts, buffer depth, sample loss, decoder status, disk usage and driver versions |
| Maintenance | Redacted diagnostics, backup/restore, update preview, configuration migrations, rollback and audit history |

No bot filter identifies all automation. Rate limits, authentication and bounded resources provide enforceable protection; user-agent labels alone do not.

Enforce permissions on the server for every relevant command, including WebSocket commands. Validate origins, payload sizes and allowed fields; escape decoded content; protect admin mutations against cross-site requests. Use secure password storage, scoped sessions, and admin MFA/passkeys through the chosen authentication implementation. Launch with a safe first-run admin setup and no default password.

Support HTTPS/reverse proxies and Cloudflare Tunnel. Derive client IPs only from explicitly trusted proxies; do not accept arbitrary forwarded headers as identity. Keep admin reachability configurable, including a Tailscale/private-access option. Run the application and decoder processes with the required limited privileges and resource bounds.

## 10. Additional features included in scope

- **Recording:** reliable WAV audio download first, clear recording state and filenames containing UTC/frequency/mode; add compressed formats and server jobs as validated. Default to clean demodulated audio; offer an explicit “as heard” option if enhanced-audio recording is implemented. Longer IQ recording is a separate storage-controlled feature.
- **Presets/bookmarks:** import existing receiver presets where useful, with a preview; custom groups, optional band defaults, remembered last frequency and mode. No mandatory profile rewriting to tune or change antennas.
- **Useful signal controls:** Fast/Medium/Slow/Off AGC, passband presets and drag handles, squelch, calibrated-or-relative S-meter, optional established impulse blanker with explicit bypass.
- **Mobile:** large touch targets, frequency keypad, portrait/landscape layouts, resilient reconnect, and real-device suspend/network-change testing. Browser audio may require a tap after suspension; server jobs stay independent.
- **Activity/history:** heard stations, time/band filters, raw decoder details, channel heatmap and optional grayline overlay after the core map is usable.
- **Remote operation:** health monitoring, diagnostic export, automatic worker recovery within bounded retries, visible degraded state and an explicit policy for restoring server jobs.
- **Maintainable updates:** fingerprinted static assets, an uncached/revalidated application shell, versioned API/configuration, pinned dependencies, reversible releases, and automatic configuration backup before migrations.
- **Extension interfaces:** documented decoder and hardware APIs. Extensions are admin-installed trusted code; a broken decoder must not take down the receiver service.

## 11. Proposed implementation structure

| Component | Initial direction | Responsibility |
| --- | --- | --- |
| Receiver worker | Native C/C++ with established drivers and DSP libraries | Hardware I/O, sample timing, channel extraction, filtering, demodulation, FFT and streaming |
| Control service | Python with a typed HTTP/WebSocket API | Authentication, command ownership, state, jobs, administration and supervision |
| Decoder workers | Existing engines behind a documented adapter protocol | Audio/IQ decoding, structured events, health and controlled lifecycle |
| Browser | TypeScript with a modern component framework, initially Svelte | Waterfall rendering, RIG, map, logs, admin, audio playback and RNNoise |
| Storage | SQLite plus bounded files | Configuration metadata, jobs, decode events, sources, users, audit and recordings |
| Deployment | Debian packages/releases and systemd first | Repeatable install, permissions, service lifecycle, backup and rollback |

Confirm exact DSP, streaming and UI dependencies in the prototype. Prefer one maintainable deployment over premature distributed infrastructure. Keep hardware I/O and real-time DSP out of Python request handlers and browser UI callbacks.

The server owns global device state. Give commands identifiers and acknowledged results; expose a versioned state snapshot on reconnect. Distinguish requested, pending, applied and failed states. Stream events carry receiver ID, stream/session generation, frequency, timestamps and provenance where relevant.

Per-listener tuning inside the available capture and per-listener audio controls can be independent. Capture frequency, sample rate, physical antenna and hardware gain affect the physical device. Operator control and background jobs use an explicit ownership policy so neither silently overrides the other. Only one acquisition process opens a given physical SDR at a time.

## 12. Milestones and release gates

| Milestone | Deliverable | Completion evidence |
| --- | --- | --- |
| 0 — Foundation | Repository, architecture decisions, versioned protocol, IQ replay fixture, baseline mode ledger, minimal private admin | Deterministic replay and command/state tests; explicit hardware ownership |
| 1 — First live receiver | Airspy, AM/USB/LSB/CW/FM, waterfall, tuning, compact RIG, basic recording | Actual RF and recorded-signal tests; first overnight run; audio continuity and format measured |
| 2 — Hardware and audio | SDRplay with antenna controls, RX888 prototype and selected backend, RNNoise, AGC, gain controls | Real device matrix; controlled port switch tests; RNNoise comparison and decoder isolation; measured RX888 throughput |
| 3 — Decode and map | FT8/FT4/WSPR, common structured event pipeline, received-station map and persistent logs | Reference audio plus live reception; map provenance/precision; UI and export checks |
| 4 — ALE | 2G scanner, dated initial catalog, fast-scan profiles, simultaneous-channel prototype, holds and audio clips | Scan/detection benchmark, noise false-hold tests, multi-channel resource measurements, mobile disconnect trial |
| 5 — Broader parity | Remaining mode families and optional background services, expanded admin and operations | Compatibility ledger with reference results and explicit dependency/hardware limitations |
| 6 — Remote-ready release | Packaged installation, migration tooling, update/rollback, documentation and hardening | At least 72-hour mixed-workload trial at declared capacity; restart/network/driver-failure tests; backup restore and rollback trial |

Security, permissions, mobile behavior, resource bounds and diagnostics are tested throughout these milestones. The final stage consolidates them; it is not their starting point.

Use the Optiplex as the initial live test host and recorded IQ/audio for repeatable development. Development may serve its UI on another port beside OWRX, but cannot open the same physical receiver concurrently. Keep the remote OWRX deployment available during the transition, and switch acquisition ownership deliberately for test windows.

No calendar promise or supported listener/channel count is assigned before the first hardware and throughput measurements. Define those limits as benchmarked release profiles. Call a release mode-complete only when every baseline ledger entry has a functioning, tested integration under its documented requirements.

## 13. Acceptance checklist

- All frequencies remain consistent across UI, server state, recording metadata and decoder output; unit mistakes are rejected or made visible.
- Switching bands/presets does not unexpectedly reset the physical antenna or listening audio choices.
- Unacknowledged controls never appear successfully applied.
- Decoder audio remains identical when RNNoise, mute and volume change.
- Scan speed is measured together with detection/decoding success and false holds on an identified signal corpus.
- A browser suspend, discard, disconnect or reconnect does not cancel a server job or silently reset its mode.
- An authorized manual takeover has a predictable, visible effect on a running scan.
- Old-generation decoder events cannot appear on the new frequency after a retune.
- Map locations retain their evidence, age and precision; unknown positions stay unknown.
- Map, decoder results and controls remain usable on desktop and a real iPhone, including rotation and keyboard entry.
- Permission and rate-limit tests cover HTTP and WebSocket paths, proxy IP handling, account revocation and untrusted decoded text.
- Logs/recordings obey retention limits and survive intended restarts. Disk-full behavior is visible and bounded.
- Worker failure, USB loss and dependency failure do not hang the whole application; recovery and stop behavior are demonstrated.
- Fresh install, upgrade, configuration migration, backup restore and rollback are exercised on a clean supported Debian system.
- The remote-ready build completes its declared workload trial and records CPU, memory, sample loss, audio interruptions and disk growth.

## 14. Sources reviewed

Sources were reviewed on 26 October 2026. They inform scope and candidate components; their existence does not establish new-application compatibility or a complete current frequency catalog.

- **S1 — OpenWebRX+ baseline registry:** [K2AAM/openwebrx-k2aam, owrx/modes.py](https://github.com/K2AAM/openwebrx-k2aam/blob/86915038202fb273c13274420c7e882b32b19a0a/owrx/modes.py). Reviewed Git blob: `ad85d5c7de124f0c125e5b045ada283a97acc1c9`.
- **S2 — Phantom RNNoise wrapper:** [frontend/src/lib/rnnoise.js](https://github.com/K2AAM/PhantomSDR-Plus/blob/eedde39a3e53975236933e73d2339a49b7411558/frontend/src/lib/rnnoise.js), with integration in [frontend/src/audio.js](https://github.com/K2AAM/PhantomSDR-Plus/blob/eedde39a3e53975236933e73d2339a49b7411558/frontend/src/audio.js) and dependency in [frontend/package.json](https://github.com/K2AAM/PhantomSDR-Plus/blob/eedde39a3e53975236933e73d2339a49b7411558/frontend/package.json).
- **S3 — Phantom functionality and credits:** [sv1btl/PhantomSDR-Plus](https://github.com/sv1btl/PhantomSDR-Plus), [license](https://github.com/K2AAM/PhantomSDR-Plus/blob/eedde39a3e53975236933e73d2339a49b7411558/LICENSE), [additional terms](https://github.com/K2AAM/PhantomSDR-Plus/blob/eedde39a3e53975236933e73d2339a49b7411558/ADDITIONAL_TERMS.md).
- **S4 — RNNoise upstream:** [xiph/rnnoise](https://github.com/xiph/rnnoise).
- **S5 — RX888 manufacturer software list:** [RX888 Software](https://www.rx-888.com/rx/guide/clients.html).
- **S6 — Existing RX888 adapter candidate:** [ON5HB/RX888MK2-Soapy](https://github.com/ON5HB/RX888MK2-Soapy). Its README explicitly limits the testing claim to its stated PhantomSDR use.
- **S7 — SDR driver abstraction:** [pothosware/SoapySDR](https://github.com/pothosware/SoapySDR) and [pothosware/SoapySDRPlay3](https://github.com/pothosware/SoapySDRPlay3).
- **S8 — Airspy HF+ library:** [airspy/airspyhf](https://github.com/airspy/airspyhf).
- **S9 — ALE hardware scan-rate examples:** [HFLINK recommended transceivers](https://hflink.com/hardware/).
- **S10 — Amateur ALE operating guidance:** [HFLINK sounding guidelines](https://hflink.com/sounding/).
- **S11 — Amateur channel reference:** [HFLINK channels](https://hflink.com/channels/), page effective date 15 July 2019.
- **S12 — HFGCS monitoring reference:** [UDXF USAF-HFGCS.pdf](https://www.udxf.nl/USAF-HFGCS.pdf), revision date 17 May 2025.

## Appendix — Baseline mode/service ledger

The following inventory is generated directly from the active registry entries in S1. “Required feature” names are the registry's dependency keys, with default WSJT-X and JS8Call requirements made explicit. Empty cells do not mean the new project already implements an entry or that it has no underlying DSP/decoder dependency.

| ID | Name | Kind | Required feature |
| --- | --- | --- | --- |
| `nfm` | FM | Receiver/decoder mode |  |
| `wfm` | WFM | Receiver/decoder mode |  |
| `am` | AM | Receiver/decoder mode |  |
| `lsb` | LSB | Receiver/decoder mode |  |
| `usb` | USB | Receiver/decoder mode |  |
| `cw` | CW | Receiver/decoder mode |  |
| `sam` | SAM | Receiver/decoder mode |  |
| `usbd` | DATA | Receiver/decoder mode |  |
| `dmr` | DMR | Receiver/decoder mode | digital_voice_digiham |
| `dstar` | D-Star | Receiver/decoder mode | digital_voice_digiham |
| `nxdn` | NXDN | Receiver/decoder mode | digital_voice_digiham |
| `ysf` | YSF | Receiver/decoder mode | digital_voice_digiham |
| `p25` | P25 | Receiver/decoder mode | digital_voice_digiham |
| `m17` | M17 | Receiver/decoder mode | digital_voice_m17 |
| `freedv` | FreeDV | Receiver/decoder mode | digital_voice_freedv |
| `tetra` | TETRA | Receiver/decoder mode | tetra |
| `radel` | RADEL | Receiver/decoder mode | digital_voice_rade |
| `radeu` | RADEU | Receiver/decoder mode | digital_voice_rade |
| `drm` | DRM | Receiver/decoder mode | drm |
| `dab` | DAB | Receiver/decoder mode | dab |
| `hdr` | HDR | Receiver/decoder mode | hdradio |
| `bpsk31` | BPSK31 | Receiver/decoder mode |  |
| `bpsk63` | BPSK63 | Receiver/decoder mode |  |
| `rtty170` | RTTY-170 (45) | Receiver/decoder mode |  |
| `rtty450` | RTTY-450 (50N) | Receiver/decoder mode |  |
| `rtty85` | RTTY-85 (50N) | Receiver/decoder mode |  |
| `sitorb` | SITOR-B | Receiver/decoder mode |  |
| `navtex` | NAVTEX | Receiver/decoder mode |  |
| `dsc` | DSC | Receiver/decoder mode |  |
| `ale2g` | ALE 2G (experimental) | Receiver/decoder mode | ale2g |
| `ft8` | FT8 | Receiver/decoder mode | wsjt-x |
| `ft4` | FT4 | Receiver/decoder mode | wsjt-x |
| `jt65` | JT65 | Receiver/decoder mode | wsjt-x |
| `jt9` | JT9 | Receiver/decoder mode | wsjt-x |
| `wspr` | WSPR | Receiver/decoder mode | wsjt-x |
| `fst4` | FST4 | Receiver/decoder mode | wsjt-x-2-3 |
| `fst4w` | FST4W | Receiver/decoder mode | wsjt-x-2-3 |
| `q65` | Q65 | Receiver/decoder mode | wsjt-x-2-4 |
| `msk144` | MSK144 | Receiver/decoder mode | msk144 |
| `js8` | JS8Call | Receiver/decoder mode | js8call |
| `packet` | Packet | Receiver/decoder mode | packet |
| `ais` | AIS | Receiver/decoder mode | packet |
| `page` | Page | Receiver/decoder mode | page |
| `cwdecoder` | CW Decoder | Receiver/decoder mode |  |
| `cwskimmer` | CW Skimmer | Receiver/decoder mode | skimmer |
| `rttyskimmer` | RTTY Skimmer | Receiver/decoder mode | skimmer |
| `sstv` | SSTV | Receiver/decoder mode |  |
| `fax` | Fax | Receiver/decoder mode |  |
| `selcall` | SelCall | Receiver/decoder mode | selcall |
| `zvei` | Zvei | Receiver/decoder mode | selcall |
| `eas` | EAS | Receiver/decoder mode | eas |
| `ism` | ISM | Receiver/decoder mode | ism |
| `wmbus` | WMBus | Receiver/decoder mode | ism |
| `hfdl` | HFDL | Receiver/decoder mode | hfdl |
| `vdl2` | VDL2 | Receiver/decoder mode | vdl2 |
| `acars` | ACARS | Receiver/decoder mode | acars |
| `adsb` | ADSB | Receiver/decoder mode | adsb |
| `uat` | UAT | Receiver/decoder mode | uat |
| `speech` | Speech Transcriber | Receiver/decoder mode | speech |
| `lora-wan` | LoRa WAN | Receiver/decoder mode | lora |
| `lora-aprs` | LoRa APRS | Receiver/decoder mode | lora |
| `lora-fanet` | LoRa FANET | Receiver/decoder mode | lora |
| `meshtastic` | Meshtastic | Receiver/decoder mode | meshtastic |
| `meshcore` | Meshcore | Receiver/decoder mode | lora |
| `meshcom` | MeshCom | Receiver/decoder mode | lora |
| `sonde-rs41` | Sonde RS41 | Receiver/decoder mode | sonde |
| `sonde-dfm9` | Sonde DFM9 | Receiver/decoder mode | sonde |
| `sonde-dfm17` | Sonde DFM17 | Receiver/decoder mode | sonde |
| `sonde-mts01` | Sonde MTS01 | Receiver/decoder mode | sonde |
| `sonde-m10` | Sonde M10 | Receiver/decoder mode | sonde |
| `sonde-m20` | Sonde M20 | Receiver/decoder mode | sonde |
| `audio` | Audio Recorder | Background service | mp3 |
| `meteor-lrpt` | Meteor-M2 LRPT | Background service | wxsat |
| `elektro-lrit` | Elektro-L LRIT | Background service | wxsat |
