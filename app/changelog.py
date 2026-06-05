"""Auto-generated changelog derived from the working repo's git history.

Why this module exists
----------------------
Persona ships features at a fast cadence and the operator wants a
plain-English ledger of "what changed lately" without having to keep a
hand-curated ``CHANGELOG.md`` in sync with reality. The HTML page at
``/changelog`` (see :mod:`app.web.routes.changelog`) reads from this
module, which in turn shells out to ``git log`` against the repository
that the running code lives in.

Design notes
------------
* **Async subprocess.** Persona's web stack is FastAPI on uvicorn, so a
  blocking ``subprocess.run`` would stall the event loop for the
  duration of the ``git log`` call. We use
  :func:`asyncio.create_subprocess_exec` (stdlib, no extra dep) — the
  spec mentions ``anyio.run_process`` as an alternative but anyio is a
  transitive dep here, not a direct one, and the stdlib path matches
  the rest of the codebase.
* **Argv list, no shell.** ``git log`` is invoked with a literal argv
  list (``["git", "log", ...]``) so user-controlled input never feeds
  into shell metacharacters. The ``limit`` arg is coerced to ``int``
  before being formatted into the ``-n`` flag.
* **Pipe-delimited format.** ``%h|%ai|%s|%an`` keeps parsing trivial.
  Commit subjects can contain ``|`` (rare but legal) — we ``split('|',
  3)`` so the author field absorbs any stray pipes after the fourth
  column rather than silently dropping the row.
* **Module-level cache.** The spec calls for a 60-second TTL cache
  without leaning on kv_settings — a tuple of ``(deadline, payload)``
  in a module global is the simplest thing that works. An
  ``asyncio.Lock`` guards the refresh so a thundering-herd of
  ``/changelog`` hits doesn't fan out into N concurrent git
  subprocesses.
* **Graceful when git is missing.** Production deploys *should* have
  git on PATH but a stripped container image might not. We surface the
  missing binary as a :class:`GitUnavailableError` so the route layer
  can render a friendly "Changelog unavailable" page instead of 500ing.
"""

from __future__ import annotations

import asyncio
import shutil
import time
from pathlib import Path
from typing import Final, TypedDict

from app.logging_setup import get_logger

log = get_logger("persona.changelog")

# Repo root: this file lives at ``<repo>/app/changelog.py``, so two
# ``parent`` hops land on the working-tree root that ``git`` should be
# pointed at. ``resolve()`` collapses any symlinks so a packaged install
# (e.g. running from ``site-packages`` via an editable install) still
# resolves to the source tree git knows about.
_REPO_ROOT: Final[Path] = Path(__file__).resolve().parent.parent

# 60-second cache TTL per the spec. The git log of a busy repo is cheap
# (single-digit ms even at a thousand commits) but the page can be hit
# from a feed reader or a /api/changelog.json poll, and keeping the
# subprocess count low avoids waking the disk every few seconds.
_CACHE_TTL_SECONDS: Final[float] = 60.0

# Subject-prefix → ``kind`` mapping. Conventional-commits flavoured but
# permissive: we look at the first ``":"``-delimited token, lowercase
# it, and strip a trailing ``(scope)`` clause if present. Anything not
# in this dict collapses to ``"other"`` so the UI's filter chips stay
# bounded.
_KIND_PREFIXES: Final[frozenset[str]] = frozenset(
    {"feat", "fix", "refactor", "docs", "test", "chore"}
)
_OTHER_KIND: Final[str] = "other"

# The pretty format ``git log`` should emit. ``%h`` short sha, ``%ai``
# author-date in ISO-8601 with timezone, ``%s`` subject, ``%an`` author
# name. The pipe separator is unlikely (but not impossible) to appear
# in a subject — see ``_parse_line`` for the split-with-cap that keeps
# any stray pipes inside the author column rather than dropping the row.
_GIT_PRETTY_FORMAT: Final[str] = "%h|%ai|%s|%an"


class ChangelogEntry(TypedDict):
    """One parsed row of ``git log``.

    Kept as a :class:`TypedDict` rather than a dataclass so the values
    flow straight into the Jinja2 context (``r.sha``, ``r.kind``, …)
    without an extra ``model_dump`` step.
    """

    sha: str
    date_iso: str
    subject: str
    author: str
    kind: str


class GitUnavailableError(RuntimeError):
    """Raised when the ``git`` binary cannot be located on PATH.

    The route layer in :mod:`app.web.routes.changelog` catches this and
    renders a "Changelog unavailable" page rather than letting a
    :class:`FileNotFoundError` from :mod:`asyncio` bubble into a 500.
    """


# ``(deadline_monotonic, payload)``. ``None`` means "never populated".
# ``deadline_monotonic`` is :func:`time.monotonic` based so wall-clock
# adjustments (NTP step, manual ``date`` change) can't poison the TTL.
_cache: tuple[float, list[ChangelogEntry]] | None = None
_cache_lock: asyncio.Lock = asyncio.Lock()


async def build_changelog(limit: int = 200) -> list[ChangelogEntry]:
    """Return the most recent ``limit`` non-merge commits as parsed dicts.

    Newest-first ordering — ``git log`` already emits in that order so
    no extra sort is needed. Results are cached in a module global for
    :data:`_CACHE_TTL_SECONDS` seconds; concurrent callers serialise on
    an ``asyncio.Lock`` so a burst of requests fans into a single git
    subprocess.

    Raises
    ------
    GitUnavailableError
        When the ``git`` binary is not discoverable on PATH.
    """
    global _cache  # noqa: PLW0603 — module-level cache is the documented design

    safe_limit = max(1, int(limit))

    # Fast path: serve straight from cache without taking the lock when
    # the deadline is still in the future. Reading a tuple is atomic in
    # CPython so this is race-free even without the lock.
    cached = _cache
    now = time.monotonic()
    if cached is not None and cached[0] > now:
        log.debug("changelog.cache.hit", entries=len(cached[1]))
        return cached[1]

    async with _cache_lock:
        # Re-check after acquiring the lock so a second waiter doesn't
        # re-run the subprocess that the first one just finished.
        cached = _cache
        now = time.monotonic()
        if cached is not None and cached[0] > now:
            log.debug("changelog.cache.hit.locked", entries=len(cached[1]))
            return cached[1]

        if shutil.which("git") is None:
            log.warning("changelog.git.unavailable")
            msg = "git binary not found on PATH"
            raise GitUnavailableError(msg)

        entries = await _run_git_log(safe_limit)
        _cache = (now + _CACHE_TTL_SECONDS, entries)
        log.info(
            "changelog.refreshed",
            entries=len(entries),
            limit=safe_limit,
            ttl_s=_CACHE_TTL_SECONDS,
        )
        return entries


def invalidate_cache() -> None:
    """Drop the cached changelog so the next call re-runs ``git log``.

    Mostly here for tests — production code relies on the 60-second
    TTL. Exposed at module level so a test fixture can reset between
    cases without having to monkey-patch the global directly.
    """
    global _cache  # noqa: PLW0603 — same reason as build_changelog
    _cache = None


async def _run_git_log(limit: int) -> list[ChangelogEntry]:
    """Shell out to ``git log`` and parse the pipe-delimited output.

    The argv list is built from literal strings plus the int-coerced
    ``limit`` — no user input ever reaches the subprocess, so this is
    safe against argument injection. We capture stdout via
    :class:`asyncio.subprocess.PIPE` rather than a temp file so the
    parse is purely in-memory.
    """
    cmd = [
        "git",
        "log",
        "--no-color",
        f"--pretty=format:{_GIT_PRETTY_FORMAT}",
        "--no-merges",
        "-n",
        str(limit),
    ]
    process = await asyncio.create_subprocess_exec(
        *cmd,
        cwd=str(_REPO_ROOT),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout_bytes, stderr_bytes = await process.communicate()

    if process.returncode != 0:
        stderr_text = stderr_bytes.decode("utf-8", errors="replace").strip()
        log.warning(
            "changelog.git.exit_nonzero",
            returncode=process.returncode,
            stderr=stderr_text[:400],
        )
        # A non-zero exit (e.g. "not a git repository") shouldn't 500
        # the page — surface it as the same "unavailable" path the
        # missing-binary case takes.
        msg = f"git log exited {process.returncode}"
        raise GitUnavailableError(msg)

    text = stdout_bytes.decode("utf-8", errors="replace")
    entries: list[ChangelogEntry] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        parsed = _parse_line(line)
        if parsed is not None:
            entries.append(parsed)
    return entries


def _parse_line(line: str) -> ChangelogEntry | None:
    """Split one ``git log`` row into a :class:`ChangelogEntry`.

    ``split('|', 3)`` caps the split at four columns so a literal ``|``
    inside the subject gets absorbed into the author field rather than
    silently dropping the row. Malformed lines (fewer than four
    columns) return ``None`` and the caller skips them — defensive,
    because ``git log`` should always emit four columns for the
    format string we hand it.
    """
    parts = line.split("|", 3)
    if len(parts) != 4:
        log.debug("changelog.parse.skip", reason="column-count", line=line[:120])
        return None
    sha, date_iso, subject, author = (p.strip() for p in parts)
    if not sha:
        return None
    return ChangelogEntry(
        sha=sha,
        date_iso=date_iso,
        subject=subject,
        author=author,
        kind=_classify(subject),
    )


def _classify(subject: str) -> str:
    """Map a commit subject to one of the known ``kind`` buckets.

    The prefix is read up to the first ``":"``; an optional
    ``(scope)`` suffix on the prefix is stripped so ``feat(api):`` and
    ``feat:`` both classify as ``"feat"``. Unknown prefixes collapse
    to :data:`_OTHER_KIND` so the UI's filter chip set stays bounded.
    """
    head, sep, _rest = subject.partition(":")
    if not sep:
        return _OTHER_KIND
    token = head.strip().lower()
    # Strip ``(scope)`` — ``feat(api)`` → ``feat``.
    paren_idx = token.find("(")
    if paren_idx >= 0:
        token = token[:paren_idx]
    token = token.strip()
    if token in _KIND_PREFIXES:
        return token
    return _OTHER_KIND


__all__ = [
    "ChangelogEntry",
    "GitUnavailableError",
    "build_changelog",
    "invalidate_cache",
]
