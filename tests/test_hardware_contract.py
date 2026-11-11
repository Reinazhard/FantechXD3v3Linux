"""Regression tests for the corrected XD3v3 hardware contract.

Evidence: Config.ini, the vendor's 设备1.png, the 14 LED swatches, and the
full cmd-8 flash sweep reconstructed from dpi-read-response.pcapng.
See docs/HARDWARE.md.
"""

from helios_editor.model import (
    HOTSPOTS, MAX_DPI_STAGES, DPI_MIN, DPI_MAX, DPI_STEP,
    DEFAULT_DPI_STAGES, DEFAULT_DPI_COLORS, LED_PALETTE,
    ButtonProfile, voltage_to_percent,
)
from helios_editor import protocol as P


def test_six_physical_buttons():
    assert len(HOTSPOTS) == 6
    labels = [h.label for h in HOTSPOTS]
    assert "Left Click" in labels[0]
    assert "Right Click" in labels[1]
    assert "Middle" in labels[2]
    assert "left" in labels[3] and "left" in labels[4]  # macros on LEFT side
    assert "bottom" in labels[5]                        # DPI cycle on bottom
    assert HOTSPOTS[0].fixed is True                     # Left Click reserved
    assert all(not h.fixed for h in HOTSPOTS[1:])


def test_button_flash_addresses():
    # 0x60 + 4*i, 6 buttons
    assert [h.flash_addr for h in HOTSPOTS] == [0x60, 0x64, 0x68, 0x6C, 0x70, 0x74]
    assert [P.keymap_address(i) for i in range(1, 7)] == [0x60, 0x64, 0x68, 0x6C, 0x70, 0x74]
    # default button 6 = DPI Loop (type 0x02, param 0x0001)
    assert HOTSPOTS[5].default_type == 0x02 and HOTSPOTS[5].default_param == 0x0001


def test_four_dpi_stages_with_unique_colors():
    assert MAX_DPI_STAGES == 4
    assert len(DEFAULT_DPI_STAGES) == 4
    assert len(DEFAULT_DPI_COLORS) == 4
    assert len(set(DEFAULT_DPI_COLORS)) == 4
    assert (DPI_MIN, DPI_MAX, DPI_STEP) == (50, 26000, 50)
    p = ButtonProfile()
    assert p.validate() == []
    assert len(p.settings.dpi_stages) == 4
    assert len(p.settings.dpi_colors) == 4


def test_dpi_and_color_flash_addresses():
    assert [P.dpi_stage_address(i) for i in range(4)] == [0x0C, 0x10, 0x14, 0x18]
    assert [P.dpi_color_address(i) for i in range(4)] == [0x2C, 0x30, 0x34, 0x38]
    # Config.ini DPIColor first four: red, blue, green, magenta
    assert DEFAULT_DPI_COLORS == ((255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 0, 255))


def test_led_palette_matches_vendor_swatches():
    assert len(LED_PALETTE) == 14
    assert LED_PALETTE[0] == (255, 0, 0)      # 颜色1 red
    assert LED_PALETTE[1] == (0, 255, 255)    # 颜色2 cyan
    assert LED_PALETTE[2] == (0, 255, 0)      # 颜色3 green
    assert LED_PALETTE[3] == (255, 0, 255)    # 颜色4 magenta
    assert LED_PALETTE[6] == (0, 0, 255)      # 颜色7 blue


def test_battery_decode():
    # captured replies: 70% @ 3969 mV ; live: 15% @ 3524 mV
    assert P.decode_battery(bytes.fromhex("08040000000246000f8100000000000071")) == {
        "percent": 0x46, "voltage_mv": 0x0F81}
    assert P.decode_battery(bytes.fromhex("0804000000020f000dc400000000000067")) == {
        "percent": 0x0F, "voltage_mv": 0x0DC4}
    assert voltage_to_percent(3050) == 0
    assert voltage_to_percent(4110) == 100
    assert 0 <= voltage_to_percent(3524) <= 100


def test_connection_modes():
    # Config.ini M_PID=F536 (wired), D_PID=F535,F537 (dongles)
    assert P.describe_pid(0xF536) == "Wired USB-C"
    assert "dongle" in P.describe_pid(0xF535).lower()
    assert "dongle" in P.describe_pid(0xF537).lower()
    assert P.describe_pid(0x1234) == "unknown"
    # Bluetooth has no vendor USB config interface -> not representable by a PID


def test_supported_pids_cover_all_connection_modes():
    from helios_editor import hardware as H
    assert (H.VID, H.PID, H.CONFIG_INTERFACE_NUMBER) == (0x3554, 0xF535, 1)
    assert set(H.SUPPORTED_PIDS) == {0xF535, 0xF536, 0xF537}


def test_slider_save_and_color_change():
    p = ButtonProfile()
    p.set_dpi_stage(2, 1200)
    assert p.settings.dpi_stages[2] == 1200
    p.set_dpi_color(2, (0, 255, 255))
    assert p.settings.dpi_colors[2] == (0, 255, 255)
    assert p.validate() == []
    # invalid stage/color rejected
    import pytest
    with pytest.raises(ValueError):
        p.set_dpi_stage(4, 800)          # only 4 stages
    with pytest.raises(ValueError):
        p.set_dpi_stage(0, 413)          # not a step-50 value
    # duplicate colors are invalid
    p.set_dpi_color(3, p.settings.dpi_colors[2])
    assert any("UNIQUE" in e for e in p.validate())


def test_write_profile_emits_all_six_buttons_and_four_stages():
    from helios_editor.transport import HIDTransport
    t = HIDTransport(dry_run=True)
    t.write_profile(ButtonProfile())
    w = [f for f in t.last_reports if f[1] == P.CMD_WRITE_FLASH_DATA]
    addrs = [(f[3] << 8) | f[4] for f in w]
    for a in [0x60, 0x64, 0x68, 0x6C, 0x70, 0x74]:   # 6 buttons
        assert a in addrs
    for a in [0x0C, 0x10, 0x14, 0x18]:                 # 4 DPI stages
        assert a in addrs
    for a in [0x2C, 0x30, 0x34, 0x38]:                 # 4 LED colors
        assert a in addrs
    assert 0x02 in addrs and 0x04 in addrs              # stage count + active idx
