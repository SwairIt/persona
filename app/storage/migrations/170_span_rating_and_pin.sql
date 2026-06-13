-- T29 MVP шаг4 — (A) оценка выделенного фрагмента ответа + (B) пин сообщений.

-- A. Span-level rating: пользователь выделил слово/фразу в ответе и
-- лайкнул/дизлайкнул именно её — понятно ЧТО смутило. Обогащает датасет.
CREATE TABLE IF NOT EXISTS training_dataset_span_rating (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    asst_message_id INTEGER REFERENCES chat_message(id) ON DELETE CASCADE,
    session_id      INTEGER,
    selected_text   TEXT NOT NULL,
    rating          INTEGER NOT NULL DEFAULT 0 CHECK (rating IN (-1, 0, 1)),
    captured_at     TEXT NOT NULL DEFAULT (datetime('now'))
);
CREATE INDEX IF NOT EXISTS idx_span_rating_msg ON training_dataset_span_rating(asst_message_id);

-- B. Pin: держать выбранные сообщения в контексте даже после обрезки истории.
ALTER TABLE chat_message ADD COLUMN is_pinned INTEGER NOT NULL DEFAULT 0;
