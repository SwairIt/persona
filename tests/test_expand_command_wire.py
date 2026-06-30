"""F6-03 — серверная развёртка турн-команд (/plan /ask /fast /deep /web …).

Баг: ``expand_command`` была определена, но НИКОГДА не вызывалась — турн-команды
не применяли режим/эффорт на сервере, если фронт не снял префикс сам (API/голос/
сбой JS). Здесь покрываем две вещи:

1. ``expand_command`` напрямую — ветки разбора команды.
2. ``_apply_turn_command`` (чистый хелпер из chat_sessions.py) — что
   api_send_stream реально применяет: снятие префикса + override режима/эффорта,
   приоритет явного body, неедение нераспознанного слэша и overlay-команд.
"""

from __future__ import annotations

from app.chat.commands import expand_command
from app.web.routes.chat_sessions import _apply_turn_command


# ── expand_command напрямую ───────────────────────────────────────────────


def test_expand_plan_sets_force_mode_and_strips_prefix() -> None:
    out = expand_command("/plan сделай рефакторинг")
    assert out is not None
    assert out["recognized"] is True
    assert out["force_mode"] == "plan"
    assert "force_effort" not in out
    # текст уходит модели без префикса '/plan'
    assert out["send_text"] == "сделай рефакторинг"


def test_expand_ask_and_auto_and_bypass_modes() -> None:
    assert expand_command("/ask вопрос")["force_mode"] == "ask"
    assert expand_command("/auto давай")["force_mode"] == "auto"
    assert expand_command("/bypass жми")["force_mode"] == "bypass"


def test_expand_deep_sets_force_effort() -> None:
    out = expand_command("/deep подумай как следует")
    assert out["recognized"] is True
    assert out["force_effort"] == "deep"
    assert "force_mode" not in out
    assert out["send_text"] == "подумай как следует"


def test_expand_fast_and_normal_efforts() -> None:
    assert expand_command("/fast быстро")["force_effort"] == "fast"
    assert expand_command("/normal обычно")["force_effort"] == "normal"


def test_expand_web_becomes_search_query_in_auto_mode() -> None:
    out = expand_command("/web погода в Москве")
    assert out["recognized"] is True
    assert out["force_mode"] == "auto"  # /web → авто-режим
    # превращается в поисковый запрос
    assert "погода в Москве" in out["send_text"]
    assert out["send_text"].lower().startswith("найди в интернете")


def test_expand_web_empty_arg_yields_empty_send_text() -> None:
    out = expand_command("/web")
    assert out["recognized"] is True
    assert out["force_mode"] == "auto"
    assert out["send_text"] == ""


def test_expand_unknown_slash_not_eaten() -> None:
    out = expand_command("/nonsense привет")
    assert out is not None
    assert out["recognized"] is False  # нераспознанный слэш-токен
    assert out["name"] == "nonsense"
    # send_text НЕ выставляется — текст не съедаем (уйдёт как есть)
    assert "send_text" not in out


def test_expand_plain_text_is_none() -> None:
    assert expand_command("обычное сообщение без слэша") is None
    # двойной слэш — литерал, не команда
    assert expand_command("//literal") is None


def test_expand_overlay_command_no_directives() -> None:
    # overlay-команда (/review) распознаётся, но без force_mode/effort/send_text —
    # её инструкция идёт через body['cmd'], текст не разворачиваем.
    out = expand_command("/review мой код")
    assert out["recognized"] is True
    assert out["type"] == "turn"
    assert "force_mode" not in out
    assert "force_effort" not in out
    assert "send_text" not in out


# ── _apply_turn_command (что применяет api_send_stream) ────────────────────


def test_apply_plan_strips_prefix_and_forces_mode() -> None:
    text, mode, effort = _apply_turn_command("/plan сделай X")
    assert text == "сделай X"
    assert mode == "plan"
    assert effort is None


def test_apply_deep_forces_effort_only() -> None:
    text, mode, effort = _apply_turn_command("/deep копай глубже")
    assert text == "копай глубже"
    assert mode is None
    assert effort == "deep"


def test_apply_web_search_and_auto_mode() -> None:
    text, mode, effort = _apply_turn_command("/web курс рубля")
    assert "курс рубля" in text
    assert mode == "auto"
    assert effort is None


def test_apply_unknown_slash_text_not_eaten() -> None:
    text, mode, effort = _apply_turn_command("/whatisthis привет")
    # нераспознанный слэш — текст уходит как есть, без override
    assert text == "/whatisthis привет"
    assert mode is None
    assert effort is None


def test_apply_plain_text_passthrough() -> None:
    text, mode, effort = _apply_turn_command("просто текст")
    assert text == "просто текст"
    assert mode is None
    assert effort is None


def test_apply_overlay_command_text_preserved() -> None:
    # overlay /review без body['cmd'] не разворачивается тут — текст не трогаем.
    text, mode, effort = _apply_turn_command("/review мой код")
    assert text == "/review мой код"
    assert mode is None
    assert effort is None


def test_apply_body_mode_wins_over_command() -> None:
    # фронт уже выставил режим явно (body) — он в приоритете над командой.
    text, mode, effort = _apply_turn_command("/plan текст", body_mode="bypass")
    assert text == "текст"  # префикс всё равно снят
    assert mode == "bypass"  # body победил команду
    assert effort is None


def test_apply_body_effort_wins_over_command() -> None:
    text, mode, effort = _apply_turn_command("/deep текст", body_effort="fast")
    assert text == "текст"
    assert effort == "fast"  # body победил команду


def test_apply_invalid_body_values_ignored() -> None:
    # мусорные body-значения не подхватываются — берётся override команды.
    text, mode, effort = _apply_turn_command(
        "/plan текст", body_mode="garbage", body_effort="garbage"
    )
    assert mode == "plan"
    assert effort is None


def test_apply_empty_plan_arg_yields_empty_text() -> None:
    # «/plan» без аргумента → пустой текст (HTTP-слой потом отбросит как пусто).
    text, mode, effort = _apply_turn_command("/plan")
    assert text == ""
    assert mode == "plan"
