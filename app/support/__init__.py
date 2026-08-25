"""Поддержка: «любой может написать владельцу, и это до него дойдёт».

Пакет держит ВЕСЬ SQL темы (архитектурный гейт в
``tests/test_architecture_gates.py`` запрещает роутам импортировать
``get_connection``/``write_transaction``) плюс правила защиты формы и
доставку почты.

Три модуля:

* :mod:`app.support.repository` — таблицы ``support_ticket`` /
  ``support_message``, инстансная соль и подпись формы;
* :mod:`app.support.service`    — валидация и анти-абуз (чистые функции,
  без БД и без ``Request``);
* :mod:`app.support.notify`     — best-effort почта владельцу и автору.

Единственный ЖЁСТКИЙ инвариант всего пакета: **обращение сохраняется на
сайте, что бы ни случилось с почтой**. Почта на этом инстансе не настроена
(``smtp_enabled='true'`` при пустом ``smtp_host``), поэтому письмо — зеркало,
а не канал: любой сбой доставки записывается на обращение и НИКОГДА не
превращается в ошибку у посетителя.
"""

from __future__ import annotations

from app.support.service import (
    BODY_MAX,
    BODY_MIN,
    EMAIL_MAX,
    MIN_SECONDS_ON_FORM,
    SUBJECT_MAX,
    SUBJECT_MIN,
    Rejection,
    browser_class,
    validate,
)

__all__ = [
    "BODY_MAX",
    "BODY_MIN",
    "EMAIL_MAX",
    "MIN_SECONDS_ON_FORM",
    "SUBJECT_MAX",
    "SUBJECT_MIN",
    "Rejection",
    "browser_class",
    "validate",
]
