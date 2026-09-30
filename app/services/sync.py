import hashlib
import json
import queue
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from flask import current_app
from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.check_back_date import CheckBackDate
from app.models.interaction_event import InteractionEvent
from app.models.student_record import StudentRecord
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


# ---------------------------------------------------------------------------
# Phase functions — each puts progress messages on a Queue and returns events
# ---------------------------------------------------------------------------

def _latest_activity(conv):
    """Newest message timestamp a conversation summary reports, as a datetime."""
    stamps = [conv[k] for k in ('last_message_at', 'last_authored_message_at') if conv.get(k)]
    return max(datetime.fromisoformat(t) for t in stamps) if stamps else None


def _phase_conversations(client, course_id, student_ids, cutoff, instructor_id, progress_q):
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

        # The list view only gives a thread-level summary (one shared id per
        # conversation, one "latest activity" timestamp) — Canvas threads all
        # messages to the same recipient together, so a student messaged
        # twice on different days would collapse onto one row keyed by that
        # shared id. Fetch each relevant thread's full detail (real per-
        # message ids/timestamps/authors) so every distinct message gets its
        # own event instead of overwriting the last one.
        for conv in conversations:
            summary_ts = conv.get('last_authored_message_at') or conv.get('last_message_at')
            if not summary_ts or datetime.fromisoformat(summary_ts) < since:
                continue
            participant_ids = {p['id'] for p in conv.get('participants', [])}
            relevant_students = participant_ids & student_ids
            if not relevant_students:
                continue

            try:
                detail = client.get_conversation(conv['id'], newer_than=_latest_activity(conv))
            except Exception:
                continue

            for msg in detail.get('messages', []):
                if msg.get('author_id') != instructor_id:
                    continue
                created_str = msg.get('created_at')
                if not created_str:
                    continue
                occurred_at = datetime.fromisoformat(created_str)
                if occurred_at < since:
                    continue
                for sid in relevant_students:
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': sid,
                        'event_type': 'conversation',
                        'occurred_at': occurred_at,
                        'source_id': msg['id'],
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

        # Same reasoning as _phase_conversations: fetch per-message detail so
        # repeat messages from the same student don't collapse onto one row.
        for conv in inbox:
            summary_ts = conv.get('last_message_at')
            if not summary_ts or datetime.fromisoformat(summary_ts) < since:
                continue
            participant_ids = {p['id'] for p in conv.get('participants', [])}
            relevant_students = participant_ids & student_ids
            if not relevant_students:
                continue

            try:
                detail = client.get_conversation(conv['id'], newer_than=_latest_activity(conv))
            except Exception:
                continue

            for msg in detail.get('messages', []):
                author_id = msg.get('author_id')
                if author_id not in relevant_students:
                    continue
                created_str = msg.get('created_at')
                if not created_str:
                    continue
                occurred_at = datetime.fromisoformat(created_str)
                if occurred_at < since:
                    continue
                events.append({
                    'course_id': course_id,
                    'student_canvas_id': author_id,
                    'event_type': 'student_message',
                    'occurred_at': occurred_at,
                    'source_id': msg['id'],
                })
                matched += 1
    except Exception as exc:
        progress_q.put({'status': 'error', 'phase': phase, 'msg': str(exc)})
    progress_q.put({'status': 'done_phase', 'phase': phase, 'count': matched,
                    'elapsed_ms': int((time.perf_counter() - t0) * 1000)})
    return events


def _topic_due_at(topic):
    """Extract the effective due date from a discussion topic, or None."""
    # Graded discussions have an embedded assignment with a due_at
    assignment = topic.get('assignment')
    if assignment and assignment.get('due_at'):
        return datetime.fromisoformat(assignment['due_at'])
    # Ungraded discussions may have a lock_at (closes for new posts)
    if topic.get('lock_at'):
        return datetime.fromisoformat(topic['lock_at'])
    return None


def _phase_discussions(client, course_id, student_ids, cutoff, instructor_id, progress_q,
                       refresh=False):
    phase = 'discussions'
    progress_q.put({'status': 'start', 'phase': phase})
    t0 = time.perf_counter()
    events = []
    matched = 0
    try:
        # `refresh` bypasses the cache; kwargs only when set so plain syncs call as before.
        kw = {'refresh': True} if refresh else {}
        all_topics = client.get_discussion_topics(course_id, **kw)

        # Only fetch entries for topics that are relevant: due date is
        # between the cutoff and a week from now.  Topics with no due date
        # are always included (could have activity at any time).
        future_limit = datetime.now(timezone.utc) + timedelta(days=7)
        topics = []
        for t in all_topics:
            due = _topic_due_at(t)
            if due is None or (due >= cutoff and due <= future_limit):
                topics.append(t)

        for i, topic in enumerate(topics, 1):
            progress_q.put({'status': 'page', 'phase': phase, 'n': i,
                            'total': len(topics), 'topic': topic.get('title', '')})
            entries = client.get_discussion_entries(course_id, topic['id'], **kw)
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

        # Skip assignments due more than a week in the future — no submissions
        # expected yet.  Past assignments are always included because students
        # frequently submit late work.  Assignments with no due date are kept.
        future_limit = datetime.now(timezone.utc) + timedelta(days=7)
        assignments = []
        for a in all_assignments:
            if skip_types.intersection(a.get('submission_types', [])):
                continue
            due_str = a.get('due_at')
            if due_str:
                due = datetime.fromisoformat(due_str)
                if due > future_limit:
                    continue
            assignments.append(a)

        for i, assignment in enumerate(assignments, 1):
            progress_q.put({'status': 'page', 'phase': phase, 'n': i,
                            'total': len(assignments),
                            'assignment': assignment.get('name', '')})
            submissions = client.get_submissions(course_id, assignment['id'])
            for sub in submissions:
                # Only count submissions the student actually made —
                # skip graded-only entries (e.g. instructor entered a grade
                # for attendance/participation without a student upload).
                if not sub.get('attempt'):
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


def _course_cutoff(course_obj, today):
    """Earliest timestamp worth syncing: the course start, else 365 days back."""
    fallback = datetime(today.year, today.month, today.day,
                        tzinfo=timezone.utc) - timedelta(days=365)
    start_str = course_obj.get('start_at') if course_obj else None
    if not start_str:
        return fallback
    try:
        return datetime.fromisoformat(start_str.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return fallback


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
    # Sync the whole term so late submissions from way-behind students are
    # captured. Falls back to a generous 365-day window if Canvas returns no
    # course start_at. The cutoff applies to event timestamps (sent/posted/
    # submitted), not assignment due dates — late work submitted today still
    # appears regardless of how old the assignment is.
    try:
        course_obj = client.get_course(course_id)
    except Exception:
        course_obj = None
    cutoff = _course_cutoff(course_obj, today)
    # When a course concludes, Canvas drops every enrollment (including
    # students') out of the 'active' state we filter on below. Without this
    # guard that would look like every student withdrew on the same day —
    # flipping them all to 'dropped' and wiping their check-back dates.
    concluded = bool(course_obj) and course_obj.get('workflow_state') in ('completed', 'deleted')
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

    # ── Upsert student records and detect drops ─────────────────────────────
    if enrollments:
        records = []
        for e in enrollments:
            u = e.get('user', {})
            records.append({
                'course_id': course_id,
                'student_canvas_id': e['user_id'],
                'name': u.get('name', f'Student {e["user_id"]}'),
                'sortable_name': u.get('sortable_name') or u.get('name', f'Student {e["user_id"]}'),
                'status': 'active',
                'dropped_at': None,
            })
        stmt = pg_insert(StudentRecord.__table__).values(records)
        stmt = stmt.on_conflict_do_update(
            constraint='uq_student_record',
            set_={
                'name': stmt.excluded.name,
                'sortable_name': stmt.excluded.sortable_name,
                'status': 'active',
                'dropped_at': None,
            },
        )
        db.session.execute(stmt)

    # Mark students no longer in the enrollment as dropped — skipped for
    # concluded courses, see `concluded` above.
    if not concluded:
        now_utc = datetime.now(timezone.utc)
        newly_dropped = StudentRecord.query.filter(
            StudentRecord.course_id == course_id,
            StudentRecord.status == 'active',
            ~StudentRecord.student_canvas_id.in_(student_ids) if student_ids else True,
        ).all()
        for rec in newly_dropped:
            rec.status = 'dropped'
            rec.dropped_at = now_utc

        # Remove check-back dates for dropped students
        if newly_dropped:
            dropped_ids = [rec.student_canvas_id for rec in newly_dropped]
            CheckBackDate.query.filter(
                CheckBackDate.course_id == course_id,
                CheckBackDate.student_canvas_id.in_(dropped_ids),
            ).delete(synchronize_session=False)

    db.session.commit()

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
                            client, course_id, student_ids, cutoff, instructor_id, progress_q),
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


# ---------------------------------------------------------------------------
# Single-student refresh
# ---------------------------------------------------------------------------

STUDENT_REFRESH_MAX_AGE = timedelta(hours=1)


def _student_scope(student_id):
    return f'student_{student_id}'


def last_student_refresh(course_id, student_id):
    """When this student's data was last pulled from Canvas, or None.

    A whole-course sync covers every student, so its marker counts too."""
    stamps = [t for t in (
        _get_sync_marker(course_id, _student_scope(student_id)),
        _get_sync_marker(course_id, 'conv_inbox'),
    ) if t]
    return max(stamps) if stamps else None


def student_needs_refresh(course_id, student_id):
    last = last_student_refresh(course_id, student_id)
    return last is None or datetime.now(timezone.utc) - last > STUDENT_REFRESH_MAX_AGE


def _student_conversation_events(client, course_id, student_id, instructor_id):
    """Messages in every thread shared with the student: the instructor's become
    'conversation' events and the student's own 'student_message' events."""
    threads = {}
    for scope in ('sent', 'inbox'):
        for conv in client.get_student_conversations(student_id, scope):
            threads[conv['id']] = conv
    events = []
    for conv_id, conv in threads.items():
        detail = client.get_conversation(conv_id, newer_than=_latest_activity(conv))
        for msg in detail.get('messages', []):
            if not msg.get('created_at'):
                continue
            author = msg.get('author_id')
            if author == instructor_id:
                event_type = 'conversation'
            elif author == student_id:
                event_type = 'student_message'
            else:
                continue
            events.append({
                'course_id': course_id,
                'student_canvas_id': student_id,
                'event_type': event_type,
                'occurred_at': datetime.fromisoformat(msg['created_at']),
                'source_id': msg['id'],
            })
    return events


def _student_submission_events(client, course_id, student_id, cutoff):
    skip_types = {'discussion_topic', 'online_quiz'}
    events = []
    for sub in client.get_student_submissions(course_id, student_id):
        # Same rule as _phase_submissions: only work the student actually turned in.
        if not sub.get('attempt') or not sub.get('submitted_at'):
            continue
        if sub.get('submission_type') in skip_types:
            continue
        submitted = datetime.fromisoformat(sub['submitted_at'])
        if submitted < cutoff:
            continue
        events.append({
            'course_id': course_id,
            'student_canvas_id': student_id,
            'event_type': 'submission',
            'occurred_at': submitted,
            'source_id': sub['id'],
        })
    return events


def sync_student(course_id, student_id):
    """Pull one student's messages, discussion posts and submissions from Canvas
    and upsert them into interaction_event. Returns the number of events found.

    Deliberately leaves the course-wide sync markers alone: those mean "every
    student is up to date through here", which a single-student pull can't
    promise. Raises on any Canvas failure, without recording a refresh time,
    so a failed refresh is retried rather than trusted."""
    client = CanvasClient()
    today = datetime.now(timezone.utc).date()
    try:
        course_obj = client.get_course(course_id)
    except Exception:
        course_obj = None
    cutoff = _course_cutoff(course_obj, today)
    instructor_id = client.get_current_user()['id']

    events = _student_conversation_events(client, course_id, student_id, instructor_id)
    events += _student_submission_events(client, course_id, student_id, cutoff)

    errors = queue.Queue()
    events += _phase_discussions(client, course_id, {student_id}, cutoff,
                                 instructor_id, errors, refresh=True)
    while not errors.empty():
        msg = errors.get_nowait()
        if msg.get('status') == 'error':
            raise RuntimeError(msg.get('msg', 'discussion refresh failed'))

    if events:
        stmt = pg_insert(InteractionEvent.__table__).values(events)
        stmt = stmt.on_conflict_do_update(
            constraint='uq_interaction_event_type_source_student',
            set_={'occurred_at': stmt.excluded.occurred_at},
        )
        db.session.execute(stmt)
        db.session.commit()
    _set_sync_marker(course_id, _student_scope(student_id))
    return len(events)
