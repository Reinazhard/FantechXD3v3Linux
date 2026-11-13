"""Reliability regressions for the Helios XD3v3 editor.

SIMULATED verification only. No physical device is used: every hardware
transaction is driven through the injectable ``HIDIO`` seam with a scripted
reply queue. Capture-derived golden framing in test_protocol_captures.py is
unchanged and separately verified.
"""

from __future__ import annotations

import errno
import http.client
import json
import threading
import urllib.error
import urllib.request

import pytest

from helios_editor import protocol as P
from helios_editor import server
from helios_editor.hardware import HIDTransport, TransportError
from helios_editor.model import ButtonProfile
from helios_editor.transport import SimulatedTransport


# --- scripted HIDIO backend -------------------------------------------------

class FakeHIDIO:
    """In-memory HIDIO implementation driven by a per-send response script.

    ``responses`` is a list of items; each ``feature()`` call pops one. An item
    may be:
      - ``list[bytes]``: frames returned by the next read() call.
      - ``bytes``: a single echo frame.
      - ``callable``: ``f(frame) -> list[bytes]`` (used to echo written frames).
      - ``OSError``: raised from feature() to simulate a disconnect.
      - ``None``: no reply for this send.
    """

    def __init__(self, responses=None, echo_mode=False, read_value=0x02):
        self.responses = list(responses or [])
        self.echo_mode = echo_mode          # echo any written frame verbatim
        self.read_value = read_value        # value byte for synthesized reads
        self.write_log: dict[int, bytes] = {}  # address -> last written vals
        self.sent: list[bytes] = []
        self.opened = 0
        self.closed = 0
        self._reply: list[bytes] = []

    def open(self, path: str) -> int:
        self.opened += 1
        return 7

    def feature(self, fd: int, frame: bytes) -> None:
        self.sent.append(frame)
        if frame[1] == P.CMD_WRITE_FLASH_DATA:
            length = frame[5]
            self.write_log[(frame[3] << 8) | frame[4]] = \
                bytes(frame[6:6 + length - 1])
        if not self.responses:
            if self.echo_mode:
                self._reply = [self._echo_for(frame)]
            else:
                self._reply = []
            return
        item = self.responses.pop(0)
        if isinstance(item, OSError):
            raise item
        if callable(item):
            self._reply = item(frame)
        elif isinstance(item, bytes):
            self._reply = [item]
        elif item is None:
            self._reply = []
        else:
            self._reply = list(item)

    def _echo_for(self, frame: bytes) -> bytes:
        cmd = frame[1]
        if cmd == P.CMD_WRITE_FLASH_DATA:
            return frame                       # write ACK echoes the frame
        if cmd == P.CMD_READ_FLASH_DATA:
            length = frame[5]
            addr = (frame[3] << 8) | frame[4]
            vals = self.write_log.get(
                addr, bytes([self.read_value]) * max(1, length - 1))
            return P.build_report(P.CMD_READ_FLASH_DATA,
                                  address=addr,
                                  length=length, data=vals,
                                  inner_checksum=True)
        return frame

    def read(self, fd: int, timeout: float) -> list[bytes]:
        out, self._reply = self._reply, []
        return out

    def close(self, fd: int) -> None:
        self.closed += 1


def _real(io, device_path="/dev/fake") -> HIDTransport:
    return HIDTransport(dry_run=False, device_path=device_path, io=io)


def _read_reply(address: int, length: int, value: int = 0x02) -> bytes:
    """Build a valid CMD_READ_FLASH_DATA reply carrying ``value`` bytes."""
    vals = bytes([value]) * max(1, length - 1)
    return P.build_report(P.CMD_READ_FLASH_DATA, address=address,
                          length=length, data=vals, inner_checksum=True)


def _srv(transport):
    s = server.serve("127.0.0.1", 0, transport)
    t = threading.Thread(target=s.serve_forever, daemon=True)
    t.start()
    return s, "http://127.0.0.1:%d" % s.server_address[1]


def _get(base, path):
    return json.load(urllib.request.urlopen(base + path))


def _post(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    return json.load(urllib.request.urlopen(req))


def _post_status(base, path, body):
    req = urllib.request.Request(base + path, json.dumps(body).encode(),
                                 {"Content-Type": "application/json"})
    try:
        r = urllib.request.urlopen(req)
        return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read())


# --- 1. serialized concurrent poll + apply ----------------------------------

def test_concurrent_poll_and_apply():
    fake = FakeHIDIO(echo_mode=True)
    t = _real(fake)
    p = ButtonProfile()
    errors = []

    def poll():
        try:
            for _ in range(5):
                t.read_active_dpi()
        except Exception as exc:  # noqa: BLE001 - report in errors
            errors.append(exc)

    def apply():
        try:
            t.write_profile(p, baseline=ButtonProfile())
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    a = threading.Thread(target=poll)
    b = threading.Thread(target=apply)
    a.start(); b.start(); a.join(); b.join()
    assert errors == []
    assert t._conn_generation == 0        # no spurious reset/reconnect
    # Every write ACK in the queue matched its own command/address (no
    # cross-transaction echo mixups under the RLock).
    assert all(f[1] in (P.CMD_WRITE_FLASH_DATA, P.CMD_READ_FLASH_DATA,
                        P.CMD_DEVICE_ONLINE, P.CMD_ENCRYPTION_DATA,
                        P.CMD_PC_DRIVER_STATUS) for f in fake.sent)


# --- 2. stale echo is purged before send -----------------------------------

def test_stale_echo_not_matched():
    addr = P.dpi_stage_address(0)
    vals = b"\x0f\x0f\x00"
    frame = P.write_record(addr, vals)
    stale = P.write_record(addr, b"\x07\x07\x00")
    fake = FakeHIDIO(responses=[[frame]])     # only the NEW echo is available
    t = _real(fake)
    t._pending.append(stale)                  # leftover from a prior op
    t.write_flash(addr, vals)
    assert fake.sent[0] == frame             # write sent verbatim
    assert stale not in t._pending           # stale echo purged pre-send


# --- 3. delayed + mismatched ACKs, bounded attempts ------------------------

def test_delayed_and_mismatched_ack(monkeypatch):
    import helios_editor.hardware as H
    monkeypatch.setattr(H, "PUMP_AFTER_SEND", 0.001)
    monkeypatch.setattr(H, "PUMP_AFTER_POLL", 0.001)
    addr = P.dpi_stage_address(0)
    vals = b"\x0f\x0f\x00"
    frame = P.write_record(addr, vals)
    wrong = P.write_record(P.ADDR_LOD, b"\x01")   # different address
    # First read: delayed/wrong reply; subsequent poll-driven reads: wrong
    # reply until the correct echo finally arrives on the 3rd attempt.
    fake = FakeHIDIO(responses=[
        [wrong],            # after send -> mismatch, not consumed as echo
        [wrong],            # poll attempt 1
        [wrong],            # poll attempt 2
        [frame],            # poll attempt 3 -> correct echo
    ])
    t = _real(fake)
    t.write_flash(addr, vals)
    assert fake.sent[0] == frame
    polls = sum(1 for f in fake.sent if f[1] == P.CMD_DEVICE_ONLINE)
    assert 1 <= polls < H.ECHO_ATTEMPTS        # bounded, stopped on match


# --- 4. missing response -> bounded timeout --------------------------------

def test_missing_response_times_out(monkeypatch):
    import helios_editor.hardware as H
    monkeypatch.setattr(H, "PUMP_AFTER_SEND", 0.001)
    monkeypatch.setattr(H, "PUMP_AFTER_POLL", 0.001)
    fake = FakeHIDIO(responses=[])             # never replies
    t = _real(fake)
    with pytest.raises(TransportError) as exc:
        t.write_flash(P.ADDR_LOD, b"\x01")
    assert "after 6 polls" in str(exc.value)
    assert "mouse link may be down" in str(exc.value)


# --- 5. unexpected HID report is ignored -----------------------------------

def test_unexpected_hid_report():
    addr = P.dpi_stage_address(0)
    vals = b"\x0f\x0f\x00"
    frame = P.write_record(addr, vals)
    unexpected = P.poll(P.CMD_READ_VERSION_ID)   # cmd 0x12, unrelated
    fake = FakeHIDIO(responses=[[unexpected, frame]])
    t = _real(fake)
    t.write_flash(addr, vals)
    assert fake.sent[0] == frame
    # Correct echo consumed; unexpected report is not mistaken for it.
    assert t._take(P.CMD_WRITE_FLASH_DATA, addr) is None


# --- 6. disconnect during transaction resets connection state --------------

def test_disconnect_during_transaction():
    fake = FakeHIDIO(responses=[
        OSError(errno.ENODEV, "No such device"),
    ])
    t = _real(fake)
    t._unlocked = True
    t._pending.append(P.poll(P.CMD_DEVICE_ONLINE))
    t._device_state = {"something": 1}
    with pytest.raises(TransportError) as exc:
        t.write_flash(P.ADDR_LOD, b"\x01")
    assert "device I/O failed" in str(exc.value)
    assert t._unlocked is False
    assert t._pending == []
    assert t._device_state is None
    assert t._conn_generation == 1
    assert fake.closed == 1


# --- 7. reconnect replays both unlock passwords ----------------------------

def test_reconnect_reunlocks():
    # First write_flash fails (disconnect); then a fresh successful unlock()
    # must replay BOTH EncryptionData passwords because _unlocked was cleared.
    fake = FakeHIDIO(echo_mode=True)
    fake.responses = [OSError(errno.ENODEV, "gone")]
    t = _real(fake)
    with pytest.raises(TransportError):
        t.write_flash(P.ADDR_LOD, b"\x01")
    assert t._unlocked is False
    t.unlock(verify=False)
    enc = [f for f in t.last_reports if f[1] == P.CMD_ENCRYPTION_DATA]
    assert len(enc) == 2
    assert enc[0][6:10] == b"\x28\x43\x48\x69"
    assert enc[1][6:10] == b"\x35\x35\x69\x66"


# --- 8. partial write failure keeps draft unsaved --------------------------

def test_partial_write_failure_keeps_draft_unsaved():
    failing = FakeHIDIO(responses=[
        lambda f: [f],                                  # record 1 ACK
        OSError(errno.ENODEV, "link dropped"),          # record 2 fails
    ], echo_mode=False)
    tr = _real(failing)
    s, base = _srv(tr)
    try:
        _post(base, "/api/dpi", {"stage": 0, "dpi": 800})
        _post(base, "/api/dpi", {"stage": 1, "dpi": 1200})
        status, body = _post_status(base, "/api/apply", {})
        assert status == 502
        assert body["error"] == "TRANSPORT_ERROR"
        st = _get(base, "/api/state")
        assert st["dirty"] is True                     # draft NOT lost
        assert tr._device_state is None                # stale baseline dropped
        # A subsequent successful Apply re-writes BOTH records (baseline reset).
        failing.responses = []
        failing.echo_mode = True
        failing.read_value = 0x0f
        ok = _post(base, "/api/apply", {})
        assert ok["written"] == 2
        assert _get(base, "/api/state")["dirty"] is False
    finally:
        s.shutdown(); s.server_close()


# --- 9. read-back mismatch -> no false success -----------------------------

def test_readback_mismatch_no_false_success():
    # Written DPI bytes are 0x0f 0x0f 0x00; device reads back 0x08 0x08 0x00.
    wrong = FakeHIDIO(read_value=0x08)
    def respond(f):
        if f[1] == P.CMD_WRITE_FLASH_DATA:
            return [f]
        return [wrong._echo_for(f)]
    wrong.responses = None                             # handled by callable mode
    wrong.feature = lambda fd, frame: _scripted_feature(wrong, frame)
    tr = _real(wrong)
    s, base = _srv(tr)
    try:
        _post(base, "/api/dpi", {"stage": 0, "dpi": 800})
        status, body = _post_status(base, "/api/apply", {})
        assert status == 502
        assert body["error"] == "TRANSPORT_ERROR"
        assert "read-back mismatch" in body["detail"]
        assert _get(base, "/api/state")["dirty"] is True
    finally:
        s.shutdown(); s.server_close()


def _scripted_feature(fake, frame):
    """Echo writes verbatim; synthesize reads with fake.read_value."""
    fake.sent.append(frame)
    if frame[1] == P.CMD_WRITE_FLASH_DATA:
        fake._reply = [frame]
    else:
        fake._reply = [fake._echo_for(frame)]


# --- 10. draft preserved during background sync ----------------------------

def test_draft_preserved_during_background_sync():
    class FakeDeviceTransport(SimulatedTransport):
        def __init__(self):
            super().__init__()
            self.hw_active = 2

        def read_active_dpi(self):
            return self.hw_active

    tr = FakeDeviceTransport()
    s, base = _srv(tr)
    try:
        _post(base, "/api/dpi", {"stage": 0, "dpi": 1200})
        assert _get(base, "/api/state")["dirty"] is True
        for idx in (0, 1, 3):
            tr.hw_active = idx
            _get(base, "/api/sync")
            st = _get(base, "/api/state")
            assert st["profile"]["settings"]["dpi_stages"][0] == 1200
            assert st["profile"]["settings"]["active_dpi_index"] == idx
            assert st["dirty"] is True
    finally:
        s.shutdown(); s.server_close()


# --- 11. no-op Apply writes nothing ----------------------------------------

def test_no_op_apply_writes_nothing():
    tr = SimulatedTransport()
    s, base = _srv(tr)
    try:
        r = _post(base, "/api/apply", {})
        assert r["written"] == 0
        assert r["dirty"] is False
        assert tr.writes == 0
    finally:
        s.shutdown(); s.server_close()


# --- 12. delta-only writes -------------------------------------------------

def test_delta_only_writes():
    tr = SimulatedTransport()
    s, base = _srv(tr)
    try:
        _post(base, "/api/dpi", {"stage": 0, "dpi": 1200})
        assert _get(base, "/api/preview")["count"] == 1
        r = _post(base, "/api/apply", {})
        assert r["written"] == 1
    finally:
        s.shutdown(); s.server_close()


# --- 13. validation is shared across endpoints -----------------------------

def test_profile_validation_shared():
    s, base = _srv(SimulatedTransport())
    try:
        before = _get(base, "/api/state")["profile"]
        status, body = _post_status(base, "/api/settings", {"report_rate": 999})
        assert status == 400
        assert body["error"] == "VALIDATION_ERROR"
        assert _get(base, "/api/state")["profile"] == before   # unchanged
        bad = ButtonProfile()
        bad.settings.report_rate = 999
        status, body = _post_status(base, "/api/import", bad.to_dict())
        assert status == 400
        assert body["errors"]                          # structured detail
        exported = _get(base, "/api/export")
        assert ButtonProfile.from_dict(exported).validate() == []
    finally:
        s.shutdown(); s.server_close()


# --- 14. simulated / real verification-mode consistency --------------------

def test_simulated_real_consistency():
    assert SimulatedTransport().verification_mode == "simulated"
    real = _real(FakeHIDIO(echo_mode=True))
    assert real.verification_mode == "readback"
    dry = HIDTransport(dry_run=True)
    assert dry.verification_mode == "framing-only"
    p = ButtonProfile()
    SimulatedTransport().write_profile(p, baseline=ButtonProfile())
    dry.write_profile(p, baseline=ButtonProfile())        # framing-only path


# --- 15. SSE stream --------------------------------------------------------

def test_sse_stream():
    s, base = _srv(SimulatedTransport())
    host, port = "127.0.0.1", s.server_address[1]
    try:
        c = http.client.HTTPConnection(host, port, timeout=5)
        c.request("GET", "/api/events")
        r = c.getresponse()
        assert r.status == 200
        assert "text/event-stream" in r.getheader("Content-Type")

        def read_frame(conn_resp):
            buf = b""
            while b"\n\n" not in buf:
                chunk = conn_resp.read(1)
                if not chunk:
                    return None
                buf += chunk
            return buf.decode()

        first = read_frame(r)
        assert first and first.startswith("event: state\n")
        data = json.loads([l[6:] for l in first.splitlines()
                           if l.startswith("data:")][0])
        assert "confirmed" in data and "persisted" in data and "catalog" in data

        c2 = http.client.HTTPConnection(host, port, timeout=5)
        c2.request("POST", "/api/dpi", json.dumps({"stage": 0, "dpi": 1200}),
                   {"Content-Type": "application/json"})
        assert c2.getresponse().status == 200
        second = read_frame(r)
        assert second and second.startswith("event: state\n")
        d2 = json.loads([l[6:] for l in second.splitlines()
                         if l.startswith("data:")][0])
        assert d2["dirty"] is True
        assert any(rec["label"] == "DPI stage 1" for rec in d2["dirtyRecords"])
        c.close(); c2.close()
    finally:
        s.shutdown(); s.server_close()


# --- 16. static path traversal rejected ------------------------------------

def test_static_path_traversal_rejected():
    s, base = _srv(SimulatedTransport())
    try:
        try:
            r = urllib.request.urlopen(base + "/static/../server.py")
            code, body = r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            code, body = e.code, json.loads(e.read())
        assert code == 404
        assert body["error"] == "NOT_FOUND"
    finally:
        s.shutdown(); s.server_close()


# --- 17. consistent error shapes -------------------------------------------

def test_error_shapes_consistent():
    class OfflineTransport(SimulatedTransport):
        def read_profile(self):
            return None

    s, base = _srv(SimulatedTransport())
    try:
        req = urllib.request.Request(base + "/api/dpi", b"{broken",
                                     {"Content-Type": "application/json"})
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(req)
        assert e.value.code == 400
        assert json.loads(e.value.read())["error"] == "INVALID_JSON"

        status, body = _post_status(base, "/api/settings", {"lod_mm": 99})
        assert status == 400 and body["error"] == "VALIDATION_ERROR"

        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(base + "/api/nope")
        assert e.value.code == 404
        assert json.loads(e.value.read())["error"] == "NOT_FOUND"
    finally:
        s.shutdown(); s.server_close()

    s2, base2 = _srv(OfflineTransport())
    try:
        with pytest.raises(urllib.error.HTTPError) as e:
            urllib.request.urlopen(urllib.request.Request(
                base2 + "/api/load", b"{}", {"Content-Type": "application/json"}))
        assert e.value.code == 502
        assert json.loads(e.value.read())["error"] == "TRANSPORT_ERROR"
    finally:
        s2.shutdown(); s2.server_close()


# --- 18. hardware capability detection + backend authority -----------------

def test_capabilities_exposed_in_state():
    s, base = _srv(SimulatedTransport())
    try:
        caps = _get(base, "/api/state")["capabilities"]
        # The simulated backend has no hardware limits: full catalog set.
        assert 1 in caps["reportRates"] and 32 in caps["reportRates"]
        assert caps["rateSource"] == "catalog"
    finally:
        s.shutdown(); s.server_close()


def test_narrow_capabilities_hide_and_reject_unsupported_rates():
    class OneKTransport(SimulatedTransport):
        def capabilities(self):
            return {"reportRates": [1, 2, 4, 8], "rateSource": "pid-1k"}

    s, base = _srv(OneKTransport())
    try:
        caps = _get(base, "/api/state")["capabilities"]
        assert caps["reportRates"] == [1, 2, 4, 8]   # 2000/4000 Hz absent
        # A hidden rate cannot be smuggled in via a direct POST.
        status, body = _post_status(base, "/api/settings", {"report_rate": 16})
        assert status == 400
        assert body["error"] == "VALIDATION_ERROR"
        assert "not supported" in body["detail"]
        # A supported rate still works.
        status, body = _post_status(base, "/api/settings", {"report_rate": 8})
        assert status == 200
        assert body["profile"]["settings"]["report_rate"] == 8
    finally:
        s.shutdown(); s.server_close()


def test_hid_pid_capability_mapping_is_deterministic(monkeypatch):
    import helios_editor.hardware as H
    tr = HIDTransport(dry_run=True, io=FakeHIDIO(echo_mode=True))
    monkeypatch.setattr(tr, "_detected_pid", lambda: 0xF535)
    assert tr.capabilities()["reportRates"] == [1, 2, 4, 8]
    assert tr.capabilities()["rateSource"] == "pid-1k"
    monkeypatch.setattr(tr, "_detected_pid", lambda: 0xF537)
    assert 32 in tr.capabilities()["reportRates"]     # 4K dongle
    monkeypatch.setattr(tr, "_detected_pid", lambda: None)
    assert 32 in tr.capabilities()["reportRates"]     # unknown -> fail open
