"""In-process metrics counters for Parallax.

Thread-safe ``Counter`` + module-level ``registry`` dict. Pre-registered
counters (``ingest_memory_total``, ``ingest_claim_total``, ``dedup_hit_total``,
``retrieve_total``) are created at import time.
"""

from __future__ import annotations

import threading

__all__ = ["Counter", "registry", "get_counter"]


class Counter:
    def __init__(self, name: str) -> None:
        self.name = name
        self._value = 0
        self._lock = threading.Lock()

    def inc(self, n: int = 1) -> None:
        with self._lock:
            self._value += n

    @property
    def value(self) -> int:
        with self._lock:
            return self._value

    def reset(self) -> None:
        with self._lock:
            self._value = 0


registry: dict[str, Counter] = {}


def get_counter(name: str) -> Counter:
    """Return a registered counter, creating it on first access."""
    if name not in registry:
        registry[name] = Counter(name)
    return registry[name]


for _name in ("ingest_memory_total", "ingest_claim_total", "dedup_hit_total", "retrieve_total"):
    get_counter(_name)
