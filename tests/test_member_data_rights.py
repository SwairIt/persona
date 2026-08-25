"""Права участника на СВОИ данные (152-ФЗ): выгрузка, удаление, согласие.

Что тут закрывается
-------------------
1. **Право на доступ.** ``/settings/my-data/export.json`` отдаёт данные
   участника и НИ ОДНОЙ строки владельца или второго участника. Проверка —
   канареечная (как в ``tests/test_member_data_isolation_audit.py``): каждая
   посеянная строка уникальна, поэтому её появление в теле — настоящая утечка.
2. **Редактирование секретов.** Свой ключ API и токен бота в выгрузку не
   попадают: их значение заменено маркером, факт наличия — остаётся.
3. **Право на удаление.** После удаления в КАЖДОЙ таблице по этому ``user_id``
   ноль строк, «хвостатые» ключи ``kv_settings`` тоже стёрты, сессии отозваны,
   e-mail снова свободен для регистрации.
4. **Границы.** Владельца этим роутом не удалить; чужой аккаунт не удалить
   никаким способом передачи id; ``/settings/privacy/snapshot`` (VACUUM всей
   базы) участнику недоступен ни через гейт, ни через явный owner-guard.
5. **Согласие.** При регистрации пишется строка ``user_consent`` с текущей
   версией политики и честным ``source``.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient

from app import i18n
from app.auth import owner as owner_mod
from app.auth.account_delete import can_delete, delete_own_account
from app.auth.consent import POLICY_VERSION
from app.auth.data_export import REDACTED, build_export
from app.auth.sessions import SESSION_COOKIE_NAME, issue_session, verify_session
from app.auth.users import create_user
from app.storage.db import get_connection, init_database
from app.storage.repository import set_kv, set_user_kv
from app.web import rate_limit, templates_engine
from app.web.main import create_app
from app.web.middleware import auth_gate
from app.web.routes import setup_gate

# ── Канарейки ───────────────────────────────────────────────────────────────

O_CHAT = "KANAREYKA-RIGHTS-OWNER-CHAT-01"
O_MEMORY = "KANAREYKA-RIGHTS-OWNER-MEM-02"
O_SKILL = "KANAREYKA-RIGHTS-OWNER-SKILL-03"
O_KEY = "sk-kanareyka-rights-owner-04"
O_EMAIL = "rights-owner-05@rights.test"

B_CHAT = "KANAREYKA-RIGHTS-BEE-CHAT-06"
B_MEMORY = "KANAREYKA-RIGHTS-BEE-MEM-07"
B_SKILL = "KANAREYKA-RIGHTS-BEE-SKILL-08"
B_KEY = "sk-kanareyka-rights-bee-09"
B_EMAIL = "rights-bee-10@rights.test"

#: ЧУЖИЕ строки — ни одна не имеет права появиться в выгрузке участника A.
FOREIGN_CANARIES: tuple[str, ...] = (
    O_CHAT, O_MEMORY, O_SKILL, O_KEY, O_EMAIL,
    B_CHAT, B_MEMORY, B_SKILL, B_KEY, B_EMAIL,
)

A_EMAIL = "rights-alpha-11@rights.test"
A_CHAT = "KANAREYKA-RIGHTS-A-CHAT-12"
A_MEMORY = "KANAREYKA-RIGHTS-A-MEM-13"
A_SKILL = "KANAREYKA-RIGHTS-A-SKILL-14"
A_REFLECTION = "KANAREYKA-RIGHTS-A-REFLECT-15"
A_ENTITY = "KANAREYKA-RIGHTS-A-ENTITY-16"
A_NOTIF = "KANAREYKA-RIGHTS-A-NOTIF-17"
A_PROFILE = "KANAREYKA-RIGHTS-A-PROFILE-18"
A_KEY = "sk-kanareyka-rights-alpha-19"
A_DM_SENT = "KANAREYKA-RIGHTS-A-DMSENT-20"
A_TRAINING = "KANAREYKA-RIGHTS-A-TRAIN-21"
#: Сообщение, которое B прислал A. Это переписка A — она в выгрузке БЫТЬ должна.
B_DM_TO_A = "KANAREYKA-RIGHTS-BEE-DM-TO-A-22"

#: СВОИ строки — каждая обязана быть в выгрузке.
OWN_CANARIES: tuple[str, ...] = (
    A_CHAT, A_MEMORY, A_SKILL, A_REFLECTION, A_ENTITY, A_NOTIF, A_PROFILE,
    A_DM_SENT, B_DM_TO_A,
)


def _reset_caches() -> None:
    owner_mod._cache["value"] = None
    owner_mod._cache["checked_at"] = 0.0
    owner_mod._fa_cache["value"] = None
    owner_mod._fa_cache["checked_at"] = 0.0
    auth_gate._cache["value"] = False
    auth_gate._cache["checked_at"] = 0.0
    auth_gate._role_gate_cache["value"] = False
    auth_gate._role_gate_cache["checked_at"] = 0.0
    auth_gate._owner_exclusive_cache["value"] = False
    auth_gate._owner_exclusive_cache["checked_at"] = 0.0
    templates_engine._kv_value_cache.clear()
    templates_engine._user_kv_value_cache.clear()
    templates_engine.invalidate_theme_cache()
    i18n.invalidate_language_cache()
    rate_limit._EVENTS.clear()


async def _seed(owner_id: int, a_id: int, b_id: int) -> dict[str, int]:
    """Владелец, участник A и участник B — у каждого свой набор данных."""
    ids: dict[str, int] = {}
    async with get_connection() as conn:
        await set_kv(conn, "owner_user_id", str(owner_id))
        await set_kv(conn, "owner_exclusive_mode", "0")
        await set_kv(conn, "setup_complete", "true")

        # --- владелец ---
        cur = await conn.execute(
            "INSERT INTO chat_session (user_id, title, created_at, updated_at, "
            " summary_up_to_id, auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (owner_id, O_CHAT),
        )
        osid = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO chat_message (session_id, role, content, created_at, "
            " is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (osid, O_CHAT),
        )
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (owner_id, O_MEMORY),
        )
        await conn.execute(
            "INSERT INTO skill (user_id, name, content, enabled) VALUES (?, ?, ?, 1)",
            (owner_id, O_SKILL, O_SKILL),
        )
        await set_user_kv(conn, owner_id, "byo_api_key_openai", O_KEY)

        # --- участник B ---
        cur = await conn.execute(
            "INSERT INTO chat_session (user_id, title, created_at, updated_at, "
            " summary_up_to_id, auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (b_id, B_CHAT),
        )
        bsid = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO chat_message (session_id, role, content, created_at, "
            " is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (bsid, B_CHAT),
        )
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (b_id, B_MEMORY),
        )
        await conn.execute(
            "INSERT INTO skill (user_id, name, content, enabled) VALUES (?, ?, ?, 1)",
            (b_id, B_SKILL, B_SKILL),
        )
        await set_user_kv(conn, b_id, "byo_api_key_openai", B_KEY)

        # --- участник A: полный набор, включая «ловушечные» таблицы ---
        cur = await conn.execute(
            "INSERT INTO chat_session (user_id, title, created_at, updated_at, "
            " summary_up_to_id, auto_switch_on_image) "
            "VALUES (?, ?, datetime('now'), datetime('now'), 0, 0)",
            (a_id, A_CHAT),
        )
        asid = int(cur.lastrowid or 0)
        ids["a_session"] = asid
        cur = await conn.execute(
            "INSERT INTO chat_message (session_id, role, content, created_at, "
            " is_streaming, is_pinned, access_count) "
            "VALUES (?, 'user', ?, datetime('now'), 0, 0, 0)",
            (asid, A_CHAT),
        )
        amid = int(cur.lastrowid or 0)
        ids["a_message"] = amid
        await conn.execute(
            "INSERT INTO user_memory (user_id, kind, text) VALUES (?, 'fact', ?)",
            (a_id, A_MEMORY),
        )
        await conn.execute(
            "INSERT INTO skill (user_id, name, content, enabled) VALUES (?, ?, ?, 1)",
            (a_id, A_SKILL, A_SKILL),
        )
        await conn.execute(
            "INSERT INTO reflection (user_id, kind, text) VALUES (?, 'insight', ?)",
            (a_id, A_REFLECTION),
        )
        cur = await conn.execute(
            "INSERT INTO kg_entity (user_id, name, kind) VALUES (?, ?, 'person')",
            (a_id, A_ENTITY),
        )
        ent_a = int(cur.lastrowid or 0)
        cur = await conn.execute(
            "INSERT INTO kg_entity (user_id, name, kind) VALUES (?, ?, 'topic')",
            (a_id, A_ENTITY + "-B"),
        )
        ent_b = int(cur.lastrowid or 0)
        await conn.execute(
            "INSERT INTO kg_edge (user_id, from_entity_id, to_entity_id, relation_type) "
            "VALUES (?, ?, ?, 'знает')",
            (a_id, ent_a, ent_b),
        )
        await conn.execute(
            "INSERT INTO social_notif_item (user_id, event, title, body) "
            "VALUES (?, 'dm_message', ?, ?)",
            (a_id, A_NOTIF, A_NOTIF),
        )
        await conn.execute(
            "INSERT INTO social_notif_pref (user_id, event, channel, enabled) "
            "VALUES (?, 'dm_message', 'browser', 1)",
            (a_id,),
        )
        await conn.execute(
            "INSERT INTO llm_usage (kind, provider, input_tokens, output_tokens, "
            " success, user_id) VALUES ('chat', 'openai', 10, 20, 1, ?)",
            (a_id,),
        )
        await conn.execute(
            "INSERT INTO voice_tts (user_id, session_id, text, voice, status) "
            "VALUES (?, ?, 'привет', 'alloy', 'done')",
            (a_id, asid),
        )
        await conn.execute(
            "INSERT INTO chat_reaction (message_id, user_id, reaction) "
            "VALUES (?, ?, '👍')",
            (amid, a_id),
        )
        # Ловушка D: training_dataset держит ПОЛНЫЙ текст и на удалении чата
        # получает SET NULL, а не DELETE.
        await conn.execute(
            "INSERT INTO training_dataset (session_id, user_message_id, user_text, "
            " assistant_text) VALUES (?, ?, ?, ?)",
            (asid, amid, A_TRAINING, A_TRAINING),
        )
        await set_user_kv(conn, a_id, "byo_api_key_openai", A_KEY)
        await set_user_kv(conn, a_id, "social_telegram_token", "999:" + A_KEY)
        await set_user_kv(conn, a_id, "llm_provider", "openai")
        # Ловушка E: «хвостатые» ключи ГЛОБАЛЬНОГО kv.
        await set_kv(conn, f"user_profile_{a_id}", A_PROFILE)
        await set_kv(conn, f"onboarded_{a_id}", "1")
        await set_kv(conn, f"email_verified_{a_id}", "1")
        await set_kv(conn, f"chat_mode_{asid}", "plan")
        await set_kv(conn, f"chat_effort_{asid}", "high")
        await set_kv(conn, f"chat_stop_{asid}", "0")

        # --- социальный слой A ↔ B (дружба + переписка в обе стороны) ---
        for x, y in ((a_id, b_id), (b_id, a_id)):
            await conn.execute(
                "INSERT OR IGNORE INTO friendship (user_id, friend_id) VALUES (?, ?)",
                (x, y),
            )
        lo, hi = min(a_id, b_id), max(a_id, b_id)
        cur = await conn.execute(
            "INSERT INTO dm_thread (user_a_id, user_b_id, created_at) "
            "VALUES (?, ?, datetime('now'))",
            (lo, hi),
        )
        tid = int(cur.lastrowid or 0)
        ids["thread"] = tid
        await conn.execute(
            "INSERT INTO dm_message (thread_id, sender_id, body, kind) "
            "VALUES (?, ?, ?, 'human')",
            (tid, a_id, A_DM_SENT),
        )
        await conn.execute(
            "INSERT INTO dm_message (thread_id, sender_id, body, kind) "
            "VALUES (?, ?, ?, 'human')",
            (tid, b_id, B_DM_TO_A),
        )
        await conn.execute(
            "INSERT INTO dm_ai_pref (user_id, peer_id, mode) VALUES (?, ?, 'off')",
            (a_id, b_id),
        )
        await conn.execute(
            "INSERT INTO llm_grant (grantor_id, grantee_id, daily_limit) "
            "VALUES (?, ?, 5)",
            (a_id, b_id),
        )
        await conn.commit()
    return ids


@pytest_asyncio.fixture
async def env() -> Any:
    await init_database()
    owner_user = await create_user(O_EMAIL, "Zx7-Alpha-Passphrase")
    member_a = await create_user(A_EMAIL, "Qw4-Bravo-Passphrase")
    member_b = await create_user(B_EMAIL, "Rt9-Delta-Passphrase")
    ids = await _seed(owner_user["id"], member_a["id"], member_b["id"])
    setup_gate._cache.mark_done()
    _reset_caches()

    transport = ASGITransport(app=create_app())
    try:
        async with AsyncClient(transport=transport, base_url="http://test") as client:
            yield {
                "client": client,
                "owner": owner_user,
                "a": member_a,
                "b": member_b,
                "ids": ids,
            }
    finally:
        _reset_caches()


async def _as(client: AsyncClient, uid: int) -> str:
    client.cookies.clear()
    token, _ = await issue_session(uid)
    client.cookies.set(SESSION_COOKIE_NAME, token)
    return token


def _leaked(body: str, canaries: tuple[str, ...]) -> list[str]:
    found = []
    for canary in canaries:
        escaped = json.dumps(canary, ensure_ascii=True)[1:-1]
        if canary in body or escaped in body:
            found.append(canary)
    return found


# ── 1. Экран «Мои данные» доступен участнику ────────────────────────────────


@pytest.mark.asyncio
async def test_member_reaches_my_data_page(env: Any) -> None:
    client = env["client"]
    await _as(client, env["a"]["id"])
    response = await client.get("/settings/my-data", follow_redirects=False)
    assert response.status_code == 200, response.text[:300]
    assert A_EMAIL in response.text


@pytest.mark.asyncio
async def test_my_data_page_is_listed_in_the_member_hub(env: Any) -> None:
    client = env["client"]
    await _as(client, env["a"]["id"])
    body = (await client.get("/settings/hub")).text
    assert "/settings/my-data" in body


# ── 2. Экспорт: свои данные есть, чужих нет ─────────────────────────────────


@pytest.mark.asyncio
async def test_export_contains_own_data(env: Any) -> None:
    client = env["client"]
    await _as(client, env["a"]["id"])
    response = await client.get("/settings/my-data/export.json")
    assert response.status_code == 200
    body = response.text
    missing = [c for c in OWN_CANARIES if c not in body]
    assert not missing, f"своих данных нет в выгрузке: {missing}"


@pytest.mark.asyncio
async def test_export_leaks_nothing_foreign(env: Any) -> None:
    """Канареечная проверка: ни владельца, ни второго участника."""
    client = env["client"]
    await _as(client, env["a"]["id"])
    body = (await client.get("/settings/my-data/export.json")).text
    assert not _leaked(body, FOREIGN_CANARIES), _leaked(body, FOREIGN_CANARIES)


@pytest.mark.asyncio
async def test_export_masks_other_peoples_emails(env: Any) -> None:
    """Адрес друга/собеседника — данные ДРУГОГО человека, отдаём маской."""
    client = env["client"]
    await _as(client, env["a"]["id"])
    body = (await client.get("/settings/my-data/export.json")).text
    assert B_EMAIL not in body
    assert "r***@rights.test" in body


@pytest.mark.asyncio
async def test_export_redacts_secrets_but_admits_they_exist(env: Any) -> None:
    """Свой ключ API и токен бота обратно не отдаём — только факт наличия."""
    client = env["client"]
    await _as(client, env["a"]["id"])
    body = (await client.get("/settings/my-data/export.json")).text
    assert A_KEY not in body, "действующий ключ API уехал в выгрузку"
    assert REDACTED in body
    payload = json.loads(body)
    keys = {row["key"]: row for row in payload["settings"]}
    assert keys["byo_api_key_openai"]["redacted"] is True
    assert keys["byo_api_key_openai"]["present"] is True
    assert keys["social_telegram_token"]["redacted"] is True
    # Несекретная настройка выгружается как есть.
    assert keys["llm_provider"]["value"] == "openai"


@pytest.mark.asyncio
async def test_export_never_contains_password_hash_or_session_token(env: Any) -> None:
    client = env["client"]
    token = await _as(client, env["a"]["id"])
    body = (await client.get("/settings/my-data/export.json")).text
    assert "password_hash" not in body
    assert token not in body


@pytest.mark.asyncio
async def test_export_zip_is_a_real_archive(env: Any) -> None:
    import io
    import zipfile

    client = env["client"]
    await _as(client, env["a"]["id"])
    response = await client.get("/settings/my-data/export.zip")
    assert response.status_code == 200
    with zipfile.ZipFile(io.BytesIO(response.content)) as zf:
        names = zf.namelist()
        assert any(n.endswith(".json") for n in names), names
        inner = zf.read([n for n in names if n.endswith(".json")][0]).decode("utf-8")
    assert A_CHAT in inner
    assert not _leaked(inner, FOREIGN_CANARIES)


@pytest.mark.asyncio
async def test_export_is_per_user_not_shared(env: Any) -> None:
    """B видит своё, A — своё. Один и тот же роут, разные тела."""
    client = env["client"]
    await _as(client, env["b"]["id"])
    body = (await client.get("/settings/my-data/export.json")).text
    assert B_CHAT in body
    assert A_CHAT not in body
    assert O_CHAT not in body


# ── 3. Инстанс-глобальный артефакт участнику недоступен ─────────────────────


@pytest.mark.asyncio
async def test_member_cannot_reach_database_snapshot(env: Any) -> None:
    """VACUUM всей базы — только владельцу. Гейт + явный owner-guard."""
    client = env["client"]
    await _as(client, env["a"]["id"])
    for path in (
        "/settings/privacy",
        "/settings/privacy/snapshot",
        "/settings/privacy/export-memory",
    ):
        response = await client.get(path, follow_redirects=False)
        assert response.status_code != 200, f"{path} открылся участнику"
        assert response.headers.get("content-type", "") != "application/octet-stream"


@pytest.mark.asyncio
async def test_snapshot_stays_owner_only_even_if_the_gate_is_opened(env: Any) -> None:
    """Защита в глубину: даже с member-префиксом снимок отдаёт 403.

    Гейт — не единственный рубеж. Симулируем «префикс открыли по недосмотру»
    и убеждаемся, что явная owner-зависимость в обработчике держит.
    """
    client = env["client"]
    await _as(client, env["a"]["id"])
    original = auth_gate._MEMBER_PREFIXES
    auth_gate._MEMBER_PREFIXES = (*original, "/settings/privacy")
    try:
        response = await client.get("/settings/privacy/snapshot", follow_redirects=False)
    finally:
        auth_gate._MEMBER_PREFIXES = original
    assert response.status_code == 403, response.status_code


@pytest.mark.asyncio
async def test_owner_still_gets_his_privacy_page_and_snapshot(env: Any) -> None:
    """Контроль: «утечку» нельзя закрыть, сломав фичу владельцу."""
    client = env["client"]
    await _as(client, env["owner"]["id"])
    assert (
        await client.get("/settings/privacy", follow_redirects=False)
    ).status_code == 200
    snapshot = await client.get("/settings/privacy/snapshot", follow_redirects=False)
    assert snapshot.status_code == 200
    assert snapshot.content[:15].startswith(b"SQLite format 3")


# ── 4. Удаление аккаунта ────────────────────────────────────────────────────


async def _count(sql: str, params: tuple[Any, ...]) -> int:
    async with get_connection() as conn:
        try:
            cur = await conn.execute(sql, params)
        except Exception:  # noqa: BLE001 — отсутствующая таблица = ноль строк
            return 0
        row = await cur.fetchone()
    return int(row["n"]) if row else 0


#: Таблица → колонка с id пользователя. После удаления в каждой ноль строк.
_TABLES: tuple[tuple[str, str], ...] = (
    ("users", "id"),
    ("user_settings", "user_id"),
    ("auth_session", "user_id"),
    ("chat_session", "user_id"),
    ("user_memory", "user_id"),
    ("reflection", "user_id"),
    ("skill", "user_id"),
    ("kg_entity", "user_id"),
    ("kg_edge", "user_id"),
    ("social_notif_item", "user_id"),
    ("social_notif_pref", "user_id"),
    ("llm_usage", "user_id"),
    ("voice_tts", "user_id"),
    ("chat_reaction", "user_id"),
    ("user_consent", "user_id"),
)


@pytest.mark.asyncio
async def test_delete_removes_every_table_and_the_tailed_kv_keys(env: Any) -> None:
    client = env["client"]
    a_id = int(env["a"]["id"])
    sid = env["ids"]["a_session"]
    await _as(client, a_id)

    response = await client.post(
        "/settings/my-data/delete",
        data={"confirm": A_EMAIL},
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]

    for table, column in _TABLES:
        n = await _count(
            f"SELECT COUNT(*) AS n FROM {table} WHERE {column} = ?",  # noqa: S608
            (a_id,),
        )
        assert n == 0, f"{table}.{column}: осталось {n} строк"

    # Дочерние таблицы по цепочке.
    assert await _count(
        "SELECT COUNT(*) AS n FROM chat_message WHERE session_id = ?", (sid,)
    ) == 0
    assert await _count(
        "SELECT COUNT(*) AS n FROM training_dataset WHERE user_text = ?", (A_TRAINING,)
    ) == 0, "ловушка SET NULL: обучающая пара с текстом переписки пережила удаление"
    assert await _count(
        "SELECT COUNT(*) AS n FROM friendship WHERE user_id = ? OR friend_id = ?",
        (a_id, a_id),
    ) == 0
    assert await _count(
        "SELECT COUNT(*) AS n FROM llm_grant WHERE grantor_id = ? OR grantee_id = ?",
        (a_id, a_id),
    ) == 0
    assert await _count(
        "SELECT COUNT(*) AS n FROM dm_ai_pref WHERE user_id = ? OR peer_id = ?",
        (a_id, a_id),
    ) == 0

    # Ловушка E: «хвостатые» ключи глобального kv.
    for key in (
        f"user_profile_{a_id}",
        f"onboarded_{a_id}",
        f"email_verified_{a_id}",
        f"chat_mode_{sid}",
        f"chat_effort_{sid}",
        f"chat_stop_{sid}",
    ):
        assert await _count(
            "SELECT COUNT(*) AS n FROM kv_settings WHERE key = ?", (key,)
        ) == 0, f"kv-ключ {key} пережил удаление"


@pytest.mark.asyncio
async def test_delete_hard_removes_the_dm_thread_for_both_sides(env: Any) -> None:
    """Выбранная политика: переписка исчезает у ОБЕИХ сторон (см. docstring модуля)."""
    client = env["client"]
    a_id = int(env["a"]["id"])
    tid = env["ids"]["thread"]
    await _as(client, a_id)
    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )

    assert await _count(
        "SELECT COUNT(*) AS n FROM dm_thread WHERE id = ?", (tid,)
    ) == 0
    assert await _count(
        "SELECT COUNT(*) AS n FROM dm_message WHERE thread_id = ?", (tid,)
    ) == 0
    # Именно ОБЕ стороны: сообщение B тоже стёрто.
    assert await _count(
        "SELECT COUNT(*) AS n FROM dm_message WHERE body = ?", (B_DM_TO_A,)
    ) == 0


@pytest.mark.asyncio
async def test_delete_revokes_sessions(env: Any) -> None:
    client = env["client"]
    token = await _as(client, int(env["a"]["id"]))
    assert await verify_session(token) is not None

    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    assert await verify_session(token) is None
    # И тем же cookie в кабинет уже не войти.
    bounced = await client.get("/chat", follow_redirects=False)
    assert bounced.status_code in (302, 303, 307)


@pytest.mark.asyncio
async def test_deleted_email_can_register_again(env: Any) -> None:
    client = env["client"]
    await _as(client, int(env["a"]["id"]))
    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    reborn = await create_user(A_EMAIL, "Nw2-Echo-Passphrase")
    assert reborn["id"] > 0
    # Новый аккаунт ПУСТ: старые данные не «прилипли» к адресу.
    assert await _count(
        "SELECT COUNT(*) AS n FROM chat_session WHERE user_id = ?", (reborn["id"],)
    ) == 0


@pytest.mark.asyncio
async def test_other_members_survive_the_deletion(env: Any) -> None:
    """Удаление A не задевает данные B и владельца (кроме общей DM-ветки)."""
    client = env["client"]
    await _as(client, int(env["a"]["id"]))
    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    assert await _count(
        "SELECT COUNT(*) AS n FROM chat_message WHERE content = ?", (B_CHAT,)
    ) == 1
    assert await _count(
        "SELECT COUNT(*) AS n FROM user_memory WHERE text = ?", (O_MEMORY,)
    ) == 1
    assert await _count(
        "SELECT COUNT(*) AS n FROM skill WHERE name = ?", (B_SKILL,)
    ) == 1


@pytest.mark.asyncio
async def test_deletion_is_logged_without_content(env: Any) -> None:
    client = env["client"]
    a_id = int(env["a"]["id"])
    await _as(client, a_id)
    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT user_id, initiated_by, rows_deleted, deleted_at "
            "FROM account_deletion_log WHERE user_id = ?",
            (a_id,),
        )
        row = await cur.fetchone()
    assert row is not None, "факт удаления не задокументирован"
    assert int(row["user_id"]) == a_id
    assert str(row["initiated_by"]) == "self"
    assert int(row["rows_deleted"]) > 0
    assert row["deleted_at"]


# ── 5. Границы удаления ─────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_wrong_confirmation_deletes_nothing(env: Any) -> None:
    client = env["client"]
    a_id = int(env["a"]["id"])
    await _as(client, a_id)
    response = await client.post(
        "/settings/my-data/delete", data={"confirm": "удали"}, follow_redirects=False
    )
    assert response.status_code == 400
    assert await _count("SELECT COUNT(*) AS n FROM users WHERE id = ?", (a_id,)) == 1


@pytest.mark.asyncio
async def test_owner_cannot_delete_himself_through_this_route(env: Any) -> None:
    client = env["client"]
    owner_id = int(env["owner"]["id"])
    await _as(client, owner_id)
    response = await client.post(
        "/settings/my-data/delete", data={"confirm": O_EMAIL}, follow_redirects=False
    )
    assert response.status_code == 403
    assert await _count("SELECT COUNT(*) AS n FROM users WHERE id = ?", (owner_id,)) == 1


@pytest.mark.asyncio
async def test_owner_guard_holds_at_the_service_layer_too(env: Any) -> None:
    owner_id = int(env["owner"]["id"])
    allowed, reason = await can_delete(owner_id)
    assert allowed is False
    assert reason == "owner_not_deletable"
    result = await delete_own_account(owner_id)
    assert result.ok is False
    assert await _count("SELECT COUNT(*) AS n FROM users WHERE id = ?", (owner_id,)) == 1


@pytest.mark.asyncio
async def test_member_cannot_delete_another_member_by_passing_a_foreign_id(
    env: Any,
) -> None:
    """Чужой uid скармливаем ВСЕМИ способами, какие принимает эндпоинт.

    Роут вообще не объявляет параметра с id: личность берётся только из
    сессии. Тест фиксирует это как контракт, а не как «сейчас так вышло».
    """
    client = env["client"]
    a_id = int(env["a"]["id"])
    b_id = int(env["b"]["id"])
    owner_id = int(env["owner"]["id"])
    await _as(client, a_id)

    attempts = (
        {"confirm": B_EMAIL},
        {"confirm": B_EMAIL, "user_id": str(b_id)},
        {"confirm": A_EMAIL, "user_id": str(b_id)},
        {"confirm": A_EMAIL, "uid": str(b_id)},
        {"confirm": A_EMAIL, "id": str(b_id)},
        {"confirm": O_EMAIL, "user_id": str(owner_id)},
    )
    for data in attempts[:-1]:
        response = await client.post(
            f"/settings/my-data/delete?user_id={b_id}&uid={b_id}",
            data=data,
            follow_redirects=False,
        )
        # Либо отказ по подтверждению, либо удаление СВОЕГО аккаунта — но
        # чужой обязан выжить в любом случае.
        assert response.status_code in (200, 303, 400)
        assert await _count(
            "SELECT COUNT(*) AS n FROM users WHERE id = ?", (b_id,)
        ) == 1, f"чужой аккаунт удалён через {data}"
        assert await _count(
            "SELECT COUNT(*) AS n FROM users WHERE id = ?", (owner_id,)
        ) == 1
        if response.status_code == 303:
            break

    # JSON-тело тоже не проходит (роут принимает только form-data).
    await _as(client, b_id)
    json_try = await client.post(
        "/settings/my-data/delete",
        json={"confirm": B_EMAIL, "user_id": a_id},
        follow_redirects=False,
    )
    assert json_try.status_code in (400, 422)
    assert await _count("SELECT COUNT(*) AS n FROM users WHERE id = ?", (b_id,)) == 1


@pytest.mark.asyncio
async def test_anonymous_cannot_call_the_delete_endpoint(env: Any) -> None:
    client = env["client"]
    client.cookies.clear()
    response = await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    assert response.status_code in (302, 303, 307, 401, 403)
    assert await _count(
        "SELECT COUNT(*) AS n FROM users WHERE id = ?", (int(env["a"]["id"]),)
    ) == 1


# ── 6. Согласие ─────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_signup_records_consent_with_the_current_policy_version(
    env: Any,
) -> None:
    client = env["client"]
    client.cookies.clear()
    response = await client.post(
        "/auth/signup",
        data={
            "email": "consent-signup@rights.test",
            "password": "Ty6-Foxtrot-Passphrase",
            "display_name": "Согласный",
            "consent": "1",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303, response.text[:400]
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT c.policy_version, c.source, c.user_agent FROM user_consent c "
            "JOIN users u ON u.id = c.user_id WHERE u.email = ?",
            ("consent-signup@rights.test",),
        )
        row = await cur.fetchone()
    assert row is not None, "согласие не зафиксировано"
    assert str(row["policy_version"]) == POLICY_VERSION
    assert str(row["source"]) == "checkbox"


@pytest.mark.asyncio
async def test_register_without_checkbox_is_recorded_honestly(env: Any) -> None:
    """Легаси-путь лендинга регистрирует без поля — источник помечается честно."""
    client = env["client"]
    client.cookies.clear()
    response = await client.post(
        "/auth/register",
        data={"email": "consent-register@rights.test"},
        headers={"X-Requested-With": "fetch"},
    )
    assert response.status_code == 200, response.text[:300]
    async with get_connection() as conn:
        cur = await conn.execute(
            "SELECT c.source, c.policy_version FROM user_consent c "
            "JOIN users u ON u.id = c.user_id WHERE u.email = ?",
            ("consent-register@rights.test",),
        )
        row = await cur.fetchone()
    assert row is not None
    assert str(row["source"]) == "form_submit"
    assert str(row["policy_version"]) == POLICY_VERSION


@pytest.mark.asyncio
async def test_existing_accounts_without_a_row_are_pre_versioning(env: Any) -> None:
    """Старые аккаунты не блокируются и задним числом согласие не выдумывается."""
    from app.auth.consent import PRE_VERSIONING, consent_state

    assert await consent_state(int(env["a"]["id"])) == PRE_VERSIONING


@pytest.mark.asyncio
async def test_consent_row_is_exported_and_dies_with_the_account(env: Any) -> None:
    client = env["client"]
    a_id = int(env["a"]["id"])
    async with get_connection() as conn:
        await conn.execute(
            "INSERT INTO user_consent (user_id, policy_version, ip, user_agent, source) "
            "VALUES (?, ?, '203.0.113.9', 'pytest-ua', 'checkbox')",
            (a_id, POLICY_VERSION),
        )
        await conn.commit()

    payload = await build_export(a_id)
    assert payload["consent"]["state"] == POLICY_VERSION
    assert payload["consent"]["records"][0]["ip"] == "203.0.113.9"

    await _as(client, a_id)
    await client.post(
        "/settings/my-data/delete", data={"confirm": A_EMAIL}, follow_redirects=False
    )
    assert await _count(
        "SELECT COUNT(*) AS n FROM user_consent WHERE user_id = ?", (a_id,)
    ) == 0
