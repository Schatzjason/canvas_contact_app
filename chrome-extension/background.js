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

const STORAGE_DEFAULTS = { reuseTab: true, baseUrl: 'http://127.0.0.1:5000' };

async function getSettings() {
  const settings = await chrome.storage.local.get(STORAGE_DEFAULTS);
  // Strip any trailing slash so URL-building below doesn't produce "//".
  settings.baseUrl = (settings.baseUrl || STORAGE_DEFAULTS.baseUrl).replace(/\/+$/, '');
  return settings;
}

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

async function openTrackerUrl(targetUrl, settings) {
  if (settings.reuseTab) {
    const existing = await chrome.tabs.query({ url: `${settings.baseUrl}/*` });
    if (existing.length > 0) {
      const [tab] = existing;
      await chrome.tabs.update(tab.id, { url: targetUrl, active: true });
      await chrome.windows.update(tab.windowId, { focused: true });
      return;
    }
  }

  chrome.tabs.create({ url: targetUrl });
}

async function openStudent(courseId, studentId) {
  const settings = await getSettings();
  return openTrackerUrl(`${settings.baseUrl}/course/${courseId}/student/${studentId}`, settings);
}

async function openInboxSearch() {
  const settings = await getSettings();
  const { inboxStudentName } = await chrome.storage.local.get('inboxStudentName');
  if (!inboxStudentName) {
    console.warn('Contact Tracker: no student name captured for this click (right-click a conversation row in the list)');
    return;
  }
  await openTrackerUrl(`${settings.baseUrl}/?q=${encodeURIComponent(inboxStudentName)}`, settings);
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
