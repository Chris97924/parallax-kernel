"""Parse MEMORY.md + companion files and ingest into the memory_cards table.

Lives at ``parallax/memory_md.py`` (top-level) because ``parallax/ingest.py``
already exists as a module and would collide with a ``parallax/ingest/``
package.
"""

from __future__ import annotations

import dataclasses
import hashlib
import pathlib
import re
import sqlite3
from collections.abc import Iterator
from contextlib import contextmanager

from parallax.sqlite_store import now_iso

__all__ = [
    "MemoryMdEntry",
    "CompanionFile",
    "IngestReport",
    "parse_memory_md",
    "parse_companion",
    "ingest_memory_md",
    "SECRET_KEYWORDS",
    "contains_secret",
    "body_looks_like_secret",
]

SECRET_KEYWORDS: tuple[str, ...] = (
    "password",
    "token",
    "api_key",
    "api-key",
    "secret",
    "credential",
    ".env",
    "private_key",
)


def contains_secret(text: str) -> bool:
    """Return True if *text* contains any secret keyword (case-insensitive)."""
    lower = text.lower()
    return any(kw in lower for kw in SECRET_KEYWORDS)


# Compiled once at module load. Matches key=value / key: value pairs where the
# value looks high-entropy (8+ word/path chars). Case-insensitive.
# Note: \.env starts with a dot (non-word char) so it uses (?<!\w) instead of
# \b to avoid the word-boundary anchor failing before a non-word character.
_SECRET_PATTERN = re.compile(
    r"(?i)(?:(?<!\w)\.env|\b(?:password|token|api[-_]?key|api[-_]?token|"
    r"secret|credential|private[-_]?key))\s*[:=]\s*[\w\-./+]{8,}"
)


def body_looks_like_secret(body: str) -> bool:
    """True if *body* contains a key-value pair that looks like a real secret.

    Stricter than ``contains_secret``: substring mentions of 'token' or 'secret'
    in prose do NOT trigger; only key=value / key: value pairs with a
    high-entropy-looking value do. Intended for ingest/export scoping on the
    companion body text, not on user-authored names or descriptions.
    """
    return bool(_SECRET_PATTERN.search(body))


_SECTION_CATEGORY_MAP: dict[str, str] = {
    "User": "user",
    "Projects (Active)": "project",
    "Feedback": "feedback",
    "Reference": "reference",
}


@dataclasses.dataclass(frozen=True)
class MemoryMdEntry:
    category: str
    title: str
    filename: str
    description: str


@dataclasses.dataclass(frozen=True)
class CompanionFile:
    name: str
    description: str
    type: str
    body: str


@dataclasses.dataclass(frozen=True)
class IngestReport:
    cards_inserted: int
    cards_updated: int
    skipped_missing_companion: tuple[str, ...]
    skipped_malformed: tuple[str, ...]
    skipped_privacy: tuple[str, ...]


# Matches ``- [Title](filename.md) — description``; tolerates em-dash (U+2014)
# and ASCII ``" - "`` as the separator.
_BULLET_RE = re.compile(
    r"^\s*-\s+\[(?P<title>[^\]]+)\]\((?P<filename>[^)]+)\)"
    r"(?:\s+(?:—|-)\s+(?P<description>.+))?$"
)


def parse_memory_md(text: str) -> list[MemoryMdEntry]:
    """Parse MEMORY.md text into a list of MemoryMdEntry objects.

    Recognises four section headings (``# User``, ``# Projects (Active)``,
    ``# Feedback``, ``# Reference``).  Lines that don't match the bullet
    pattern are silently skipped.
    """
    entries: list[MemoryMdEntry] = []
    current_category: str | None = None

    for line in text.splitlines():
        stripped = line.strip()
        if stripped.startswith("# "):
            heading = stripped[2:].strip()
            current_category = _SECTION_CATEGORY_MAP.get(heading)
            continue

        if current_category is None:
            continue

        m = _BULLET_RE.match(line)
        if not m:
            continue

        entries.append(
            MemoryMdEntry(
                category=current_category,
                title=m.group("title"),
                filename=m.group("filename"),
                description=(m.group("description") or "").strip(),
            )
        )

    return entries


def parse_companion(path: pathlib.Path) -> CompanionFile:
    """Read a companion .md file and return a CompanionFile.

    Expected format::

        ---
        name: ...
        description: ...
        type: ...
        ---

        <body>

    Raises ValueError on malformed input (missing delimiters or required keys).
    """
    text = path.read_text(encoding="utf-8")
    lines = text.splitlines()

    if not lines or lines[0].strip() != "---":
        raise ValueError(f"Missing opening '---' frontmatter delimiter in {path}")

    try:
        close_idx = lines.index("---", 1)
    except ValueError:
        raise ValueError(
            f"Missing closing '---' frontmatter delimiter in {path}"
        ) from None

    frontmatter_lines = lines[1:close_idx]
    body = "\n".join(lines[close_idx + 1 :]).strip()

    meta: dict[str, str] = {}
    for fm_line in frontmatter_lines:
        if ":" in fm_line:
            key, _, value = fm_line.partition(":")
            meta[key.strip()] = value.strip()

    required = ("name", "description", "type")
    missing = [k for k in required if k not in meta]
    if missing:
        raise ValueError(
            f"Frontmatter missing required keys {missing} in {path}"
        )

    return CompanionFile(
        name=meta["name"],
        description=meta["description"],
        type=meta["type"],
        body=body,
    )


@contextmanager
def _manual_tx(conn: sqlite3.Connection) -> Iterator[None]:
    """Run a block under an explicit BEGIN IMMEDIATE / COMMIT.

    Copied from parallax/migrations/__init__.py to avoid a cross-layer import.
    On any exception the transaction is rolled back and the exception re-raised.
    """
    prev = conn.isolation_level
    conn.isolation_level = None
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield
        conn.execute("COMMIT")
    except BaseException:
        try:
            conn.execute("ROLLBACK")
        except sqlite3.OperationalError:
            pass
        raise
    finally:
        conn.isolation_level = prev


_UPSERT_SQL = """
INSERT INTO memory_cards
    (id, user_id, category, name, filename, description, body, created_at, updated_at)
VALUES
    (:id, :user_id, :category, :name, :filename, :description, :body, :now, :now)
ON CONFLICT(user_id, filename) DO UPDATE SET
    category    = excluded.category,
    name        = excluded.name,
    description = excluded.description,
    body        = excluded.body,
    updated_at  = excluded.updated_at
"""


def ingest_memory_md(
    conn: sqlite3.Connection,
    *,
    memory_md_path: pathlib.Path,
    user_id: str,
) -> IngestReport:
    """Parse *memory_md_path* and upsert each entry into *memory_cards*.

    Idempotent: running twice on the same input produces the same DB state;
    the second run reports cards_inserted=0, cards_updated=N.

    Atomic: all upserts run inside a single BEGIN IMMEDIATE / COMMIT block.
    On any exception the entire transaction is rolled back and the exception
    is re-raised — callers will never see a partial ingest result.

    Privacy filter scope is companion.body only. Names/descriptions/titles are
    not plausible secret carriers; filter previously ran on them only to be
    paranoid but caused false positives on prose mentions of 'token'/'secret'/etc.

    Returns an IngestReport summarising what happened.
    """
    text = memory_md_path.read_text(encoding="utf-8")
    entries = parse_memory_md(text)
    companion_dir = memory_md_path.parent

    inserted = 0
    updated = 0
    skipped_missing: list[str] = []
    skipped_malformed: list[str] = []
    skipped_privacy: list[str] = []

    with _manual_tx(conn):
        for entry in entries:
            companion_path = companion_dir / entry.filename

            # Reject filenames that escape companion_dir (run BEFORE exists()
            # so a crafted '..' cannot even trigger an existence probe).
            try:
                companion_path.resolve().relative_to(companion_dir.resolve())
            except ValueError:
                skipped_malformed.append(entry.filename)
                continue

            if not companion_path.exists():
                skipped_missing.append(entry.filename)
                continue

            try:
                companion = parse_companion(companion_path)
            except (ValueError, OSError):
                skipped_malformed.append(entry.filename)
                continue

            if body_looks_like_secret(companion.body):
                skipped_privacy.append(entry.filename)
                continue

            card_id = hashlib.sha256(
                f"{user_id}::{entry.filename}".encode()
            ).hexdigest()[:16]

            now = now_iso()

            # SQLite's changes() returns 1 for both INSERT and UPDATE on
            # ON CONFLICT DO UPDATE, so distinguish the two with a pre-check.
            existing = conn.execute(
                "SELECT id FROM memory_cards WHERE user_id = ? AND filename = ?",
                (user_id, entry.filename),
            ).fetchone()

            conn.execute(
                _UPSERT_SQL,
                {
                    "id": card_id,
                    "user_id": user_id,
                    "category": entry.category,
                    "name": companion.name,
                    "filename": entry.filename,
                    "description": entry.description,
                    "body": companion.body,
                    "now": now,
                },
            )

            if existing is None:
                inserted += 1
            else:
                updated += 1

    return IngestReport(
        cards_inserted=inserted,
        cards_updated=updated,
        skipped_missing_companion=tuple(skipped_missing),
        skipped_malformed=tuple(skipped_malformed),
        skipped_privacy=tuple(skipped_privacy),
    )
