# Persona Companion (browser extension)

Sends the URL + title of the focused tab to a local Persona instance every
minute. Useful when most of your work happens in the browser and screen
OCR alone can't tell pages apart.

## Privacy

- Only the **URL and title** of the **active** tab leave the browser.
- No page DOM, no cookies, no history sweep.
- Sends to **127.0.0.1 only** (or the URL you put in the popup).
- No third-party hosts in the manifest, so the browser sandbox blocks any
  egress beyond what's listed.

## Install (Chromium-based browsers)

1. Open `chrome://extensions/`
2. Toggle **Developer mode** in the top-right
3. **Load unpacked** → choose this `browser-extension/` folder
4. Click the puzzle-piece icon, pin the **Persona Companion**
5. Open the popup, confirm the endpoint (default
   `http://127.0.0.1:8765/api/companion/tab`)
6. Make sure your Persona instance is running. Visit
   `http://127.0.0.1:8765/companion/tabs` to see ingested tabs.

## Install (Firefox)

`manifest_version: 3` is supported by Firefox 109+. Steps:

1. Open `about:debugging#/runtime/this-firefox`
2. **Load Temporary Add-on…** → pick `manifest.json` inside this folder
3. Same popup config as above

The temporary add-on disappears on Firefox restart unless you sign it
through addons.mozilla.org.

## Disabling

The popup has a checkbox to stop sending. Or remove the extension.
