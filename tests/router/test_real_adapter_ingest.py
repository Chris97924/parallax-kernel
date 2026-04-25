"""US-D3-01: Tests for RealMemoryRouter.ingest real implementation.

Coverage of PRD addendum AC:
- alias resolution precedence (declared-order wins)
- missing required field raises ValueError with exact alias list in message
- empty-string and None alias fall-through
- non-str type rejection (numeric 0, dict, list, bool)
- lone-surrogate Unicode rejection
- dedup returns deduped=True on second insert of identical payload
- IngestResult.identifier round-trips to memory_id / claim_id
"""

from __future__ import annotations

import sqlite3

import pytest

from parallax.router.contracts import IngestRequest, IngestResult
from parallax.router.real_adapter import RealMemoryRouter

_USER = "test_user_d3_01"


# --- Memory ingest happy paths --------------------------------------------


def test_ingest_memory_returns_ingest_result(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = IngestRequest(
        user_id=_USER,
        kind="memory",
        payload={
            "body": "Python setup notes",
            "title": "Setup",
            "vault_path": "notes/setup.md",
        },
    )
    result = router.ingest(req)
    assert isinstance(result, IngestResult)
    assert result.kind == "memory"
    assert isinstance(result.identifier, str) and len(result.identifier) > 0
    assert result.deduped is False


def test_ingest_memory_dedup_on_second_call(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"body": "same body", "title": "T", "vault_path": "v.md"}
    req = IngestRequest(user_id=_USER, kind="memory", payload=payload)

    first = router.ingest(req)
    second = router.ingest(req)

    assert first.identifier == second.identifier
    assert first.deduped is False
    assert second.deduped is True


# --- Memory alias precedence ----------------------------------------------


def test_ingest_memory_body_alias_first_key_wins(conn: sqlite3.Connection) -> None:
    """`body` wins over `object_` even when both present."""
    router = RealMemoryRouter(conn)
    payload = {
        "body": "body-wins",
        "object_": "object-loses",
        "vault_path": "v.md",
    }
    result = router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    # Verify the persisted summary matches the winning alias.
    row = conn.execute(
        "SELECT summary FROM memories WHERE memory_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["summary"] == "body-wins"


def test_ingest_memory_alias_falls_through_on_empty(conn: sqlite3.Connection) -> None:
    """body='' falls through to object_."""
    router = RealMemoryRouter(conn)
    payload = {"body": "", "object_": "fallback", "vault_path": "v.md"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    row = conn.execute(
        "SELECT summary FROM memories WHERE memory_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["summary"] == "fallback"


def test_ingest_memory_alias_falls_through_on_none(conn: sqlite3.Connection) -> None:
    """body=None falls through to text alias."""
    router = RealMemoryRouter(conn)
    payload = {"body": None, "text": "fallback-text", "vault_path": "v.md"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    row = conn.execute(
        "SELECT summary FROM memories WHERE memory_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["summary"] == "fallback-text"


# --- Memory error paths ----------------------------------------------------


def test_ingest_memory_missing_body_raises_with_alias_list(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"vault_path": "v.md"}  # no body alias at all
    with pytest.raises(ValueError) as exc:
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    msg = str(exc.value)
    # All declared body aliases must appear in the error message.
    for alias in ("body", "object_", "object", "payload_text", "text", "summary", "description"):
        assert alias in msg


def test_ingest_memory_non_str_body_rejected(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"body": 0, "vault_path": "v.md"}
    with pytest.raises(ValueError, match=r"non-str"):
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))


def test_ingest_memory_lone_surrogate_rejected(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"body": "hello\ud800world", "vault_path": "v.md"}
    with pytest.raises(ValueError, match=r"surrogate"):
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))


def test_ingest_unsupported_kind_raises(conn: sqlite3.Connection) -> None:
    """Codex P2: an out-of-Literal kind must be rejected explicitly, not
    silently treated as a claim and written to the claims table.

    ``IngestRequest`` is a plain frozen dataclass; the ``Literal`` type hint
    is a static-checker hint, not a runtime constraint. Construct with a
    bypassing kind via ``object.__setattr__`` to simulate an out-of-contract
    caller (e.g. an unvalidated MCP request body).
    """
    router = RealMemoryRouter(conn)
    req = IngestRequest(user_id=_USER, kind="memory", payload={"body": "x"})
    object.__setattr__(req, "kind", "bogus")
    with pytest.raises(ValueError, match=r"unsupported ingest kind"):
        router.ingest(req)
    # Must not have left a row in either table.
    mem_count = conn.execute(
        "SELECT COUNT(*) AS n FROM memories WHERE user_id = ?", (_USER,)
    ).fetchone()["n"]
    claim_count = conn.execute(
        "SELECT COUNT(*) AS n FROM claims WHERE user_id = ?", (_USER,)
    ).fetchone()["n"]
    assert mem_count == 0 and claim_count == 0


def test_ingest_memory_missing_vault_path_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"body": "x"}  # no vault_path
    with pytest.raises(ValueError) as exc:
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    assert "vault_path" in str(exc.value)


# --- HIGH-4 (Codex finding): title alias errors must propagate, not silently None


def test_ingest_memory_invalid_title_type_propagates(conn: sqlite3.Connection) -> None:
    """title=0 is a type error — must raise, not silently store NULL title."""
    router = RealMemoryRouter(conn)
    payload = {"body": "ok", "vault_path": "v.md", "title": 0}
    with pytest.raises(ValueError, match=r"non-str"):
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))


def test_ingest_memory_lone_surrogate_in_title_propagates(conn: sqlite3.Connection) -> None:
    """title with lone surrogate — must raise, not silently store NULL title."""
    router = RealMemoryRouter(conn)
    payload = {"body": "ok", "vault_path": "v.md", "title": "bad\ud800title"}
    with pytest.raises(ValueError, match=r"surrogate"):
        router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))


def test_ingest_memory_missing_title_stores_null(conn: sqlite3.Connection) -> None:
    """No title alias at all → NULL title persisted (the only acceptable fallback)."""
    router = RealMemoryRouter(conn)
    payload = {"body": "no title here", "vault_path": "v.md"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    row = conn.execute(
        "SELECT title FROM memories WHERE memory_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["title"] is None


def test_ingest_memory_title_alias_name_resolves(conn: sqlite3.Connection) -> None:
    """`name` alias falls through to title when `title` absent."""
    router = RealMemoryRouter(conn)
    payload = {"body": "x", "vault_path": "v.md", "name": "named"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="memory", payload=payload))
    row = conn.execute(
        "SELECT title FROM memories WHERE memory_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["title"] == "named"


# --- Claim ingest happy paths ---------------------------------------------


def test_ingest_claim_returns_ingest_result(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    req = IngestRequest(
        user_id=_USER,
        kind="claim",
        payload={
            "subject": "alice",
            "predicate": "likes",
            "object_": "coffee",
        },
    )
    result = router.ingest(req)
    assert isinstance(result, IngestResult)
    assert result.kind == "claim"
    assert isinstance(result.identifier, str) and len(result.identifier) > 0
    assert result.deduped is False


def test_ingest_claim_dedup_on_second_call(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"subject": "alice", "predicate": "likes", "object_": "tea"}
    req = IngestRequest(user_id=_USER, kind="claim", payload=payload)

    first = router.ingest(req)
    second = router.ingest(req)

    assert first.identifier == second.identifier
    assert first.deduped is False
    assert second.deduped is True


# --- Claim alias precedence -----------------------------------------------


def test_ingest_claim_object_alias_first_key_wins(conn: sqlite3.Connection) -> None:
    """`object_` wins over `object` (declared-order precedence)."""
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "drinks",
        "object_": "object_-wins",
        "object": "object-loses",
    }
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT object FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["object"] == "object_-wins"


def test_ingest_claim_subject_alias_entity_fallback(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"entity": "bob", "predicate": "knows", "object_": "alice"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT subject FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["subject"] == "bob"


def test_ingest_claim_predicate_alias_event_type_fallback(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"subject": "alice", "event_type": "joined", "object_": "team-x"}
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT predicate FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["predicate"] == "joined"


# --- Claim error paths -----------------------------------------------------


def test_ingest_claim_missing_subject_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"predicate": "likes", "object_": "x"}
    with pytest.raises(ValueError) as exc:
        router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    msg = str(exc.value)
    for alias in ("subject", "entity", "name"):
        assert alias in msg


def test_ingest_claim_missing_predicate_raises(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"subject": "alice", "object_": "x"}
    with pytest.raises(ValueError) as exc:
        router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    msg = str(exc.value)
    for alias in ("predicate", "event_type"):
        assert alias in msg


def test_ingest_claim_non_str_subject_rejected(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {"subject": 42, "predicate": "p", "object_": "o"}
    with pytest.raises(ValueError, match=r"non-str"):
        router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))


def test_ingest_claim_confidence_coerced_from_int(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "rates",
        "object_": "coffee",
        "confidence": 1,  # int → coerced to 1.0
    }
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT confidence FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["confidence"] == 1.0


def test_ingest_claim_confidence_bool_rejected(conn: sqlite3.Connection) -> None:
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "rates",
        "object_": "coffee",
        "confidence": True,
    }
    with pytest.raises(ValueError, match=r"bool not accepted"):
        router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))


# --- Codex Round-3 P2: claim state must round-trip from payload ----------


def test_ingest_claim_state_from_payload_persists(conn: sqlite3.Connection) -> None:
    """Codex P2: caller-supplied claim state (e.g. 'pending' from review
    workflow) must NOT be silently rewritten to the 'auto' default."""
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "asserts",
        "object_": "coffee is best",
        "state": "pending",
    }
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT state FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["state"] == "pending"


def test_ingest_claim_state_omitted_defaults_to_auto(conn: sqlite3.Connection) -> None:
    """When state is absent from payload, defaults to 'auto' (preserves
    pre-D-3 behavior)."""
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "asserts",
        "object_": "coffee",
    }
    result = router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
    row = conn.execute(
        "SELECT state FROM claims WHERE claim_id = ?",
        (result.identifier,),
    ).fetchone()
    assert row["state"] == "auto"


def test_ingest_claim_invalid_state_in_payload_raises(conn: sqlite3.Connection) -> None:
    """Caller-supplied invalid state (not in CLAIM_TRANSITIONS) must raise,
    not silently fall back to a default."""
    router = RealMemoryRouter(conn)
    payload = {
        "subject": "alice",
        "predicate": "asserts",
        "object_": "x",
        "state": "bogus",
    }
    with pytest.raises(ValueError, match=r"invalid claim state"):
        router.ingest(IngestRequest(user_id=_USER, kind="claim", payload=payload))
