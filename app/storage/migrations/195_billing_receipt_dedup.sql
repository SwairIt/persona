-- 195_billing_receipt_dedup.sql — флаг «письмо об оплате уже отправлено».
-- Гонка двух почти одновременных вебхуков ЮKassa (она ретраит) давала ДУБЛЬ
-- письма «Оплата получена»: проверка already_succeeded и отправка письма были
-- неатомарны (разные соединения, без write-lock). Теперь письмо шлётся только
-- если ИМЕННО этот вызов атомарно «застолбил» флаг:
--   UPDATE payment SET receipt_sent=1 WHERE provider_payment_id=? AND receipt_sent=0
-- (rowcount==1 у победителя гонки; SQLite сериализует писателей → ровно один раз).
-- Финансовая часть и так идемпотентна — это закрывает только дубль письма.
-- Идемпотентно (раннер глотает duplicate column при повторном прогоне).
ALTER TABLE payment ADD COLUMN receipt_sent INTEGER NOT NULL DEFAULT 0;
