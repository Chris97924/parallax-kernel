-- Acceptance 03: any claim's state history is replayable from events.
-- Returns (actor, created_at, payload_json) ordered by created_at for the
-- given (target_kind, target_id) pair.
SELECT actor, created_at, payload_json
FROM events
WHERE target_kind = ? AND target_id = ?
ORDER BY created_at;
