# Lane C Phase 1 — MEMORY.md interception via Parallax

## Goal

Let Parallax own Chris's MEMORY.md surface. On every Claude Code SessionStart, a
lightweight hook asks Parallax to re-render MEMORY.md + companion files from
its `memory_cards` table and writes the result as `*.preview` files alongside
the live originals. No production write happens in Phase 1 — the preview
files are inert. After a 7-day observation window of checking diffs against
the live files, Phase 2 will flip the promotion switch and add pm2 + SQLite
cache.

## Architecture (Path A, schema-first)

```
MEMORY.md  ──►  parallax.memory_md.ingest_memory_md  ──►  memory_cards table
                                                              │
                                                              ▼
                                                   GET /export/memory_md
                                                              │
                                                              ▼
            regenerate.py (SessionStart hook)  ◄──────────────┘
                          │
                          ▼
                MEMORY.md.preview + *.preview
```

## Components

| File | Purpose |
|------|---------|
| `parallax/migrations/m0010_memory_cards.py` | Schema migration, table `memory_cards` |
| `parallax/memory_md.py` | Parser, ingest function, privacy filter |
| `parallax/server/routes/export.py` | `GET /export/memory_md` (auth-gated) |
| `C:/Users/user/.claude/scripts/parallax_memory/regenerate.py` | SessionStart hook script, stdlib-only |

## Installation status (2026-04-22)

- [x] Migration m0010 shipped at version 10; registered in `parallax/migrations/__init__.py`.
- [x] Ingest tested against the real MEMORY.md — 8/10 cards survive the privacy filter (2 blocked for the substring `token` — expected).
- [x] Export endpoint returns stable-ordered rendering + companion bodies.
- [x] Hook script exits 0 in ~150 ms when Parallax is down (verified).
- [x] `settings.json` SessionStart hook appended after `parallax-session-inject.js` with `timeout: 2` seconds. Existing ECC bootstrap + parallax-session-inject entries preserved.

## Dry-run contract (Phase 1)

- `PARALLAX_REGEN_DRY_RUN` defaults to `"1"` inside `regenerate.py`.
- Even if someone sets `PARALLAX_REGEN_DRY_RUN=0`, Phase 1 still writes only
  `.preview` files and logs `phase2_not_enabled` into `diff.log`.
- The hook **never** modifies the live `MEMORY.md` or any companion file.

## Operational checks during the 7-day window

1. After a session starts, look in `C:/Users/user/.claude/projects/C--Users-user/memory/` for fresh `.preview` files.
2. Diffs accumulate in `C:/Users/user/.claude/scripts/parallax_memory/diff.log` (auto-rotates at 1 MiB).
3. If Parallax is not running, the hook exits 0 silently within ~200 ms — no session-start regression.
4. If Parallax is running but not yet ingested, `memory_md` comes back as empty section skeleton (`# User\n\n# Projects (Active)\n\n...`) — no harm.

## Promotion to Phase 2 (future)

Phase 2 adds:

- pm2-managed Parallax daemon so the HTTP server survives between sessions.
- A thin local SQLite cache so the hook doesn't need the HTTP server alive at all.
- A promotion flag that flips the hook from "write .preview" to "atomic replace live MEMORY.md".

Phase 2 is deliberately out of scope for Phase 1. Do not remove dry-run
behaviour until:

1. A full week of `diff.log` review shows no surprising drift between
   `.preview` and the live MEMORY.md that Chris maintains manually.
2. The pm2 daemon is in place (otherwise we risk `# User\n# Projects (Active)\n…`
   skeleton overwriting hand-edited memory when Parallax is down).
