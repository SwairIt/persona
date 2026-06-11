"""T29 — overnight self-play loop.

Runs unattended (e.g. overnight): generates a prompt from a rotating seed
list, runs the configured chat model (some iterations browse the web via
the web_browse tool), and records each Q&A pair into the training dataset
UNRATED, so the user can review + rate them in the morning.

Honest scope: this GATHERS data and stress-tests the stack. It does NOT
self-improve the model (that needs a separate fine-tune run) and it does
NOT self-rate (a 7B judging itself = noise; ratings stay human).

Run:
    python -m ops.selfplay --user 2 --minutes 480 --interval 90

Stop early: create the file ~/.persona/selfplay.stop  (or Ctrl-C).
"""

from __future__ import annotations

import argparse
import asyncio
import sys
import time
from pathlib import Path

# Rotating seed prompts. The user asked me (the assistant) to write these.
# Mix of code, ideas, reflection, and web-research (browse) seeds.
_TEXT_SEEDS: list[str] = [
    "Напиши на Python небольшую полезную функцию (на свой выбор) с примером использования и кратким объяснением.",
    "Объясни простыми словами одну нетривиальную концепцию из программирования, как другу за чаем.",
    "Придумай 3 свежие идеи для пет-проекта, который можно сделать за выходные. Кратко, по делу.",
    "Дай честный разбор: какие частые ошибки делают новички в вебе и как их избежать.",
    "Напиши короткое, тёплое, человечное сообщение поддержки тому, у кого тяжёлый день.",
    "Объясни, чем отличается хороший код от плохого, на конкретном маленьком примере до/после.",
    "Предложи план на день для продуктивной, но не выгорающей работы. Реалистично.",
    "Разбери плюсы и минусы локальных LLM против облачных — коротко и по делу.",
    "Напиши CSS-сниппет для красивого glassmorphism-эффекта карточки и объясни ключевые свойства.",
    "Дай 5 принципов хорошего UX одностраничного сайта, каждый с одним примером.",
    "Объясни, как работает parallax-эффект при скролле, и покажи минимальный рабочий пример на JS.",
    "Сформулируй, что делает ответ ИИ-ассистента по-настоящему полезным, а не просто многословным.",
]

# (url, question) — exercises web_browse + vision analysis.
_BROWSE_SEEDS: list[tuple[str, str]] = [
    ("https://news.ycombinator.com", "Какие темы сейчас на главной? Кратко перескажи по-русски."),
    ("https://example.com", "Что это за страница, по-русски?"),
    ("https://www.python.org", "Что предлагает сайт, главное, по-русски."),
]


async def _run(user_id: int, minutes: int, interval: int) -> int:
    # Lazy imports so the script fails loudly only when actually run.
    from app.chat import (
        append_message,
        create_session,
        get_active_system_prompt,
        list_sessions,
    )
    from app.llm.client import CompletionRequest, OllamaClient
    from app.mcp.builtin_tools import call_tool
    from app.storage.db import get_connection
    from app.storage.repository import get_kv
    from app.training import record_qa_pair

    stop_file = Path.home() / ".persona" / "selfplay.stop"
    if stop_file.exists():
        stop_file.unlink()

    # Resolve the configured Ollama endpoint + model.
    async with get_connection() as conn:
        endpoint = (await get_kv(conn, "byo_api_key_ollama") or "").strip()
        model = (await get_kv(conn, "ollama_model") or "").strip()
    endpoint = endpoint or "http://localhost:11434"
    model = model or "qwen2.5:7b"
    system_prompt = await get_active_system_prompt()
    client = OllamaClient(api_key=endpoint, model=model)

    # One dedicated session for all self-play turns.
    title = "🌙 Ночной самотренинг"
    existing = await list_sessions(user_id, limit=50)
    sess = next((s for s in existing if s["title"] == title), None)
    if sess is None:
        sess = await create_session(user_id, title=title)
    session_id = int(sess["id"])

    deadline = time.monotonic() + minutes * 60
    i = 0
    recorded = 0
    print(f"[selfplay] user={user_id} model={model} session={session_id} "
          f"minutes={minutes} interval={interval}s", flush=True)

    while time.monotonic() < deadline:
        if stop_file.exists():
            print("[selfplay] stop file found — exiting", flush=True)
            break
        i += 1
        try:
            # Every 4th iteration → browse the web; else → text Q&A.
            if i % 4 == 0:
                url, question = _BROWSE_SEEDS[(i // 4) % len(_BROWSE_SEEDS)]
                prompt = f"{question} (сайт: {url})"
                answer = await call_tool(
                    "web_browse", {"url": url, "question": question}, user_id=user_id
                )
                used_model = "web_browse+qwen2.5vl"
            else:
                base = _TEXT_SEEDS[(i - 1) % len(_TEXT_SEEDS)]
                prompt = f"{base} (вариация #{i})"
                answer = await client.complete(
                    CompletionRequest(
                        system=system_prompt, user=prompt,
                        # Short answers → faster iterations → many more pairs
                        # overnight, and easier for the user to rate.
                        max_tokens=700, temperature=0.8,
                    )
                )
                used_model = model

            if not (answer or "").strip():
                answer = "(пустой ответ)"
            user_msg = await append_message(session_id, "user", prompt)
            asst_msg = await append_message(
                session_id, "assistant", answer, model_used=used_model
            )
            await record_qa_pair(
                session_id=session_id,
                user_message_id=int(user_msg["id"]),
                asst_message_id=int(asst_msg["id"]),
                user_text=prompt,
                assistant_text=answer,
                system_prompt=system_prompt,
                context_turns=None,
                image_present=False,
                provider="ollama",
                model=used_model,
            )
            recorded += 1
            print(f"[selfplay] #{i} recorded ({recorded} total): {prompt[:60]}",
                  flush=True)
        except Exception as exc:  # noqa: BLE001 — never die on one bad turn
            print(f"[selfplay] #{i} error: {type(exc).__name__}: {exc}", flush=True)

        # Sleep in short slices so the stop file / deadline are responsive.
        slept = 0
        while slept < interval and time.monotonic() < deadline:
            if stop_file.exists():
                break
            await asyncio.sleep(min(5, interval - slept))
            slept += 5

    print(f"[selfplay] done. recorded {recorded} pairs in {i} iterations.",
          flush=True)
    return 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--user", type=int, default=2)
    ap.add_argument("--minutes", type=int, default=480)  # 8 hours
    ap.add_argument("--interval", type=int, default=90)
    args = ap.parse_args()
    return asyncio.run(_run(args.user, args.minutes, args.interval))


if __name__ == "__main__":
    sys.exit(main())
