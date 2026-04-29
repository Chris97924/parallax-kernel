"""Unit tests for EventIngestRequest / EventIngestResponse Pydantic schemas.

Validates the boundary contract for POST /event:
- All required fields enforced (each individual omission rejected)
- Empty strings on required fields rejected (min_length=1)
- Default empty dicts on judge_metadata + payload
- Extra fields rejected (_StrictModel extra='forbid')
"""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from parallax.server.schemas import EventIngestRequest, EventIngestResponse


def _valid_kwargs() -> dict[str, object]:
    return {
        "source": "orbit",
        "source_instance": "orbit-test",
        "schema_version": "1.0",
        "event_type": "dissident_record",
        "run_id": "run-1",
        "record_id": "rec-1",
        "created_at": "2026-04-29T17:00:00.000000+00:00",
        "commit_sha": "abc1234",
        "payload_hash": "sha256-deadbeef",
    }


def test_minimal_envelope_accepted() -> None:
    """All required fields present + judge_metadata/payload default → OK."""
    req = EventIngestRequest(**_valid_kwargs())
    assert req.source == "orbit"
    assert req.judge_metadata == {}
    assert req.payload == {}
    assert req.user_id is None


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "source_instance",
        "schema_version",
        "event_type",
        "run_id",
        "record_id",
        "created_at",
        "commit_sha",
        "payload_hash",
    ],
)
def test_missing_required_field_rejected(field: str) -> None:
    kwargs = _valid_kwargs()
    del kwargs[field]
    with pytest.raises(ValidationError):
        EventIngestRequest(**kwargs)


@pytest.mark.parametrize(
    "field",
    [
        "source",
        "source_instance",
        "schema_version",
        "event_type",
        "run_id",
        "record_id",
        "created_at",
        "commit_sha",
        "payload_hash",
    ],
)
def test_empty_string_required_field_rejected(field: str) -> None:
    kwargs = _valid_kwargs()
    kwargs[field] = ""
    with pytest.raises(ValidationError):
        EventIngestRequest(**kwargs)


def test_extra_field_rejected() -> None:
    """_StrictModel uses extra='forbid'."""
    kwargs = _valid_kwargs()
    kwargs["unexpected"] = "fail"
    with pytest.raises(ValidationError):
        EventIngestRequest(**kwargs)


def test_judge_metadata_and_payload_round_trip() -> None:
    """Nested dicts in judge_metadata + payload pass through unchanged."""
    kwargs = _valid_kwargs()
    kwargs["judge_metadata"] = {"a": 1, "b": [2, 3]}
    kwargs["payload"] = {"nested": {"deep": "value"}}
    req = EventIngestRequest(**kwargs)
    assert req.judge_metadata == {"a": 1, "b": [2, 3]}
    assert req.payload == {"nested": {"deep": "value"}}


def test_response_model_shape() -> None:
    resp = EventIngestResponse(event_id="evt-1", user_id="chris", event_type="dissident_record")
    assert resp.event_id == "evt-1"
    assert resp.user_id == "chris"
    assert resp.event_type == "dissident_record"
