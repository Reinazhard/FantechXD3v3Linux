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
        "transport": None,  # filled by handler
    }


class EditorState:
    def __init__(self, transport: Transport) -> None:
        self.transport = transport
        # Try to show the device's real state; an unplugged/unreadable backend
        # falls back to a default profile so the editor stays usable.
        try:
            self.profile = transport.read_profile()
        except TransportError:
            self.profile = ButtonProfile()
        self.applied = self.profile.to_dict()

    def snapshot(self) -> dict[str, Any]:
        cat = catalog_payload()
        cat["transport"] = self.transport.describe()
        return {
            "catalog": cat,
            "profile": self.profile.to_dict(),
            "dirty": self.profile.to_dict() != self.applied,
        }


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
            elif p == "/assets/mouse.svg":
                self._file(WEB_DIR / "mouse.svg")
            else:
                self.send_error(404)

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
                                "motion_sync", "angle_snapping", "ripple_control"):
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
                elif p == "/api/dpi-color":
                    rgb = tuple(int(v) for v in body["rgb"])
                    state.profile.set_dpi_color(int(body["stage"]), rgb)
                    self._json({"ok": True, "profile": state.profile.to_dict(),
                                "dirty": state.profile.to_dict() != state.applied})
                elif p == "/api/validate":
                    self._json({"errors": state.profile.validate()})
                elif p == "/api/apply":
                    state.transport.write_profile(state.profile)
                    state.applied = state.profile.to_dict()
                    self._json({"ok": True, "transport": state.transport.describe(), "dirty": False})
                elif p == "/api/load":
                    state.profile = state.transport.read_profile()
                    state.applied = state.profile.to_dict()
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
    httpd = ThreadingHTTPServer((host, port), make_handler(state))
    httpd.editor_state = state  # type: ignore[attr-defined]
    return httpd
