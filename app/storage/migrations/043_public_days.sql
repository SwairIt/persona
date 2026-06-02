-- v0.44 — public-day opt-in.
--
-- An admin can mark specific calendar days as "public" so a stable,
-- unauthenticated URL (/public/day/{slug}) can render that day's
-- captures + notes for a public audience. The mapping is one row per
-- day, keyed by the local YYYY-MM-DD string the rest of Persona already
-- uses (see ``app.storage.time`` and the day_scrubber / day_collage
-- routes). The slug is the URL-visible identifier — short, lowercase,
-- a-z / 0-9 / hyphen — independent of the day so the same publication
-- survives if the operator wants to rename the URL later.
--
-- Sensitive content (annotations, screenshots tagged ``private`` or
-- ``confidential``, redacted OCR) is filtered out at *render* time by
-- the route, not the schema, so flipping a tag instantly drops a shot
-- from the public view without rewriting any rows here.

CREATE TABLE IF NOT EXISTS public_day (
    day TEXT PRIMARY KEY,
    slug TEXT NOT NULL UNIQUE,
    title TEXT NOT NULL,
    blurb TEXT,
    published_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_public_day_published_at ON public_day(published_at);
