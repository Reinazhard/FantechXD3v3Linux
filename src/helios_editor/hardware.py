"""Real hardware transport for the Helios II XD3v3.

Uses the Linux hidraw ioctl interface (HIDIOCSFEATURE) to send confirmed
17-byte feature reports from :mod:`protocol`. No kernel module, no libusb, no
root permission: the device's config interface is exposed as ``/dev/hidrawN``
owned by ``plugdev`` (see ``udev/50-helios-ii.rules``). The device is
``3554:f535`` (Compx Fantech Helios II); interface 1 has a 17-byte interrupt-IN
endpoint 0x82, matching the USBPcap captures exactly. Read-only operations
(status polls, flash reads) are safe. Writes use only confirmed
``WriteFlashData`` records; no firmware/bootloader commands are sent.

All device I/O is serialized under ``self._io_lock`` (an RLock so high-level
wrappers can call low-level primitives). The lock order in the whole app is::

    EditorState._sync_lock -> EditorState._state_lock -> HIDTransport._io_lock

Never acquire these in the reverse order.
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
from .model import ButtonProfile, MAX_DPI_STAGES
from .catalog import REPORT_RATES
from .transport import Transport, TransportError

VID = 0x3554
PID = 0xF535
CONFIG_INTERFACE_NUMBER = 1
SUPPORTED_PIDS = (0xF535, 0xF536, 0xF537)   # dongle, wired USB-C, 4K dongle

# Startup unlock. These are the two 4-byte "password" words the vendor app
# sends as EncryptionData (cmd 1); captured verbatim from startup-handshake and
# every *-read-response capture. The device replies with its own 4-byte words;
# after both exchanges plus PCDriverStatus(1) and a poll, flash reads work.
# The words come from CS_SetPassward1/2 in HIDUsb.dll (AES.dll is not needed
# for this model's dongle). Treated as opaque constants, not invented.
UNLOCK_PASSWORDS = (b"\x28\x43\x48\x69", b"\x35\x35\x69\x66")

# Bounded I/O timeouts (seconds). Every transaction has a finite worst case;
# a missing/late echo can never hang the request thread forever.
PUMP_AFTER_SEND = 0.08
PUMP_AFTER_POLL = 0.10
PUMP_DRAIN = 0.2
ECHO_ATTEMPTS = 6
READBACK_TIMEOUT = 0.2

_IOC_TYPESHIFT, _IOC_SIZESHIFT, _IOC_DIRSHIFT = 8, 16, 30
_IOC_WRITE, _IOC_READ = 1, 2


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return ((direction << _IOC_DIRSHIFT) | (ord(typ) << _IOC_TYPESHIFT)
            | nr | (size << _IOC_SIZESHIFT))


HIDIOCSFEATURE = _ioc(_IOC_WRITE | _IOC_READ, 'H', 0x06, P.REPORT_LEN)


class HIDIO:
    """The real hidraw syscall boundary.

    Injected into :class:`HIDTransport` so tests can drive ACK/timeout/reconnect
    with a scripted backend without a physical device. Production code leaves
    ``io=None`` and the transport constructs this default. This is a genuine
    I/O boundary, not debug scaffolding.
    """

    def open(self, path: str) -> int:
        return os.open(path, os.O_RDWR | os.O_NONBLOCK)

    def feature(self, fd: int, frame: bytes) -> None:
        fcntl.ioctl(fd, HIDIOCSFEATURE, array.array('B', frame), True)

    def read(self, fd: int, timeout: float) -> list[bytes]:
        """Read EVERY pending 17-byte interrupt-IN report within ``timeout``."""
        out: list[bytes] = []
        end = time.time() + timeout
        while time.time() < end:
            r, _, _ = select.select([fd], [], [],
                                    min(0.05, max(0, end - time.time())))
            if not r:
                continue
            try:
                d = os.read(fd, P.REPORT_LEN)
            except OSError:
                break
            if len(d) >= P.REPORT_LEN:
                out.append(bytes(d[:P.REPORT_LEN]))
        return out

    def close(self, fd: int) -> None:
        os.close(fd)


def find_config_hidraw() -> str | None:
    """Locate the hidraw node for the device's config interface (interface 1).

    Accepts every Fantech Helios II XD3v3 PID: 2.4 GHz dongle (0xF535 / 4K
    0xF537) and wired USB-C (0xF536). Bluetooth exposes no vendor HID config
    interface and is intentionally NOT matched. Matches by VID/PID and
    interface number read from sysfs, so it works despite hidraw renumbering.
    """
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        try:
            uevent = open(os.path.join(path, "device", "uevent")).read()
        except OSError:
            continue
        vid = pid = None
        for tok in uevent.split():
            if tok.startswith("HID_ID="):
                parts = tok.split(":")
                if len(parts) == 3:
                    try:
                        bus, vid, pid = (int(parts[0][7:], 16), int(parts[1], 16),
                                         int(parts[2], 16))
                    except ValueError:
                        continue
        if vid is None:  # uevent lacked HID_ID=; fall back to substring form
            upper = uevent.upper()
            if not (f"{VID:04X}" in upper and
                    any(f"{p:04X}" in upper for p in SUPPORTED_PIDS)):
                continue
            real = os.path.realpath(os.path.join(path, "device"))
            if f":1.{CONFIG_INTERFACE_NUMBER}" in real:
                return "/dev/" + os.path.basename(path)
            continue
        if vid != VID or pid not in SUPPORTED_PIDS:
            continue
        # e.g. .../1-3:1.1/0003:3554:F535.0003 -> interface 1
        real = os.path.realpath(os.path.join(path, "device"))
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
    io:
        Optional :class:`HIDIO` backend for tests. Ignored (all calls skipped)
        when ``dry_run`` is True.
    """

    verification_mode = "readback"

    def __init__(self, device_path: str | None = None, dry_run: bool = False,
                 io: HIDIO | None = None) -> None:
        self.device_path = device_path
        self.dry_run = dry_run
        self._io = io
        self._fd: int | None = None
        self.last_reports: list[bytes] = []
        self.last_written_records: list = []
        self._unlocked = False
        self._pending: list[bytes] = []   # device reports not yet consumed
        self._device_state: object | None = None  # last known/applied profile
        self._io_lock = threading.RLock()   # serialize ALL device I/O
        self._conn_generation = 0
        if dry_run:
            self.verification_mode = "framing-only"

    # --- low level ---

    def _open(self) -> int:
        # A cached fd may point at a device that was unplugged. Re-detect on
        # the real backend when the cached path no longer exists.
        if self._fd is not None:
            if self._io is None and self.device_path and \
                    not os.path.exists(self.device_path):
                self._reset_connection()
            else:
                return self._fd
        path = self.device_path or ("/dev/fake" if self._io is not None
                                    else find_config_hidraw())
        if not path:
            raise TransportError(
                "no XD3v3 config hidraw node found; is the device connected "
                "and is the udev rule installed?"
            )
        io = self._io if self._io is not None else HIDIO()
        try:
            self._fd = io.open(path)
        except OSError as exc:
            raise TransportError(f"cannot open {path}: {exc.strerror}") from exc
        self.device_path = path
        return self._fd

    def _io_backend(self) -> HIDIO:
        return self._io if self._io is not None else HIDIO()

    def _pump(self, fd: int, timeout: float = PUMP_DRAIN) -> None:
        for r in self._io_backend().read(fd, timeout):
            self._pending.append(r)

    def _take(self, command: int, address: int | None = None) -> bytes | None:
        """Pop and return the first pending report matching ``command``.

        Multiple echoes per poll are capture-confirmed: the device queues write
        echoes behind cmd-3 poll echoes, so we scan the whole queue, not just
        the head. See test_pending_queue_handles_multiple_echoes.
        """
        for i, r in enumerate(self._pending):
            if r[1] == command and (address is None or
                                    ((r[3] << 8) | r[4]) == address):
                return self._pending.pop(i)
        return None

    def _purge_stale(self, command: int, address: int) -> None:
        """Drop queued echoes for (command, address) left by a prior op.

        The confirmed wire protocol carries no sequence id, so a stale echo
        with the same tuple could otherwise satisfy a later transaction. Purge
        BEFORE send; this changes no wire bytes.
        """
        self._pending = [r for r in self._pending
                         if not (r[1] == command and
                                 ((r[3] << 8) | r[4]) == address)]

    def _transact(self, frame: bytes, expect_command: int, expect_address: int,
                  *, verify_frame: bool = False, attempts: int = ECHO_ATTEMPTS,
                  retry_on_disconnect: bool = True) -> bytes:
        """Send one report and return its validated echo, serialized.

        Drives bounded poll clocks when the echo is late. Raises TransportError
        on a missing echo, malformed reply, or device I/O failure. Read/poll
        requests may be retried once after a reconnect (idempotent); flash
        writes pass ``retry_on_disconnect=False`` to avoid double-writes.
        """
        if self.dry_run:
            self.last_reports.append(frame)
            return b""
        if len(frame) != P.REPORT_LEN:
            raise ValueError(f"report must be {P.REPORT_LEN} bytes")
        with self._io_lock:
            return self._transact_locked(frame, expect_command, expect_address,
                                         verify_frame=verify_frame,
                                         attempts=attempts,
                                         retry_on_disconnect=retry_on_disconnect,
                                         _retried=False)

    def _transact_locked(self, frame: bytes, expect_command: int,
                         expect_address: int, *, verify_frame: bool,
                         attempts: int, retry_on_disconnect: bool,
                         _retried: bool) -> bytes:
        self._purge_stale(expect_command, expect_address)
        self.last_reports.append(frame)
        fd = self._open()
        io = self._io_backend()
        try:
            io.feature(fd, frame)
        except OSError as exc:
            self._reset_connection()
            if retry_on_disconnect and not _retried:
                return self._transact_locked(
                    frame, expect_command, expect_address,
                    verify_frame=verify_frame, attempts=attempts,
                    retry_on_disconnect=retry_on_disconnect, _retried=True)
            raise TransportError(
                f"device I/O failed (disconnected?): {exc.strerror}") from exc

        def _pump_or_fail(timeout: float) -> None:
            try:
                self._pending.extend(io.read(fd, timeout))
            except OSError as exc:
                self._reset_connection()
                raise TransportError(
                    f"device I/O failed (disconnected?): {exc.strerror}") from exc

        _pump_or_fail(PUMP_AFTER_SEND)
        echo = self._take(expect_command, expect_address)
        for _ in range(attempts):
            if echo is not None:
                break
            # Interleave a DeviceOnLine poll so the dongle keeps servicing the
            # config command; captures require this clock between commands.
            poll_frame = P.poll(P.CMD_DEVICE_ONLINE)
            self.last_reports.append(poll_frame)
            try:
                io.feature(fd, poll_frame)
            except OSError as exc:
                self._reset_connection()
                if retry_on_disconnect and not _retried:
                    return self._transact_locked(
                        frame, expect_command, expect_address,
                        verify_frame=verify_frame, attempts=attempts,
                        retry_on_disconnect=retry_on_disconnect, _retried=True)
                raise TransportError(
                    f"device I/O failed (disconnected?): {exc.strerror}") from exc
            _pump_or_fail(PUMP_AFTER_POLL)
            echo = self._take(expect_command, expect_address)

        if echo is None:
            # A read/poll may be re-sent once after reconnect; a write must not.
            if retry_on_disconnect and not _retried:
                return self._transact_locked(
                    frame, expect_command, expect_address,
                    verify_frame=verify_frame, attempts=attempts,
                    retry_on_disconnect=retry_on_disconnect, _retried=True)
            raise TransportError(
                f"device did not echo command 0x{expect_command:02x} "
                f"(addr 0x{expect_address:04x}) after {attempts} polls - "
                "mouse link may be down")

        parsed = P.parse_report(echo)
        if not (parsed["outer_valid"] and parsed["command"] == expect_command
                and parsed["address"] == expect_address):
            raise TransportError(
                f"device reply invalid for command 0x{expect_command:02x} "
                f"(addr 0x{expect_address:04x})")
        if verify_frame and echo != frame:
            raise TransportError(
                f"device did not echo command 0x{expect_command:02x} verbatim "
                f"(addr 0x{expect_address:04x})")
        return echo

    def _send(self, frame: bytes) -> bytes | None:
        """Send one report; return its echo when available, else None.

        ``_send`` never drives a bounded poll campaign (single attempt), so
        callers such as unlock/device_status keep their original "best effort"
        contract. Framing-only dry_run returns None.
        """
        if self.dry_run:
            if len(frame) != P.REPORT_LEN:
                raise ValueError(f"report must be {P.REPORT_LEN} bytes")
            self.last_reports.append(frame)
            return None
        try:
            return self._transact(frame, frame[1],
                                  (frame[3] << 8) | frame[4], attempts=1,
                                  retry_on_disconnect=False)
        except TransportError:
            return None

    def clock(self) -> bytes | None:
        """Send a cmd-3 DeviceOnLine poll; pump all replies into the queue.

        The vendor's native ``CS_UsbServer_Thread`` continuously polls the
        device; our synchronous transport must do the same between config
        commands or the firmware drops them.
        """
        with self._io_lock:
            frame = P.poll(P.CMD_DEVICE_ONLINE)
            if self.dry_run:
                self.last_reports.append(frame)
                return None
            fd = self._open()
            io = self._io_backend()
            try:
                io.feature(fd, frame)
            except OSError as exc:
                self._reset_connection()
                raise TransportError(
                    f"device I/O failed (disconnected?): {exc.strerror}") from exc
            self.last_reports.append(frame)
            try:
                self._pending.extend(io.read(fd, PUMP_AFTER_POLL))
            except OSError as exc:
                self._reset_connection()
                raise TransportError(
                    f"device I/O failed (disconnected?): {exc.strerror}") from exc
            return self._take(P.CMD_DEVICE_ONLINE)

    def _reset_connection(self) -> None:
        """Drop all cached connection state after a disconnect/replug."""
        if self._fd is not None:
            try:
                self._io_backend().close(self._fd)
            except OSError:
                pass
        self._fd = None
        self._unlocked = False
        self._pending.clear()
        self.device_path = None
        self._conn_generation += 1
        self._device_state = None

    def close(self) -> None:
        with self._io_lock:
            self._reset_connection()

    def __enter__(self) -> "HIDTransport":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    # --- confirmed primitives ---

    def poll_status(self) -> DeviceStatus:
        """Send cmd 3 (DeviceOnLine) and decode the reply."""
        with self._io_lock:
            reply = self.clock()
            if reply is None:
                raise TransportError("no response to DeviceOnLine poll")
            online = bool(reply[6]) if len(reply) > 6 else False
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
        # Infer the active PID from the hidraw node's uevent (single source).
        pid = self._detected_pid()
        info["pid"] = pid if pid is not None else PID
        info["connection"] = P.describe_pid(info["pid"])
        info["note"] = ("Bluetooth mode is not detectable here: it uses the "
                        "host BT stack and exposes no vendor HID config interface.")
        try:
            s = self.poll_status()
            info["online"] = s.online
            self.unlock(verify=False)
            batt_echo = self._send(P.poll(P.CMD_BATTERY_LEVEL))
            # _send already pumped the queue; drive a few polls in case the
            # battery reply is late, then look through everything collected.
            for _ in range(3):
                if batt_echo is not None or self._take(P.CMD_BATTERY_LEVEL) is not None:
                    break
                self.clock()
            candidates = ([batt_echo] if batt_echo else []) + list(self._pending)
            # Consume the battery echo so it cannot leak into a later
            # transaction with the same (command, address) tuple.
            for r in candidates:
                if r and r[1] == P.CMD_BATTERY_LEVEL:
                    dec = P.decode_battery(r)
                    info["battery_percent"] = dec["percent"]
                    info["battery_mv"] = dec["voltage_mv"]
                    self._take(P.CMD_BATTERY_LEVEL)
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
        with self._io_lock:
            if self._unlocked:
                return
            for word in UNLOCK_PASSWORDS:
                self._send(P.raw_frame(P.CMD_ENCRYPTION_DATA, word, length=8))
                self._send(P.raw_frame(P.CMD_PC_DRIVER_STATUS, b"\x01"))
                self.clock()
            self._unlocked = True
            if verify and not self.dry_run:
                # A read request must now be echoed; otherwise the unlock failed.
                self._send(P.read_request(P.ADDR_REPORT_RATE, 10))
                self.clock()
                echoes = self._drain_all(PUMP_DRAIN)
                if not any(r[1] == P.CMD_READ_FLASH_DATA for r in echoes):
                    raise TransportError("startup unlock did not enable flash reads")

    def read_flash(self, address: int, length: int = 10) -> bytes:
        """Request a flash window (cmd 8) and return the reply record.

        The reply's command, address and checksums are validated by
        ``_transact`` before its data bytes are trusted.
        """
        with self._io_lock:
            frame = P.read_request(address, length)
            if self.dry_run:
                self.last_reports.append(frame)
                return bytes(length)
            echo = self._transact(frame, P.CMD_READ_FLASH_DATA, address)
            return echo[6:6 + length]

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
            # _transact purges stale cmd-8 echoes before each read, so a
            # background poll cannot return an old active-DPI value.
            rec = self.read_flash(P.ADDR_ACTIVE_DPI_INDEX, 2)
            idx = rec[0]
            if 0 <= idx < MAX_DPI_STAGES:
                return idx
        except (TransportError, IndexError):
            pass
        return None

    def write_flash(self, address: int, values: bytes) -> None:
        """Write one confirmed flash record (cmd 7), then verify its echo.

        Flash writes are NOT auto-retried after a disconnect: resending the
        same record risks a double-write. A failure propagates to /api/apply.
        """
        with self._io_lock:
            frame = P.write_record(address, values)
            if self.dry_run:
                self.last_reports.append(frame)
                return
            self._transact(frame, P.CMD_WRITE_FLASH_DATA, address,
                           verify_frame=True, retry_on_disconnect=False)

    def _drain_all(self, timeout: float = PUMP_DRAIN) -> list[bytes]:
        """Pump and return all currently pending reports (queue is drained)."""
        with self._io_lock:
            if not self.dry_run:
                self._pump(self._open(), timeout)
            out = list(self._pending)
            self._pending.clear()
            return out

    # --- Transport interface (whole-profile apply) ---

    def read_profile(self) -> ButtonProfile | None:
        """Read the confirmable flash fields into a ButtonProfile.

        Flash layout was reconstructed from the cmd-8 sweep (see
        docs/HARDWARE.md). Reads require the two-password startup unlock.
        Returns None when the device is unreachable (asleep / link down) so
        callers never mistake factory defaults for the device's real state.
        """
        with self._io_lock:
            return self._read_profile_locked()

    def _read_profile_locked(self) -> ButtonProfile | None:
        profile = ButtonProfile()
        s = profile.settings
        if self.dry_run:
            self._device_state = profile.to_dict()
            return profile
        try:
            self.unlock()
        except TransportError:
            # Unlock failed (mouse asleep / link down). Do NOT cache defaults
            # as the device state and do NOT return a fake profile.
            return None
        from .model import HOTSPOTS, Binding
        try:
            rr = self.read_flash(P.ADDR_REPORT_RATE, 2)[0]
            if rr in REPORT_RATES:
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

    def set_baseline(self, profile: ButtonProfile) -> None:
        """Align the delta-write baseline with server-confirmed state."""
        with self._io_lock:
            self._device_state = profile.to_dict()

    def write_profile(self, profile: ButtonProfile, *,
                      baseline: ButtonProfile | None = None) -> None:
        """Write ONLY the records that changed since the last known device state.

        Sending all 23 records on every Apply caused extreme mouse lag (each
        record costs an ACK round-trip plus a poll). With a cached baseline a
        single DPI edit writes one record. On the first Apply with no baseline
        (e.g. a cold process) we fall back to a full write, which is still
        bounded and only happens once per session.

        On the real (non-dry_run) path, every written record is read back and
        compared; a mismatch raises TransportError so Apply never reports a
        false success. A partial failure discards the stale delta baseline.
        """
        with self._io_lock:
            self._write_profile_locked(profile, baseline=baseline)

    def _write_profile_locked(self, profile: ButtonProfile,
                              baseline: ButtonProfile | None) -> None:
        errors = profile.validate()
        if errors:
            raise TransportError("invalid profile: " + "; ".join(errors))

        # A real device needs a known baseline for delta writes. Without one
        # we would full-write 23 records (the lag bug). Try one read; if the
        # device is unreachable, fail fast with an actionable message.
        if baseline is None and self._device_state is None and not self.dry_run:
            self.read_profile()
        if baseline is None and self._device_state is None and not self.dry_run:
            raise TransportError(
                "cannot read device state (mouse asleep or link down); wake the "
                "mouse and retry Apply. No writes were sent.")

        effective_baseline = baseline
        if effective_baseline is None and self._device_state:
            effective_baseline = ButtonProfile.from_dict(self._device_state)
        records = (P.changed_records(effective_baseline, profile)
                   if effective_baseline is not None
                   else P.profile_to_records(profile))
        self.last_written_records = [(a, v, l) for a, v, l in records]
        if not records:
            return  # nothing to do; no device churn at all

        # The poll clock plus the startup unlock keeps the dongle servicing
        # config commands; without it writes are silently dropped.
        try:
            self.unlock(verify=False)
            for addr, vals, _label in records:
                self.write_flash(addr, vals)
            # PCDriverStatus after writes, as observed in dpi-0400 /
            # click-debounce captures. This is the LAST frame (golden test).
            self._send(P.raw_frame(P.CMD_PC_DRIVER_STATUS, b"\x01"))
            if not self.dry_run:
                for addr, vals, _label in records:
                    got = self.read_flash(addr, len(vals) + 1)
                    if got[:len(vals)] != vals:
                        raise TransportError(
                            f"read-back mismatch at 0x{addr:04x}: wrote "
                            f"{vals.hex()}, read {got[:len(vals)].hex()}")
            self._device_state = profile.to_dict()
        except TransportError:
            # A partial write leaves unknown device state; force a fresh
            # baseline read on the next Apply. The server keeps the draft.
            self._device_state = None
            raise

    def describe(self) -> str:
        loc = self.device_path or "auto-detect"
        return f"HIDTransport(device={loc}{', dry_run' if self.dry_run else ''})"

    def _detected_pid(self) -> int | None:
        """Read the connected config interface's PID from sysfs uevent."""
        path = self.device_path or find_config_hidraw()
        if path:
            try:
                uev = open(os.path.join("/sys/class/hidraw",
                                        os.path.basename(path), "device", "uevent")).read()
                for tok in uev.split():
                    if tok.startswith("HID_ID="):
                        return int(tok.split(":")[-1], 16)
            except (OSError, ValueError):
                pass
        return None

    def capabilities(self) -> dict:
        """Narrow supported polling rates from the detected dongle PID.

        [INFERENCE, not capture-confirmed] The 4K dongle (0xF537) is the only
        receiver known to expose 2000/4000 Hz. The standard 2.4 GHz dongle
        (0xF535) and wired USB-C (0xF536) cap at 1000 Hz. Firmware can differ
        independently of the receiver, so a 4K dongle does not GUARANTEE 4K;
        we simply expose the rates the receiver class can carry. When no device
        is detected we fail open to the full catalog set rather than hiding
        controls we cannot prove unsupported.
        """
        base = super().capabilities()
        pid = self._detected_pid()
        if pid is None:
            return base
        if pid == 0xF537:   # 4K dongle
            return {"reportRates": base["reportRates"], "rateSource": "pid-4k"}
        if pid in SUPPORTED_PIDS:   # standard dongle / wired: <= 1000 Hz
            return {
                "reportRates": [r for r in base["reportRates"] if r in (1, 2, 4, 8)],
                "rateSource": "pid-1k",
            }
        return base
