"""Standalone Playwright screenshot — T29.

Run in a SUBPROCESS (never in the server's event loop) so headless
Chromium can't crash or block the web server. Usage:

    python -m app.browse.shot <url> <out_png>

Prints ``OK <page title>`` on success, ``ERR <message>`` on failure, and
exits 0 / non-zero accordingly. The caller (web_browse builtin tool)
reads the PNG and hands it to a vision model for analysis.
"""

from __future__ import annotations

import sys


def main() -> int:
    if len(sys.argv) < 3:
        print("ERR usage: shot.py <url> <out_png>")
        return 2
    url, out = sys.argv[1], sys.argv[2]
    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print("ERR playwright not installed (uv pip install playwright)")
        return 3
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True, args=["--no-sandbox"])
            try:
                page = browser.new_page(viewport={"width": 1280, "height": 900})
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(1500)  # let late content paint
                title = page.title()
                page.screenshot(path=out, full_page=True)
            finally:
                browser.close()
    except Exception as exc:  # noqa: BLE001 — surface any failure to caller
        print("ERR " + str(exc).replace("\n", " ")[:300])
        return 1
    print("OK " + (title or url))
    return 0


if __name__ == "__main__":
    sys.exit(main())
