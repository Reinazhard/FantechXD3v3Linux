"""Real hardware transport for the Helios II XD3v3.

Uses the Linux hidraw ioctl interface (HIDIOCSFEATURE / HIDIOCGFEATURE) to
send the confirmed 17-byte feature reports from :mod:`protocol`. No kernel
module, no libusb root permission: the device's config interface is exposed as
``/dev/hidrawN`` owned by ``plugdev`` (see ``udev/50-helios-ii.rules``).

The device is ``3554:f535`` (Compx Fantech Helios II); its interface 1 has an
interrupt-IN endpoint 0x82 with 17-byte max packet, matching the USBPcap
captures exactly.

Read-only operations (status polls, flash reads) are safe. Writes use only the
confirmed ``WriteFlashData`` records; no firmware/bootloader commands are sent.
"""

from __future__ import annotations

import array
import fcntl
import glob
import os
import select
import threading
import time
from dataclasses import dataclass

from . import protocol as P
from .model import ButtonProfile
from .transport import Transport, TransportError

VID = 0x3554
PID = 0xF535
CONFIG_INTERFACE_NUMBER = 1

# Startup unlock. These are the two 4-byte "password" words the vendor app
# sends as EncryptionData (cmd 1); captured verbatim from startup-handshake and
# every *-read-response capture. The device replies with its own 4-byte words;
# after both exchanges plus PCDriverStatus(1) and a poll, flash reads work.
# The words come from CS_SetPassward1/2 in HIDUsb.dll (AES.dll is not needed
# for this model's dongle). Treated as opaque constants, not invented.
UNLOCK_PASSWORDS = (b"\x28\x43\x48\x69", b"\x35\x35\x69\x66")

_IOC_TYPESHIFT, _IOC_SIZESHIFT, _IOC_DIRSHIFT = 8, 16, 30
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return ((direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT)
            | nr | (size << _IOC_SIZESHIFT))


HIDIOCSFEATURE = _ioc(_IOC_WRITE | _IOC_READ, 'H', 0x06, P.REPORT_LEN)
HIDIOCGFEATURE = _ioc(_IOC_WRITE | _IOC_READ, 'H', 0x07, P.REPORT_LEN)


def find_config_hidraw() -> str | None:
    """Locate the hidraw node for the device's config interface (interface 1).

    Matches by VID/PID and interface number read from sysfs, so it works even
    when hidraw numbering changes.
    """
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
        except OSError:
            continue
        if not all(s in uevent for s in (f"{VID:04X}", f"{PID:04X}")):
            continue
        # HID_UNIQ / HID_PHYS / modalias; interface lives in the parent path
        real = os.path.realpath(os.path.join(path, "device"))
        # e.g. .../1-3:1.1/0003:3554:F535.0003 -> interface 1
        for part in real.split(os.sep):
            if ":" in part and part.endswith(tuple(f".{i}" for i in range(4))):
                pass
        if f":1.{CONFIG_INTERFACE_NUMBER}" in real or f":1.{CONFIG_INTERFACE_NUMBER}/" in real:
            return "/dev/" + os.path.basename(path)
    return None


@dataclass
class DeviceStatus:
    online: bool
    raw: bytes


class HIDTransport(Transport):
    """Send confirmed feature reports to a real XD3v3 device over hidraw.

    Parameters
    ----------
    device_path:
        Explicit hidraw node (e.g. ``/dev/hidraw2``). When None, autodetect.
    dry_run:
        When True, build and return report bytes but never perform ioctl.
        Used by fixture tests and by cautious users.
    """

    def __init__(self, device_path: str | None = None, dry_run: bool = False) -> None:
        self.device_path = device_path
        self.dry_run = dry_run
        self._fd: int | None = None
        self.last_reports: list[bytes] = []
        self._unlocked = False
        self._pending: list[bytes] = []   # device reports not yet consumed
        self._device_state: object | None = None  # last known/applied profile
        self._io_lock = threading.RLock()   # serialize all device I/O

    # --- low level ---

    def _open(self) -> int:
        if self._fd is not None:
            return self._fd
        path = self.device_path or find_config_hidraw()
        if not path:
            raise TransportError(
                "no XD3v3 config hidraw node found; is the device connected "
                "and is the udev rule installed?"
            )
        try:
            self._fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
        except OSError as exc:
            raise TransportError(f"cannot open {path}: {exc.strerror}") from exc
        self.device_path = path
        return self._fd

    def _send(self, frame: bytes) -> bytes | None:
        """Send one report; return the device's 17-byte reply when available.

        All incoming reports are pumped into ``self._pending``; the echo for
        ``frame`` is returned when present. Reading MORE than one report per
        call matters: the device queues write echoes behind poll echoes.
        """
        if len(frame) != P.REPORT_LEN:
            raise ValueError(f"report must be {P.REPORT_LEN} bytes")
        self.last_reports.append(frame)
        if self.dry_run:
            return None
        fd = self._open()
        try:
            fcntl.ioctl(fd, HIDIOCSFEATURE, array.array('B', frame), True)
        except OSError as exc:
            raise TransportError(f"HIDIOCSFEATURE failed: {exc.strerror}") from exc
        self._pump(fd, timeout=0.08)
        return self._take(frame[1], (frame[3] << 8) | frame[4])

    @staticmethod
    def _read_all(fd: int, timeout: float) -> list[bytes]:
        """Read EVERY pending 17-byte interrupt-IN report within ``timeout``."""
        out: list[bytes] = []
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([fd], [], [], min(0.05, max(0, end - time.time())))
            if not r:
                continue
            try:
                d = os.read(fd, P.REPORT_LEN)
            except OSError:
                break
            if len(d) >= P.REPORT_LEN:
                out.append(bytes(d[:P.REPORT_LEN]))
        return out

    def _pump(self, fd: int, timeout: float = 0.2) -> None:
        for r in self._read_all(fd, timeout):
            self._pending.append(r)

    def _take(self, command: int, address: int | None = None) -> bytes | None:
        """Pop and return the first pending report matching ``command``."""
        for i, r in enumerate(self._pending):
            if r[1] == command and (address is None or
                                    ((r[3] << 8) | r[4]) == address):
                return self._pending.pop(i)
        return None

    def clock(self) -> bytes | None:
        """Send a cmd-3 DeviceOnLine poll; pump all replies into the queue.

        The vendor's native ``CS_UsbServer_Thread`` continuously polls the
        device; our synchronous transport must do the same between config
        commands or the firmware drops them.
        """
        if self.dry_run:
            self.last_reports.append(P.poll(P.CMD_DEVICE_ONLINE))
            return None
        fd = self._open()
        try:
            fcntl.ioctl(fd, HIDIOCSFEATURE,
                        array.array('B', P.poll(P.CMD_DEVICE_ONLINE)), True)
        except OSError as exc:
            raise TransportError(f"poll failed: {exc.strerror}") from exc
        self._pump(fd, timeout=0.10)
        return self._take(P.CMD_DEVICE_ONLINE)

    def close(self) -> None:
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    def __enter__(self) -> "HIDTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- confirmed primitives ---

    def poll_status(self) -> DeviceStatus:
        """Send cmd 3 (DeviceOnLine) and decode the reply."""
        reply = self.clock()
        if reply is None:
            raise TransportError("no response to DeviceOnLine poll")
        parsed = P.parse_report(reply)
        online = bool(parsed["command"] == P.CMD_DEVICE_ONLINE and len(reply) > 6)
        return DeviceStatus(online=online, raw=reply)

    def device_status(self) -> dict:
        """Return live connection + battery info for the UI.

        Honest boundaries: USB config path only exists for the wired mouse
        (0xF536) and the 2.4 GHz dongle (0xF535/0xF537). Bluetooth is handled
        by the host BT stack and exposes NO vendor HID config interface, so it
        cannot be detected here and is reported as such.
        """
        with self._io_lock:
            return self._device_status_locked()

    def _device_status_locked(self) -> dict:
        info = {
            "vid": VID, "pid": None, "connection": None,
            "bluetooth": False, "online": False,
            "battery_percent": None, "battery_mv": None,
            "charging": None, "hidraw": self.device_path,
            "note": None,
        }
        if self.dry_run:
            info["connection"] = "dry run (no device access)"
            return info
        # Infer the active PID from the hidraw node's uevent.
        path = self.device_path or find_config_hidraw()
        if path:
            try:
                uev = open(os.path.join("/sys/class/hidraw",
                                        os.path.basename(path), "device", "uevent")).read()
                for tok in uev.split():
                    if tok.startswith("HID_ID="):
                        pid = int(tok.split(":")[-1], 16)
                        info["pid"] = pid
                        info["connection"] = P.describe_pid(pid)
            except OSError:
                pass
        if info["pid"] is None:
            info["pid"] = PID
            info["connection"] = P.describe_pid(PID)
        # Bluetooth has no vendor USB config interface: it can never be
        # detected from this process. Say so instead of guessing.
        info["note"] = ("Bluetooth mode is not detectable here: it uses the "
                        "host BT stack and exposes no vendor HID config interface.")
        try:
            s = self.poll_status()
            info["online"] = bool(s.raw[6]) if len(s.raw) > 6 else False
            self.unlock(verify=False)
            batt_echo = self._send(P.poll(P.CMD_BATTERY_LEVEL))
            # _send already pumped the queue; drive a few polls in case the
            # battery reply is late, then look through everything collected.
            for _ in range(3):
                if batt_echo is not None or self._take(P.CMD_BATTERY_LEVEL) is not None:
                    break
                self.clock()
            all_replies = ([batt_echo] if batt_echo else []) + list(self._pending)
            for r in all_replies:
                if r[1] == P.CMD_BATTERY_LEVEL:
                    dec = P.decode_battery(r)
                    info["battery_percent"] = dec["percent"]
                    info["battery_mv"] = dec["voltage_mv"]
                    break
        except TransportError:
            pass
        if not info["online"]:
            info["note"] = (info["note"] + " Mouse link is down (asleep/off) — "
                            "move the mouse to refresh battery.")
        return info

    def unlock(self, verify: bool = True) -> None:
        """Perform the captured startup handshake so flash reads work.

        Sequence (startup-handshake.pcapng events 0-5, 24-27):
            EncryptionData(pass1) -> PCDriverStatus(1) -> DeviceOnLine
            EncryptionData(pass2) -> PCDriverStatus(1) -> DeviceOnLine
        Writes do not need this; reads (cmd 8) are ignored without it.
        """
        if self._unlocked:
            return
        for word in UNLOCK_PASSWORDS:
            self._send(P.raw_frame(P.CMD_ENCRYPTION_DATA, word, length=8))
            self._send(P.raw_frame(P.CMD_PC_DRIVER_STATUS, b"\x01"))
            self.clock()
        self._unlocked = True
        if verify:
            if self.dry_run:
                return
            # A read request must now be echoed; otherwise the unlock failed.
            self._send(P.read_request(P.ADDR_REPORT_RATE, 10))
            self.clock()
            echoes = self._drain_all(0.2)
            if not any(r[1] == P.CMD_READ_FLASH_DATA for r in echoes):
                raise TransportError("startup unlock did not enable flash reads")

    def read_flash(self, address: int, length: int = 10) -> bytes:
        """Request a flash window (cmd 8) and return the reply record."""
        reply = self._send(P.read_request(address, length))
        if self.dry_run:
            return bytes(length)
        if reply is None or reply[1] != P.CMD_READ_FLASH_DATA:
            reply = self._await_echo(P.CMD_READ_FLASH_DATA, address)
        return reply[6:6 + length]

    def read_active_dpi(self) -> int | None:
        """Read ONLY the active DPI stage index (flash 0x04).

        Cheap single-record read for the background sync loop: pressing the
        bottom DPI-cycle button changes this on the device, and the web UI
        must follow it without a full (multi-second) flash sweep. Returns None
        when the device is unreachable/asleep. Requires the startup unlock.
        """
        if self.dry_run:
            return None
        with self._io_lock:
            return self._read_active_dpi_locked()

    def _read_active_dpi_locked(self) -> int | None:
        try:
            self.unlock(verify=False)
            # Drop any stale cmd-8 replies left in the echo queue from an
            # earlier full read; otherwise a background poll can return an
            # old active-DPI value and miss a hardware button cycle.
            self._pending = [r for r in self._pending
                             if r[1] != P.CMD_READ_FLASH_DATA]
            rec = self.read_flash(P.ADDR_ACTIVE_DPI_INDEX, 2)
            idx = rec[0]
            if 0 <= idx < 4:   # MAX_DPI_STAGES
                return idx
        except (TransportError, IndexError):
            pass
        return None

    def write_flash(self, address: int, values: bytes) -> None:
        """Write one confirmed flash record (cmd 7), then verify its echo."""
        frame = P.write_record(address, values)
        echo = self._send(frame)
        if self.dry_run:
            return
        if echo is None or (echo[1] != P.CMD_WRITE_FLASH_DATA):
            echo = self._await_echo(P.CMD_WRITE_FLASH_DATA, address)
        if echo != frame:
            raise TransportError(
                f"WriteFlashData(0x{address:04x}) not acknowledged by device; "
                "is the mouse awake and connected to the dongle?"
            )

    def _await_echo(self, command: int, address: int, attempts: int = 6) -> bytes:
        """Drive the poll clock until the device echoes ``command``."""
        for _ in range(attempts):
            found = self._take(command, address)
            if found is not None:
                return found
            self.clock()   # pumps all queued reports into self._pending
            found = self._take(command, address)
            if found is not None:
                return found
        raise TransportError(
            f"device did not echo command 0x{command:02x} (addr 0x{address:04x}) "
            f"after {attempts} polls - mouse link may be down"
        )

    def _drain_all(self, timeout: float = 0.3) -> list[bytes]:
        """Pump and return all currently pending reports (queue is drained)."""
        if not self.dry_run:
            self._pump(self._open(), timeout)
        out = list(self._pending)
        self._pending.clear()
        return out

    # --- Transport interface (whole-profile apply) ---

    def read_profile(self) -> ButtonProfile:
        """Read the confirmable flash fields into a ButtonProfile.

        Flash layout was reconstructed from the cmd-8 sweep (see
        docs/HARDWARE.md). Reads require the two-password startup unlock.
        """
        with self._io_lock:
            return self._read_profile_locked()

    def _read_profile_locked(self) -> ButtonProfile:
        profile = ButtonProfile()
        s = profile.settings
        if self.dry_run:
            self._device_state = profile.to_dict()
            return profile
        try:
            self.unlock()
        except TransportError:
            # Unlock failed (mouse asleep / link down). Do NOT cache defaults
            # as the device state: a later Apply would full-write against a
            # wrong baseline. Leave _device_state as-is.
            return profile
        from .model import HOTSPOTS, MAX_DPI_STAGES, Binding
        try:
            rr = self.read_flash(P.ADDR_REPORT_RATE, 2)[0]
            if rr in P.REPORT_RATE_TO_WIRE:
                s.report_rate = rr
        except (TransportError, IndexError):
            pass
        try:
            s.lod_mm = self.read_flash(P.ADDR_LOD, 2)[0] or 1
        except (TransportError, IndexError):
            pass
        try:
            s.debounce_ms = self.read_flash(P.ADDR_DEBOUNCE, 2)[0]
        except (TransportError, IndexError):
            pass
        try:
            s.motion_sync = bool(self.read_flash(P.ADDR_MOTION_SYNC, 2)[0])
            s.angle_snapping = bool(self.read_flash(P.ADDR_ANGLE_SNAPPING, 2)[0])
            s.ripple_control = bool(self.read_flash(P.ADDR_RIPPLE_CONTROL, 2)[0])
        except (TransportError, IndexError):
            pass
        try:
            count = self.read_flash(P.ADDR_DPI_STAGE_COUNT, 2)[0]
            idx = self.read_flash(P.ADDR_ACTIVE_DPI_INDEX, 2)[0]
            if 0 <= idx < MAX_DPI_STAGES:
                s.active_dpi_index = idx
            n = min(count or MAX_DPI_STAGES, MAX_DPI_STAGES)
            for i in range(n):
                rec = self.read_flash(P.dpi_stage_address(i), 4)
                s.dpi_stages[i] = (rec[0] + 1) * 50 if rec[0] < 0x3F else s.dpi_stages[i]
                col = self.read_flash(P.dpi_color_address(i), 4)
                s.dpi_colors[i] = (col[0], col[1], col[2])
        except (TransportError, IndexError):
            pass
        for h in HOTSPOTS:
            try:
                rec = self.read_flash(P.keymap_address(h.id), 4)
                if (sum(rec) & 0xFF) == 0x55:
                    profile.bindings[h.id] = Binding(rec[0], rec[1], rec[2],
                                                           rec[1] | (rec[2] << 8))
            except (TransportError, IndexError):
                pass
        try:
            s.sensor_mode = self.read_flash(P.ADDR_SENSOR_MODE, 2)[0] & 1
        except (TransportError, IndexError):
            pass
        self._device_state = profile.to_dict()
        return profile
    def write_profile(self, profile: ButtonProfile) -> None:
        """Write ONLY the records that changed since the last known device state.

        Sending all 23 records on every Apply caused extreme mouse lag (each
        record costs an ACK round-trip plus a poll). With a cached baseline a
        single DPI edit writes one record. On the first Apply with no baseline
        (e.g. a cold process) we fall back to a full write, which is still
        bounded and only happens once per session.
        """
        with self._io_lock:
            return self._write_profile_locked(profile)

    def _write_profile_locked(self, profile: ButtonProfile) -> None:
        errors = profile.validate()
        if errors:
            raise TransportError("invalid profile: " + "; ".join(errors))
        # A real device needs a known baseline for delta writes. Without one
        # we would full-write 23 records (the lag bug). Try one read; if the
        # device is unreachable, fail fast with an actionable message.
        if self._device_state is None and not self.dry_run:
            self.read_profile()
        if self._device_state is None and not self.dry_run:
            raise TransportError(
                "cannot read device state (mouse asleep or link down); wake the "
                "mouse and retry Apply. No writes were sent.")
        baseline = (ButtonProfile.from_dict(self._device_state)
                    if self._device_state else None)
        records = (P.changed_records(baseline, profile) if baseline
                   else P.profile_to_records(profile))
        self.last_written_records = [(a, v, l) for a, v, l in records]
        if not records:
            return  # nothing to do; no device churn at all
        # The poll clock plus the startup unlock keeps the dongle servicing
        # config commands; without it writes are silently dropped.
        self.unlock(verify=False)
        for addr, vals, _label in records:
            self.write_flash(addr, vals)
        # PCDriverStatus after writes, as observed in dpi-0400 / click-debounce
        self._send(P.raw_frame(P.CMD_PC_DRIVER_STATUS, b"\x01"))
        self._device_state = profile.to_dict()

    def describe(self) -> str:
        loc = self.device_path or "auto-detect"
        return f"HIDTransport(device={loc}{', dry_run' if self.dry_run else ''})"
