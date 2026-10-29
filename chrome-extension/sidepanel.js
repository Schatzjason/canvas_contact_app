// Side panel: shows the tracker's compact student panel for whichever
// Outlook message is open. content-outlook.js writes the sender candidates
// to storage; this just turns them into a tracker URL.
const DEFAULTS = { baseUrl: 'http://127.0.0.1:5000' };
const frame = document.getElementById('panel');
const seen  = document.getElementById('seen');
let lastUrl = null;

async function render() {
  const { baseUrl } = await chrome.storage.local.get(DEFAULTS);
  const { outlookSender } = await chrome.storage.local.get('outlookSender');
  const base = (baseUrl || DEFAULTS.baseUrl).replace(/\/+$/, '');
  const params = new URLSearchParams();
  const s = outlookSender || { emails: [], names: [] };
  s.emails.forEach((e) => params.append('email', e));
  s.names.forEach((n) => params.append('name', n));
  const url = `${base}/outlook-panel?${params}`;
  const found = [...s.emails.slice(0, 3), ...s.names.slice(0, 3)];
  seen.textContent = outlookSender
    ? (found.length ? `Detected: ${found.join(', ')}` : 'Detected: nothing in the open message')
    : 'Waiting for an Outlook message (reload the Outlook tab once)';
  if (url !== lastUrl) {   // don't reload (and lose focus in the notes box) for the same sender
    lastUrl = url;
    frame.src = url;
  }
}

chrome.storage.onChanged.addListener((changes, area) => {
  if (area === 'local' && (changes.outlookSender || changes.baseUrl)) render();
});
render();
