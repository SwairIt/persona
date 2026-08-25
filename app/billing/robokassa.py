"""Robokassa: ссылка на оплату, подписи, ResultURL/SuccessURL, чек 54-ФЗ.

Реализовано строго по официальной документации (docs.robokassa.ru, раздел
«Интерфейс оплаты» / «Уведомления и переадресация» / «Фискализация»). Где
документация молчит — здесь не догадка, а явный отказ (см. :func:`charge_recurring`).

Модель отличается от ЮKassa: у Robokassa НЕТ серверного API создания платежа.
Мы формируем подписанную ССЫЛКУ, пользователь уходит на неё, а обратно приходят
три разных обращения:

``ResultURL``  (сервер→сервер)  подпись на **Пароле #2** — ЕДИНСТВЕННЫЙ источник
                               правды: только он открывает доступ. Ответ должен
                               быть ровно ``OK<InvId>``, иначе Robokassa ретраит.
``SuccessURL`` (браузер юзера)  подпись на **Пароле #1** — только «спасибо».
``FailURL``    (браузер юзера)  без подписи — только «не получилось».

Формулы подписи (взяты из документации, регистр букв в хеше не важен —
сравниваем без учёта регистра):

* запрос      ``MerchantLogin:OutSum:InvId[:Receipt]:Пароль#1[:Shp_...]``
* ResultURL   ``OutSum:InvId:Пароль#2[:Shp_...]``
* Success     ``OutSum:InvId:Пароль#1[:Shp_...]``

``Shp_*``-параметры ВСЕГДА идут после пароля и сортируются по имени
(строго по алфавиту), в виде ``:Shp_key=value``. Именно через них мы протаскиваем
``Shp_uid`` (наш user_id) и ``Shp_plan`` (id тарифа) — Robokassa вернёт их
дословно во всех трёх обращениях.

``Receipt`` (чек 54-ФЗ) участвует в подписи запроса В ТОМ ЖЕ url-encoded виде,
в каком уходит в ссылке — поэтому кодируем ровно один раз и переиспользуем
и в подписи, и в query (см. :func:`build_payment_link`).
"""

from __future__ import annotations

import hashlib
import hmac
import json
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import Any, Mapping
from urllib.parse import quote

from app.billing.config import RobokassaCredentials, get_robokassa_credentials

PAYMENT_URL = "https://auth.robokassa.ru/Merchant/Index.aspx"
RECURRING_URL = "https://auth.robokassa.ru/Merchant/Recurring"

# IP, с которых Robokassa шлёт ResultURL (из раздела «Уведомления и
# переадресация»). ДОПОЛНИТЕЛЬНАЯ проверка, НИКОГДА не единственная: адреса
# провайдер меняет без предупреждения, а подпись на Пароле #2 — настоящая
# гарантия подлинности. По умолчанию фильтр ВЫКЛЮЧЕН (см. роут).
RESULT_IP_ALLOWLIST: tuple[str, ...] = ("185.59.216.65/32", "185.59.217.65/32")

_SHP_PREFIX = "shp_"


class RobokassaError(RuntimeError):
    """Ошибка конфигурации или протокола Robokassa."""


# --------------------------------------------------------------------- подписи

def digest(payload: str, algo: str) -> str:
    """Хеш строки подписи в нижнем регистре hex.

    Алгоритм берётся из технастроек магазина (md5 по умолчанию у Robokassa,
    доступны sha1/sha256/sha384/sha512/ripemd160). Robokassa сравнивает подпись
    без учёта регистра; мы наружу отдаём lower-hex, внутрь сравниваем через
    :func:`hmac.compare_digest`.
    """
    name = (algo or "").strip().lower().replace("-", "")
    try:
        return hashlib.new(name, payload.encode("utf-8")).hexdigest().lower()
    except ValueError as exc:
        raise RobokassaError(f"неподдерживаемый алгоритм подписи: {algo}") from exc


def shp_tail(shp: Mapping[str, Any] | None) -> str:
    """``:Shp_a=1:Shp_b=2`` — пользовательские параметры, отсортированные по имени.

    Пустой словарь (и ``None``) дают пустую строку, чтобы формула подписи без
    Shp-параметров совпадала с документационной ``MerchantLogin:OutSum:InvId:Пароль#1``.
    """
    if not shp:
        return ""
    items = sorted((str(k), "" if v is None else str(v)) for k, v in shp.items())
    return "".join(f":{k}={v}" for k, v in items)


def collect_shp(params: Mapping[str, Any]) -> dict[str, str]:
    """Выбрать из входящего запроса только ``Shp_*`` (регистр префикса не важен)."""
    return {
        str(k): "" if v is None else str(v)
        for k, v in params.items()
        if str(k).lower().startswith(_SHP_PREFIX)
    }


def request_signature(
    *,
    login: str,
    out_sum: str,
    inv_id: str | int | None,
    password1: str,
    receipt: str | None = None,
    shp: Mapping[str, Any] | None = None,
    algo: str,
) -> str:
    """Подпись ссылки на оплату.

    ``MerchantLogin:OutSum:InvId[:Receipt]:Пароль#1[:Shp_...]``

    ``receipt`` — УЖЕ url-encoded строка чека (как она уйдёт в ссылке) или
    ``None``. ``inv_id=None`` даёт пустой слот (``OutSum::Пароль#1``) — так
    документация предписывает подписывать платёж без номера заказа.
    """
    parts = [login, out_sum, "" if inv_id is None else str(inv_id)]
    if receipt is not None:
        parts.append(receipt)
    parts.append(password1)
    return digest(":".join(parts) + shp_tail(shp), algo)


def result_signature(
    *,
    out_sum: str,
    inv_id: str | int,
    password2: str,
    shp: Mapping[str, Any] | None = None,
    algo: str,
) -> str:
    """Подпись ResultURL: ``OutSum:InvId:Пароль#2[:Shp_...]``."""
    return digest(f"{out_sum}:{inv_id}:{password2}" + shp_tail(shp), algo)


def success_signature(
    *,
    out_sum: str,
    inv_id: str | int,
    password1: str,
    shp: Mapping[str, Any] | None = None,
    algo: str,
) -> str:
    """Подпись SuccessURL: ``OutSum:InvId:Пароль#1[:Shp_...]``."""
    return digest(f"{out_sum}:{inv_id}:{password1}" + shp_tail(shp), algo)


def _equal(a: str, b: str) -> bool:
    return hmac.compare_digest((a or "").strip().lower(), (b or "").strip().lower())


def verify_result(
    params: Mapping[str, Any], creds: RobokassaCredentials | None = None
) -> bool:
    """Проверить подпись уведомления ResultURL (Пароль #2).

    ``params`` — форма/квери как пришли. Shp-параметры берём ИЗ ЗАПРОСА (их
    состав задаём мы сами при создании ссылки, Robokassa возвращает дословно).
    """
    creds = creds or require_credentials()
    supplied = str(params.get("SignatureValue") or params.get("signaturevalue") or "")
    out_sum = str(params.get("OutSum") or params.get("outSum") or "")
    inv_id = str(params.get("InvId") or params.get("invId") or "")
    if not supplied or not out_sum or not inv_id:
        return False
    expected = result_signature(
        out_sum=out_sum,
        inv_id=inv_id,
        password2=creds.password2,
        shp=collect_shp(params),
        algo=creds.hash_algo,
    )
    return _equal(supplied, expected)


def verify_success(
    params: Mapping[str, Any], creds: RobokassaCredentials | None = None
) -> bool:
    """Проверить подпись SuccessURL (Пароль #1). Доступ НЕ выдаёт — только UI."""
    creds = creds or require_credentials()
    supplied = str(params.get("SignatureValue") or params.get("signaturevalue") or "")
    out_sum = str(params.get("OutSum") or params.get("outSum") or "")
    inv_id = str(params.get("InvId") or params.get("invId") or "")
    if not supplied or not out_sum or not inv_id:
        return False
    expected = success_signature(
        out_sum=out_sum,
        inv_id=inv_id,
        password1=creds.password1,
        shp=collect_shp(params),
        algo=creds.hash_algo,
    )
    return _equal(supplied, expected)


# ----------------------------------------------------------------------- сумма

def format_amount(amount: str | Decimal | float) -> str:
    """``690`` / ``690.0`` / ``Decimal('690')`` → ``'690.00'`` (формат Robokassa)."""
    try:
        value = Decimal(str(amount))
    except (InvalidOperation, ValueError) as exc:
        raise RobokassaError(f"некорректная сумма: {amount!r}") from exc
    return f"{value:.2f}"


def amounts_match(a: str | Decimal | float, b: str | Decimal | float) -> bool:
    """Сравнение сумм по значению (``'690.00' == '690'``), а не по строке."""
    try:
        return Decimal(str(a)) == Decimal(str(b))
    except (InvalidOperation, ValueError):
        return False


# ------------------------------------------------------------------ чек 54-ФЗ

def build_receipt(
    *,
    name: str,
    amount: str,
    quantity: int = 1,
    sno: str = "usn_income",
    tax: str = "none",
    payment_method: str = "full_payment",
    payment_object: str = "service",
) -> str:
    """Минифицированный JSON чека для параметра ``Receipt`` (ДО url-encode).

    Поля — из раздела «Фискализация»: ``sno`` (система налогообложения) и
    ``items[]`` c ``name``/``quantity``/``sum``/``tax``/``payment_method``/
    ``payment_object``. Имя позиции режем до 128 символов, как требует документация.

    ВНИМАНИЕ (не техническое решение, а юридическое): нужен ли чек вообще и КТО
    его выбивает — зависит от статуса продавца (ИП / ООО / самозанятый) и от
    того, подключена ли у него онлайн-касса Robokassa. Самозанятый чек НПД
    выбивает сам в «Мой налог». Поэтому отправка чека выключена по умолчанию
    (``PERSONA_ROBOKASSA_RECEIPT_ENABLED``) — включать осознанно.
    """
    receipt: dict[str, Any] = {
        "items": [
            {
                "name": name[:128],
                "quantity": quantity,
                "sum": float(Decimal(format_amount(amount))),
                "tax": tax,
                "payment_method": payment_method,
                "payment_object": payment_object,
            }
        ]
    }
    if sno:
        receipt["sno"] = sno
    return json.dumps(receipt, ensure_ascii=False, separators=(",", ":"))


def encode_receipt(receipt_json: str) -> str:
    """url-encode чека. Ровно эта строка идёт И в подпись, И в ссылку."""
    return quote(receipt_json, safe="")


# ------------------------------------------------------------------- ссылка

@dataclass(frozen=True)
class PaymentLink:
    url: str
    inv_id: int
    out_sum: str


def require_credentials() -> RobokassaCredentials:
    creds = get_robokassa_credentials()
    if creds is None:
        raise RobokassaError("Robokassa не настроена (нет login/паролей)")
    return creds


def build_payment_link(
    *,
    inv_id: int,
    amount: str,
    description: str,
    email: str | None = None,
    shp: Mapping[str, Any] | None = None,
    recurring: bool = False,
    creds: RobokassaCredentials | None = None,
) -> PaymentLink:
    """Собрать подписанную ссылку на оплату.

    ``recurring=True`` помечает платёж как «родительский» для будущих списаний.
    Флаг НЕ участвует в подписи (в документации он не входит в список
    модификаторов). Сервис его сегодня не ставит — см. :func:`charge_recurring`.
    """
    creds = creds or require_credentials()
    out_sum = format_amount(amount)
    shp = dict(shp or {})

    receipt_encoded: str | None = None
    if creds.receipt_enabled:
        receipt_encoded = encode_receipt(
            build_receipt(
                name=description,
                amount=out_sum,
                sno=creds.sno,
                tax=creds.tax,
            )
        )

    signature = request_signature(
        login=creds.login,
        out_sum=out_sum,
        inv_id=inv_id,
        password1=creds.password1,
        receipt=receipt_encoded,
        shp=shp,
        algo=creds.hash_algo,
    )

    # Собираем query РУКАМИ: Receipt уже закодирован ровно тем же вызовом, что
    # ушёл в подпись — повторный encode рассинхронизировал бы их.
    pairs: list[tuple[str, str]] = [
        ("MerchantLogin", quote(creds.login, safe="")),
        ("OutSum", out_sum),
        ("InvId", str(inv_id)),
        ("Description", quote(description[:100], safe="")),
        ("SignatureValue", signature),
        ("Culture", "ru"),
        ("Encoding", "utf-8"),
    ]
    if receipt_encoded is not None:
        pairs.append(("Receipt", receipt_encoded))
    if email:
        pairs.append(("Email", quote(email, safe="")))
    if recurring:
        pairs.append(("Recurring", "true"))
    if creds.is_test:
        pairs.append(("IsTest", "1"))
    for key in sorted(shp):
        pairs.append((quote(str(key), safe=""), quote(str(shp[key]), safe="")))

    query = "&".join(f"{k}={v}" for k, v in pairs)
    return PaymentLink(url=f"{PAYMENT_URL}?{query}", inv_id=inv_id, out_sum=out_sum)


def ok_response(inv_id: str | int) -> str:
    """Тело ответа на ResultURL. Робокасса ждёт РОВНО ``OK<InvId>``."""
    return f"OK{inv_id}"


# ---------------------------------------------------------------- рекуррент

async def charge_recurring(
    *,
    inv_id: int,
    previous_inv_id: int,
    amount: str,
    description: str,
) -> dict[str, Any]:
    """НЕ РЕАЛИЗОВАНО СОЗНАТЕЛЬНО — см. docs/BILLING_ROBOKASSA.md.

    Что известно точно (документация «Периодические платежи»):

    * первый платёж помечается ``Recurring=true`` (мы это умеем — см.
      :func:`build_payment_link`);
    * дочернее списание — POST на ``https://auth.robokassa.ru/Merchant/Recurring``
      с ``MerchantLogin``, ``InvoiceID`` (наш новый номер), ``PreviousInvoiceID``
      (номер родительского платежа), ``Description``, ``OutSum``, ``SignatureValue``;
    * ``PreviousInvoiceID`` в подпись НЕ входит;
    * ответ ``OK<InvoiceID>`` означает лишь «операция создана», НЕ «деньги
      списаны» — итог приходит на ResultURL;
    * рекуррент включает техподдержка Robokassa по заявке (около 3 рабочих дней).

    Чего документация НЕ говорит — точной строки подписи дочернего списания
    (какие поля и в каком порядке идут вокруг Пароля #1). Гадать здесь нельзя:
    ошибка даёт не тест, а молчаливый отказ на живых деньгах. Поэтому автопродление
    сегодня не реализовано, а подписка выдаётся ровно на один оплаченный период
    (``cancel_at_period_end=1``) — пользователь продлевает вручную.

    TODO(биллинг): получить от техподдержки Robokassa точную формулу подписи для
    ``/Merchant/Recurring``, покрыть её тестом-вектором и включить автопродление.
    """
    raise RobokassaError(
        "рекуррент Robokassa не реализован: формула подписи дочернего списания "
        "не документирована (см. docs/BILLING_ROBOKASSA.md)"
    )
