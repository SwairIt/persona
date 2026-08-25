-- 232_billing_robokassa.sql — Robokassa рядом с ЮKassa (второй провайдер).
--
-- Robokassa не выдаёт «payment id»: единственный идентификатор операции — наш
-- собственный номер заказа InvId (целое, уникальное в пределах магазина). Его
-- Robokassa возвращает в ResultURL/SuccessURL, и именно по нему делается
-- идемпотентность: повторное уведомление по тому же InvId не должно выдать
-- второй оплаченный период.
--
-- Всё остальное переиспользуем как есть:
--   payment.provider            — 'robokassa' | 'yookassa' (колонка уже была);
--   payment.provider_payment_id — для Robokassa кладём 'robokassa:<InvId>',
--                                 чтобы UNIQUE-дедуп и claim_receipt работали
--                                 без второй ветки кода;
--   subscription.provider_method_id — для рекуррента Robokassa сюда ляжет InvId
--                                 родительского платежа (когда рекуррент включат).
--
-- Идемпотентно: ALTER ... ADD COLUMN на существующей колонке гасится раннером
-- («duplicate column name»), индекс — IF NOT EXISTS.

ALTER TABLE payment ADD COLUMN inv_id INTEGER;

-- Уникальность только среди заполненных значений: у платежей ЮKassa inv_id NULL,
-- а NULL в SQLite не конфликтует сам с собой даже без WHERE — фильтр оставлен
-- явным, чтобы намерение читалось.
CREATE UNIQUE INDEX IF NOT EXISTS idx_payment_inv_id
    ON payment(inv_id) WHERE inv_id IS NOT NULL;
