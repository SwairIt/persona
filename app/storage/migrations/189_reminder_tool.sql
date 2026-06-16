-- 189_reminder_tool.sql — builtin-инструмент schedule_reminder (ROADMAP S3b-2).
-- Включён по умолчанию: пишет только в локальную таблицу reminders (без exec/сети),
-- безопасно. Даёт ассистенту планировать задачи естественным языком прямо в чате
-- («напомни завтра …»). Идемпотентно (INSERT OR IGNORE).
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-reminder',
   'Создать напоминание/задачу из естественного языка (schedule_reminder). Пишет в локальные напоминания, безопасно.',
   'builtin:schedule_reminder', 1);
