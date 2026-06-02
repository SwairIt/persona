/**
 * Persona Companion — sends the active tab to a local Persona instance.
 *
 * Default endpoint: http://127.0.0.1:8765/api/companion/tab
 * The local Persona accepts POST {url, title, captured_at}.
 *
 * Sampling: every 60 seconds we check the focused tab. If URL changed,
 * we POST it. No history scraping, no content reading.
 */

const ENDPOINT_DEFAULT = 'http://127.0.0.1:8765/api/companion/tab';
const SAMPLE_INTERVAL_MIN = 1;
let lastUrl = '';

chrome.runtime.onInstalled.addListener(() => {
  chrome.alarms.create('persona-sample', { periodInMinutes: SAMPLE_INTERVAL_MIN });
});

chrome.alarms.onAlarm.addListener(async (alarm) => {
  if (alarm.name !== 'persona-sample') return;
  await sampleActiveTab();
});

async function sampleActiveTab() {
  const tabs = await chrome.tabs.query({ active: true, lastFocusedWindow: true });
  const tab = tabs[0];
  if (!tab || !tab.url || tab.url === lastUrl) return;
  if (!tab.url.startsWith('http')) return;
  lastUrl = tab.url;

  const { endpoint = ENDPOINT_DEFAULT, enabled = true } = await chrome.storage.local.get([
    'endpoint',
    'enabled',
  ]);
  if (!enabled) return;

  try {
    await fetch(endpoint, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        url: tab.url,
        title: tab.title || '',
        captured_at: new Date().toISOString(),
      }),
    });
  } catch (e) {
    /* swallow — local endpoint might be off */
  }
}
