-- T23 (2026-06-08) — collect chat Q&A as training data for future
-- PersonaAI fine-tune.
--
-- One row per (user prompt, assistant response) pair from /chat. We
-- store both the bare turns AND the surrounding context so a future
-- fine-tune can include conversation memory. Filter on ``rating`` to
-- exclude bad answers from the training set — the UI surfaces 👍/👎
-- buttons that update this column.
--
-- ``provider`` + ``model`` snapshot the model that produced the
-- assistant turn so the dataset can be sliced 'only Gemini answers',
-- 'only Ollama qwen2.5vl:7b', etc.

CREATE TABLE IF NOT EXISTS training_dataset (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id      INTEGER REFERENCES chat_session(id) ON DELETE SET NULL,
    user_message_id INTEGER REFERENCES chat_message(id) ON DELETE SET NULL,
    asst_message_id INTEGER REFERENCES chat_message(id) ON DELETE SET NULL,
    user_text       TEXT NOT NULL,
    assistant_text  TEXT NOT NULL,
    system_prompt   TEXT,
    context_json    TEXT,         -- prior turns as JSON array of {role,content}
    image_present   INTEGER NOT NULL DEFAULT 0,
    provider        TEXT,
    model           TEXT,
    rating          INTEGER NOT NULL DEFAULT 0
                    CHECK (rating IN (-1, 0, 1)),  -- -1 bad, 0 unrated, 1 good
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);

CREATE INDEX IF NOT EXISTS idx_training_dataset_recent
    ON training_dataset(captured_at DESC);
CREATE INDEX IF NOT EXISTS idx_training_dataset_rating
    ON training_dataset(rating);

-- Collection toggle — opt-in. Defaults to 'on' so anyone running this
-- own server starts collecting from day one without an extra step,
-- but the /admin/dataset page can flip it off.
INSERT OR IGNORE INTO kv_settings (key, value)
VALUES ('training_dataset_enabled', '1');
