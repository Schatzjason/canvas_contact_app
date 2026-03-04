import json
import pathlib
from datetime import datetime, timedelta, timezone

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


def sync_course(course_id):
    """Generator that syncs Canvas data for course_id into interaction_event.

    Yields progress dicts: {'status': 'fetching'|'reading'|'saving'|'done', 'item': str}
    The final dict has status='done' and a 'count' key with the number of events upserted.

    Aggregates from:
      - Sent conversations (participants matched against enrolled students)
      - Discussion entries authored by students
      - recent_replies on those entries authored by students
    """
    client = CanvasClient()
    events = []
    # Round to midnight so the cache key is stable throughout the day.
    # A per-second timestamp would produce a unique key on every sync call,
    # defeating the cache entirely.
    today = datetime.now(timezone.utc).date()
    cutoff = datetime(today.year, today.month, today.day, tzinfo=timezone.utc) - timedelta(days=21)

    yield {'status': 'fetching', 'item': 'enrollments'}
    enrollments = client.get_enrollments(course_id)
    student_ids = {e['user_id'] for e in enrollments}

    if not student_ids:
        yield {'status': 'done', 'count': 0}
        return

    instructor_id = client.get_current_user()['id']

    # --- Conversations -------------------------------------------------------
    # scope=sent means messages the instructor sent; participants includes all
    # people in the thread.  We record one event per enrolled student per convo.
    yield {'status': 'fetching', 'item': 'conversations'}
    last_conv_sync = _get_sync_marker(course_id, 'conv_sent')
    conv_since = last_conv_sync if last_conv_sync else cutoff
    conversations = []
    fetched_live = False
    for page, is_cached in client.stream_conversations(since=conv_since):
        conversations.extend(page)
        if not is_cached:
            fetched_live = True
            yield {'status': 'fetching_page', 'item': 'conversations'}
    if fetched_live:
        _set_sync_marker(course_id, 'conv_sent')

    for conv in conversations:
        ts_str = conv.get('last_authored_at') or conv.get('last_message_at')
        if not ts_str:
            continue
        occurred_at = datetime.fromisoformat(ts_str)
        participant_ids = {p['id'] for p in conv.get('participants', [])}
        for student_id in participant_ids & student_ids:
            events.append({
                'course_id': course_id,
                'student_canvas_id': student_id,
                'event_type': 'conversation',
                'occurred_at': occurred_at,
                'source_id': conv['id'],
            })

    # --- Student messages (inbox) --------------------------------------------
    # scope=inbox gives conversations where students wrote to the instructor.
    # last_message_at is used as the timestamp (best available from the list API).
    yield {'status': 'fetching', 'item': 'student messages'}
    last_inbox_sync = _get_sync_marker(course_id, 'conv_inbox')
    inbox_since = last_inbox_sync if last_inbox_sync else cutoff
    inbox = []
    fetched_live = False
    for page, is_cached in client.stream_conversations(since=inbox_since, scope='inbox'):
        inbox.extend(page)
        if not is_cached:
            fetched_live = True
            yield {'status': 'fetching_page', 'item': 'student messages'}
    if fetched_live:
        _set_sync_marker(course_id, 'conv_inbox')

    for conv in inbox:
        ts_str = conv.get('last_message_at')
        if not ts_str:
            continue
        occurred_at = datetime.fromisoformat(ts_str)
        participant_ids = {p['id'] for p in conv.get('participants', [])}
        for student_id in participant_ids & student_ids:
            events.append({
                'course_id': course_id,
                'student_canvas_id': student_id,
                'event_type': 'student_message',
                'occurred_at': occurred_at,
                'source_id': conv['id'],
            })

    # --- Discussion entries + recent_replies ----------------------------------
    yield {'status': 'fetching', 'item': 'discussion topics'}
    topics = client.get_discussion_topics(course_id)

    for i, topic in enumerate(topics, 1):
        yield {'status': 'fetching', 'item': f'discussion {i} entries'}
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
                elif reply_author == instructor_id and entry.get('user_id') in student_ids:
                    # Instructor replied to a student's entry — record on the student's row
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': entry['user_id'],
                        'event_type': 'discussion_instructor_reply',
                        'occurred_at': reply_at,
                        'source_id': reply['id'],
                    })

    if events:
        yield {'status': 'saving', 'item': f'{len(events)} events'}
        stmt = pg_insert(InteractionEvent.__table__).values(events)
        stmt = stmt.on_conflict_do_update(
            constraint='uq_interaction_event_type_source_student',
            set_={'occurred_at': stmt.excluded.occurred_at},
        )
        db.session.execute(stmt)
        db.session.commit()

    inject_fixture_responses(course_id)

    yield {'status': 'done', 'count': len(events)}


def run_sync(course_id):
    """Consume sync_course to completion without streaming progress. Returns event count."""
    for msg in sync_course(course_id):
        if msg.get('status') == 'done':
            return msg.get('count', 0)
    return 0
