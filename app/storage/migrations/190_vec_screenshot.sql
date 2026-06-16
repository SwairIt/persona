-- 190_vec_screenshot.sql — vec0-ускорение поиска по скриншотам (ROADMAP S4b), ОПЦИОНАЛЬНО.
-- Унифицирует слой эмбеддингов скриншотов с chat_message_vec (мигр.186): KNN считается
-- в SQL через sqlite-vec, а не перебором всех BLOB в Python. Если расширение sqlite-vec
-- НЕ установлено — CREATE VIRTUAL TABLE USING vec0 падает с 'no such module' и тихо
-- пропускается раннером; поиск тогда работает по-старому (полный перебор cosine).
--
-- Размерность 384 = intfloat/multilingual-e5-small (дефолтная embeddings_model).
-- Источник истины эмбеддингов остаётся screenshot_embeddings (BLOB); vec0 — ускоряющий
-- индекс-зеркало, наполняется идемпотентным backfill по vec_screenshot_meta.
CREATE VIRTUAL TABLE IF NOT EXISTS screenshot_vec USING vec0(
    screenshot_id INTEGER PRIMARY KEY,
    embedding FLOAT[384]
);

-- Маркер «уже в vec0» (обычная таблица — создаётся всегда, даже без sqlite-vec,
-- чтобы backfill знал что докинуть, когда расширение появится).
CREATE TABLE IF NOT EXISTS vec_screenshot_meta (
    screenshot_id INTEGER PRIMARY KEY,
    captured_at TEXT,
    app_name TEXT
);
CREATE INDEX IF NOT EXISTS idx_vec_shot_captured ON vec_screenshot_meta(captured_at);
