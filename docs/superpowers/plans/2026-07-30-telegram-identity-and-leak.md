# Telegram Identity & Prompt-Leak Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Persona в Telegram перестаёт выплёвывать служебные блоки промпта в чат и начинает надёжно различать участников, а владелец может править их имена и роли на сайте.

**Architecture:** Три независимых изменения в существующем коде плюс одна новая таблица. Гвард вывода получает санитайзер служебной разметки. Снимаются обрезки, которые выбрасывали уже вычисленный справочник участников из промпта. Правки владельца хранятся в отдельной таблице `telegram_person_override`, а не в колонках `telegram_person`, потому что `observe_message` перезаписывает их на каждом входящем сообщении через `ON CONFLICT DO UPDATE`.

**Tech Stack:** Python 3.12, FastAPI, aiosqlite, Jinja2, pytest + pytest-asyncio.

## Global Constraints

- Интерпретатор: `./.venv/Scripts/python.exe` (Windows, Git Bash).
- Каждый фич-коммит бампает `app/__init__.py __version__` и `CACHE_VERSION` в `app/web/static/sw.js` — одинаковая версия. Текущая: `2.30.2`.
- Новые страницы настроек регистрируются в `app/web/routes/settings_hub.py` → `_CATEGORIES`. `/settings/telegram-people` уже зарегистрирована (строки 57 и 115) — повторно добавлять не нужно.
- Файл миграции: следующий свободный номер — `221`. Каталог `app/storage/migrations/`.
- `tests/test_copilot_route.py` не собирается из-за отсутствующего `markdown_it` — запускать тесты с `--ignore=tests/test_copilot_route.py`.
- В `tests/test_ambient_group.py` четыре теста падают до начала этой работы (`test_decision_adapter_is_bounded_and_rejects_raw_metadata` ×3, `test_group_reply_adapter_uses_only_group_history_and_persists_once`). Это известное расхождение теста с кодом, не регрессия — чинить в рамках этого плана не нужно, но и сломать сильнее нельзя.
- Владелец в `telegram_person` защищён частичным уникальным индексом `uq_telegram_person_single_owner`. Любая запись `is_owner=1` обязана сначала снять флаг с остальных в той же транзакции.
- Инвариант доверия: правки владельца через сайт — доверенные; `telegram_person_fact` (слова самого участника) остаются недоверенными и в промпте лежат под ключом `untrusted_remembered_claims_by_current_sender`. Смешивать нельзя.

**Область плана:** срезы 1 и 2 из `docs/superpowers/specs/2026-07-30-telegram-rich-media-identity-design.md`. Срез 3 (медиа и стикеры), срезы 4-5 (инструменты и MCP-гейт) получают отдельные планы после сдачи этого.

---

### Task 1: Санитайзер служебной разметки в выводе

Модель воспроизвела в чат содержимое `<TRUSTED_TELEGRAM_IDENTITY>`. Блок вклеивается в системный промпт в `app/adapters/conversation/legacy.py:228`, а `persona_only_reply` его не ловит.

**Files:**
- Modify: `app/integrations/telegram/output_guard.py`
- Test: `tests/test_telegram_people.py`

**Interfaces:**
- Consumes: ничего.
- Produces: `strip_internal_markup(value: str) -> str` в `app/integrations/telegram/output_guard.py`; вызывается первой строкой `persona_only_reply`, которая сохраняет сигнатуру `persona_only_reply(value: str) -> str`.

- [ ] **Step 1: Write the failing tests**

Добавить в конец `tests/test_telegram_people.py`:

```python
def test_identity_block_never_reaches_the_chat() -> None:
    leaked = (
        "Конечно, вот два новых стикера. "
        "<TRUSTED_TELEGRAM_IDENTITY>\n"
        "AUTHORITATIVE CURRENT TELEGRAM TURN:\n"
        "- current_message_author_id=2133993638\n"
        "- current_message_author_name=Empty\n"
        "</TRUSTED_TELEGRAM_IDENTITY>"
    )
    assert persona_only_reply(leaked) == "Конечно, вот два новых стикера."


def test_unclosed_internal_tag_drops_the_tail() -> None:
    leaked = (
        "Ладно, смотрю. <TRUSTED_PERSONA_STYLE>\n"
        "Ты — Persona, самостоятельный участник разговора"
    )
    assert persona_only_reply(leaked) == "Ладно, смотрю."


def test_bare_identity_header_lines_are_removed() -> None:
    leaked = (
        "SERVER-VERIFIED TELEGRAM IDENTITY (numeric ids are authoritative):\n"
        "- sole_owner_creator_id=100\n"
        "Only Telegram user_id=100 is Persona's owner.\n"
        "Привет, чем занят?"
    )
    assert persona_only_reply(leaked) == "Привет, чем занят?"


def test_reply_that_is_only_internal_markup_becomes_empty() -> None:
    assert persona_only_reply(
        "<TRUSTED_TELEGRAM_IDENTITY>AUTHORITATIVE CURRENT TELEGRAM TURN:"
        "</TRUSTED_TELEGRAM_IDENTITY>"
    ) == ""


def test_ordinary_angle_brackets_survive() -> None:
    assert persona_only_reply("Условие: a < b и b > c, это важно.") == (
        "Условие: a < b и b > c, это важно."
    )
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -k "internal or identity_block or bare_identity or angle" -v`

Expected: FAIL — служебный текст остаётся в результате.

- [ ] **Step 3: Implement the sanitiser**

В `app/integrations/telegram/output_guard.py` после блока `_AI_REFUSAL_PREFIX_RE` добавить:

```python
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
_INTERNAL_BLOCK_RE = re.compile(
    r"<(?P<tag>" + "|".join(_INTERNAL_TAGS) + r")\b[^>]*>.*?(?:</(?P=tag)>|\Z)",
    re.IGNORECASE | re.DOTALL,
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


def strip_internal_markup(value: str) -> str:
    """Удалить служебные секции промпта, если модель их воспроизвела.

    Ловит и незакрытый тег: обрыв генерации оставляет открывающий тег без
    пары, и всё после него — служебный текст.
    """
    text = str(value or "")
    text = _INTERNAL_BLOCK_RE.sub(" ", text)
    text = _INTERNAL_LINE_RE.sub("", text)
    return re.sub(r"[ \t]+", " ", text).strip()
```

Первой строкой тела `persona_only_reply` заменить

```python
    text = str(value or "").strip()
```

на

```python
    text = strip_internal_markup(value)
```

и дополнить экспорт:

```python
__all__ = ["persona_only_reply", "strip_internal_markup"]
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -v`

Expected: PASS, включая старые тесты гварда.

- [ ] **Step 5: Проверить, что молчание доходит до транспорта**

`app/integrations/telegram/ambient.py:174` уже содержит `if not answer or _contains_tool_markup(answer): return ""`. `app/integrations/telegram/service.py:108` при пустом результате отдаёт `"Я не буду придумывать ответы за других участников."` — для чисто служебного вывода это неверная фраза. Заменить на:

```python
        guarded = persona_only_reply(result.answer)
        return guarded or "Что-то пошло не так с ответом — переспроси, пожалуйста."
```

- [ ] **Step 6: Bump version and commit**

В `app/__init__.py` поднять `__version__` до `2.30.3`, в `app/web/static/sw.js` — `CACHE_VERSION` на `'persona-v2.30.3'`.

```bash
git add app/integrations/telegram/output_guard.py app/integrations/telegram/service.py app/__init__.py app/web/static/sw.js tests/test_telegram_people.py
git commit -m "Strip leaked internal prompt markup from Telegram replies"
git push origin master
```

---

### Task 2: Вернуть в промпт справочник участников и историю

`identity_context()` собирает корректный блок, который затем режется дважды по 2 000 символов, из-за чего JSON с участниками обрывается на середине. Транскрипт Telegram обрезан до 800 символов.

**Files:**
- Modify: `app/integrations/telegram/service.py:105`
- Modify: `app/adapters/conversation/legacy.py:229`, `app/adapters/conversation/legacy.py:240`
- Test: `tests/test_conversation_service.py`

**Interfaces:**
- Consumes: `TurnCommand.metadata["telegram_identity_context"]` — уже существует.
- Produces: ничего нового; меняются только числовые пределы.

- [ ] **Step 1: Write the failing test**

Добавить в `tests/test_conversation_service.py` рядом с `test_telegram_entrypoint_maps_to_same_shared_command`:

```python
async def test_telegram_identity_context_survives_forty_people() -> None:
    """Справочник участников не должен обрываться на середине JSON."""
    from app.integrations.telegram.service import PersonaTelegramService

    identity = "AUTHORITATIVE CURRENT TELEGRAM TURN:\n" + "\n".join(
        f'{{"telegram_user_id":{1000 + i},"display_name":"Участник {i}"}}'
        for i in range(40)
    )
    assert len(identity) > 2_000

    capture = CapturingConversationService()
    service = PersonaTelegramService(
        repository=FakeTelegramRepository(),
        conversation_service=capture,
    )
    await service.respond(
        persona_user_id=7,
        telegram_chat_id=42,
        question="кто здесь есть?",
        chat_title="Личка",
        sender_label="Owner",
        is_owner=True,
        trusted_identity_context=identity,
    )
    passed = capture.commands[0].metadata["telegram_identity_context"]
    assert passed == identity, "identity context не должен обрезаться в адаптере"
```

`CapturingConversationService` и `FakeTelegramRepository` уже используются в `test_telegram_entrypoint_maps_to_same_shared_command` — переиспользовать их, а не писать новые.

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_conversation_service.py -k identity_context_survives -v`

Expected: FAIL — строка обрезана до 2 000 символов.

- [ ] **Step 3: Raise the three limits**

В `app/integrations/telegram/service.py` заменить

```python
                    "telegram_identity_context": trusted_identity_context[:2_000]
```

на

```python
                    # 2 000 обрывало JSON участников на середине — бот переставал
                    # различать людей. Блок строится сервером и ограничен 40 людьми.
                    "telegram_identity_context": trusted_identity_context[:12_000]
```

В `app/adapters/conversation/legacy.py` заменить `f"{identity[:2_000]}\n"` на `f"{identity[:12_000]}\n"` и в `_bounded_transcript` заменить

```python
                800
                if command.surface is ConversationSurface.TELEGRAM
```

на

```python
                6_000
                if command.surface is ConversationSurface.TELEGRAM
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_conversation_service.py tests/test_telegram_integration.py -v --ignore=tests/test_copilot_route.py`

Expected: PASS.

- [ ] **Step 5: Bump version and commit**

`__version__` → `2.30.4`, `CACHE_VERSION` → `'persona-v2.30.4'`.

```bash
git add app/integrations/telegram/service.py app/adapters/conversation/legacy.py app/__init__.py app/web/static/sw.js tests/test_conversation_service.py
git commit -m "Stop truncating Telegram identity context and transcript"
git push origin master
```

---

### Task 3: Таблица правок владельца

Правки нельзя хранить в колонках `telegram_person`: `observe_message` делает `ON CONFLICT(...) DO UPDATE SET display_name=excluded.display_name`, то есть затрёт их на первом же входящем сообщении.

**Files:**
- Create: `app/storage/migrations/221_telegram_person_override.sql`
- Modify: `app/integrations/telegram/people.py`
- Test: `tests/test_telegram_people.py`

**Interfaces:**
- Consumes: таблицу `telegram_person` (составной ключ `persona_user_id, telegram_user_id`).
- Produces: на `TelegramPeopleRepository` два метода —
  `set_override(persona_user_id: int, telegram_user_id: int, *, display_name: str, note: str, ignored: bool) -> None`
  и `get_override(persona_user_id: int, telegram_user_id: int) -> dict[str, Any] | None`
  (ключи `display_name_override`, `note`, `ignored`);
  плюс `is_ignored(persona_user_id: int, telegram_user_id: int) -> bool`.

- [ ] **Step 1: Write the migration**

Создать `app/storage/migrations/221_telegram_person_override.sql`:

```sql
CREATE TABLE IF NOT EXISTS telegram_person_override (
    persona_user_id       INTEGER NOT NULL,
    telegram_user_id      INTEGER NOT NULL,
    display_name_override TEXT,
    note                  TEXT,
    ignored               INTEGER NOT NULL DEFAULT 0 CHECK (ignored IN (0, 1)),
    updated_at            TEXT NOT NULL DEFAULT (datetime('now')),
    PRIMARY KEY (persona_user_id, telegram_user_id),
    FOREIGN KEY (persona_user_id, telegram_user_id)
        REFERENCES telegram_person(persona_user_id, telegram_user_id)
        ON DELETE CASCADE
);
```

- [ ] **Step 2: Write the failing test**

Добавить в `tests/test_telegram_people.py`:

```python
async def test_owner_override_survives_new_messages(db) -> None:
    await _user(db)
    repository = TelegramPeopleRepository()
    await repository.observe_message(
        persona_user_id=7,
        owner_telegram_user_id=100,
        chat_id=-5,
        sender={"id": 100, "first_name": "Empty", "username": "YaroslavEmpty"},
        message_id=1,
        text="привет",
    )
    await repository.set_override(
        7, 100, display_name="Ярослав", note="владелец, зови по имени", ignored=False
    )
    # Новое сообщение переписывает telegram_person из данных Telegram.
    await repository.observe_message(
        persona_user_id=7,
        owner_telegram_user_id=100,
        chat_id=-5,
        sender={"id": 100, "first_name": "Empty", "username": "YaroslavEmpty"},
        message_id=2,
        text="ещё раз привет",
    )
    override = await repository.get_override(7, 100)
    assert override is not None
    assert override["display_name_override"] == "Ярослав"
    assert override["note"] == "владелец, зови по имени"


async def test_ignored_flag_round_trips(db) -> None:
    await _user(db)
    repository = TelegramPeopleRepository()
    await repository.observe_message(
        persona_user_id=7,
        owner_telegram_user_id=100,
        chat_id=-5,
        sender={"id": 555, "first_name": "Спамер"},
        message_id=1,
        text="купите крипту",
    )
    assert await repository.is_ignored(7, 555) is False
    await repository.set_override(7, 555, display_name="", note="", ignored=True)
    assert await repository.is_ignored(7, 555) is True
```

- [ ] **Step 3: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -k "override or ignored_flag" -v`

Expected: FAIL с `AttributeError: 'TelegramPeopleRepository' object has no attribute 'set_override'`.

- [ ] **Step 4: Implement the repository methods**

В `app/integrations/telegram/people.py` добавить в класс `TelegramPeopleRepository` после `get_person`:

```python
    async def set_override(
        self,
        persona_user_id: int,
        telegram_user_id: int,
        *,
        display_name: str,
        note: str,
        ignored: bool,
    ) -> None:
        """Сохранить доверенную правку владельца об участнике."""
        clean_name = _clean(display_name, 160)
        clean_note = _clean(note, 500)
        async with write_transaction() as conn:
            await conn.execute(
                """
                INSERT INTO telegram_person_override(
                    persona_user_id, telegram_user_id, display_name_override,
                    note, ignored, updated_at
                )
                VALUES(?,?,?,?,?,datetime('now'))
                ON CONFLICT(persona_user_id, telegram_user_id) DO UPDATE SET
                    display_name_override=excluded.display_name_override,
                    note=excluded.note,
                    ignored=excluded.ignored,
                    updated_at=datetime('now')
                """,
                (
                    int(persona_user_id),
                    _positive_id(telegram_user_id, "person"),
                    clean_name or None,
                    clean_note or None,
                    1 if ignored else 0,
                ),
            )

    async def get_override(
        self, persona_user_id: int, telegram_user_id: int
    ) -> dict[str, Any] | None:
        async with get_connection() as conn:
            cursor = await conn.execute(
                """
                SELECT display_name_override, note, ignored
                  FROM telegram_person_override
                 WHERE persona_user_id=? AND telegram_user_id=?
                """,
                (int(persona_user_id), int(telegram_user_id)),
            )
            row = await cursor.fetchone()
        return dict(row) if row is not None else None

    async def is_ignored(self, persona_user_id: int, telegram_user_id: int) -> bool:
        override = await self.get_override(persona_user_id, telegram_user_id)
        return bool(override and override.get("ignored"))
```

`write_transaction`, `get_connection`, `_clean` и `_positive_id` уже импортированы/определены в модуле.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -v`

Expected: PASS.

- [ ] **Step 6: Bump version and commit**

`__version__` → `2.30.5`, `CACHE_VERSION` → `'persona-v2.30.5'`.

```bash
git add app/storage/migrations/221_telegram_person_override.sql app/integrations/telegram/people.py app/__init__.py app/web/static/sw.js tests/test_telegram_people.py
git commit -m "Store owner overrides for Telegram people"
git push origin master
```

---

### Task 4: Применить правки владельца в identity-контексте

**Files:**
- Modify: `app/integrations/telegram/people.py:263-366` (`identity_context`)
- Test: `tests/test_telegram_people.py`

**Interfaces:**
- Consumes: `get_override` из Task 3.
- Produces: в JSON identity-блока появляется ключ `trusted_owner_notes` — список строк `"<display_name> [tg_user_id=N]: <note>"`. Ключ `untrusted_remembered_claims_by_current_sender` сохраняется без изменений.

- [ ] **Step 1: Write the failing test**

```python
async def test_identity_context_uses_owner_override_and_separates_trust(db) -> None:
    await _user(db)
    repository = TelegramPeopleRepository()
    await repository.observe_message(
        persona_user_id=7,
        owner_telegram_user_id=100,
        chat_id=-5,
        sender={"id": 100, "first_name": "Empty", "username": "YaroslavEmpty"},
        message_id=1,
        text="я люблю архитектуру",
    )
    await repository.set_override(
        7, 100, display_name="Ярослав", note="владелец проекта", ignored=False
    )
    context = await repository.identity_context(
        persona_user_id=7,
        owner_telegram_user_id=100,
        current_sender_id=100,
        chat_id=-5,
    )
    assert "current_message_author_name=Ярослав" in context
    assert "Empty" not in context
    assert "trusted_owner_notes" in context
    assert "владелец проекта" in context
    # Слова самого участника остаются в недоверенной секции.
    assert "untrusted_remembered_claims_by_current_sender" in context
```

- [ ] **Step 2: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -k identity_context_uses_owner_override -v`

Expected: FAIL — в контексте `Empty`, ключа `trusted_owner_notes` нет.

- [ ] **Step 3: Apply overrides inside identity_context**

В `identity_context`, внутри блока `async with get_connection() as conn:` после запроса `people_cur`/`facts_cur` добавить третий запрос:

```python
            override_cur = await conn.execute(
                """
                SELECT telegram_user_id, display_name_override, note
                  FROM telegram_person_override
                 WHERE persona_user_id=?
                   AND (display_name_override IS NOT NULL OR note IS NOT NULL)
                """,
                (tenant,),
            )
            overrides = {
                int(row["telegram_user_id"]): dict(row)
                for row in await override_cur.fetchall()
            }
```

Сразу после строки `by_id = {int(item["telegram_user_id"]): item for item in people}` вставить применение правок и сбор заметок:

```python
        owner_notes: list[str] = []
        for person_id, item in by_id.items():
            override = overrides.get(person_id)
            if not override:
                continue
            name_override = str(override.get("display_name_override") or "").strip()
            if name_override:
                item["display_name"] = name_override
            note = str(override.get("note") or "").strip()
            if note:
                owner_notes.append(
                    f"{item['display_name']} [tg_user_id={person_id}]: {note}"
                )
```

В словарь, который уходит в `json.dumps`, добавить ключ **перед** недоверенным:

```python
                "trusted_owner_notes": owner_notes,
                "untrusted_remembered_claims_by_current_sender": claims,
```

Пояснение про доверие дописать в хвост возвращаемой строки, сразу после `"Keep every person's facts separate. "`:

```python
            "trusted_owner_notes are written by the owner in Persona's web "
            "settings and are authoritative about who these people are. "
```

Поскольку `sender` берётся из `by_id`, правка имени применяется к нему автоматически, и `current_name` собирается уже с новым именем — важно, чтобы вычисление `current_name` осталось **после** цикла применения правок.

- [ ] **Step 4: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -v`

Expected: PASS.

- [ ] **Step 5: Bump version and commit**

`__version__` → `2.30.6`, `CACHE_VERSION` → `'persona-v2.30.6'`.

```bash
git add app/integrations/telegram/people.py app/__init__.py app/web/static/sw.js tests/test_telegram_people.py
git commit -m "Apply owner overrides to Telegram identity context"
git push origin master
```

---

### Task 5: Редактирование людей на сайте

**Files:**
- Modify: `app/web/routes/telegram_people.py`
- Modify: `app/web/templates/telegram_person.html`
- Modify: `app/integrations/telegram/people.py` (`person_detail` отдаёт override)
- Test: `tests/test_telegram_people.py`

**Interfaces:**
- Consumes: `set_override`, `get_override` из Task 3.
- Produces: `POST /settings/telegram-people/{telegram_user_id}` (форма: `display_name`, `note`, `ignored`) и `POST /settings/telegram-people/{telegram_user_id}/owner`; оба отвечают редиректом 303 на страницу человека. `person_detail` начинает возвращать ключ `override`.

- [ ] **Step 1: Write the failing test**

```python
async def test_person_detail_includes_override(db) -> None:
    await _user(db)
    repository = TelegramPeopleRepository()
    await repository.observe_message(
        persona_user_id=7,
        owner_telegram_user_id=100,
        chat_id=-5,
        sender={"id": 100, "first_name": "Empty"},
        message_id=1,
        text="привет",
    )
    await repository.set_override(7, 100, display_name="Ярослав", note="", ignored=False)
    detail = await repository.person_detail(7, 100)
    assert detail is not None
    assert detail["override"]["display_name_override"] == "Ярослав"


async def test_owner_reassignment_keeps_single_owner(db) -> None:
    await _user(db)
    repository = TelegramPeopleRepository()
    for telegram_id, name in ((100, "Empty"), (555, "Олег")):
        await repository.observe_message(
            persona_user_id=7,
            owner_telegram_user_id=100,
            chat_id=-5,
            sender={"id": telegram_id, "first_name": name},
            message_id=telegram_id,
            text="привет",
        )
    await repository.set_owner(7, 555)
    people = {int(p["telegram_user_id"]): p for p in await repository.list_people(7)}
    assert people[555]["is_owner"] == 1
    assert people[100]["is_owner"] == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -k "detail_includes_override or owner_reassignment" -v`

Expected: FAIL — `KeyError: 'override'` и отсутствующий `set_owner`.

- [ ] **Step 3: Add set_owner and extend person_detail**

В `TelegramPeopleRepository` добавить:

```python
    async def set_owner(self, persona_user_id: int, telegram_user_id: int) -> None:
        """Перепривязать роль владельца. Ровно один владелец на арендатора."""
        tenant = int(persona_user_id)
        owner_id = _positive_id(telegram_user_id, "owner")
        async with write_transaction() as conn:
            # Снять флаг первым: частичный уникальный индекс
            # uq_telegram_person_single_owner не допускает двух владельцев.
            await conn.execute(
                "UPDATE telegram_person SET is_owner=0 "
                "WHERE persona_user_id=? AND telegram_user_id<>?",
                (tenant, owner_id),
            )
            await conn.execute(
                "UPDATE telegram_person SET is_owner=1 "
                "WHERE persona_user_id=? AND telegram_user_id=?",
                (tenant, owner_id),
            )
```

В `person_detail` перед `return` заменить возвращаемый словарь на:

```python
        return {
            "person": person,
            "facts": facts,
            "messages": messages,
            "override": await self.get_override(persona_user_id, telegram_user_id)
            or {"display_name_override": "", "note": "", "ignored": 0},
        }
```

- [ ] **Step 4: Add the POST routes**

В `app/web/routes/telegram_people.py` заменить импорт FastAPI на

```python
from fastapi import APIRouter, Depends, Form, HTTPException, Request
from fastapi.responses import HTMLResponse, RedirectResponse
```

и добавить в конец файла перед `__all__`:

```python
@router.post("/settings/telegram-people/{telegram_user_id}")
async def telegram_person_save(
    telegram_user_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
    display_name: str = Form(""),
    note: str = Form(""),
    ignored: str = Form(""),
) -> RedirectResponse:
    user_id = await _owner_id(session)
    if await _people.get_person(user_id, telegram_user_id) is None:
        raise HTTPException(status_code=404, detail="Telegram-пользователь не найден")
    await _people.set_override(
        user_id,
        telegram_user_id,
        display_name=display_name,
        note=note,
        ignored=ignored == "on",
    )
    return RedirectResponse(
        f"/settings/telegram-people/{telegram_user_id}", status_code=303
    )


@router.post("/settings/telegram-people/{telegram_user_id}/owner")
async def telegram_person_make_owner(
    telegram_user_id: int,
    session: Annotated[SessionRecord, Depends(current_user_required)],
) -> RedirectResponse:
    user_id = await _owner_id(session)
    if await _people.get_person(user_id, telegram_user_id) is None:
        raise HTTPException(status_code=404, detail="Telegram-пользователь не найден")
    await _people.set_owner(user_id, telegram_user_id)
    return RedirectResponse(
        f"/settings/telegram-people/{telegram_user_id}", status_code=303
    )
```

- [ ] **Step 5: Add the form to the template**

В `app/web/templates/telegram_person.html` вставить перед строкой `<div class="grid gap-6 lg:grid-cols-2">`:

```html
<form method="post" action="/settings/telegram-people/{{ person.telegram_user_id }}"
      class="mb-6 rounded-xl border border-ink-700 bg-ink-800 p-6">
  <h2 class="mb-4 font-semibold">Как Persona воспринимает этого человека</h2>
  <label class="block text-sm text-zinc-400" for="display_name">Имя</label>
  <input id="display_name" name="display_name" maxlength="160"
         value="{{ override.display_name_override or '' }}"
         placeholder="{{ person.display_name }}"
         class="mt-1 w-full rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm">
  <label class="mt-4 block text-sm text-zinc-400" for="note">Кто это и как с ним говорить</label>
  <textarea id="note" name="note" rows="3" maxlength="500"
            placeholder="брат · коллега по работе · не воспринимать всерьёз"
            class="mt-1 w-full rounded-lg border border-ink-700 bg-ink-900 px-3 py-2 text-sm">{{ override.note or '' }}</textarea>
  <label class="mt-4 flex items-center gap-2 text-sm text-zinc-300">
    <input type="checkbox" name="ignored" {% if override.ignored %}checked{% endif %}
           class="rounded border-ink-700 bg-ink-900">
    Не отвечать этому человеку
  </label>
  <button type="submit"
          class="mt-5 rounded-lg bg-violet-600 px-4 py-2 text-sm font-semibold hover:bg-violet-500">
    Сохранить
  </button>
</form>

{% if not person.is_owner %}
<form method="post" action="/settings/telegram-people/{{ person.telegram_user_id }}/owner"
      class="mb-6">
  <button type="submit" class="text-sm text-zinc-500 hover:text-violet-300">
    Сделать владельцем Persona
  </button>
</form>
{% endif %}
```

- [ ] **Step 6: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_people.py -v`

Expected: PASS.

- [ ] **Step 7: Bump version and commit**

`__version__` → `2.30.7`, `CACHE_VERSION` → `'persona-v2.30.7'`.

```bash
git add app/web/routes/telegram_people.py app/web/templates/telegram_person.html app/integrations/telegram/people.py app/__init__.py app/web/static/sw.js tests/test_telegram_people.py
git commit -m "Let the owner edit Telegram people on the site"
git push origin master
```

---

### Task 6: Учитывать флаг «не отвечать»

**Files:**
- Modify: `app/integrations/telegram/worker.py` (рядом с `_observe_person`, строка ~440)
- Test: `tests/test_telegram_integration.py`

**Interfaces:**
- Consumes: `is_ignored` из Task 3.
- Produces: ничего; поведение транспорта.

- [ ] **Step 1: Read the current flow**

Прочитать `app/integrations/telegram/worker.py:430-460`, чтобы увидеть, что возвращает `self._observe_person(...)` и где заканчивается наблюдение и начинается ответ. Флаг проверяется **после** `observe_person` — сообщение игнорируемого человека всё равно записывается в историю (Persona должна знать, что было сказано), но ответ не формируется.

- [ ] **Step 2: Write the failing test**

Добавить в `tests/test_telegram_integration.py`. Воркер принимает репозиторий людей четвёртым именованным аргументом (`worker.py:153`), поэтому подменяем его заглушкой — база в этом файле не нужна:

```python
class FakeIgnoringPeople:
    """Репозиторий людей, у которого один конкретный человек заглушен."""

    def __init__(self, ignored_id: int) -> None:
        self.ignored_id = ignored_id
        self.observed: list[int] = []

    async def observe_message(self, **kwargs: Any) -> Any:
        sender_id = int(kwargs["sender"]["id"])
        self.observed.append(sender_id)
        return SimpleNamespace(telegram_user_id=sender_id)

    async def identity_context(self, **kwargs: Any) -> str:
        return ""

    async def is_ignored(self, persona_user_id: int, telegram_user_id: int) -> bool:
        return int(telegram_user_id) == self.ignored_id


@pytest.mark.asyncio
async def test_muted_person_is_recorded_but_never_answered() -> None:
    repository = FakeRepository(
        TelegramBinding(telegram_user_id=1, persona_user_id=42)
    )
    worker, api, service = _worker(repository)
    people = FakeIgnoringPeople(ignored_id=1)
    worker.people = people  # type: ignore[assignment]

    await worker.handle_update(_private(1, "ответь мне что-нибудь", message_id=31))

    assert people.observed == [1], "сообщение должно попасть в историю"
    assert service.responses == [], "ответ формироваться не должен"
    assert api.sent == []
```

В шапку файла добавить импорт `from types import SimpleNamespace`, если его там ещё нет.

- [ ] **Step 3: Run test to verify it fails**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_integration.py -k muted_person -v`

Expected: FAIL — `service.responses` не пуст, бот ответил.

- [ ] **Step 4: Add the guard**

В `app/integrations/telegram/worker.py` найти вызов (строка ~440):

```python
        person, identity_context = await self._observe_person(
            incoming,
            binding,
        )
```

и сразу после него вставить:

```python
        if person is not None and await self.people.is_ignored(
            binding.persona_user_id, person.telegram_user_id
        ):
            # Сообщение уже записано — Persona знает, что было сказано, но
            # владелец отключил ответы этому человеку.
            return
```

`_observe_person` возвращает `(None, "")` при сбое (`worker.py:634`), поэтому проверка на `None` обязательна: без неё упавшее наблюдение превратится в `AttributeError` и уронит обработку сообщения.

- [ ] **Step 5: Run tests to verify they pass**

Run: `./.venv/Scripts/python.exe -m pytest tests/test_telegram_integration.py tests/test_telegram_people.py -v`

Expected: PASS.

- [ ] **Step 6: Full suite and commit**

Run: `./.venv/Scripts/python.exe -m pytest tests -q --ignore=tests/test_copilot_route.py`

Expected: падают только четыре известных теста в `tests/test_ambient_group.py`, перечисленные в Global Constraints. Любое другое падение — регрессия, чинить до коммита.

`__version__` → `2.30.8`, `CACHE_VERSION` → `'persona-v2.30.8'`.

```bash
git add app/integrations/telegram/worker.py app/__init__.py app/web/static/sw.js tests/test_telegram_integration.py
git commit -m "Skip replies to Telegram people the owner muted"
git push origin master
```

---

## Проверка вручную после плана

1. Перезапустить uvicorn (`lean`, `factory`, 3 воркера) — убить процессы WORKER раньше родителей, иначе на `:8000` останется старый код.
2. Открыть `/settings/telegram-people`, задать себе имя «Ярослав» вместо `Empty`, сохранить.
3. Написать боту в личку — он должен обратиться по имени.
4. В группе спросить «кто здесь есть» — в ответе должны фигурировать разные участники, а не один слипшийся.
5. Убедиться, что ни в одном ответе нет `AUTHORITATIVE`, `current_message_author` или угловых тегов.
