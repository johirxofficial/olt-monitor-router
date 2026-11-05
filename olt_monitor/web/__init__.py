"""FastAPI web application (dashboard + JSON API)."""

from .app import create_app  # noqa: F401

__all__ = ["create_app"]
