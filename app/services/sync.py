from datetime import datetime, timedelta, timezone

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient


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
    cutoff = datetime.now(timezone.utc) - timedelta(days=21)

    yield {'status': 'fetching', 'item': 'enrollments'}
    enrollments = client.get_enrollments(course_id)
    student_ids = {e['user_id'] for e in enrollments}

    if not student_ids:
        yield {'status': 'done', 'count': 0}
        return

    # --- Conversations -------------------------------------------------------
    # scope=sent means messages the instructor sent; participants includes all
    # people in the thread.  We record one event per enrolled student per convo.
    yield {'status': 'fetching', 'item': 'conversations'}
    conversations = []
    for page, is_cached in client.stream_conversations(since=cutoff):
        conversations.extend(page)
        if not is_cached:
            yield {'status': 'fetching_page', 'item': 'conversations'}

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
            # TODO: recurse into full reply threads (not just recent_replies)
            for reply in entry.get('recent_replies', []):
                reply_at = datetime.fromisoformat(reply['created_at'])
                if reply_at >= cutoff and reply.get('user_id') in student_ids:
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': reply['user_id'],
                        'event_type': 'discussion_reply',
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

    yield {'status': 'done', 'count': len(events)}


def run_sync(course_id):
    """Consume sync_course to completion without streaming progress. Returns event count."""
    for msg in sync_course(course_id):
        if msg.get('status') == 'done':
            return msg.get('count', 0)
    return 0
