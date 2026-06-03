// Persona Capture - background service worker (MV3)
// Registers a context-menu item and forwards selection to the local Persona API.

const PERSONA_ENDPOINT = "http://localhost:8000/api/bookmarklet/capture";
const MENU_ID = "persona-save-selection";

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: "Save selection to Persona",
    contexts: ["selection", "page"],
  });
});

chrome.contextMenus.onClicked.addListener(async (info, tab) => {
  if (info.menuItemId !== MENU_ID) return;

  const payload = {
    url: info.pageUrl || (tab && tab.url) || "",
    title: (tab && tab.title) || "",
    selection: info.selectionText || "",
  };

  try {
    const res = await fetch(PERSONA_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (!res.ok) {
      console.error("[Persona] capture failed:", res.status, await res.text());
    }
  } catch (err) {
    console.error("[Persona] capture error:", err);
  }
});
