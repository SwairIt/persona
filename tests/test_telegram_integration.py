"""Security and routing tests for the owner-only Telegram adapter."""

from __future__ import annotations

import asyncio
import socket
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.integrations.telegram.api import _split_message
from app.integrations.telegram.config import TelegramConfig
from app.integrations.telegram.repository import (
    TelegramBinding,
    TelegramRepository,
)
from app.integrations.telegram.worker import (
    TelegramConsumerLeaseLost,
    TelegramWorker,
)


class FakeAPI:
    def __init__(self) -> None:
        self.sent: list[tuple[int, str, int | None]] = []
        self.typing: list[int] = []
        self.reactions: list[tuple[int, int, str]] = []
        self.commands: list[list[dict[str, str]]] = []
        self.fail_commands = False

    async def get_me(self) -> dict[str, Any]:
        return {"id": 777, "username": "PersonaTestBot"}

    async def send_message(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        self.sent.append((chat_id, text, reply_to_message_id))

    async def send_typing(self, chat_id: int) -> None:
        self.typing.append(chat_id)

    async def set_message_reaction(
        self,
        chat_id: int,
        message_id: int,
        reaction: str,
    ) -> None:
        self.reactions.append((chat_id, message_id, reaction))

    async def set_my_commands(self, commands: list[dict[str, str]]) -> None:
        if self.fail_commands:
            raise RuntimeError("temporary command metadata failure")
        self.commands.append(commands)


class FakeRepository:
    def __init__(self, binding: TelegramBinding | None = None) -> None:
        self.binding = binding
        self.groups: set[int] = set()
        self.pairing_code = "correct-code"
        self.cleared: list[int] = []
        self.claimed_updates: set[int] = set()
        self.finished_updates: list[tuple[int, str, str]] = []
        self.renew_processing = True
        self.lease_holder: str | None = None
        self.released_holders: list[str] = []
        self.behavior_rules: dict[int, list[str]] = {}

    async def get_binding(self) -> TelegramBinding | None:
        return self.binding

    async def bind_owner(self, telegram_user_id: int, persona_user_id: int) -> TelegramBinding:
        self.binding = TelegramBinding(telegram_user_id, persona_user_id)
        return self.binding

    async def allowed_chat_ids(self) -> set[int]:
        return set(self.groups)

    async def set_chat_allowed(self, chat_id: int, allowed: bool) -> None:
        if allowed:
            self.groups.add(chat_id)
        else:
            self.groups.discard(chat_id)

    async def group_behavior_rules(self, chat_id: int) -> tuple[str, ...]:
        return tuple(self.behavior_rules.get(chat_id, []))

    async def remember_group_behavior_rule(self, chat_id: int, rule: str) -> None:
        self.behavior_rules.setdefault(chat_id, []).append(rule)

    async def verify_pairing_code(self, candidate: str, configured_secret: str = "") -> bool:
        expected = configured_secret or self.pairing_code
        return candidate == expected

    async def claim_update(
        self,
        update_id: int,
        holder_id: str,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        del holder_id, lease_seconds
        if update_id in self.claimed_updates:
            return False
        self.claimed_updates.add(update_id)
        return True

    async def renew_processing_lease(
        self,
        update_id: int,
        holder_id: str,
        *,
        lease_seconds: int = 600,
    ) -> bool:
        del update_id, holder_id, lease_seconds
        return self.renew_processing

    async def finish_update(
        self,
        update_id: int,
        holder_id: str,
        *,
        status: str,
        outcome: str,
    ) -> bool:
        del holder_id
        self.finished_updates.append((update_id, status, outcome))
        return True

    async def worker_lease_holder(self) -> str | None:
        return self.lease_holder

    async def release_worker_lease(self, holder_id: str) -> None:
        self.released_holders.append(holder_id)
        if self.lease_holder == holder_id:
            self.lease_holder = None


class FakeService:
    def __init__(self) -> None:
        self.responses: list[dict[str, Any]] = []
        self.passive: list[dict[str, Any]] = []
        self.resets: list[int] = []
        self.ambient_answer = ""

    async def respond(self, **kwargs: Any) -> str:
        self.responses.append(kwargs)
        return "Ответ Persona"

    async def record_passive_group_message(self, **kwargs: Any) -> None:
        self.passive.append(kwargs)

    async def handle_ambient_group_message(self, **kwargs: Any) -> str:
        self.passive.append(kwargs)
        return self.ambient_answer

    async def reset_chat(self, chat_id: int) -> None:
        self.resets.append(chat_id)


def _private(sender_id: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": sender_id, "first_name": "User"},
            "chat": {"id": sender_id, "type": "private", "first_name": "User"},
            "text": text,
        },
    }


def _group(sender_id: int, chat_id: int, text: str, message_id: int = 1) -> dict[str, Any]:
    return {
        "update_id": message_id,
        "message": {
            "message_id": message_id,
            "from": {"id": sender_id, "first_name": f"User {sender_id}"},
            "chat": {"id": chat_id, "type": "supergroup", "title": "Team"},
            "text": text,
        },
    }


def _worker(
    repository: FakeRepository,
) -> tuple[TelegramWorker, FakeAPI, FakeService]:
    api = FakeAPI()
    service = FakeService()
    worker = TelegramWorker(
        TelegramConfig(bot_token="not-a-real-token"),
        api=api,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
    )
    worker._bot_id = 777
    worker._bot_username = "PersonaTestBot"
    worker._persona_owner_id = 42
    worker._binding = repository.binding
    return worker, api, service


@pytest.mark.asyncio
async def test_prepare_sets_commands_idempotently_and_failure_is_best_effort(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from app.integrations.telegram import worker as worker_module  # noqa: PLC0415

    async def owner_id() -> int:
        return 42

    monkeypatch.setattr(worker_module, "get_owner_user_id", owner_id)
    worker, api, _service = _worker(FakeRepository(TelegramBinding(1, 42)))

    await worker.prepare()
    await worker.prepare()

    assert len(api.commands) == 2
    assert api.commands[0] == api.commands[1]
    assert [item["command"] for item in api.commands[0]] == [
        "start",
        "help",
        "new",
        "persona",
        "allow_here",
        "deny_here",
    ]

    api.fail_commands = True
    await worker.prepare()
    assert worker._bot_id == 777


@pytest.mark.asyncio
async def test_unbound_and_foreign_private_messages_are_default_denied() -> None:
    unbound, api, service = _worker(FakeRepository())
    await unbound.handle_update(_private(100, "привет"))
    assert api.sent == []
    assert service.responses == []

    bound, api, service = _worker(
        FakeRepository(TelegramBinding(telegram_user_id=1, persona_user_id=42))
    )
    await bound.handle_update(_private(100, "привет"))
    assert api.sent == []
    assert service.responses == []


@pytest.mark.asyncio
async def test_explicit_reaction_is_immediate_and_skips_llm() -> None:
    worker, api, service = _worker(
        FakeRepository(TelegramBinding(telegram_user_id=1, persona_user_id=42))
    )

    await worker.handle_update(
        _private(1, "Привет, поставь на это сообщение реакцию любую", message_id=17)
    )

    assert api.reactions == [(1, 17, "👍")]
    assert service.responses == []
    assert api.sent == []


@pytest.mark.asyncio
async def test_multiple_reaction_request_explains_telegram_limit() -> None:
    worker, api, service = _worker(
        FakeRepository(TelegramBinding(telegram_user_id=1, persona_user_id=42))
    )

    await worker.handle_update(
        _private(1, "Посмтавь ка несколько сразу, можешь", message_id=18)
    )

    assert api.reactions == [(1, 18, "👍")]
    assert len(api.sent) == 1
    assert "только одну" in api.sent[0][1]
    assert service.responses == []


@pytest.mark.asyncio
async def test_dead_same_host_telegram_lease_is_reclaimed(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repository = FakeRepository(TelegramBinding(telegram_user_id=1, persona_user_id=42))
    stale = f"{socket.gethostname()}:99999999:stale-token"
    repository.lease_holder = stale
    worker, _api, _service = _worker(repository)
    monkeypatch.setattr("app.integrations.telegram.worker.psutil.pid_exists", lambda _pid: False)

    assert await worker._reclaim_dead_local_worker_lease() is True
    assert repository.lease_holder is None
    assert repository.released_holders == [stale]


@pytest.mark.asyncio
async def test_pairing_only_works_in_private_with_correct_code() -> None:
    repository = FakeRepository()
    worker, api, _service = _worker(repository)

    await worker.handle_update(_private(10, "/claim wrong"))
    assert repository.binding is None
    assert api.sent == []

    await worker.handle_update(_group(10, -50, "/claim correct-code"))
    assert repository.binding is None

    await worker.handle_update(_private(10, "/claim correct-code"))
    assert repository.binding == TelegramBinding(10, 42)
    assert len(api.sent) == 1


@pytest.mark.asyncio
async def test_only_owner_can_allow_group_then_member_can_address_persona() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    worker, api, service = _worker(repository)

    await worker.handle_update(_group(2, -100, "/allow_here"))
    assert -100 not in repository.groups
    assert api.sent == []

    await worker.handle_update(_group(1, -100, "/allow_here", 2))
    assert -100 in repository.groups

    await worker.handle_update(_group(2, -100, "@PersonaTestBot что ты помнишь?", 3))
    assert service.responses[0]["persona_user_id"] == 42
    assert service.responses[0]["question"] == "что ты помнишь?"
    assert service.responses[0]["include_private_context"] is False
    assert service.responses[0]["allow_tools"] is False
    assert api.sent[-1][1] == "Ответ Persona"

    await worker.handle_update(_group(1, -100, "/persona мой контекст", 4))
    assert service.responses[1]["include_private_context"] is False
    assert service.responses[1]["allow_tools"] is False

    reply = _group(2, -100, "продолжи, пожалуйста", 5)
    reply["message"]["reply_to_message"] = {"from": {"id": 777, "is_bot": True}}
    await worker.handle_update(reply)
    assert service.responses[2]["question"] == "продолжи, пожалуйста"
    assert service.responses[2]["include_private_context"] is False
    assert service.passive == []


@pytest.mark.asyncio
async def test_owner_first_group_message_auto_allows_and_gets_answer() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    worker, api, service = _worker(repository)

    await worker.handle_update(_group(1, -101, "Привет, Persona", 20))

    assert -101 in repository.groups
    assert len(service.responses) == 1
    assert service.responses[0]["question"] == "Привет, Persona"
    assert service.responses[0]["include_private_context"] is False
    assert service.responses[0]["allow_tools"] is False
    assert api.sent[-1] == (-101, "Ответ Persona", 20)


@pytest.mark.asyncio
async def test_plain_persona_name_addresses_bot_without_at_mention() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-102)
    worker, api, service = _worker(repository)

    await worker.handle_update(
        _group(2, -102, "Персона, поздоровайся со всеми", 21)  # noqa: RUF001
    )

    assert len(service.responses) == 1
    assert service.responses[0]["question"] == "поздоровайся со всеми"  # noqa: RUF001
    assert service.responses[0]["include_private_context"] is False
    assert service.responses[0]["allow_tools"] is False
    assert api.sent[-1] == (-102, "Ответ Persona", 21)


@pytest.mark.asyncio
async def test_owner_private_turn_enables_tools_and_has_stable_correlation_id() -> None:
    worker, _api, service = _worker(FakeRepository(TelegramBinding(1, 42)))

    await worker.handle_update(_private(1, "use a tool", 55))

    assert len(service.responses) == 1
    assert service.responses[0]["is_owner"] is True
    assert service.responses[0]["include_private_context"] is True
    assert service.responses[0]["allow_tools"] is True
    assert service.responses[0]["correlation_id"] == "telegram-update:55"
    assert "tg_user_id=1" in service.responses[0]["sender_label"]
    assert "OWNER" in service.responses[0]["sender_label"]


@pytest.mark.asyncio
async def test_owner_private_chat_skips_expensive_tool_prompt() -> None:
    worker, _api, service = _worker(FakeRepository(TelegramBinding(1, 42)))

    await worker.handle_update(_private(1, "Обосри Клода пожёстче", 56))

    assert len(service.responses) == 1
    assert service.responses[0]["include_private_context"] is True
    assert service.responses[0]["allow_tools"] is False


@pytest.mark.asyncio
async def test_worker_picks_up_a_rebound_owner_without_restart() -> None:
    """The worker caches ``self._binding`` and runs as a separate process
    from the web settings page, so a rebind must be noticed without a
    restart. Simulate the settings-page reassignment updating the stored
    binding directly (as ``rebind_owner_and_sync_person`` does) while the
    worker keeps running, and confirm authority moves on the very next
    message -- old owner denied, new owner accepted."""
    repository = FakeRepository(TelegramBinding(100, 42))
    worker, _api, service = _worker(repository)

    await worker.handle_update(_private(100, "hi", 1))
    assert len(service.responses) == 1
    assert service.responses[-1]["is_owner"] is True

    # Simulate the DB-level rebind the settings page performs; the worker
    # never restarts and never re-reads this itself except on demand.
    repository.binding = TelegramBinding(555, 42)

    await worker.handle_update(_private(555, "hi", 2))
    assert len(service.responses) == 2
    assert service.responses[-1]["is_owner"] is True

    # The old account is a private chat sender that is no longer the
    # owner, so it must be silently denied (private chats require is_owner).
    await worker.handle_update(_private(100, "hi", 3))
    assert len(service.responses) == 2


@pytest.mark.asyncio
async def test_allowed_group_passive_message_is_stored_without_reply() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-100)
    worker, api, service = _worker(repository)

    await worker.handle_update(_group(2, -100, "обычное сообщение"))

    assert len(service.passive) == 1
    assert service.responses == []
    assert api.sent == []


@pytest.mark.asyncio
async def test_allowed_group_can_reply_ambiently_without_mention() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-100)
    worker, api, service = _worker(repository)
    service.ambient_answer = "Ambient Persona reply"

    await worker.handle_update(
        _group(2, -100, "кто-нибудь знает, как решить эту ошибку?", 77)
    )

    assert len(service.passive) == 1
    ambient = service.passive[0]
    assert ambient["update_id"] == 77
    assert ambient["message_id"] == 77
    assert "include_private_context" not in ambient
    assert "allow_tools" not in ambient
    assert service.responses == []
    assert api.sent == [(-100, "Ambient Persona reply", 77)]


@pytest.mark.asyncio
async def test_owner_can_teach_group_rule_with_natural_persona_alias() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-100)
    worker, api, service = _worker(repository)

    await worker.handle_update(
        _group(1, -100, "Персоныч, не отвечай когда обращаются к Инди", 81)
    )

    assert repository.behavior_rules[-100] == [
        "не отвечай когда обращаются к Инди"
    ]
    assert len(service.passive) == 1
    assert "Запомнила правило" in api.sent[0][1]
    assert service.responses == []


@pytest.mark.asyncio
async def test_other_bot_group_messages_are_analyzed_but_own_are_ignored() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-100)
    worker, api, service = _worker(repository)
    update = _group(2, -100, "bot noise", 78)
    update["message"]["from"]["is_bot"] = True

    await worker.handle_update(update)

    assert len(service.passive) == 1
    assert service.responses == []
    assert api.sent == []

    own = _group(777, -100, "own bot message", 79)
    own["message"]["from"]["is_bot"] = True
    await worker.handle_update(own)
    assert len(service.passive) == 1


@pytest.mark.asyncio
async def test_repository_hashes_pairing_and_persists_access(db: Any) -> None:
    repository = TelegramRepository()
    code = await repository.create_pairing_code()
    assert await repository.verify_pairing_code(code)
    assert not await repository.verify_pairing_code("wrong")

    assert await repository.get_binding() is None
    await repository.bind_owner(123, 9)
    assert await repository.get_binding() == TelegramBinding(123, 9)

    await repository.set_chat_allowed(-55, True)
    assert await repository.allowed_chat_ids() == {-55}
    await repository.set_chat_allowed(-55, False)
    assert await repository.allowed_chat_ids() == set()

    await repository.save_session_id(-55, 77)
    assert await repository.session_id(-55) == 77
    await repository.clear_session_id(-55)
    assert await repository.session_id(-55) is None


def test_telegram_message_split_respects_limit() -> None:
    chunks = _split_message(("слово " * 2000).strip(), limit=100)
    assert len(chunks) > 1
    assert all(0 < len(chunk) <= 100 for chunk in chunks)


def test_pair_launcher_prints_code_then_continues_to_worker() -> None:
    script = (
        Path(__file__).resolve().parents[1] / "ops" / "persona_telegram_worker.ps1"
    ).read_text(encoding="utf-8")
    pairing = script.index("--pairing-code-only")
    worker_loop = script.index("while ($true)")
    assert pairing < worker_loop
    assert "PERSONA_TG_BOT_TOKEN" not in script


@pytest.mark.asyncio
async def test_telegram_worker_lease_is_singleton_and_recoverable(db) -> None:
    repository = TelegramRepository()

    assert (
        await repository.acquire_worker_lease("worker-a", lease_seconds=600) is True
    )
    assert (
        await repository.acquire_worker_lease("worker-a", lease_seconds=600) is True
    )
    assert (
        await repository.acquire_worker_lease("worker-b", lease_seconds=600) is False
    )

    await repository.release_worker_lease("worker-a")
    assert await repository.acquire_worker_lease("worker-b") is True


@pytest.mark.asyncio
async def test_update_inbox_suppresses_db_llm_replay_and_checks_live_lease(db) -> None:
    repository = TelegramRepository()
    assert await repository.acquire_worker_lease("worker-a", lease_seconds=600)
    assert await repository.claim_update(501, "worker-a", lease_seconds=600)
    assert not await repository.claim_update(501, "worker-a", lease_seconds=600)
    assert not await repository.acquire_worker_lease("worker-b", lease_seconds=600)
    assert await repository.renew_processing_lease(
        501,
        "worker-a",
        lease_seconds=600,
    )
    assert await repository.finish_update(
        501,
        "worker-a",
        status="processed",
        outcome="handled",
    )
    assert not await repository.claim_update(501, "worker-a", lease_seconds=600)
    assert await repository.save_update_offset_if_leased(502, "worker-a")
    assert await repository.update_offset() == 502

    row = await (
        await db.execute(
            "SELECT status, outcome, holder_id, lease_until "
            "FROM telegram_update_inbox WHERE update_id=501"
        )
    ).fetchone()
    assert tuple(row) == ("processed", "handled", None, None)

    await repository.release_worker_lease("worker-a")
    assert await repository.acquire_worker_lease("worker-b", lease_seconds=600)
    assert not await repository.save_update_offset_if_leased(999, "worker-a")
    assert await repository.update_offset() == 502


@pytest.mark.asyncio
async def test_processing_heartbeat_cancels_llm_when_lease_is_lost() -> None:
    class _SlowService(FakeService):
        async def respond(self, **kwargs: Any) -> str:
            self.responses.append(kwargs)
            await asyncio.sleep(1)
            return "late"

    repository = FakeRepository(TelegramBinding(1, 42))
    repository.renew_processing = False
    api = FakeAPI()
    service = _SlowService()
    worker = TelegramWorker(
        TelegramConfig(bot_token="not-a-real-token"),
        api=api,  # type: ignore[arg-type]
        repository=repository,  # type: ignore[arg-type]
        service=service,  # type: ignore[arg-type]
    )
    worker._bot_id = 777
    worker._bot_username = "PersonaTestBot"
    worker._persona_owner_id = 42
    worker._binding = repository.binding
    worker._processing_heartbeat_seconds = 0.01

    with pytest.raises(TelegramConsumerLeaseLost):
        await worker._process_update_with_lease(700, _private(1, "долго", 700))

    assert len(service.responses) == 1
    assert api.sent == []
    assert repository.finished_updates == []


@pytest.mark.asyncio
async def test_worker_processes_each_update_id_only_once() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    worker, api, service = _worker(repository)
    update = _private(1, "один раз", 701)

    await worker._process_update_with_lease(701, update)
    await worker._process_update_with_lease(701, update)

    assert len(service.responses) == 1
    assert len(api.sent) == 1


class FakeIgnoringPeople:
    """People repository where one specific sender is muted."""

    def __init__(self, ignored_id: int) -> None:
        self.ignored_id = ignored_id
        self.observed: list[int] = []

    async def observe_message(self, **kwargs: Any) -> Any:
        sender_id = int(kwargs["sender"]["id"])
        self.observed.append(sender_id)
        return SimpleNamespace(
            telegram_user_id=sender_id, stable_label=f"User {sender_id}"
        )

    async def identity_context(self, **kwargs: Any) -> str:
        return ""

    async def is_ignored(self, persona_user_id: int, telegram_user_id: int) -> bool:
        return int(telegram_user_id) == self.ignored_id


class FakeFailingPeople:
    """People repository whose observation always fails."""

    async def observe_message(self, **kwargs: Any) -> Any:
        raise RuntimeError("boom")

    async def identity_context(self, **kwargs: Any) -> str:
        return ""

    async def is_ignored(self, persona_user_id: int, telegram_user_id: int) -> bool:
        raise AssertionError("is_ignored must not be called without a person")


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


@pytest.mark.asyncio
async def test_unmuted_person_in_same_chat_still_gets_a_reply() -> None:
    repository = FakeRepository(
        TelegramBinding(telegram_user_id=1, persona_user_id=42)
    )
    worker, api, service = _worker(repository)
    people = FakeIgnoringPeople(ignored_id=999)
    worker.people = people  # type: ignore[assignment]

    await worker.handle_update(_private(1, "ответь мне что-нибудь", message_id=32))

    assert people.observed == [1]
    assert len(service.responses) == 1
    assert api.sent != []


@pytest.mark.asyncio
async def test_muted_person_in_group_is_recorded_but_not_answered() -> None:
    repository = FakeRepository(TelegramBinding(1, 42))
    repository.groups.add(-200)
    worker, api, service = _worker(repository)
    people = FakeIgnoringPeople(ignored_id=2)
    worker.people = people  # type: ignore[assignment]

    await worker.handle_update(
        _group(2, -200, "@PersonaTestBot ответь мне что-нибудь", 33)
    )

    assert people.observed == [2]
    assert service.responses == []
    assert api.sent == []


@pytest.mark.asyncio
async def test_failed_observation_does_not_raise() -> None:
    repository = FakeRepository(
        TelegramBinding(telegram_user_id=1, persona_user_id=42)
    )
    worker, api, service = _worker(repository)
    worker.people = FakeFailingPeople()  # type: ignore[assignment]

    await worker.handle_update(_private(1, "привет", message_id=34))

    assert len(service.responses) == 1
    assert api.sent != []
