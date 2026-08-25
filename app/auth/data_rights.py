"""Порт «права участника на свои данные» — единственная дверь для роутов.

Роут ``app/web/routes/my_data.py`` не имеет права видеть SQL: архитектурный
гейт (``tests/test_architecture_gates.py::
test_legacy_routes_do_not_increase_direct_database_import_debt``) запрещает
новым модулям в ``app/web/routes`` импортировать ``app.storage.db``. Вся
работа с базой живёт в этом пакете:

* :mod:`app.auth.data_export`   — сборка выгрузки (право на доступ);
* :mod:`app.auth.account_delete` — удаление аккаунта (право на удаление);
* этот модуль                    — тонкий фасад: то, что нужно ОДНОМУ экрану.

Тут же — единственное место, где решается, совпало ли подтверждение удаления.
Роут получает готовый ответ, а не сравнивает строки сам: правило «впиши свой
e-mail» — часть доменной логики удаления, а не оформления страницы.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.auth.account_delete import can_delete
from app.auth.consent import POLICY_VERSION, consent_state
from app.auth.data_export import export_counts
from app.storage.db import get_connection

#: Фраза-подтверждение, если у аккаунта почему-то нет адреса.
CONFIRM_PHRASE = "УДАЛИТЬ АККАУНТ"


@dataclass(slots=True)
class DataRightsSummary:
    """Всё, что нужно экрану «Мои данные». Ни одной строки чужих данных."""

    email: str
    counts: dict[str, int]
    consent_state: str
    policy_version: str
    can_delete: bool
    refuse_reason: str | None


async def account_email(user_id: int) -> str:
    """E-mail владельца сессии. Пустая строка, если аккаунта уже нет."""
    async with get_connection() as conn:
        cur = await conn.execute("SELECT email FROM users WHERE id = ?", (int(user_id),))
        row = await cur.fetchone()
    return str(row["email"]) if row else ""


async def summary(user_id: int) -> DataRightsSummary:
    """Собрать сводку экрана: адрес, счётчики, состояние согласия, гард удаления."""
    uid = int(user_id)
    deletable, reason = await can_delete(uid)
    return DataRightsSummary(
        email=await account_email(uid),
        counts=await export_counts(uid),
        consent_state=await consent_state(uid),
        policy_version=POLICY_VERSION,
        can_delete=deletable,
        refuse_reason=reason,
    )


async def confirmation_matches(user_id: int, supplied: str) -> bool:
    """Совпало ли подтверждение удаления с собственным адресом пользователя.

    Сравнение регистронезависимое: адрес в базе уже нормализован в нижний
    регистр (``app.auth.users.normalise_email``), а человек печатает как
    печатается. Если адреса нет — принимается :data:`CONFIRM_PHRASE`.
    """
    expected = (await account_email(user_id)).strip().lower() or CONFIRM_PHRASE.lower()
    return (supplied or "").strip().lower() == expected


def summary_context(data: DataRightsSummary, refusals: dict[str, str]) -> dict[str, Any]:
    """Разложить сводку в контекст шаблона (роут только рендерит)."""
    return {
        "email": data.email,
        "counts": data.counts,
        "consent_state": data.consent_state,
        "policy_version": data.policy_version,
        "can_delete": data.can_delete,
        "delete_blocked_reason": refusals.get(data.refuse_reason or "", ""),
        "confirm_phrase": CONFIRM_PHRASE,
    }


__all__ = [
    "CONFIRM_PHRASE",
    "DataRightsSummary",
    "account_email",
    "confirmation_matches",
    "summary",
    "summary_context",
]
