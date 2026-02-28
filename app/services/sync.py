from datetime import datetime

from sqlalchemy.dialects.postgresql import insert as pg_insert

from app import db
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient


def sync_course(course_id):
    """Pull Canvas data for course_id and upsert into interaction_event.

    Aggregates from:
      - Sent conversations (participants matched against enrolled students)
      - Discussion entries authored by students
      - recent_replies on those entries authored by students

    Returns the number of event rows processed.
    """
    client = CanvasClient()
    events = []

    # Enrolled student IDs — used to filter Canvas objects down to students only
    enrollments = client.get_enrollments(course_id)
    student_ids = {e['user_id'] for e in enrollments}

    if not student_ids:
        return 0

    # --- Conversations -------------------------------------------------------
    # scope=sent means messages the instructor sent; participants includes all
    # people in the thread.  We record one event per enrolled student per convo.
    for conv in client.get_conversations():
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
    for topic in client.get_discussion_topics(course_id):
        for entry in client.get_discussion_entries(course_id, topic['id']):
            if entry.get('user_id') in student_ids:
                events.append({
                    'course_id': course_id,
                    'student_canvas_id': entry['user_id'],
                    'event_type': 'discussion_entry',
                    'occurred_at': datetime.fromisoformat(entry['created_at']),
                    'source_id': entry['id'],
                })
            # TODO: recurse into full reply threads (not just recent_replies)
            for reply in entry.get('recent_replies', []):
                if reply.get('user_id') in student_ids:
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': reply['user_id'],
                        'event_type': 'discussion_reply',
                        'occurred_at': datetime.fromisoformat(reply['created_at']),
                        'source_id': reply['id'],
                    })

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
