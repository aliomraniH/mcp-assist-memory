-- ---------------------------------------------------------------------------
-- Content hash — the always-present, git-free dimension of the version vector.
--
-- A sha256 of the canonical value lets a reader ask "did this fact actually
-- change, or was it just restated?" and lets the coordination detectors group
-- identical entries: duplicate facts within a namespace, and the same fact
-- under two namespaces (the canvas-case / canvas-glp1 drift class).
--
-- NULLABLE: legacy rows (and tombstones) carry no hash; the detectors compute
-- it on the fly from the value when the column is null, so nothing is required
-- to be backfilled for them to work.
-- ---------------------------------------------------------------------------
ALTER TABLE memory_entry ADD COLUMN IF NOT EXISTS content_hash text;

-- Backs the store-wide drift scan: GROUP BY content_hash across namespaces.
CREATE INDEX IF NOT EXISTS memory_entry_content_hash ON memory_entry (content_hash);
