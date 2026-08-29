"""``| tojson`` внутри HTML-атрибута обязан идти с ``| forceescape``.

Фильтр ``tojson`` рассчитан на вставку внутрь ``<script>``: он экранирует
``<``, ``>``, ``&`` и ``'``, чтобы нельзя было закрыть тег или выйти из
JS-строки, но двойную кавычку оставляет как есть — в JSON она стоит вокруг
каждого ключа и каждой строки.

В атрибуте, ограниченном двойными кавычками, это ломает разметку на первой
же кавычке. Браузер обрезает значение, Alpine получает синтаксический мусор
вроде ``settingsHub([{`` и молча остаётся с пустыми данными. Внешне это не
похоже на ошибку — просто раздел выглядит пустым. Ровно так была сломана
страница ``/settings/hub``: заголовок и поиск на месте, а двенадцати
категорий и девяноста ссылок нет (см.
``tests/test_settings_hub_catalogue_reaches_page.py``).

Лечится добавлением ``| forceescape``: он экранирует кавычки в HTML-сущности,
а браузер разворачивает их обратно при разборе атрибута.

Тест смотрит ТОЛЬКО на атрибуты. Внутри ``<script>`` голый ``tojson`` — то,
что нужно, и трогать его нельзя: там ``&#34;`` не развернётся и сломает JS.
"""

from __future__ import annotations

import re
from pathlib import Path

TEMPLATES = Path(__file__).resolve().parents[1] / "app" / "web" / "templates"

_EXPR = re.compile(r"\{\{(?P<body>[^{}]*?\|\s*tojson[^{}]*?)\}\}")
_SCRIPTISH = re.compile(r"<(script|style)\b.*?</\1\s*>", re.S | re.I)


def _mask_scriptish(text: str) -> str:
    """Заменяем тела ``<script>``/``<style>`` пробелами, сохраняя длину.

    Без этого не обойтись: внутри JS сплошь и рядом встречаются ``<`` (в
    сравнениях, стрелках, комментариях), и проверка «мы внутри тега?»
    принимает середину скрипта за список атрибутов. Один раз уже приняла —
    и ``forceescape`` уехал в JS-литерал, где ``&#34;`` никто не разворачивает:
    чат встретил ``Unexpected token '&'`` и не поднялся вовсе. Голый
    ``tojson`` внутри ``<script>`` — правильный и единственно рабочий вариант.
    """
    return _SCRIPTISH.sub(lambda m: " " * len(m.group(0)), text)


def _enclosing_quote(text: str, position: int) -> str | None:
    """Какой кавычкой ограничен атрибут, внутри которого стоит позиция.

    Возвращает ``'"'``, ``"'"`` или ``None`` (вне тега / вне значения).

    Различать кавычки обязательно: ``tojson`` экранирует одинарную (в
    ``\\u0027``), но не двойную. Значит в ``x-data='{...}'`` голый ``tojson``
    БЕЗОПАСЕН и трогать его не надо, а в ``x-data="{...}"`` — ломает разметку.
    Так в проекте написана половина мест, и без этой проверки правка уехала бы
    туда, где всё и так корректно.

    Вызывать только на тексте, прошедшем :func:`_mask_scriptish`.
    """
    open_at = text.rfind("<", 0, position)
    if open_at < 0 or text.rfind(">", 0, position) > open_at:
        return None  # не внутри тега
    quote: str | None = None
    for ch in text[open_at:position]:
        if quote is None:
            if ch in "\"'":
                quote = ch
        elif ch == quote:
            quote = None
    return quote


def _inside_a_tag(text: str, position: int) -> bool:
    """True, когда позиция стоит внутри значения атрибута в двойных кавычках."""
    return _enclosing_quote(text, position) == '"'


def _offenders() -> list[str]:
    found: list[str] = []
    for path in sorted(TEMPLATES.rglob("*.html")):
        text = path.read_text(encoding="utf-8", errors="ignore")
        masked = _mask_scriptish(text)
        for match in _EXPR.finditer(text):
            if "forceescape" in match.group("body"):
                continue
            if not _inside_a_tag(masked, match.start()):
                continue
            line = text.count("\n", 0, match.start()) + 1
            found.append(f"{path.name}:{line}  {match.group(0).strip()[:90]}")
    return found


def test_no_template_puts_raw_tojson_into_an_attribute() -> None:
    offenders = _offenders()
    assert not offenders, (
        "tojson без forceescape внутри HTML-атрибута — значение оборвётся на "
        "первой кавычке, и блок отрисуется пустым:\n  " + "\n  ".join(offenders)
    )
