-- v0.98 feature 3/3 — explicit screenshot groups (cherry-picked bundles).
--
-- Background
-- ----------
-- Persona already ships *two* ways to gather shots into a named bundle:
--
--   * ``auto_collection`` (migration 044) — slug -> tag binding. Membership
--     is computed on read by joining ``screenshot_tags`` against the tag
--     name, so the contents shift the moment a tag is added or removed
--     from any shot. Great for "everything tagged ``invoice``", useless
--     for "the seven specific shots I'm sending to the accountant on
--     Tuesday".
--
--   * ``query_collection`` (migration 059) — saved FTS query + facet
--     bundle. Same drawback: re-runs the query against current data, so
--     yesterday's matches may be gone today.
--
-- Both are *rule-based*. This migration adds the missing primitive: a
-- hand-curated, immutable-by-default bundle. The user picks shots one
-- by one and drops them into a named group; membership only changes
-- when the user explicitly adds or removes a shot, never as a side
-- effect of tagging or query churn.
--
-- Schema
-- ------
-- Two tables, deliberately separate so a group can exist with zero
-- members (and a future "rename" / "merge groups" feature has somewhere
-- to mutate without touching the membership rows).
--
--   * ``shot_group``         — one row per named bundle. ``slug`` is the
--                              primary key so the URL path segment can
--                              skip percent-encoding (see ``SLUG_RE`` in
--                              :mod:`app.web.routes.shot_groups`).
--                              ``title`` is operator-visible; the column
--                              is just ``NOT NULL`` here so a direct
--                              INSERT cannot leave the list page with an
--                              empty heading — the route enforces the
--                              1..120 length window.
--                              ``created_at`` uses ``datetime('now')`` so
--                              the timestamp shape matches every adjacent
--                              table (025, 074, 076, 078).
--
--   * ``shot_group_member``  — many-to-many between ``shot_group`` and
--                              ``screenshots`` via the composite PK.
--                              ``shot_id`` references an integer-PK on
--                              ``screenshots``; no FK constraint here
--                              because the rest of the schema also avoids
--                              hard FKs on ``screenshot_id`` (cf. the
--                              ``screenshot_tags`` table) so that
--                              physical-row deletes don't cascade through
--                              soft-archive workflows. The route is
--                              responsible for skipping orphan members
--                              when rendering.
--                              ``added_at`` lets the detail page sort
--                              members by insertion order even when the
--                              screenshot ids are noisy.
--
-- Indexes
-- -------
--   * ``idx_shot_group_member_shot`` — reverse lookup ("which groups is
--     shot 42 a member of?"); used by the per-shot toggle widget.
--   * ``idx_shot_group_member_added_at`` — chronological ordering inside
--     the detail page without forcing a sort over the heap.
--
-- ``CREATE TABLE IF NOT EXISTS`` + ``CREATE INDEX IF NOT EXISTS`` keep
-- the migration idempotent across re-runs of ``init_database``.

CREATE TABLE IF NOT EXISTS shot_group (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE TABLE IF NOT EXISTS shot_group_member (
    group_slug TEXT NOT NULL,
    shot_id INTEGER NOT NULL,
    added_at TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (group_slug, shot_id)
);

CREATE INDEX IF NOT EXISTS idx_shot_group_member_shot
    ON shot_group_member (shot_id);

CREATE INDEX IF NOT EXISTS idx_shot_group_member_added_at
    ON shot_group_member (group_slug, added_at DESC);

CREATE INDEX IF NOT EXISTS idx_shot_group_created_at
    ON shot_group (created_at DESC);
