"""Hierarchical tag tree — treat ``work/projects/foo`` as a 3-level path.

Tag names are split on ``/`` and grouped into a nested dict so the UI can
render collapsible ``<details>`` blocks without doing the grouping in
Jinja. The split is purely cosmetic — ``tags.name`` is still stored as
the raw ``"work/projects/foo"`` string, and the screenshot ↔ tag join
table is untouched. Tags without a slash become a single top-level leaf.

The leaf node carries the actual ``screenshot_tags`` count so callers
can show a number next to every branch in the tree. Internal (non-leaf)
nodes get the *sum of their descendants' counts* so a collapsed branch
still tells the operator how much is hiding underneath.

The output shape is a recursive ``TagTreeNode`` TypedDict — the route
layer and the JSON endpoint share the same structure so the page and
the API never drift.
"""

from __future__ import annotations

from typing import TypedDict

from app.logging_setup import get_logger
from app.storage.db import get_connection

log = get_logger("persona.tag_tree")

# Three explicit levels per the spec — anything deeper is folded into
# the third level so a stray ``a/b/c/d`` doesn't blow up the tree.
_MAX_DEPTH = 3
_SEPARATOR = "/"


class TagTreeNode(TypedDict):
    """One node in the nested tag tree.

    * ``name`` — the path segment at this depth (``"projects"`` for
      ``work/projects/foo``), not the full tag name.
    * ``full_path`` — the joined path from the root, useful as a stable
      DOM id / search query (``"work/projects"`` for the internal node,
      ``"work/projects/foo"`` for the leaf).
    * ``count`` — for a leaf, the number of screenshots tagged with the
      full path; for an internal node, the sum of descendant leaf
      counts.
    * ``children`` — child nodes keyed by their own ``name``. Empty dict
      on a leaf.
    * ``is_leaf`` — ``True`` when an actual ``tags`` row exists for
      ``full_path``. An internal node can also be a leaf when a tag
      named ``"work/projects"`` exists alongside ``"work/projects/foo"``.
    """

    name: str
    full_path: str
    count: int
    children: dict[str, TagTreeNode]
    is_leaf: bool


def _split_path(name: str) -> list[str]:
    """Split ``name`` on ``/`` into at most ``_MAX_DEPTH`` segments.

    Empty segments (``"work//foo"``) are dropped so the tree never grows
    an empty branch. Extra depth is collapsed into the third segment so
    ``a/b/c/d`` ends up under ``a → b → c/d``.
    """
    raw = [segment.strip() for segment in name.split(_SEPARATOR)]
    parts = [segment for segment in raw if segment]
    if not parts:
        return []
    if len(parts) <= _MAX_DEPTH:
        return parts
    head = parts[: _MAX_DEPTH - 1]
    tail = _SEPARATOR.join(parts[_MAX_DEPTH - 1 :])
    return [*head, tail]


def _new_node(name: str, full_path: str) -> TagTreeNode:
    """Create an empty internal node — leaf flag flips on later if needed."""
    return TagTreeNode(
        name=name,
        full_path=full_path,
        count=0,
        children={},
        is_leaf=False,
    )


def _insert(root: dict[str, TagTreeNode], name: str, count: int) -> None:
    """Insert one tag into the nested tree, propagating counts upward."""
    segments = _split_path(name)
    if not segments:
        return

    cursor = root
    accumulated: list[str] = []
    last_index = len(segments) - 1
    for index, segment in enumerate(segments):
        accumulated.append(segment)
        full_path = _SEPARATOR.join(accumulated)
        node = cursor.get(segment)
        if node is None:
            node = _new_node(segment, full_path)
            cursor[segment] = node
        node["count"] += count
        if index == last_index:
            node["is_leaf"] = True
        cursor = node["children"]


def _sort_tree(nodes: dict[str, TagTreeNode]) -> dict[str, TagTreeNode]:
    """Return ``nodes`` sorted alphabetically at every depth.

    Python dicts preserve insertion order, so rebuilding the dict in the
    sorted order is enough — the template iterates ``.values()`` and
    will see the segments alphabetised.
    """
    ordered: dict[str, TagTreeNode] = {}
    for key in sorted(nodes.keys()):
        node = nodes[key]
        node["children"] = _sort_tree(node["children"])
        ordered[key] = node
    return ordered


async def build_tree() -> dict[str, TagTreeNode]:
    """Build the nested tag tree from ``tags`` + ``screenshot_tags``.

    Returns a dict keyed by the top-level segment (``"work"``) whose
    values are :class:`TagTreeNode` instances. A tag with no ``/`` ends
    up as a single-segment leaf at the root.

    The SQL is intentionally simple — one ``LEFT JOIN`` per tag, the
    count is computed in the same query so we make a single round-trip
    to SQLite. The join is left so tags that nobody has applied yet
    still appear in the tree with ``count = 0``.
    """
    root: dict[str, TagTreeNode] = {}

    async with get_connection() as conn:
        cursor = await conn.execute(
            """
            SELECT t.name AS name,
                   COUNT(st.screenshot_id) AS shot_count
            FROM tags t
            LEFT JOIN screenshot_tags st ON st.tag_id = t.id
            GROUP BY t.id, t.name
            ORDER BY t.name
            """,
        )
        rows = list(await cursor.fetchall())

    for row in rows:
        raw_name = row["name"]
        if raw_name is None:
            continue
        name = str(raw_name).strip()
        if not name:
            continue
        count = int(row["shot_count"] or 0)
        _insert(root, name, count)

    ordered = _sort_tree(root)

    log.info(
        "tag_tree.built",
        tag_rows=len(rows),
        top_level=len(ordered),
        total_count=sum(node["count"] for node in ordered.values()),
    )

    return ordered
