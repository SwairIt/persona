"""Hierarchical tag tree — HTML page + JSON endpoint.

The page is a pure-HTML ``<details>``/``<summary>`` tree — no JS, no
Alpine state — so collapsing a branch is a single browser-native
interaction with zero round-trips. The JSON endpoint exposes the same
nested structure for scripts and the browser extension.
"""

from __future__ import annotations

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse

from app.logging_setup import get_logger
from app.tag_tree import TagTreeNode, build_tree
from app.web.templates_engine import templates

log = get_logger("persona.tag_tree")

router = APIRouter(tags=["tag-tree"])


def _total_count(nodes: dict[str, TagTreeNode]) -> int:
    """Sum of all top-level node counts — used as the page subtitle."""
    return sum(node["count"] for node in nodes.values())


def _leaf_count(nodes: dict[str, TagTreeNode]) -> int:
    """Recursive count of leaf nodes — the "you have N tags" headline."""
    total = 0
    for node in nodes.values():
        if node["is_leaf"]:
            total += 1
        total += _leaf_count(node["children"])
    return total


@router.get("/tags/tree", response_class=HTMLResponse)
async def tag_tree_page(request: Request) -> HTMLResponse:
    """Render the hierarchical tag tree with collapsible ``<details>``."""
    tree = await build_tree()
    return templates.TemplateResponse(
        request,
        "tag_tree.html",
        {
            "title": "Tag tree",
            "active_nav": "tags",
            "tree": tree,
            "total_count": _total_count(tree),
            "leaf_count": _leaf_count(tree),
            "json_url": "/api/tags/tree.json",
        },
    )


@router.get("/api/tags/tree.json", response_class=JSONResponse)
async def tag_tree_json() -> JSONResponse:
    """Machine-readable counterpart to :func:`tag_tree_page`."""
    tree = await build_tree()
    return JSONResponse(
        {
            "total_count": _total_count(tree),
            "leaf_count": _leaf_count(tree),
            "tree": tree,
        }
    )
