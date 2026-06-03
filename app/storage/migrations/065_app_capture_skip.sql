-- v0.67 — per-app capture pause list.
--
-- The capture loop normally screenshots whatever window happens to be
-- active. Some apps are categorically off-limits: a password manager,
-- a banking page, a private chat. Adding them to the OCR skip-list
-- (``ocr_skip_app``) is not enough — the image still hits the disk and
-- the dedup pipeline still runs; the OCR text is the only thing that
-- gets suppressed. This table is a stricter sibling: a hit here means
-- the capture loop never even creates the screenshot row.
--
-- The semantics intentionally mirror ``ocr_skip_app`` so the operator
-- never has to learn two different normalisation rules: the lookup key
-- is the raw ``app_name`` value lowercased and stripped, and the lookup
-- is a single indexed-PK probe per capture iteration. We don't store
-- ``original_name`` separately for display because the normalised form
-- is what the user typed (sans whitespace/case) — round-tripping is
-- good enough for a settings page that exists to bulk-mute apps.
--
-- ``added_at`` is recorded mainly so a future audit page can show "you
-- muted Bitwarden three weeks ago" — the capture loop itself does not
-- read it.

CREATE TABLE IF NOT EXISTS app_capture_skip (
    app_name TEXT PRIMARY KEY,
    added_at TEXT NOT NULL DEFAULT (datetime('now'))
);
