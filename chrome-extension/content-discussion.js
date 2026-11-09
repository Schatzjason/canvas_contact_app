// Runs on Canvas discussion topic pages. Adds a "Save to Discussion Helper"
// link under each of the instructor's own top-level posts.
//
// Post data (id, body, board name) comes from Canvas's own API, which the
// page's session cookie can already read — nothing is scraped from the DOM
// except to find WHERE to put the link. That placement is the fragile part:
// Canvas's discussion UI has no stable per-post ids in the markup, so each
// post is located by matching the start of its text, then the first "Reply"
// control after it (a post's own action row comes before its nested replies).
const TOPIC_PATH = /^\/courses\/(\d+)\/discussion_topics\/(\d+)/;
const LABEL = 'Save to Discussion Helper';

const norm = (s) => s.replace(/\s+/g, ' ').trim();

// DOMParser keeps the parsed HTML inert (no image loads / handlers firing).
function plainText(html) {
  const doc = new DOMParser().parseFromString(html || '', 'text/html');
  return norm(doc.body.textContent || '');
}

// Canvas prefixes JSON for cookie-authenticated requests with "while(1);".
async function canvasJson(path) {
  const resp = await fetch(path, { credentials: 'same-origin' });
  if (!resp.ok) throw new Error(`${path} returned ${resp.status}`);
  return JSON.parse((await resp.text()).replace(/^while\(1\);/, ''));
}

// Deepest element whose text contains `snippet`.
function findTextElement(snippet) {
  let found = null;
  for (const el of document.querySelectorAll('main p, main div, main span, main li')) {
    if (!norm(el.textContent).includes(snippet)) continue;
    if (!found || found.contains(el)) found = el;  // later matches nested inside = deeper
  }
  return found;
}

function firstReplyAfter(el) {
  const controls = document.querySelectorAll('button, a, [role="button"]');
  for (const c of controls) {
    if (norm(c.textContent) !== 'Reply') continue;
    if (el.contains(c)) continue;
    if (el.compareDocumentPosition(c) & Node.DOCUMENT_POSITION_FOLLOWING) return c;
  }
  return null;
}

// The element that sits in the action row alongside its siblings.
function actionUnit(control) {
  let unit = control;
  while (unit.parentElement && unit.parentElement.children.length === 1) unit = unit.parentElement;
  return unit;
}

function makeLink(template, onClick) {
  const link = template.cloneNode(true);
  link.removeAttribute('id');
  link.querySelectorAll('[id]').forEach((n) => n.removeAttribute('id'));
  const inner = link.matches('button, a') ? link : (link.querySelector('button, a') || link);
  inner.removeAttribute('href');
  inner.textContent = LABEL;
  inner.style.cursor = 'pointer';
  inner.addEventListener('click', (e) => {
    e.preventDefault();
    e.stopPropagation();
    onClick(inner);
  });
  return link;
}

function save(ctx, entry, label) {
  const original = LABEL;
  label.textContent = 'Saving…';
  chrome.runtime.sendMessage({
    type: 'saveDiscussionPost',
    payload: {
      course_id: ctx.courseId,
      topic_id: ctx.topicId,
      entry_id: entry.id,
      body: entry.message,
      board_name: ctx.title,
    },
  }, (data) => {
    const err = chrome.runtime.lastError;
    if (err || !data || !data.ok) {
      label.textContent = `Not saved: ${(data && data.error) || 'app not reachable'}`;
    } else {
      label.textContent = data.created ? 'Saved ✓' : 'Updated ✓';
    }
    setTimeout(() => { label.textContent = original; }, 3000);
  });
}

async function loadContext() {
  const m = TOPIC_PATH.exec(location.pathname);
  if (!m) return null;
  const [courseId, topicId] = [m[1], m[2]];
  const base = `/api/v1/courses/${courseId}/discussion_topics/${topicId}`;
  const [me, topic, view] = await Promise.all([
    canvasJson('/api/v1/users/self'),
    canvasJson(base),
    canvasJson(`${base}/view`),
  ]);
  const mine = (view.view || []).filter(
    (e) => e.user_id === me.id && !e.deleted && e.message && plainText(e.message));
  return { courseId, topicId, title: topic.title || '', entries: mine };
}

function placeLinks(ctx) {
  for (const entry of ctx.entries) {
    if (document.querySelector(`[data-ct-entry="${entry.id}"]`)) continue;
    const body = findTextElement(plainText(entry.message).slice(0, 40));
    const reply = body && firstReplyAfter(body);
    if (!reply) continue;  // not rendered yet; the observer will retry
    const unit = actionUnit(reply);
    let anchor = unit;
    for (const sib of unit.parentElement.children) {
      if (/^Mark as (Un)?[Rr]ead$/.test(norm(sib.textContent))) anchor = sib;
    }
    const link = makeLink(anchor, (label) => save(ctx, entry, label));
    link.dataset.ctEntry = entry.id;
    anchor.after(link);
  }
}

(async function init() {
  let ctx;
  try {
    ctx = await loadContext();
  } catch (e) {
    console.warn('Contact Tracker: could not load discussion data', e);
    return;
  }
  if (!ctx || !ctx.entries.length) return;

  let timer = null;
  const schedule = () => {
    clearTimeout(timer);
    timer = setTimeout(() => placeLinks(ctx), 400);
  };
  new MutationObserver(schedule).observe(document.body, { childList: true, subtree: true });
  schedule();
})();
