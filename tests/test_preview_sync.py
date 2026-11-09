"""Regression tests: HP/LP mode, delta writes, preview, staged DPI, sync safety."""

import json
import threading
import urllib.request

from helios_editor import protocol as P, server
from helios_editor.model import ButtonProfile
from helios_editor.transport import HIDTransport, SimulatedTransport


def _srv(transport):
    s = server.serve("127.0.0.1", 0, transport)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    return s, "http://127.0.0.1:%d" % s.server_address[1]


def _post(base, path, body):
    r = urllib.request.Request(base + path, json.dumps(body).encode(),
                               {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(r))


def _get(base, path):
    return json.load(urllib.request.urlopen(base + path))


# --- HP / LP ------------------------------------------------------------

def test_sensor_mode_defaults_and_validation():
    p = ButtonProfile()
    assert p.settings.sensor_mode == 0
    assert p.validate() == []
    p.settings.sensor_mode = 1
    assert p.validate() == []
    p.settings.sensor_mode = 7
    assert any("sensor_mode" in e for e in p.validate())


def test_sensor_mode_in_records():
    p = ButtonProfile()
    p.settings.sensor_mode = 1
    recs = {a: v for a, v, _ in P.profile_to_records(p)}
    assert recs[P.ADDR_SENSOR_MODE] == b"\x01"  # flash 0xb9 = HP
    p2 = ButtonProfile(); p2.settings.sensor_mode = 0
    assert {a: v for a, v, _ in P.profile_to_records(p2)}[P.ADDR_SENSOR_MODE] == b"\x00"


def test_sensor_mode_roundtrip_json():
    p = ButtonProfile(); p.settings.sensor_mode = 1
    q = ButtonProfile.from_json(p.to_json())
    assert q.settings.sensor_mode == 1


# --- delta writes (lag fix) --------------------------------------------

def test_changed_records_only_returns_differences():
    base = ButtonProfile()
    draft = ButtonProfile()
    draft.set_dpi_stage(2, 1200)
    draft.settings.sensor_mode = 1
    ch = P.changed_records(base, draft)
    labels = {c[2] for c in ch}
    assert labels == {"DPI stage 3", "Sensor mode"}
    assert len(ch) == 2


def test_no_op_apply_writes_nothing():
    t = HIDTransport(dry_run=True)
    t.read_profile()                    # establish baseline
    t.write_profile(ButtonProfile())    # identical -> no records
    assert t.last_written_records == []
    assert not any(f[1] == P.CMD_WRITE_FLASH_DATA for f in t.last_reports)


def test_single_change_writes_one_record():
    t = HIDTransport(dry_run=True)
    t.read_profile()
    p = ButtonProfile(); p.set_dpi_stage(0, 800)
    t.write_profile(p)
    assert len(t.last_written_records) == 1
    assert t.last_written_records[0][0] == P.dpi_stage_address(0)
    assert sum(1 for f in t.last_reports if f[1] == P.CMD_WRITE_FLASH_DATA) == 1


# --- preview + staged DPI + discard + sync ------------------------------

def test_preview_matches_delta_and_discard():
    s, base = _srv(SimulatedTransport())
    try:
        assert _get(base, "/api/preview")["count"] == 0
        _post(base, "/api/dpi", {"stage": 1, "dpi": 2000})
        pv = _get(base, "/api/preview")
        assert pv["count"] == 1 and pv["changes"][0]["label"] == "DPI stage 2"
        assert pv["changes"][0]["address"] == "0x10"
        _post(base, "/api/dpi-color", {"stage": 1, "rgb": [0, 255, 255]})
        assert _get(base, "/api/preview")["count"] == 2
        j = _post(base, "/api/discard", {})
        assert j["dirty"] is False
        assert _get(base, "/api/preview")["count"] == 0
    finally:
        s.shutdown(); s.server_close()


def test_stage_click_sets_active_dpi_staged():
    s, base = _srv(SimulatedTransport())
    try:
        j = _post(base, "/api/dpi-active", {"stage": 3})
        assert j["profile"]["settings"]["active_dpi_index"] == 3
        assert j["dirty"] is True  # staged, not applied
        pv = _get(base, "/api/preview")
        assert any(c["label"] == "Active DPI stage" for c in pv["changes"])
        # out-of-range stage is rejected with HTTP 400
        import urllib.error
        try:
            _post(base, "/api/dpi-active", {"stage": 9})
            assert False, "expected HTTP 400"
        except urllib.error.HTTPError as e:
            assert e.code == 400
    finally:
        s.shutdown(); s.server_close()


class FakeDeviceTransport(SimulatedTransport):
    """Simulates the bottom DPI button changing the device's active stage."""
    def __init__(self):
        super().__init__()
        self.hw_active = 2

    def read_active_dpi(self):
        return self.hw_active

    def describe(self):
        return "FakeDeviceTransport"


def test_auto_sync_merges_hardware_dpi_without_clobbering_draft():
    tr = FakeDeviceTransport()
    s, base = _srv(tr)
    try:
        _post(base, "/api/dpi", {"stage": 0, "dpi": 1200})   # user draft
        assert _get(base, "/api/state")["dirty"] is True
        tr.hw_active = 0                                      # hardware cycles DPI
        r = _get(base, "/api/sync")
        assert r["ok"] is True
        st = _get(base, "/api/state")
        assert st["profile"]["settings"]["active_dpi_index"] == 0  # hardware wins
        assert st["profile"]["settings"]["dpi_stages"][0] == 1200     # draft kept
        assert st["dirty"] is True                                     # still a draft
        assert _get(base, "/api/preview")["count"] == 1                 # only DPI value
        assert _get(base, "/api/sync-status")["generation"] >= 1
    finally:
        s.shutdown(); s.server_close()


def test_auto_sync_single_flight_and_no_draft_clobber():
    tr = FakeDeviceTransport()
    s, base = _srv(tr)
    try:
        a = _get(base, "/api/sync")
        b = _get(base, "/api/sync")
        assert a["ok"] is True and b["ok"] is True
        assert _get(base, "/api/state")["dirty"] is False
    finally:
        s.shutdown(); s.server_close()
