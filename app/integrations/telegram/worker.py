"""Long-poll worker and access policy for the Telegram channel."""

# ruff: noqa: RUF001

from __future__ import annotations

import asyncio
import os
import re
import secrets
import socket
import time
from contextlib import suppress
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

import psutil

from app.auth.owner import get_owner_user_id
from app.integrations.telegram.actions import (
    TelegramActionPlan,
    immediate_reaction,
    multiple_reactions_requested,
    plan_telegram_actions,
    resolve_media_reference,
)
from app.integrations.telegram.api import TelegramAPIError, TelegramBotAPI
from app.integrations.telegram.media import (
    TelegramAttachment,
    attachments_from_message,
    build_media_context,
    non_file_content_summary,
)
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
_CONSUMER_LEASE_SECONDS = 600
_PROCESSING_LEASE_HEARTBEAT_SECONDS = 20.0
_BOT_COMMANDS = [
    {"command": "start", "description": "Подключить Persona"},
    {"command": "help", "description": "Показать справку"},
    {"command": "new", "description": "Начать новую ветку"},
    {"command": "persona", "description": "Обратиться к Persona в группе"},
    {"command": "allow_here", "description": "Разрешить текущую группу"},
    {"command": "deny_here", "description": "Запретить текущую группу"},
]
_HELP = (
    "Persona подключена к твоему аккаунту.\n\n"
    "Личные сообщения: просто напиши вопрос.\n"
    "Группа: /allow_here — разрешить этот чат, /deny_here — закрыть. "
    "После разрешения Persona видит каждое доставленное обычное сообщение и "
    "сама решает, когда полезно ответить.\n"
    "/new — начать новую ветку Persona для текущего чата.\n"
    "В группе позови @бота, ответь на его сообщение или напиши /persona вопрос "
    "для немедленного ответа.\n"
    "Persona понимает фото, текстовые файлы и, если доступна локальная "
    "расшифровка, голосовые. Она может ставить реакции. В личке владельца "
    "можно обычной фразой попросить отправить медиа/стикер/опрос/локацию, "
    "бросить кубик, а также изменить или удалить её последнее сообщение.\n\n"
    "Важно: чтобы Telegram доставлял боту ВСЕ сообщения группы, открой "
    "@BotFather → /setprivacy → выбери бота → Disable, затем удали и снова "
    "добавь бота в группу. При включённом Privacy Mode бот получает только "
    "команды, упоминания и ответы на свои сообщения."
)


@dataclass(frozen=True, slots=True)
class IncomingMessage:
    update_id: int
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
    attachments: tuple[TelegramAttachment, ...] = ()

    @property
    def is_group(self) -> bool:
        return self.chat_type in _GROUP_TYPES


class TelegramConsumerLeaseLost(RuntimeError):
    """The singleton consumer can no longer safely finish this update."""


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
        self._lease_holder = (
            f"{socket.gethostname()}:{os.getpid()}:{secrets.token_hex(8)}"
        )
        self._consumer_lease_seconds = _CONSUMER_LEASE_SECONDS
        self._processing_heartbeat_seconds = _PROCESSING_LEASE_HEARTBEAT_SECONDS
        self._last_reaction_at: dict[int, float] = {}

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
        try:
            await self.api.set_my_commands(_BOT_COMMANDS)
        except Exception as exc:  # best-effort metadata must never stop polling
            log.warning(
                "telegram.commands.prepare_failed",
                error_type=type(exc).__name__,
            )

    async def run(self) -> None:
        await self.prepare()
        offset = await self.repository.update_offset()
        log.info(
            "telegram.worker.started",
            owner_bound=self._binding is not None,
            groups=len(await self._allowed_groups()),
        )
        backoff = 1.0
        waiting_for_lease = False
        try:
            while not self._stop.is_set():
                if not await self.repository.acquire_worker_lease(
                    self._lease_holder,
                    lease_seconds=self._consumer_lease_seconds,
                ):
                    if await self._reclaim_dead_local_worker_lease():
                        continue
                    if not waiting_for_lease:
                        log.warning("telegram.worker.lease_held_elsewhere")
                        waiting_for_lease = True
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=5.0)
                    continue
                if waiting_for_lease:
                    log.info("telegram.worker.lease_acquired")
                    waiting_for_lease = False
                # A process that waited behind the previous consumer started
                # with a stale in-memory cursor. Refresh it under the lease;
                # the durable inbox remains the final replay guard.
                offset = max(offset, await self.repository.update_offset())
                try:
                    offset = await self._poll_and_process(offset)
                    backoff = 1.0
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
                except TelegramConsumerLeaseLost:
                    waiting_for_lease = True
                    log.warning("telegram.worker.lease_lost")
                    with suppress(TimeoutError):
                        await asyncio.wait_for(self._stop.wait(), timeout=1.0)
                except Exception as exc:
                    log.exception("telegram.update.failed", error_type=type(exc).__name__)
                    await asyncio.sleep(1.0)
        finally:
            await self.repository.release_worker_lease(self._lease_holder)
            log.info("telegram.worker.stopped")

    def stop(self) -> None:
        self._stop.set()

    async def _reclaim_dead_local_worker_lease(self) -> bool:
        """Release a lease whose same-host process no longer exists.

        Hard watchdog restarts cannot run the old process' ``finally`` block.
        The random holder suffix and exact conditional delete preserve the
        singleton guarantee, while the hostname/PID checks prevent stealing a
        live lease from another process or host.
        """

        getter = getattr(self.repository, "worker_lease_holder", None)
        releaser = getattr(self.repository, "release_worker_lease", None)
        if not callable(getter) or not callable(releaser):
            return False
        holder = await getter()
        if not holder or holder == self._lease_holder:
            return False
        parts = holder.split(":", 2)
        if len(parts) != 3 or parts[0].casefold() != socket.gethostname().casefold():
            return False
        try:
            pid = int(parts[1])
        except ValueError:
            return False
        if pid <= 0 or pid == os.getpid() or psutil.pid_exists(pid):
            return False
        await releaser(holder)
        log.warning("telegram.worker.orphaned_lease_reclaimed", stale_pid=pid)
        return True

    async def _poll_and_process(self, offset: int) -> int:
        updates = await self.api.get_updates(offset, self.config.poll_timeout_seconds)
        # A long or suspended poll must not process updates after another
        # process legitimately took an expired lease.
        if not await self.repository.acquire_worker_lease(
            self._lease_holder,
            lease_seconds=self._consumer_lease_seconds,
        ):
            raise TelegramConsumerLeaseLost("consumer lease lost during long poll")
        for update in updates:
            update_id = _int(update.get("update_id"))
            if update_id is None:
                continue
            await self._process_update_with_lease(update_id, update)
            next_offset = max(offset, update_id + 1)
            if not await self.repository.save_update_offset_if_leased(
                next_offset,
                self._lease_holder,
            ):
                raise TelegramConsumerLeaseLost(
                    "consumer lease lost before offset commit"
                )
            offset = next_offset
        return offset

    async def _process_update_with_lease(
        self,
        update_id: int,
        update: dict[str, Any],
    ) -> None:
        """Handle one never-before-seen update under a renewable lease.

        Existing inbox rows are deliberately skipped even if their former
        process crashed. Once DB/LLM side effects may have started, replay is
        more dangerous than dropping an ambiguous update.
        """
        claimed = await self.repository.claim_update(
            update_id,
            self._lease_holder,
            lease_seconds=self._consumer_lease_seconds,
        )
        if not claimed:
            return

        processing = asyncio.create_task(
            self.handle_update(update),
            name=f"telegram-update-{update_id}",
        )
        heartbeat = asyncio.create_task(
            self._guard_processing_lease(update_id),
            name=f"telegram-update-lease-{update_id}",
        )
        try:
            done, _pending = await asyncio.wait(
                (processing, heartbeat),
                return_when=asyncio.FIRST_COMPLETED,
            )
            if heartbeat in done:
                try:
                    await heartbeat
                finally:
                    processing.cancel()
                    await asyncio.gather(processing, return_exceptions=True)
                raise TelegramConsumerLeaseLost(
                    "processing lease guard stopped unexpectedly"
                )

            try:
                await processing
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                await self.repository.finish_update(
                    update_id,
                    self._lease_holder,
                    status="failed",
                    outcome=type(exc).__name__,
                )
                raise

            if not await self.repository.finish_update(
                update_id,
                self._lease_holder,
                status="processed",
                outcome="handled",
            ):
                raise TelegramConsumerLeaseLost(
                    "consumer lease lost before update completion"
                )
        finally:
            if not heartbeat.done():
                heartbeat.cancel()
            await asyncio.gather(heartbeat, return_exceptions=True)
            if not processing.done():
                processing.cancel()
                await asyncio.gather(processing, return_exceptions=True)

    async def _guard_processing_lease(self, update_id: int) -> None:
        while True:
            await asyncio.sleep(self._processing_heartbeat_seconds)
            try:
                renewed = await self.repository.renew_processing_lease(
                    update_id,
                    self._lease_holder,
                    lease_seconds=self._consumer_lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                raise TelegramConsumerLeaseLost(
                    "processing lease renewal failed"
                ) from exc
            if not renewed:
                raise TelegramConsumerLeaseLost("processing lease was lost")

    async def handle_update(  # noqa: PLR0911,PLR0912 - explicit policy exits
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
        instant_reaction = immediate_reaction(incoming.text)
        if instant_reaction is not None:
            await self._set_reaction(incoming, instant_reaction, force=True)
            if multiple_reactions_requested(incoming.text):
                await self._send_text(
                    incoming.chat_id,
                    "Поставил одну 👍. Telegram разрешает ботам установить "
                    "только одну обычную реакцию на сообщение.",
                    reply_to_message_id=incoming.message_id,
                )
            return

        addressed, clean_text = self._addressed(
            incoming.raw,
            incoming.text,
            incoming.command,
            incoming.argument,
        )
        media_context = await build_media_context(self.api, incoming.attachments)
        enriched_text = incoming.text
        if media_context.text_suffix:
            enriched_text = f"{enriched_text}\n{media_context.text_suffix}".strip()
            clean_text = f"{clean_text}\n{media_context.text_suffix}".strip()
        sender_label = _sender_label(incoming.sender)
        chat_title = _chat_title(incoming.chat)
        if incoming.is_group and not addressed:
            try:
                ambient_answer = await self.service.handle_ambient_group_message(
                    persona_user_id=binding.persona_user_id,
                    telegram_chat_id=incoming.chat_id,
                    update_id=incoming.update_id,
                    message_id=incoming.message_id,
                    text=enriched_text,
                    chat_title=chat_title,
                    sender_label=sender_label,
                    image_data_url=media_context.image_data_url,
                )
            except Exception as exc:
                log.warning(
                    "telegram.ambient.failed_silent",
                    error_type=type(exc).__name__,
                )
                return
            if ambient_answer:
                await self._deliver_answer(
                    incoming,
                    ambient_answer,
                    is_owner_private=False,
                )
            else:
                await self._deliver_reaction_only(incoming)
            return
        if not clean_text:
            return
        typing = asyncio.create_task(
            self._typing_heartbeat(incoming.chat_id),
            name=f"telegram-typing-{incoming.chat_id}",
        )
        try:
            private_owner = is_owner and not incoming.is_group
            answer = await self.service.respond(
                persona_user_id=binding.persona_user_id,
                telegram_chat_id=incoming.chat_id,
                question=clean_text,
                image_data_url=media_context.image_data_url,
                chat_title=chat_title,
                sender_label=sender_label if incoming.is_group else None,
                # Every group turn is fail-closed: even the owner must move to
                # the private DM before Persona exposes private recall or
                # executes a side-effecting tool.
                is_owner=is_owner,
                include_private_context=private_owner,
                allow_tools=private_owner,
                correlation_id=f"telegram-update:{incoming.update_id}",
            )
        except Exception as exc:
            log.warning(
                "telegram.response.failed",
                error_type=type(exc).__name__,
                chat_kind=incoming.chat_type,
            )
            if is_owner:
                await self._send_text(
                    incoming.chat_id,
                    "Persona сейчас не смогла ответить. Проверь, что LLM worker "
                    "или выбранный провайдер запущен.",
                    reply_to_message_id=incoming.message_id,
                )
            return
        finally:
            typing.cancel()
            await asyncio.gather(typing, return_exceptions=True)
        await self._deliver_answer(
            incoming,
            answer,
            is_owner_private=is_owner and not incoming.is_group,
        )

    async def _deliver_reaction_only(self, incoming: IncomingMessage) -> None:
        plan = await plan_telegram_actions(
            message_text=incoming.text,
            answer="",
            attachments=incoming.attachments,
            is_owner_private=False,
        )
        await self._set_reaction(incoming, plan.reaction)

    async def _deliver_answer(
        self,
        incoming: IncomingMessage,
        answer: str,
        *,
        is_owner_private: bool,
    ) -> None:
        plan = await plan_telegram_actions(
            message_text=incoming.text,
            answer=answer,
            attachments=incoming.attachments,
            is_owner_private=is_owner_private,
        )
        await self._set_reaction(incoming, plan.reaction)
        try:
            sent = await self._execute_plan(incoming, answer, plan)
        except (TelegramAPIError, ValueError):
            sent = False
        if not sent:
            await self._send_text(
                incoming.chat_id,
                answer,
                reply_to_message_id=incoming.message_id,
            )

    async def _execute_plan(  # noqa: PLR0911,PLR0912 - one action dispatcher
        self,
        incoming: IncomingMessage,
        answer: str,
        plan: TelegramActionPlan,
    ) -> bool:
        kind = plan.kind
        if kind == "text":
            return False
        if kind == "none":
            return True
        if kind in {
            "photo",
            "document",
            "audio",
            "video",
            "animation",
            "voice",
            "sticker",
        }:
            reference = resolve_media_reference(
                plan.media_ref or "",
                incoming.attachments,
            )
            if reference is None:
                return False
            message_id = await self.api.send_media(
                kind,
                incoming.chat_id,
                reference,
                caption=answer,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if kind == "dice":
            message_id = await self.api.send_dice(
                incoming.chat_id,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if kind == "poll" and plan.poll_question is not None:
            message_id = await self.api.send_poll(
                incoming.chat_id,
                plan.poll_question,
                plan.poll_options,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if (
            kind == "location"
            and plan.latitude is not None
            and plan.longitude is not None
        ):
            message_id = await self.api.send_location(
                incoming.chat_id,
                plan.latitude,
                plan.longitude,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if (
            kind == "contact"
            and plan.phone_number is not None
            and plan.first_name is not None
        ):
            message_id = await self.api.send_contact(
                incoming.chat_id,
                plan.phone_number,
                plan.first_name,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if kind == "copy_current":
            message_id = await self.api.copy_message(
                incoming.chat_id,
                incoming.chat_id,
                incoming.message_id,
                reply_to_message_id=incoming.message_id,
            )
            await self._record_last_message(incoming.chat_id, message_id)
            return True
        if kind in {"edit_last", "delete_last"}:
            last_message = await self._last_bot_message(incoming.chat_id)
            if last_message is None:
                return False
            if kind == "edit_last" and plan.text is not None:
                await self.api.edit_message_text(
                    incoming.chat_id,
                    last_message,
                    plan.text,
                )
            elif kind == "delete_last":
                await self.api.delete_message(incoming.chat_id, last_message)
                await self._clear_last_message(incoming.chat_id)
            else:
                return False
            await self._send_text(
                incoming.chat_id,
                answer,
                reply_to_message_id=incoming.message_id,
            )
            return True
        return False

    async def _set_reaction(
        self,
        incoming: IncomingMessage,
        reaction: str | None,
        *,
        force: bool = False,
    ) -> None:
        if not reaction:
            return
        now = time.monotonic()
        previous = self._last_reaction_at.get(incoming.chat_id)
        if not force and previous is not None and now - previous < 10.0:
            return
        try:
            await self.api.set_message_reaction(
                incoming.chat_id,
                incoming.message_id,
                reaction,
            )
        except (AttributeError, TelegramAPIError):
            return
        self._last_reaction_at[incoming.chat_id] = now

    async def _typing_heartbeat(self, chat_id: int) -> None:
        """Keep Telegram's short-lived typing indicator alive during slow turns."""

        while True:
            with suppress(AttributeError, TelegramAPIError):
                await self.api.send_typing(chat_id)
            await asyncio.sleep(4.0)

    async def _send_text(
        self,
        chat_id: int,
        text: str,
        *,
        reply_to_message_id: int | None = None,
    ) -> None:
        sent = await self.api.send_message(
            chat_id,
            text,
            reply_to_message_id=reply_to_message_id,
        )
        if isinstance(sent, tuple) and sent:
            await self._record_last_message(chat_id, sent[-1])

    async def _record_last_message(
        self,
        chat_id: int,
        message_id: int | None,
    ) -> None:
        if message_id is None:
            return
        save = getattr(self.repository, "save_last_bot_message", None)
        if callable(save):
            await save(chat_id, message_id)

    async def _last_bot_message(self, chat_id: int) -> int | None:
        getter = getattr(self.repository, "last_bot_message_id", None)
        if not callable(getter):
            return None
        value = await getter(chat_id)
        return int(value) if value is not None else None

    async def _clear_last_message(self, chat_id: int) -> None:
        clear = getattr(self.repository, "clear_last_bot_message", None)
        if callable(clear):
            await clear(chat_id)

    async def _handle_access_command(self, incoming: IncomingMessage, is_owner: bool) -> bool:
        if incoming.command not in {"allow_here", "deny_here"}:
            return False
        if not is_owner or not incoming.is_group:
            return True
        allowed = incoming.command == "allow_here"
        await self.repository.set_chat_allowed(incoming.chat_id, allowed)
        text = (
            "Этот чат разрешён. Persona будет учитывать каждое сообщение, "
            "которое Telegram передаёт боту, и иногда отвечать сама. Если "
            "обычные сообщения не приходят: @BotFather → /setprivacy → Disable, "
            "затем заново добавь бота в группу."
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
    update_id = _int(update.get("update_id"))
    message = update.get("message") or update.get("edited_message")
    if not isinstance(message, dict):
        return None
    sender = message.get("from")
    chat = message.get("chat")
    if not isinstance(sender, dict) or not isinstance(chat, dict) or bool(sender.get("is_bot")):
        return None
    sender_id = _int(sender.get("id"))
    chat_id = _int(chat.get("id"))
    message_id = _int(message.get("message_id"))
    attachments = attachments_from_message(message)
    text = str(message.get("text") or message.get("caption") or "").strip()
    non_file_summary = non_file_content_summary(message)
    if not text and attachments:
        text = "[Вложение Telegram: " + ", ".join(
            item.summary() for item in attachments
        ) + "]"
    if non_file_summary:
        text = f"{text}\n{non_file_summary}".strip()
    if (
        update_id is None
        or sender_id is None
        or chat_id is None
        or message_id is None
        or not text
    ):
        return None
    command, argument = _command(text)
    return IncomingMessage(
        update_id=update_id,
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
        attachments=attachments,
    )


def _command(text: str) -> tuple[str, str]:
    match = re.match(r"^/([A-Za-z_]+)(?:@[A-Za-z0-9_]+)?(?:\s+(.*))?$", text, re.S)
    if not match:
        return "", text
    return match.group(1).casefold(), (match.group(2) or "").strip()


def _int(value: object) -> int | None:
    if not isinstance(value, (int, str)):
        return None
    try:
        return int(value)
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


__all__ = ["TelegramConsumerLeaseLost", "TelegramWorker"]
