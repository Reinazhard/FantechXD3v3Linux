"""Stdlib HTTP server for the Helios button editor.

Exposes a small JSON API over a local socket and serves the static web UI.
No third-party dependencies: runnable with a bare CPython install, which
matches the project's "userspace driver" constraint.

State ownership (authoritative model):
  - ``profile``  : the user-editable draft.
  - ``applied``  : last hardware-confirmed / discard / preview baseline.
  - ``persisted``: last profile confirmed written to device flash.
  ``dirty`` is ``profile != applied``; ``dirty_records()`` is the flash-record
  level diff shared by preview and Apply. The background sync merges only
  device-observed active-DPI changes and never overwrites draft fields.

Lock order (never reverse):
  ``EditorState._sync_lock`` -> ``EditorState._state_lock``
  -> ``HIDTransport._io_lock``.
"""

from __future__ import annotations

import json
import queue
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from . import protocol as P
from .catalog import (
    KEY_FUNCTIONS,
    KEY_FUNCTION_CATEGORIES,
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
    SENSOR_MODES,
)
from .transport import Transport, TransportError, SimulatedTransport
from .protocol import CONFIRMED_DPI_MAX

WEB_DIR = Path(__file__).parent / "web"


class _ValidationError(ValueError):
    """Carries the full error list for a structured VALIDATION_ERROR body."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = errors



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
        "sensorModes": [{"value": k, "label": v} for k, v in SENSOR_MODES.items()],
        "categories": dict(KEY_FUNCTION_CATEGORIES),
        "transport": None,  # filled by handler
    }


class EditorState:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        self._state_lock = threading.RLock()
        self._sync_lock = threading.Lock()
        self._subscribers: set[queue.Queue] = set()

        # Boot: try to show the device's real state; an unplugged/unreadable
        # backend falls back to a default profile so the editor stays usable.
        try:
            loaded = transport.read_profile()
        except TransportError:
            loaded = None
        self.profile = loaded or ButtonProfile()
        from_device = loaded is not None
        self.applied: dict = {}
        self.persisted: dict = {}
        self.baseline_known: bool = False
        self._set_confirmed(self.profile.to_dict(), from_device=from_device)
        self.persisted = dict(self.applied)

        self._catalog = catalog_payload()
        self._catalog["transport"] = self.transport.describe()
        self._capabilities = self.transport.capabilities()
        self._sync_failures = 0
        self._sync_generation = 0        # bumped whenever device state is observed
        self._state_generation = 0       # bumped on every published state patch
        self._sync_message = "starting"
        # At process start read_profile() may have returned factory defaults
        # because the mouse was offline/asleep. A later online transition must
        # re-read the real config ONCE, but only while there is no user draft.
        self._boot_load_pending = True
        self._stop = threading.Event()

    # --- authoritative state helpers (call under _state_lock) ---

    def _set_confirmed(self, profile_dict: dict, *, from_device: bool) -> None:
        """Set the confirmed baseline and keep the transport delta baseline."""
        self.applied = dict(profile_dict)
        self.transport.set_baseline(ButtonProfile.from_dict(profile_dict))
        self.baseline_known = from_device

    def _snapshot_locked(self) -> dict[str, Any]:
        cat = dict(self._catalog)
        return {
            "catalog": cat,
            "profile": self.profile.to_dict(),
            "dirty": self.have_draft(),
            "confirmed": dict(self.applied),
            "persisted": dict(self.persisted),
            "baselineKnown": self.baseline_known,
            "stateGeneration": self._state_generation,
            "capabilities": dict(self._capabilities),
            "dirtyRecords": [
                {"address": "0x%02x" % a, "label": label}
                for a, _v, label in self.dirty_records()
            ],
            "sync": {
                "generation": self._sync_generation,
                "message": self._sync_message,
                "failures": self._sync_failures,
            },
        }

    def snapshot(self) -> dict[str, Any]:
        with self._state_lock:
            return self._snapshot_locked()

    def snapshot_subset(self) -> dict[str, Any]:
        """Compact initial SSE payload (catalog + all authoritative state)."""
        with self._state_lock:
            return self._snapshot_locked()

    def have_draft(self) -> bool:
        return self.profile.to_dict() != self.applied

    def dirty_records(self) -> list:
        baseline = ButtonProfile.from_dict(self.applied)
        return P.changed_records(baseline, self.profile)

    def _mutate_result_locked(self) -> dict[str, Any]:
        return {
            "ok": True,
            "profile": self.profile.to_dict(),
            "dirty": self.have_draft(),
            "dirtyRecords": [
                {"address": "0x%02x" % a, "label": label}
                for a, _v, label in self.dirty_records()
            ],
        }

    def _mutate(self, fn) -> dict[str, Any]:
        """Validate-then-commit a mutation of a deep copy of the draft.

        Invalid input raises ValueError; the live draft is never touched.
        """
        with self._state_lock:
            candidate = ButtonProfile.from_dict(self.profile.to_dict())
            new_profile = fn(candidate)
            if new_profile is not None:
                candidate = new_profile
            errors = candidate.validate()
            if errors:
                raise _ValidationError(errors)
            self.profile = candidate
            result = self._mutate_result_locked()
            self._publish_locked("state", self._state_patch_locked())
            return result

    def _publish_locked(self, event_type: str, payload: dict) -> None:
        for q in list(self._subscribers):
            try:
                q.put_nowait({"type": event_type, "data": payload})
            except queue.Full:
                pass

    def publish(self, event_type: str, payload: dict) -> None:
        with self._state_lock:
            self._publish_locked(event_type, payload)

    def observe_device(self, *, quick: bool = True) -> bool:
        """Merge device-observed (volatile) state into the CURRENT profile.

        Never discards user-authored draft fields. The hardware DPI-cycle
        button changes the active DPI stage on the device; we mirror just that
        one field so the web selection stays in sync. ALWAYS uses a single
        cheap 0x04 read (a full flash sweep is far too slow for a 2s loop).
        Returns True on success.
        """
        if not self._sync_lock.acquire(blocking=False):
            return False
        try:
            tr = self.transport
            reader = getattr(tr, "read_active_dpi", None)
            if reader is None:
                with self._state_lock:
                    self._sync_message = "simulated (no device poll)"
                return False
            idx = reader()
            if idx is None:
                with self._state_lock:
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
                with self._state_lock:
                    if not self.have_draft():
                        try:
                            loaded = self.transport.read_profile()
                        except TransportError:
                            loaded = None
                        if loaded is not None:
                            self.profile = loaded
                            self._set_confirmed(loaded.to_dict(), from_device=True)
                            self._sync_generation += 1
                            self._sync_message = "loaded settings from mouse"
            changed = False
            with self._state_lock:
                new_confirmed = dict(self.applied)
                new_confirmed["settings"] = dict(self.applied["settings"])
                if self.profile.settings.active_dpi_index != idx:
                    self.profile.settings.active_dpi_index = idx
                    changed = True
                    self._sync_message = f"hardware DPI cycle -> stage {idx + 1}"
                else:
                    self._sync_message = "in sync"
                # Hardware wins on the active-DPI field in BOTH draft and
                # confirmed baseline; all other draft fields are untouched.
                new_confirmed["settings"]["active_dpi_index"] = idx
                self._set_confirmed(new_confirmed,
                                    from_device=self.baseline_known)
                if changed:
                    self._sync_generation += 1
                self._sync_failures = 0
                result_state = self._state_patch_locked()
            if changed:
                self.publish("state", result_state)
            return True
        except TransportError as exc:
            with self._state_lock:
                self._sync_failures += 1
                self._sync_message = f"device unavailable: {exc}"
            return False
        finally:
            self._sync_lock.release()

    def _state_patch_locked(self) -> dict[str, Any]:
        self._state_generation += 1
        return {
            "stateGeneration": self._state_generation,
            "generation": self._sync_generation,
            "dirty": self.have_draft(),
            "confirmed": dict(self.applied),
            "persisted": dict(self.persisted),
            "profile": self.profile.to_dict(),
            "dirtyRecords": [
                {"address": "0x%02x" % a, "label": label}
                for a, _v, label in self.dirty_records()
            ],
        }

    def preview(self) -> dict[str, Any]:
        """Human-readable diff of what Apply will write (delta vs applied)."""
        with self._state_lock:
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

        def _error(self, code: str, detail: str, status: int, **extra) -> None:
            body: dict[str, Any] = {"error": code, "detail": detail}
            body.update(extra)
            self._json(body, status)

        def _file(self, path: Path) -> None:
            resolved = path.resolve()
            if (not resolved.is_relative_to(WEB_DIR.resolve())
                    or not resolved.is_file()):
                self._error("NOT_FOUND", "unknown path", 404)
                return
            types = {".html": "text/html", ".js": "text/javascript",
                     ".css": "text/css", ".svg": "image/svg+xml",
                     ".png": "image/png"}
            body = resolved.read_bytes()
            self.send_response(200)
            self.send_header("Content-Type", types.get(resolved.suffix, "application/octet-stream") + "; charset=utf-8")
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

        def _sse_frame(self, event: str, data: Any) -> None:
            payload = json.dumps(data, ensure_ascii=False)
            self.wfile.write(
                f"event: {event}\ndata: {payload}\n\n".encode("utf-8"))
            self.wfile.flush()

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
                self._json(state.transport.device_status())
            elif p == "/api/export":
                with state._state_lock:
                    self._json(state.profile.to_dict())
            elif p == "/api/events":
                self._events()
            elif p == "/":
                self._file(WEB_DIR / "index.html")
            elif p.startswith("/static/"):
                self._file(WEB_DIR / p[len("/static/"):])
            else:
                self._error("NOT_FOUND", "unknown path", 404)

        def _events(self) -> None:
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Connection", "keep-alive")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()
            q: queue.Queue = queue.Queue(maxsize=64)
            with state._state_lock:
                state._subscribers.add(q)
                initial = state.snapshot_subset()
            try:
                self._sse_frame("state", initial)
                while True:
                    try:
                        item = q.get(timeout=15)
                    except queue.Empty:
                        self.wfile.write(b": keepalive\n\n")
                        self.wfile.flush()
                        continue
                    self._sse_frame(item["type"], item["data"])
            except (BrokenPipeError, ConnectionResetError, OSError):
                pass
            finally:
                with state._state_lock:
                    state._subscribers.discard(q)

        def _sync(self) -> dict[str, Any]:
            """Explicit sync (also runs in the background loop)."""
            ok = state.observe_device(quick=True)
            with state._state_lock:
                return {"ok": ok, "generation": state._sync_generation,
                        "message": state._sync_message,
                        "profile": state.profile.to_dict(),
                        "dirty": state.have_draft()}

        def _mutation_response(self, result: dict) -> None:
            self._json(result)

        def do_POST(self) -> None:
            p = urlparse(self.path).path
            try:
                body = self._read_body()
            except json.JSONDecodeError as exc:
                self._error("INVALID_JSON", f"invalid JSON: {exc}", 400)
                return
            try:
                if p == "/api/binding":
                    def _b(c):
                        c.set_binding(int(body["buttonId"]), int(body["type"]),
                                      int(body["value"]))
                    self._mutation_response(state._mutate(_b))
                elif p == "/api/settings":
                    def _s(c):
                        if "report_rate" in body and \
                                int(body["report_rate"]) not in state._capabilities["reportRates"]:
                            raise ValueError("polling rate not supported by this device")
                        for key in ("active_dpi_index", "report_rate", "lod_mm",
                                    "debounce_ms", "motion_sync", "angle_snapping",
                                    "ripple_control", "sensor_mode"):
                            if key in body:
                                setattr(c.settings, key, body[key])
                        if "dpi_stages" in body:
                            c.settings.dpi_stages = [int(v) for v in body["dpi_stages"]]
                    self._mutation_response(state._mutate(_s))
                elif p == "/api/dpi":
                    stage, dpi = int(body["stage"]), int(body["dpi"])
                    self._mutation_response(state._mutate(lambda c: c.set_dpi_stage(stage, dpi)))
                elif p == "/api/dpi-active":
                    idx = int(body["stage"])
                    def _da(c):
                        if not 0 <= idx < len(c.settings.dpi_stages):
                            raise ValueError("DPI stage out of range")
                        c.settings.active_dpi_index = idx
                    self._mutation_response(state._mutate(_da))
                elif p == "/api/dpi-color":
                    rgb = tuple(int(v) for v in body["rgb"])
                    stage = int(body["stage"])
                    self._mutation_response(
                        state._mutate(lambda c: c.set_dpi_color(stage, rgb)))
                elif p == "/api/discard":
                    with state._state_lock:
                        state.profile = ButtonProfile.from_dict(state.applied)
                        result = {"ok": True, "profile": state.profile.to_dict(),
                                  "dirty": False, "dirtyRecords": []}
                        state._publish_locked("state", state._state_patch_locked()
                                              | {"ok": True, "dirtyRecords": []})
                    self._json(result)
                elif p == "/api/sync":
                    self._json(self._sync())
                elif p == "/api/validate":
                    with state._state_lock:
                        self._json({"errors": state.profile.validate()})
                elif p == "/api/apply":
                    self._apply()
                elif p == "/api/load":
                    loaded = state.transport.read_profile()
                    if loaded is None:
                        self._error("TRANSPORT_ERROR",
                                    "Mouse is asleep or disconnected; move the mouse and retry.",
                                    502)
                        return
                    with state._state_lock:
                        state.profile = loaded
                        state._set_confirmed(loaded.to_dict(), from_device=True)
                        state.persisted = dict(state.applied)
                        result = {"ok": True, "profile": state.profile.to_dict(),
                                  "dirty": False, "dirtyRecords": []}
                        state._publish_locked("state", state._state_patch_locked()
                                              | {"ok": True, "dirtyRecords": []})
                    self._json(result)
                elif p == "/api/import":
                    self._mutation_response(
                        state._mutate(lambda c: ButtonProfile.from_dict(body)))
                elif p == "/api/reset":
                    self._mutation_response(
                        state._mutate(lambda c: ButtonProfile()))
                else:
                    self._error("NOT_FOUND", "unknown path", 404)
            except _ValidationError as exc:
                self._error("VALIDATION_ERROR", str(exc), 400, errors=exc.errors)
            except (KeyError, ValueError, TypeError) as exc:
                self._error("VALIDATION_ERROR", str(exc), 400)
            except TransportError as exc:
                self._error("TRANSPORT_ERROR", str(exc), 502)

        def _apply(self) -> None:
            with state._state_lock:
                records = state.dirty_records()
                baseline = (ButtonProfile.from_dict(state.applied)
                            if state.baseline_known else None)
                if not records:
                    self._json({"ok": True, "transport": state.transport.describe(),
                                "dirty": False,
                                "verification": state.transport.verification_mode,
                                "written": 0})
                    return
                try:
                    state.transport.write_profile(state.profile, baseline=baseline)
                except TransportError as exc:
                    self._error("TRANSPORT_ERROR", str(exc), 502)
                    return
                state._set_confirmed(state.profile.to_dict(), from_device=True)
                state.persisted = dict(state.applied)
                result = {"ok": True, "transport": state.transport.describe(),
                          "dirty": False,
                          "verification": state.transport.verification_mode,
                          "written": len(records)}
                state._publish_locked("state", state._state_patch_locked() | result)
                self._json(result)

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
