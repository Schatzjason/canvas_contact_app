const LINK_MENU_ID = 'open-in-contact-tracker';
const SPEEDGRADER_MENU_ID = 'open-in-contact-tracker-speedgrader';
const INBOX_MENU_ID = 'open-in-contact-tracker-inbox';

// Matches the two Canvas student-link shapes confirmed so far:
//   .../courses/<course_id>/users/<student_id>   (Discussions, People page)
//   .../courses/<course_id>/grades/<student_id>  (Gradebook)
// Confirmed against real links on 2026-08-31.
const CANVAS_USER_LINK = /^https:\/\/ccsf\.instructure\.com\/courses\/(\d+)\/(?:users|grades)\/(\d+)/;

// SpeedGrader has no student-name link to right-click — the student is
// selected via a JS dropdown button, not an <a href>. Instead we read the
// course_id and student_id straight out of the SpeedGrader page's own URL,
// e.g. .../courses/73658/gradebook/speed_grader?assignment_id=...&student_id=325786
const SPEEDGRADER_URL = /^https:\/\/ccsf\.instructure\.com\/courses\/(\d+)\/gradebook\/speed_grader/;

const TRACKER_BASE_URL = 'http://127.0.0.1:5000';
const STORAGE_DEFAULTS = { reuseTab: true };

chrome.runtime.onInstalled.addListener(() => {
  chrome.contextMenus.create({
    id: LINK_MENU_ID,
    title: 'Open in Contact Tracker',
    contexts: ['link'],
    documentUrlPatterns: ['https://ccsf.instructure.com/*'],
    targetUrlPatterns: [
      'https://ccsf.instructure.com/courses/*/users/*',
      'https://ccsf.instructure.com/courses/*/grades/*',
    ],
  });

  chrome.contextMenus.create({
    id: SPEEDGRADER_MENU_ID,
    title: 'Open in Contact Tracker',
    contexts: ['page'],
    documentUrlPatterns: ['https://ccsf.instructure.com/courses/*/gradebook/speed_grader*'],
  });

  chrome.contextMenus.create({
    id: INBOX_MENU_ID,
    title: 'Open in Contact Tracker',
    contexts: ['page'],
    documentUrlPatterns: ['https://ccsf.instructure.com/conversations*'],
  });
});

async function openTrackerUrl(targetUrl) {
  const { reuseTab } = await chrome.storage.local.get(STORAGE_DEFAULTS);

  if (reuseTab) {
    const existing = await chrome.tabs.query({ url: `${TRACKER_BASE_URL}/*` });
    if (existing.length > 0) {
      const [tab] = existing;
      await chrome.tabs.update(tab.id, { url: targetUrl, active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return;
    }
  }

  chrome.tabs.create({ url: targetUrl });
}

function openStudent(courseId, studentId) {
  return openTrackerUrl(`${TRACKER_BASE_URL}/course/${courseId}/student/${studentId}`);
}

async function openInboxSearch() {
  const { inboxStudentName } = await chrome.storage.local.get('inboxStudentName');
  if (!inboxStudentName) {
    console.warn('Contact Tracker: no student name captured for this click (right-click a conversation row in the list)');
    return;
  }
  await openTrackerUrl(`${TRACKER_BASE_URL}/?q=${encodeURIComponent(inboxStudentName)}`);
}

chrome.contextMenus.onClicked.addListener((info, tab) => {
  if (info.menuItemId === LINK_MENU_ID) {
    const match = CANVAS_USER_LINK.exec(info.linkUrl || '');
    if (!match) {
      console.warn('Contact Tracker: could not parse student link', info.linkUrl);
      return;
    }
    const [, courseId, studentId] = match;
    openStudent(courseId, studentId);
    return;
  }

  if (info.menuItemId === SPEEDGRADER_MENU_ID) {
    const pageUrl = tab && tab.url;
    const courseMatch = SPEEDGRADER_URL.exec(pageUrl || '');
    const studentId = courseMatch && new URL(pageUrl).searchParams.get('student_id');
    if (!courseMatch || !studentId) {
      console.warn('Contact Tracker: could not parse SpeedGrader URL', pageUrl);
      return;
    }
    openStudent(courseMatch[1], studentId);
    return;
  }

  if (info.menuItemId === INBOX_MENU_ID) {
    openInboxSearch();
  }
});
