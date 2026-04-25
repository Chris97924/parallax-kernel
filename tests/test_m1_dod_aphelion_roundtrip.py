"""M1 DoD — L0 Round-trip: Parallax claim ↔ Aphelion pkg ↔ re-ingest byte-equal.

Flow:
  1. Ingest a claim directly into Parallax → record (claim_id, content_hash).
  2. Export the claim to an Aphelion package directory (.md frontmatter format).
  3. Pack the directory into an .aphelion.tar using aphelion.packer.pack.
  4. Unpack the archive with aphelion.unpacker.unpack.
  5. Parse the claim .md frontmatter to recover (subject, predicate, object_).
  6. Re-ingest with the same parameters → Parallax INSERT-OR-IGNORE returns the
     same claim_id (dedup proves content_hash byte-equality end-to-end).
"""

from __future__ import annotations

import hashlib
import json
import pathlib
import sqlite3

import pytest

from parallax.ingest import ingest_claim
from parallax.migrations import migrate_to_latest
from parallax.sqlite_store import connect

try:
    from aphelion.packer import pack as aphelion_pack
    from aphelion.unpacker import unpack as aphelion_unpack

    _APHELION_AVAILABLE = True
except ImportError:
    _APHELION_AVAILABLE = False

pytestmark = pytest.mark.skipif(
    not _APHELION_AVAILABLE,
    reason="aphelion package not installed",
)

_CLAIM_UUID = "01930001-0000-7000-8000-000000000a1b"
_INSTANCE_UUID = "01930001-0000-7000-8000-0000000000c2"
_PACKAGE_UUID = "01930001-0000-7000-8000-000000000001"
_EVENT_UUID = "01930001-0000-7000-8000-eeeeeee00001"
_TIMESTAMP = "2026-04-25T00:00:00Z"
_USER = "dod_user"


def _build_aphelion_pkg(
    pkg_dir: pathlib.Path,
    *,
    subject: str,
    predicate: str,
    object_: str,
    source_id: str | None,
) -> None:
    """Write an Aphelion package directory for one claim."""
    claims_dir = pkg_dir / "claims"
    claims_dir.mkdir()

    # Build YAML-ish frontmatter exactly as aphelion samples use it
    source_val = f'"{source_id}"' if source_id is not None else "null"
    md_body = (
        "---\n"
        f'"claim_id": "{_CLAIM_UUID}"\n'
        f'"claim_instance_id": "{_INSTANCE_UUID}"\n'
        f'"created_at": "{_TIMESTAMP}"\n'
        f'"object": "{object_}"\n'
        f'"predicate": "{predicate}"\n'
        f'"source": {source_val}\n'
        '"state": "active"\n'
        f'"subject": "{subject}"\n'
        '"type": "architecture_decision"\n'
        f'"updated_at": "{_TIMESTAMP}"\n'
        "---\n"
        f"Round-trip test claim: {subject} {predicate} {object_}\n"
    )
    md_bytes = md_body.encode("utf-8")
    md_path = claims_dir / f"{_CLAIM_UUID}.md"
    md_path.write_bytes(md_bytes)
    file_hash = hashlib.sha256(md_bytes).hexdigest()

    manifest = {
        "aphelion_spec_version": "0.4.0",
        "claims": [
            {
                "claim_id": _CLAIM_UUID,
                "claim_instance_id": _INSTANCE_UUID,
                "hash": file_hash,
                "path": f"claims/{_CLAIM_UUID}.md",
                "state": "active",
            }
        ],
        "created_at": _TIMESTAMP,
        "format_version": "2.0",
        "license": "Apache-2.0",
        "package_id": _PACKAGE_UUID,
        "producer": "parallax-test",
        "provenance_path": "provenance.jsonl",
    }
    (pkg_dir / "manifest.json").write_text(
        json.dumps(manifest, sort_keys=True), encoding="utf-8"
    )

    provenance_event = {
        "actor": "parallax-test",
        "claim_id": _CLAIM_UUID,
        "claim_instance_id": _INSTANCE_UUID,
        "event_id": _EVENT_UUID,
        "event_type": "create",
        "timestamp": _TIMESTAMP,
    }
    (pkg_dir / "provenance.jsonl").write_text(
        json.dumps(provenance_event, sort_keys=True) + "\n", encoding="utf-8"
    )


def _parse_md_frontmatter(md_text: str) -> dict[str, str | None]:
    """Extract key-value pairs from the YAML-ish frontmatter block."""
    lines = md_text.split("\n")
    in_front = False
    fields: dict[str, str | None] = {}
    for line in lines:
        if line.strip() == "---":
            if not in_front:
                in_front = True
                continue
            else:
                break
        if in_front and ":" in line:
            raw_key, _, raw_val = line.partition(":")
            key = raw_key.strip().strip('"')
            val = raw_val.strip()
            if val == "null":
                fields[key] = None
            else:
                fields[key] = val.strip('"')
    return fields


@pytest.fixture()
def conn(tmp_path: pathlib.Path) -> sqlite3.Connection:
    db = tmp_path / "dod.db"
    c = connect(db)
    migrate_to_latest(c)
    c.commit()
    return c


def test_aphelion_round_trip_claim_byte_equal(
    conn: sqlite3.Connection, tmp_path: pathlib.Path
) -> None:
    """M1 DoD: claim survives Aphelion pack/unpack with content_hash byte-equal."""
    subject = "Parallax"
    predicate = "decision:use-sqlite"
    object_ = "sqlite-as-main-store"
    source_id = None

    # Step 1: direct Parallax ingest
    claim_id_orig = ingest_claim(
        conn,
        user_id=_USER,
        subject=subject,
        predicate=predicate,
        object_=object_,
        source_id=source_id,
    )
    orig_hash = conn.execute(
        "SELECT content_hash FROM claims WHERE claim_id=?", (claim_id_orig,)
    ).fetchone()[0]

    # Step 2: build Aphelion package directory
    pkg_dir = tmp_path / "pkg"
    pkg_dir.mkdir()
    _build_aphelion_pkg(
        pkg_dir,
        subject=subject,
        predicate=predicate,
        object_=object_,
        source_id=source_id,
    )

    # Step 3: pack to .aphelion.tar
    archive = tmp_path / "test.aphelion.tar"
    aphelion_pack(pkg_dir, archive)

    # Step 4: unpack
    out_dir = tmp_path / "unpacked"
    aphelion_unpack(archive, out_dir)

    # Step 5: parse .md frontmatter
    md_file = out_dir / "claims" / f"{_CLAIM_UUID}.md"
    fields = _parse_md_frontmatter(md_file.read_text(encoding="utf-8"))

    # Step 6: re-ingest from unpacked Aphelion data
    claim_id_rt = ingest_claim(
        conn,
        user_id=_USER,
        subject=fields["subject"],
        predicate=fields["predicate"],
        object_=fields["object"],
        source_id=fields.get("source"),
    )

    # Assertion: same claim_id proves content_hash byte-equality (dedup hit)
    assert claim_id_rt == claim_id_orig, (
        f"Round-trip produced a NEW claim_id={claim_id_rt!r} instead of "
        f"deduping against original claim_id={claim_id_orig!r}. "
        f"content_hash mismatch: orig_hash={orig_hash!r}"
    )

    # Sanity: verify the stored hash is still the same row
    rt_hash = conn.execute(
        "SELECT content_hash FROM claims WHERE claim_id=?", (claim_id_rt,)
    ).fetchone()[0]
    assert rt_hash == orig_hash, (
        f"content_hash changed after round-trip: {orig_hash!r} → {rt_hash!r}"
    )
