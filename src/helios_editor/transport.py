"""Device transports for applying a button profile to the mouse.

The wire protocol is now CONFIRMED from the USBPcap captures in ``captures/``
and is implemented in :mod:`helios_editor.protocol` (17-byte HID feature
report, 0x55-seeded checksum, 16-bit big-endian flash address, WriteFlashData
records). The real hardware transport lives in :mod:`helios_editor.hardware`
and is re-exported here for a single import location.

``SimulatedTransport`` remains the in-memory device for the editor UI and
tests. It never touches hardware.
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from .model import ButtonProfile


class TransportError(RuntimeError):
    """Transport is present but the device rejected or failed the operation."""


class Transport(ABC):
    """Minimal contract the editor needs from a device backend."""

    verification_mode = "simulated"

    @abstractmethod
    def read_profile(self) -> ButtonProfile | None:
        """Load the profile currently stored on the device.

        Returns None when the device is unreachable; callers must not treat
        that as a real (default) profile.
        """

    @abstractmethod
    def write_profile(self, profile: ButtonProfile, *,
                      baseline: ButtonProfile | None = None) -> None:
        """Persist a profile to device flash, optionally with a delta baseline."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable backend identity."""

    def device_status(self) -> dict:
        """Live connection + battery info; simulated backends report honestly."""
        return {
            "connection": "simulated", "online": False,
            "battery_percent": None, "battery_mv": None,
            "charging": None, "hidraw": None,
            "note": "Simulated device; no hardware access.",
        }

    def capabilities(self) -> dict:
        """Hardware capability flags for the WebUI.

        ``reportRates`` lists the REPORT_RATE enum values the connected device
        can use. Simulated/dry-run backends expose every catalog rate; real
        hardware narrows this from the detected PID (see HIDTransport).
        """
        from .catalog import REPORT_RATES
        return {
            "reportRates": list(REPORT_RATES.keys()),
            "rateSource": "catalog",
        }

    def set_baseline(self, profile: ButtonProfile) -> None:
        """Align an optional delta-write baseline with confirmed state."""
        return None


class SimulatedTransport(Transport):
    """In-memory stand-in for a device. Used by the web editor and tests."""

    def __init__(self, initial: ButtonProfile | None = None) -> None:
        self._profile = initial or ButtonProfile()
        self.writes = 0

    def read_profile(self) -> ButtonProfile:
        return ButtonProfile.from_dict(self._profile.to_dict())

    def write_profile(self, profile: ButtonProfile, *,
                      baseline: ButtonProfile | None = None) -> None:
        errors = profile.validate()
        if errors:
            raise TransportError("invalid profile: " + "; ".join(errors))
        self._profile = ButtonProfile.from_dict(profile.to_dict())
        self.writes += 1

    def describe(self) -> str:
        return f"SimulatedTransport(writes={self.writes})"


# Real hardware transport (hidraw ioctl). Imported lazily-style at module level
__all__ = [
    "Transport", "TransportError",
    "SimulatedTransport", "HIDTransport",
]


def __getattr__(name: str):  # lazy: hardware.py imports TransportError from here
    if name == "HIDTransport":
        from .hardware import HIDTransport
        return HIDTransport
    raise AttributeError(name)
