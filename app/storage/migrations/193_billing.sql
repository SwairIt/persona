-- 193_billing.sql — подписки, платежи, лицензии (биллинг, фаза 1).
--
-- Модель: self-host + Pro-лицензия. Этот инстанс = «магазин»: продаёт подписку
-- через ЮKassa и выдаёт license_key, который чужой self-host валидирует по API.
-- Три сущности:
--   subscription — договор подписки на billing-пользователя (+ его license_key);
--   payment      — лог транзакций ЮKassa (дедуп по provider_payment_id);
--   license_check — журнал валидаций ключа (анти-абьюз, опционально).
-- Всё идемпотентно: CREATE TABLE/INDEX IF NOT EXISTS, повторный прогон безопасен.

CREATE TABLE IF NOT EXISTS subscription (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id              INTEGER NOT NULL UNIQUE,
    plan                 TEXT NOT NULL DEFAULT 'free',      -- 'free' | 'pro'
    billing_cycle        TEXT,                              -- 'monthly' | 'yearly' | NULL
    status               TEXT NOT NULL DEFAULT 'inactive',  -- inactive|pending|active|past_due|canceled
    provider             TEXT NOT NULL DEFAULT 'yookassa',
    provider_method_id   TEXT,                              -- ЮKassa saved payment_method.id для рекуррента
    license_key          TEXT UNIQUE,                       -- ключ, который клиент вставляет в свой self-host
    amount               TEXT,                              -- сумма периодического списания, напр. '690.00'
    currency             TEXT NOT NULL DEFAULT 'RUB',
    current_period_start TEXT,
    current_period_end   TEXT,                              -- Pro действует до этого момента (ISO-8601 UTC, naive)
    cancel_at_period_end INTEGER NOT NULL DEFAULT 0,        -- 1 = не продлевать
    created_at           TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at           TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

-- планировщик ищет активные подписки с истекающим периодом для автосписания
CREATE INDEX IF NOT EXISTS idx_subscription_renew
    ON subscription(status, current_period_end);
-- быстрый lookup при валидации лицензии
CREATE INDEX IF NOT EXISTS idx_subscription_license
    ON subscription(license_key);

CREATE TABLE IF NOT EXISTS payment (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id             INTEGER NOT NULL,
    subscription_id     INTEGER,
    provider            TEXT NOT NULL DEFAULT 'yookassa',
    provider_payment_id TEXT UNIQUE,                        -- ЮKassa payment.id (дедуп вебхуков)
    idempotence_key     TEXT,                               -- ключ, отправленный в ЮKassa
    kind                TEXT NOT NULL DEFAULT 'initial',    -- initial | recurring | refund
    amount              TEXT NOT NULL,
    currency            TEXT NOT NULL DEFAULT 'RUB',
    status              TEXT NOT NULL DEFAULT 'pending',     -- pending|succeeded|canceled|refunded
    period_start        TEXT,
    period_end          TEXT,
    fiscalized          INTEGER NOT NULL DEFAULT 0,          -- чек НПД в «Мой налог» зарегистрирован? (самозанятый)
    description         TEXT,
    created_at          TEXT NOT NULL DEFAULT (datetime('now')),
    updated_at          TEXT NOT NULL DEFAULT (datetime('now')),
    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE,
    FOREIGN KEY (subscription_id) REFERENCES subscription(id) ON DELETE SET NULL
);

CREATE INDEX IF NOT EXISTS idx_payment_user ON payment(user_id, created_at DESC);
-- список платежей, по которым самозанятому надо выбить чек в «Мой налог»
CREATE INDEX IF NOT EXISTS idx_payment_unfiscalized ON payment(fiscalized, status) WHERE fiscalized = 0;
