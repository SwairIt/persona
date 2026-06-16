"""Онбординг: пустой экран чата содержит примеры-кейсы + сид черновика (S2c).

Чисто шаблонный guard — рендерим chat_index без активной сессии и проверяем,
что появились кнопки-примеры и механика ?draft=.
"""

from __future__ import annotations

from app.web.templates_engine import templates


def _render_empty() -> str:
    t = templates.env.get_template("chat_index.html")
    return t.render(
        request=None,
        app_version="test",
        title="t",
        active_nav="chat",
        sessions=[],
        active_session=None,
        messages=[],
        adv={},
        provider_badge={"provider": "ollama", "is_local": True},
        lang="ru",
        t=lambda *a, **k: (a[0] if a else ""),
    )


def test_empty_state_has_onboarding_cases() -> None:
    html = _render_empty()
    assert "onboardCases" in html  # массив примеров в Alpine
    assert "newChatWith" in html  # клик создаёт чат с текстом
    assert "?draft=" in html  # сид черновика в URL
    assert "пустой чат" in html  # запасной выход


def test_empty_state_shows_privacy_badge_context() -> None:
    # На пустом экране бейдж в шапке не рисуется (нет active_session),
    # но провайдер всё равно прокинут без падений рендера.
    html = _render_empty()
    assert "Чат с памятью" in html
