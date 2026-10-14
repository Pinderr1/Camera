"""Network and camera diagnostics for onboarding: what network this computer is
on, which cameras and recorders answer, and why one does not, in plain English.

Read-only by design: Sentinel inspects and probes, and tells people what to
change; it never changes router, firewall, computer or device settings."""

from . import guidance
from .diagnose import analyze_network, analyze_target, discover_devices, troubleshoot
from .model import Finding, NetworkSnapshot, TargetRefused, check_target, route_for
from .probes import NetworkBackend, SystemNetwork
from .simulated import SimulatedNetwork

__all__ = [
    "Finding", "NetworkBackend", "guidance", "NetworkSnapshot", "SimulatedNetwork", "SystemNetwork", "TargetRefused",
    "analyze_network", "analyze_target", "check_target", "discover_devices", "route_for", "troubleshoot",
]
