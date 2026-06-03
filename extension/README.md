# Persona Capture - Browser Extension

A minimal MV3 extension that sends the current page (or a text selection) to
the local Persona API at `http://localhost:8000/api/bookmarklet/capture`.

## Features

- Right-click context menu: **Save selection to Persona** (works on any
  selection or page).
- Toolbar popup with current page info and a **Save page to Persona** button
  that also picks up the current selection if one exists.

## Prerequisites

- Persona running locally on `http://localhost:8000` with the
  `/api/bookmarklet/capture` endpoint reachable.

## Sideload in Chrome / Edge / Brave / Chromium

1. Open `chrome://extensions`.
2. Toggle **Developer mode** (top-right).
3. Click **Load unpacked**.
4. Select the `extension/` directory of this repo.
5. Pin **Persona Capture** to the toolbar (optional).

## Sideload in Firefox

Firefox supports MV3 with minor differences. To load this extension
temporarily:

1. Open `about:debugging#/runtime/this-firefox`.
2. Click **Load Temporary Add-on...**.
3. Select the `extension/manifest.json` file.

The temporary add-on is unloaded when Firefox restarts. For a permanent
install you need to sign the extension via AMO.

## Usage

- **Context menu:** select some text on any page, right-click, choose
  *Save selection to Persona*.
- **Popup:** click the toolbar icon, then *Save page to Persona*.

A small status line in the popup reports success or HTTP errors. Background
script errors are logged to the service-worker console
(`chrome://extensions` -> *Service worker* link under the extension).

## Files

| File            | Purpose                                                 |
|-----------------|---------------------------------------------------------|
| `manifest.json` | MV3 manifest, permissions, background + action wiring.  |
| `background.js` | Service worker: context-menu registration + fetch POST. |
| `popup.html`    | Toolbar popup UI.                                       |
| `popup.js`      | Popup logic: read active tab + selection, POST capture. |
