"""Tests for parallax.extract.shadow.shadow_write."""

from __future__ import annotations

import logging
import sqlite3

import pytest

from parallax.extract import RawClaim
from parallax.extract.providers.mock import MockProvider
from parallax.extract.shadow import shadow_write


def _raw() -> RawClaim:
    return RawClaim(
        entity="x",
        claim_text="y",
        polarity=1,
        confidence=0.9,
        claim_type="feature",
        evidence="",
    )


class _CrashingProvider:
    def extract_claims(self, text: str) -> list[RawClaim]:
        raise RuntimeError("provider exploded")


def test_shadow_write_success_emits_log(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="parallax_shadow_write")
    ids = shadow_write(
        conn,
        "hello",
        provider=MockProvider(claims=[_raw()]),
        user_id="chris",
    )
    assert len(ids) == 1
    records = [r for r in caplog.records if r.name == "parallax_shadow_write"]
    assert len(records) == 1
    rec = records[0]
    assert rec.message == "parallax_shadow_write"
    assert rec.count == 1
    assert rec.user_id == "chris"
    assert isinstance(rec.elapsed_ms, float)
    assert not hasattr(rec, "error")


def test_shadow_write_swallows_provider_exception(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.WARNING, logger="parallax_shadow_write")
    ids = shadow_write(
        conn,
        "hello",
        provider=_CrashingProvider(),
        user_id="chris",
    )
    assert ids == []
    records = [r for r in caplog.records if r.name == "parallax_shadow_write"]
    assert len(records) == 1
    rec = records[0]
    # failure path logs at WARNING so silent shadow failures stay visible
    assert rec.levelno == logging.WARNING
    assert rec.count == 0
    assert "provider exploded" in rec.error


def test_shadow_write_empty_text_is_quiet_success(
    conn: sqlite3.Connection, caplog: pytest.LogCaptureFixture
) -> None:
    caplog.set_level(logging.INFO, logger="parallax_shadow_write")
    ids = shadow_write(
        conn, "", provider=MockProvider(claims=[_raw()]), user_id="chris"
    )
    assert ids == []
    records = [r for r in caplog.records if r.name == "parallax_shadow_write"]
    # still emits one record for observability
    assert len(records) == 1
    assert records[0].count == 0
