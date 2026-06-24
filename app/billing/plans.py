"""Тарифы Persona — единый источник правды (цены, циклы, фичи).

Решение по ценам (опора на конкурентов, 2026): рынок облачных AI якорит $20/мес
(в РФ через реселлеров 2000–3500 ₽), GigaChat 600 ₽, Yandex Plus 449 ₽. Pro
позиционируется как «дешевле облачного налога, но не игрушка». Гейтим масштаб и
интеллект — НЕ приватность (она и есть позиционирование).
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class Plan:
    id: str            # 'pro_monthly' | 'pro_yearly'
    plan: str          # 'pro'
    title: str
    cycle: str         # 'monthly' | 'yearly'
    amount: str        # строка для ЮKassa, напр. '690.00'
    currency: str      # 'RUB'
    period_days: int   # длительность оплаченного периода
    badge: str | None = None

    @property
    def amount_rub(self) -> int:
        return int(round(float(self.amount)))


PRO_MONTHLY = Plan(
    id="pro_monthly", plan="pro", title="Persona Pro",
    cycle="monthly", amount="690.00", currency="RUB", period_days=30,
)
PRO_YEARLY = Plan(
    id="pro_yearly", plan="pro", title="Persona Pro",
    cycle="yearly", amount="5900.00", currency="RUB", period_days=365, badge="−29%",
)

PLANS: dict[str, Plan] = {p.id: p for p in (PRO_MONTHLY, PRO_YEARLY)}

# Pro-фичи (для гейтинга и витрины). Ключи совпадают с проверками в коде.
PRO_FEATURES: tuple[str, ...] = (
    "unlimited_history",    # recall по всей истории (free — ~30 дней)
    "vector_recall",        # вектор/гибрид/генеративный recall
    "nightly_reflection",   # ночной воркер-«сон»
    "memory_graph",         # граф памяти
    "fine_tuned_model",     # своя дообученная «вторая копия»
    "multi_device",         # несколько устройств захвата
    "advanced_chat",        # полный набор режимов/инструментов/effort
)

FREE_RECALL_DAYS = 30  # free: индексируем/отдаём recall только за последние N дней


def get_plan(plan_id: str) -> Plan | None:
    return PLANS.get(plan_id)
