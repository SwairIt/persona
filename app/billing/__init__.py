"""Биллинг Persona — модель self-host + Pro-лицензия.

Этот инстанс работает «магазином»: продаёт подписку через ЮKassa и выдаёт
license_key. Чужой self-host валидирует ключ по API и включает Pro-фичи.

Слои:
  plans      — единый источник правды по тарифам/ценам/фичам;
  config     — секреты провайдеров (env или {data_dir}/billing_secrets.json,
               не в БД/git) + переключатели billing_enabled / payment_provider;
  yookassa   — httpx-клиент ЮKassa (платёж, рекуррент, статус);
  robokassa  — подписи, ссылка на оплату, ResultURL/SuccessURL, чек 54-ФЗ;
  repo       — CRUD по subscription/payment;
  licensing  — генерация ключа + гейтинг Pro (is_pro/plan_of).

ВАЖНО: продажи выключены по умолчанию. Витрина, кабинет и /billing/checkout
оживают только когда владелец руками поднимет kv ``billing_enabled=1`` И выберет
настроенного провайдера в kv ``payment_provider``. Подробности и чек-лист
запуска — docs/BILLING_ROBOKASSA.md.
"""
