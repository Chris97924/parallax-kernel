# Parallax v0.6 Phase A — Hackathon Demo

**One sentence:** Claude sessions remember. Every new session starts with
the context from the last one — files you touched, decisions you made,
claims you asserted.

## The problem

Every Claude session starts from zero. Last session's breakthroughs,
yesterday's bug fixes, the architectural decision from Tuesday — all
gone the moment the window closes.

## The fix

Parallax is a content-addressed knowledge kernel. v0.6 exposes it as a
FastAPI HTTP hub:

```
┌─────────────┐        HTTP         ┌──────────────────┐
│ Claude Code │ ──────────────────▶ │ parallax-server  │
│ SessionStart│ ◀ system-reminder ─ │ (FastAPI + kernel)│
└─────────────┘                     └──────────────────┘
```

The `parallax-session-hook` plugin runs on every `SessionStart`, calls
`GET /query/reminder`, and splices the response into the session context
**before Claude's first token**.

## Run the walkthrough

```bash
pip install -e '.[server]'
bash demo/hackathon_walkthrough.sh
```

Five steps, all visible to the judge:

1. Start `parallax serve` on :8765
2. Ingest a memory (today's work)
3. Ingest a claim (a decision)
4. Query it back — with 3-tier progressive disclosure
5. Fetch the `<system-reminder>` block the next Claude session will see

## Install the hook

Add to your Claude Code `settings.json`:

```json
{
  "hooks": {
    "SessionStart": [
      {
        "matcher": "",
        "hooks": [
          {
            "type": "command",
            "command": "python /path/to/plugins/parallax-session-hook/hook.py"
          }
        ]
      }
    ]
  }
}
```

Set `PARALLAX_API_URL` and optionally `PARALLAX_TOKEN` in your shell. The
hook fails silently if the server is down — a broken Parallax never
blocks a Claude session.

## Why this architecture

Previous drafts ("one brain per box") didn't scale past one machine.
v0.6 is single hub, multi-client, with local-mode fallback preserved:
if `PARALLAX_API_URL` is unset, clients talk to the local SQLite store
directly. No coupling, no regression from v0.5.

Endpoint surface:

| Method | Path                | Purpose                                  |
|--------|---------------------|------------------------------------------|
| GET    | `/healthz`          | Liveness probe (unauthenticated)         |
| POST   | `/ingest/memory`    | Ingest a memory row                      |
| POST   | `/ingest/claim`     | Ingest a claim row                       |
| GET    | `/query`            | Dispatch to 6 retrieval kinds, L1/L2/L3  |
| GET    | `/query/reminder`   | `<system-reminder>` block for hooks      |
| GET    | `/inspect/health`   | Telemetry health                         |
| GET    | `/inspect/info`     | Version + row counts + health            |

Full OpenAPI at `/docs` once the server is up.
