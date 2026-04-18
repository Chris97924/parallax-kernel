"""MockProvider — deterministic in-memory backend for unit tests."""

from __future__ import annotations

from collections.abc import Callable

from parallax.extract.types import RawClaim

__all__ = ["MockProvider"]


class MockProvider:
    """Test double implementing the ``Provider`` Protocol.

    Two modes:

    * ``MockProvider(claims=[...])`` — return the preset list verbatim on
      every ``extract_claims`` call.
    * ``MockProvider(fn=callable)`` — call ``fn(text)`` and return its
      ``list[RawClaim]`` result.

    Either mode records every invocation in ``self.calls`` so assertions
    like ``assert provider.calls == ["..."]`` work.
    """

    def __init__(
        self,
        claims: list[RawClaim] | None = None,
        *,
        fn: Callable[[str], list[RawClaim]] | None = None,
    ) -> None:
        if claims is not None and fn is not None:
            raise ValueError("MockProvider: pass either claims= or fn=, not both")
        self._claims: list[RawClaim] = list(claims) if claims is not None else []
        self._fn = fn
        self.calls: list[str] = []

    def extract_claims(self, text: str) -> list[RawClaim]:
        self.calls.append(text)
        if self._fn is not None:
            return list(self._fn(text))
        return list(self._claims)
