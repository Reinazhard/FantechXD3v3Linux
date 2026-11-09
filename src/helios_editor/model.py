"""Device layout and configuration model for the Helios II XD3v3 mouse editor.

HARDWARE CONTRACT (corrected from vendor evidence, 2026-11-09)
-------------------------------------------------------------
The XD3v3 has **6 physical buttons**, not 16. Config.ini ``KeyParam1..6`` are
the only hotspots on the mouse body (x < 340); ``KeyParam7..16`` are at x=384
(right-hand function-list column) or y=266 (bottom strip) = the vendor's
software assignment list, not hardware. The shipped ``设备1.png`` (432x356)
shows exactly 6 buttons: L/R click, scroll wheel, two right-side macro buttons,
and a bottom DPI-cycle button.

The on-device ``FlashDataMap`` layout was recovered from a full cmd-8 flash
sweep (dpi-read-response.pcapng, reconstructed 0x00..0x7f)::

    0x00          report-rate enum (2-byte value+checksum record)
    0x02          DPI stage count   (DPIMaxGrade = 4)
    0x04          active DPI stage index
    0x0a          LOD
    0x0c + 4*i    DPI stage registers (8 slots physically, 4 user-facing)
    0x2c + 4*i    per-stage LED RGB colors (3 bytes + checksum)
    0x60 + 4*i    6 button KeyFunMap records (type,p1,p2,checksum)

DPI stages are limited to **4** by ``DPIMaxGrade=4`` and the vendor's
``customComboBox_DPIGrade``. The DPI slider uses ``DPIRange=50,26000,50,...``
(50..26000 step 50). Each stage has a unique LED color from ``DPIColor``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
import json


@dataclass(frozen=True)
class Hotspot:
    """A physical button on the mouse diagram."""

    id: int
    x: int
    y: int
    # Default KeyFunMap (type, param) from Config.ini KeyParamN / flash 0x60+4*i.
    default_type: int
    default_param: int
    label: str
    flash_addr: int  # confirmed flash address of this button's KeyFunMap
    fixed: bool = False  # button 1 (Left) must stay bound to Left Click


# 6 physical buttons. Coordinates are on the 432x356 vendor diagram.
HOTSPOTS: tuple[Hotspot, ...] = (
    Hotspot(1, 127, 160, 0x01, 0x0001, "Left Click", 0x60, fixed=True),
    Hotspot(2, 31, 154, 0x01, 0x0002, "Right Click", 0x64),
    Hotspot(3, 100, 124, 0x01, 0x0004, "Middle / Scroll", 0x68),
    Hotspot(4, 70, 150, 0x01, 0x0008, "Macro 1 · left (Back)", 0x6C),
    Hotspot(5, 70, 190, 0x01, 0x0010, "Macro 2 · left (Forward)", 0x70),
    Hotspot(6, 216, 300, 0x02, 0x0001, "DPI Cycle (bottom)", 0x74),
)

DIAGRAM_WIDTH = 432
DIAGRAM_HEIGHT = 356

# DPI: Config.ini DPIMaxGrade=4, DPIRange=50,26000,50,...
MAX_DPI_STAGES = 4
DPI_MIN, DPI_MAX, DPI_STEP = 50, 26000, 50
DEFAULT_DPI_STAGES: tuple[int, ...] = (400, 800, 1600, 3200)
DEFAULT_DPI_INDEX = 2  # DefaultDPI=2 (1-based -> 1600 DPI)

# Config.ini DPIColor first 4 entries: red, blue, green, magenta. The vendor's
# 14-swatch palette (res/5灯光界面/颜色N.png) supplies the rest of the choices.
DEFAULT_DPI_COLORS: tuple[tuple[int, int, int], ...] = (
    (255, 0, 0), (0, 0, 255), (0, 255, 0), (255, 0, 255),
)
LED_PALETTE: tuple[tuple[int, int, int], ...] = (
    (255, 0, 0), (0, 255, 255), (0, 255, 0), (255, 0, 255),
    (255, 255, 0), (255, 80, 0), (0, 0, 255), (227, 0, 127),
    (46, 166, 223), (96, 25, 133), (255, 255, 255), (139, 178, 36),
    (255, 155, 51), (208, 113, 173),
)

DEFAULT_DEBOUNCE_MS = 8
DEFAULT_REPORT_RATE = 1  # 1000 Hz (REPORT_RATE.R_1000)
LOD_CHOICES = {1: "1 mm", 2: "2 mm"}

# BatteryParam voltage curve from Config.ini (mV at 0%, then 5% steps-ish).
BATTERY_VOLTAGE_CURVE: tuple[int, ...] = (
    3050, 3420, 3480, 3540, 3600, 3660, 3720, 3760, 3800, 3840, 3880,
    3920, 3940, 3960, 3980, 4000, 4020, 4040, 4060, 4080, 4110,
)


def voltage_to_percent(mv: int) -> int:
    """Map a battery voltage to a percentage using the vendor's curve."""
    c = BATTERY_VOLTAGE_CURVE
    if mv <= c[0]:
        return 0
    for i in range(len(c) - 1):
        if c[i] <= mv <= c[i + 1]:
            frac = (mv - c[i]) / (c[i + 1] - c[i])
            return round((i + frac) / (len(c) - 1) * 100)
    return 100


@dataclass(frozen=True)
class Binding:
    """One on-device KeyFunMap entry: (type, param1, param2)."""

    type: int
    param1: int
    param2: int
    value: int  # convenience: param1 | param2<<8

    @classmethod
    def from_value(cls, type_: int, value: int) -> "Binding":
        return cls(type_, value & 0xFF, (value >> 8) & 0xFF, value)

    def describe(self) -> str:
        from .catalog import KEY_FUNCTIONS, SUB_FUNCTIONS

        for kf in KEY_FUNCTIONS:
            if kf.type == self.type and kf.param == self.value:
                return kf.label
        for sf in SUB_FUNCTIONS:
            if sf.type == self.type and sf.value == self.value:
                return sf.label
        return f"type=0x{self.type:02X} value=0x{self.value:04X}"


@dataclass
class MouseSettings:
    """Scalar settings from DriverLib.MouseConfig / DPIConfig / LOD."""

    dpi_stages: list[int] = field(default_factory=lambda: list(DEFAULT_DPI_STAGES))
    dpi_colors: list[tuple[int, int, int]] = field(
        default_factory=lambda: [tuple(c) for c in DEFAULT_DPI_COLORS]
    )
    active_dpi_index: int = DEFAULT_DPI_INDEX
    report_rate: int = DEFAULT_REPORT_RATE
    lod_mm: int = 1
    debounce_ms: int = DEFAULT_DEBOUNCE_MS
    motion_sync: bool = False
    angle_snapping: bool = False
    ripple_control: bool = False
    # customComboBox_ModeSelect: 0000=LP, 0001=HP (0010=Corded, not used here)
    sensor_mode: int = 0   # 0 = LP, 1 = HP

    def __post_init__(self) -> None:
        # Enforce the 4-stage hardware limit; pad/trim defensively on import.
        self.dpi_stages = (list(self.dpi_stages) + list(DEFAULT_DPI_STAGES))[:MAX_DPI_STAGES]
        self.dpi_colors = (list(self.dpi_colors) + list(DEFAULT_DPI_COLORS))[:len(self.dpi_stages)]
        self.dpi_colors = [tuple(int(v) & 0xFF for v in c[:3]) for c in self.dpi_colors]
        if not 0 <= self.active_dpi_index < len(self.dpi_stages):
            self.active_dpi_index = 0
        if self.sensor_mode not in (0, 1):
            self.sensor_mode = 0

    def asdict(self) -> dict:
        d = asdict(self)
        d["dpi_colors"] = [list(c) for c in self.dpi_colors]
        return d


@dataclass
class ButtonProfile:
    """A complete editable profile (the editor's unit of work)."""

    name: str = "Profile 1"
    bindings: dict[int, Binding] = field(default_factory=dict)
    settings: MouseSettings = field(default_factory=MouseSettings)

    def __post_init__(self) -> None:
        if not self.bindings:
            self.bindings = {
                h.id: Binding.from_value(h.default_type, h.default_param)
                for h in HOTSPOTS
            }

    # --- validation (mirrors the vendor app's guard rails) ---

    def validate(self) -> list[str]:
        errors: list[str] = []
        left = self.bindings.get(1)
        if left is None or left.type != 0x01 or left.value != 0x0001:
            # English.xml <ReserveLeftKey>Must keep Left key</ReserveLeftKey>
            errors.append("Button 1 (Left Click) must stay bound to Left Button")
        for h in HOTSPOTS:
            b = self.bindings.get(h.id)
            if b is None:
                errors.append(f"Button {h.id}: missing binding")
            elif not (0 <= b.type <= 0xFF and 0 <= b.param1 <= 0xFF and 0 <= b.param2 <= 0xFF):
                errors.append(f"Button {h.id}: byte out of range")
        s = self.settings
        if len(s.dpi_stages) > MAX_DPI_STAGES:
            errors.append(f"at most {MAX_DPI_STAGES} DPI stages supported")
        for i, dpi in enumerate(s.dpi_stages):
            if not (DPI_MIN <= dpi <= DPI_MAX) or (dpi - DPI_MIN) % DPI_STEP:
                errors.append(f"DPI stage {i + 1}: {dpi} is outside {DPI_MIN}..{DPI_MAX}/{DPI_STEP}")
        if not (0 <= s.active_dpi_index < len(s.dpi_stages)):
            errors.append("active_dpi_index: out of range")
        colors = [tuple(c) for c in s.dpi_colors]
        if len(colors) != len(s.dpi_stages):
            errors.append("each DPI stage needs one LED color")
        elif len(set(colors)) != len(colors):
            errors.append("each DPI stage needs a UNIQUE LED color")
        for c in colors:
            if any(not (0 <= v <= 255) for v in c):
                errors.append("LED color channel out of 0..255")
        if s.report_rate not in (1, 2, 4, 8, 16, 32):
            errors.append("report_rate: invalid REPORT_RATE enum")
        if s.lod_mm not in LOD_CHOICES:
            errors.append("lod_mm: must be 1 or 2")
        if not (0 <= s.debounce_ms <= 20):
            errors.append("debounce_ms: must be 0..20")
        if s.sensor_mode not in (0, 1):
            errors.append("sensor_mode: must be 0 (LP) or 1 (HP)")
        return errors

    def set_binding(self, button_id: int, type_: int, value: int) -> None:
        h = next((h for h in HOTSPOTS if h.id == button_id), None)
        if h is None:
            raise ValueError(f"unknown button id {button_id}")
        if h.fixed and not (type_ == 0x01 and value == 0x0001):
            raise ValueError("Left Click cannot be rebound (vendor guard rail)")
        self.bindings[button_id] = Binding.from_value(type_, value)

    def set_dpi_stage(self, index: int, dpi: int) -> None:
        if not (0 <= index < MAX_DPI_STAGES):
            raise ValueError(f"DPI stage must be 1..{MAX_DPI_STAGES}")
        if not (DPI_MIN <= dpi <= DPI_MAX) or (dpi - DPI_MIN) % DPI_STEP:
            raise ValueError(f"DPI must be {DPI_MIN}..{DPI_MAX} step {DPI_STEP}")
        self.settings.dpi_stages[index] = dpi

    def set_dpi_color(self, index: int, rgb: tuple[int, int, int]) -> None:
        if not (0 <= index < MAX_DPI_STAGES):
            raise ValueError(f"DPI stage must be 1..{MAX_DPI_STAGES}")
        self.settings.dpi_colors[index] = tuple(int(v) & 0xFF for v in rgb[:3])

    def to_keys_bytes(self) -> bytes:
        """Serialize the 6 KeyFunMap entries as consecutive 3-byte records."""
        out = bytearray()
        for h in HOTSPOTS:
            b = self.bindings[h.id]
            out += bytes((b.type & 0xFF, b.param1 & 0xFF, b.param2 & 0xFF))
        return bytes(out)

    # --- persistence ---

    def to_dict(self) -> dict:
        return {
            "format": "helios-button-profile",
            "version": 2,
            "name": self.name,
            "settings": self.settings.asdict(),
            "bindings": {
                str(k): {"type": b.type, "param1": b.param1, "param2": b.param2}
                for k, b in sorted(self.bindings.items())
            },
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), indent=2, ensure_ascii=False)

    @classmethod
    def from_dict(cls, data: dict) -> "ButtonProfile":
        if data.get("format") != "helios-button-profile":
            raise ValueError("unsupported profile format")
        ver = data.get("version", 1)
        if ver not in (1, 2):
            raise ValueError("unsupported profile version")
        s = data.get("settings", {})
        settings = MouseSettings(
            dpi_stages=s.get("dpi_stages", list(DEFAULT_DPI_STAGES)),
            dpi_colors=[tuple(c) for c in s.get("dpi_colors", DEFAULT_DPI_COLORS)],
            active_dpi_index=s.get("active_dpi_index", DEFAULT_DPI_INDEX),
            report_rate=s.get("report_rate", DEFAULT_REPORT_RATE),
            lod_mm=s.get("lod_mm", 1),
            debounce_ms=s.get("debounce_ms", DEFAULT_DEBOUNCE_MS),
            motion_sync=s.get("motion_sync", False),
            angle_snapping=s.get("angle_snapping", False),
            ripple_control=s.get("ripple_control", False),
            sensor_mode=s.get("sensor_mode", 0),
        )
        bindings = {
            int(k): Binding(v["type"], v["param1"], v["param2"],
                            v["param1"] | (v["param2"] << 8))
            for k, v in data.get("bindings", {}).items()
            if int(k) <= len(HOTSPOTS)  # drop the old 16-button list on v1 import
        }
        return cls(name=data.get("name", "Profile 1"), bindings=bindings, settings=settings)

    @classmethod
    def from_json(cls, text: str) -> "ButtonProfile":
        return cls.from_dict(json.loads(text))


def default_profile() -> ButtonProfile:
    return ButtonProfile()
