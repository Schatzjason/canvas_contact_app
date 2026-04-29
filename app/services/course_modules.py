from datetime import date, datetime, timedelta, timezone

from app import db
from app.models.course_module import CourseModule
from app.services.canvas_client import CanvasClient


def _parse_canvas_date(value, tz):
    """Parse a Canvas ISO timestamp into a date in the given timezone."""
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz).date()


def recompute_course_modules(course_id, tz=timezone.utc):
    """Wipe and recompute course_module rows for a course.

    Walks Canvas modules in position order and assigns each a [start_date, end_date]
    range. Rules:
      - end_date = max due date among the module's items (cross-referenced against
        the assignments cache by content_id).
      - start_date = previous module's end_date + 1 day, or the course start_at for
        the first module.
      - Empty modules (no due-dated items) get end_date = start_date — a single-day
        window so the next module picks up immediately. The lookup function handles
        ties (earlier position wins) and post-last-module messages (assigned to last).
    """
    client = CanvasClient()
    course_obj = client.get_course(course_id)
    modules = client.get_modules(course_id)
    assignments = client.get_assignments(course_id)

    course_start = _parse_canvas_date(course_obj.get('start_at'), tz)
    if course_start is None:
        # Fall back to earliest assignment due date, or today, so the cursor advances.
        earliest = None
        for a in assignments:
            d = _parse_canvas_date(a.get('due_at'), tz)
            if d and (earliest is None or d < earliest):
                earliest = d
        course_start = earliest or date.today()

    # Build content_id -> due_date map. Canvas module items reference assignments
    # via content_id; quizzes/discussions appear in the assignments list as shadow
    # assignments keyed by quiz_id / discussion_topic.id.
    due_by_assignment = {}
    due_by_quiz = {}
    due_by_discussion = {}
    for a in assignments:
        d = _parse_canvas_date(a.get('due_at'), tz)
        if not d:
            continue
        due_by_assignment[a['id']] = d
        if a.get('quiz_id'):
            due_by_quiz[a['quiz_id']] = d
        topic = a.get('discussion_topic') or {}
        if topic.get('id'):
            due_by_discussion[topic['id']] = d

    def _item_due(item):
        cid = item.get('content_id')
        if not cid:
            return None
        t = item.get('type')
        if t == 'Assignment':
            return due_by_assignment.get(cid)
        if t == 'Quiz':
            return due_by_quiz.get(cid)
        if t == 'Discussion':
            return due_by_discussion.get(cid)
        return None

    sorted_modules = sorted(modules, key=lambda m: m.get('position') or 0)

    rows = []
    prev_end = course_start - timedelta(days=1)
    for m in sorted_modules:
        start = prev_end + timedelta(days=1)
        item_dues = [d for d in (_item_due(it) for it in (m.get('items') or [])) if d]
        if item_dues:
            end = max(item_dues)
            if end < start:
                end = start  # out-of-order due dates — collapse to single day
        else:
            end = start  # empty module
        rows.append({
            'course_id': course_id,
            'canvas_module_id': m['id'],
            'name': m.get('name', '') or '',
            'position': m.get('position') or 0,
            'start_date': start,
            'end_date': end,
        })
        prev_end = end

    db.session.query(CourseModule).filter_by(course_id=course_id).delete()
    if rows:
        db.session.bulk_insert_mappings(CourseModule, rows)
    db.session.commit()
    return len(rows)


def module_for_event(course_id, occurred_at, tz=timezone.utc):
    """Return the CourseModule that contains the given timestamp, or None.

    Rules:
      - If occurred_at falls within a module's [start_date, end_date] range, return
        the earliest-position module that matches (ties go to earlier).
      - If occurred_at is after every module's end_date, return the last module by
        position.
      - If occurred_at is before the first module's start_date, return None.
    """
    if occurred_at.tzinfo is None:
        occurred_at = occurred_at.replace(tzinfo=timezone.utc)
    on_date = occurred_at.astimezone(tz).date()

    match = (CourseModule.query
             .filter(CourseModule.course_id == course_id,
                     CourseModule.start_date <= on_date,
                     CourseModule.end_date >= on_date)
             .order_by(CourseModule.position.asc())
             .first())
    if match:
        return match

    last = (CourseModule.query
            .filter_by(course_id=course_id)
            .order_by(CourseModule.position.desc())
            .first())
    if last and on_date > last.end_date:
        return last
    return None
