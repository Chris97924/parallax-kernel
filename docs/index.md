# Parallax

**Content-addressed canonical knowledge-base store.**

Parallax gives Claude Code (and any developer assistant) a persistent memory that survives session boundaries. It stores what you did, what you decided, and what you know — and retrieves the right slice at `SessionStart` so the next session picks up where the last one stopped.

## What it stores

Six object types form the schema:

| Object | Role |
|---|---|
| `sources` | Where content came from (files, hooks, direct input). |
| `memories` | Structured summaries tied to vault paths. |
| `claims` | Subject-predicate-object triples extracted from text. |
| `decisions` | Explicit decisions with rationale and target entity. |
| `events` | Immutable append-only audit log (hook fires, state changes). |
| `index_state` | Search-index rebuild snapshots. |

## How it works

Three layers, one direction:

1. **Capture** — Claude Code hooks (`SessionStart`, `PreToolUse`, etc.) append rows to the `events` log.
2. **Compress** — Extractors read events and UPSERT into `memories` and `claims` using content-hash dedup. Same content always converges to the same row on any machine.
3. **Retrieve** — Six typed ops (`recent_context`, `by_file`, `by_decision`, `by_bug_fix`, `by_timeline`, `by_entity`) expose L1/L2/L3 progressive-disclosure hits.

See [Architecture](architecture.md) for the ASCII diagrams and full contract details.

## Quick start

```bash
git clone https://github.com/<your-user>/parallax-kernel.git
cd parallax-kernel
pip install -e .[dev]
python bootstrap.py /tmp/my-parallax
python examples/quickstart.py
```

Or install via pipx for the CLI + server:

```bash
pipx install 'parallax-kernel[server]'
parallax serve --host 127.0.0.1 --port 8765
```

## Where to go next

- [Install](install.md) — all installation paths including pipx and Docker.
- [Architecture](architecture.md) — three-layer flow, schema diagram, retrieval API, ADR index.
- [Deploy](deploy.md) — Fly.io, Railway, and Docker deployment.
- [ADRs](adr/index.md) — the six load-bearing design decisions frozen into schema and migrations.
