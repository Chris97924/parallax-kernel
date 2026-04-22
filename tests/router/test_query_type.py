"""US-001: Tests for QueryType enum, MappingState enum, and FieldCandidate dataclass."""

from __future__ import annotations

import subprocess
import sys

from parallax.router.types import FieldCandidate, MappingState, QueryType


def test_query_type_count() -> None:
    assert len(list(QueryType)) == 5


def test_query_type_values_order() -> None:
    assert list(QueryType) == [
        QueryType.RECENT_CONTEXT,
        QueryType.ARTIFACT_CONTEXT,
        QueryType.ENTITY_PROFILE,
        QueryType.CHANGE_TRACE,
        QueryType.TEMPORAL_CONTEXT,
    ]
    assert [m.value for m in QueryType] == [
        "recent_context",
        "artifact_context",
        "entity_profile",
        "change_trace",
        "temporal_context",
    ]


def test_query_type_value_roundtrip() -> None:
    assert QueryType("recent_context") is QueryType.RECENT_CONTEXT


def test_query_type_name() -> None:
    assert QueryType.RECENT_CONTEXT.name == "RECENT_CONTEXT"


def test_query_type_is_str() -> None:
    assert isinstance(QueryType.RECENT_CONTEXT, str) is True


def test_mapping_state_count() -> None:
    assert len(list(MappingState)) == 3


def test_mapping_state_values() -> None:
    assert [m.value for m in MappingState] == ["mapped", "unmapped", "conflict"]


def test_subprocess_import_isolation() -> None:
    """Verify router.types does not drag in parallax.retrieval or parallax.server."""
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import parallax.router.types; import sys; "
                "assert 'parallax.retrieval' not in sys.modules, 'retrieval imported'; "
                "assert 'parallax.server' not in sys.modules, 'server imported'"
            ),
        ],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, f"Isolation check failed:\n{result.stderr}"


def test_field_candidate_frozen() -> None:
    fc = FieldCandidate(source="s", field_name="f", value=42, confidence=0.9)
    try:
        fc.source = "other"  # type: ignore[misc]
        raise AssertionError("Should have raised FrozenInstanceError")
    except Exception as exc:
        assert "frozen" in type(exc).__name__.lower() or "assign" in str(exc).lower()
