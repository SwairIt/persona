-- T31 E6 — встроенный инструмент install_mcp: модель добавляет и включает
-- MCP-сервер по просьбе пользователя («установи mcp …»). Только регистрирует
-- конфиг в mcp_server, ничего не запускает сама. Включён по умолчанию.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-mcp',
   'Добавить и включить MCP-сервер по просьбе пользователя (name + команда/URL). Появляется в /admin/mcp. Включено по умолчанию.',
   'builtin:install_mcp', 1);
