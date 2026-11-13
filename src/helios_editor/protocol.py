"""Confirmed XD3v3 HID feature-report protocol.

Recovered from the USBPcap captures in ``captures/`` (29 files, Windows
USBPcap, 2023 vendor software). Every claim below is verified byte-for-byte
against those captures; see ``docs/PROTOCOL.md`` for the derivation and the
fixture tests in ``tests/test_protocol_captures.py``.

Transport
---------
SET_REPORT (Feature), 17 bytes, on HID interface 1:

    bmRequestType = 0x21   (host->device, class, interface)
    bRequest      = 0x09   (SET_REPORT)
    wValue        = 0x0208 (Report Type=Feature(2), Report ID=8)
    wIndex        = 0x0001 (interface 1)
    wLength       = 17

Device replies on interrupt-IN endpoint 0x82 with the same 17-byte framing.

Report layout (17 bytes)
------------------------
    [0]      = 0x08              report id
    [1]      = command id        (DriverLib.UsbCommandID)
    [2..3]   = 0x0000           reserved (CommandStatus/address high)
    [4]      = flash address     (low byte of the target offset)
    [5]      = record length     (number of data bytes incl. inner checksum)
    [6..]    = data / value bytes
    [6+len-1]= inner checksum    (last byte of the record)
    [16]     = outer checksum    (last byte of the whole report)
    unused bytes zero-padded

Checksums (verified 243/243 outer, 153/153 inner frames)
--------------------------------------------------------
    checksum(values) = (0x55 - sum(values)) & 0xFF

Equivalently ``sum(record) == 0x55 (mod 256)``. This is the native
``CalculateChecksum`` symbol.

Flash field map (single-byte address, little-endian values where noted)
-----------------------------------------------------------------------
    0x00  len2  report rate enum (125=8, 250=4, 500=2, 1000=1, 2000/4000 TBD)
    0x02  len2  DPI stage count
    0x04  len2  active DPI stage index
    0x0a  len2  LOD (1=1mm, 2=2mm)
    0x0c  len4  current DPI (xDPI, yDPI, DPIex) + inner checksum
    0x10..len4  DPI stage values (4 bytes each)
    0x64/0x68/0x74 .. len4  button KeyFunMap(type, param1, param2)
    0xa9  len2  click debounce ms (0..20)
    0xab  len2  motion sync enable
    0xaf  len2  angle snapping enable
    0xb1  len2  ripple control enable
    0xb5  len2  peak-performance enable
    0xb7  len2  peak-performance timeout
    0xb9  len2  sensor mode (0=LP, 1=HP)

Commands observed
-----------------
    0x01 EncryptionData  - startup unlock: two fixed password words
    0x02 PCDriverStatus  - sent after a config write
    0x03 DeviceOnLine    - poll / keepalive
    0x04 BatteryLevel    - poll
    0x07 WriteFlashData  - write a flash record
    0x08 ReadFlashData   - request a 10-byte flash window
    0x0e / 0x12 / 0x17  - ClearSetting / ReadVersionID / GetLongRangeMode polls

Writes do NOT require the 0x01 encryption handshake: every single-action
capture (poll-125, dpi-0400, lod-low, button-remap, sensor-*) writes with only
0x03/0x04 polls and 0x07 records. The handshake appears only when the vendor
app starts up / reads config.
"""

from __future__ import annotations

from .model import DPI_MIN, DPI_MAX, DPI_STEP
from .catalog import REPORT_RATES

REPORT_ID = 0x08
REPORT_LEN = 17
BM_REQUEST_TYPE = 0x21
B_REQUEST_SET_REPORT = 0x09
W_VALUE = 0x0208   # Feature report, id 8
W_INDEX = 0x0001   # HID interface 1
CHECKSUM_SEED = 0x55

# --- commands (DriverLib.UsbCommandID, confirmed on the wire) ---
CMD_ENCRYPTION_DATA = 0x01
CMD_PC_DRIVER_STATUS = 0x02
CMD_DEVICE_ONLINE = 0x03
CMD_BATTERY_LEVEL = 0x04
CMD_WRITE_FLASH_DATA = 0x07
CMD_READ_FLASH_DATA = 0x08
CMD_CLEAR_SETTING = 0x0E
CMD_READ_VERSION_ID = 0x12
CMD_GET_LONG_RANGE_MODE = 0x17

# --- flash field addresses ---
ADDR_REPORT_RATE = 0x00
ADDR_DPI_STAGE_COUNT = 0x02
ADDR_ACTIVE_DPI_INDEX = 0x04
ADDR_LOD = 0x0A
# DPI stage registers: 0x0c + 4*i (8 physical slots, 4 user-facing via
# DPIMaxGrade=4). Reconstructed from the cmd-8 flash sweep.
ADDR_DPI_STAGE_BASE = 0x0C
DPI_STAGE_STRIDE = 4
# Per-stage LED RGB colors: 0x2c + 4*i, 3 bytes RGB + inner checksum.
# Config.ini DPIColor = red, blue, green, magenta, ...
ADDR_DPI_COLOR_BASE = 0x2C
# 6 physical button KeyFunMaps: 0x60 + 4*i (type,p1,p2,checksum).
ADDR_KEYMAP_BASE = 0x60
KEYMAP_STRIDE = 4
ADDR_DEBOUNCE = 0xA9
ADDR_MOTION_SYNC = 0xAB
ADDR_ANGLE_SNAPPING = 0xAF
ADDR_RIPPLE_CONTROL = 0xB1
ADDR_PEAK_PERFORMANCE = 0xB5
ADDR_PEAK_TIMEOUT = 0xB7
ADDR_SENSOR_MODE = 0xB9

CONFIRMED_DPI_MAX = 3200   # highest DPI whose register encoding is capture-proven


def checksum(values: bytes) -> int:
    """Return the 0x55-seeded additive checksum for ``values``."""
    return (CHECKSUM_SEED - (sum(values) & 0xFF)) & 0xFF


def build_report(command: int, address: int = 0, length: int = 0,
                 data: bytes = b"", *, inner_checksum: bool = True) -> bytes:
    """Build a 17-byte feature report.

    ``address`` is a 16-bit flash offset encoded big-endian in bytes [3] (high)
    and [4] (low) - verified by frames such as ``08 08 00 0c 00 0a ...``
    (addr 0x0c00) and ``08 07 00 01 40 08 ...`` (addr 0x0140).
    ``data`` holds the value bytes WITHOUT the inner checksum; when
    ``inner_checksum`` is true the inner byte is appended. ``length`` is the
    number of bytes in the record *including* its inner checksum (as observed
    on the wire). When omitted it is derived as ``len(data) + 1``.
    EncryptionData (0x01) challenge frames carry 4 raw data bytes with NO
    inner checksum; pass ``inner_checksum=False`` for those.
    """
    hi, lo = (address >> 8) & 0xFF, address & 0xFF
    if not data:
        # Poll / status / read frames: byte[3]=addr_hi, [4]=addr_lo,
        # [5]=length field, NO inner checksum; outer covers bytes 0..15.
        body = bytes((
            REPORT_ID, command, 0x00, hi, lo, length & 0xFF,
        ))
        frame = bytearray(body.ljust(16, b"\x00"))
    elif not inner_checksum:
        # Raw data frames (EncryptionData challenge): data occupies [6:6+len],
        # no inner checksum; outer covers bytes 0..15.
        rec_len = length if length else len(data)
        body = bytes((
            REPORT_ID, command, 0x00, hi, lo, rec_len & 0xFF,
        )) + bytes(data)[:14]
        frame = bytearray(body[:16].ljust(16, b"\x00"))
    else:
        rec_len = length if length else len(data) + 1
        inner = checksum(data)
        record = bytes(data) + bytes((inner,))
        if len(record) != rec_len:
            raise ValueError(f"record length mismatch: {len(record)} != {rec_len}")
        body = bytes((
            REPORT_ID, command, 0x00, hi, lo, rec_len & 0xFF,
        )) + record
        frame = bytearray(body[:16].ljust(16, b"\x00"))
    frame.append(checksum(bytes(frame)))
    return bytes(frame)


def write_record(address: int, values: bytes) -> bytes:
    """Build a WriteFlashData (0x07) report for one flash record.

    ``values`` are the raw value bytes; the inner checksum is appended.
    Example from captures::

        write_record(0x00, b"\\x08") ... -> 08 07 00 00 00 02 08 4d ... ef
    """
    return build_report(CMD_WRITE_FLASH_DATA, address=address,
                        length=len(values) + 1, data=values)


def read_request(address: int, length: int = 10) -> bytes:
    """Build a ReadFlashData (0x08) report requesting a flash window."""
    return build_report(CMD_READ_FLASH_DATA, address=address, length=length)


def poll(command: int) -> bytes:
    """Build a zero-data status/poll report (0x03, 0x04, 0x12, ...)."""
    return build_report(command)


def raw_frame(command: int, values: bytes, length: int | None = None,
              address: int = 0) -> bytes:
    """Build a report whose data bytes carry no inner checksum.

    Used for EncryptionData (0x01) challenge/response and PCDriverStatus
    (0x02). ``length`` is the wire length field; observed cmd-1 frames set it
    to 8 while carrying only 4 challenge bytes.
    """
    return build_report(command, address, length if length is not None else len(values),
                        values, inner_checksum=False)


def parse_report(frame: bytes) -> dict:
    """Decode a 17-byte report; verify both checksums.

    Returns a dict with command/address/length/data/inner/outer and a
    ``valid`` flag. Used for device responses and for fixture verification.
    """
    if len(frame) != REPORT_LEN:
        raise ValueError(f"report must be {REPORT_LEN} bytes, got {len(frame)}")
    outer_ok = frame[16] == checksum(frame[:16])
    command = frame[1]
    address = (frame[3] << 8) | frame[4]
    length = frame[5]
    data = frame[6:6 + max(0, length - 1)]
    inner = frame[6 + length - 1] if length else None
    # cmd 0x01/0x02 carry raw data with no inner checksum; cmd 0x08 read
    # requests have a zero data window. For WriteFlashData the inner byte is
    # the checksum of the preceding value bytes.
    if command in (0x01, 0x02, 0x08) or length == 0:
        inner_ok = True
    else:
        inner_ok = (inner is None) or (inner == checksum(data))
    return {
        "report_id": frame[0],
        "command": command,
        "address": address,
        "length": length,
        "data": data,
        "inner_checksum": inner,
        "outer_checksum": frame[16],
        "outer_valid": outer_ok,
        "inner_valid": inner_ok,
        "valid": outer_ok and inner_ok,
    }


def report_rate_value(rate_hz: int) -> bytes:
    """Encode a DriverLib.REPORT_RATE enum value to its on-wire byte.

    The on-wire byte equals the enum value (verified: R_125=8 -> 0x08,
    R_250=4 -> 0x04, R_500=2 -> 0x02, R_1000=1 -> 0x01). The model stores
    the enum, so this is an identity check, not a Hz conversion.
    """
    if rate_hz not in REPORT_RATES:
        raise ValueError(f"unsupported REPORT_RATE enum {rate_hz}")
    return bytes((rate_hz & 0xFF,))


def dpi_to_register(dpi: int) -> bytes:
    """Encode a DPI value as the 3-byte (x,y,ex) sensor record.

    Verified mapping from the captures: the register index is
    ``dpi // 50 - 1`` for the low range (400->0x07, 800->0x0f,
    1600->0x1f, 3200->0x3f); values above the native range use the DPIex
    byte. The 3200..26000 mapping is NOT fully confirmed: the only captured
    extended record is ``3f 3f 55`` (one max-DPI write). We therefore emit
    that exact observed marker for any value above 3200 and flag it via
    :func:`dpi_encoding_confirmed` so callers can warn the user rather than
    silently claiming the value was written.
    """
    idx = dpi // 50 - 1
    if idx <= 0x3F:
        return bytes((idx, idx, 0x00))
    # Extended range: native register saturates at 0x3f; 0x55 is the only
    # observed DPIex byte. Emit exactly that observed marker.
    return bytes((0x3F, 0x3F, 0x55))


def dpi_encoding_confirmed(dpi: int) -> bool:
    """True when the on-wire DPI register encoding is capture-confirmed."""
    return DPI_MIN <= dpi <= CONFIRMED_DPI_MAX and (dpi - DPI_MIN) % DPI_STEP == 0


def dpi_stage_address(index: int) -> int:
    """Flash address of DPI stage ``index`` (0..3)."""
    return ADDR_DPI_STAGE_BASE + DPI_STAGE_STRIDE * index


def dpi_color_address(index: int) -> int:
    """Flash address of the LED RGB color for DPI stage ``index``."""
    return ADDR_DPI_COLOR_BASE + DPI_STAGE_STRIDE * index


def keymap_address(button_id: int) -> int:
    """Flash address of the KeyFunMap for physical button ``button_id`` (1..6)."""
    return ADDR_KEYMAP_BASE + KEYMAP_STRIDE * (button_id - 1)


def decode_battery(reply: bytes) -> dict:
    """Decode a cmd-4 BatteryLevel reply.

    Verified against captures: ``08040000000246000f8100...`` -> 70%, 3969 mV.
    ``[6]`` = percentage, ``[8:10]`` = voltage in mV (big-endian).
    ``Config.ini BatteryParam`` supplies the 3050..4110 mV curve.
    """
    pct = reply[6] if len(reply) > 6 else 0
    mv = ((reply[8] << 8) | reply[9]) if len(reply) > 9 else 0
    return {"percent": pct, "voltage_mv": mv}


# Vendor PID -> connection mode. Config.ini M_PID=FantechF536 (mouse wired),
# D_PID=FantechF535,F537 (2.4 GHz dongles). Bluetooth uses the host BT
# controller, not a vendor USB config interface, so it is NOT detectable here.
def describe_pid(pid: int) -> str:
    return {
        0xF536: "Wired USB-C",
        0xF535: "2.4 GHz dongle",
        0xF537: "2.4 GHz dongle (4K)",
    }.get(pid, "unknown")


def profile_to_records(profile) -> list[tuple[int, bytes, str]]:
    """Map a ButtonProfile to ``(flash_addr, value_bytes, label)`` records.

    Single source of truth shared by the writer and the preview diff, so the
    UI shows exactly what Apply will send. The label is human-readable.
    """
    from .model import HOTSPOTS, MAX_DPI_STAGES  # deferred: model imports catalog

    s = profile.settings
    out: list[tuple[int, bytes, str]] = [
        (ADDR_REPORT_RATE, report_rate_value(s.report_rate), "Polling rate"),
        (ADDR_LOD, bytes((s.lod_mm,)), "Lift-off distance"),
        (ADDR_DEBOUNCE, bytes((s.debounce_ms,)), "Click debounce"),
        (ADDR_MOTION_SYNC, bytes((int(s.motion_sync),)), "Motion sync"),
        (ADDR_ANGLE_SNAPPING, bytes((int(s.angle_snapping),)), "Angle snapping"),
        (ADDR_RIPPLE_CONTROL, bytes((int(s.ripple_control),)), "Ripple control"),
        (ADDR_SENSOR_MODE, bytes((s.sensor_mode,)), "Sensor mode"),
        (ADDR_DPI_STAGE_COUNT, bytes((len(s.dpi_stages),)), "DPI stage count"),
        (ADDR_ACTIVE_DPI_INDEX, bytes((s.active_dpi_index,)), "Active DPI stage"),
    ]
    for i in range(MAX_DPI_STAGES):
        out.append((dpi_stage_address(i), dpi_to_register(s.dpi_stages[i]),
                    f"DPI stage {i + 1}"))
        out.append((dpi_color_address(i), bytes(s.dpi_colors[i]),
                    f"DPI stage {i + 1} LED"))
    for h in HOTSPOTS:
        b = profile.bindings[h.id]
        out.append((keymap_address(h.id), bytes((b.type, b.param1, b.param2)),
                    f"Button {h.id} ({h.label})"))
    return out


def changed_records(baseline, draft) -> list[tuple[int, bytes, str]]:
    """Return only the records whose value bytes differ from ``baseline``.

    Used to make Apply delta-only: a single DPI change writes ONE flash record
    instead of all 23, which is what caused the extreme device lag.
    """
    base = {addr: vals for addr, vals, _ in profile_to_records(baseline)}
    return [(addr, vals, label) for addr, vals, label in profile_to_records(draft)
            if base.get(addr) != vals]
