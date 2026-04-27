"""US-D3-07: 410 Gone for kind=bug under MEMORY_ROUTER=true.

RFC 8594 Deprecation + Sunset headers, structured log, counter metric.
"""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient

_QUERY_PARAMS = {"kind": "bug", "user_id": "test_user"}


class TestBugKindDeprecation:
    def test_kind_bug_router_on_returns_410(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        resp = client.get("/query", params=_QUERY_PARAMS)
        assert resp.status_code == 410

    def test_kind_bug_router_on_has_deprecation_header(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        resp = client.get("/query", params=_QUERY_PARAMS)
        assert resp.headers.get("Deprecation") == "true"

    def test_kind_bug_router_on_has_sunset_header(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.setenv("MEMORY_ROUTER", "true")
        resp = client.get("/query", params=_QUERY_PARAMS)
        assert "Sunset" in resp.headers
        assert "2026" in resp.headers["Sunset"]

    def test_kind_bug_router_off_still_works(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        monkeypatch.delenv("MEMORY_ROUTER", raising=False)
        resp = client.get("/query", params=_QUERY_PARAMS)
        # router-off uses legacy by_bug_fix path; may return 200 (empty hits ok)
        assert resp.status_code == 200

    def test_kind_bug_router_on_increments_counter(
        self, client: TestClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        from parallax.obs.metrics import get_counter

        counter = get_counter("deprecated_kind_bug_total")
        before = counter.value

        monkeypatch.setenv("MEMORY_ROUTER", "true")
        client.get("/query", params=_QUERY_PARAMS)

        assert counter.value == before + 1
