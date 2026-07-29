"""Ownership and single-active-generation guards for chat control routes."""

from __future__ import annotations

import asyncio
from typing import TYPE_CHECKING
from unittest.mock import AsyncMock

import pytest
from fastapi import HTTPException
from fastapi.responses import StreamingResponse

from app.web.routes import chat_sessions

if TYPE_CHECKING:
    from collections.abc import AsyncIterator


async def _hold() -> None:
    await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_send_stream_rejects_second_active_generation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    task = asyncio.create_task(_hold())
    live = chat_sessions._LiveGen()
    live.task = task
    chat_sessions._LIVE_GENS[91] = live
    monkeypatch.setattr(
        chat_sessions,
        "get_session",
        AsyncMock(return_value={"id": 91, "user_id": 7}),
    )
    clear_stop = AsyncMock()
    monkeypatch.setattr(chat_sessions, "_set_stop", clear_stop)
    try:
        with pytest.raises(HTTPException) as raised:
            await chat_sessions.api_send_stream(
                request=None,  # type: ignore[arg-type]
                session_id=91,
                session={"user_id": 7},  # type: ignore[typeddict-item]
                body={"question": "second turn"},
            )
    finally:
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task
        chat_sessions._LIVE_GENS.pop(91, None)

    assert raised.value.status_code == 409
    clear_stop.assert_not_awaited()


@pytest.mark.asyncio
async def test_concurrent_send_stream_reserves_generation_atomically(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    both_owned = asyncio.Event()
    service_entered = asyncio.Event()
    release_service = asyncio.Event()
    ownership_reads = 0

    async def get_owned(_user_id: int, _session_id: int) -> dict[str, int]:
        nonlocal ownership_reads
        ownership_reads += 1
        if ownership_reads == 2:
            both_owned.set()
        await both_owned.wait()
        return {"id": 92, "user_id": 7}

    async def clear_stop(_session_id: int, _on: bool) -> None:
        await asyncio.sleep(0)

    async def flags() -> dict[str, bool]:
        return {"master": False}

    async def service_stream(**_kwargs: object) -> StreamingResponse:
        service_entered.set()
        await release_service.wait()

        async def body() -> AsyncIterator[str]:
            yield 'data: {"type":"done"}\n\n'

        return StreamingResponse(body(), media_type="text/event-stream")

    monkeypatch.setattr(chat_sessions, "get_session", get_owned)
    monkeypatch.setattr(chat_sessions, "_set_stop", clear_stop)
    monkeypatch.setattr(chat_sessions, "get_advanced_flags", flags)
    monkeypatch.setattr(
        chat_sessions,
        "_stream_via_conversation_service",
        service_stream,
    )

    async def send() -> StreamingResponse:
        return await chat_sessions.api_send_stream(
            request=None,  # type: ignore[arg-type]
            session_id=92,
            session={"user_id": 7},  # type: ignore[typeddict-item]
            body={"question": "one turn"},
        )

    first = asyncio.create_task(send())
    second = asyncio.create_task(send())
    await asyncio.wait_for(service_entered.wait(), timeout=1)
    release_service.set()
    results = await asyncio.gather(first, second, return_exceptions=True)
    chat_sessions._LIVE_GENS.pop(92, None)

    assert sum(isinstance(item, StreamingResponse) for item in results) == 1
    conflicts = [
        item
        for item in results
        if isinstance(item, HTTPException) and item.status_code == 409
    ]
    assert len(conflicts) == 1


@pytest.mark.asyncio
async def test_stop_rejects_foreign_or_missing_conversation_before_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(
        chat_sessions,
        "get_session",
        AsyncMock(return_value=None),
    )
    set_stop = AsyncMock()
    monkeypatch.setattr(chat_sessions, "_set_stop", set_stop)

    with pytest.raises(HTTPException) as raised:
        await chat_sessions.api_stop_generation(
            session_id=404,
            session={"user_id": 7},  # type: ignore[typeddict-item]
        )

    assert raised.value.status_code == 404
    set_stop.assert_not_awaited()
