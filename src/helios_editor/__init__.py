"""Helios II XD3v3 button editor.

Reconstructed from the vendor Windows configuration software and its USBPcap
captures. Provides a visual, image-based mouse button remapper plus DPI /
report-rate / LOD / debounce editing with a real hidraw hardware transport.
"""

from .model import ButtonProfile, default_profile
from .transport import HIDTransport, SimulatedTransport, TransportError

__all__ = [
    "ButtonProfile", "default_profile",
    "HIDTransport", "SimulatedTransport", "TransportError",
]
__version__ = "0.2.0"
