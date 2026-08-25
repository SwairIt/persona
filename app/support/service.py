"""Правила формы поддержки: валидация, лимиты, огрубление контекста.

ЧИСТЫЕ функции — ни БД, ни ``Request``, ни сети. Всё, что зависит от
окружения (IP, соль, время), приходит аргументами. Так правила проверяются
тестами по одному, а роут остаётся тонким.

Почему отказ ЧЕСТНЫЙ, а не «тихий успех»
----------------------------------------
Соблазн понятен: показать боту «спасибо, отправлено» и выбросить письмо.
Но ту же страницу видит ЖИВОЙ человек, у которого сработала эвристика
(вставил готовый текст из блокнота за две секунды; браузер сам заполнил
скрытое поле). Он уйдёт уверенный, что владелец читает его обращение,
которого нет. Для продукта, чья цель — «чтобы дошло», это худший исход из
возможных, хуже пропущенного спама. Поэтому каждый отказ возвращает
:class:`Rejection` с человеческой русской причиной, а форма перерисовывается
с уже введённым текстом.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final

#: Границы полей. Верхние — защита диска и почты (обращение уезжает письмом),
#: нижние — против «...» и пустого «привет» в теме.
SUBJECT_MIN: Final[int] = 3
SUBJECT_MAX: Final[int] = 200
BODY_MIN: Final[int] = 10
BODY_MAX: Final[int] = 4000
#: RFC 5321 максимум для адреса — длиннее просто не бывает.
EMAIL_MAX: Final[int] = 254

#: Сколько секунд человек минимально проводит на форме. Осмысленное обращение
#: невозможно набрать быстрее; бот отправляет за доли секунды.
MIN_SECONDS_ON_FORM: Final[int] = 4
#: Через сколько подпись формы протухает. Сутки: вкладку оставляют открытой.
MAX_SECONDS_ON_FORM: Final[int] = 24 * 60 * 60

#: Намеренно СЛАБАЯ проверка адреса. Строгая регулярка по RFC 5322 отвергает
#: валидные адреса (плюсы, юникод, редкие TLD) — а цена ошибки здесь не
#: «пропустили мусор», а «человеку не дали написать». Реальную проверку
#: делает попытка отправки ответа.
_EMAIL_RE: Final[re.Pattern[str]] = re.compile(r"^[^@\s]+@[^@\s.]+(\.[^@\s.]+)+$")


@dataclass(frozen=True)
class Rejection:
    """Причина отказа. ``code`` — для логов и тестов, ``message`` — человеку."""

    code: str
    message: str


def _clean(raw: object, limit: int) -> str:
    """Обрезать до ``limit`` и убрать управляющие символы, кроме перевода строки."""
    text = "" if raw is None else str(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = "".join(ch for ch in text if ch in {"\n", "\t"} or ch >= " ")
    return text.strip()[:limit]


def validate(
    *,
    subject: object,
    body: object,
    email: object,
    honeypot: object,
    seconds_on_form: float | None,
    logged_in: bool,
) -> tuple[dict[str, str], Rejection | None]:
    """Проверить отправку формы.

    Возвращает ``(поля, отказ_или_None)``. Поля возвращаются ВСЕГДА — уже
    очищенные и обрезанные, — чтобы при отказе форма перерисовалась с текстом
    человека, а не пустая. Терять набранное на ошибке валидации — верный
    способ, чтобы обращение вообще не дошло: второй раз человек не наберёт.

    ``seconds_on_form`` = ``None`` означает «подпись формы отсутствует или
    подделана» — обрабатывается как отдельный отказ, а не как «0 секунд»:
    причины разные, и человеку надо сказать разное.
    """
    fields = {
        "subject": _clean(subject, SUBJECT_MAX),
        "body": _clean(body, BODY_MAX),
        "email": _clean(email, EMAIL_MAX).lower(),
    }

    # 1. Ловушка. Поле спрятано от глаз и от скринридеров и не имеет
    #    autocomplete-имени — человек его не заполнит, примитивный бот
    #    заполняет всё подряд.
    if _clean(honeypot, 64):
        return fields, Rejection(
            "honeypot",
            "Форма отклонена: заполнено служебное поле, которое должно "
            "оставаться пустым. Если ты человек — отключи автозаполнение "
            "и отправь ещё раз.",
        )

    # 2. Подпись формы (время выдачи). См. repository.sign_form_ts.
    if seconds_on_form is None:
        return fields, Rejection(
            "stale_form",
            "Форма устарела или была открыта слишком давно. "
            "Обнови страницу и отправь ещё раз.",
        )
    if seconds_on_form < MIN_SECONDS_ON_FORM:
        return fields, Rejection(
            "too_fast",
            f"Слишком быстро — форма отправлена меньше чем за "
            f"{MIN_SECONDS_ON_FORM} с. Так делают боты. Подожди пару секунд "
            f"и нажми «Отправить» снова.",
        )

    # 3. Обратный адрес. Для анонима — обязателен: иначе владелец физически
    #    не сможет ответить, и обращение станет запиской в никуда.
    if not logged_in and not fields["email"]:
        return fields, Rejection(
            "email_required",
            "Укажи email — без него владельцу некуда ответить.",
        )
    if fields["email"] and not _EMAIL_RE.match(fields["email"]):
        return fields, Rejection(
            "email_invalid",
            "Проверь email: адрес выглядит некорректным.",
        )

    # 4. Длины. Верхнюю границу НЕ обрезаем молча: человек должен узнать, что
    #    его текст не влез целиком, а не обнаружить это в ответе владельца.
    if len(fields["subject"]) < SUBJECT_MIN:
        return fields, Rejection(
            "subject_short", f"Тема слишком короткая (минимум {SUBJECT_MIN} символа)."
        )
    if len(fields["body"]) < BODY_MIN:
        return fields, Rejection(
            "body_short", f"Сообщение слишком короткое (минимум {BODY_MIN} символов)."
        )
    raw_body_len = len(str(body or ""))
    if raw_body_len > BODY_MAX:
        return fields, Rejection(
            "body_long",
            f"Сообщение длиннее {BODY_MAX} символов ({raw_body_len}). "
            f"Сократи текст — или напиши коротко, а подробности приложи ссылкой.",
        )
    if len(str(subject or "")) > SUBJECT_MAX:
        return fields, Rejection(
            "subject_long", f"Тема длиннее {SUBJECT_MAX} символов."
        )

    return fields, None


# ── Огрубление контекста ────────────────────────────────────────────────────


def browser_class(user_agent: str | None) -> str:
    """Крупнозернистый класс браузера: ``"Chrome · мобильный"`` и т.п.

    Сырой User-Agent НЕ возвращается и нигде не сохраняется: он уникален с
    точностью до сборки ОС и служит готовым идентификатором для связывания
    визитов. Для разбора жалобы («у меня не грузится») хватает движка и
    того, телефон это или десктоп, — а это ~20 возможных значений на всех.
    """
    ua = (user_agent or "").lower()
    if not ua:
        return "неизвестно"
    # Порядок важен: Edge и Opera представляются Chrome'ом, Chrome — Safari.
    if "edg/" in ua or "edge" in ua:
        engine = "Edge"
    elif "opr/" in ua or "opera" in ua:
        engine = "Opera"
    elif "yabrowser" in ua:
        engine = "Yandex"
    elif "firefox" in ua:
        engine = "Firefox"
    elif "chrome" in ua or "chromium" in ua:
        engine = "Chrome"
    elif "safari" in ua:
        engine = "Safari"
    elif "bot" in ua or "spider" in ua or "curl" in ua or "python" in ua:
        engine = "бот/скрипт"
    else:
        engine = "другой"
    mobile = any(m in ua for m in ("mobile", "android", "iphone", "ipad"))
    return f"{engine} · {'мобильный' if mobile else 'десктоп'}"


def source_path(raw: str | None) -> str:
    """Путь страницы БЕЗ query string и без хвоста длиннее 120 символов.

    Query отрезается намеренно: в нём ходят одноразовые токены установки
    (``/api/install/?t=…``), поисковые запросы и id — то есть данные, которых
    в обращении в поддержку быть не должно.
    """
    text = (raw or "").strip()
    if not text.startswith("/"):
        return ""
    return text.split("?", 1)[0].split("#", 1)[0][:120]
