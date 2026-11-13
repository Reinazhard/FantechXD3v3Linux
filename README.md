# Helios XD3V3 Mouse Button Editor

[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
![Python](https://img.shields.io/badge/python-%3E%3D3.10-blue.svg)

An unofficial, dependency-free Linux configuration tool for the **Fantech
Helios II XD3v3** gaming mouse. It reconstructs the vendor Windows app's mouse
button editor from static analysis and USB packet captures, and writes settings
to the device over the Linux `hidraw` HID feature-report interface.

> Not affiliated with Fantech / Compx. Trademarks belong to their owners.
> See [NOTICE](NOTICE) for the reverse-engineering and no-warranty notice.

## Features

* Visual editor for the **6 physical buttons**: Left / Right Click,
  Middle / Scroll, two **left-side macro buttons**, and a **bottom DPI-cycle
  button** (defaults to "DPI Loop" — each press cycles to the next stage).
* **4 DPI stages** (`DPIMaxGrade=4`) with a 50–26000 step-50 drag slider and a
  **unique LED color per stage** from the vendor's 14-color palette.
* Polling rate, LOD, click debounce, motion sync, angle snapping, ripple
  control, **HP/LP sensor mode**, per-stage DPI/LED — all written to onboard
  flash with ACK verification (no silent false successes).
* Stage tabs select WHICH stage's DPI/LED to edit; the active DPI stage is
  device-owned (set by the bottom hardware button, shown read-only as `● on`).
  Polling rates are gated by the detected receiver: the 1K dongle/wired mouse
  expose 125–1000 Hz, the 4K dongle additionally 2000/4000 Hz.
* **Preview before Apply**: a "Pending changes" panel lists exactly which
  flash records Apply will write (old -> new bytes). Apply is **delta-only**:
  one edit writes one record, a no-op writes nothing — no device lag.
  Discard reverts the draft.
* **Automated sync (no Reload needed)**: the backend is the sole authority
  and pushes state changes to the WebUI over Server-Sent Events (`/api/events`,
  stdlib only). A server-side background thread still polls the device (~2s)
  even with no browser tab open; pressing the bottom DPI-cycle button updates
  the WebUI automatically (device-observed state is MERGED, user drafts are
  preserved). Polling-based `/api/sync-status` remains as an SSE fallback.
* Live connection mode (wired USB-C / 2.4 GHz dongle) and battery % + voltage.
  Bluetooth is reported as **not detectable** (it exposes no vendor HID config
  interface) rather than guessed.
* Profile import / export / restore; simulated backend for hardware-free use.

The protocol was recovered from 29 USBPcap captures (0.2.0 validated the
capture-confirmed encoder end-to-end on a real device). The reliability and
UI changes in this release are SIMULATED-verified only; no physical
read → write → read-back was performed for them. Full evidence:
[docs/HARDWARE.md](docs/HARDWARE.md), [docs/PROTOCOL.md](docs/PROTOCOL.md),
[docs/EVIDENCE.md](docs/EVIDENCE.md), [docs/CAPTURE_GUIDE.md](docs/CAPTURE_GUIDE.md).

## Install

Requires **Python ≥ 3.10** and Linux. No third-party runtime dependencies.

```bash
# from a clone
python -m pip install .
# or, without installing
PYTHONPATH=src python -m helios_editor.cli serve
```

Non-root access to the device needs a udev rule — see [SETUP.md](SETUP.md).

## Run

```bash
helios-editor serve                    # web UI, simulated device (no hardware)
helios-editor serve --real             # web UI, real device (writes to flash)
helios-editor serve --real --dry-run   # real framing, no ioctl performed
helios-editor probe                    # read-only status + battery + flash values
helios-editor validate profile.json
helios-editor show > default-profile.json
```

Open <http://127.0.0.1:8765>. The server binds to `127.0.0.1` by default; use
`--host` explicitly if you need another interface (see [SECURITY.md](SECURITY.md)).

`--real` autodetects the config interface by VID/PID `3554:f535` (2.4 GHz
dongle), `3554:f536` (wired), or `3554:f537` (4K dongle). It sends only
confirmed `WriteFlashData` records; the two-password startup unlock and an
interleaved poll clock keep
the firmware servicing commands. Apply reports success only after **every
changed** flash record is acknowledged AND read back from flash (a mismatch
returns HTTP 502 — no false success); on a sleeping mouse it fails loudly and
keeps changes flagged as unsaved. A single-field edit writes one record.
Running with no arguments starts the server in real-device mode.

### CLI examples (installed console script)

```bash
helios-editor --help
helios-editor serve --real --port 9000
```

Without installing, replace `helios-editor` with
`PYTHONPATH=src python -m helios_editor.cli`.

## Layout

```
src/helios_editor/
  catalog.py    function/sub-function catalog + decoded enums
  model.py      6 buttons, 4 DPI stages + LED colors, profile JSON, validation
  protocol.py   capture-confirmed 17-byte HID report encoder (checksum, addr)
  hardware.py   real hidraw transport (HIDIOCSFEATURE) + device autodetect
  transport.py  Transport base + Simulated + lazy real re-export
  server.py     stdlib HTTP JSON API + SSE /api/events + authoritative state
  cli.py        serve / probe / validate / show
  web/          MD3 Expressive visual editor (HTML/CSS/JS + 432x356 mouse SVG)
tests/          pytest suite (no hardware required; 74 tests)
docs/           HARDWARE, PROTOCOL, EVIDENCE, CAPTURE_GUIDE
udev/           50-helios-ii.rules
captures/       USBPcap evidence (gitignored; supply your own)
```

## Test

```bash
python -m pip install pytest
PYTHONPATH=src python -m pytest tests/ -q
```

74 tests cover the capture-pinned encoder (14 golden-frame tests), the
corrected hardware contract, the startup unlock, the poll clock, the
multi-report echo queue, delta writes, preview, HP/LP mode, post-write
read-back, SSE state delivery, reconnect, and auto-sync safety. No device is
needed.

## Known limitations

* DPI register encoding is capture-confirmed only up to **3200**; above that
  the UI warns and the only observed extended record is used. The vendor
  advertises up to 26000 DPI.
* Battery telemetry requires the mouse to be **awake**; when asleep the UI
  says so instead of showing a fake number.
* **Bluetooth** configuration is unsupported (no vendor HID config interface).
* Only fields proven by the captures are written; unknown flash regions are
  left untouched.
* The vendor Windows application was never executed; findings are static
  analysis + captures only.

## Contributing / Security

See [CONTRIBUTING.md](CONTRIBUTING.md) and [SECURITY.md](SECURITY.md).
Release history: [CHANGELOG.md](CHANGELOG.md).

## License

GPL-3.0-or-later. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
