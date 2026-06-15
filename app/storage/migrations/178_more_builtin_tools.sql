-- 178_more_builtin_tools.sql — Phase 1.2: расширенный каталог builtin-инструментов.
-- Чтения/поиск/сеть — включены по умолчанию; правка файлов и запуск тестов —
-- выключены (пользователь включает в /admin/mcp), как write_file/run_shell.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-edit', 'Точечная правка файла (edit_file): заменить фрагмент, не перезаписывая весь файл. Безопаснее write_file. Включи для редактирования кода.',
   'builtin:edit_file', 0),
  ('builtin-multiedit', 'Несколько правок в одном файле за раз (multi_edit).',
   'builtin:multi_edit', 0),
  ('builtin-readmany', 'Прочитать несколько файлов сразу (read_many). Только чтение, безопасно.',
   'builtin:read_many', 1),
  ('builtin-find', 'Найти файлы по glob-маске в workspace (find_files). Только чтение.',
   'builtin:find_files', 1),
  ('builtin-grep', 'Поиск текста/regex по файлам (search_code). Только чтение.',
   'builtin:search_code', 1),
  ('builtin-fetch', 'HTTP-запрос к API (fetch_json). Только http(s), без локальной сети.',
   'builtin:fetch_json', 1),
  ('builtin-websearch', 'Поиск в интернете через Brave (web_search). Нужен ключ.',
   'builtin:web_search', 1),
  ('builtin-runtests', 'Запуск тестов (run_tests) на устройстве/сервере. Включи осознанно.',
   'builtin:run_tests', 0),
  ('builtin-querymemory', 'Осознанный поиск по памяти всех чатов (query_memory). Только чтение.',
   'builtin:query_memory', 1);
