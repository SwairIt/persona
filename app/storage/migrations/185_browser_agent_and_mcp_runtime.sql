-- 185_browser_agent_and_mcp_runtime.sql (Phase 2, 2026-06-16)
-- Регистрирует интерактивный браузер-агент (постоянный Playwright-воркер на
-- сессию: open/click/type/read/screenshot/close) как builtin-инструменты,
-- и сидит kv-переключатели новой страницы /settings/automation.
--
-- Браузер-инструменты ВКЛЮЧЕНЫ по умолчанию: навигация безопасна (localhost и
-- RFC1918 жёстко запрещены в коде, есть allow/deny-домены, лимит шагов и idle-TTL).
-- MCP-рантайм по умолчанию ВЫКЛЮЧЕН — он запускает внешние процессы, включается
-- осознанно мастер-переключателем mcp_runtime_enabled.

INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-browser-open',
   'Открыть URL в ПОСТОЯННОМ браузере чата (живёт всю сессию). Кликать/вводить/читать между ходами. localhost и частные сети запрещены.',
   'builtin:browser_open', 1),
  ('builtin-browser-click',
   'Кликнуть по элементу в открытом браузере (CSS-селектор или text=Подпись).',
   'builtin:browser_click', 1),
  ('builtin-browser-type',
   'Ввести текст в поле открытой страницы (можно с Enter для отправки).',
   'builtin:browser_type', 1),
  ('builtin-browser-read',
   'Прочитать видимый текст страницы или элемента (selector). Только чтение.',
   'builtin:browser_read', 1),
  ('builtin-browser-screenshot',
   'Скриншот текущей страницы открытого браузера → файл в workspace + окно активности.',
   'builtin:browser_screenshot', 1),
  ('builtin-browser-close',
   'Закрыть постоянный браузер сессии (освободить Chromium).',
   'builtin:browser_close', 1);

-- kv-настройки автоматизации (значения по умолчанию; страница их перезапишет).
INSERT OR IGNORE INTO kv_settings (key, value, updated_at) VALUES
  ('browser_backend', 'builtin', datetime('now')),
  ('mcp_runtime_enabled', '0', datetime('now')),
  ('browser_allow_domains', '', datetime('now')),
  ('browser_deny_domains', '', datetime('now'));
