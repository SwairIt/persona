"""Auto-draft a short note for a screenshot using the BYO LLM."""

from __future__ import annotations

from app.llm.client import CompletionRequest, LLMClient, make_client

_SYSTEM = (
    "You are a memory assistant. The user is looking at one of their captured "
    "screenshots and wants a short journaling note that captures what's worth "
    "remembering. Given the app name, window title, and OCR text, write ONE or "
    "TWO sentences in the user's language (Russian if Cyrillic dominates, "
    "English otherwise). Be specific — quote a concrete detail. Don't paraphrase "
    "the OCR verbatim; summarise it. Avoid headings or bullet lists. Output ONLY "
    "the note text — no preamble."
)


async def draft_note(
    *,
    app_name: str | None,
    window_title: str | None,
    ocr_text: str | None,
    client: LLMClient | None = None,
) -> str:
    llm = client or make_client()
    body = "\n".join(
        [
            f"App: {app_name or '(unknown)'}",
            f"Window: {window_title or '(unknown)'}",
            "OCR:",
            (ocr_text or "(no OCR available)")[:2000],
        ]
    )
    request = CompletionRequest(
        system=_SYSTEM,
        user=body,
        max_tokens=160,
        temperature=0.5,
    )
    return await llm.complete(request)
