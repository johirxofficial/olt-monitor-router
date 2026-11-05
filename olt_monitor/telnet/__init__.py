"""Telnet CLI collection (read-only) for OLT telemetry.

The public entry points are :func:`collect` and :func:`probe`.
"""

from .client import TelnetError, TelnetSession, is_read_only
from .collector import collect, probe

__all__ = ["TelnetError", "TelnetSession", "is_read_only", "collect", "probe"]
