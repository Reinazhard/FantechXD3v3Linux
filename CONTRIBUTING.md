# Contributing

Thanks for helping improve this project.

## Ground rules

* **Evidence first.** Protocol changes MUST be backed by a USB capture or a
  documented static-analysis finding. Never invent wire bytes. Update
  `docs/PROTOCOL.md` / `docs/HARDWARE.md` and add a fixture test for any new
  frame or field.
* **No vendor artifacts.** Do not commit vendor executables, DLLs, images,
  firmware, or USBPcap captures. Captures are gitignored; see
  `docs/CAPTURE_GUIDE.md` to record your own.
* **Stdlib only** at runtime. The package must remain installable without
  third-party dependencies. `pytest` is a dev-only dependency.
* **Python ≥ 3.10.** Keep `X | Y` and builtin generics; do not add syntax that
  breaks the floor declared in `pyproject.toml`.

## Development setup

```bash
git clone https://github.com/Reinazhard/FantechXD3v3Linux
cd FantechXD3v3Linux
python -m venv .venv && . .venv/bin/activate
python -m pip install -e . pytest
PYTHONPATH=src python -m pytest tests/ -q
```

## Before opening a PR

1. `PYTHONPATH=src python -m pytest tests/ -q` passes (no hardware needed).
2. `python -m build` produces a clean wheel/sdist (optional).
3. If you changed the UI, verify it in a real browser on the simulated
   backend; for hardware changes, do a **read → write → read-back** on the
   device and restore defaults afterwards.
4. Update `CHANGELOG.md` and the relevant docs.
5. Keep commit messages in Conventional Commits form, e.g.
   `fix(transport): pump all pending echo reports`.

## Reporting a device that isn't detected

Include `lsusb`, `ls -l /dev/hidraw*`, the output of
`helios-editor probe`, and the relevant `dmesg` lines. Do not paste private
data from USB captures.

## Legal

By contributing you agree your work is licensed under GPL-3.0-or-later and
that you have the right to submit it. See [LICENSE](LICENSE) and [NOTICE](NOTICE).
