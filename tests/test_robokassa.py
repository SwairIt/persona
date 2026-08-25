"""Robokassa — СПЯЩАЯ библиотека: подписи, ссылка, чек 54-ФЗ.

Интеграция сознательно НЕ подключена (25.08.2026, решение владельца): продавать
нечего — ни одна фича из ``PRO_FEATURES`` в коде не гейтится, продукт бесплатен
целиком. Роутов, публичных префиксов и UI нет; :mod:`app.billing.robokassa` не
импортируется ниоткуда, кроме этого файла. Полная картина и чек-лист включения —
``docs/BILLING_ROBOKASSA.md``.

Что здесь проверяется:

* чистые функции протокола — векторы подписи считаются ЗДЕСЬ ЖЕ из
  документационной формулы, а не копируются из реализации (иначе тест проверял
  бы, что код равен сам себе);
* что модуль остаётся спящим — ни импорта из приложения, ни роутов, ни шаблона;
* что конфиг инертен без кредов и без kv-флагов;
* что юридический подвал (он остаётся независимо от оплаты) есть на /pricing и
  в кабинете подписки.

Сеть не трогается ни разу: у Robokassa нет серверного API создания платежа,
а рекуррент осознанно не реализован.

Формулы (docs.robokassa.ru):
  запрос     MerchantLogin:OutSum:InvId[:Receipt]:Пароль#1[:Shp_...]
  ResultURL  OutSum:InvId:Пароль#2[:Shp_...]
  Success    OutSum:InvId:Пароль#1[:Shp_...]
Shp_-параметры идут после пароля и сортируются по имени.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from urllib.parse import parse_qs, quote, urlparse

import pytest
import pytest_asyncio

from app.billing import config as billing_config
from app.billing import robokassa
from app.billing.plans import PRO_MONTHLY
from app.storage.db import init_database

LOGIN = "persona_test"
PASS1 = "p1-secret-never-logged"
PASS2 = "p2-secret-never-logged"

PROJECT_ROOT = Path(__file__).resolve().parents[1]


# --------------------------------------------------------------------- fixtures

@pytest.fixture
def robokassa_env(monkeypatch: pytest.MonkeyPatch):
    """Боевой (не тестовый) магазин на SHA256 — креды только через env.

    Фикстура НЕ autouse: часть тестов проверяет ровно обратное — что без кредов
    всё молчит.
    """
    monkeypatch.setenv("PERSONA_ROBOKASSA_LOGIN", LOGIN)
    monkeypatch.setenv("PERSONA_ROBOKASSA_PASSWORD1", PASS1)
    monkeypatch.setenv("PERSONA_ROBOKASSA_PASSWORD2", PASS2)
    monkeypatch.setenv("PERSONA_ROBOKASSA_IS_TEST", "0")
    monkeypatch.setenv("PERSONA_ROBOKASSA_HASH", "sha256")
    monkeypatch.delenv("PERSONA_ROBOKASSA_RECEIPT_ENABLED", raising=False)
    monkeypatch.delenv("PERSONA_ROBOKASSA_INV_OFFSET", raising=False)
    billing_config.reset_cache()
    yield
    billing_config.reset_cache()


@pytest_asyncio.fixture
async def db():
    await init_database()
    yield


def _sha256(payload: str) -> str:
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _fake_request(path: str = "/"):
    """Минимальный объект запроса для прямого рендера шаблона.

    Шаблоны публичной витрины трогают ``request.cookies`` (баннер согласия) и
    ``request.url.path`` (активный пункт навигации) — больше ничего.
    """
    from types import SimpleNamespace

    return SimpleNamespace(
        cookies={},
        url=SimpleNamespace(path=path),
        headers={},
        query_params={},
        state=SimpleNamespace(user_id=None, is_owner=False),
    )


# ------------------------------------------------------------- модуль спит

def test_robokassa_is_imported_by_nothing_in_the_app():
    """Интеграция выключена — модуль не должен быть подключён к приложению.

    Если кто-то соберётся его оживить, пусть сделает это осознанно (и заодно
    прочитает docs/BILLING_ROBOKASSA.md), а не «случайно импортнётся и заработает».
    """
    # Разбираем AST, а не ищем подстроку: упоминание модуля в докстринге
    # (``см. app.billing.robokassa``) — это документация, а не подключение.
    import ast

    offenders = []
    for path in (PROJECT_ROOT / "app").rglob("*.py"):
        if path.name == "robokassa.py":
            continue
        try:
            tree = ast.parse(path.read_text(encoding="utf-8", errors="replace"))
        except SyntaxError:
            continue
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom):
                mod = node.module or ""
                if mod == "app.billing.robokassa" or (
                    mod == "app.billing"
                    and any(a.name == "robokassa" for a in node.names)
                ):
                    offenders.append(str(path.relative_to(PROJECT_ROOT)))
            elif isinstance(node, ast.Import) and any(
                a.name.startswith("app.billing.robokassa") for a in node.names
            ):
                offenders.append(str(path.relative_to(PROJECT_ROOT)))
    assert offenders == [], f"robokassa.py снова импортируется из приложения: {offenders}"


def test_no_robokassa_routes_or_templates_exist():
    """Ни ручек, ни страницы возврата — нулевая новая поверхность."""
    routes = (PROJECT_ROOT / "app" / "web" / "routes" / "billing.py").read_text(
        encoding="utf-8", errors="replace"
    )
    assert "robokassa" not in routes.lower()
    assert not (
        PROJECT_ROOT / "app" / "web" / "templates" / "billing_robokassa_return.html"
    ).exists()


def test_middleware_has_no_robokassa_prefixes():
    """Публичный префикс и CSRF-исключение сняты вместе с ручками."""
    from app.web.middleware import auth_gate, csrf

    assert auth_gate._is_public_path("/billing/robokassa/result") is False
    assert not any(
        "/billing/robokassa/result".startswith(p) for p in csrf._EXEMPT_PREFIXES
    )


# ------------------------------------------------------------- конфиг инертен

def test_config_is_inert_without_credentials(monkeypatch: pytest.MonkeyPatch):
    """Без env и без файла секретов — ни кредов, ни исключений."""
    for name in (
        "LOGIN", "PASSWORD1", "PASSWORD2", "TEST_PASSWORD1", "TEST_PASSWORD2",
        "IS_TEST", "HASH", "RECEIPT_ENABLED", "SNO", "TAX", "INV_OFFSET",
    ):
        monkeypatch.delenv(f"PERSONA_ROBOKASSA_{name}", raising=False)
    billing_config.reset_cache()
    assert billing_config.get_robokassa_credentials() is None
    assert billing_config.is_robokassa_configured() is False
    assert billing_config.is_provider_configured("robokassa") is False
    with pytest.raises(robokassa.RobokassaError):
        robokassa.require_credentials()


@pytest.mark.asyncio
async def test_kv_switches_default_to_off(db):
    """Без явных kv продажи выключены и провайдер не выбран."""
    billing_config.reset_cache()
    assert await billing_config.billing_enabled() is False
    assert await billing_config.active_provider() == "none"
    ready, provider = await billing_config.checkout_ready()
    assert (ready, provider) == (False, "none")


def test_yookassa_config_still_behaves_as_before(monkeypatch: pytest.MonkeyPatch):
    """Ветку ЮKassa переписанный config трогать не должен — на неё опираются роуты."""
    monkeypatch.delenv("PERSONA_YOOKASSA_SHOP_ID", raising=False)
    monkeypatch.delenv("PERSONA_YOOKASSA_SECRET_KEY", raising=False)
    assert billing_config.get_credentials() is None
    assert billing_config.is_configured() is False

    monkeypatch.setenv("PERSONA_YOOKASSA_SHOP_ID", "shop-1")
    monkeypatch.setenv("PERSONA_YOOKASSA_SECRET_KEY", "sec-1")
    creds = billing_config.get_credentials()
    assert creds is not None and creds.shop_id == "shop-1"
    assert billing_config.is_configured() is True
    # Секрет не должен утечь через repr датакласса (он попадает в логи «бесплатно»).
    assert "sec-1" not in repr(creds)


# ---------------------------------------------------------------- подписи

def test_request_signature_matches_hand_computed_vector():
    expected = _sha256(f"{LOGIN}:690.00:42:{PASS1}")
    got = robokassa.request_signature(
        login=LOGIN, out_sum="690.00", inv_id=42, password1=PASS1, algo="sha256"
    )
    assert got == expected


def test_request_signature_with_shp_sorted_alphabetically():
    # Передаём заведомо не по алфавиту — подпись обязана собраться по алфавиту.
    expected = _sha256(f"{LOGIN}:690.00:42:{PASS1}:Shp_plan=pro_monthly:Shp_uid=7")
    got = robokassa.request_signature(
        login=LOGIN, out_sum="690.00", inv_id=42, password1=PASS1, algo="sha256",
        shp={"Shp_uid": "7", "Shp_plan": "pro_monthly"},
    )
    assert got == expected


def test_request_signature_empty_inv_id_slot():
    """Без номера заказа документация требует ПУСТОЙ слот: OutSum::Пароль#1."""
    expected = _sha256(f"{LOGIN}:690.00::{PASS1}")
    got = robokassa.request_signature(
        login=LOGIN, out_sum="690.00", inv_id=None, password1=PASS1, algo="sha256"
    )
    assert got == expected


def test_request_signature_receipt_goes_before_password_url_encoded():
    receipt_json = robokassa.build_receipt(name="Persona Pro", amount="690.00")
    encoded = robokassa.encode_receipt(receipt_json)
    assert encoded == quote(receipt_json, safe="")
    expected = _sha256(f"{LOGIN}:690.00:42:{encoded}:{PASS1}")
    got = robokassa.request_signature(
        login=LOGIN, out_sum="690.00", inv_id=42, password1=PASS1,
        receipt=encoded, algo="sha256",
    )
    assert got == expected


def test_result_and_success_use_different_passwords():
    result = robokassa.result_signature(
        out_sum="690.00", inv_id=42, password2=PASS2, algo="sha256"
    )
    success = robokassa.success_signature(
        out_sum="690.00", inv_id=42, password1=PASS1, algo="sha256"
    )
    assert result == _sha256(f"690.00:42:{PASS2}")
    assert success == _sha256(f"690.00:42:{PASS1}")
    assert result != success


def test_md5_is_supported_too():
    expected = hashlib.md5(f"{LOGIN}:690.00:42:{PASS1}".encode()).hexdigest()  # noqa: S324
    got = robokassa.request_signature(
        login=LOGIN, out_sum="690.00", inv_id=42, password1=PASS1, algo="md5"
    )
    assert got == expected


def test_unknown_hash_algorithm_is_rejected():
    with pytest.raises(robokassa.RobokassaError):
        robokassa.digest("anything", "totally-not-a-hash")


def test_verify_result_is_case_insensitive_but_rejects_tampering(robokassa_env):
    creds = billing_config.get_robokassa_credentials()
    sig = robokassa.result_signature(
        out_sum="690.00", inv_id=42, password2=PASS2,
        shp={"Shp_uid": "1"}, algo="sha256",
    )
    good = {"OutSum": "690.00", "InvId": "42", "Shp_uid": "1",
            "SignatureValue": sig.upper()}
    assert robokassa.verify_result(good, creds) is True

    # Подменённая сумма ломает подпись (её считали от 690.00).
    assert robokassa.verify_result({**good, "OutSum": "1.00"}, creds) is False
    # Подменённый Shp_ тоже: он участвует в подписи.
    assert robokassa.verify_result({**good, "Shp_uid": "2"}, creds) is False
    # Пустая подпись — не проходит.
    assert robokassa.verify_result({**good, "SignatureValue": ""}, creds) is False


def test_verify_success_uses_password_one(robokassa_env):
    creds = billing_config.get_robokassa_credentials()
    sig = robokassa.success_signature(
        out_sum="690.00", inv_id=42, password1=PASS1, algo="sha256"
    )
    params = {"OutSum": "690.00", "InvId": "42", "SignatureValue": sig}
    assert robokassa.verify_success(params, creds) is True
    # Та же строка, подписанная Паролем #2, на Success не проходит.
    wrong = robokassa.result_signature(
        out_sum="690.00", inv_id=42, password2=PASS2, algo="sha256"
    )
    assert robokassa.verify_success({**params, "SignatureValue": wrong}, creds) is False


def test_amount_formatting_and_comparison():
    assert robokassa.format_amount("690") == "690.00"
    assert robokassa.format_amount(690.0) == "690.00"
    assert robokassa.amounts_match("690.00", PRO_MONTHLY.amount) is True
    assert robokassa.amounts_match("1.00", PRO_MONTHLY.amount) is False
    with pytest.raises(robokassa.RobokassaError):
        robokassa.format_amount("не-число")


# ------------------------------------------------------------- ссылка оплаты

def test_payment_link_shape_and_self_consistency(robokassa_env):
    link = robokassa.build_payment_link(
        inv_id=42, amount="690.00", description="Persona Pro — monthly",
        email="buyer@example.com", shp={"Shp_uid": "1", "Shp_plan": "pro_monthly"},
    )
    assert link.url.startswith("https://auth.robokassa.ru/Merchant/Index.aspx?")
    q = {k: v[0] for k, v in parse_qs(urlparse(link.url).query).items()}
    assert q["MerchantLogin"] == LOGIN
    assert q["OutSum"] == "690.00"
    assert q["InvId"] == "42"
    assert q["Culture"] == "ru"
    assert q["Encoding"] == "utf-8"
    assert q["Shp_uid"] == "1" and q["Shp_plan"] == "pro_monthly"
    assert "IsTest" not in q  # боевой режим
    assert "Receipt" not in q  # чек выключен по умолчанию
    # Подпись в ссылке пересчитывается независимо и сходится.
    assert q["SignatureValue"] == _sha256(
        f"{LOGIN}:690.00:42:{PASS1}:Shp_plan=pro_monthly:Shp_uid=1"
    )


def test_payment_link_never_leaks_passwords(robokassa_env):
    link = robokassa.build_payment_link(
        inv_id=42, amount="690.00", description="Persona Pro",
        email="buyer@example.com", shp={"Shp_uid": "1"},
    )
    assert PASS1 not in link.url
    assert PASS2 not in link.url


def test_test_mode_uses_test_passwords_and_sets_is_test(robokassa_env, monkeypatch):
    monkeypatch.setenv("PERSONA_ROBOKASSA_IS_TEST", "1")
    monkeypatch.setenv("PERSONA_ROBOKASSA_TEST_PASSWORD1", "t1")
    monkeypatch.setenv("PERSONA_ROBOKASSA_TEST_PASSWORD2", "t2")
    creds = billing_config.get_robokassa_credentials()
    assert creds is not None
    assert (creds.password1, creds.password2) == ("t1", "t2")
    link = robokassa.build_payment_link(inv_id=7, amount="690.00", description="Pro")
    q = {k: v[0] for k, v in parse_qs(urlparse(link.url).query).items()}
    assert q["IsTest"] == "1"
    assert q["SignatureValue"] == _sha256(f"{LOGIN}:690.00:7:t1")


def test_test_mode_without_test_passwords_is_not_configured(robokassa_env, monkeypatch):
    """Боевые пароли в тестовом режиме — ошибка 29 у Robokassa. Лучше «не настроено»."""
    monkeypatch.setenv("PERSONA_ROBOKASSA_IS_TEST", "1")
    monkeypatch.delenv("PERSONA_ROBOKASSA_TEST_PASSWORD1", raising=False)
    monkeypatch.delenv("PERSONA_ROBOKASSA_TEST_PASSWORD2", raising=False)
    assert billing_config.is_robokassa_configured() is False


def test_receipt_json_shape(robokassa_env, monkeypatch):
    monkeypatch.setenv("PERSONA_ROBOKASSA_RECEIPT_ENABLED", "1")
    monkeypatch.setenv("PERSONA_ROBOKASSA_SNO", "usn_income")
    monkeypatch.setenv("PERSONA_ROBOKASSA_TAX", "none")
    link = robokassa.build_payment_link(inv_id=7, amount="690.00", description="Persona Pro")
    q = {k: v[0] for k, v in parse_qs(urlparse(link.url).query).items()}
    receipt = json.loads(q["Receipt"])
    assert receipt["sno"] == "usn_income"
    item = receipt["items"][0]
    assert item["name"] == "Persona Pro"
    assert item["quantity"] == 1
    assert item["sum"] == 690.0
    assert item["tax"] == "none"
    assert item["payment_method"] == "full_payment"
    assert item["payment_object"] == "service"


def test_ok_response_is_exactly_what_robokassa_waits_for():
    assert robokassa.ok_response(42) == "OK42"


@pytest.mark.asyncio
async def test_recurring_is_an_explicit_refusal_not_a_guess():
    """Формула подписи дочернего списания не документирована — гадать нельзя."""
    with pytest.raises(robokassa.RobokassaError):
        await robokassa.charge_recurring(
            inv_id=2, previous_inv_id=1, amount="690.00", description="Pro"
        )


# ------------------------------------------------- витрина и юридический подвал

def test_pricing_still_has_no_pro_card():
    """Продукт бесплатен — карточка Pro на витрине остаётся закомментированной."""
    from app.web.templates_engine import templates

    html = templates.env.get_template("pricing.html").render(
        title="Цена Persona", app_version="test", session=None,
        request=_fake_request("/pricing"),
    )
    assert "690" not in html
    assert "Оформить Pro" not in html
    assert "Начать бесплатно" in html  # остальная страница на месте


def test_pricing_footer_carries_the_legal_block():
    """Подвал нужен независимо от оплаты: сервис обрабатывает ПДн в любом случае."""
    from app.web.templates_engine import templates

    html = templates.env.get_template("pricing.html").render(
        title="Цена Persona", app_version="test", session=None,
        request=_fake_request("/pricing"),
    )
    for href in ("/privacy-policy", "/privacy-policy/consent", "/terms",
                 "/terms/requisites"):
        assert href in html


def test_billing_portal_footer_carries_the_legal_block():
    from app.web.templates_engine import templates

    html = templates.env.get_template("billing.html").render(
        title="Подписка", app_version="test", session={"user_id": 1},
        request=_fake_request("/billing"), owner=False, email="buyer@example.com",
        summary={"active": False, "is_trial": False, "license_key": "PRSN-AAAA"},
        plans=[PRO_MONTHLY], configured=False, notice=None, payments=[],
    )
    for href in ("/privacy-policy", "/terms", "/terms/requisites"):
        assert href in html
