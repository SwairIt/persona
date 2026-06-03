// Persona Capture - popup script

const PERSONA_ENDPOINT = "http://localhost:8000/api/bookmarklet/capture";

const titleEl = document.getElementById("page-title");
const urlEl = document.getElementById("page-url");
const btn = document.getElementById("save-btn");
const statusEl = document.getElementById("status");

let currentTab = null;

async function loadActiveTab() {
  const [tab] = await chrome.tabs.query({ active: true, currentWindow: true });
  currentTab = tab || null;
  titleEl.textContent = (tab && tab.title) || "(no title)";
  urlEl.textContent = (tab && tab.url) || "(no url)";
}

async function getSelection(tabId) {
  try {
    const results = await chrome.scripting.executeScript({
      target: { tabId },
      func: () => (window.getSelection ? String(window.getSelection()) : ""),
    });
    return (results && results[0] && results[0].result) || "";
  } catch (err) {
    // scripting permission may be missing on some pages (chrome://, store, etc.)
    return "";
  }
}

function setStatus(msg, kind) {
  statusEl.textContent = msg;
  statusEl.className = kind || "";
}

async function save() {
  if (!currentTab) {
    setStatus("No active tab", "err");
    return;
  }
  btn.disabled = true;
  setStatus("Saving...");

  const selection = chrome.scripting ? await getSelection(currentTab.id) : "";
  const payload = {
    url: currentTab.url || "",
    title: currentTab.title || "",
    selection: selection || "",
  };

  try {
    const res = await fetch(PERSONA_ENDPOINT, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (res.ok) {
      setStatus("Saved.", "ok");
    } else {
      setStatus(`Failed: HTTP ${res.status}`, "err");
    }
  } catch (err) {
    setStatus(`Error: ${err && err.message ? err.message : err}`, "err");
  } finally {
    btn.disabled = false;
  }
}

btn.addEventListener("click", save);
loadActiveTab();
