"""Юридическая поверхность сайта — то, что нельзя сломать молча.

Сайт открыл публичную регистрацию и готовится принимать деньги. С этого
момента юридические страницы перестают быть маркетинговым текстом: политика
конфиденциальности обязана быть доступна анонимно (152-ФЗ), оферта, возврат и
реквизиты — это требования Закона «О защите прав потребителей» и модерации
платёжных провайдеров, а Метрика с вебвизором не имеет права загрузиться до
согласия.

Тесты проверяют ровно эти инварианты, а не красоту текста:

1. Метрики нет в разметке, пока нет куки согласия; с ней — ровно один раз.
2. Каждая юридическая страница отдаёт 200 БЕЗ сессии и достижима из подвала
   публичной страницы. Ломается это тихо: достаточно завести новый роут вне
   публичного префикса auth-gate — и страница начнёт редиректить на лендинг.
3. Политика перечисляет ВСЕХ третьих лиц, которым реально уходят данные.
   Забытый в списке получатель — не опечатка, а необъявленная передача ПДн.
4. Форма регистрации ведёт на текст согласия, и галочка не предзаполнена.
5. В наших шаблонах нет заявлений, которые перестали быть правдой после
   появления серверных аккаунтов («данные не покидают устройство» и т. п.).
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv
from app.web.main import create_app
from app.web.routes import setup_gate

ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = ROOT / "app" / "web" / "templates"

TAG = "mc.yandex.ru/metrika/tag.js?id=111901324"
NOSCRIPT = "mc.yandex.ru/watch/111901324"

# Все юридические документы сайта: путь → узнаваемый фрагмент заголовка.
LEGAL_PAGES: dict[str, str] = {
    "/privacy-policy": "Политика конфиденциальности",
    "/privacy-policy/consent": "Согласие на обработку персональных данных",
    "/privacy-policy/cookies": "Cookie и аналитика",
    "/terms": "Пользовательское соглашение",
    "/terms/offer": "оферта",
    "/terms/refund": "Возврат средств",
    "/terms/requisites": "Реквизиты",
    "/security": "Безопасность",
}

# Публичные страницы, в подвале которых обязаны быть юридические ссылки.
PUBLIC_PAGES = ["/landing", "/features", "/privacy-policy", "/terms/offer", "/blog"]

# Заявления, которые после открытия серверной регистрации стали неправдой.
# Держим список здесь, а не в голове: они возвращаются в тексты незаметно.
FORBIDDEN_CLAIMS = [
    "не покидают устройство",
    "не покидают твоё устройство",
    "не покидает устройство",
    "ноль телеметрии",
    "нулевая телеметрия",
    "0 сетевых",
    "zero telemetry",
]

# Файлы, за формулировки в которых отвечает этот тест.
#
# ``landing.py`` сюда НЕ входит намеренно: там же лежат комментарии, которые
# ЦИТИРУЮТ снятые обещания («раньше страница писала: данные не покидают твоё
# устройство»), и запрет на подстроку в исходнике запретил бы объяснять, что
# именно было исправлено. Текст, который реально видит человек, покрывает
# :func:`test_rendered_legal_pages_carry_no_forbidden_claims` — он проверяет
# отрендеренные страницы, а не комментарии.
OWNED_FILES = [
    TEMPLATES / "legal.html",
    TEMPLATES / "infopage.html",
    TEMPLATES / "_metrika.html",
    TEMPLATES / "_public_footer.html",
    ROOT / "app" / "web" / "static" / "consent.js",
]


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    """Инстанс с уже зарегистрированным владельцем → auth-gate АКТИВЕН.

    Это принципиально: гейт включается после первой регистрации, и именно в
    этом состоянии проверка «юридическая страница открывается анонимно» имеет
    смысл. На пустой базе гейт спит и тест был бы бесполезен.
    """
    await init_database()
    owner = await create_user("owner@legal.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
    setup_gate._cache.mark_done()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


# ── 1. Согласие на аналитику ────────────────────────────────────────────────


async def test_metrika_absent_until_consent(client: AsyncClient) -> None:
    body = (await client.get("/landing")).text
    assert TAG not in body, "код Метрики попал в разметку без согласия"
    assert NOSCRIPT not in body, "noscript-пиксель бьёт в Яндекс без согласия"


async def test_metrika_present_after_consent(client: AsyncClient) -> None:
    client.cookies.set("persona_consent", "all")
    body = (await client.get("/landing")).text
    assert body.count(TAG) == 1
    assert body.count(NOSCRIPT) == 1


async def test_consent_banner_offers_rejection_as_easily_as_acceptance() -> None:
    """Отказ не должен быть спрятан: обе кнопки в одном блоке, без предвыбора."""
    js = (ROOT / "app" / "web" / "static" / "consent.js").read_text(encoding="utf-8")
    assert "Принять" in js
    assert "Только необходимые" in js
    assert "вебвизор" in js.lower(), "баннер обязан назвать вебвизор своим именем"
    assert "Яндекс.Метрик" in js, "баннер обязан назвать получателя данных"
    # Никаких «продолжая пользоваться сайтом, вы соглашаетесь» В ТЕКСТЕ БАННЕРА.
    # Проверяем именно разметку баннера, а не весь файл: в шапке файла эта
    # формулировка упомянута как то, чего мы НЕ делаем.
    banner = js[js.index("banner.innerHTML"):js.index("function hide()")]
    assert "продолжая" not in banner.lower()
    assert "Принять" in banner and "Только необходимые" in banner
    # Решение переживает перезагрузку: и кука (её видит сервер), и localStorage.
    assert "localStorage" in js
    assert "persona_consent=" in js or "COOKIE + '='" in js


async def test_consent_decision_can_be_changed_later(client: AsyncClient) -> None:
    """С любой публичной страницы есть вход в повторный выбор + страница cookie."""
    body = (await client.get("/landing")).text
    assert "data-consent-open" in body
    assert "/privacy-policy/cookies" in body


# ── 2. Доступность и связность юридических страниц ──────────────────────────


@pytest.mark.parametrize("path", sorted(LEGAL_PAGES))
async def test_legal_page_opens_anonymously(client: AsyncClient, path: str) -> None:
    """Без сессии и без редиректа на лендинг — иначе документа «нет»."""
    response = await client.get(path)
    assert response.status_code == 200, (path, response.status_code,
                                         response.headers.get("location"))
    assert LEGAL_PAGES[path].lower() in response.text.lower(), path


@pytest.mark.parametrize("page", PUBLIC_PAGES)
async def test_every_legal_page_is_linked_from_the_public_footer(
    client: AsyncClient, page: str
) -> None:
    body = (await client.get(page, follow_redirects=True)).text
    missing = [p for p in LEGAL_PAGES if f'href="{p}"' not in body]
    assert missing == [], (page, missing)


async def test_legal_pages_cross_link_to_requisites(client: AsyncClient) -> None:
    """Оферта и возврат обязаны вести к реквизитам продавца."""
    for path in ("/terms/offer", "/terms/refund"):
        body = (await client.get(path)).text
        assert '/terms/requisites' in body, path


# ── 3. Политика описывает реальных получателей данных ───────────────────────


@pytest.mark.parametrize(
    "third_party",
    [
        "ЯНДЕКС",       # веб-аналитика (Метрика 111901324 + вебвизор)
        "Telegram",     # уведомления через СВОЙ бот участника (social_tg_token)
        "SMTP",         # почтовый релей владельца инстанса
        "языковой модели",  # LLM-провайдер, выбранный самим пользователем
    ],
)
async def test_privacy_policy_names_every_third_party(
    client: AsyncClient, third_party: str
) -> None:
    body = (await client.get("/privacy-policy")).text
    assert third_party in body, third_party


async def test_privacy_policy_admits_admin_database_access(client: AsyncClient) -> None:
    """Главная неудобная правда: данные участника не скрыты от админа."""
    body = (await client.get("/privacy-policy")).text.lower()
    assert "доступ к базе" in body
    assert "администратор" in body or "владелец" in body


async def test_privacy_policy_states_the_rights_and_how_to_use_them(
    client: AsyncClient,
) -> None:
    body = (await client.get("/privacy-policy")).text
    for word in ("исправл", "удален", "отзыв"):
        assert word.lower() in body.lower(), word


@pytest.mark.parametrize(
    "endpoint",
    [
        "/settings/my-data",              # экран прав участника
        "/settings/my-data/export.json",  # право на доступ / переносимость
        "/settings/my-data/export.zip",
        "/settings/my-data/delete",       # право на удаление
    ],
)
async def test_privacy_policy_links_the_real_self_serve_endpoints(
    client: AsyncClient, endpoint: str
) -> None:
    """Права описываются рабочими адресами, а не «напишите нам».

    Регресс, который этот тест ловит: политика ссылалась на
    ``/settings/privacy/export-memory``. Эта страница — пульт ВЛАДЕЛЬЦА
    (её сосед ``/settings/privacy/snapshot`` выгружает всю базу целиком), и
    участник получал там 403. Инструкция в юридическом документе, ведущая на
    отказ, хуже отсутствия инструкции.
    """
    body = (await client.get("/privacy-policy")).text
    assert endpoint in body, endpoint


async def test_privacy_policy_does_not_send_members_to_the_owner_console(
    client: AsyncClient,
) -> None:
    """Owner-only адреса не подаются участнику как способ реализовать право."""
    body = (await client.get("/privacy-policy")).text
    assert "/settings/privacy/export-memory" not in body
    assert "/settings/privacy/snapshot" not in body


async def test_privacy_policy_warns_that_deletion_wipes_dms_for_both_sides(
    client: AsyncClient,
) -> None:
    """Последствие для ТРЕТЬЕГО лица обязано быть названо до нажатия кнопки."""
    body = (await client.get("/privacy-policy")).text.lower()
    assert "у обеих сторон" in body
    assert "не получает" in body or "не уведомля" in body
    assert "собеседник" in body


async def test_privacy_policy_does_not_promise_rights_the_product_lacks(
    client: AsyncClient,
) -> None:
    """Не обещаем то, чего нет: правку email/имени и отзыв без удаления.

    Если такие функции появятся — тест упадёт на ключевых словах-оговорках,
    и это правильный сигнал переписать раздел, а не удалить проверку.
    """
    body = (await client.get("/privacy-policy")).text.lower()
    # Отзыв согласия без удаления аккаунта прямо назван невозможным.
    assert "сохранить аккаунт нельзя" in body
    # Самостоятельная правка имени/почты прямо названа отсутствующей.
    assert "нет" in body and "электронной почты аккаунта" in body


async def test_privacy_policy_mentions_consent_is_recorded_with_a_version(
    client: AsyncClient,
) -> None:
    from app.auth.consent import POLICY_VERSION  # noqa: PLC0415

    body = (await client.get("/privacy-policy")).text
    assert POLICY_VERSION in body, "в политике нет версии, под которой пишется согласие"


def test_policy_version_matches_the_published_revision_date() -> None:
    """``POLICY_VERSION`` и ``_LEGAL_UPDATED`` — одна дата в двух форматах.

    Разъезд означает, что журнал согласий ссылается на редакцию, которой на
    сайте уже нет. Держим их сцепленными тестом, а не памятью.
    """
    import datetime as _dt  # noqa: PLC0415

    from app.auth.consent import POLICY_VERSION  # noqa: PLC0415
    from app.web.routes.landing import _LEGAL_UPDATED  # noqa: PLC0415

    months = ("января", "февраля", "марта", "апреля", "мая", "июня",
              "июля", "августа", "сентября", "октября", "ноября", "декабря")
    iso = _dt.date.fromisoformat(POLICY_VERSION)
    expected = f"{iso.day} {months[iso.month - 1]} {iso.year}"
    assert _LEGAL_UPDATED == expected, (POLICY_VERSION, _LEGAL_UPDATED, expected)


async def test_privacy_policy_lists_the_strictly_necessary_cookies(
    client: AsyncClient,
) -> None:
    body = (await client.get("/privacy-policy")).text
    for cookie in ("persona_session", "persona_csrf", "persona_consent"):
        assert cookie in body, cookie


# ── 4. Регистрация ──────────────────────────────────────────────────────────


def _signup_markup() -> str:
    return (TEMPLATES / "auth_signup.html").read_text(encoding="utf-8")


def test_signup_form_has_a_consent_checkbox_linking_to_the_text() -> None:
    markup = _signup_markup()
    assert 'name="consent"' in markup, "на форме регистрации нет галочки согласия"
    assert "/privacy-policy/consent" in markup
    assert "/privacy-policy" in markup


def test_signup_consent_checkbox_is_not_pre_ticked() -> None:
    """Предзаполненная галочка согласием не является (152-ФЗ, ст. 9)."""
    for markup, name in ((_signup_markup(), "auth_signup.html"),
                         ((TEMPLATES / "landing_v2.html").read_text(encoding="utf-8"),
                          "landing_v2.html")):
        for tag in re.findall(r"<input[^>]*name=\"consent\"[^>]*>", markup):
            assert "checked" not in tag, (name, tag)
            assert "required" in tag, (name, tag)


def test_quick_signup_on_the_landing_also_asks_for_consent() -> None:
    """Быстрая форма лендинга создаёт аккаунт в одно нажатие — согласие нужно и там."""
    markup = (TEMPLATES / "landing_v2.html").read_text(encoding="utf-8")
    assert 'name="consent"' in markup
    assert "/privacy-policy/consent" in markup


def test_both_landing_registration_forms_ask_for_consent() -> None:
    """На лендинге ДВЕ точки регистрации — галочка нужна у каждой.

    Нижняя CTA-форма отправляется нативно и раньше согласия не спрашивала
    вовсе.
    """
    markup = (TEMPLATES / "landing_v2.html").read_text(encoding="utf-8")
    forms = re.findall(r'<form[^>]*action="/auth/register"[^>]*>', markup)
    assert len(forms) == 2, forms
    ids = [re.search(r'id="([^"]+)"', f) for f in forms]
    assert all(ids), "у формы регистрации нет id — галочку не к чему привязать"
    for match in ids:
        form_id = match.group(1)  # type: ignore[union-attr]
        assert f'form="{form_id}"' in markup, form_id


def test_landing_consent_checkboxes_are_owned_by_their_form() -> None:
    """Галочка вне <form> без атрибута ``form`` — декорация, а не согласие.

    Именно так и было: браузер не применял ``required`` и не клал поле в тело,
    поэтому журнал согласий писал source='form_submit' вместо 'checkbox'.
    """
    markup = (TEMPLATES / "landing_v2.html").read_text(encoding="utf-8")
    for tag in re.findall(r"<input[^>]*name=\"consent\"[^>]*>", markup):
        assert "form=" in tag, tag
        assert "required" in tag, tag
        assert "checked" not in tag, tag


def test_landing_js_submit_sends_the_consent_field() -> None:
    """JSON-сабмит собирает тело руками — consent надо класть явно."""
    markup = (TEMPLATES / "landing_v2.html").read_text(encoding="utf-8")
    handler = markup[markup.index("magicForm.addEventListener"):]
    handler = handler[: handler.index("});")]
    assert "consent" in handler, "быстрая регистрация не отправляет поле consent"


# ── 5. Честность формулировок ───────────────────────────────────────────────


@pytest.mark.parametrize("path", [str(p.relative_to(ROOT)) for p in OWNED_FILES])
def test_no_forbidden_claims_in_owned_files(path: str) -> None:
    text = (ROOT / path).read_text(encoding="utf-8")
    # Отрицания разрешены: в /security мы прямо пишем, что НЕ обещаем
    # «данные не покидают ваше устройство».
    hits = [
        claim for claim in FORBIDDEN_CLAIMS
        if claim in text and "не обещаем" not in text[max(0, text.index(claim) - 220):text.index(claim)]
    ]
    assert hits == [], (path, hits)


async def test_rendered_legal_pages_carry_no_forbidden_claims(
    client: AsyncClient,
) -> None:
    for path in LEGAL_PAGES:
        body = (await client.get(path)).text
        for claim in FORBIDDEN_CLAIMS:
            if claim not in body:
                continue
            before = body[max(0, body.index(claim) - 220):body.index(claim)]
            assert "не обещаем" in before, (path, claim)


async def test_offer_is_marked_inactive_while_the_product_is_free(
    client: AsyncClient,
) -> None:
    """Пока реквизиты не заполнены — оферта обязана честно говорить, что не действует."""
    from app.web.routes.landing import operator_ready  # noqa: PLC0415

    body = (await client.get("/terms/offer")).text
    if operator_ready():
        pytest.skip("реквизиты заполнены — оферта действует, проверка неприменима")
    assert "не действует" in body.lower()


async def test_requisites_page_shows_unfilled_placeholders_loudly(
    client: AsyncClient,
) -> None:
    """Пустые реквизиты не скрываются: владелец должен их увидеть и заполнить."""
    from app.web.routes.landing import operator_ready  # noqa: PLC0415

    body = (await client.get("/terms/requisites")).text
    if operator_ready():
        assert "НЕ ЗАПОЛНЕНО" not in body
    else:
        assert "НЕ ЗАПОЛНЕНО" in body
        assert "OPERATOR" in body, "нет подсказки, где именно заполнять"
