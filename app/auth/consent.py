"""Запись согласия на обработку персональных данных (152-ФЗ, ст. 9).

Что тут решается
----------------
Галочка «даю согласие» на ``/auth/signup`` и на лендинге была браузерной:
атрибут ``required`` в HTML, и всё. Сервер её не читал, в БД ничего не
ложилось. Оператор не мог показать проверяющему ни одного доказательства
согласия — и, главное, не мог сказать, под КАКОЙ редакцией политики человек
согласился. Здесь появляется единственная точка правды.

Версия политики
---------------
:data:`POLICY_VERSION` — ОДНО место, где живёт версия. Меняешь текст
``/privacy-policy`` — двигаешь дату здесь, и с этого момента новые регистрации
пишутся под новой версией, а старые строки остаются как история. Отчёт «кто на
что соглашался» — :func:`consent_report`.

ВАЖНО: строковое значение обязано совпадать по смыслу с ``_LEGAL_UPDATED`` в
``app/web/routes/landing.py`` (там оно человекочитаемое: «25 августа 2026»).
Здесь — ISO, чтобы версии сортировались.

Существующие аккаунты
---------------------
Строк у них нет и не будет: задним числом согласие не выдумывается. Отсутствие
строки трактуется как «согласился при доверсионном режиме» — см.
:func:`consent_state`, значение ``pre_versioning``. Это НЕ ошибка и НЕ повод
блокировать вход: до этой миграции сервер физически не мог зафиксировать акт,
и подставлять человеку задним числом «версию 2026-08-25» было бы подлогом.

Честность источника
-------------------
Колонка ``source``:

* ``checkbox``    — поле ``consent`` реально пришло в теле запроса;
* ``form_submit`` — форма отправлена, поля не было. Так регистрируют два
  легаси-пути лендинга (JSON-сабмит ``post('/auth/register', {email})`` и
  вторая CTA-форма внизу страницы) — они не кладут ``consent`` в тело.
  Регистрацию мы им не ломаем, но и согласием это не называем: аудитор
  фильтрует ``source = 'checkbox'``.
"""

from __future__ import annotations

from typing import Any, Literal

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.auth.consent")

#: Версия опубликованной политики/согласия. ISO-дата, сортируемая.
#: Синхронизировать с ``_LEGAL_UPDATED`` в app/web/routes/landing.py.
POLICY_VERSION = "2026-08-25"

#: Что вернёт :func:`consent_state` для аккаунта без единой строки согласия.
PRE_VERSIONING = "pre_versioning"

ConsentSource = Literal["checkbox", "form_submit"]

#: user_agent режем — в журнал не нужен килобайтный заголовок.
_UA_MAX = 200


def _trim(value: str | None, limit: int) -> str | None:
    clean = (value or "").strip()
    if not clean:
        return None
    return clean[:limit]


def client_ip(request: Any) -> str | None:
    """IP клиента с учётом обратного прокси. ``None``, если не определяется.

    Читаем ``X-Forwarded-For`` (первый элемент — исходный клиент) и падаем на
    ``request.client.host``. Значение идёт только в журнал согласия, решений по
    доступу на нём не принимается, поэтому подделываемость заголовка тут не
    угроза, а особенность записи.
    """
    try:
        headers = getattr(request, "headers", {}) or {}
        fwd = headers.get("x-forwarded-for") or headers.get("X-Forwarded-For")
        if fwd:
            first = str(fwd).split(",")[0].strip()
            if first:
                return first[:64]
        client = getattr(request, "client", None)
        host = getattr(client, "host", None)
        return _trim(str(host) if host else None, 64)
    except Exception as exc:  # noqa: BLE001 — журнал не имеет права ронять регистрацию
        log.debug("consent.ip_failed", error=str(exc))
        return None


async def record_consent(
    user_id: int,
    *,
    ip: str | None = None,
    user_agent: str | None = None,
    source: ConsentSource = "checkbox",
    policy_version: str = POLICY_VERSION,
) -> bool:
    """Записать акт согласия. ``False`` — запись не удалась (регистрацию не рушим).

    Никогда не бросает: регистрация пользователя не должна падать из-за журнала.
    Сбой пишется в лог — там его видно, и это лучше, чем 500 на форме signup.
    """
    try:
        async with get_connection() as conn:
            await conn.execute(
                "INSERT INTO user_consent "
                "(user_id, policy_version, ip, user_agent, source) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    int(user_id),
                    str(policy_version),
                    _trim(ip, 64),
                    _trim(user_agent, _UA_MAX),
                    source,
                ),
            )
            await conn.commit()
        log.info(
            "consent.recorded",
            user_id=int(user_id),
            policy_version=policy_version,
            source=source,
        )
        return True
    except Exception as exc:  # noqa: BLE001
        log.warning("consent.record_failed", user_id=int(user_id), error=str(exc))
        return False


async def consent_rows(user_id: int) -> list[dict[str, Any]]:
    """История согласий одного человека, свежие сверху. Для экспорта и /root."""
    try:
        async with get_connection() as conn:
            cur = await conn.execute(
                "SELECT policy_version, consented_at, ip, user_agent, source "
                "FROM user_consent WHERE user_id = ? "
                "ORDER BY consented_at DESC, id DESC",
                (int(user_id),),
            )
            rows = await cur.fetchall()
    except Exception as exc:  # noqa: BLE001 — таблицы может не быть на старой БД
        log.debug("consent.rows_failed", error=str(exc))
        return []
    return [dict(r) for r in rows]


async def consent_state(user_id: int) -> str:
    """Под какой редакцией человек согласился.

    Возвращает версию политики из свежей строки, либо :data:`PRE_VERSIONING`,
    если строк нет — аккаунт создан до появления журнала. Второе НЕ означает
    «не согласен»: до миграции 233 акт было негде зафиксировать.
    """
    rows = await consent_rows(user_id)
    return str(rows[0]["policy_version"]) if rows else PRE_VERSIONING


async def consent_report(policy_version: str = POLICY_VERSION) -> dict[str, int]:
    """Сводка «кто согласился на текущую редакцию» — для отчёта оператора.

    ``total`` — всего активных аккаунтов; ``explicit`` — с галочкой под этой
    версией; ``form_submit`` — регистрации без явной галочки; ``missing`` —
    без единой строки (доверсионный режим).
    """
    out = {"total": 0, "explicit": 0, "form_submit": 0, "missing": 0}
    try:
        async with get_connection() as conn:
            cur = await conn.execute("SELECT COUNT(*) AS n FROM users")
            out["total"] = int((await cur.fetchone())["n"])
            cur = await conn.execute(
                "SELECT source, COUNT(DISTINCT user_id) AS n FROM user_consent "
                "WHERE policy_version = ? GROUP BY source",
                (policy_version,),
            )
            for row in await cur.fetchall():
                key = "explicit" if row["source"] == "checkbox" else "form_submit"
                out[key] += int(row["n"])
            cur = await conn.execute(
                "SELECT COUNT(*) AS n FROM users u "
                "WHERE NOT EXISTS (SELECT 1 FROM user_consent c WHERE c.user_id = u.id)"
            )
            out["missing"] = int((await cur.fetchone())["n"])
    except Exception as exc:  # noqa: BLE001
        log.debug("consent.report_failed", error=str(exc))
    return out


__all__ = [
    "POLICY_VERSION",
    "PRE_VERSIONING",
    "client_ip",
    "consent_report",
    "consent_rows",
    "consent_state",
    "record_consent",
]
