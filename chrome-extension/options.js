const DEFAULTS = { reuseTab: true };

const checkbox = document.getElementById('reuseTab');
const status = document.getElementById('status');

chrome.storage.local.get(DEFAULTS, (items) => {
  checkbox.checked = items.reuseTab;
});

checkbox.addEventListener('change', () => {
  chrome.storage.local.set({ reuseTab: checkbox.checked }, () => {
    status.textContent = 'Saved.';
    setTimeout(() => { status.textContent = ''; }, 1500);
  });
});
