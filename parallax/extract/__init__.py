"""parallax.extract — optional LLM-backed claim extraction layer.

This subpackage is NOT imported by ``parallax.__init__`` on purpose:
the core install (``pip install parallax-kernel``) must never transitively
pull in ``httpx`` / ``anthropic`` / ``google-generativeai``. Only users
who install the extra (``pip install 'parallax-kernel[extract]'``) and
explicitly ``from parallax.extract import ...`` pay that cost.

Public surface re-exports the four primitives a caller needs to wire a
provider and push extracted claims into the canonical store::

    from parallax.extract import (
        RawClaim,
        Provider,
        extract_claims,
        extract_and_ingest,
    )

Concrete provider backends (OpenRouter, Claude subprocess) live under
``parallax.extract.providers`` and must be imported explicitly.
"""

from __future__ import annotations

from parallax.extract.extractor import chunk_text, extract_claims
from parallax.extract.ingest import extract_and_ingest
from parallax.extract.providers.base import Provider
from parallax.extract.types import RawClaim

__all__ = [
    "RawClaim",
    "Provider",
    "extract_claims",
    "extract_and_ingest",
    "chunk_text",
]
