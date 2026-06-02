"""Optional LLM integration — BYO API key, requests go user → provider directly."""

from app.llm.auto_tag import suggest_tags
from app.llm.client import LLMClient, LLMNotConfigured, make_client
from app.llm.note_draft import draft_note
from app.llm.qa import QAResult, ask
from app.llm.summariser import build_daily_summary_prompt, summarise_day

__all__ = [
    "LLMClient",
    "LLMNotConfigured",
    "QAResult",
    "ask",
    "build_daily_summary_prompt",
    "draft_note",
    "make_client",
    "suggest_tags",
    "summarise_day",
]
