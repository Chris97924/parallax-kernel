"""P1-02: in-process metrics counter tests."""

from __future__ import annotations

import threading

from parallax.ingest import ingest_claim, ingest_memory
from parallax.obs.metrics import Counter, get_counter, registry
from parallax.retrieve import memories_by_user


def _reset_all() -> None:
    for c in registry.values():
        c.reset()


def test_pre_registered_counters_exist() -> None:
    for name in (
        "ingest_memory_total", "ingest_claim_total",
        "dedup_hit_total", "retrieve_total",
    ):
        assert name in registry


def test_counters_increment_on_ingest(conn) -> None:
    _reset_all()
    ingest_memory(
        conn, user_id="u1", title="t1", summary="s1",
        vault_path="v1.md", source_id=None,
    )
    ingest_memory(
        conn, user_id="u1", title="t2", summary="s2",
        vault_path="v2.md", source_id=None,
    )
    ingest_claim(
        conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None,
    )
    memories_by_user(conn, "u1")
    assert get_counter("ingest_memory_total").value == 2
    assert get_counter("ingest_claim_total").value == 1
    assert get_counter("retrieve_total").value == 1


def test_dedup_hit_counter_fires_on_collision(conn) -> None:
    _reset_all()
    ingest_claim(
        conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None,
    )
    ingest_claim(
        conn, user_id="u1", subject="a", predicate="b", object_="c", source_id=None,
    )
    assert get_counter("dedup_hit_total").value == 1


def test_counter_thread_safety() -> None:
    c = Counter("thread_test")
    def worker() -> None:
        for _ in range(100):
            c.inc()
    threads = [threading.Thread(target=worker) for _ in range(100)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert c.value == 10_000
