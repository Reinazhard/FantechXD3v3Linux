# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

* **HP / LP sensor mode** toggle (flash `0xb9`: 0=LP, 1=HP, from the vendor's
  `customComboBox_ModeSelect` and `sensor-mode-lp/hp` captures).
* **Pending-changes preview**: shows exactly which flash records Apply will
  write, with old -> new bytes; Discard reverts the draft.
* **Automated robust sync**: device -> server refresh that never clobbers an
  unsaved draft, is single-flight, throttled, and backs off on transport
  failure. Manual Reload now confirms before discarding a draft.
* **Server-Sent Events** (`/api/events`, stdlib `queue.Queue` fan-out): the
  backend is the sole authority and pushes compact state patches to the WebUI;
  `/api/sync-status` polling remains only as an `EventSource.onerror` fallback.
* **Post-write read-back**: on the real path every changed flash record is read
  back and compared; a mismatch raises `TransportError` -> HTTP 502, keeping
  the draft unsaved (no false success). `verification_mode` distinguishes
  `readback` / `framing-only` / `simulated`.
* **Capability-gated polling rates**: detected receiver PID narrows the rate
  list (`0xF535`/`0xF536` -> 125–1000 Hz, `0xF537` 4K dongle -> also 2000/4000
  Hz); backend `/api/settings` rejects unsupported rates.
* **MD3 Expressive WebUI** (stylesheet-only): spring/spatial motion tokens,
  XS–XL size scale with 48px touch targets, expressive radii and contained
  button groups, bolder label weights, raised tonal surfaces, thicker slider
  tracks, and one coherent Sensor-panel type hierarchy + divider rhythm.
* 20 reliability regression tests (`tests/test_reliability.py`): correlated
  `_transact()`, stale-echo purge, disconnect/reconnect, read-back mismatch,
  concurrent poll+apply, SSE delivery, validation and error shapes, PID gating.

### Fixed (sync follow-up)

* **Auto-sync no longer needs the Reload button**: a server-side background
  thread polls the device every 2s (no browser tab required) via a cheap
  single-record `read_active_dpi()` (flash `0x04`), with staggered backoff.
  The client subscribes to SSE `/api/events` (polling `/api/sync-status` only
  as a fallback).
* **Hardware DPI cycling now syncs to the web selection**: sync MERGES the
  device-observed active DPI index into the current profile instead of
  skipping whenever a draft exists. User-authored draft fields are preserved;
  only the volatile `active_dpi_index` follows the hardware. A stale queued
  cmd-8 reply is flushed before each cheap read so a cycle is never missed.
* All device I/O (Apply, full read, cheap poll, status) is serialized behind
  a reentrant lock so the background poll cannot interleave with a write and
  drop an ACK.
* Active DPI stage is now **device-owned and read-only** in the WebUI: stage
  tabs select which stage to edit, the hardware `● on` indicator is display
  only, and software no longer writes `active_dpi_index` from the UI.

### Changed / Fixed

* **Apply is delta-only**: `profile_to_records()` is the single source of
  truth shared by the writer and preview; Apply writes ONLY changed records.
  A single-field edit writes 1 record (was 22); a no-op Apply writes nothing.
  This removes the extreme mouse lag during Apply. Reduced per-record pump
  timeouts; ACK verification via the echo queue is unchanged.
* A real-device Apply now requires a valid cached device baseline; if the
  mouse is asleep it fails fast with "wake the mouse" instead of attempting
  a full 23-record write.

### Verified

* SIMULATED suite: `PYTHONPATH=src python -m pytest tests/ -q` -> **74 passed**
  (54 original + 20 reliability); capture gate
  `tests/test_protocol_captures.py` -> **14 passed** (golden frames unchanged).
* Headless CDP (both themes, five viewports): zero console errors, no
  horizontal overflow, single font family + tabular-nums preserved, MD3E shape
  morph verified. `node --check` on `app.js` clean.
* No physical-device read -> write -> read-back verification was performed for
  the reliability/MD3E changes; the real hidraw path remains a manual gate.

## [0.2.0] - 2026-11-09

First public release.

### Added

* Visual editor for the 6 physical buttons (Left/Right Click, Middle/Scroll,
  two left-side macro buttons, bottom DPI-cycle button), with the Left button
  reserved per the vendor guard rail.
* 4 DPI stages with a 50–26000 step-50 drag slider, per-stage "Save to stage",
  and a unique LED color per stage from the vendor's 14-color palette.
* Capture-confirmed HID feature-report protocol: 17-byte reports, `0x55`-seeded
  checksum, 16-bit big-endian flash address, startup unlock, poll clock,
  multi-report echo queue, and per-record ACK verification.
* Real `hidraw` transport (`HIDIOCSFEATURE`) with VID/PID autodetect, plus a
  `SimulatedTransport` for hardware-free use.
* Live connection mode (wired / 2.4 GHz dongle) and battery % + voltage;
  Bluetooth reported as not detectable.
* Profile import / export / restore; server-side dirty tracking.
* Stdlib HTTP JSON API and browser UI (single screen, tabbed inspector).
* `probe`, `validate`, `show` CLI commands; `--real` and `--real --dry-run`.
* Documentation: `docs/HARDWARE.md`, `docs/PROTOCOL.md`, `docs/EVIDENCE.md`,
  `docs/CAPTURE_GUIDE.md`, plus release/legal files.
* 40 pytest tests (encoder fixtures, hardware contract, unlock, poll clock,
  echo queue, HTTP API). No hardware required.

### Verified

* Encoder reproduces 243/243 unique captured SET_REPORT frames byte-for-byte.
* Live read → write → read-back on the connected `3554:f535` device, including
  DPI stages, colors, buttons, battery (15%, 3520 mV) and report rate.
* Browser UI Apply on the real backend reports success only after device ACK.

### Known limitations

* DPI register encoding capture-confirmed only to 3200 DPI; above that the UI
  warns and uses the single observed extended record.
* Battery requires the mouse to be awake.
* Bluetooth configuration unsupported.
* The vendor Windows app was never executed (static analysis + captures only).

[0.2.0]: https://github.com/Reinazhard/FantechXD3v3Linux/releases/tag/v0.2.0
