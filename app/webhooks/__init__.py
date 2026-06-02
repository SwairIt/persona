"""Outbound webhook dispatcher."""

from app.webhooks.dispatcher import dispatch_event

__all__ = ["dispatch_event"]
