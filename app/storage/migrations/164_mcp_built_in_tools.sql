-- T25 (2026-06-08) — built-in Persona tools (Python-native) + clearer
-- descriptions for MCP servers. Built-in tools have command="builtin:NAME"
-- so the runtime knows to dispatch internally instead of spawning npm.

-- Replace placeholder npm-based defaults with clearer descriptions.
UPDATE mcp_server SET description = 'Дать модели читать и писать файлы на диске D:\Projects. Можно подменить путь.'
WHERE name = 'filesystem';
UPDATE mcp_server SET description = 'Выполнять команды cmd/PowerShell. ОПАСНО — может удалить файлы или установить malware. Включай только если доверяешь модели.'
WHERE name = 'shell';
UPDATE mcp_server SET description = 'Дать модели смотреть git: статус, лог, diff. Безопасно (только чтение).'
WHERE name = 'git';
UPDATE mcp_server SET description = 'Модель ведёт свою долговременную память — заметки между чатами, граф знаний.'
WHERE name = 'memory';
UPDATE mcp_server SET description = 'Поиск в интернете через Brave Search. Требует ключ BRAVE_API_KEY (можно бесплатный).'
WHERE name = 'brave-search';

-- Built-in Persona tools — работают БЕЗ Node.js, БЕЗ npm. Включаешь —
-- модель сразу может ими пользоваться.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-read', 'Встроенное чтение файлов. Модель может прочитать любой текстовый файл на сервере (txt, py, md, json, и т.д.). Безопасно, только чтение.',
   'builtin:read_file', 1),
  ('builtin-list', 'Встроенный листинг папок. Модель видит что лежит в директории. Безопасно.',
   'builtin:list_dir', 1),
  ('builtin-write', 'Встроенная запись файлов. Модель может создать или перезаписать файл. Включай если хочешь чтобы она писала код или заметки сама.',
   'builtin:write_file', 0),
  ('builtin-shell', 'Встроенное выполнение PowerShell. ОПАСНО — модель может удалить файлы, поставить программы. Включай только если очень доверяешь и понимаешь риски.',
   'builtin:run_shell', 0),
  ('builtin-git', 'Встроенные git-команды: status, log, diff. Только чтение, безопасно.',
   'builtin:git_status', 0);
