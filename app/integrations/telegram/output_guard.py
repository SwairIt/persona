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
# Common Russian discourse nouns that are routinely followed by a colon in
# ordinary prose (``Условие: a < b``, ``Итог: ...``). A bare regex cannot
# tell these apart from a person's name by shape alone (both are a single
# capitalised word), so anything on this list is never treated as a speaker
# label even when it otherwise matches the name shape below.
_NON_NAME_LEAD_WORDS = {
    "условие", "пример", "итог", "итого", "вопрос", "внимание", "важно",
    "примечание", "кстати", "итак", "вывод", "заметка", "результат",
    "причина", "решение", "проблема", "совет", "подсказка", "файл",
    "ссылка", "ошибка", "статус", "дата", "время", "тема", "цель",
    "задача", "плюс", "минус", "всего", "например", "формула", "правило",
    "определение", "замечание", "справка", "инструкция", "заголовок",
    "note", "example", "warning", "important", "result", "summary",
}
# A plausible person name: one or two Title-Case words made of letters only
# (Cyrillic or Latin). Rules out formulas, acronyms and punctuation-bearing
# "labels" like ``a < b`` or ``TODO``.
_NAME_LABEL_RE = re.compile(
    r"^[A-ZА-ЯЁ][a-zа-яё]{1,19}(?:[ \-][A-ZА-ЯЁ][a-zа-яё]{1,19})?$"
)
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
    r"^.*(?:"
    r"AUTHORITATIVE\s+CURRENT\s+TELEGRAM\s+TURN"
    r"|SERVER-VERIFIED\s+TELEGRAM\s+IDENTITY"
    r"|-\s*current_message_author_\w*"
    r"|-\s*sole_owner_creator_id"
    r"|Only\s+Telegram\s+user_id="
    # The identity_context() JSON payload (people.py) is emitted as one long
    # line via json.dumps(..., separators=(",",":")); its `<`/`>` are escaped
    # to </> so the tag scanner above never sees it. Catch it by
    # its fixed key names instead, wherever they land in the line.
    r'|"sole_owner_creator"'
    r'|"people_seen_in_this_chat"'
    r'|"untrusted_remembered_claims_by_current_sender"'
    r'|"trusted_owner_notes"'
    r").*$",
    re.IGNORECASE | re.MULTILINE,
)
# Collapse only space/tab runs that follow a non-whitespace character, so
# line-leading indentation (e.g. inside a code block) is never touched.
_MID_LINE_SPACE_RE = re.compile(r"(?<=\S)[ \t]+")
_TRAILING_LINE_SPACE_RE = re.compile(r"[ \t]+$", re.MULTILINE)


def _same_tag_event_re(tag: str) -> re.Pattern[str]:
    """Match only the opening/closing pair for one specific tag name."""
    return re.compile(
        r"<(?P<slash>/?)" + re.escape(tag) + r"\b[^>]*>",
        re.IGNORECASE,
    )


def _find_block_end(text: str, tag: str, search_from: int) -> int | None:
    """Return the end position of the closer that matches ``tag``'s opener.

    Tracks nesting depth for same-named tags: an opener increments depth, a
    closer decrements it, and the block ends at the closer that brings depth
    back to 0. This lets a nested occurrence of the same tag collapse fully
    (no orphaned closer/tail) while two *sibling* blocks of the same tag each
    close on their own nearest closer, leaving the legitimate text between
    them untouched. Returns ``None`` if depth never reaches 0 (unmatched
    opener) — the caller then eats to end-of-string.
    """
    same_tag_re = _same_tag_event_re(tag)
    depth = 1
    pos = search_from
    while True:
        match = same_tag_re.search(text, pos)
        if match is None:
            return None
        if match.group("slash"):
            depth -= 1
            if depth == 0:
                return match.end()
        else:
            depth += 1
        pos = match.end()


def _strip_internal_tag_blocks(text: str) -> str:
    """Remove every internal-tag block, however it is malformed.

    An unmatched opening tag eats to the end of the string (a truncated
    generation leaves the rest as internal text). Symmetrically, a stray
    closing tag with no opening before it eats from the start of the string
    up to and including itself — content before it cannot be trusted either,
    since it is what led into that unmatched closer. A matched pair closes on
    the same-named closer that brings nesting depth back to 0 (see
    ``_find_block_end``), so a nested occurrence of the same tag collapses
    fully while sibling (non-nested) blocks of the same tag each close on
    their own closer, preserving legitimate text in between.
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
        end = _find_block_end(text, match.group("tag"), match.end())
        if end is None:
            # Unmatched opening tag: everything after it is internal.
            pos = length
            break
        pos = end
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


def _looks_like_speaker_name(raw_speaker: str) -> bool:
    """Heuristic: does a leading ``Word:`` label read as someone's name?

    Requires the plain Title-Case letters-only shape of a name (so a
    formula or acronym before a colon never qualifies) and excludes common
    Russian discourse nouns that are routinely followed by a colon in
    ordinary prose (``\u0423\u0441\u043b\u043e\u0432\u0438\u0435: a < b``), so plain sentences are never
    mistaken for dialogue.
    """
    if not _NAME_LABEL_RE.match(raw_speaker.strip()):
        return False
    words = _normalise_speaker(raw_speaker).split()
    return bool(words) and not any(word in _NON_NAME_LEAD_WORDS for word in words)


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


def strip_leading_speaker_label(text: str) -> str:
    """Strip a leading ``Имя:`` speaker label from the first line of *text*.

    Telegram already shows who is speaking, so a name-colon label at the
    very start of Persona's own words never reads as her addressing someone
    — it reads as her impersonating them. This is the single-label half of
    ``persona_only_reply``'s guard, factored out so it can also be applied
    when replaying a *stored* assistant message back into the prompt: a past
    reply that slipped through with a leading label should not keep
    reinforcing that habit every time it is loaded into context. Returns
    *text* unchanged if the first line isn't a name label (see
    ``_looks_like_speaker_name`` for what counts as one).
    """
    if not text:
        return text
    lines = text.splitlines()
    if not lines:
        return text
    match = _SPEAKER_RE.match(lines[0])
    if not match:
        return text
    speaker_raw = match.group("speaker")
    speaker = _normalise_speaker(speaker_raw)
    if (
        speaker in _PERSONA_ALIASES
        or speaker in _KNOWN_OTHERS
        or _looks_like_speaker_name(speaker_raw)
    ):
        lines[0] = match.group("text").strip()
        return "\n".join(lines)
    return text


def persona_only_reply(value: str) -> str:
    """Remove fabricated multi-speaker scripts while preserving Persona's words.

    Telegram already shows who sent the message, so a leading ``Имя:``
    label on Persona's own reply never reads as her addressing someone —
    it reads as her *being* that someone. A single such label at the start
    of the message is stripped regardless of whose name it is (her own,
    another agent's, or an arbitrary person's). Two or more speaker-labelled
    blocks indicate role-play/script output; in that case only
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
        # answer as ``Клод: ...`` or ``Персик: ...``. Telegram already shows
        # the sender, so ANY leading name label is redundant at best and, at
        # worst, makes Persona's own message read as though someone else
        # wrote it. Strip it regardless of whose name it is — her own alias,
        # a known other agent, or an arbitrary person — as long as it looks
        # like a name label and not the start of an ordinary sentence (see
        # ``_looks_like_speaker_name``). Addressing someone stays possible in
        # natural speech (``Клод, глянь логи``): that has no colon, so it
        # never matches here.
        return _without_support_cliches(strip_leading_speaker_label(text))

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


__all__ = [
    "persona_only_reply",
    "strip_internal_markup",
    "strip_leading_speaker_label",
]
