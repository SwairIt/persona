"""Биллинг Persona — модель self-host + Pro-лицензия.

Этот инстанс работает «магазином»: продаёт подписку через ЮKassa и выдаёт
license_key. Чужой self-host валидирует ключ по API и включает Pro-фичи.

Слои:
  plans      — единый источник правды по тарифам/ценам/фичам;
  config     — секреты ЮKassa (env или {data_dir}/billing_secrets.json, не в БД/git);
  yookassa   — httpx-клиент ЮKassa (платёж, рекуррент, статус);
  repo       — CRUD по subscription/payment;
  licensing  — генерация ключа + гейтинг Pro (is_pro/plan_of).
"""
