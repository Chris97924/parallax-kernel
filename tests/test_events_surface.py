"""P0-01: events public surface whitelist + leakage test.

Asserts that the canonical event-append function is exported by
``parallax.sqlite_store.__all__`` and that internal helpers (prefixed ``_``)
or mutating event helpers (``update_event`` / ``delete_event``) never leak
into the public surface.
"""

from __future__ import annotations

import parallax.sqlite_store as store


def test_insert_event_in_public_surface() -> None:
    assert "insert_event" in store.__all__
    assert hasattr(store, "insert_event")


def test_no_update_or_delete_event_in_surface() -> None:
    forbidden = {"update_event", "delete_event"}
    assert not (forbidden & set(store.__all__))
    for name in forbidden:
        assert not hasattr(store, name), f"{name} must not exist on sqlite_store"


def test_no_underscore_prefixed_names_in_all() -> None:
    leaked = [name for name in store.__all__ if name.startswith("_")]
    assert leaked == [], f"Internal helpers leaked into __all__: {leaked}"


def test_all_listed_names_resolve() -> None:
    for name in store.__all__:
        assert hasattr(store, name), f"{name} listed in __all__ but missing on module"


def test_ingest_all_has_no_underscore_leaks() -> None:
    import parallax.ingest as ingest

    leaked = [name for name in ingest.__all__ if name.startswith("_")]
    assert leaked == [], f"Internal helpers leaked into ingest.__all__: {leaked}"
