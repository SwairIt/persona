"""Фирменный HTML-шаблон писем Persona — тёмный космос + «чёрная дыра».

Email-клиенты не умеют JS/WebGL/внешний CSS, поэтому всё на таблицах +
инлайн-стилях, а «чёрная дыра» — multi-stop radial-gradient (рендерится в
Gmail / Яндекс.Почте / Apple Mail; в Outlook деградирует в тёмный круг через
background-color-фолбэк). Используется в auth-письмах (вход по ссылке, пароль
при регистрации, сброс пароля).
"""

from __future__ import annotations

# Палитра — как на лендинге v2 (starlit violet cosmos).
_BG = "#030014"
_CARD = "#0a0420"
_INK = "#f4f0ff"
_FOG = "#7d76a0"
_LEAD = "#c5bde0"
_RING = "rgba(147,130,255,.18)"


def _button(label: str, url: str) -> str:
    return (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:8px 0 6px;">'
        "<tr><td align=\"center\" style=\"border-radius:12px;background-color:#7c3aed;"
        "background-image:linear-gradient(135deg,#7c3aed,#c026d3);\">"
        f'<a href="{url}" target="_blank" style="display:inline-block;padding:14px 32px;'
        "font-family:Arial,Helvetica,sans-serif;font-size:15px;font-weight:700;color:#ffffff;"
        f'text-decoration:none;border-radius:12px;">{label} &rarr;</a>'
        "</td></tr></table>"
    )


def branded_email_html(
    *,
    preheader: str,
    heading: str,
    lead: str,
    footer: str,
    button_label: str | None = None,
    button_url: str | None = None,
    extra_html: str = "",
) -> str:
    """Собрать фирменное письмо. ``extra_html`` вставляется между ``lead`` и кнопкой
    (например, блок с паролем). Все переданные строки уже должны быть безопасны для HTML."""
    button = _button(button_label, button_url) if button_label and button_url else ""
    # «Чёрная дыра»: чёрное ядро → тонкое яркое кольцо аккреционного диска
    # (фиолетовый→магента→тёплый блик) → затухание в фон. background-color —
    # фолбэк для Outlook (тёмный круг вместо градиента).
    hole = (
        '<table role="presentation" cellpadding="0" cellspacing="0" style="margin:30px auto 8px;">'
        '<tr><td style="width:124px;height:124px;border-radius:50%;background-color:#0a0420;'
        "background-image:radial-gradient(circle at 50% 50%,#000000 23%,#1c0a3e 32%,"
        "#7c3aed 43%,#e59cff 49%,#ffb86b 53%,#3a1566 60%,#0a0420 74%);"
        'box-shadow:0 0 60px 10px rgba(124,58,237,.45),inset 0 0 26px rgba(0,0,0,.9);">&nbsp;</td></tr></table>'
    )
    return (
        '<!doctype html><html lang="ru"><head>'
        '<meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1">'
        '<meta name="color-scheme" content="dark">'
        '<meta name="supported-color-schemes" content="dark light">'
        f"<title>{heading}</title></head>"
        f'<body style="margin:0;padding:0;background:{_BG};">'
        # preheader (превью в списке писем, скрыт в теле)
        f'<div style="display:none;max-height:0;overflow:hidden;opacity:0;color:{_BG};">{preheader}</div>'
        '<table role="presentation" width="100%" cellpadding="0" cellspacing="0" '
        f'style="background:{_BG};background-image:radial-gradient(120% 80% at 50% -10%,#1a0b3a 0%,#07021a 46%,{_BG} 100%);padding:34px 12px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;background:{_CARD};border:1px solid {_RING};border-radius:20px;overflow:hidden;">'
        # hero
        '<tr><td align="center" style="padding:6px 0 0;">'
        f"{hole}"
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:13px;font-weight:700;'
        'letter-spacing:4px;color:#b9a8ff;text-transform:uppercase;">PERSONA</div>'
        "</td></tr>"
        # content
        '<tr><td style="padding:22px 38px 6px;">'
        f'<h1 style="margin:0 0 12px;font-family:Arial,Helvetica,sans-serif;font-size:24px;'
        f'font-weight:700;color:{_INK};line-height:1.25;">{heading}</h1>'
        f'<p style="margin:0 0 18px;font-family:Arial,Helvetica,sans-serif;font-size:15px;'
        f'line-height:1.6;color:{_LEAD};">{lead}</p>'
        f"{extra_html}{button}"
        "</td></tr>"
        # footer
        f'<tr><td style="padding:18px 38px 30px;border-top:1px solid rgba(147,130,255,.12);">'
        f'<p style="margin:0;font-family:Arial,Helvetica,sans-serif;font-size:12px;line-height:1.5;color:{_FOG};">{footer}</p>'
        "</td></tr></table>"
        '<div style="font-family:Arial,Helvetica,sans-serif;font-size:11px;line-height:1.5;'
        'color:#5a5478;padding:16px;">persona.getdoday.ru · твоя память как сингулярность</div>'
        "</td></tr></table></body></html>"
    )
