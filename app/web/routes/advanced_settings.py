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
from app.storage.repository import get_kv, get_user_kv, set_kv, set_user_kv

# Fail-closed резолв роли: сбой резолва = «участник» (app/web/routes/owner_view.py).
from app.web.routes.owner_view import viewer_is_owner as is_owner
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


# режимы поиска памяти по всем чатам (показываемые на этой странице)
_RECALL_MODES: tuple[tuple[str, str, str], ...] = (
    ("off", "Выключена", "ИИ помнит только текущий чат."),
    ("keyword", "По ключевым словам", "Быстро ищет прошлые сообщения по именам и словам из вопроса. (по умолчанию)"),
    ("smart", "Умная (ИИ сам ищет)", "ИИ сам решает, что искать — имена, темы, синонимы, падежи. Точнее, но чуть медленнее (доп. запрос к модели)."),
)

# Согласованный набор валидных значений recall_mode по ВСЕМ страницам настроек.
# advanced_settings показывает off/keyword/smart, а memory_settings — keyword/hybrid/
# vector/generative; раньше валидаторы расходились и значение, сохранённое на одной
# странице, считалось «невалидным» на другой и тихо сбрасывалось. Объединяем оба
# набора, чтобы любое легитимно сохранённое значение оставалось валидным везде.
_VALID_RECALL_MODES: frozenset[str] = frozenset(
    {m[0] for m in _RECALL_MODES} | {"hybrid", "vector", "generative"}
)


# ── Владелец пишет глобальный kv, участник — свой user_settings ──────────────
#
# Раньше ЛЮБОЙ зарегистрированный пользователь этой страницей переписывал
# ГЛОБАЛЬНЫЕ ``advanced_mode`` / ``feat_*`` / ``recall_mode``: чужой аккаунт
# выключал владельцу инструменты и режимы во всём инстансе. Теперь личность
# решает адрес записи, а имена ключей и дефолты (всё ВКЛ) — те же.


async def _is_member(user: SessionRecord) -> bool:
    """True — участник (пишем в ``user_settings``). Сбой резолва → участник."""
    try:
        return not await is_owner(user["user_id"])
    except Exception:  # noqa: BLE001 — сбой гейта → не трогаем настройки владельца
        return True


async def _read_raw(user: SessionRecord) -> dict[str, object]:
    member = await _is_member(user)
    uid = int(user["user_id"])
    async with get_connection() as conn:

        async def _get(key: str, default: str) -> str:
            if member:
                return (await get_user_kv(conn, uid, key)) or default
            return (await get_kv(conn, key)) or default

        out: dict[str, object] = {
            "master": (await _get("advanced_mode", "1")).strip() == "1"
        }
        for key, _t, _d in _FEATURES:
            out[key] = (await _get(f"feat_{key}", "1")).strip() == "1"
        rm = (await _get("recall_mode", "keyword")).strip()
        out["recall_mode"] = rm if rm in _VALID_RECALL_MODES else "keyword"
    return out


@router.get("/settings/advanced", response_class=HTMLResponse)
async def advanced_page(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> HTMLResponse:
    flags = await _read_raw(user)
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
            # Участнику блок recall не рисуем: его recall всегда keyword
            # (см. _get_recall_mode в chat_sessions.py) — hybrid/vector/smart
            # считали бы на Ollama и LLM ВЛАДЕЛЬЦА.
            "is_owner": not await _is_member(user),
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
        member = await _is_member(user)
        uid = int(user["user_id"])
        async with get_connection() as conn:

            async def _put(key: str, value: str) -> None:
                if member:
                    await set_user_kv(conn, uid, key, value)
                else:
                    await set_kv(conn, key, value)

            await _put("advanced_mode", "1" if master else "0")
            for key, _t, _d in _FEATURES:
                await _put(f"feat_{key}", "1" if feats.get(key, master) else "0")
    return RedirectResponse(url="/settings/advanced", status_code=303)


@router.post("/settings/advanced")
async def advanced_save(
    request: Request,
    user: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    form = await request.form()
    valid_modes = _VALID_RECALL_MODES
    rm = str(form.get("recall_mode", "keyword"))
    member = await _is_member(user)
    uid = int(user["user_id"])
    async with get_connection() as conn:

        async def _put(key: str, value: str) -> None:
            if member:
                await set_user_kv(conn, uid, key, value)
            else:
                await set_kv(conn, key, value)

        await _put("advanced_mode", "1" if form.get("master") else "0")
        for key, _t, _d in _FEATURES:
            await _put(f"feat_{key}", "1" if form.get(key) else "0")
        # recall_mode участнику НЕ пишем вовсе: контрола в его форме нет, а
        # подделанный POST иначе завёл бы ему vector/smart, которые всё равно
        # игнорируются (_get_recall_mode форсит keyword не-владельцу).
        if not member:
            await _put("recall_mode", rm if rm in valid_modes else "keyword")
    return RedirectResponse(url="/settings/advanced", status_code=303)


# Быстрый тумблер мастера одним кликом (для кнопки «выключить всё»).
@router.post("/api/advanced/master")
async def advanced_master_toggle(
    user: Annotated[SessionRecord, Depends(current_user_required)],
    on: Annotated[bool, Form()] = False,
) -> RedirectResponse:
    member = await _is_member(user)
    async with get_connection() as conn:
        if member:
            await set_user_kv(
                conn, int(user["user_id"]), "advanced_mode", "1" if on else "0"
            )
        else:
            await set_kv(conn, "advanced_mode", "1" if on else "0")
    return RedirectResponse(url="/settings/advanced", status_code=303)


__all__ = ["router"]
