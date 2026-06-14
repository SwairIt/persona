-- Инструмент удаления файла/папки на Mac (delete_path). Модели часто просят
-- «удали …» и выдумывают rm_dir/rm — теперь есть реальный инструмент с
-- защитой пути. Включён по умолчанию (реально удаляет только при mac-fs +
-- онлайн-агенте, либо в локальном workspace).
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-delete',
   'Удалить файл или папку (рекурсивно) на Mac / в workspace.',
   'builtin:delete_path', 1);
