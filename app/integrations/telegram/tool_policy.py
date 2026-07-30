"""Explicit tool-access policy: WHERE a tool is usable, not just WHETHER.

Persona's group input includes text written by other people -- in the
owner's group there are two humans and two AI agents alongside the owner.
Anything that can execute code or otherwise change the machine must never
be reachable from that text, so it stays entirely out of group reach no
matter who is speaking.

Group access is deliberately narrower than "read-only": only
``web_search`` is offered there.

- ``fetch_json`` accepts POST/PUT/PATCH with arbitrary headers and body --
  pointed at the internet from the owner's IP, driven by text a stranger
  wrote in a group. See ``app/mcp/tool_policy.py`` for the same risk
  classification (``ToolRisk.MUTATING``).
- ``web_browse`` bypasses the SSRF policy that ``_url_is_safe`` in
  ``app/mcp/builtin_tools.py`` provides for other tools (see
  ``ToolRisk.UNSAFE_NETWORK`` in ``app/mcp/tool_policy.py``).
- ``web_search`` goes through a search provider and carries neither
  property, so it stays safe everywhere, including group chats where the
  sender may not be the owner.

This module intentionally replaces a keyword/length heuristic that used
to silently mute every tool whenever the owner's message was short and
matched no trigger word.
"""

from __future__ import annotations

#: The only tool offered in group chats, regardless of who is speaking --
#: see the module docstring for why web_browse and fetch_json are excluded.
READ_ONLY_TOOLS: frozenset[str] = frozenset({"web_search"})


def allowed_tools(*, is_owner: bool, is_group: bool) -> frozenset[str] | None:
    """Return the tools usable for this turn.

    ``None`` means every enabled tool is usable (the owner, in private).
    A ``frozenset`` means exactly those tools are usable. An empty
    ``frozenset`` means no tools at all (a non-owner in a private chat --
    such a turn should not reach the model in the first place, but the
    policy must not depend on that being enforced elsewhere).
    """
    if is_group:
        # Group text is written by other people too, so only read-only
        # internet access is offered, regardless of who sent this message.
        return READ_ONLY_TOOLS
    if is_owner:
        return None
    return frozenset()


__all__ = ["READ_ONLY_TOOLS", "allowed_tools"]
