# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [Unreleased]

### Added

* **HP / LP sensor mode** toggle (flash `0xb9`: 0=LP, 1=HP, from the vendor's
  `customComboBox_ModeSelect` and `sensor-mode-lp/hp` captures).
* **DPI stage cycling from the app**: clicking a stage tab sets it as the
  active DPI stage (staged, Apply to write) instead of relying only on the
  bottom hardware button.
* **Pending-changes preview**: shows exactly which flash records Apply will
  write, with old -> new bytes; Discard reverts the draft.
* **Automated robust sync**: device -> server refresh that never clobbers an
  unsaved draft, is single-flight, throttled, and backs off on transport
  failure. Manual Reload now confirms before discarding a draft.

### Fixed (sync follow-up)

* **Auto-sync no longer needs the Reload button**: a server-side background
  thread polls the device every 2s (no browser tab required) via a cheap
  single-record `read_active_dpi()` (flash `0x04`), with staggered backoff.
  The client only watches a heartbeat (`/api/sync-status`) and re-renders on
  a generation bump.
* **Hardware DPI cycling now syncs to the web selection**: sync MERGES the
  device-observed active DPI index into the current profile instead of
  skipping whenever a draft exists. User-authored draft fields are preserved;
  only the volatile `active_dpi_index` follows the hardware. A stale queued
  cmd-8 reply is flushed before each cheap read so a cycle is never missed.
* All device I/O (Apply, full read, cheap poll, status) is serialized behind
  a reentrant lock so the background poll cannot interleave with a write and
  drop an ACK.

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

* Real device: two staged changes -> 2 records in 2.09s; read-back confirmed
  mode=HP and DPI=1200; no-op Apply 0 records / 0.00s; restored to defaults.
* 50 pytest tests pass (incl. delta-write, preview, staged DPI, HP/LP, and
  auto-sync-does-not-clobber-draft regressions).

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
