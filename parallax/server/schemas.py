"""Pydantic v2 DTOs for the Parallax HTTP API.

All request bodies are validated at the system boundary (per the repo-wide
input-validation rule). Response models mirror the shapes produced by
:mod:`parallax.retrieve` / :mod:`parallax.telemetry` / :mod:`parallax.introspection`
so callers get a stable contract even if the internal dataclasses shift.
"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

__all__ = [
    "IngestMemoryRequest",
    "IngestClaimRequest",
    "IngestResponse",
    "RetrievalHitDTO",
    "QueryResponse",
    "ReminderResponse",
    "HealthResponse",
    "InspectResponse",
    "ErrorResponse",
    "ExportMemoryMdResponse",
    "RETRIEVE_KINDS",
]


# Mirror of parallax.retrieve._RETRIEVE_KINDS — duplicated here so the API
# schema is self-contained (OpenAPI consumers shouldn't need to import the
# internal module to learn valid kinds).
RETRIEVE_KINDS = ("recent", "file", "decision", "bug", "entity", "timeline")
RetrieveKind = Literal["recent", "file", "decision", "bug", "entity", "timeline"]


class _StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class IngestMemoryRequest(_StrictModel):
    user_id: str = Field(min_length=1, max_length=128)
    title: str | None = None
    summary: str | None = None
    vault_path: str = Field(min_length=1)
    source_id: str | None = None

    @field_validator("vault_path")
    @classmethod
    def _reject_traversal(cls, v: str) -> str:
        # vault_path is a logical label, not a real FS path — any `..` segment,
        # absolute path, or NUL byte is a client bug or an attack attempt.
        normalized = v.replace("\\", "/")
        if any(part == ".." for part in normalized.split("/")):
            raise ValueError("vault_path must not contain '..' segments")
        if normalized.startswith("/") or (len(v) >= 2 and v[1] == ":"):
            raise ValueError("vault_path must be relative")
        if "\x00" in v:
            raise ValueError("vault_path must not contain NUL bytes")
        return v


class IngestClaimRequest(_StrictModel):
    # `object_` is aliased to `object` on the wire — avoids shadowing the
    # Python builtin in route handlers while keeping the API name stable.
    model_config = ConfigDict(
        extra="forbid",
        str_strip_whitespace=True,
        populate_by_name=True,
    )

    user_id: str = Field(min_length=1, max_length=128)
    subject: str = Field(min_length=1)
    predicate: str = Field(min_length=1)
    object_: str = Field(min_length=1, alias="object")
    source_id: str | None = None
    confidence: float | None = Field(default=None, ge=0.0, le=1.0)
    state: str = "auto"


class IngestResponse(BaseModel):
    kind: Literal["memory", "claim"]
    id: str
    user_id: str


class RetrievalHitDTO(BaseModel):
    """L1/L2/L3 projection of a :class:`parallax.retrieve.RetrievalHit`.

    ``level`` records the requested disclosure tier so callers can tell
    the server rendered what they asked for without re-reading the request.
    """

    entity_kind: str
    entity_id: str
    title: str
    score: float
    level: Literal[1, 2, 3]
    evidence: str | None = None
    full: dict[str, Any] | str | None = None
    explain: dict[str, Any] | None = None


class QueryResponse(BaseModel):
    kind: RetrieveKind
    level: Literal[1, 2, 3]
    count: int
    hits: list[RetrievalHitDTO]


class ReminderResponse(BaseModel):
    reminder: str
    length: int


class HealthResponse(BaseModel):
    """Shape of :func:`parallax.telemetry.health` plus a status flag."""

    status: Literal["ok", "degraded"]
    db_path: str
    journal_mode: str
    table_counts: dict[str, int]
    last_error: str | None = None


class InspectResponse(BaseModel):
    version: str
    db_path: str
    schema_version: int | None
    memories_count: int
    claims_count: int
    sources_count: int
    events_count: int
    health: HealthResponse


class ErrorResponse(BaseModel):
    error: str
    detail: str | None = None


class ExportMemoryMdResponse(BaseModel):
    memory_md: str
    companion_files: dict[str, str]
