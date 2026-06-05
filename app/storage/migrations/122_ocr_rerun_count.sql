-- v1.23 — Per-shot manual OCR re-run counter.
--
-- The Re-run OCR button on the screenshot detail page (route
-- ``POST /api/screenshot/{id}/ocr-rerun`` — see
-- :mod:`app.web.routes.ocr_rerun`) lets the operator re-process a
-- single shot through the OCR pipeline on demand, without nuking the
-- row or going through the batch-oriented ``/ocr-retry`` admin page.
--
-- This column is a forensic side-channel: every successful manual
-- re-run bumps the counter, so we can later quantify how often the
-- automatic OCR pass produced garbage that humans had to correct.
-- Defaults to ``0`` (NOT NULL) so existing rows are queryable without
-- a COALESCE; the route uses ``COALESCE(ocr_rerun_count, 0) + 1`` only
-- as defence-in-depth in case some pre-migration row escapes the
-- default.

ALTER TABLE screenshots ADD COLUMN ocr_rerun_count INTEGER NOT NULL DEFAULT 0;
