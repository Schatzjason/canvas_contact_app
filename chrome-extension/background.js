const MENU_ID = 'open-in-contact-tracker';

// Matches Canvas's canonical per-course user profile link:
// https://ccsf.instructure.com/courses/<course_id>/users/<student_id>
// Confirmed against a real Discussions author link on 2026-08-31.
const CANVAS_USER_LINK = /^https:\/\/ccsf\.instructure\.com\/courses\/(\d+)\/users\/(\d+)/;

// TODO(step 6): move to an options page + chrome.storage instead of hardcoding.
const TRACKER_BASE_URL = 'http://127.0.0.1:5000';

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: MENU_ID,
    title: 'Open in Contact Tracker',
    contexts: ['link'],
    documentUrlPatterns: ['https://ccsf.instructure.com/*'],
    targetUrlPatterns: ['https://ccsf.instructure.com/courses/*/users/*'],
  });
});

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId !== MENU_ID) return;

  const match = CANVAS_USER_LINK.exec(info.linkUrl || '');
  if (!match) {
    console.warn('Contact Tracker: could not parse student link', info.linkUrl);
    return;
  }

  const [, courseId, studentId] = match;
  const targetUrl = `${TRACKER_BASE_URL}/course/${courseId}/student/${studentId}`;
  chrome.tabs.create({ url: targetUrl });
});
