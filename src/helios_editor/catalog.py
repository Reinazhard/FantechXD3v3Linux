"""Mouse-button function catalog for the Helios II XD3v3 editor.

The type/param values below are transcribed from the vendor software shipped in
Helios_II_XD3V3_Software_V1.0_23121900.setup.exe. See docs/EVIDENCE.md for the
source files and the exact lines each value comes from.

A binding is the on-device ``KeyFunMap`` triple ``(type, param1, param2)``.
``type`` is a DriverLib.KEY_CLASS value; ``param1``/``param2`` are the sub-function
value split into two bytes (low, high).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class KeyFunction:
    """One selectable top-level button function (XML <KeyFunction> <name>)."""

    type: int
    param: int
    pull: int
    label: str


@dataclass(frozen=True)
class SubFunction:
    """One selectable sub-function (XML <SubKeyFunction> <name>)."""

    type: int
    value: int
    label: str


# DriverLib.KEY_CLASS enum values (decoded from .NET Constant table).
KC_CLOSE = 0x00
KC_MOUSE = 0x01
KC_DPI = 0x02
KC_ACPAN = 0x03
KC_FIRE = 0x04
KC_SHORTCUT = 0x05
KC_MACRO = 0x06
KC_REPORT_RATE = 0x07
KC_LIGHT = 0x08
KC_CONFIG = 0x09
KC_DPI_LOCK = 0x0A

# Human-readable grouping of KEY_CLASS values for the WebUI filter. Values
# mirror the exact labels previously hardcoded in app.js categoryOf().
KEY_FUNCTION_CATEGORIES: dict[int, str] = {
    0x00: "Disabled",
    0x01: "Mouse functions",
    0x02: "Special functions",
    0x0A: "Special functions",
    0x08: "Special functions",
    0x03: "System functions",
    0x0B: "System functions",
    0x05: "Multimedia",
    0x04: "Keyboard functions",
    0x06: "Keyboard functions",
    0x07: "Keyboard functions",
}

# <KeyFunction> catalog, English.xml lines 227-241. ``value`` is stored in
# ``param``; ``pull="01"`` means the function is a pull-down (sub-function) list.
KEY_FUNCTIONS: tuple[KeyFunction, ...] = (
    KeyFunction(0x01, 0x0001, 0x00, "Left Button"),
    KeyFunction(0x01, 0x0002, 0x00, "Right Button"),
    KeyFunction(0x01, 0x0004, 0x00, "Middle Button"),
    KeyFunction(0x01, 0x0008, 0x00, "Back"),
    KeyFunction(0x01, 0x0010, 0x00, "Forward"),
    KeyFunction(0x07, 0x0000, 0x00, "Polling Rate Switch"),
    KeyFunction(0x04, 0x0000, 0x00, "Fire key"),
    KeyFunction(0x05, 0x0000, 0x00, "Combo Key"),
    KeyFunction(0x00, 0x0000, 0x00, "Disabled"),
    KeyFunction(0x0B, 0x0000, 0x01, "Scroll Up/Down"),
    KeyFunction(0x03, 0x0000, 0x01, "Scroll Left/Right"),
    KeyFunction(0x02, 0x0000, 0x01, "DPI Switch"),
    KeyFunction(0x05, 0x0000, 0x01, "Media Key"),
    KeyFunction(0x0A, 0x0000, 0x01, "DPI Lock"),
    KeyFunction(0x06, 0x0000, 0x01, "Macro"),
)

# <SubKeyFunction> catalog (English.xml). Kept as a flat list so callers can
# filter by ``type``. DPI Lock values are the raw DPI/10 codes from the XML.
SUB_FUNCTIONS: tuple[SubFunction, ...] = (
    SubFunction(0x02, 0x0001, "DPI Loop"),
    SubFunction(0x02, 0x0002, "DPI+"),
    SubFunction(0x02, 0x0003, "DPI-"),
    SubFunction(0x03, 0x0001, "Scroll Left"),
    SubFunction(0x03, 0x0002, "Scroll Right"),
    SubFunction(0x0B, 0x0001, "Scroll Up"),
    SubFunction(0x0B, 0x0002, "Scroll Down"),
    SubFunction(0x05, 0x0183, "Media Player"),
    SubFunction(0x05, 0x00CD, "Play/Pause"),
    SubFunction(0x05, 0x00B5, "Next"),
    SubFunction(0x05, 0x00B6, "Previous"),
    SubFunction(0x05, 0x00B7, "Stop"),
    SubFunction(0x05, 0x00E2, "Mute"),
    SubFunction(0x05, 0x00E9, "Volume Up"),
    SubFunction(0x05, 0x00EA, "Volume Down"),
    SubFunction(0x05, 0x018A, "Email"),
    SubFunction(0x05, 0x0192, "Calculator"),
    SubFunction(0x05, 0x0194, "My Computer"),
    SubFunction(0x05, 0x0223, "Home Page"),
    SubFunction(0x05, 0x0221, "Search"),
    SubFunction(0x05, 0x0225, "Web Forward"),
    SubFunction(0x05, 0x0224, "Web Back"),
    SubFunction(0x05, 0x0226, "Stop"),
    SubFunction(0x05, 0x0227, "Refresh"),
    SubFunction(0x05, 0x022A, "Favorites"),
    SubFunction(0x08, 0x0000, "All Lights On/Off"),
    SubFunction(0x08, 0x0003, "Light Strip On/Off"),
    SubFunction(0x08, 0x0004, "Loop switch light effects"),
    SubFunction(0x0A, 0x0001, "100"),
    SubFunction(0x0A, 0x0003, "200"),
    SubFunction(0x0A, 0x0005, "300"),
    SubFunction(0x0A, 0x0007, "400"),
    SubFunction(0x0A, 0x0009, "500"),
    SubFunction(0x0A, 0x000B, "600"),
    SubFunction(0x0A, 0x000D, "700"),
    SubFunction(0x0A, 0x000F, "800"),
    SubFunction(0x0A, 0x0011, "900"),
    SubFunction(0x0A, 0x0013, "1000"),
    SubFunction(0x0A, 0x0015, "1100"),
    SubFunction(0x0A, 0x0017, "1200"),
)

# DriverLib.REPORT_RATE enum.
REPORT_RATES: dict[int, str] = {
    1: "1000 Hz",
    2: "500 Hz",
    4: "250 Hz",
    8: "125 Hz",
    16: "2000 Hz",
    32: "4000 Hz",
}

def sub_functions_for(kf: KeyFunction) -> list[SubFunction]:
    """Return the sub-function choices for a pull-down key function."""
    if kf.pull != 0x01:
        return []
    # Scroll Up/Down in XML uses type 0x0B; DPI Switch 0x02; Media 0x05; etc.
    # Pull-down Media Key and Combo Key both use type 0x05: media entries only.
    return [s for s in SUB_FUNCTIONS if s.type == kf.type]


def selection_to_bytes(type_: int, value: int) -> tuple[int, int]:
    """Split a 16-bit sub-function value into the KeyFunMap param bytes."""
    return (value & 0xFF, (value >> 8) & 0xFF)
