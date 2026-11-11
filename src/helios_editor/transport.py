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

    @abstractmethod
    def read_profile(self) -> ButtonProfile | None:
        """Load the profile currently stored on the device.

        Returns None when the device is unreachable; callers must not treat
        that as a real (default) profile.
        """

    @abstractmethod
    def write_profile(self, profile: ButtonProfile) -> None:
        """Persist a profile to device flash."""

    @abstractmethod
    def describe(self) -> str:
        """Human-readable backend identity."""


class SimulatedTransport(Transport):
    """In-memory stand-in for a device. Used by the web editor and tests."""

    def __init__(self, initial: ButtonProfile | None = None) -> None:
        self._profile = initial or ButtonProfile()
        self.writes = 0

    def read_profile(self) -> ButtonProfile:
        return ButtonProfile.from_dict(self._profile.to_dict())

    def write_profile(self, profile: ButtonProfile) -> None:
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
