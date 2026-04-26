"""Lane C US-006 — Shadow Mode interceptor (read-only observation).

Wraps a canonical router and computes a shadow decision in parallel without
mutating the canonical result. Decisions are appended as JSONL to a daily
log file for downstream Grafana / discrepancy / checksum tooling (WS-3).

Flag-gated at caller boundary by ``SHADOW_MODE`` env var; per-user gating
via ``SHADOW_USER_ALLOWLIST`` (comma-separated). Bypass path is zero-cost
beyond two env reads.
"""

from __future__ import annotations

import dataclasses
import json
import os
import time
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, Literal

from parallax.obs.log import get_logger
from parallax.retrieval.contracts import RetrievalEvidence
from parallax.router.contracts import QueryRequest

__all__ = [
    "CrosswalkStatus",
    "ArbitrationOutcome",
    "ShadowDecisionLog",
    "ShadowInterceptor",
]

_log = get_logger("parallax.router.shadow")

CrosswalkStatus = Literal["ok", "miss", "conflict", "skipped"]
ArbitrationOutcome = Literal["match", "shadow_only", "canonical_only", "diverge"]


@dataclasses.dataclass(frozen=True)
class ShadowDecisionLog:
    """Six-field canonical decision-log record for Lane C v0.2.0-beta.

    Field set is the contract surface for downstream Grafana panels and the
    checksum chain in WS-3 — adding or renaming a field is a breaking change.
    """

    query_type: str
    selected_port: str
    crosswalk_status: CrosswalkStatus
    arbitration_outcome: ArbitrationOutcome
    latency_ms: float
    correlation_id: str

    def to_jsonl(self) -> str:
        """One-line JSON with sorted keys so checksum chains stay deterministic."""
        return json.dumps(dataclasses.asdict(self), sort_keys=True)


def _is_enabled(user_id: str) -> bool:
    """True when SHADOW_MODE=true AND user_id is in SHADOW_USER_ALLOWLIST."""
    if os.environ.get("SHADOW_MODE", "false").strip().lower() != "true":
        return False
    raw_allow = os.environ.get("SHADOW_USER_ALLOWLIST", "")
    allow = {u.strip() for u in raw_allow.split(",") if u.strip()}
    return user_id in allow


def _log_path() -> Path:
    """Today's JSONL path under SHADOW_LOG_DIR (default parallax/logs/)."""
    base = Path(os.environ.get("SHADOW_LOG_DIR", "parallax/logs"))
    base.mkdir(parents=True, exist_ok=True)
    today = time.strftime("%Y-%m-%d")
    return base / f"shadow-decisions-{today}.jsonl"


def _write_log(entry: ShadowDecisionLog) -> None:
    """Append one JSONL line. Fail-silent so user-facing call is never affected."""
    try:
        with _log_path().open("a", encoding="utf-8") as fh:
            fh.write(entry.to_jsonl() + "\n")
    except OSError as exc:
        _log.warning(
            "shadow_log_write_failed",
            extra={"event": "shadow_log_write_failed", "error": str(exc)},
        )


def _hits_equal(canonical: RetrievalEvidence, shadow: RetrievalEvidence) -> bool:
    """Compare hits on (id, kind, score) — body fields can legitimately diverge."""
    if len(canonical.hits) != len(shadow.hits):
        return False
    for c_hit, s_hit in zip(canonical.hits, shadow.hits, strict=True):
        if (
            c_hit.get("id") != s_hit.get("id")
            or c_hit.get("kind") != s_hit.get("kind")
            or c_hit.get("score") != s_hit.get("score")
        ):
            return False
    return True


class ShadowInterceptor:
    """Wrap a canonical router; observe shadow decisions without mutating output.

    Hard invariant: ``query()`` always returns the canonical result. Shadow
    failures are caught and logged with ``arbitration_outcome="shadow_only"``;
    they never propagate to the caller.
    """

    def __init__(
        self,
        canonical: Any,
        shadow_factory: Callable[[], Any],
    ) -> None:
        self._canonical = canonical
        self._shadow_factory = shadow_factory

    def query(
        self,
        request: QueryRequest,
        *,
        correlation_id: str | None = None,
    ) -> RetrievalEvidence:
        """Dispatch the canonical query; observe shadow when flag + allowlist permit."""
        canonical_result = self._canonical.query(request)

        if not _is_enabled(request.user_id):
            return canonical_result

        cid = correlation_id if correlation_id is not None else str(uuid.uuid4())
        start = time.perf_counter()
        outcome: ArbitrationOutcome
        crosswalk: CrosswalkStatus
        try:
            shadow = self._shadow_factory()
            shadow_result = shadow.query(request)
            outcome = "match" if _hits_equal(canonical_result, shadow_result) else "diverge"
            crosswalk = "ok"
        except Exception as exc:  # noqa: BLE001 — shadow failures must never break canonical
            _log.warning(
                "shadow_query_error",
                extra={"event": "shadow_query_error", "error": str(exc)},
            )
            outcome = "shadow_only"
            crosswalk = "skipped"

        latency_ms = (time.perf_counter() - start) * 1000.0
        _write_log(
            ShadowDecisionLog(
                query_type=request.query_type.value,
                selected_port="QueryPort",
                crosswalk_status=crosswalk,
                arbitration_outcome=outcome,
                latency_ms=latency_ms,
                correlation_id=cid,
            )
        )
        return canonical_result
