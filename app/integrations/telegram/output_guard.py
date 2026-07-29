"""Hard postconditions for text Persona sends to Telegram."""

from __future__ import annotations

import re

_SPEAKER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<speaker>[^:\n]{1,50})"
    r":\s*(?P<text>.*)$",
    re.UNICODE,
)
_PERSONA_ALIASES = {
    "persona",
    "персона",
    "персоныч",
    "персик",
    "перс",
}
_KNOWN_OTHERS = {
    "claude",
    "клод",
    "инди",
    "индик",
    "indi",
}
_SUPPORT_CLICHE_MARKERS = (
    "спасибо за сообщение",
    "спасибо, что поделил",
    "всегда здесь, чтобы помочь",
    "всегда здесь чтобы помочь",
    "всегда готов помочь",
    "всегда готова помочь",
    "я всегда рядом",
    "чем могу помочь",
    "обращайся, если",
    "обращайтесь, если",
    "давай перейдем к делу",
    "давай перейдём к делу",
    "выход из трудной ситуации",
    "вышел из трудной ситуации",
    "вышла из трудной ситуации",
    "ты большой молодец",
    "ты молодец",
    "горжусь тобой",
    "это показывает твою силу",
    "i'm always here to help",
    "i am always here to help",
    "thanks for reaching out",
)


def _normalise_speaker(value: str) -> str:
    """Normalise a Telegram display name, ignoring emoji decorations."""
    clean = re.sub(r"[^\w\u0400-\u04ff -]+", "", value.lstrip("@").casefold())
    return " ".join(clean.split())


def _without_support_cliches(value: str) -> str:
    """Drop stock support/praise sentences from a conversational reply."""
    pieces = re.split(r"(?<=[.!?…])(?:\s+|$)|\n+", str(value or "").strip())
    kept = [
        piece.strip()
        for piece in pieces
        if piece.strip()
        and not any(
            marker in piece.casefold() for marker in _SUPPORT_CLICHE_MARKERS
        )
    ]
    return " ".join(kept).strip()


def persona_only_reply(value: str) -> str:
    """Remove fabricated multi-speaker scripts while preserving Persona's words.

    A single ``Клод: проверь...`` can be a legitimate address. Two or more
    speaker-labelled blocks indicate role-play/script output; in that case only
    Persona-labelled blocks survive. If Persona wrote solely for others, the
    safe result is silence.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    lines = text.splitlines()
    parsed: list[tuple[int, str, str]] = []
    for index, line in enumerate(lines):
        match = _SPEAKER_RE.match(line)
        if not match:
            continue
        speaker = _normalise_speaker(match.group("speaker"))
        if speaker:
            parsed.append((index, speaker, match.group("text").strip()))
    speakers = [item[1] for item in parsed]
    script_mode = len(parsed) >= 2 and (
        any(speaker in _PERSONA_ALIASES for speaker in speakers)
        or any(speaker in _KNOWN_OTHERS for speaker in speakers)
        or len(parsed) >= 3
        or len(set(speakers)) < len(speakers)
    )
    if not script_mode:
        # Even without a full role-play script, models sometimes sign their
        # answer as ``Персик: ...``. Telegram already shows the sender, so a
        # leading Persona label is redundant and makes the message look like
        # generated dialogue. Strip only Persona's own label; labels naming an
        # addressee (``Клод: ...``) remain untouched.
        first = _SPEAKER_RE.match(lines[0])
        if first:
            speaker = _normalise_speaker(first.group("speaker"))
            if speaker in _PERSONA_ALIASES:
                lines[0] = first.group("text").strip()
                return _without_support_cliches("\n".join(lines))
        return _without_support_cliches(text)

    kept: list[str] = []
    persona_block = False
    for line in lines:
        match = _SPEAKER_RE.match(line)
        if match:
            speaker = _normalise_speaker(match.group("speaker"))
            if speaker in _PERSONA_ALIASES:
                persona_block = True
                content = match.group("text").strip()
                if content:
                    kept.append(content)
                continue
            # Once script mode is confirmed, every non-Persona speaker label
            # ends the Persona block, including people not known in advance.
            persona_block = False
            continue
        if persona_block and line.strip():
            kept.append(line.strip())
    return _without_support_cliches("\n".join(kept))


__all__ = ["persona_only_reply"]
