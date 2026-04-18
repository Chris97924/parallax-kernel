"""Provider Protocol — contract every extract backend must satisfy."""

from __future__ import annotations

from typing import Protocol, runtime_checkable

from parallax.extract.types import RawClaim

__all__ = ["Provider"]


@runtime_checkable
class Provider(Protocol):
    """Duck-typed contract: anything with ``extract_claims(str) -> list[RawClaim]``."""

    def extract_claims(self, text: str) -> list[RawClaim]:
        ...
