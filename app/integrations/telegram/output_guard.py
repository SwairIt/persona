"""Hard postconditions for text Persona sends to Telegram."""

from __future__ import annotations

import re

_SPEAKER_RE = re.compile(
    r"^\s*(?:[-*]\s*)?(?P<speaker>@?[\w\u0400-\u04ff -]{1,40})"
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
        speaker = " ".join(match.group("speaker").lstrip("@").casefold().split())
        if speaker in _PERSONA_ALIASES or speaker in _KNOWN_OTHERS:
            parsed.append((index, speaker, match.group("text").strip()))
    if len(parsed) < 2:
        # Even without a full role-play script, models sometimes sign their
        # answer as ``Персик: ...``. Telegram already shows the sender, so a
        # leading Persona label is redundant and makes the message look like
        # generated dialogue. Strip only Persona's own label; labels naming an
        # addressee (``Клод: ...``) remain untouched.
        first = _SPEAKER_RE.match(lines[0])
        if first:
            speaker = " ".join(
                first.group("speaker").lstrip("@").casefold().split()
            )
            if speaker in _PERSONA_ALIASES:
                lines[0] = first.group("text").strip()
                return "\n".join(lines).strip()
        return text

    kept: list[str] = []
    persona_block = False
    for line in lines:
        match = _SPEAKER_RE.match(line)
        if match:
            speaker = " ".join(match.group("speaker").lstrip("@").casefold().split())
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
    return "\n".join(kept).strip()


__all__ = ["persona_only_reply"]
