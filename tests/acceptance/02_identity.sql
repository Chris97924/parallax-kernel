-- Acceptance 02: every object has a PK and the claim->source FK is joinable.
-- Each PK lookup MUST return exactly 1 row for the seeded id; the JOIN
-- query MUST return exactly 1 row (no row dropped by JOIN).
SELECT COUNT(*) FROM claims  WHERE claim_id  = ?;
SELECT COUNT(*) FROM memories WHERE memory_id = ?;
SELECT COUNT(*) FROM sources  WHERE source_id = ?;
SELECT COUNT(*) FROM events   WHERE event_id  = ?;
SELECT COUNT(*) FROM claims c JOIN sources s ON c.source_id = s.source_id WHERE c.claim_id = ?;
