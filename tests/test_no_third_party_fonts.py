"""Шрифты — свои. Ни одна публичная страница не зовёт Google.

ЧТО СЛОМАЛОСЬ И ПОЧЕМУ ЭТО СТОРОЖИТСЯ ТЕСТОМ
--------------------------------------------
``landing_v2.html`` и ``blog_base.html`` тянули Space Grotesk / Inter /
JetBrains Mono напрямую с ``fonts.googleapis.com`` (+ ``preconnect`` на
``fonts.gstatic.com``). Такой ``<link>`` — не аналитика, его нельзя отложить
до баннера: он срабатывает на ПЕРВОЙ отрисовке, до ``/static/consent.js`` и
совершенно независимо от куки ``persona_consent``. То есть Google получал IP,
User-Agent и адрес страницы каждого посетителя раньше, чем тот успевал
что-либо выбрать, — при том что ``/privacy-policy`` §7 перечисляет всех, кому
уходят данные, и заканчивается словами «Больше — никому». Google там не было.
Хуже обычного счётчика: счётчик оператор ставит сам, а это ехало в комплекте и
не отключалось даже при self-host.

Прятать шрифты за согласием — неправильное лечение: у отказавшихся сломалась бы
типографика. Поэтому третья сторона убрана целиком — файлы лежат в
``app/web/static/fonts/``.

Тест держит четыре рубежа:

1. **Шаблоны.** Ни один шаблон не упоминает Google-хосты, и отрисованный HTML
   лендинга, блога, статьи, тарифов и «возможностей» их не содержит (проверка
   идёт по ответу сервера, а не по исходнику шаблона: ссылка могла приехать из
   include, из макроса, из JSON-LD).
2. **Файлы.** Каждый ``src: url(...)`` из ``fonts.css`` резолвится в живой файл
   на диске, отдаётся статикой со статусом 200 и является настоящим WOFF2
   (сигнатура ``wOF2``). Иначе «self-hosted» превращается в тихий 404 и
   системный шрифт — визуально почти незаметно, а типографики нет.
3. **Кириллица.** Сайт русский. Скачать только latin-подмножество —
   классическая ошибка: латиница выглядит правильно, а каждая русская буква
   молча падает на системный шрифт. Поэтому проверяется, что у ``Inter`` и
   ``JetBrains Mono`` есть подмножество с ``unicode-range``, покрывающим
   U+0410–U+044F, и что за ним стоит непустой файл.
4. **CSP.** ``style-src``/``font-src`` больше не рекламируют Google-хосты — ни в
   боевой политике, ни в её Report-Only двойнике. Иначе политика продолжает
   разрешать то, что мы только что вычистили, и следующий ``<link>`` проедет
   молча.

Про Space Grotesk: кириллического подмножества у него НЕ СУЩЕСТВУЕТ (Google его
тоже не отдавал), поэтому русские заголовки как падали на системный гротеск,
так и падают — поведение не менялось, и тест этого не требует.
"""

from __future__ import annotations

import pathlib
import re

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import blog
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.web.main import create_app
from app.web.middleware.security_headers import CSP_ENFORCED, CSP_REPORT_ONLY
from app.web.routes import setup_gate
from app.storage.repository import set_kv

REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]
TEMPLATES_DIR = REPO_ROOT / "app" / "web" / "templates"
FONTS_DIR = REPO_ROOT / "app" / "web" / "static" / "fonts"
FONTS_CSS = FONTS_DIR / "fonts.css"

#: Хосты, обращение к которым и было утечкой.
GOOGLE_FONT_HOSTS = ("fonts.googleapis.com", "fonts.gstatic.com")

#: Начало русского алфавита в Unicode — то, что обязано быть покрыто.
CYRILLIC_LO = 0x0410
CYRILLIC_HI = 0x044F

_SRC_RE = re.compile(r"""src:\s*url\(\s*['"]?([^'")]+)['"]?\s*\)""")
_RANGE_RE = re.compile(r"unicode-range:\s*([^;]+);")


@pytest_asyncio.fixture
async def client() -> AsyncClient:
    await init_database()
    owner = await create_user("owner@fonts.test", "Zq7-frost-lantern-91")
    async with get_connection() as conn:
        await set_kv(conn, "setup_complete", "true")
        await set_kv(conn, "owner_user_id", str(owner["id"]))
        await set_kv(conn, "owner_exclusive_mode", "0")
    setup_gate._cache.mark_done()

    transport = ASGITransport(app=create_app())
    async with AsyncClient(transport=transport, base_url="http://test") as ac:
        yield ac


def _public_paths() -> list[str]:
    """Все публичные страницы — ни на одной не должно быть Google-хостов."""
    return [*_webfont_paths(), "/pricing", "/features"]


def _webfont_paths() -> list[str]:
    """Страницы, которые действительно набраны этими тремя семействами.

    ``/pricing`` и ``/features`` — отдельные шаблоны на ``static/landing/``
    (первый лендинг): они всегда жили на системных шрифтах и в Google не
    ходили, поэтому требовать от них ``fonts.css`` неправильно.
    """
    paths = ["/", "/landing", "/blog"]
    posts = blog.list_posts()
    if posts:
        paths.append(f"/blog/{posts[0].slug}")
    return paths


def _font_css_sources() -> list[str]:
    """Все ``url(...)`` из ``src:`` в fonts.css."""
    return _SRC_RE.findall(FONTS_CSS.read_text(encoding="utf-8"))


def _unicode_ranges() -> list[str]:
    return _RANGE_RE.findall(FONTS_CSS.read_text(encoding="utf-8"))


def _range_covers_cyrillic(spec: str) -> bool:
    """True, если ``unicode-range`` покрывает весь диапазон U+0410–U+044F."""
    for chunk in spec.split(","):
        m = re.fullmatch(r"\s*U\+([0-9A-Fa-f]+)(?:-([0-9A-Fa-f]+))?\s*", chunk)
        if not m:
            continue
        start = int(m.group(1), 16)
        end = int(m.group(2), 16) if m.group(2) else start
        if start <= CYRILLIC_LO and end >= CYRILLIC_HI:
            return True
    return False


# ── 1. Шаблоны и отрисованный HTML ──────────────────────────────────────────


def test_no_template_references_a_google_font_host() -> None:
    guilty: list[str] = []
    for path in TEMPLATES_DIR.rglob("*.html"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        if any(host in text for host in GOOGLE_FONT_HOSTS):
            guilty.append(str(path.relative_to(REPO_ROOT)))
    assert not guilty, (
        "шаблоны всё ещё зовут Google Fonts (запрос уйдёт до баннера согласия): "
        f"{guilty}"
    )


@pytest.mark.asyncio
async def test_public_pages_render_without_google_fonts(client: AsyncClient) -> None:
    for path in _public_paths():
        resp = await client.get(path, follow_redirects=True)
        assert resp.status_code == 200, f"{path} -> {resp.status_code}"
        body = resp.text
        for host in GOOGLE_FONT_HOSTS:
            assert host not in body, f"{path} тянет {host}"


@pytest.mark.asyncio
async def test_public_pages_link_the_local_font_stylesheet(
    client: AsyncClient,
) -> None:
    for path in _webfont_paths():
        body = (await client.get(path, follow_redirects=True)).text
        assert "/static/fonts/fonts.css?v=" in body, (
            f"{path} не подключает локальные шрифты с кэш-бастером"
        )


# ── 2. Файлы на диске и по HTTP ─────────────────────────────────────────────


def test_local_font_stylesheet_exists_and_declares_faces() -> None:
    assert FONTS_CSS.is_file(), f"нет {FONTS_CSS}"
    css = FONTS_CSS.read_text(encoding="utf-8")
    for family in ("Space Grotesk", "Inter", "JetBrains Mono"):
        assert f"'{family}'" in css, f"в fonts.css нет семейства {family}"
    # swap, а не block: пока шрифт едет, текст должен быть виден.
    assert css.count("font-display: swap") == css.count("@font-face")


def test_every_font_src_resolves_to_a_real_woff2_on_disk() -> None:
    sources = _font_css_sources()
    assert sources, "в fonts.css нет ни одного src: url(...)"
    for src in sources:
        assert not src.startswith(("http://", "https://", "//")), (
            f"src ведёт наружу, а должен быть локальным: {src}"
        )
        target = (FONTS_CSS.parent / src.split("?", 1)[0]).resolve()
        assert target.is_file(), f"fonts.css ссылается на несуществующий {src}"
        assert target.read_bytes()[:4] == b"wOF2", f"{src} — не WOFF2"


@pytest.mark.asyncio
async def test_static_mount_serves_every_font_file(client: AsyncClient) -> None:
    resp = await client.get("/static/fonts/fonts.css")
    assert resp.status_code == 200, "статикой не отдаётся сам fonts.css"
    for src in _font_css_sources():
        url = f"/static/fonts/{src.split('?', 1)[0]}"
        got = await client.get(url)
        assert got.status_code == 200, f"{url} -> {got.status_code}"
        assert got.content[:4] == b"wOF2", f"{url} отдаёт не WOFF2"


# ── 3. Кириллица ────────────────────────────────────────────────────────────


def test_a_cyrillic_subset_is_declared() -> None:
    assert any(_range_covers_cyrillic(r) for r in _unicode_ranges()), (
        "ни один @font-face не покрывает U+0410–U+044F: русский текст молча "
        "уедет на системный шрифт"
    )


def test_cyrillic_faces_are_backed_by_real_subset_files() -> None:
    css = FONTS_CSS.read_text(encoding="utf-8")
    seen: list[str] = []
    for body in re.findall(r"@font-face\s*\{(.*?)\}", css, re.S):
        ur = _RANGE_RE.search(body)
        src = _SRC_RE.search(body)
        if not ur or not src or not _range_covers_cyrillic(ur.group(1)):
            continue
        target = (FONTS_CSS.parent / src.group(1).split("?", 1)[0]).resolve()
        assert target.stat().st_size > 2000, (
            f"{target.name} подозрительно мал для кириллического подмножества"
        )
        seen.append(target.name.lower())
    # Основной текст (Inter) и моноширинный (JetBrains Mono) — оба русские.
    assert any("inter" in n for n in seen), "нет кириллического Inter"
    assert any("jetbrains" in n for n in seen), "нет кириллического JetBrains Mono"


# ── 4. CSP ──────────────────────────────────────────────────────────────────


@pytest.mark.parametrize("policy", [CSP_ENFORCED, CSP_REPORT_ONLY])
def test_csp_no_longer_advertises_google_font_hosts(policy: str) -> None:
    for host in GOOGLE_FONT_HOSTS:
        assert host not in policy, (
            f"CSP всё ещё разрешает {host}: политика шире, чем то, что грузится"
        )


def test_csp_still_allows_self_hosted_fonts() -> None:
    assert "font-src 'self' data:" in CSP_ENFORCED
    assert "font-src 'self'" in CSP_REPORT_ONLY
    # Яндекс трогать было нельзя — он консент-гейтед и живёт по своим правилам.
    assert "mc.yandex.ru" in CSP_ENFORCED


@pytest.mark.asyncio
async def test_served_csp_header_has_no_google_hosts(client: AsyncClient) -> None:
    resp = await client.get("/", follow_redirects=True)
    for header in ("Content-Security-Policy", "Content-Security-Policy-Report-Only"):
        value = resp.headers.get(header, "")
        for host in GOOGLE_FONT_HOSTS:
            assert host not in value, f"{header} всё ещё содержит {host}"
