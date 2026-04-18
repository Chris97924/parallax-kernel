-- Acceptance 04: rebuild_index produces an identical (doc_count, state)
-- snapshot pre/post when no source data has changed. version increments
-- monotonically, but doc_count + state are stable.
SELECT index_name, version, doc_count, state
FROM index_state
WHERE index_name = ?
ORDER BY version;
