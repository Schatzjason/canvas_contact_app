import hashlib
import json
import pathlib
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient

# Stored as CanvasCache rows with these keys; cleared automatically when user flushes cache.
_MARKER_TTL = 10 * 365 * 24 * 3600  # ~10 years — never expires on its own


def _get_sync_marker(course_id, scope):
    """Return the datetime of the last live Canvas fetch for this scope, or None."""
    key = f'sync_marker:{scope}:{course_id}'
    entry = CanvasCache.query.filter_by(cache_key=key).first()
    return entry.fetched_at if entry else None


def _set_sync_marker(course_id, scope):
    """Record now as the last live Canvas fetch time for this scope."""
    key = f'sync_marker:{scope}:{course_id}'
    now = datetime.now(timezone.utc)
    entry = CanvasCache.query.filter_by(cache_key=key).first()
    if entry:
        entry.fetched_at = now
    else:
        db.session.add(CanvasCache(
            cache_key=key,
            response_json=[],
            fetched_at=now,
            ttl_seconds=_MARKER_TTL,
        ))
    db.session.commit()


def _enrollment_cache_key(course_id):
    """Return the canvas_cache key for enrollment listings (mirrors CanvasClient._make_cache_key)."""
    params = {'type[]': 'StudentEnrollment', 'state[]': 'active'}
    payload = json.dumps({
        'path': f'/api/v1/courses/{course_id}/enrollments',
        'params': sorted(params.items()),
    })
    return hashlib.sha256(payload.encode()).hexdigest()


FIXTURES_DIR = pathlib.Path(__file__).parent.parent.parent / 'fixtures'


def inject_fixture_responses(course_id):
    """Upsert fabricated instructor replies from a JSON fixture file."""
    fixture_path = FIXTURES_DIR / f'discussion_responses_{course_id}.json'
    if not fixture_path.exists():
        return 0
    entries = json.loads(fixture_path.read_text())
    events = [
        {
            'course_id': e['course_id'],
            'student_canvas_id': e['student_canvas_id'],
            'event_type': 'discussion_instructor_reply',
            'occurred_at': datetime.fromisoformat(e['entry_occurred_at']),
            'source_id': e['source_id'],
        }
        for e in entries
        if e.get('response', '').strip()
    ]
    if not events:
        return 0
    stmt = pg_insert(InteractionEvent.__table__).values(events)
    stmt = stmt.on_conflict_do_update(
        constraint='uq_interaction_event_type_source_student',
        set_={'occurred_at': stmt.excluded.occurred_at},
    )
    db.session.execute(stmt)
    db.session.commit()
    return len(events)


# ---------------------------------------------------------------------------
# Phase functions — each puts progress messages on a Queue and returns events
# ---------------------------------------------------------------------------

def _phase_conversations(client, course_id, student_ids, cutoff, progress_q):
    phase = 'conversations'
    progress_q.put({'status': 'start', 'phase': phase})
    t0 = time.perf_counter()
    events = []
    matched = 0
    try:
        last_sync = _get_sync_marker(course_id, 'conv_sent')
        since = last_sync if last_sync else cutoff
        conversations = []
        fetched_live = False
        page_n = 0
        for page, is_cached in client.stream_conversations(since=since):
            conversations.extend(page)
            if is_cached:
                progress_q.put({'status': 'cached', 'phase': phase})
            else:
                fetched_live = True
                page_n += 1
                progress_q.put({'status': 'page', 'phase': phase, 'n': page_n, 'count': len(page)})
        if fetched_live:
            _set_sync_marker(course_id, 'conv_sent')
        for conv in conversations:
            ts_str = conv.get('last_authored_at') or conv.get('last_message_at')
            if not ts_str:
                continue
            occurred_at = datetime.fromisoformat(ts_str)
            participant_ids = {p['id'] for p in conv.get('participants', [])}
            for sid in participant_ids & student_ids:
                events.append({
                    'course_id': course_id,
                    'student_canvas_id': sid,
                    'event_type': 'conversation',
                    'occurred_at': occurred_at,
                    'source_id': conv['id'],
                })
                matched += 1
    except Exception as exc:
        progress_q.put({'status': 'error', 'phase': phase, 'msg': str(exc)})
    progress_q.put({'status': 'done_phase', 'phase': phase, 'count': matched,
                    'elapsed_ms': int((time.perf_counter() - t0) * 1000)})
    return events


def _phase_student_messages(client, course_id, student_ids, cutoff, progress_q):
    phase = 'student_messages'
    progress_q.put({'status': 'start', 'phase': phase})
    t0 = time.perf_counter()
    events = []
    matched = 0
    try:
        last_sync = _get_sync_marker(course_id, 'conv_inbox')
        since = last_sync if last_sync else cutoff
        inbox = []
        fetched_live = False
        page_n = 0
        for page, is_cached in client.stream_conversations(since=since, scope='inbox'):
            inbox.extend(page)
            if is_cached:
                progress_q.put({'status': 'cached', 'phase': phase})
            else:
                fetched_live = True
                page_n += 1
                progress_q.put({'status': 'page', 'phase': phase, 'n': page_n, 'count': len(page)})
        if fetched_live:
            _set_sync_marker(course_id, 'conv_inbox')
        for conv in inbox:
            ts_str = conv.get('last_message_at')
            if not ts_str:
                continue
            occurred_at = datetime.fromisoformat(ts_str)
            participant_ids = {p['id'] for p in conv.get('participants', [])}
            for sid in participant_ids & student_ids:
                events.append({
                    'course_id': course_id,
                    'student_canvas_id': sid,
                    'event_type': 'student_message',
                    'occurred_at': occurred_at,
                    'source_id': conv['id'],
                })
                matched += 1
    except Exception as exc:
        progress_q.put({'status': 'error', 'phase': phase, 'msg': str(exc)})
    progress_q.put({'status': 'done_phase', 'phase': phase, 'count': matched,
                    'elapsed_ms': int((time.perf_counter() - t0) * 1000)})
    return events


def _phase_discussions(client, course_id, student_ids, cutoff, instructor_id, progress_q):
    phase = 'discussions'
    progress_q.put({'status': 'start', 'phase': phase})
    t0 = time.perf_counter()
    events = []
    matched = 0
    try:
        topics = client.get_discussion_topics(course_id)
        for i, topic in enumerate(topics, 1):
            progress_q.put({'status': 'page', 'phase': phase, 'n': i,
                            'total': len(topics), 'topic': topic.get('title', '')})
            entries = client.get_discussion_entries(course_id, topic['id'])
            for entry in entries:
                entry_at = datetime.fromisoformat(entry['created_at'])
                if entry_at >= cutoff and entry.get('user_id') in student_ids:
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': entry['user_id'],
                        'event_type': 'discussion_entry',
                        'occurred_at': entry_at,
                        'source_id': entry['id'],
                    })
                    matched += 1
                for reply in entry.get('recent_replies', []):
                    reply_at = datetime.fromisoformat(reply['created_at'])
                    reply_author = reply.get('user_id')
                    if reply_at < cutoff:
                        continue
                    if reply_author in student_ids:
                        events.append({
                            'course_id': course_id,
                            'student_canvas_id': reply_author,
                            'event_type': 'discussion_reply',
                            'occurred_at': reply_at,
                            'source_id': reply['id'],
                        })
                        matched += 1
                    elif reply_author == instructor_id and entry.get('user_id') in student_ids:
                        events.append({
                            'course_id': course_id,
                            'student_canvas_id': entry['user_id'],
                            'event_type': 'discussion_instructor_reply',
                            'occurred_at': reply_at,
                            'source_id': reply['id'],
                        })
                        matched += 1
    except Exception as exc:
        progress_q.put({'status': 'error', 'phase': phase, 'msg': str(exc)})
    progress_q.put({'status': 'done_phase', 'phase': phase, 'count': matched,
                    'elapsed_ms': int((time.perf_counter() - t0) * 1000)})
    return events


def _phase_submissions(client, course_id, student_ids, cutoff, progress_q):
    phase = 'submissions'
    progress_q.put({'status': 'start', 'phase': phase})
    t0 = time.perf_counter()
    events = []
    matched = 0
    try:
        all_assignments = client.get_assignments(course_id)
        skip_types = {'discussion_topic', 'online_quiz'}
        assignments = [
            a for a in all_assignments
            if not skip_types.intersection(a.get('submission_types', []))
        ]
        for i, assignment in enumerate(assignments, 1):
            progress_q.put({'status': 'page', 'phase': phase, 'n': i,
                            'total': len(assignments),
                            'assignment': assignment.get('name', '')})
            submissions = client.get_submissions(course_id, assignment['id'])
            for sub in submissions:
                if sub.get('workflow_state') in ('unsubmitted', None):
                    continue
                submitted_at = sub.get('submitted_at')
                if not submitted_at:
                    continue
                sub_at = datetime.fromisoformat(submitted_at)
                if sub_at < cutoff:
                    continue
                if sub.get('user_id') not in student_ids:
                    continue
                events.append({
                    'course_id': course_id,
                    'student_canvas_id': sub['user_id'],
                    'event_type': 'submission',
                    'occurred_at': sub_at,
                    'source_id': sub['id'],
                })
                matched += 1
    except Exception as exc:
        progress_q.put({'status': 'error', 'phase': phase, 'msg': str(exc)})
    progress_q.put({'status': 'done_phase', 'phase': phase, 'count': matched,
                    'elapsed_ms': int((time.perf_counter() - t0) * 1000)})
    return events


# ---------------------------------------------------------------------------
# Main sync generator
# ---------------------------------------------------------------------------

def sync_course(course_id):
    """Generator that syncs Canvas data for course_id into interaction_event.

    Yields progress dicts:
      {'status': 'start',      'phase': str}
      {'status': 'cached',     'phase': str}
      {'status': 'page',       'phase': str, 'n': int, ['topic': str]}
      {'status': 'done_phase', 'phase': str, 'count': int, 'elapsed_ms': int}
      {'status': 'error',      'phase': str, 'msg': str}
      {'status': 'done',       'count': int, 'students': int, 'elapsed_ms': int}
    """
    app = current_app._get_current_object()
    client = CanvasClient()
    today = datetime.now(timezone.utc).date()
    cutoff = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=21)
    t_start = time.perf_counter()

    # ── Phase: enrollments (serial — must complete before parallel phases) ──
    phase = 'enrollments'
    yield {'status': 'start', 'phase': phase}
    t0 = time.perf_counter()
    try:
        enr_entry = CanvasCache.query.filter_by(cache_key=_enrollment_cache_key(course_id)).first()
        enr_cached = enr_entry is not None and enr_entry.is_fresh()
        if enr_cached:
            yield {'status': 'cached', 'phase': phase}
        enrollments = client.get_enrollments(course_id)
        student_ids = {e['user_id'] for e in enrollments}
        if not enr_cached:
            yield {'status': 'page', 'phase': phase, 'n': 1}
    except Exception as exc:
        yield {'status': 'error', 'phase': phase, 'msg': str(exc)}
        return
    yield {'status': 'done_phase', 'phase': phase, 'count': len(student_ids),
           'elapsed_ms': int((time.perf_counter() - t0) * 1000)}

    if not student_ids:
        yield {'status': 'done', 'count': 0, 'students': 0,
               'elapsed_ms': int((time.perf_counter() - t_start) * 1000)}
        return

    instructor_id = client.get_current_user()['id']

    # ── Parallel phases ──────────────────────────────────────────────────────
    progress_q = queue.Queue()

    def _in_context(fn, *args):
        with app.app_context():
            return fn(*args)

    with ThreadPoolExecutor(max_workers=4) as executor:
        futures = [
            executor.submit(_in_context, _phase_conversations,
                            client, course_id, student_ids, cutoff, progress_q),
            executor.submit(_in_context, _phase_student_messages,
                            client, course_id, student_ids, cutoff, progress_q),
            executor.submit(_in_context, _phase_discussions,
                            client, course_id, student_ids, cutoff, instructor_id, progress_q),
            executor.submit(_in_context, _phase_submissions,
                            client, course_id, student_ids, cutoff, progress_q),
        ]

        # Yield progress messages as phases run
        while not all(f.done() for f in futures):
            try:
                yield progress_q.get(timeout=0.05)
            except queue.Empty:
                continue

        # Drain remaining messages after all threads complete
        while True:
            try:
                yield progress_q.get_nowait()
            except queue.Empty:
                break

        # Collect events from all phases
        events = []
        for f in futures:
            try:
                events.extend(f.result())
            except Exception:
                pass  # errors already reported via progress_q

    # ── Phase: saving (serial) ────────────────────────────────────────────────
    phase = 'saving'
    yield {'status': 'start', 'phase': phase}
    t0 = time.perf_counter()
    try:
        if events:
            stmt = pg_insert(InteractionEvent.__table__).values(events)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_interaction_event_type_source_student',
                set_={'occurred_at': stmt.excluded.occurred_at},
            )
            db.session.execute(stmt)
            db.session.commit()
    except Exception as exc:
        yield {'status': 'error', 'phase': phase, 'msg': str(exc)}
    inject_fixture_responses(course_id)
    yield {'status': 'done_phase', 'phase': phase, 'count': len(events),
           'elapsed_ms': int((time.perf_counter() - t0) * 1000)}

    students_touched = len({e['student_canvas_id'] for e in events})
    yield {'status': 'done', 'count': len(events), 'students': students_touched,
           'elapsed_ms': int((time.perf_counter() - t_start) * 1000)}


def run_sync(course_id):
    """Consume sync_course to completion without streaming progress. Returns event count."""
    for msg in sync_course(course_id):
        if msg.get('status') == 'done':
            return msg.get('count', 0)
    return 0
