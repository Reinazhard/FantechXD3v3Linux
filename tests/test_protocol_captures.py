"""Protocol regression tests against real USBPcap-derived fixtures.

Every frame below is an observed SET_REPORT(Feature) report from
``captures/*.pcapng`` (USBPcap, vendor software V1.0). The encoder must
reproduce them byte-for-byte. The checksum rule was derived from all 243
unique frames in the capture set.
"""

import pytest

from helios_editor import protocol as P
from helios_editor.hardware import HIDTransport
from helios_editor.model import ButtonProfile

# Golden frames: (name, hex, command, addr16, length, values_or_None)
FRAMES = [
    ("poll-125 cmd7", "080700000002084d0000000000000000ef", 0x07, 0x0000, 2, b"\x08"),
    ("poll-500 cmd7", "08070000000202530000000000000000ef", 0x07, 0x0000, 2, b"\x02"),
    ("poll-1000 cmd7", "08070000000201540000000000000000ef", 0x07, 0x0000, 2, b"\x01"),
    ("lod-low cmd7", "080700000a0201540000000000000000e5", 0x07, 0x000a, 2, b"\x01"),
    ("lod-high cmd7", "080700000a0202530000000000000000e5", 0x07, 0x000a, 2, b"\x02"),
    ("dpi-0400 cmd7", "080700000c0407070047000000000000e1", 0x07, 0x000c, 4, b"\x07\x07\x00"),
    ("dpi-0800 cmd7", "080700000c0408080045000000000000e1", 0x07, 0x000c, 4, b"\x08\x08\x00"),
    ("dpi-1600 cmd7", "080700000c0410100035000000000000e1", 0x07, 0x000c, 4, b"\x10\x10\x00"),
    ("button-remap cmd7", "0807000074040202005100000000000079", 0x07, 0x0074, 4, b"\x02\x02\x00"),
    ("debounce 0xa9", "08070000a9021441000000000000000046", 0x07, 0x00a9, 2, b"\x14"),
    ("dpi-32000 0x0c", "080700000c043f3f5582000000000000e1", 0x07, 0x000c, 4, b"\x3f\x3f\x55"),
    ("high addr 0x0140", "08070001400802828301428301870000a8", 0x07, 0x0140, 8, b"\x02\x82\x83\x01\x42\x83\x01"),
    ("poll cmd3", "080300000000000000000000000000004a", 0x03, 0x0000, 0, None),
    ("poll cmd4", "0804000000000000000000000000000049", 0x04, 0x0000, 0, None),
    ("read 0x00", "08080000000a000000000000000000003b", 0x08, 0x0000, 10, None),
    ("read 0x0c00", "0808000c000a000000000000000000002f", 0x08, 0x0c00, 10, None),
    ("read 0xaa", "08080000aa0a0000000000000000000091", 0x08, 0x00aa, 10, None),
    ("cmd2 status", "0802000000010100000000000000000049", 0x02, 0x0000, 1, b"\x01"),
    ("cmd1 challenge", "0801000000082843486900000000000028", 0x01, 0x0000, 8, b"\x28\x43\x48\x69"),
]

# Device responses on endpoint 0x82 (also 17-byte, same outer checksum).
RESPONSES = [
    "08030000000101b50a4900000000000040",  # DeviceOnLine online
    "08040000000232000f28000000000000de",  # BatteryLevel
    "0812000000020200000000000000000037",  # ReadVersionID
    "08010000000878d241cc0d0400000000dc",  # EncryptionData challenge
]


def test_checksum_rule():
    assert P.checksum(b"\x00") == 0x55
    assert P.checksum(bytes(range(16))) == (0x55 - 120) & 0xFF
    # sum(record) == 0x55 mod 256
    for _, h, *_ in FRAMES:
        assert sum(bytes.fromhex(h)) & 0xFF == 0x55, h


def test_encoder_reproduces_every_golden_frame():
    for name, h, cmd, addr, ln, vals in FRAMES:
        frame = bytes.fromhex(h)
        if cmd == 0x07:
            got = P.write_record(addr, vals)
        elif cmd == 0x08:
            got = P.read_request(addr, ln)
        elif cmd in (0x01, 0x02):
            got = P.raw_frame(cmd, vals, length=ln)
        else:
            got = P.poll(cmd)
        assert got == frame, f"{name}: got {got.hex()} want {h}"


def test_parse_report_validates_golden_frames():
    for name, h, cmd, addr, ln, vals in FRAMES:
        parsed = P.parse_report(bytes.fromhex(h))
        assert parsed["command"] == cmd, name
        assert parsed["address"] == addr, name
        assert parsed["outer_valid"], name
        assert parsed["valid"], name


def test_parse_report_validates_device_responses():
    for h in RESPONSES:
        parsed = P.parse_report(bytes.fromhex(h))
        assert parsed["outer_valid"], h
        assert parsed["report_id"] == 0x08


def test_write_record_inner_and_outer_checksum():
    f = P.write_record(0x00, b"\x08")
    assert f.hex() == "080700000002084d0000000000000000ef"
    parsed = P.parse_report(f)
    assert parsed["inner_checksum"] == P.checksum(b"\x08")
    assert parsed["outer_checksum"] == P.checksum(f[:16])


def test_report_rate_encoding():
    # REPORT_RATE enum: R_125=8, R_250=4, R_500=2, R_1000=1
    assert P.report_rate_value(8) == b"\x08"
    assert P.report_rate_value(4) == b"\x04"
    assert P.report_rate_value(2) == b"\x02"
    assert P.report_rate_value(1) == b"\x01"
    with pytest.raises(ValueError):
        P.report_rate_value(999)


def test_dpi_register_encoding():
    assert P.dpi_to_register(400) == b"\x07\x07\x00"
    assert P.dpi_to_register(800) == b"\x0f\x0f\x00"
    assert P.dpi_to_register(1600) == b"\x1f\x1f\x00"
    assert P.dpi_to_register(3200) == b"\x3f\x3f\x00"
    assert P.dpi_to_register(32000) == b"\x3f\x3f\x55"


def test_hardware_transport_dry_run_builds_confirmed_reports():
    """Dry-run uses the real framing but performs no ioctl."""
    t = HIDTransport(dry_run=True)
    p = ButtonProfile()
    p.settings.report_rate = 8   # R_125 enum
    p.settings.lod_mm = 2
    p.settings.debounce_ms = 8
    t.write_profile(p)
    frames = t.last_reports
    # startup unlock frames precede the writes; find the first WriteFlashData
    writes = [f for f in frames if f[1] == P.CMD_WRITE_FLASH_DATA]
    # first write = report rate enum 8 (125 Hz)
    assert writes[0].hex() == "080700000002084d0000000000000000ef"
    assert any(f[1] == P.CMD_ENCRYPTION_DATA for f in frames)  # unlock present
    assert any(f[1] == P.CMD_WRITE_FLASH_DATA and f[4] == P.ADDR_LOD for f in frames)
    assert frames[-1][1] == P.CMD_PC_DRIVER_STATUS


def test_hardware_autodetect_finds_connected_device():
    """When the real mouse is attached, autodetect resolves its hidraw node."""
    from helios_editor.hardware import find_config_hidraw
    path = find_config_hidraw()
    # On a host without the device this is None; that is acceptable.
    if path:
        assert path.startswith("/dev/hidraw")


def test_find_config_hidraw_matches_all_pids(monkeypatch, tmp_path):
    """Synthetic sysfs: discovery accepts F535/F536/F537 and rejects others."""
    from helios_editor import hardware as H

    def build(hid_id, name):
        node = tmp_path / name
        dev = node / "device"
        dev.mkdir(parents=True)
        (dev / "uevent").write_text(f"HID_ID={hid_id}\nHID_NAME=Fantech Helios\n")
        return str(node)

    def discover(paths, realpath_map):
        monkeypatch.setattr(H.glob, "glob", lambda pat: paths)
        monkeypatch.setattr(H.os.path, "realpath", lambda p: realpath_map)
        return H.find_config_hidraw()

    for pid in ("F535", "F536", "F537"):
        node = build(f"0003:00003554:0000{pid}", f"hidraw_{pid}")
        assert discover([node], "/x/1-3:1.1/0003:3554:F535.0003") == \
            "/dev/" + f"hidraw_{pid}"

    unrelated = build("0003:00001234:0000ABCD", "hidraw_other")
    assert discover([unrelated], "/x/1-3:1.1/0003:1234:ABCD.0003") is None

    # Fallback branch: uevent WITHOUT HID_ID= (upper-hex substring form).
    fb = tmp_path / "hidraw_fallback"
    (fb / "device").mkdir(parents=True)
    (fb / "device" / "uevent").write_text("PRODUCT=3554/F536/100\n")
    assert discover([str(fb)], "/x/1-3:1.1/0003:3554:F536.0007") == \
        "/dev/hidraw_fallback"


def test_unlock_replays_captured_passwords():
    """The startup unlock must use the two passwords from startup-handshake."""
    from helios_editor.hardware import UNLOCK_PASSWORDS, HIDTransport
    from helios_editor import protocol as P
    assert UNLOCK_PASSWORDS == (b"\x28\x43\x48\x69", b"\x35\x35\x69\x66")
    t = HIDTransport(dry_run=True)
    t.unlock(verify=False)
    enc = [f for f in t.last_reports if f[1] == P.CMD_ENCRYPTION_DATA]
    assert len(enc) == 2
    assert enc[0][6:10] == b"\x28\x43\x48\x69"
    assert enc[1][6:10] == b"\x35\x35\x69\x66"
    # PCDriverStatus(1) after each password
    sts = [f for f in t.last_reports if f[1] == P.CMD_PC_DRIVER_STATUS]
    assert len(sts) == 2 and all(f[6] == 1 for f in sts)
    # clock() polls are interleaved
    assert any(f[1] == P.CMD_DEVICE_ONLINE for f in t.last_reports)


def test_unlock_is_idempotent():
    from helios_editor.hardware import HIDTransport
    t = HIDTransport(dry_run=True)
    t.unlock(verify=False)
    n = len(t.last_reports)
    t.unlock(verify=False)
    assert len(t.last_reports) == n


def test_write_profile_includes_unlock_and_clock():
    """A real Apply must unlock and interleave polls, or writes are dropped."""
    t = HIDTransport(dry_run=True)
    t.write_profile(ButtonProfile())
    f = t.last_reports
    assert any(x[1] == P.CMD_ENCRYPTION_DATA for x in f)   # unlock
    assert any(x[1] == P.CMD_DEVICE_ONLINE for x in f)     # poll clock
    assert sum(1 for x in f if x[1] == P.CMD_WRITE_FLASH_DATA) >= 7


def test_pending_queue_handles_multiple_echoes():
    """The device queues several reports per poll; _take must find each one.

    Regression: reading only ONE report per call dropped write echoes behind
    cmd-3 poll echoes, so a 22-record Apply aborted mid-sequence ("DPI doesn't
    apply") and cmd-4 battery echoes were lost ("battery N/A").
    """
    from helios_editor.hardware import HIDTransport
    t = HIDTransport(dry_run=True)
    poll = P.poll(P.CMD_DEVICE_ONLINE)
    write = P.write_record(0x0C, b"\x0f\x0f\x00")
    batt = bytes.fromhex("0804000000020f000dc00000000000006b")
    t._pending.extend([poll, write, batt, poll])
    assert t._take(P.CMD_WRITE_FLASH_DATA, 0x0C) == write
    assert t._take(P.CMD_BATTERY_LEVEL) == batt
    assert t._take(P.CMD_DEVICE_ONLINE) == poll
    assert len(t._pending) == 1   # one remaining poll echo
    assert t._take(P.CMD_READ_FLASH_DATA) is None
