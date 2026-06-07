-- T6 (v1.66) — Cross-device shot identity.
--
-- Why screenshots.uuid:
--   The autoincrement ``id`` only makes sense inside one SQLite file.
--   Once the user has notes / tags / annotations attached to a shot
--   and wants those attachments to follow them to another device, the
--   sync system needs a stable identifier that means the same thing
--   on both sides. ``id`` doesn't — the same shot would be id=17 on
--   the iPhone and id=4231 on the Mac.
--
--   ``uuid`` is generated server-side (or by the agent during ingest)
--   the FIRST time a shot is touched by anything sync-aware. Existing
--   rows are NULL until then; the sync_event API and the annotation
--   handler refuse to operate on a shot without uuid and instead call
--   :func:`app.shots.uuid_helper.ensure_uuid` which mints one and
--   updates the row.
--
-- Why shot_annotation.shot_uuid:
--   The handler picks the canonical screenshot row by uuid (not by
--   screenshot_id) so an annotation written on device A can be applied
--   on device B where the same conceptual shot has a different id.
--
-- Why screenshot_tags.shot_uuid:
--   Same reasoning. Per-shot tag attachments are the most-used sync
--   payload — a user tagging "#work" on a shot on their Mac wants the
--   same shot to be tagged on their iPhone.

ALTER TABLE screenshots ADD COLUMN uuid TEXT;

CREATE UNIQUE INDEX IF NOT EXISTS idx_screenshots_uuid
    ON screenshots(uuid) WHERE uuid IS NOT NULL;

ALTER TABLE shot_annotation ADD COLUMN shot_uuid TEXT;

CREATE INDEX IF NOT EXISTS idx_shot_annotation_shot_uuid
    ON shot_annotation(shot_uuid) WHERE shot_uuid IS NOT NULL;

ALTER TABLE screenshot_tags ADD COLUMN shot_uuid TEXT;

CREATE INDEX IF NOT EXISTS idx_screenshot_tags_shot_uuid
    ON screenshot_tags(shot_uuid) WHERE shot_uuid IS NOT NULL;
