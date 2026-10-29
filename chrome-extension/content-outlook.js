// Runs in Outlook on the web. Reads who sent the open message and stashes the
// candidates (emails + display names, in page order, sender first) for the
// side panel. The tracker decides which candidate is a student; the
// instructor's own address and anyone else simply don't match.
//
// Outlook's class names are generated and change without notice, so this
// avoids them: it looks inside the reading pane and reads title/aria-label
// attributes and the first lines of text. If the sender isn't being found,
// the side panel shows what was detected ("Detected: ...") to debug with.
const EMAIL_RE = /[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}/gi;
const PANE_SELECTORS = [
  '[data-app-section="ConversationContainer"]',
  '#ConversationReadingPaneContainer',
  '[aria-label="Reading Pane"]',
  '[role="main"]',
];
const MAX = 10;

function findPane() {
  for (const sel of PANE_SELECTORS) {
    const el = document.querySelector(sel);
    if (el) return el;
  }
  return null;
}

function collect() {
  const emails = [];
  const names = [];
  const add = (list, v) => {
    v = v.trim();
    if (v && !list.some((x) => x.toLowerCase() === v.toLowerCase()) && list.length < MAX) list.push(v);
  };
  const pane = findPane();
  if (!pane) return { emails, names };

  // Header attributes first (sender persona/tooltips), then top-of-pane text.
  for (const el of pane.querySelectorAll('[title],[aria-label]')) {
    for (const attr of [el.getAttribute('title'), el.getAttribute('aria-label')]) {
      if (!attr) continue;
      const found = attr.match(EMAIL_RE);
      if (found) found.forEach((e) => add(emails, e));
      else if (/^[A-Za-z][A-Za-z'.\-]+(?: [A-Za-z][A-Za-z'.\-]+){1,3}$/.test(attr.trim())) add(names, attr);
    }
  }
  (pane.innerText || '').slice(0, 800).match(EMAIL_RE)?.forEach((e) => add(emails, e));
  return { emails, names };
}

let lastSig = null;
let timer = null;
function publish() {
  const data = collect();
  const sig = JSON.stringify(data);
  if (sig === lastSig) return;
  lastSig = sig;
  chrome.storage.local.set({ outlookSender: data });
}

new MutationObserver(() => {
  clearTimeout(timer);
  timer = setTimeout(publish, 400);
}).observe(document.body, { childList: true, subtree: true });
publish();
