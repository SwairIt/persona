-- Rule 3 (owner mandate 2026-07-30): every thought records whether it points
-- at supplied evidence ('observation') or was inferred ('guess'). Default is
-- 'guess' — unmarked or unparsable model output is never silently upgraded
-- to an observation.
ALTER TABLE persona_thought
    ADD COLUMN certainty TEXT NOT NULL DEFAULT 'guess'
        CHECK (certainty IN ('observation', 'guess'));
