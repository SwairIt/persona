"""Legacy-compatible adapters for the conversation application slice."""

from app.adapters.conversation.legacy import build_conversation_service

__all__ = ["build_conversation_service"]
