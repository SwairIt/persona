-- v1.18 — voice-segment auto-translate.
--
-- Whisper detects the segment's language and writes the transcript in
-- whatever language it was actually spoken (~99 possible per the
-- faster-whisper model card). The Persona UI is one of a small set of
-- locales (today: ``en`` / ``ru`` — see ``app.i18n.SUPPORTED_LANGUAGES``)
-- and a user recording a meeting in German cannot search it from a
-- Russian UI without a translated sidecar.
--
-- Column contract
-- ---------------
--   * ``transcript_translated`` — the UI-language translation of
--     ``transcript``. NULL until ``app.audio.auto_translate.translate_segment``
--     runs (manually or via the worker). Written exactly once per row;
--     a non-NULL value means "already done, do not re-bill the user".
--   * ``source_language`` — best-effort BCP-47-ish hint detected by
--     ``langdetect`` (or NULL when the package isn't installed). Filled
--     lazily on the first translation attempt and used by the worker to
--     skip rows whose ``source_language == target_lang`` without an API
--     round-trip.
--
-- Both columns are added via ``ALTER TABLE`` so existing rows survive
-- with NULL values; the idempotent migration runner swallows the
-- "duplicate column" error so re-running this migration on an already-
-- migrated schema is safe.

ALTER TABLE audio_segment ADD COLUMN transcript_translated TEXT;
ALTER TABLE audio_segment ADD COLUMN source_language TEXT DEFAULT NULL;
