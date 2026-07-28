"""Long-poll worker and access policy for the Telegram channel."""

from __future__ import annotations

import asyncio
import re
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from app.auth.owner import get_owner_user_id
from app.integrations.telegram.api import TelegramAPIError, TelegramBotAPI
from app.integrations.telegram.repository import (
    TelegramBinding,
    TelegramRepository,
)
from app.integrations.telegram.service import PersonaTelegramService
from app.logging_setup import get_logger

if TYPE_CHECKING:
    from app.integrations.telegram.config import TelegramConfig

log = get_logger("persona.telegram.worker")

_GROUP_TYPES = {"group", "supergroup"}
_HELP = (
    "Persona подключена к твоему аккаунту.\n\n"
    "Личные сообщения: просто напиши вопрос.\n"
    "Группа: /allow_here — разрешить этот чат, /deny_here — закрыть.\n"
    "/new — начать новую ветку Persona для текущего чата.\n"
    "В группе позови @бота, ответь на его сообщение или напиши /persona вопрос."
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    sender_id: int
    chat_id: int
    message_id: int
    text: str
    chat_type: str
    sender: dict[str, Any]
    chat: dict[str, Any]
    raw: dict[str, Any]
    command: str
    argument: str

    @property
    def is_group(self) -> bool:
        return self.chat_type in _GROUP_TYPES


class TelegramWorker:
    def __init__(
        self,
        config: TelegramConfig,
        *,
        api: TelegramBotAPI | None = None,
        repository: TelegramRepository | None = None,
        service: PersonaTelegramService | None = None,
    ) -> None:
        self.config = config
        self.api = api or TelegramBotAPI(config.bot_token)
        self.repository = repository or TelegramRepository()
        self.service = service or PersonaTelegramService(self.repository)
        self._stop = asyncio.Event()
        self._bot_id = 0
        self._bot_username = ""
        self._persona_owner_id = 0
        self._binding: TelegramBinding | None = None

    async def prepare(self) -> None:
        self.config.require_token()
        owner_id = await get_owner_user_id()
        if owner_id is None:
            raise RuntimeError(
                "Persona owner account is not configured. Finish /setup on the "
                "site before starting the Telegram worker."
            )
        self._persona_owner_id = int(owner_id)
        self._binding = await self.repository.get_binding()
        configured = self.config.owner_telegram_user_id
        if self._binding is not None:
            if self._binding.persona_user_id != self._persona_owner_id:
                raise RuntimeError(
                    "Telegram binding points to a different Persona owner; refusing to start."
                )
            if configured is not None and (self._binding.telegram_user_id != configured):
                raise RuntimeError("PERSONA_TG_OWNER_USER_ID conflicts with the stored binding.")
        elif configured is not None:
            self._binding = await self.repository.bind_owner(configured, self._persona_owner_id)
        me = await self.api.get_me()
        self._bot_id = int(me.get("id") or 0)
        self._bot_username = str(me.get("username") or "")
        if not self._bot_id:
            raise RuntimeError("Telegram getMe did not return a valid bot id.")

    async def run(self) -> None:
        await self.prepare()
        offset = await self.repository.update_offset()
        log.info(
            "telegram.worker.started",
            owner_bound=self._binding is not None,
            groups=len(await self._allowed_groups()),
        )
        backoff = 1.0
        while not self._stop.is_set():
            try:
                updates = await self.api.get_updates(offset, self.config.poll_timeout_seconds)
                backoff = 1.0
                for update in updates:
                    update_id = _int(update.get("update_id"))
                    if update_id is None:
                        continue
                    await self.handle_update(update)
                    offset = max(offset, update_id + 1)
                    await self.repository.save_update_offset(offset)
            except TelegramAPIError as exc:
                log.warning(
                    "telegram.poll.failed",
                    reason=str(exc),
                    retry_seconds=backoff,
                )
                with suppress(TimeoutError):
                    await asyncio.wait_for(self._stop.wait(), timeout=backoff)
                backoff = min(30.0, backoff * 2)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                log.exception("telegram.update.failed", error_type=type(exc).__name__)
                await asyncio.sleep(1.0)
        log.info("telegram.worker.stopped")

    def stop(self) -> None:
        self._stop.set()

    async def handle_update(  # noqa: PLR0911 - explicit fail-closed policy exits
        self, update: dict[str, Any]
    ) -> None:
        incoming = _incoming_message(update)
        if incoming is None:
            return

        if incoming.command == "claim":
            await self._claim(
                incoming.sender_id,
                incoming.chat_id,
                incoming.chat_type,
                incoming.message_id,
                incoming.argument,
            )
            return

        binding = self._binding or await self.repository.get_binding()
        if binding is None:
            return
        self._binding = binding
        is_owner = incoming.sender_id == binding.telegram_user_id

        if await self._handle_access_command(incoming, is_owner):
            return
        if not await self._is_authorized(incoming, is_owner):
            return

        if await self._handle_owner_command(incoming, is_owner):
            return

        addressed, clean_text = self._addressed(
            incoming.raw,
            incoming.text,
            incoming.command,
            incoming.argument,
        )
        sender_label = _sender_label(incoming.sender)
        chat_title = _chat_title(incoming.chat)
        if incoming.is_group and not addressed:
            await self.service.record_passive_group_message(
                persona_user_id=binding.persona_user_id,
                telegram_chat_id=incoming.chat_id,
                text=incoming.text,
                chat_title=chat_title,
                sender_label=sender_label,
            )
            return
        if not clean_text:
            return
        with suppress(TelegramAPIError):
            await self.api.send_typing(incoming.chat_id)
        try:
            answer = await self.service.respond(
                persona_user_id=binding.persona_user_id,
                telegram_chat_id=incoming.chat_id,
                question=clean_text,
                chat_title=chat_title,
                sender_label=sender_label if incoming.is_group else None,
                # A non-owner member of an allowlisted group may talk to the
                # bot, but can never retrieve the owner's profile, cross-chat
                # memory or activity.  The owner deliberately addressing the
                # bot retains the full Persona context.
                include_private_context=is_owner,
            )
        except Exception as exc:
            log.warning(
                "telegram.response.failed",
                error_type=type(exc).__name__,
                chat_kind=incoming.chat_type,
            )
            if is_owner:
                await self.api.send_message(
                    incoming.chat_id,
                    "Persona сейчас не смогла ответить. Проверь, что LLM worker "
                    "или выбранный провайдер запущен.",
                    reply_to_message_id=incoming.message_id,
                )
            return
        await self.api.send_message(
            incoming.chat_id,
            answer,
            reply_to_message_id=incoming.message_id,
        )

    async def _handle_access_command(self, incoming: IncomingMessage, is_owner: bool) -> bool:
        if incoming.command not in {"allow_here", "deny_here"}:
            return False
        if not is_owner or not incoming.is_group:
            return True
        allowed = incoming.command == "allow_here"
        await self.repository.set_chat_allowed(incoming.chat_id, allowed)
        text = (
            "Этот чат разрешён. Persona будет запоминать сообщения, которые Telegram передаёт боту."
            if allowed
            else "Доступ этого чата закрыт."
        )
        await self.api.send_message(incoming.chat_id, text, reply_to_message_id=incoming.message_id)
        return True

    async def _is_authorized(self, incoming: IncomingMessage, is_owner: bool) -> bool:
        if incoming.is_group:
            return incoming.chat_id in await self._allowed_groups()
        return incoming.chat_type == "private" and is_owner

    async def _handle_owner_command(self, incoming: IncomingMessage, is_owner: bool) -> bool:
        if incoming.command in {"help", "start"}:
            if is_owner:
                await self.api.send_message(
                    incoming.chat_id,
                    _HELP,
                    reply_to_message_id=incoming.message_id,
                )
            return True
        if incoming.command != "new":
            return False
        if is_owner:
            await self.service.reset_chat(incoming.chat_id)
            await self.api.send_message(
                incoming.chat_id,
                "Новая ветка создана. Общая память Persona сохранена.",
                reply_to_message_id=incoming.message_id,
            )
        return True

    async def _claim(
        self,
        sender_id: int,
        chat_id: int,
        chat_type: str,
        message_id: int,
        candidate: str,
    ) -> None:
        if chat_type != "private" or not candidate:
            return
        if self._binding is not None or await self.repository.get_binding():
            return
        if not await self.repository.verify_pairing_code(candidate, self.config.pairing_secret):
            return
        self._binding = await self.repository.bind_owner(sender_id, self._persona_owner_id)
        await self.api.send_message(
            chat_id,
            "Готово: этот Telegram привязан к аккаунту владельца Persona. "
            "Остальным личным чатам бот отвечать не будет.",
            reply_to_message_id=message_id,
        )

    async def _allowed_groups(self) -> set[int]:
        stored = await self.repository.allowed_chat_ids()
        return stored | set(self.config.allowed_chat_ids)

    def _addressed(
        self,
        message: dict[str, Any],
        text: str,
        command: str,
        argument: str,
    ) -> tuple[bool, str]:
        if command in {"persona", "ask"}:
            return True, argument
        reply = message.get("reply_to_message")
        if isinstance(reply, dict):
            author = reply.get("from")
            if isinstance(author, dict) and _int(author.get("id")) == self._bot_id:
                return True, text
        if self._bot_username:
            mention = re.compile(rf"@{re.escape(self._bot_username)}\b", re.IGNORECASE)
            if mention.search(text):
                return True, mention.sub("", text).strip()
        return False, text


def _incoming_message(update: dict[str, Any]) -> IncomingMessage | None:
    message = update.get("message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict) or bool(sender.get("is_bot")):
        return None
    sender_id = _int(sender.get("id"))
    chat_id = _int(chat.get("id"))
    message_id = _int(message.get("message_id"))
    text = str(message.get("text") or message.get("caption") or "").strip()
    if sender_id is None or chat_id is None or message_id is None or not text:
        return None
    command, argument = _command(text)
    return IncomingMessage(
        sender_id=sender_id,
        chat_id=chat_id,
        message_id=message_id,
        text=text,
        chat_type=str(chat.get("type") or ""),
        sender=sender,
        chat=chat,
        raw=message,
        command=command,
        argument=argument,
    )


def _command(text: str) -> tuple[str, str]:
    match = re.match(r"^/([A-Za-z_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", text, re.S)
    if not match:
        return "", text
    return match.group(1).casefold(), (match.group(2) or "").strip()


def _int(value: object) -> int | None:
    try:
        return int(value)  # type: ignore[arg-type]
    except (TypeError, ValueError):
        return None


def _sender_label(sender: dict[str, Any]) -> str:
    parts = [
        str(sender.get("first_name") or "").strip(),
        str(sender.get("last_name") or "").strip(),
    ]
    name = " ".join(part for part in parts if part)
    username = str(sender.get("username") or "").strip()
    return name or (f"@{username}" if username else "участник")


def _chat_title(chat: dict[str, Any]) -> str:
    title = str(chat.get("title") or "").strip()
    if title:
        return title
    return _sender_label(chat)


__all__ = ["TelegramWorker"]
