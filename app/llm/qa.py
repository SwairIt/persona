"""Question-answering over your past captures.

Given a natural-language question, we:
  1. Run a semantic search (if embeddings enabled) + FTS5 search.
  2. Take the top-K most relevant screenshots as context.
  3. Ask the configured BYO LLM to answer based ONLY on that context.
  4. Return both the answer text and the cited screenshot ids.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

from app.chrono_parse import parse_natural_date
from app.embeddings import EmbeddingsNotAvailable, is_available, semantic_search
from app.llm.client import CompletionRequest, LLMClient, make_client
from app.logging_setup import get_logger
from app.search import search as fts_search
from app.settings import get_settings
from app.storage.db import get_connection

log = get_logger("persona.qa")


_QA_SYSTEM = (
    "You are a memory assistant for a single user. You will be shown a "
    "RANKED list of past CAPTURES — each is either a screenshot (with "
    "timestamp, app, window title, OCR text) or an HOURLY SUMMARY CARD "
    "covering one hour of activity (apps used, voice transcript, OCR "
    "keywords). The user will ask a question. Answer it using ONLY the "
    "information visible in that context. If the answer is not there, "
    "say so honestly; do not invent facts. Cite items by id like [#42] "
    "for screenshots or [hour:HH:MM] for hourly cards. Reply in the "
    "user's language (Russian if the context is mostly Cyrillic, "
    "English otherwise). Keep the answer short and concrete (2-6 sentences)."
)


@dataclass(frozen=True, slots=True)
class QAResult:
    answer: str
    citations: list[int]
    used_screenshots: int


async def ask(
    question: str,
    *,
    top_k: int = 10,
    client: LLMClient | None = None,
) -> QAResult:
    question = question.strip()
    if not question:
        msg = "Empty question"
        raise ValueError(msg)

    context = await _gather_context(question, top_k=top_k)
    if not context:
        return QAResult(
            answer="No relevant captures found for your question.",
            citations=[],
            used_screenshots=0,
        )

    llm = client or make_client()
    prompt = _build_prompt(question, context)
    completion = await llm.complete(
        CompletionRequest(system=_QA_SYSTEM, user=prompt, max_tokens=600),
    )

    citations = _extract_citations(completion, valid_ids={c["id"] for c in context})
    return QAResult(
        answer=completion,
        citations=sorted(citations),
        used_screenshots=len(context),
    )


async def _gather_context(question: str, *, top_k: int) -> list[dict[str, object]]:
    settings = get_settings()
    out: dict[int, dict[str, object]] = {}

    async with get_connection() as conn:
        if settings.embeddings_enabled and is_available():
            try:
                sem_hits = await semantic_search(conn, query=question, limit=top_k)
            except EmbeddingsNotAvailable:
                sem_hits = []
            for hit in sem_hits:
                out[hit["screenshot_id"]] = {
                    "id": hit["screenshot_id"],
                    "captured_at": str(hit["captured_at"]),
                    "app_name": hit.get("app_name"),
                    "window_title": hit.get("window_title"),
                    "ocr_text": "",
                    "rank_source": "semantic",
                    "similarity": hit.get("similarity"),
                }

        fts_hits = await fts_search(conn, query=question, limit=top_k)
        for hit in fts_hits:
            existing = out.get(hit.screenshot_id)
            if existing is None:
                out[hit.screenshot_id] = {
                    "id": hit.screenshot_id,
                    "captured_at": hit.captured_at.isoformat(),
                    "app_name": hit.app_name,
                    "window_title": hit.window_title,
                    "ocr_text": "",
                    "rank_source": "fts",
                    "similarity": None,
                }

        for sid in list(out.keys()):
            cursor = await conn.execute(
                "SELECT ocr_text FROM screenshots WHERE id = ?", (sid,)
            )
            row = await cursor.fetchone()
            if row and row["ocr_text"]:
                out[sid]["ocr_text"] = str(row["ocr_text"])[:1500]

        # v1.14 — also pull top hourly cards matching the question via
        # FTS5 over summary/transcript/keywords. These are returned as a
        # second slice of context with distinct ids (negative to avoid
        # collisions with screenshot ids) so the prompt builder can show
        # them under their own "Hourly summaries" section.
        try:
            cursor = await conn.execute(
                "SELECT c.rowid AS rid, c.hour_start, c.summary, "
                "       c.transcript_excerpt, c.top_words "
                "FROM hourly_card_fts f "
                "JOIN hourly_card c ON c.rowid = f.rowid "
                "WHERE f MATCH ? ORDER BY rank LIMIT ?",
                (question, top_k),
            )
            card_rows = await cursor.fetchall()
        except Exception as exc:  # noqa: BLE001 — FTS may raise on weird tokens
            log.debug("qa.hourly_card_search_failed", error=str(exc))
            card_rows = []

        for row in card_rows:
            card_key = -int(row["rid"])  # negative id namespace for cards
            out[card_key] = {
                "id": card_key,
                "is_card": True,
                "hour_start": str(row["hour_start"]),
                "summary": str(row["summary"]),
                "transcript_excerpt": str(row["transcript_excerpt"] or ""),
                "top_words": str(row["top_words"] or ""),
                "rank_source": "card_fts",
            }

    return list(out.values())[: top_k * 2]


def _build_prompt(question: str, context: list[dict[str, object]]) -> str:
    screenshots = [c for c in context if not c.get("is_card")]
    cards = [c for c in context if c.get("is_card")]

    lines = [f"Question: {question}", ""]

    # v1.x — surface any natural-language date phrase the user typed
    # ("yesterday", "на прошлой неделе", "3 days ago", ...) as an
    # explicit "Дата: <start>..<end>" line so the LLM has an unambiguous
    # filter window even though retrieval already ranked by relevance.
    chrono_hit = parse_natural_date(question, now=datetime.now(tz=UTC))
    if chrono_hit is not None:
        lines.append(
            f"Дата: {chrono_hit['start_iso']}..{chrono_hit['end_iso']} "
            f"(matched: {chrono_hit['matched_phrase']!r}, "
            f"kind={chrono_hit['kind']})"
        )
        lines.append("")
    if screenshots:
        lines.append("Screenshots (top relevant):")
        for c in screenshots:
            lines.append(
                f"[#{c['id']}] {c['captured_at']} {c.get('app_name') or '?'} — "
                f"{c.get('window_title') or ''}"
            )
            text = (c.get("ocr_text") or "").strip()
            if text:
                lines.append(f"  >> {text[:600]}")
            lines.append("")

    if cards:
        lines.append("Hourly summary cards (top relevant):")
        for c in cards:
            lines.append(f"[hour:{c.get('hour_start')}]")
            summary = (c.get("summary") or "").strip()
            if summary:
                lines.append(summary[:1500])
            transcript = (c.get("transcript_excerpt") or "").strip()
            if transcript:
                lines.append(f"  voice: {transcript[:400]}")
            lines.append("")

    lines.append(
        "Answer the question using only the context above. Cite "
        "screenshots like [#42] and hourly cards like [hour:2026-06-04T14:00:00+00:00]."
    )
    return "\n".join(lines)


def _extract_citations(text: str, *, valid_ids: set[int]) -> set[int]:
    import re

    found = set()
    for match in re.finditer(r"\[#?(\d+)\]", text):
        try:
            sid = int(match.group(1))
        except ValueError:
            continue
        if sid in valid_ids:
            found.add(sid)
    return found
