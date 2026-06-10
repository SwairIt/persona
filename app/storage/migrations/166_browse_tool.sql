-- T29 (2026-06-10) — built-in web-browsing tool (Playwright headless
-- Chromium + vision analysis). Enabled by default so the model can do
-- "посмотри X в интернете, сделай скрин и проанализируй" out of the box.
-- Needs Playwright + Chromium installed on the server (uv pip install
-- playwright && python -m playwright install chromium).
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-browse',
   'Открыть веб-страницу, сделать скриншот и проанализировать его vision-моделью (qwen2.5vl). Модель сама ходит в интернет, фотографирует страницу и описывает что видит. Включено по умолчанию.',
   'builtin:web_browse', 1);
