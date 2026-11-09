"""Behavioral tests for the Helios button editor. No hardware required."""

import json
import threading

import pytest

from helios_editor.catalog import (
    KEY_FUNCTIONS, SUB_FUNCTIONS, selection_to_bytes,
    sub_functions_for,
)
from helios_editor.model import (
    HOTSPOTS, DIAGRAM_WIDTH, DIAGRAM_HEIGHT, ButtonProfile,
)
from helios_editor.transport import (
    HIDTransport, SimulatedTransport, TransportError,
)
from helios_editor import server


# --- catalog ---

def test_catalog_defaults_match_vendor_config():
    left = [k for k in KEY_FUNCTIONS if k.type == 0x01 and k.param == 0x0001]
    assert len(left) == 1 and left[0].label == "Left Button"
    d = {h.id: (h.default_type, h.default_param) for h in HOTSPOTS}
    assert d[1] == (0x01, 0x0001)   # Left
    assert d[2] == (0x01, 0x0002)   # Right
    assert d[3] == (0x01, 0x0004)   # Middle
    assert d[4] == (0x01, 0x0008)   # Macro 1 (Back)
    assert d[5] == (0x01, 0x0010)   # Macro 2 (Forward)
    assert d[6] == (0x02, 0x0001)   # bottom DPI cycle


def test_hotspots_fit_diagram():
    for h in HOTSPOTS:
        assert 0 <= h.x <= DIAGRAM_WIDTH
        assert 0 <= h.y <= DIAGRAM_HEIGHT
    assert len(HOTSPOTS) == 6  # physical hardware buttons


def test_sub_function_split_roundtrip():
    for sf in SUB_FUNCTIONS:
        lo, hi = selection_to_bytes(sf.type, sf.value)
        assert lo | (hi << 8) == sf.value
    assert selection_to_bytes(0x02, 0x0183) == (0x83, 0x01)


def test_pulldown_functions_have_sublists():
    dpi = next(k for k in KEY_FUNCTIONS if k.label == "DPI Switch")
    assert dpi.pull == 0x01
    subs = sub_functions_for(dpi)
    assert {s.label for s in subs} == {"DPI Loop", "DPI+", "DPI-"}
    fixed = next(k for k in KEY_FUNCTIONS if k.label == "Left Button")
    assert sub_functions_for(fixed) == []


# --- model / validation ---

def test_default_profile_is_valid():
    p = ButtonProfile()
    assert p.validate() == []
    assert len(p.bindings) == 6


def test_left_button_is_reserved():
    p = ButtonProfile()
    # model now rejects the rebind itself (vendor guard rail)
    import pytest
    with pytest.raises(ValueError):
        p.set_binding(1, 0x00, 0x0000)
    assert p.validate() == []
    # vendor dialog: <ReserveLeftKey>Must keep Left key</ReserveLeftKey>


def test_dpi_validation():
    p = ButtonProfile()
    # DPIMaxGrade=4: at most 4 stages, each with a unique LED color
    p.settings.dpi_stages = [400, 800, 1600, 3200]
    p.settings.dpi_colors = [(255,0,0),(0,0,255),(0,255,0),(255,0,255)]
    assert p.validate() == []
    p.settings.dpi_colors[1] = p.settings.dpi_colors[0]
    assert any("UNIQUE" in e for e in p.validate())
    p.settings.dpi_stages[0] = 40001
    assert any("DPI stage 1" in e for e in p.validate())
    p.settings.dpi_stages[0] = 413
    assert any("DPI stage 1" in e for e in p.validate())


def test_enum_validation():
    p = ButtonProfile()
    p.settings.report_rate = 999
    p.settings.lod_mm = 3
    p.settings.debounce_ms = 99
    errs = p.validate()
    assert any("report_rate" in e for e in errs)
    assert any("lod_mm" in e for e in errs)
    assert any("debounce_ms" in e for e in errs)


def test_binding_byte_range_and_unknown_button():
    p = ButtonProfile()
    with pytest.raises(ValueError):
        p.set_binding(99, 0x01, 1)


def test_profile_json_roundtrip():
    p = ButtonProfile(name="Test")
    p.set_binding(6, 0x02, 0x0001)  # DPI Loop (bottom DPI cycle button)
    p.settings.report_rate = 16      # 2000 Hz
    q = ButtonProfile.from_json(p.to_json())
    assert q.name == "Test"
    assert q.bindings[6].describe() == "DPI Loop"
    assert q.settings.report_rate == 16
    assert q.validate() == []


def test_from_dict_rejects_bad_format():
    with pytest.raises(ValueError):
        ButtonProfile.from_dict({"format": "other", "version": 1})


def test_keys_bytes_is_6_x_3():
    p = ButtonProfile()
    b = p.to_keys_bytes()
    assert len(b) == 6 * 3
    assert b[0:3] == bytes((0x01, 0x01, 0x00))  # button 1 = Left
    assert b[3:6] == bytes((0x01, 0x02, 0x00))  # button 2 = Right
    # change then confirm bytes change
    p.set_binding(2, 0x00, 0)
    assert p.to_keys_bytes()[3:6] == bytes((0x00, 0x00, 0x00))


# --- transport ---

def test_simulated_transport_roundtrip():
    t = SimulatedTransport()
    p = ButtonProfile()
    p.set_binding(6, 0x02, 0x0002)  # DPI+
    t.write_profile(p)
    assert t.writes == 1
    assert t.read_profile().bindings[6].describe() == "DPI+"


def test_simulated_transport_rejects_invalid():
    t = SimulatedTransport()
    p = ButtonProfile()
    # Left Click rebind is now rejected by the model itself; make the profile
    # invalid by corrupting a DPI stage instead.
    p.settings.dpi_stages[0] = 40001
    with pytest.raises(TransportError):
        t.write_profile(p)


def test_real_transport_dry_run_is_safe():
    t = HIDTransport(dry_run=True)
    # No ioctl is performed; reports are built with confirmed framing.
    t.write_profile(ButtonProfile())
    assert t.last_reports, "dry-run must still build the report bytes"
    assert all(len(f) == 17 and f[0] == 0x08 for f in t.last_reports)


def test_real_transport_decoded_ids():
    from helios_editor.hardware import VID, PID, CONFIG_INTERFACE_NUMBER
    assert (VID, PID) == (0x3554, 0xF535)   # decoded Config.ini tokens
    assert CONFIG_INTERFACE_NUMBER == 1


# --- HTTP API (in-process server) ---

@pytest.fixture(scope="module")
def httpd():
    srv = server.serve("127.0.0.1", 0, SimulatedTransport())
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    yield srv
    srv.shutdown()
    srv.server_close()


def test_api_state_and_binding(httpd):
    import urllib.request
    port = httpd.server_address[1]
    base = f"http://127.0.0.1:{port}"

    r = json.load(urllib.request.urlopen(base + "/api/state"))
    assert r["catalog"]["diagram"] == {"width": 432, "height": 356}
    assert len(r["catalog"]["hotspots"]) == 6
    assert r["catalog"]["dpi"]["maxStages"] == 4
    assert len(r["catalog"]["dpi"]["palette"]) == 14
    assert r["catalog"]["transport"].startswith("SimulatedTransport")

    body = json.dumps({"buttonId": 6, "type": 0x02, "value": 0x0003}).encode()  # bottom DPI cycle
    req = urllib.request.Request(base + "/api/binding", body,
                                 {"Content-Type": "application/json"})
    r = json.load(urllib.request.urlopen(req))
    b = r["profile"]["bindings"]["6"]
    assert b["type"] == 0x02 and b["param1"] == 0x03 and b["param2"] == 0x00

    # DPI slider save + LED color endpoints
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        base + "/api/dpi", json.dumps({"stage": 0, "dpi": 1200}).encode(),
        {"Content-Type": "application/json"})))
    assert r["profile"]["settings"]["dpi_stages"][0] == 1200
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        base + "/api/dpi-color", json.dumps({"stage": 0, "rgb": [0, 255, 255]}).encode(),
        {"Content-Type": "application/json"})))
    assert r["profile"]["settings"]["dpi_colors"][0] == [0, 255, 255]
    # status endpoint
    st = json.load(urllib.request.urlopen(base + "/api/status"))
    assert "connection" in st

    # apply succeeds on the simulated device
    r = json.load(urllib.request.urlopen(urllib.request.Request(
        base + "/api/apply", b"{}", {"Content-Type": "application/json"})))
    assert r["ok"] is True


def test_api_apply_on_real_dry_run_backend():
    srv = server.serve("127.0.0.1", 0, HIDTransport(dry_run=True))
    t = threading.Thread(target=srv.serve_forever, daemon=True)
    t.start()
    try:
        import urllib.request
        base = f"http://127.0.0.1:{srv.server_address[1]}"
        req = urllib.request.Request(base + "/api/apply", b"{}",
                                     {"Content-Type": "application/json"})
        r = json.load(urllib.request.urlopen(req))
        assert r["ok"] is True
        assert "HIDTransport" in r["transport"]
        # No-op Apply writes NOTHING (delta-only). This is the lag fix: an
        # unchanged profile must not churn the USB config interface.
        assert srv.editor_state.transport.last_reports == []
        assert srv.editor_state.transport.last_written_records == []
        # A single staged change must produce exactly one flash record.
        req2 = urllib.request.Request(base + "/api/dpi",
                                      json.dumps({"stage": 0, "dpi": 1200}).encode(),
                                      {"Content-Type": "application/json"})
        json.load(urllib.request.urlopen(req2))
        prev = json.load(urllib.request.urlopen(base + "/api/preview"))
        assert prev["count"] == 1 and prev["changes"][0]["label"] == "DPI stage 1"
        json.load(urllib.request.urlopen(urllib.request.Request(
            base + "/api/apply", b"{}", {"Content-Type": "application/json"})))
        assert len(srv.editor_state.transport.last_written_records) == 1
    finally:
        srv.shutdown()
        srv.server_close()
