-- 187_user_memory_bitemporal.sql — bi-temporal память (mem0-стиль).
-- Чтобы факты не копили противоречий («живёт в Москве» + «переехал в Берлин»):
-- при противоречии старый факт НЕ удаляется (hard-delete опасен — можно стереть
-- верное), а SOFT-инвалидируется: valid_until = время, superseded_by = id нового.
-- recall/list берут только актуальные (valid_until IS NULL); история сохраняется
-- и откатывается. created_at играет роль valid_from. Идемпотентно (раннер глотает
-- duplicate column). Без расширений.
ALTER TABLE user_memory ADD COLUMN valid_until TEXT;
ALTER TABLE user_memory ADD COLUMN superseded_by INTEGER;

-- Частичный индекс: горячий путь recall/list — только актуальные факты.
CREATE INDEX IF NOT EXISTS idx_user_memory_active
    ON user_memory(user_id, pinned DESC, id DESC) WHERE valid_until IS NULL;
