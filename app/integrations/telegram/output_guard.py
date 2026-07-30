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
    "извини, что засмущал",
    "извини что засмущал",
    "извиняюсь, что засмущал",
    "извиняюсь что засмущал",
    "стремлюсь быть конструктивн",
    "стараюсь быть конструктивн",
    "не хотел обидеть",
    "не хотела обидеть",
    # Только явные корпоративные формулы. Голые подстроки «пересмотр» /
    # «изменить подход» вырезали обычные живые предложения целиком — ответ
    # выглядел рваным и недописанным.
    "пересмотрим подход",
    "пересмотреть подход",
    "пересмотрим наш",
    "изменим подход",
    "изменить подход",
    "сменим подход",
    "давай без оскорблений",
    "не буду опускаться",
    "предпочитаю оставаться",
    "понимаю вашу точку зрения",
    "понимаю твою точку зрения",
    "стараюсь быть вежлив",
    "стараюсь оставаться вежлив",
    "в нашем взаимодействии",
    "в нашем диалоге",
    "i'm always here to help",
    "i am always here to help",
    "thanks for reaching out",
)
_SOFT_REFUSAL_PREFIX_RE = re.compile(
    r"^\s*(?:(?:я\s+)?(?:понимаю|слышу)\s+(?:тебя|вас)?\s*,?\s*"
    r"(?:однако|но)\s+|(?:мне\s+жаль|извини(?:те)?|к\s+сожалению)\s*,?\s*"
    r"(?:но\s+)?)"
    r"(?P<refusal>я\s+не\s+могу\b)",
    re.IGNORECASE,
)
_AI_REFUSAL_PREFIX_RE = re.compile(
    r"^\s*(?:как|будучи)\s+(?:искусственный\s+интеллект|ии)\s*,?\s*"
    r"(?P<refusal>я\s+не\s+могу\b)",
    re.IGNORECASE,
)

# Служебные секции системного промпта. Модель иногда воспроизводит их
# дословно; в чат они попадать не должны ни при каких условиях.
_INTERNAL_TAGS = (
    "TRUSTED_TELEGRAM_IDENTITY",
    "TRUSTED_PERSONA_STYLE",
    "UNTRUSTED_TELEGRAM_ACTION_JSON",
    "UNTRUSTED_GROUP_TRANSCRIPT",
    "GROUP_RULES",
    "ADAPTIVE_PERSONA_LAYER",
    "tool",
)
_INTERNAL_TAG_ALTERNATION = "|".join(_INTERNAL_TAGS)
# Matches either an opening (``<TAG ...>``) or a closing (``</TAG>``) internal
# tag; the ``slash`` group tells the scanner below which one it hit.
_INTERNAL_TAG_EVENT_RE = re.compile(
    r"<(?P<slash>/?)(?P<tag>" + _INTERNAL_TAG_ALTERNATION + r")\b[^>]*>",
    re.IGNORECASE,
)
_INTERNAL_LINE_RE = re.compile(
    r"^\s*(?:"
    r"AUTHORITATIVE\s+CURRENT\s+TELEGRAM\s+TURN"
    r"|SERVER-VERIFIED\s+TELEGRAM\s+IDENTITY"
    r"|-\s*current_message_author_\w*"
    r"|-\s*sole_owner_creator_id"
    r"|Only\s+Telegram\s+user_id="
    r").*$",
    re.IGNORECASE | re.MULTILINE,
)
# Collapse only space/tab runs that follow a non-whitespace character, so
# line-leading indentation (e.g. inside a code block) is never touched.
_MID_LINE_SPACE_RE = re.compile(r"(?<=\S)[ \t]+")
_TRAILING_LINE_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _closing_tag_re(tag: str) -> re.Pattern[str]:
    return re.compile(r"</" + re.escape(tag) + r"\s*>", re.IGNORECASE)


def _strip_internal_tag_blocks(text: str) -> str:
    """Remove every internal-tag block, however it is malformed.

    An unmatched opening tag eats to the end of the string (a truncated
    generation leaves the rest as internal text). Symmetrically, a stray
    closing tag with no opening before it eats from the start of the string
    up to and including itself — content before it cannot be trusted either,
    since it is what led into that unmatched closer. A matched pair consumes
    through the *last* same-named closing tag rather than the first, so a
    nested occurrence of the same tag cannot leave an orphaned closer and a
    tail of internal content behind.
    """
    kept: list[str] = []
    pos = 0
    length = len(text)
    while pos < length:
        match = _INTERNAL_TAG_EVENT_RE.search(text, pos)
        if match is None:
            kept.append(text[pos:])
            break
        if match.group("slash"):
            # Orphaned closing tag: discard everything gathered so far plus
            # this tag itself, then resume scanning after it.
            kept.clear()
            pos = match.end()
            continue
        kept.append(text[pos:match.start()])
        closing_re = _closing_tag_re(match.group("tag"))
        last_close = None
        for close_match in closing_re.finditer(text, match.end()):
            last_close = close_match
        if last_close is None:
            # Unmatched opening tag: everything after it is internal.
            pos = length
            break
        pos = last_close.end()
    return "".join(kept)


def strip_internal_markup(value: str) -> str:
    """Удалить служебные секции промпта, если модель их воспроизвела.

    Ловит и незакрытый тег: обрыв генерации оставляет открывающий тег без
    пары, и всё после него — служебный текст. Симметрично: одинокий
    закрывающий тег без пары съедает всё до себя включительно, а вложенный
    тег с тем же именем закрывается по последнему закрывающему тегу, чтобы
    не оставался «хвост» служебного текста.
    """
    text = str(value or "")
    text = _strip_internal_tag_blocks(text)
    text = _INTERNAL_LINE_RE.sub("", text)
    text = _MID_LINE_SPACE_RE.sub(" ", text)
    text = _TRAILING_LINE_SPACE_RE.sub("", text)
    return text.strip()


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
    clean = " ".join(kept).strip()
    if not clean and str(value or "").strip():
        return "Ладно."
    clean = _SOFT_REFUSAL_PREFIX_RE.sub(r"Нет: \g<refusal>", clean)
    clean = _AI_REFUSAL_PREFIX_RE.sub(r"Нет: \g<refusal>", clean)
    return clean


def persona_only_reply(value: str) -> str:
    """Remove fabricated multi-speaker scripts while preserving Persona's words.

    A single ``Клод: проверь...`` can be a legitimate address. Two or more
    speaker-labelled blocks indicate role-play/script output; in that case only
    Persona-labelled blocks survive. If Persona wrote solely for others, the
    safe result is silence.
    """
    text = strip_internal_markup(value)
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


__all__ = ["persona_only_reply", "strip_internal_markup"]
