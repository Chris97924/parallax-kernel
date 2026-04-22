# parallax-session-hook

Claude Code `SessionStart` hook that fetches a Parallax reminder over HTTP
and prints it as a `<system-reminder>` block, so every new Claude session
starts with context about recent files, decisions, and claims.

## How it works

1. Claude Code fires `SessionStart` before the first user prompt.
2. This hook reads `PARALLAX_API_URL` (default `http://127.0.0.1:8765`)
   and optional `PARALLAX_TOKEN` from the environment.
3. It calls `GET /query/reminder?user_id=...` on the Parallax server.
4. The server returns a pre-rendered `<system-reminder>` block capped at
   2000 chars.
5. The hook prints the block to stdout. Claude Code splices it into the
   session context.

On any failure (server down, auth wrong, network timeout) the hook exits
**silently with code 0** so a broken Parallax never blocks a Claude
session. The cost of a missed reminder is much lower than a crashed
session.

## Install

In your Claude Code `settings.json`:

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

## Environment

| Variable            | Default                   | Purpose                            |
|---------------------|---------------------------|------------------------------------|
| `PARALLAX_API_URL`  | `http://127.0.0.1:8765`   | Parallax server base URL           |
| `PARALLAX_TOKEN`    | (unset → no auth header)  | Shared bearer token                |
| `PARALLAX_USER_ID`  | `chris`                   | Scope of the reminder query        |
| `PARALLAX_HOOK_TIMEOUT` | `3.0`                  | HTTP timeout in seconds            |
| `PARALLAX_HOOK_DEBUG`   | (unset)                | When `1`, log failures to stderr   |
