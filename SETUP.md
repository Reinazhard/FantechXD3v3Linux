# Setup

## Requirements

* Linux with `hidraw` support (standard on any modern distribution).
* Python ≥ 3.10.
* The Fantech Helios II XD3v3 connected via **2.4 GHz USB dongle** (1K
  `3554:f535`, 4K `3554:f537`) or **wired USB-C** (`3554:f536`). Bluetooth
  exposes no vendor HID config interface and is not supported for
  configuration.

No kernel module and no third-party Python packages are needed at runtime.

## Install

```bash
git clone https://github.com/Reinazhard/FantechXD3v3Linux
cd FantechXD3v3Linux
python -m venv .venv && . .venv/bin/activate
python -m pip install .
```

For development (editable + tests):

```bash
python -m pip install -e . pytest
PYTHONPATH=src python -m pytest tests/ -q
```

## udev permissions (non-root access)

The config interface is a `hidraw` node. By default only root can open it.

```bash
sudo cp udev/50-helios-ii.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger
sudo usermod -aG plugdev "$USER"
```

Log out and back in for the group change to take effect. Verify:

```bash
lsusb | grep -i 3554
ls -l /dev/hidraw*          # config node should be group plugdev, mode 0660
helios-editor probe         # read-only status + battery + flash values
```

`helios-editor probe` expects an **awake** mouse. If it reports the mouse link
as down, move the mouse to wake it; battery telemetry is unavailable while the
mouse sleeps.

## Run

```bash
helios-editor serve                    # simulated device, no hardware needed
helios-editor serve --real             # real device, writes to flash
helios-editor serve --real --dry-run   # build reports, no ioctl
```

Open <http://127.0.0.1:8765>. The UI binds to loopback only; see
[SECURITY.md](SECURITY.md) before changing `--host`.
Running `helios-editor` with no arguments starts the server in real-device
mode (equivalent to `serve --real`).

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `no XD3v3 config hidraw node found` | Device not plugged in, or wrong PID. Check `lsusb`. |
| `cannot open /dev/hidrawN: Permission denied` | udev rule not installed, or you did not re-login for `plugdev`. |
| `device did not echo ... mouse link may be down` | Mouse asleep / dongle not linked. Move the mouse and retry Apply. |
| Battery shows "mouse asleep?" | cmd-4 needs the mouse awake; the UI says so instead of faking a number. |
| Bluetooth mode | Not detectable / not configurable here — use wired or the 2.4 GHz dongle. |
| Apply fails but settings look changed in UI | Expected: failed writes keep the "unsaved changes" badge. The device was NOT written. |

## Uninstall

```bash
python -m pip uninstall helios-xd3v3-button-editor
sudo rm /etc/udev/rules.d/50-helios-ii.rules
sudo udevadm control --reload-rules
```
