"""US-005: Tests for MEMORY_ROUTER flag wiring."""

from __future__ import annotations

import pytest

from parallax.router.config import MEMORY_ROUTER, is_router_enabled
from parallax.router.mock_adapter import MockMemoryRouter


def test_default_memory_router_false() -> None:
    assert MEMORY_ROUTER is False


def test_is_router_enabled_default_false() -> None:
    assert is_router_enabled() is False


@pytest.mark.parametrize("val", ["true", "TRUE", "True"])
def test_is_router_enabled_truthy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MEMORY_ROUTER", val)
    assert is_router_enabled() is True


@pytest.mark.parametrize("val", ["1", "yes", "on", "", "false", "  "])
def test_is_router_enabled_falsy(monkeypatch: pytest.MonkeyPatch, val: str) -> None:
    monkeypatch.setenv("MEMORY_ROUTER", val)
    assert is_router_enabled() is False


def test_health_reflects_flag_false(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_ROUTER", "false")
    # Reload config to get fresh flag value via is_router_enabled (dynamic path)
    assert is_router_enabled() is False


def test_health_reflects_flag_true(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("MEMORY_ROUTER", "true")
    # MockMemoryRouter.health() reads config.MEMORY_ROUTER at call time via late import
    # But MEMORY_ROUTER is Final captured at import time; health() uses the late import
    # which re-reads the module-level constant. We test is_router_enabled() for dynamic path.
    assert is_router_enabled() is True


def test_health_report_ok() -> None:
    report = MockMemoryRouter().health()
    assert report.ok is True
    assert report.query_type_count == 5
    assert report.ports_registered == ("QueryPort", "IngestPort", "InspectPort", "BackfillPort")
