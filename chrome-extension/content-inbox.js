// Runs on the Canvas Inbox page. Canvas's conversation list items have no
// stable link/id to read a student's Canvas user id from (React SPA, no
// per-conversation URL state), so this captures a best-effort NAME instead:
// on right-click, find the nearest conversation row and read its participant
// heading (e.g. "Miguel Isip, Jason Schatz"), strip the instructor's own
// name, and stash whatever's left for background.js to read when the
// context menu item is clicked. The tracker app resolves the name via its
// own search — duplicates/near-misses just show up in the results there.
//
// data-testid="conversation" is one of Canvas's own test hooks (confirmed
// 2026-09-02) and much more stable than its hashed CSS class names.
const INSTRUCTOR_NAME = 'Jason Schatz';

document.addEventListener('contextmenu', (e) => {
  const row = e.target.closest('[data-testid="conversation"]');
  const heading = row && row.querySelector('h2');
  const text = heading ? heading.textContent.trim() : '';

  const otherNames = text
    .split(',')
    .map((s) => s.trim())
    .filter((name) => name && name !== INSTRUCTOR_NAME);

  // chrome.storage.session is blocked from content-script contexts by
  // default, so this uses .local instead — the value is always overwritten
  // on the next right-click, so staleness isn't a concern.
  chrome.storage.local.set({ inboxStudentName: otherNames[0] || null });
});
