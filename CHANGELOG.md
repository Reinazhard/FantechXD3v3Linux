# Changelog

All notable changes to this project are documented here. Format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project uses
[Semantic Versioning](https://semver.org/spec/v2.0.0.html).

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
