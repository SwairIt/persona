-- T31 E4 — встроенный инструмент run_mac: модель выполняет команды
-- (bash/zsh/powershell) на Mac пользователя через агента (op=exec в
-- agent_fs_command). Требует включённого mac-fs и онлайн-агента — иначе
-- инструмент сам возвращает [error] (безопасно). Включён по умолчанию,
-- но реально работает только когда пользователь включил mac-fs.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-run-mac',
   'Выполнять команды на Mac (bash/zsh/powershell) через агента. Работает только при включённом mac-fs.',
   'builtin:run_mac', 1);
