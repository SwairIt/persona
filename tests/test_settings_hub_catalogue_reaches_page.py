"""Каталог настроек должен доезжать до страницы, а не обрываться в атрибуте.

``settings_hub.html`` отдаёт каталог в разметку так::

    x-data="settingsHub({{ categories_json | tojson }})"

Фильтр ``tojson`` экранирует ``<``, ``>``, ``&`` и ``'``, но НЕ двойную
кавычку — она в JSON на каждом ключе. Внутри атрибута, ограниченного
двойными кавычками, первая же такая кавычка закрывает атрибут: Alpine
получает обрывок ``settingsHub([{``, роняет выражение и остаётся с пустым
``cats``. Снаружи это выглядит не как ошибка, а как **пустая страница
настроек**: заголовок и поиск на месте, а все двенадцать категорий и
девяносто ссылок просто не нарисованы.

Проверено в браузере до фикса: ``catsInState: 0`` и
``PAGEERROR Unexpected token ';'``.

Тест держит контракт ШАБЛОНА: что бы ни отдал роут, каталог обязан
пережить путешествие в HTML-атрибут целиком.
"""

from __future__ import annotations

import json
from html.parser import HTMLParser

from app.web.routes.settings_hub import _categories_json
from app.web.templates_engine import templates


class _XDataFinder(HTMLParser):
    """Читаем атрибут ровно так, как его прочитает браузер.

    Именно поэтому здесь настоящий HTML-парсер, а не регулярка: весь баг в
    том, ГДЕ по мнению парсера кончается значение атрибута. Регулярка бы
    «дочитала» до нужной кавычки и спрятала проблему.
    """

    value: str | None = None

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        for name, val in attrs:
            if name == "x-data" and val and val.startswith("settingsHub("):
                self.value = val


def _render(categories: list[dict[str, object]]) -> str:
    return templates.env.get_template("settings_hub.html").render(
        request=None,
        session=None,
        title="Настройки",
        active_nav="settings",
        categories=categories,
        categories_json=categories,
        is_owner=True,
    )


def _catalogue_from_attribute(page: str) -> list[dict[str, object]]:
    """Достаём каталог ровно так, как его увидит браузер."""
    finder = _XDataFinder()
    finder.feed(page)
    assert finder.value, 'в разметке нет x-data="settingsHub(...)"'
    expression = finder.value.strip()
    assert expression.endswith(")"), (
        "значение x-data оборвалось внутри JSON — браузер получит незакрытый "
        f"вызов и Alpine упадёт: {expression[-60:]!r}"
    )
    return json.loads(expression[len("settingsHub(") : -1])


def test_every_category_survives_the_html_attribute() -> None:
    """Все категории и страницы доезжают до Alpine без обрыва."""
    categories = _categories_json()
    assert categories, "каталог настроек пуст — проверять нечего"

    parsed = _catalogue_from_attribute(_render(categories))

    assert len(parsed) == len(categories)
    assert sum(len(c["pages"]) for c in parsed) == sum(
        len(c["pages"]) for c in categories  # type: ignore[arg-type]
    )


def test_a_quote_in_a_label_does_not_break_the_page() -> None:
    """Кавычка в подписи не должна закрывать атрибут раньше времени."""
    categories = [
        {
            "title": 'Тест "в кавычках"',
            "icon": "🧪",
            "description": "Проверка экранирования",
            "advanced": False,
            "pages": [{"href": "/settings/x", "label": 'ключ "api"', "keywords": ""}],
        }
    ]

    parsed = _catalogue_from_attribute(_render(categories))

    assert parsed[0]["title"] == 'Тест "в кавычках"'
    assert parsed[0]["pages"][0]["label"] == 'ключ "api"'
