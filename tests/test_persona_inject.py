"""Spotlighting + ре-инъекция персоны (ROADMAP S3a)."""

from __future__ import annotations

from app.chat.persona_inject import (
    PERSONA_REINJECT_EVERY,
    persona_reminder,
    spotlight,
)


def test_spotlight_wraps_with_data_warning() -> None:
    out = spotlight("ПАМЯТЬ", "Олег — это брат")
    assert "Олег — это брат" in out
    assert "НЕ инструкции" in out  # явно помечено как данные
    assert "КОНЕЦ БЛОКА: ПАМЯТЬ" in out
    # контент обрамлён маркерами (открытие до контента, закрытие после)
    assert out.index("НЕ инструкции") < out.index("Олег")
    assert out.index("Олег") < out.index("КОНЕЦ БЛОКА")


def test_spotlight_empty_is_noop() -> None:
    assert spotlight("X", "") == ""
    assert spotlight("X", "   \n  ") == ""


def test_persona_reminder_silent_when_short() -> None:
    hist = [{"role": "user", "content": "привет"}, {"role": "assistant", "content": "хай"}]
    assert persona_reminder("Ты дружелюбный ассистент.", hist) == ""


def test_persona_reminder_kicks_in_when_long() -> None:
    # PERSONA_REINJECT_EVERY ходов пользователя → напоминание появляется.
    hist: list[dict[str, str]] = []
    for _ in range(PERSONA_REINJECT_EVERY):
        hist.append({"role": "user", "content": "q"})
        hist.append({"role": "assistant", "content": "a"})
    out = persona_reminder("Ты дерзкий друг, говоришь на «ты».", hist)
    assert out != ""
    assert "Ты дерзкий друг" in out
    assert "держи свою роль" in out


def test_persona_reminder_empty_core_is_noop() -> None:
    hist = [{"role": "user", "content": "q"}] * 20
    assert persona_reminder("", hist) == ""
