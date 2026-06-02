"""HTTP surface for :func:`app.query_api.run_query`.

POST ``/api/query`` accepts the structured request body documented on the
pydantic v2 :class:`QueryRequest` model below and returns a JSON payload
with one bucket per requested ``kind``. GET ``/api/query/example`` returns
a hand-rolled sample request so clients (jq / curl users / future SDKs)
can self-discover the schema without reading source.

Read-only — never commits, never touches user-supplied SQL fragments. All
validation lives in pydantic + :func:`app.query_api.run_query`; this file
is intentionally thin glue.
"""

from __future__ import annotations

from datetime import date, datetime
from typing import Annotated, Any, Literal

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse
from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.logging_setup import get_logger
from app.query_api import run_query

router = APIRouter(prefix="/api/query", tags=["query-api"])

log = get_logger("persona.query_api")

# Mirror the limit ceiling enforced in app.query_api so callers see the
# 422 here rather than getting a silently clamped value back.
_MAX_LIMIT = 500
_MAX_FTS_LEN = 500
_MAX_APP_LEN = 200
_MAX_TAG_LEN = 100
_MAX_TAGS = 32

Kind = Literal["screenshot", "note", "tag", "day"]


class QueryRequest(BaseModel):
    """Structured query body for ``POST /api/query``.

    Every field is optional except ``kinds``, which defaults to all four
    so a bare ``{}`` request returns a useful exploratory payload. Date
    fields accept ``YYYY-MM-DD`` or a full ISO 8601 timestamp — the
    storage layer normalises both.
    """

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)

    fts: Annotated[str | None, Field(default=None, max_length=_MAX_FTS_LEN)]
    app: Annotated[str | None, Field(default=None, max_length=_MAX_APP_LEN)] = None
    date_from: Annotated[str | None, Field(default=None, max_length=64)] = None
    date_to: Annotated[str | None, Field(default=None, max_length=64)] = None
    tags: Annotated[
        list[str] | None,
        Field(default=None, max_length=_MAX_TAGS),
    ] = None
    kinds: Annotated[
        list[Kind] | None,
        Field(default=None, max_length=len(("screenshot", "note", "tag", "day"))),
    ] = None
    limit: Annotated[int, Field(default=50, ge=1, le=_MAX_LIMIT)] = 50

    @field_validator("tags")
    @classmethod
    def _validate_tags(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        cleaned: list[str] = []
        for raw in value:
            stripped = raw.strip()
            if not stripped:
                continue
            if len(stripped) > _MAX_TAG_LEN:
                msg = f"tag too long (>{_MAX_TAG_LEN} chars)"
                raise ValueError(msg)
            cleaned.append(stripped)
        return cleaned or None

    @field_validator("date_from", "date_to")
    @classmethod
    def _validate_date(cls, value: str | None) -> str | None:
        if value is None or not value.strip():
            return None
        stripped = value.strip()
        # Accept bare YYYY-MM-DD or any ISO 8601 timestamp Python can parse.
        try:
            if len(stripped) == 10 and stripped[4] == "-" and stripped[7] == "-":
                date.fromisoformat(stripped)
            else:
                datetime.fromisoformat(stripped.replace("Z", "+00:00"))
        except ValueError as exc:
            msg = f"invalid ISO date / datetime: {stripped!r}"
            raise ValueError(msg) from exc
        return stripped

    @model_validator(mode="after")
    def _validate_window(self) -> QueryRequest:
        """Ensure ``date_from <= date_to`` if both are supplied."""
        if self.date_from and self.date_to:
            df_norm = self.date_from
            dt_norm = self.date_to
            # Compare as ISO strings — same prefix order as datetime cmp.
            if df_norm > dt_norm:
                msg = "date_from must be <= date_to"
                raise ValueError(msg)
        return self


@router.post("", response_class=JSONResponse)
async def query_endpoint(payload: QueryRequest) -> JSONResponse:
    """Run a structured query and return the mixed-kind result map."""
    try:
        result = await run_query(payload.model_dump(exclude_none=False))
    except ValueError as exc:
        # ``run_query`` re-validates after pydantic (it is also reachable
        # from in-process callers). If it complains here, the pydantic
        # model failed to catch something — surface a 400, not a 500.
        log.warning("query_api.bad_request", error=str(exc))
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return JSONResponse(result)


@router.get("/example", response_class=JSONResponse)
async def query_example() -> JSONResponse:
    """Return a representative request body for client discoverability."""
    example: dict[str, Any] = {
        "fts": "invoice",
        "app": "Code.exe",
        "date_from": "2026-05-01",
        "date_to": "2026-06-01",
        "tags": ["work", "billing"],
        "kinds": ["screenshot", "note", "tag", "day"],
        "limit": 50,
    }
    return JSONResponse(
        {
            "endpoint": "POST /api/query",
            "request": example,
            "notes": [
                "All fields are optional except kinds (defaults to all four).",
                "fts uses FTS5 over OCR text, window titles, app names "
                "(screenshots) and note body (notes).",
                "date_from / date_to accept YYYY-MM-DD or full ISO 8601.",
                "tags filter is AND across the supplied tag names.",
                "limit is clamped to [1, 500]; default is 50.",
            ],
        }
    )
