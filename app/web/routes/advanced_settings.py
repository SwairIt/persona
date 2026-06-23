"""Расширенные функции чата — один мастер-выключатель + по-фичам.

Когда мастер выключен, Persona в чате работает как простой ассистент-друг:
без планов/режимов, без инструментов и кода, без эффорта и авто-промптов.
Реальное применение флагов — в app/web/routes/chat_sessions.py
(get_advanced_flags + сборка системного промпта в send-stream).
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse

from app.auth import current_user_required
from app.auth.sessions import SessionRecord
from app.storage.db import get_connection
from app.storage.repository import get_kv, set_kv
from app.web.templates_engine import templates

router = APIRouter(tags=["settings"])

# ключ kv → (заголовок, описание)
_FEATURES: tuple[tuple[str, str, str], ...] = (
    ("auto_prompt", "Авто-смена системного промпта",
     "ИИ сам подстраивает свою роль под тип задачи (код, перевод, анализ…)."),
    ("effort", "Выбор эффорта",
     "Переключатель мощности ответа: Быстро / Норма / Глубоко."),
    ("modes", "Режимы работы (План / Спрашивать / Авто / Без спроса)",
     "«Режим кода»: планирование, подтверждения перед действиями, автономность."),
    ("tools", "Инструменты и код",
     "Выполнение инструментов, создание файлов, сборка проектов (📦), команды."),
    ("choices", "Меню выбора ответов",
     "ИИ предлагает кнопки-варианты вместо длинных простыней."),
)


# Профили = пресеты (мастер + фич-флаги). Имя → (label, desc, master, {feat:bool}).
# Ключи, не указанные в dict, наследуют значение master.
_PROFILES: tuple[tuple[str, str, str, bool, dict[str, bool]], ...] = (
    ("assistant", "🧑‍🤝‍🧑 ИИ-ассистент",
     "Простой быстрый собеседник: без кода, планов, инструментов и лишних кнопок. Всегда быстро.",
     False, {}),
    ("simple", "🌱 Простой (для начала)",
     "Чуть-чуть умных функций для новичка: только меню выбора. Без кода, режимов и эффорта.",
     True, {"auto_prompt": False, "effort": False, "modes": False, "tools": False, "choices": True}),
    ("balanced", "⚖️ Сбалансированный",
     "Умный помощник: авто-промпт, выбор мощности и меню выбора. Без выполнения кода/инструментов.",
     True, {"auto_prompt": True, "effort": True, "modes": False, "tools": False, "choices": True}),
    ("full", "🛠 Полный (разработчик)",
     "Все возможности: режимы плана/авто, инструменты и код, эффорт, авто-промпт, меню.",
     True, {"auto_prompt": True, "effort": True, "modes": True, "tools": True, "choices": True}),
)


def _detect_profile(flags: dict[str, object]) -> str:
    """Какой профиль соответствует текущим флагам (или '' если кастом)."""
    for name, _l, _d, master, feats in _PROFILES:
        if bool(flags.get("master")) != master:
            continue
        if not master:
            return name  # ассистент: фичи не важны, мастер выключен
        if all(bool(flags.get(k)) == feats.get(k, master) for k, _t, _dd in _FEATURES):
            return name
    return ""


# режимы поиска памяти по всем чатам
_RECALL_MODES: tuple[tuple[str, str, str], ...] = (
    ("off", "Выключена", "ИИ помнит только текущий чат."),
    ("keyword", "По ключевым словам", "Быстро ищет прошлые сообщения по именам и словам из вопроса. (по умолчанию)"),
    ("smart", "Умная (ИИ сам ищет)", "ИИ сам решает, что искать — имена, темы, синонимы, падежи. Точнее, но чуть медленнее (доп. запрос к модели)."),
)


async def _read_raw() -> dict[str, object]:
    async with get_connection() as conn:
        out: dict[str, object] = {"master": (await get_kv(conn, "advanced_mode") or "1").strip() == "1"}
        for key, _t, _d in _FEATURES:
            out[key] = (await get_kv(conn, f"feat_{key}") or "1").strip() == "1"
        rm = (await get_kv(conn, "recall_mode") or "keyword").strip()
        out["recall_mode"] = rm if rm in {m[0] for m in _RECALL_MODES} else "keyword"
    return out


@router.get("/settings/advanced", response_class=HTMLResponse)
async def advanced_page(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    flags = await _read_raw()
    return templates.TemplateResponse(
        request,
        "advanced_settings.html",
        {
            "title": "Расширенные функции",
            "active_nav": "settings",
            "flags": flags,
            "features": _FEATURES,
            "recall_modes": _RECALL_MODES,
            "profiles": _PROFILES,
            "active_profile": _detect_profile(flags),
        },
    )


@router.post("/settings/advanced/profile")
async def advanced_apply_profile(
    user: Annotated[SessionRecord, Depends(current_user_required)],
    profile: Annotated[str, Form()] = "",
) -> RedirectResponse:
    prof = next((p for p in _PROFILES if p[0] == profile), None)
    if prof is not None:
        _name, _label, _desc, master, feats = prof
        async with get_connection() as conn:
            await set_kv(conn, "advanced_mode", "1" if master else "0")
            for key, _t, _d in _FEATURES:
                await set_kv(conn, f"feat_{key}", "1" if feats.get(key, master) else "0")
    return RedirectResponse(url="/settings/advanced", status_code=303)


@router.post("/settings/advanced")
async def advanced_save(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    form = await request.form()
    valid_modes = {m[0] for m in _RECALL_MODES}
    rm = str(form.get("recall_mode", "keyword"))
    async with get_connection() as conn:
        await set_kv(conn, "advanced_mode", "1" if form.get("master") else "0")
        for key, _t, _d in _FEATURES:
            await set_kv(conn, f"feat_{key}", "1" if form.get(key) else "0")
        await set_kv(conn, "recall_mode", rm if rm in valid_modes else "keyword")
    return RedirectResponse(url="/settings/advanced", status_code=303)


# Быстрый тумблер мастера одним кликом (для кнопки «выключить всё»).
@router.post("/api/advanced/master")
async def advanced_master_toggle(
    user: Annotated[SessionRecord, Depends(current_user_required)],
    on: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    async with get_connection() as conn:
        await set_kv(conn, "advanced_mode", "1" if on else "0")
    return RedirectResponse(url="/settings/advanced", status_code=303)


__all__ = ["router"]
