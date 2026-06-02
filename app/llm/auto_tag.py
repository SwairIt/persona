"""LLM-assisted tag suggestion — read OCR, return 3-5 short tag candidates."""

from __future__ import annotations

import json
import re

from app.llm.client import CompletionRequest, LLMClient, make_client

_SYSTEM = (
    "You suggest tags for personal-memory screenshots. Given the app name, "
    "window title and OCR text, return 3-5 SHORT lowercase tags that someone "
    "could use to find this moment again. Tags are single words or short "
    "kebab-case phrases (e.g. 'auth-bug', 'meeting', 'invoice'). Avoid generic "
    "terms ('screen', 'computer', 'app'). Match the user's language if Cyrillic "
    "dominates. Output ONLY valid JSON in this shape: "
    '{"tags": ["tag1", "tag2", "tag3"]}'
)


async def suggest_tags(
    *,
    app_name: str | None,
    window_title: str | None,
    ocr_text: str | None,
    client: LLMClient | None = None,
) -> list[str]:
    llm = client or make_client()
    body = "\n".join(
        [
            f"App: {app_name or '(unknown)'}",
            f"Window: {window_title or '(unknown)'}",
            "OCR:",
            (ocr_text or "(none)")[:2000],
        ]
    )
    request = CompletionRequest(
        system=_SYSTEM,
        user=body,
        max_tokens=160,
        temperature=0.3,
    )
    text = await llm.complete(request)
    return _parse_tags(text)


_JSON_RE = re.compile(r"\{[^{}]*\}", re.DOTALL)


def _parse_tags(text: str) -> list[str]:
    """Best-effort extract tags array from LLM JSON output."""
    if not text:
        return []
    candidates = _JSON_RE.findall(text)
    for raw in (text, *candidates):
        try:
            obj = json.loads(raw)
        except (ValueError, TypeError):
            continue
        if isinstance(obj, dict) and isinstance(obj.get("tags"), list):
            cleaned: list[str] = []
            for tag in obj["tags"]:
                if not isinstance(tag, str):
                    continue
                normalised = tag.strip().lower()
                normalised = re.sub(r"\s+", "-", normalised)
                normalised = re.sub(r"[^a-zа-я0-9_\-]", "", normalised)
                if 2 <= len(normalised) <= 32:
                    cleaned.append(normalised)
            seen: set[str] = set()
            out: list[str] = []
            for tag in cleaned:
                if tag not in seen:
                    seen.add(tag)
                    out.append(tag)
                if len(out) >= 8:
                    break
            return out
    return []
