"""Tests for is_dual_read_enabled() router config function (M3-T1.2, US-011).

Verifies dynamic re-read via monkeypatch and that ONLY literal "true"
(case-insensitive) is truthy — all other values are falsy.
"""

from __future__ import annotations

import pytest

from parallax.router.config import is_dual_read_enabled

# ---------------------------------------------------------------------------
# Default: env unset -> False
# ---------------------------------------------------------------------------


def test_default_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("DUAL_READ", raising=False)
    assert is_dual_read_enabled() is False


# ---------------------------------------------------------------------------
# Truthy: only "true" (case-insensitive)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["true", "True", "TRUE"])
def test_truthy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("DUAL_READ", val)
    assert is_dual_read_enabled() is True


# ---------------------------------------------------------------------------
# Falsy: "yes", "", "false", "1", unset
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("val", ["yes", "", "false", "False", "FALSE", "1", "0", "on"])
def test_falsy_values(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("DUAL_READ", val)
    assert is_dual_read_enabled() is False


# ---------------------------------------------------------------------------
# Dynamic re-read: monkeypatch sequence
# ---------------------------------------------------------------------------


def test_dynamic_reread_sequence(monkeypatch: pytest.MonkeyPatch) -> None:
    """Full sequence: unset -> false; set true -> true; set TRUE -> true;
    set yes -> false; set '' -> false; set false -> false; unset -> false."""
    monkeypatch.delenv("DUAL_READ", raising=False)
    assert is_dual_read_enabled() is False

    monkeypatch.setenv("DUAL_READ", "true")
    assert is_dual_read_enabled() is True

    monkeypatch.setenv("DUAL_READ", "TRUE")
    assert is_dual_read_enabled() is True

    monkeypatch.setenv("DUAL_READ", "yes")
    assert is_dual_read_enabled() is False

    monkeypatch.setenv("DUAL_READ", "")
    assert is_dual_read_enabled() is False

    monkeypatch.setenv("DUAL_READ", "false")
    assert is_dual_read_enabled() is False

    monkeypatch.delenv("DUAL_READ", raising=False)
    assert is_dual_read_enabled() is False


# ---------------------------------------------------------------------------
# is_dual_read_enabled exported from config.__all__
# ---------------------------------------------------------------------------


def test_exported_in_all() -> None:
    from parallax.router import config

    assert "is_dual_read_enabled" in config.__all__


# ---------------------------------------------------------------------------
# No DUAL_READ: Final[bool] constant in config module
# ---------------------------------------------------------------------------


def test_no_final_constant() -> None:
    import parallax.router.config as cfg

    assert not hasattr(
        cfg, "DUAL_READ"
    ), "DUAL_READ must NOT be a Final[bool] constant per Q8 decision"
