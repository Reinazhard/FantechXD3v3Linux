"""Stdlib HTTP server for the Helios button editor.

Exposes a small JSON API over a local socket and serves the static web UI.
No third-party dependencies: runnable with a bare CPython install, which
matches the project's "userspace driver" constraint.
"""

from __future__ import annotations

import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse
import threading
import time

from .catalog import (
    KEY_FUNCTIONS,
    REPORT_RATES,
    SUB_FUNCTIONS,
    sub_functions_for,
)
from .model import (
    DIAGRAM_HEIGHT,
    DIAGRAM_WIDTH,
    HOTSPOTS,
    ButtonProfile,
    LOD_CHOICES,
    MAX_DPI_STAGES,
    DPI_MIN, DPI_MAX, DPI_STEP,
    LED_PALETTE,
)
from .transport import Transport, TransportError, SimulatedTransport
from .protocol import CONFIRMED_DPI_MAX

WEB_DIR = Path(__file__).parent / "web"


def catalog_payload() -> dict[str, Any]:
    kfs = []
    for kf in KEY_FUNCTIONS:
        subs = [
            {"type": s.type, "value": s.value, "label": s.label}
            for s in sub_functions_for(kf)
        ]
        kfs.append({
            "type": kf.type,
            "param": kf.param,
            "pull": kf.pull,
            "label": kf.label,
            "subs": subs,
        })
    return {
        "keyFunctions": kfs,
        "hotspots": [
            {
                "id": h.id, "x": h.x, "y": h.y, "label": h.label,
                "default_type": h.default_type, "default_param": h.default_param,
                "fixed": h.fixed,
            }
            for h in HOTSPOTS
        ],
        "diagram": {"width": DIAGRAM_WIDTH, "height": DIAGRAM_HEIGHT},
        "dpi": {
            "maxStages": MAX_DPI_STAGES,
            "min": DPI_MIN, "max": DPI_MAX, "step": DPI_STEP,
            "confirmedMax": CONFIRMED_DPI_MAX,
            "palette": ["#%02x%02x%02x" % c for c in LED_PALETTE],
        },
        "reportRates": [
            {"value": k, "label": v} for k, v in REPORT_RATES.items()
        ],
        "lod": [{"value": k, "label": v} for k, v in LOD_CHOICES.items()],
        "sensorModes": [{"value": 0, "label": "LP"}, {"value": 1, "label": "HP"}],
        "transport": None,  # filled by handler
    }


class EditorState:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        # Try to show the device's real state; an unplugged/unreadable backend
        # falls back to a default profile so the editor stays usable.
        try:
            self.profile = transport.read_profile() or ButtonProfile()
        except TransportError:
            self.profile = ButtonProfile()
        self.applied = self.profile.to_dict()
        self._sync_lock = threading.Lock()
        self._last_sync = 0.0
        self._sync_failures = 0
        self._sync_generation = 0        # bumped whenever device state is observed
        self._sync_message = "starting"
        # At process start read_profile() may have returned factory defaults
        # because the mouse was offline/asleep. A later online transition must
        # re-read the real config ONCE, but only while there is no user draft.
        self._boot_load_pending = True
        self._stop = threading.Event()

    def snapshot(self) -> dict[str, Any]:
        cat = catalog_payload()
        cat["transport"] = self.transport.describe()
        return {
            "catalog": cat,
            "profile": self.profile.to_dict(),
            "dirty": self.profile.to_dict() != self.applied,
            "sync": {
                "generation": self._sync_generation,
                "message": self._sync_message,
                "failures": self._sync_failures,
            },
        }

    def have_draft(self) -> bool:
        return self.profile.to_dict() != self.applied

    def observe_device(self, *, quick: bool = True) -> bool:
        """Merge device-observed (volatile) state into the CURRENT profile.

        Never discards user-authored draft fields. The hardware DPI-cycle
        button changes the active DPI stage on the device; we mirror just that
        one field so the web selection stays in sync. ALWAYS uses a single
        cheap 0x04 read (a full flash sweep is far too slow for a 2s loop and
        is reserved for the explicit /api/load path). Returns True on success.
        """
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            tr = self.transport
            reader = getattr(tr, "read_active_dpi", None)
            if reader is None:
                self._sync_message = "simulated (no device poll)"
                return False
            idx = reader()
            if idx is None:
                self._sync_failures += 1
                self._sync_message = "device unavailable (retrying)"
                return False
            # First online transition: re-read the full device profile once.
            # read_active_dpi() succeeding means unlock() succeeded (device is
            # awake), so read_profile() reads real flash, not cached defaults.
            # Consume the one-shot on the FIRST successful online tick even when
            # a draft exists: a later tick must never auto-load over user edits.
            if self._boot_load_pending:
                self._boot_load_pending = False
                if not self.have_draft():
                    try:
                        loaded = self.transport.read_profile()
                    except TransportError:
                        loaded = None
                    if loaded is not None:
                        self.profile = loaded
                        self.applied = loaded.to_dict()
                        self._sync_generation += 1
                        self._sync_message = "loaded settings from mouse"
            changed = False
            if self.profile.settings.active_dpi_index != idx:
                self.profile.settings.active_dpi_index = idx
                # Also move the applied baseline so this is not a user edit.
                self.applied = dict(self.applied)
                self.applied["settings"] = dict(self.applied["settings"])
                self.applied["settings"]["active_dpi_index"] = idx
                changed = True
                self._sync_message = f"hardware DPI cycle -> stage {idx + 1}"
            else:
                self._sync_message = "in sync"
            if not self.have_draft():
                self.applied = self.profile.to_dict()
            if changed:
                self._sync_generation += 1
            self._sync_failures = 0
            self._last_sync = time.monotonic()
            return True
        except TransportError as exc:
            self._sync_failures += 1
            self._sync_message = f"device unavailable: {exc}"
            return False
        finally:
            self._sync_lock.release()

    def preview(self) -> dict[str, Any]:
        """Human-readable diff of what Apply will write (delta vs applied)."""
        from . import protocol as P
        baseline = ButtonProfile.from_dict(self.applied)
        records = P.changed_records(baseline, self.profile)
        base_map = {a: v for a, v, _ in P.profile_to_records(baseline)}
        changes = [{
            "label": label,
            "address": "0x%02x" % addr,
            "from": base_map.get(addr, b"").hex(),
            "to": vals.hex(),
        } for addr, vals, label in records]
        return {"count": len(changes), "changes": changes}


def make_handler(state: EditorState):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, fmt: str, *args: Any) -> None:  # quieter
            pass

        def _json(self, obj: Any, status: int = 200) -> None:
            body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass  # client hung up (e.g. tab closed mid-request)

        def _file(self, path: Path) -> None:
            if not path.is_file():
                self.send_error(404)
                return
            types = {".html": "text/html", ".js": "text/javascript",
                     ".css": "text/css", ".svg": "image/svg+xml",
                     ".png": "image/png"}
            body = path.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", types.get(path.suffix, "application/octet-stream") + "; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            try:
                self.wfile.write(body)
            except (BrokenPipeError, ConnectionResetError):
                pass

        def _read_body(self) -> dict[str, Any]:
            length = int(self.headers.get("Content-Length", "0"))
            if not length:
                return {}
            return json.loads(self.rfile.read(length).decode("utf-8"))

        def do_GET(self) -> None:
            p = urlparse(self.path).path
            if p == "/api/state":
                self._json(state.snapshot())
            elif p == "/api/preview":
                self._json(state.preview())
            elif p == "/api/sync":
                self._json(self._sync())
            elif p == "/api/sync-status":
                self._json({
                    "generation": state._sync_generation,
                    "message": state._sync_message,
                    "failures": state._sync_failures,
                    "dirty": state.have_draft(),
                })
            elif p == "/api/status":
                getter = getattr(state.transport, "device_status", None)
                if getter is None:
                    self._json({"connection": "simulated", "online": False})
                else:
                    self._json(getter())
            elif p == "/api/export":
                self._json(state.profile.to_dict())
            elif p == "/":
                self._file(WEB_DIR / "index.html")
            elif p.startswith("/static/"):
                self._file(WEB_DIR / p[len("/static/"):])
            else:
                self.send_error(404)

        def _sync(self) -> dict[str, Any]:
            """Explicit sync (also runs in the background loop).

            Merges device-observed state without clobbering a draft, so a
            hardware DPI cycle shows up even with unsaved edits.
            """
            ok = state.observe_device(quick=True)
            return {"ok": ok, "generation": state._sync_generation,
                    "message": state._sync_message,
                    "profile": state.profile.to_dict(),
                    "dirty": state.have_draft()}

        def do_POST(self) -> None:
            p = urlparse(self.path).path
            try:
                body = self._read_body()
            except json.JSONDecodeError as exc:
                self._json({"error": f"invalid JSON: {exc}"}, 400)
                return
            try:
                if p == "/api/binding":
                    state.profile.set_binding(
                        int(body["buttonId"]), int(body["type"]), int(body["value"])
                    )
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/settings":
                    s = state.profile.settings
                    for key in ("active_dpi_index", "report_rate", "lod_mm", "debounce_ms",
                                "motion_sync", "angle_snapping", "ripple_control",
                                "sensor_mode"):
                        if key in body:
                            setattr(s, key, body[key])
                    if "dpi_stages" in body:
                        s.dpi_stages = [int(v) for v in body["dpi_stages"]]
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/dpi":
                    state.profile.set_dpi_stage(int(body["stage"]), int(body["dpi"]))
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/dpi-active":
                    # Clicking a DPI stage selects it (staged, not applied).
                    idx = int(body["stage"])
                    if not 0 <= idx < len(state.profile.settings.dpi_stages):
                        raise ValueError("DPI stage out of range")
                    state.profile.settings.active_dpi_index = idx
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/dpi-color":
                    rgb = tuple(int(v) for v in body["rgb"])
                    state.profile.set_dpi_color(int(body["stage"]), rgb)
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/discard":
                    # Drop the draft, revert to the last applied/loaded state.
                    state.profile = ButtonProfile.from_dict(state.applied)
                    self._json({"ok": True, "profile": state.profile.to_dict(), "dirty": False})
                elif p == "/api/sync":
                    self._json(self._sync())
                elif p == "/api/validate":
                    self._json({"errors": state.profile.validate()})
                elif p == "/api/apply":
                    state.transport.write_profile(state.profile)
                    state.applied = state.profile.to_dict()
                    self._json({"ok": True, "transport": state.transport.describe(), "dirty": False})
                elif p == "/api/load":
                    loaded = state.transport.read_profile()
                    if loaded is None:
                        self._json({"error": "TRANSPORT_ERROR",
                                    "detail": "Mouse is asleep or disconnected; move the mouse and retry."}, 502)
                        return
                    state.profile = loaded
                    state.applied = loaded.to_dict()
                    self._json({"ok": True, "profile": state.profile.to_dict(), "dirty": False})
                elif p == "/api/import":
                    state.profile = ButtonProfile.from_dict(body)
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/reset":
                    state.profile = ButtonProfile()
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                else:
                    self.send_error(404)
            except (KeyError, ValueError, TypeError) as exc:
                self._json({"error": str(exc)}, 400)
            except TransportError as exc:
                self._json({"error": "TRANSPORT_ERROR", "detail": str(exc)}, 502)

    return Handler


def serve(host: str = "127.0.0.1", port: int = 8765,
          transport: Transport | None = None) -> ThreadingHTTPServer:
    tr = transport or SimulatedTransport()
    state = EditorState(tr)
    class _Server(ThreadingHTTPServer):
        daemon_threads = True
        def server_close(self):
            if getattr(self, "_sync_stop", None):
                self._sync_stop.set()
            super().server_close()
    httpd = _Server((host, port), make_handler(state))
    httpd.editor_state = state  # type: ignore[attr-defined]
    # Server-side background sync: runs even with NO browser tab open, so the
    # web selection follows hardware DPI cycling without pressing Reload.
    MIN_INTERVAL = 2.0   # fast enough to feel live, cheap (single 0x04 read)
    def _loop():
        while not state._stop.wait(MIN_INTERVAL):
            if state._sync_failures:
                state._stop.wait(min(2 ** state._sync_failures, 15))
                if state._stop.is_set():
                    break
            state.observe_device(quick=True)
    bg = threading.Thread(target=_loop, name="helios-sync", daemon=True)
    httpd._sync_thread = bg  # type: ignore[attr-defined]
    httpd._sync_stop = state._stop  # type: ignore[attr-defined]
    bg.start()
    return httpd
