-- T29 (2026-06-10) — installable "skills": instruction sets the user
-- pulls from a GitHub repo (SKILL.md / README.md) that augment the chat
-- system prompt. Instructions ONLY — no code is executed — so installing
-- from an arbitrary repo is safe.
CREATE TABLE IF NOT EXISTS skill (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id     INTEGER NOT NULL DEFAULT 0,
    name        TEXT    NOT NULL,
    source_url  TEXT,
    content     TEXT    NOT NULL,
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT    NOT NULL DEFAULT (datetime('now'))
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_skill_user_name ON skill(user_id, name);

-- Built-in tool so the model can install a skill when the user says
-- "установи скилл <ссылка>". Enabled by default.
INSERT OR IGNORE INTO mcp_server (name, description, command, enabled) VALUES
  ('builtin-skill',
   'Установить «навык» (skill) из GitHub-репозитория: модель скачивает SKILL.md/README.md по ссылке и начинает применять эти инструкции. Безопасно — код не выполняется, только текст-инструкции. Включено по умолчанию.',
   'builtin:install_skill', 1);
