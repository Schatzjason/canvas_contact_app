const DEFAULTS = { reuseTab: true, baseUrl: 'http://127.0.0.1:5000' };

const checkbox   = document.getElementById('reuseTab');
const baseUrlEl  = document.getElementById('baseUrl');
const status     = document.getElementById('status');

chrome.storage.local.get(DEFAULTS, (items) => {
  checkbox.checked = items.reuseTab;
  baseUrlEl.value  = items.baseUrl;
});

function flashSaved() {
  status.textContent = 'Saved.';
  setTimeout(() => { status.textContent = ''; }, 1500);
}

checkbox.addEventListener('change', () => {
  chrome.storage.local.set({ reuseTab: checkbox.checked }, flashSaved);
});

function saveBaseUrl() {
  var value = baseUrlEl.value.trim().replace(/\/+$/, '');
  if (!value) value = DEFAULTS.baseUrl;
  baseUrlEl.value = value;
  chrome.storage.local.set({ baseUrl: value }, flashSaved);
}

baseUrlEl.addEventListener('blur', saveBaseUrl);
baseUrlEl.addEventListener('keydown', (e) => {
  if (e.key === 'Enter') {
    e.preventDefault();
    baseUrlEl.blur();
  }
});
