-- 233_user_consent.sql — доказуемость согласия (152-ФЗ, ст. 9 ч. 4) и журнал
-- самостоятельного удаления аккаунта (ст. 14 ч. 1, ст. 21).
--
-- ЗАЧЕМ. До этой миграции галочка «даю согласие на обработку ПДн» на
-- /auth/signup и на лендинге была ТОЛЬКО браузерной (`required` в HTML):
-- сервер её не читал и нигде не фиксировал. Значит оператор не мог показать
-- проверяющему НИ ОДНОГО доказательства, что конкретный человек согласие дал,
-- и — что важнее — под какой РЕДАКЦИЕЙ политики он его дал.
--
-- user_consent
-- ------------
-- Одна строка на акт согласия. Строк у пользователя может быть несколько:
-- при смене редакции политики пишется новая, старые остаются как история.
--   * policy_version — версия документа (см. app/auth/consent.POLICY_VERSION);
--   * ip / user_agent — техническое подтверждение обстоятельств акта;
--   * source         — ЧЕСТНЫЙ признак канала: 'checkbox' = галочка реально
--                      пришла на сервер; 'form_submit' = форма отправлена, но
--                      поля consent в теле не было (легаси-путь лендинга).
--                      Аудитор фильтрует по source='checkbox'.
--
-- ON DELETE CASCADE тут НАМЕРЕННО: ip + user_agent — это персональные данные
-- самого субъекта, и при реализации права на удаление они обязаны уйти вместе
-- с аккаунтом. Факт удаления доказывается отдельной таблицей ниже, где нет
-- ничего, кроме id и времени.
CREATE TABLE IF NOT EXISTS user_consent (
    id             INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id        INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    policy_version TEXT    NOT NULL,
    consented_at   TEXT    NOT NULL DEFAULT (datetime('now')),
    ip             TEXT,
    user_agent     TEXT,
    source         TEXT    NOT NULL DEFAULT 'checkbox'
);

-- «Кто и когда согласился» — история одного человека.
CREATE INDEX IF NOT EXISTS idx_user_consent_user
    ON user_consent(user_id, consented_at);

-- «Кто согласился на редакцию X» — отчёт при смене политики.
CREATE INDEX IF NOT EXISTS idx_user_consent_version
    ON user_consent(policy_version);

-- account_deletion_log
-- --------------------
-- Доказательство ИСПОЛНЕНИЯ права на удаление. НАМЕРЕННО без FK на users:
-- строка обязана пережить удаление самой строки users, иначе доказывать нечего.
-- Содержимого тут нет и быть не должно — только id, время и счётчики, чтобы
-- журнал сам не стал хранилищем персональных данных удалённого человека.
CREATE TABLE IF NOT EXISTS account_deletion_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id       INTEGER NOT NULL,
    deleted_at    TEXT    NOT NULL DEFAULT (datetime('now')),
    initiated_by  TEXT    NOT NULL DEFAULT 'self',
    rows_deleted  INTEGER NOT NULL DEFAULT 0,
    kv_keys_deleted INTEGER NOT NULL DEFAULT 0
);

CREATE INDEX IF NOT EXISTS idx_account_deletion_log_at
    ON account_deletion_log(deleted_at);
