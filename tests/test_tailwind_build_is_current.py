"""Собранный Tailwind должен покрывать классы, которые реально стоят в шаблонах.

Play-CDN генерировал CSS в браузере и прощал что угодно: дописал класс в
шаблон — он тут же работал. Сборка (2.36.0) так не умеет — она сканирует
исходники заранее, поэтому забытая пересборка приводит к классу, который
молча не применяется. Внешне это не падение, а «поехавшая вёрстка», которую
замечают уже на проде.

Тест ловит ровно этот случай: берёт классы из шаблонов и проверяет, что
селекторы для них есть в ``vendor/tailwind-built.css``. Рецепт пересборки —
``ops/tailwind/README.md``.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
BUILT = ROOT / "app" / "web" / "static" / "vendor" / "tailwind-built.css"
TEMPLATES = ROOT / "app" / "web" / "templates"
CONFIG = ROOT / "tailwind.config.js"
STATIC = ROOT / "app" / "web" / "static"

#: Утилиты, по которым судим о свежести сборки. Намеренно берём частотные
#: префиксы, а не все подряд: цель — поймать протухший файл, а не
#: пересчитать Tailwind.
_TRACKED = re.compile(
    r"\b("
    r"(?:sm:|md:|lg:|xl:|2xl:|hover:|focus:|dark:)*"
    r"(?:bg|text|border|rounded|flex|grid|gap|p|px|py|m|mx|my|mt|mb|w|h|max-w|min-h)"
    r"-[a-z0-9/\[\]#.%-]+"
    r")\b"
)

#: Классы Tailwind живут в ``class="…"``; ловим и Jinja-строки внутри.
_CLASS_ATTR = re.compile(r'class="([^"]*)"')


def _escape_for_css(cls: str) -> str:
    """Как Tailwind экранирует класс в селекторе: ``w-1/2`` → ``w-1\\/2``."""
    out = []
    for ch in cls:
        if ch in "/:.[]#%(),":
            out.append("\\" + ch)
        else:
            out.append(ch)
    return "".join(out)


def _configured_shades() -> dict[str, set[str]]:
    """Оттенки фирменных палитр из ``tailwind.config.js``.

    Нужны, чтобы отличить «забыли пересобрать» от «класса не существует в
    принципе». В шаблонах есть, например, ``bg-accent-700`` — а в палитре
    accent только 400/500/600. Такой класс не давал стиля и при Play-CDN
    (конфиг был тот же), так что к свежести сборки он отношения не имеет.
    """
    text = CONFIG.read_text(encoding="utf-8")
    shades: dict[str, set[str]] = {}
    for name in ("ink", "accent"):
        match = re.search(rf"\b{name}:\s*\{{([^}}]*)\}}", text)
        if match:
            shades[name] = set(re.findall(r"(\d+):", match.group(1)))
    return shades


def _own_css_classes() -> set[str]:
    """Классы, объявленные в НАШИХ css (``.bg-grid``, ``.bg-mesh`` и т.п.).

    Они похожи на утилиты Tailwind по написанию, но приходят из
    ``static/landing/style.css`` и других своих файлов, а не из сборки.
    """
    found: set[str] = set()
    for path in STATIC.rglob("*.css"):
        if path.name == "tailwind-built.css":
            continue
        text = path.read_text(encoding="utf-8", errors="ignore")
        found.update(re.findall(r"\.([a-z][a-z0-9-]+)\s*[,{]", text))
    return found


def _is_generatable(cls: str, shades: dict[str, set[str]]) -> bool:
    """False для классов, которые Tailwind не может собрать по этому конфигу."""
    match = re.fullmatch(
        r"(?:[a-z0-9]+:)*(?:bg|text|border)-(ink|accent)-(\d+)(?:/\d+)?", cls
    )
    if not match:
        return True
    palette, shade = match.group(1), match.group(2)
    return shade in shades.get(palette, set())


def _template_classes() -> set[str]:
    found: set[str] = set()
    for path in TEMPLATES.rglob("*.html"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        for attr in _CLASS_ATTR.findall(text):
            if "{{" in attr or "{%" in attr:
                # Значение собирается Jinja — целого имени класса тут может и
                # не быть, судить о свежести сборки по нему нельзя.
                continue
            for token in attr.split():
                if _TRACKED.fullmatch(token):
                    found.add(token)
    return found


def test_built_css_exists() -> None:
    assert BUILT.exists(), (
        "Нет app/web/static/vendor/tailwind-built.css — собери его по "
        "ops/tailwind/README.md"
    )


def test_no_template_still_pulls_the_play_cdn() -> None:
    """Никто не вернул 407 КБ JIT-компилятора обратно в шаблоны."""
    offenders = [
        path.relative_to(ROOT).as_posix()
        for path in TEMPLATES.rglob("*.html")
        if "tailwind-play.js" in path.read_text(encoding="utf-8", errors="ignore")
    ]
    assert not offenders, (
        "Tailwind Play CDN блокирует рендер и компилирует CSS в браузере; "
        f"верни vendor/tailwind-built.css в: {offenders}"
    )


def test_classes_used_in_templates_are_present_in_the_build() -> None:
    """Сборка не протухла относительно шаблонов."""
    if not BUILT.exists():
        pytest.skip("сборки нет — это ловит отдельный тест")
    css = BUILT.read_text(encoding="utf-8", errors="ignore")
    used = _template_classes()
    assert used, "не нашли ни одного класса в шаблонах — сломался разбор"

    shades = _configured_shades()
    assert shades.get("accent"), "не разобрали палитру из tailwind.config.js"
    own = _own_css_classes()

    expected = {
        cls
        for cls in used
        if cls not in own and _is_generatable(cls, shades)
    }
    missing = sorted(cls for cls in expected if f".{_escape_for_css(cls)}" not in css)
    assert not missing, (
        f"{len(missing)} классов из шаблонов нет в собранном CSS — пересобери "
        f"(ops/tailwind/README.md). Например: {missing[:15]}"
    )
