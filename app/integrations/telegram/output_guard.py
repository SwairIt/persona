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


def _normalise_speaker(value: str) -> str:
    """Normalise a Telegram display name, ignoring emoji decorations."""
    clean = re.sub(r"[^\w\u0400-\u04ff -]+", "", value.lstrip("@").casefold())
    return " ".join(clean.split())


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
                return "\n".join(lines).strip()
        return text

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
    return "\n".join(kept).strip()


__all__ = ["persona_only_reply"]
