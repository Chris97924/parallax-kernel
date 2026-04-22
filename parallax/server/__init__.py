"""Parallax HTTP server — hub-and-spoke kernel access over FastAPI.

v0.6 Phase A: localhost hackathon MVP. Wraps :mod:`parallax.ingest`,
:mod:`parallax.retrieve`, :mod:`parallax.injector`, :mod:`parallax.telemetry`,
and :mod:`parallax.introspection` under a single authenticated FastAPI app.

Import the factory:

    from parallax.server import create_app
    app = create_app()

Run via CLI: ``parallax serve --host 0.0.0.0 --port 8765``.
"""

from __future__ import annotations

from parallax.server.app import create_app

__all__ = ["create_app"]
