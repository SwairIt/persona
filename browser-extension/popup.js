const ENDPOINT_DEFAULT = 'http://127.0.0.1:8765/api/companion/tab';

async function load() {
  const { endpoint = ENDPOINT_DEFAULT, enabled = true } = await chrome.storage.local.get([
    'endpoint',
    'enabled',
  ]);
  document.getElementById('endpoint').value = endpoint;
  document.getElementById('enabled').checked = enabled;
}

async function save() {
  const endpoint = document.getElementById('endpoint').value.trim() || ENDPOINT_DEFAULT;
  const enabled = document.getElementById('enabled').checked;
  await chrome.storage.local.set({ endpoint, enabled });
  document.getElementById('status').textContent = 'Saved · ' + new Date().toLocaleTimeString();
}

document.getElementById('save').addEventListener('click', save);
load();
