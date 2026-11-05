"""Data collectors: one for the OLT (SNMP), one for the router (API)."""

from .olt import OltCollector  # noqa: F401
from .router import RouterCollector  # noqa: F401

__all__ = ["OltCollector", "RouterCollector"]
