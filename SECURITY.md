# Security Policy

## Scope

This is a local, userspace HID configuration tool. It does not listen on the
network by default, does not collect telemetry, and does not ship binaries.

## Network exposure

The web UI binds to **`127.0.0.1` by default**. The HTTP API has **no
authentication** and allows writing to a connected mouse. If you pass
`--host 0.0.0.0` (or any non-loopback address), anyone who can reach that port
can change device settings. Keep it on loopback. If you need remote access,
put it behind an authenticated reverse proxy and a firewall.

## Supported versions

| Version | Supported |
|---|---|
| 0.2.x | yes |
| < 0.2 | no (pre-release, unreleased) |

## Reporting a vulnerability

Open a GitHub issue at
<https://github.com/Reinazhard/FantechXD3v3Linux/issues> with the label
`security`, or use GitHub's private vulnerability reporting if enabled.
Please include reproduction steps and the affected version. Do not include
private USB captures or personal data.

## Device-safety notes

* The tool writes configuration flash records via HID feature reports. An
  interrupted write can leave a partially updated profile. Apply reports
  success only after every record is acknowledged; a failed Apply is reported
  loudly.
* No firmware-update, bootloader, or flash-erase commands are ever sent.
* `--real --dry-run` builds reports without performing any ioctl — use it to
  inspect behaviour safely.
* The vendor startup password words in `hardware.py` are opaque constants
  captured from USB traffic, not secrets. They grant no elevated host access.
