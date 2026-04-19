"""Hypothesis property fuzz tests for parallax.ingest.

Properties under test (v0.1.2 stress bundle):

* Idempotence: re-ingesting the same logical content returns the same id.
* NFC collapse: decomposed vs precomposed Unicode produce one row, one id.
* Distinct-input -> distinct content_hash (no spurious collisions).
* content_hash matches the schema formula at every persisted row.
* Long strings up to 10_000 chars do not break UPSERT.

User-id suffix fuzzing uses boundary ints rendered as strings -- keeping the
user_id column type (TEXT) unchanged at the schema boundary.
"""

from __future__ import annotations

import sqlite3
import unicodedata

from hypothesis import HealthCheck, assume, given, settings
from hypothesis import strategies as st

from parallax.hashing import content_hash
from parallax.ingest import ingest_claim, ingest_memory
from parallax.sqlite_store import query

# ----- Strategies -----------------------------------------------------------

# Any-codepoint text; keep non-empty to avoid trivial ""-only canonicalization.
_ANY_TEXT = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),  # no surrogates
    min_size=1,
    max_size=200,
)

# Long strings up to 10_000 chars (ASCII keeps the generation cheap).
_LONG_TEXT = st.text(
    alphabet=st.characters(min_codepoint=0x20, max_codepoint=0x7E),
    min_size=5000,
    max_size=10_000,
)

# Boundary ints used as user_id suffixes (stringified at the schema boundary).
_BOUNDARY_INTS = st.sampled_from([0, 1, -1, 2**31 - 1, -(2**31), 2**63 - 1, -(2**63)])

# NFC-equivalent pair: a single codepoint strategy that offers both
# decomposed and precomposed forms of the same character.
_NFC_PAIRS = st.sampled_from(
    [
        ("e\u0301", "\u00e9"),          # é
        ("a\u0300", "\u00e0"),          # à
        ("o\u0302", "\u00f4"),          # ô
        ("u\u0308", "\u00fc"),          # ü
        ("c\u0327", "\u00e7"),          # ç
    ]
)


# Common settings for stress fuzzing: disable the function-scoped-fixture
# health check because we reset DB state inside each example, and bound
# deadlines so CI stays snappy.
_FUZZ_SETTINGS = settings(
    max_examples=50,
    deadline=None,
    suppress_health_check=[HealthCheck.function_scoped_fixture],
)


def _reset_db(conn: sqlite3.Connection) -> None:
    """Wipe user-facing rows between hypothesis examples."""
    with conn:
        conn.execute("DELETE FROM memories")
        conn.execute("DELETE FROM claims")
        conn.execute("DELETE FROM sources")


# ----- Idempotence ----------------------------------------------------------


class TestIdempotenceFuzz:
    @_FUZZ_SETTINGS
    @given(title=_ANY_TEXT, summary=_ANY_TEXT, path=_ANY_TEXT)
    def test_memory_idempotent(
        self, conn: sqlite3.Connection, title: str, summary: str, path: str
    ) -> None:
        _reset_db(conn)
        a = ingest_memory(
            conn, user_id="u", title=title, summary=summary, vault_path=path
        )
        b = ingest_memory(
            conn, user_id="u", title=title, summary=summary, vault_path=path
        )
        assert a == b
        rows = query(conn, "SELECT COUNT(*) AS n FROM memories", ())
        assert rows[0]["n"] == 1

    @_FUZZ_SETTINGS
    @given(subject=_ANY_TEXT, predicate=_ANY_TEXT, obj=_ANY_TEXT)
    def test_claim_idempotent(
        self,
        conn: sqlite3.Connection,
        subject: str,
        predicate: str,
        obj: str,
    ) -> None:
        _reset_db(conn)
        a = ingest_claim(
            conn, user_id="u", subject=subject, predicate=predicate, object_=obj
        )
        b = ingest_claim(
            conn, user_id="u", subject=subject, predicate=predicate, object_=obj
        )
        assert a == b
        rows = query(conn, "SELECT COUNT(*) AS n FROM claims", ())
        assert rows[0]["n"] == 1


# ----- NFC collapse ---------------------------------------------------------


class TestNFCCollapseFuzz:
    @_FUZZ_SETTINGS
    @given(pair=_NFC_PAIRS, prefix=_ANY_TEXT, suffix=_ANY_TEXT)
    def test_nfc_equivalent_inputs_collapse_memory(
        self,
        conn: sqlite3.Connection,
        pair: tuple[str, str],
        prefix: str,
        suffix: str,
    ) -> None:
        decomposed, composed = pair
        assert decomposed != composed  # precondition from the pair table
        # Sanity-check that the pair is actually NFC-equivalent.
        assert unicodedata.normalize("NFC", decomposed) == unicodedata.normalize(
            "NFC", composed
        )
        _reset_db(conn)
        a = ingest_memory(
            conn,
            user_id="u",
            title=prefix + decomposed + suffix,
            summary="s",
            vault_path="v.md",
        )
        b = ingest_memory(
            conn,
            user_id="u",
            title=prefix + composed + suffix,
            summary="s",
            vault_path="v.md",
        )
        assert a == b, "NFC-equivalent inputs must collapse to one row"
        rows = query(conn, "SELECT COUNT(*) AS n FROM memories", ())
        assert rows[0]["n"] == 1


# ----- Distinct input -> distinct hash --------------------------------------


class TestDistinctInputsFuzz:
    @_FUZZ_SETTINGS
    @given(
        a=_ANY_TEXT,
        b=_ANY_TEXT,
        c=_ANY_TEXT,
        d=_ANY_TEXT,
    )
    def test_distinct_triples_produce_distinct_hashes(
        self, a: str, b: str, c: str, d: str
    ) -> None:
        # NFC-normalized canonical form must not collide across logically
        # distinct inputs. We compare the normalized projection because
        # two raw strings that NFC-collapse to the same canonical form are
        # SUPPOSED to hash equally (that's the contract).
        def norm(x: str) -> str:
            return unicodedata.normalize("NFC", x).strip()

        # Degenerate examples (same canonical input) are uninteresting —
        # use hypothesis.assume so the shrinker skips them AND they don't
        # count toward max_examples, preserving effective coverage of the
        # non-degenerate space.
        assume((norm(a), norm(b)) != (norm(c), norm(d)))
        assert content_hash(a, b) != content_hash(c, d)


# ----- Schema formula (persisted content_hash) ------------------------------


class TestSchemaFormulaFuzz:
    @_FUZZ_SETTINGS
    @given(title=_ANY_TEXT, summary=_ANY_TEXT, path=_ANY_TEXT)
    def test_memory_row_hash_matches_formula(
        self, conn: sqlite3.Connection, title: str, summary: str, path: str
    ) -> None:
        _reset_db(conn)
        mid = ingest_memory(
            conn, user_id="u", title=title, summary=summary, vault_path=path
        )
        row = query(
            conn, "SELECT content_hash FROM memories WHERE memory_id = ?", (mid,)
        )[0]
        expected = content_hash(title, summary, path)
        assert row["content_hash"] == expected

    @_FUZZ_SETTINGS
    @given(subject=_ANY_TEXT, predicate=_ANY_TEXT, obj=_ANY_TEXT)
    def test_claim_row_hash_matches_formula(
        self,
        conn: sqlite3.Connection,
        subject: str,
        predicate: str,
        obj: str,
    ) -> None:
        _reset_db(conn)
        cid = ingest_claim(
            conn, user_id="u", subject=subject, predicate=predicate, object_=obj
        )
        row = query(
            conn, "SELECT content_hash FROM claims WHERE claim_id = ?", (cid,)
        )[0]
        expected = content_hash(subject, predicate, obj, "direct:u", "u")
        assert row["content_hash"] == expected


# ----- Long strings + boundary user ids -------------------------------------


class TestExtremeInputsFuzz:
    @settings(
        max_examples=20,
        deadline=None,
        suppress_health_check=[
            HealthCheck.function_scoped_fixture,
            HealthCheck.large_base_example,
        ],
    )
    @given(body=_LONG_TEXT, uid_int=_BOUNDARY_INTS)
    def test_long_strings_and_boundary_user_ids(
        self, conn: sqlite3.Connection, body: str, uid_int: int
    ) -> None:
        _reset_db(conn)
        uid = f"u{uid_int}"
        mid = ingest_memory(
            conn, user_id=uid, title=body, summary="s", vault_path="v.md"
        )
        assert isinstance(mid, str) and len(mid) > 0
        row = query(
            conn, "SELECT memory_id FROM memories WHERE memory_id = ?", (mid,)
        )
        assert len(row) == 1
