"""Public blog + SEO endpoints.

Routes (all public — in the auth-gate allow-list):
    * GET /blog            — article index (cards + categories)
    * GET /blog/{slug}     — single article (sticky TOC, scrollspy, JSON-LD)
    * GET /sitemap.xml     — landing + blog + every article
    * GET /robots.txt      — allow all + sitemap pointer

Blog content is file-based (see app/blog.py) — global site content, not
per-user data, so nothing here is user-scoped.
"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, PlainTextResponse

from app import blog
from app.auth import current_user_optional
from app.auth.sessions import SessionRecord
from app.web.templates_engine import templates

router = APIRouter(tags=["blog"])


@router.get("/blog", response_class=HTMLResponse)
async def blog_index(
    request: Request,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    return templates.TemplateResponse(
        request,
        "blog_index.html",
        {
            "title": "Блог Persona — личный ИИ, память, приватность",
            "posts": blog.list_posts(),
            "categories": blog.list_categories(),
            "session": session,
        },
    )


@router.get("/blog/{slug}", response_class=HTMLResponse, response_model=None)
async def blog_post(
    request: Request,
    slug: str,
    session: Annotated[SessionRecord | None, Depends(current_user_optional)],
) -> HTMLResponse:
    post = blog.get_post(slug)
    if post is None:
        return templates.TemplateResponse(
            request, "blog_404.html", {"title": "Статья не найдена"}, status_code=404
        )
    posts = blog.list_posts()
    idx = next((i for i, p in enumerate(posts) if p.slug == slug), 0)
    prev_post = posts[idx + 1] if idx + 1 < len(posts) else None
    next_post = posts[idx - 1] if idx - 1 >= 0 else None
    related = [p for p in posts if p.slug != slug and p.category == post.category][:3]
    return templates.TemplateResponse(
        request,
        "blog_post.html",
        {
            "title": f"{post.title} — Persona",
            "post": post,
            "prev_post": prev_post,
            "next_post": next_post,
            "related": related,
            "session": session,
        },
    )


@router.get("/robots.txt")
async def robots(request: Request) -> PlainTextResponse:
    base = str(request.base_url).rstrip("/")
    body = f"User-agent: *\nAllow: /\nSitemap: {base}/sitemap.xml\n"
    return PlainTextResponse(content=body)
