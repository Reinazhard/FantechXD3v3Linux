"""Command-line entry point for the Helios button editor.

Examples
--------
  python -m helios_editor.cli serve                     # web UI, simulated device
  python -m helios_editor.cli serve --real             # web UI, real mouse (writes)
  python -m helios_editor.cli serve --real --dry-run   # real framing, no ioctl
  python -m helios_editor.cli probe                    # read-only status poll
  python -m helios_editor.cli validate profile.json
  python -m helios_editor.cli show [profile.json]
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser
from pathlib import Path

from .model import ButtonProfile
from .transport import HIDTransport, SimulatedTransport, TransportError


def cmd_serve(args: argparse.Namespace) -> int:
    from .server import serve

    transport = (HIDTransport(dry_run=args.dry_run) if args.real
                 else SimulatedTransport())
    httpd = serve(args.host, args.port, transport)
    print(f"Helios button editor on http://{args.host}:{args.port}", flush=True)
    print(f"Backend: {transport.describe()}", flush=True)
    if args.real and not args.dry_run:
        print("NOTE: --real performs confirmed WriteFlashData reports on the device", flush=True)
    # Best-effort browser launch after the socket is listening. Headless hosts
    # simply get a no-op; the server still starts.
    threading.Timer(0.3, lambda: _open_browser(args.host, args.port)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        httpd.server_close()
        tr = getattr(httpd, "editor_state", None)
        if tr and hasattr(tr.transport, "close"):
            tr.transport.close()
    return 0


def _open_browser(host: str, port: int) -> None:
    try:
        webbrowser.open(f"http://{host}:{port}/")
    except Exception:
        pass


def cmd_launch() -> int:
    """Zero-argument path: identical to ``serve --real`` (real HID device)."""
    ns = argparse.Namespace(host="127.0.0.1", port=8765, real=True, dry_run=False)
    return cmd_serve(ns)


def cmd_probe(args: argparse.Namespace) -> int:
    """Read-only status probe; prints the device reply."""
    from .hardware import find_config_hidraw
    from . import protocol as P

    path = find_config_hidraw()
    print(f"config hidraw node: {path}")
    if not path:
        print("device not found", file=sys.stderr)
        return 1
    t = HIDTransport(device_path=path)
    try:
        s = t.poll_status()
        print(f"DeviceOnLine reply: {s.raw.hex()}")
        print(f"mouse link (byte 6): {s.raw[6]}")
        try:
            t.unlock()
            print("startup unlock: OK")
        except TransportError as exc:
            print(f"startup unlock: {exc}")
            return 1
        for addr, name in ((P.ADDR_REPORT_RATE, "report_rate"),
                           (P.ADDR_LOD, "lod"),
                           (P.ADDR_DEBOUNCE, "debounce")):
            try:
                data = t.read_flash(addr, 2)
                print(f"flash[0x{addr:02x}] {name}: value=0x{data[0]:02x} chk=0x{data[1]:02x}")
            except TransportError as exc:
                print(f"flash[0x{addr:02x}] {name}: {exc}")
    finally:
        t.close()
    return 0


def cmd_validate(args: argparse.Namespace) -> int:
    p = ButtonProfile.from_json(Path(args.file).read_text())
    errs = p.validate()
    if errs:
        for e in errs:
            print("ERROR:", e)
        return 1
    print("Profile valid:", len(p.bindings), "bindings")
    return 0


def cmd_show(args: argparse.Namespace) -> int:
    p = ButtonProfile.from_json(Path(args.file).read_text()) if args.file else ButtonProfile()
    print(p.to_json())
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="helios-editor", description=__doc__)
    sub = ap.add_subparsers(dest="cmd")

    s = sub.add_parser("serve", help="run the visual editor web UI")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8765)
    s.add_argument("--real", action="store_true", help="use the real hidraw device")
    s.add_argument("--dry-run", action="store_true", help="build reports but perform no ioctl")
    s.set_defaults(func=cmd_serve)

    p = sub.add_parser("probe", help="read-only device status probe")
    p.set_defaults(func=cmd_probe)

    v = sub.add_parser("validate", help="validate a profile JSON file")
    v.add_argument("file")
    v.set_defaults(func=cmd_validate)

    sh = sub.add_parser("show", help="print a default or given profile as JSON")
    sh.add_argument("file", nargs="?")
    sh.set_defaults(func=cmd_show)

    args = ap.parse_args(argv)
    if args.cmd is None:
        return cmd_launch()
    try:
        return args.func(args)
    except (TransportError, ValueError, FileNotFoundError) as exc:
        print("error:", exc, file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
