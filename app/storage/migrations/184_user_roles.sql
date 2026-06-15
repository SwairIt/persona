-- 184_user_roles.sql — фундамент ролей (АДДИТИВНО, без смены поведения).
-- Добавляет users.role/status. auth_gate НЕ меняется на этом этапе: owner-gate
-- продолжает работать как раньше (владелец видит всё, остальные — /pending).
-- Колонки нужны для будущего управления пользователями в /root и для read-only
-- списка уже сейчас. Идемпотентно: повторный ALTER ADD COLUMN раннер глотает.
ALTER TABLE users ADD COLUMN role TEXT NOT NULL DEFAULT 'member';
ALTER TABLE users ADD COLUMN status TEXT NOT NULL DEFAULT 'active';

-- Backfill: владелец (kv owner_user_id, иначе минимальный id) → owner/active.
UPDATE users SET role = 'owner', status = 'active'
WHERE id = COALESCE(
    (SELECT CAST(value AS INTEGER) FROM kv_settings
       WHERE key = 'owner_user_id' AND value GLOB '[0-9]*'),
    (SELECT MIN(id) FROM users)
);

CREATE INDEX IF NOT EXISTS idx_users_role ON users(role);
CREATE INDEX IF NOT EXISTS idx_users_status ON users(status);
