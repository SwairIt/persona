-- 188_briefing_cards.sql — проактивный брифинг в виде карточек с обратной связью.
-- Раньше брифинг = один текстовый блок в колокольчик. Теперь — 3-5 карточек,
-- каждую можно оценить (👍 полезно / 👎 мимо) и скрыть. Оценки копятся и
-- подмешиваются в будущие брифинги как «избегай такого». Идемпотентно.
CREATE TABLE IF NOT EXISTS briefing_card (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now')),
    slot        TEXT    NOT NULL DEFAULT 'morning',   -- morning | evening
    icon        TEXT    NOT NULL DEFAULT '•',
    title       TEXT    NOT NULL,
    body        TEXT    NOT NULL DEFAULT '',
    feedback    INTEGER NOT NULL DEFAULT 0,           -- 1 полезно / -1 мимо / 0 нет
    dismissed   INTEGER NOT NULL DEFAULT 0            -- 1 = скрыта пользователем
);

-- Горячий путь — лента последних активных карточек.
CREATE INDEX IF NOT EXISTS idx_briefing_card_feed
    ON briefing_card(dismissed, created_at DESC);
