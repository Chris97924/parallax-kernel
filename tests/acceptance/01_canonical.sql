-- Acceptance 01: DB canonical exists.
-- Both COUNT(*) results MUST be > 0 after the seed fixture runs.
SELECT COUNT(*) FROM claims;
SELECT COUNT(*) FROM memories;
