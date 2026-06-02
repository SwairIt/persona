-- Reusable note templates — user picks one and the body is pasted into a new note.
CREATE TABLE IF NOT EXISTS note_template (
    slug TEXT PRIMARY KEY,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT (datetime('now'))
);

INSERT OR IGNORE INTO note_template (slug, title, body) VALUES
    ('standup', 'Daily standup', '## Yesterday' || char(10) || '- ' || char(10) || char(10) || '## Today' || char(10) || '- ' || char(10) || char(10) || '## Blockers' || char(10) || '- '),
    ('meeting', 'Meeting / 1:1', '## Attendees' || char(10) || '- ' || char(10) || char(10) || '## Topics' || char(10) || '- ' || char(10) || char(10) || '## Action items' || char(10) || '- [ ] '),
    ('bug', 'Bug investigation', '## Symptom' || char(10) || char(10) || '## Repro' || char(10) || '1. ' || char(10) || char(10) || '## Hypothesis' || char(10) || char(10) || '## Root cause' || char(10) || char(10) || '## Fix');
