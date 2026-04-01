import json
import re
import uuid
from datetime import date, datetime, time, timedelta, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from flask import Blueprint, Response, current_app, flash, render_template, stream_with_context
from sqlalchemy import func, text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from flask import redirect, request, url_for

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.check_back_date import CheckBackDate
from app.models.course_display_name import CourseDisplayName
from app.models.interaction_event import InteractionEvent
from app.models.message_template import MessageTemplate
from app.models.pinned_discussion import PinnedDiscussion
from app.models.student_note import StudentNote
from app.models.student_record import StudentRecord
from app.services.canvas_client import CanvasClient, TTL_CONVERSATIONS
from app.services.sync import run_sync, sync_course

bp = Blueprint('dashboard', __name__)


def fill_placeholders(text, student_context):
    """Replace placeholder tokens in *text* with values from *student_context*.

    Supported placeholders:
        <name>  — student's first name
        <time>  — days since last interaction (e.g. "5 days"), or empty string

    *student_context* is a dict with optional keys:
        first_name  (str)
        days_since  (int | None)

    This function is intentionally decoupled from any route so it can be called
    from any code path that sends a message (compose page, future batch-send, etc.).
    """
    first_name = student_context.get('first_name', '')
    days_since = student_context.get('days_since')

    if first_name:
        text = text.replace('<name>', first_name)
    if days_since is not None:
        text = text.replace('<time>', f'{days_since} days')
    return text


def _get_tz():
    """Return the user's local timezone from the browser-set cookie, falling back to UTC."""
    tz_name = request.cookies.get('tz', '')
    try:
        return ZoneInfo(tz_name) if tz_name else timezone.utc
    except ZoneInfoNotFoundError:
        return timezone.utc


def _strip_html(html):
    """Convert Canvas HTML message to plain text."""
    if not html:
        return ''
    html = re.sub(r'<(script|style)[^>]*>.*?</(script|style)>', '', html, flags=re.DOTALL | re.IGNORECASE)
    html = re.sub(r'<link[^>]*/?>',  '', html, flags=re.IGNORECASE)
    html = re.sub(r'<img[^>]*/?>',   '[image]', html, flags=re.IGNORECASE)
    html = re.sub(r'<br\s*/?>',      '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'</(p|div|li|h[1-6])>', '\n', html, flags=re.IGNORECASE)
    html = re.sub(r'<[^>]+>', '', html)
    for ent, ch in [('&nbsp;', ' '), ('&amp;', '&'), ('&lt;', '<'), ('&gt;', '>'), ('&quot;', '"'), ('&#39;', "'")]:
        html = html.replace(ent, ch)
    lines = [l.rstrip() for l in html.splitlines()]
    out, prev_blank = [], False
    for line in lines:
        blank = line == ''
        if blank and prev_blank:
            continue
        out.append(line)
        prev_blank = blank
    return '\n'.join(out).strip()


def _time_badge(last_at, now, warn_days):
    """Return (badge_text, badge_class) given the last-interaction datetime."""
    if last_at is None:
        return 'never', 'stale'
    if last_at.tzinfo is None:
        last_at = last_at.replace(tzinfo=timezone.utc)
    seconds = (now - last_at).total_seconds()
    if seconds < 60:
        text = 'just now'
    elif seconds < 3600:
        text = f'{int(seconds / 60)}m ago'
    elif seconds < 86400:
        text = f'{int(seconds / 3600)}h ago'
    else:
        text = f'{int(seconds / 86400)}d ago'
    cls = 'fresh' if seconds < warn_days * 86400 else 'stale'
    return text, cls


@bp.route('/')
def index():
    client = CanvasClient()
    try:
        courses = client.get_courses()
    except Exception as exc:
        flash(f'Could not load courses from Canvas: {exc}')
        courses = []

    now = datetime.now(timezone.utc)
    warn_days = current_app.config['STALE_WARN_DAYS']
    course_ids = [c['id'] for c in courses]
    if course_ids:
        last_rows = {
            row.course_id: row.last_at
            for row in db.session.query(
                InteractionEvent.course_id,
                func.max(InteractionEvent.occurred_at).label('last_at'),
            ).filter(InteractionEvent.course_id.in_(course_ids))
            .group_by(InteractionEvent.course_id).all()
        }
        count_rows = {
            row.course_id: row.cnt
            for row in db.session.query(
                InteractionEvent.course_id,
                func.count(InteractionEvent.student_canvas_id.distinct()).label('cnt'),
            ).filter(InteractionEvent.course_id.in_(course_ids))
            .group_by(InteractionEvent.course_id).all()
        }
        stats_by_course = {
            cid: {
                'badge_text': _time_badge(last_rows.get(cid), now, warn_days)[0],
                'badge_class': _time_badge(last_rows.get(cid), now, warn_days)[1],
                'active_count': count_rows.get(cid, 0),
            }
            for cid in course_ids
        }
    else:
        stats_by_course = {}

    display_names = {
        row.course_id: row.name
        for row in CourseDisplayName.query.filter(
            CourseDisplayName.course_id.in_(course_ids)
        ).all()
    } if course_ids else {}

    # Check-back dates across all courses
    check_back_rows = []
    all_cb = CheckBackDate.query.order_by(CheckBackDate.date).all()
    if all_cb:
        cb_course_ids = {cb.course_id for cb in all_cb}
        canvas_name_map = {c['id']: c.get('name', f'Course {c["id"]}') for c in courses}
        course_name_map = {
            cid: display_names.get(cid, canvas_name_map.get(cid, f'Course {cid}'))
            for cid in cb_course_ids
        }
        student_name_map = {}
        for cid in cb_course_ids:
            try:
                enrs = client.get_enrollments(cid)
                for e in enrs:
                    u = e.get('user', {})
                    student_name_map[(cid, e['user_id'])] = (
                        u.get('sortable_name') or u.get('name', f'Student {e["user_id"]}')
                    )
            except Exception:
                pass

        # Fill in names for dropped students from StudentRecord
        missing_keys = {(cb.course_id, cb.student_canvas_id) for cb in all_cb} - set(student_name_map.keys())
        if missing_keys:
            for cid, sid in missing_keys:
                rec = StudentRecord.query.filter_by(course_id=cid, student_canvas_id=sid).first()
                if rec:
                    student_name_map[(cid, sid)] = rec.sortable_name

        days_of_week = ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday']
        for cb in all_cb:
            check_back_rows.append({
                'student_name': student_name_map.get(
                    (cb.course_id, cb.student_canvas_id),
                    f'Student {cb.student_canvas_id}',
                ),
                'date': cb.date.isoformat(),
                'day_of_week': days_of_week[cb.date.weekday()],
                'short_date': f'{cb.date.month}/{cb.date.day}',
                'course_name': course_name_map.get(cb.course_id, f'Course {cb.course_id}'),
                'note': cb.note,
                'course_id': cb.course_id,
                'student_id': cb.student_canvas_id,
            })

    # Build student search index across all courses (enrollments are cached 24h)
    search_students = []
    for c in courses:
        section = c.get('course_code', '')
        # Extract trailing section number (e.g. "CS111B-932" → "932")
        if '-' in section:
            section = section.rsplit('-', 1)[-1]
        try:
            enrs = client.get_enrollments(c['id'])
        except Exception:
            enrs = []
        enrolled_ids = set()
        for e in enrs:
            u = e.get('user', {})
            search_students.append({
                'name': u.get('sortable_name') or u.get('name', f'Student {e["user_id"]}'),
                'section': section,
                'course_id': c['id'],
                'student_id': e['user_id'],
                'dropped': False,
            })
            enrolled_ids.add(e['user_id'])

        # Include dropped students in search results
        dropped = StudentRecord.query.filter_by(
            course_id=c['id'], status='dropped',
        ).all()
        for rec in dropped:
            if rec.student_canvas_id not in enrolled_ids:
                search_students.append({
                    'name': rec.sortable_name,
                    'section': section,
                    'course_id': rec.course_id,
                    'student_id': rec.student_canvas_id,
                    'dropped': True,
                })

    return render_template('dashboard/index.html',
        courses=courses,
        stats_by_course=stats_by_course,
        display_names=display_names,
        check_back_rows=check_back_rows,
        search_students=search_students,
    )


@bp.route('/course/<int:course_id>/stats')
def course_stats(course_id):
    """Return current last-seen badge text/class for a course (used after background refresh)."""
    now = datetime.now(timezone.utc)
    warn_days = current_app.config['STALE_WARN_DAYS']

    last_at = db.session.query(
        func.max(InteractionEvent.occurred_at)
    ).filter(InteractionEvent.course_id == course_id).scalar()

    active_count = db.session.query(
        func.count(InteractionEvent.student_canvas_id.distinct())
    ).filter(InteractionEvent.course_id == course_id).scalar() or 0

    badge_text, badge_class = _time_badge(last_at, now, warn_days)
    return {'badge_text': badge_text, 'badge_class': badge_class, 'active_count': active_count}


@bp.route('/course/<int:course_id>/flush-cache', methods=['POST'])
def flush_cache(course_id):
    """Dev tool: delete all cached Canvas API responses and redirect to index."""
    deleted = db.session.query(CanvasCache).delete()
    db.session.commit()
    flash(f'Cache cleared ({deleted} entries). Reload a course to re-sync.')
    return redirect(url_for('dashboard.index'))


@bp.route('/course/<int:course_id>/display-name', methods=['POST'])
def save_display_name(course_id):
    name = request.get_json(force=True).get('name', '').strip()
    if not name:
        return {'ok': False, 'error': 'Name cannot be empty'}, 400
    row = CourseDisplayName.query.filter_by(course_id=course_id).first()
    if row:
        row.name = name
        row.updated_at = datetime.now(timezone.utc)
    else:
        row = CourseDisplayName(course_id=course_id, name=name)
        db.session.add(row)
    db.session.commit()
    return {'ok': True, 'name': name}


@bp.route('/course/<int:course_id>/sync')
def course_sync_stream(course_id):
    """SSE endpoint that runs sync_course and streams progress to the browser."""
    def generate():
        try:
            for msg in sync_course(course_id):
                yield f'data: {json.dumps(msg)}\n\n'
        except Exception as exc:
            current_app.logger.error('Sync stream failed for course %s: %s', course_id, exc)
            yield f'data: {json.dumps({"status": "error", "item": str(exc)})}\n\n'

    return Response(
        stream_with_context(generate()),
        mimetype='text/event-stream',
        headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'},
    )


@bp.route('/course/<int:course_id>')
def course(course_id):
    client = CanvasClient()

    # Sync on direct load (e.g. refresh/bookmark) — cheap when cache is warm
    try:
        run_sync(course_id)
    except Exception as exc:
        current_app.logger.error('Sync failed for course %s: %s', course_id, exc)
        flash('Could not sync latest data from Canvas.')

    try:
        course_obj = client.get_course(course_id)
    except Exception as exc:
        flash(f'Could not load course info: {exc}')
        course_obj = {'name': f'Course {course_id}', 'course_code': ''}

    try:
        enrollments = client.get_enrollments(course_id)
    except Exception as exc:
        flash(f'Could not load enrollments: {exc}')
        enrollments = []

    tz = _get_tz()
    today = datetime.now(tz).date()
    # 21 columns: today-20 (oldest) through today (newest)
    days = [today - timedelta(days=i) for i in range(20, -1, -1)]
    window_start_dt = datetime.combine(days[0], time.min, tzinfo=tz)

    warn_days = current_app.config['STALE_WARN_DAYS']
    alert_days = current_app.config['STALE_ALERT_DAYS']

    # Last student-initiated interaction date per student (all time, not just the window)
    student_event_types = ('student_message', 'discussion_entry', 'discussion_reply', 'submission')
    last_by_student = {
        row.student_canvas_id: row.last_at.astimezone(tz).date()
        for row in db.session.query(
            InteractionEvent.student_canvas_id,
            func.max(InteractionEvent.occurred_at).label('last_at'),
        ).filter(
            InteractionEvent.course_id == course_id,
            InteractionEvent.event_type.in_(student_event_types),
        ).group_by(InteractionEvent.student_canvas_id).all()
    }

    # Last instructor-initiated interaction date per student
    instructor_event_types = ('conversation', 'group_conversation', 'discussion_instructor_reply')
    last_instr_by_student = {
        row.student_canvas_id: row.last_at.astimezone(tz).date()
        for row in db.session.query(
            InteractionEvent.student_canvas_id,
            func.max(InteractionEvent.occurred_at).label('last_at'),
        ).filter(
            InteractionEvent.course_id == course_id,
            InteractionEvent.event_type.in_(instructor_event_types),
        ).group_by(InteractionEvent.student_canvas_id).all()
    }

    # Which days within the 21-day window had an interaction, per student
    # active_days_by_student: {student_id: {date: set(event_types)}}
    active_days_by_student = {}
    disc_source_ids       = {}  # {(student_id, date): [source_id, ...]}
    msg_source_ids        = {}  # {(student_id, date): [source_id, ...]}
    instr_disc_source_ids = {}  # {(student_id, date): [source_id, ...]}
    hw_source_ids         = {}  # {(student_id, date): [source_id, ...]}
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        sid = event.student_canvas_id
        day = event.occurred_at.astimezone(tz).date()
        active_days_by_student.setdefault(sid, {}).setdefault(day, set()).add(event.event_type)
        if event.event_type in ('discussion_entry', 'discussion_reply'):
            disc_source_ids.setdefault((sid, day), []).append(event.source_id)
        if event.event_type in ('conversation', 'group_conversation', 'student_message'):
            msg_source_ids.setdefault((sid, day), []).append(event.source_id)
        if event.event_type == 'discussion_instructor_reply':
            instr_disc_source_ids.setdefault((sid, day), []).append(event.source_id)
        if event.event_type == 'submission':
            hw_source_ids.setdefault((sid, day), []).append(event.source_id)

    # Fetch discussion message text from the canvas cache
    disc_messages = {}
    all_disc_ids = [src for ids in disc_source_ids.values() for src in ids]
    if all_disc_ids:
        rows = db.session.execute(text("""
            SELECT (e->>'id')::bigint AS source_id, e->>'message' AS message
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e
            WHERE (e->>'id')::bigint = ANY(:ids)
            UNION ALL
            SELECT (r->>'id')::bigint AS source_id, r->>'message' AS message
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e,
                 jsonb_array_elements(
                     CASE WHEN jsonb_typeof(e->'recent_replies') = 'array'
                     THEN e->'recent_replies' ELSE '[]'::jsonb END
                 ) AS r
            WHERE (r->>'id')::bigint = ANY(:ids)
        """), {'ids': all_disc_ids}).fetchall()
        msg_by_source = {row.source_id: _strip_html(row.message) for row in rows}
        for (sid, day), source_ids in disc_source_ids.items():
            texts = [msg_by_source[s] for s in source_ids if s in msg_by_source]
            if texts:
                disc_messages[(sid, day)] = '\n\n---\n\n'.join(texts)

    # Fetch conversation text from the canvas cache
    msg_texts = {}
    all_msg_ids = [src for ids in msg_source_ids.values() for src in ids]
    if all_msg_ids:
        rows = db.session.execute(text("""
            SELECT (e->>'id')::bigint AS source_id,
                   e->>'subject'              AS subject,
                   e->>'last_authored_message' AS authored,
                   e->>'last_message'          AS last_msg
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e
            WHERE (e->>'id')::bigint = ANY(:ids)
        """), {'ids': all_msg_ids}).fetchall()
        conv_by_source = {}
        for row in rows:
            parts = []
            if row.subject:
                parts.append(f'Subject: {row.subject}')
            body = row.authored or row.last_msg
            if body:
                parts.append(body)
            conv_by_source[row.source_id] = '\n\n'.join(parts)
        for (sid, day), source_ids in msg_source_ids.items():
            texts = [conv_by_source[s] for s in source_ids if s in conv_by_source]
            if texts:
                msg_texts[(sid, day)] = '\n\n---\n\n'.join(texts)

    # Fetch instructor discussion reply text from the canvas cache
    instr_disc_messages = {}
    all_instr_disc_ids = [src for ids in instr_disc_source_ids.values() for src in ids]
    if all_instr_disc_ids:
        rows = db.session.execute(text("""
            SELECT (r->>'id')::bigint AS source_id, r->>'message' AS message
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e,
                 jsonb_array_elements(
                     CASE WHEN jsonb_typeof(e->'recent_replies') = 'array'
                     THEN e->'recent_replies' ELSE '[]'::jsonb END
                 ) AS r
            WHERE (r->>'id')::bigint = ANY(:ids)
        """), {'ids': all_instr_disc_ids}).fetchall()
        reply_by_source = {row.source_id: _strip_html(row.message) for row in rows}
        for (sid, day), source_ids in instr_disc_source_ids.items():
            texts = [reply_by_source[s] for s in source_ids if s in reply_by_source]
            if texts:
                instr_disc_messages[(sid, day)] = '\n\n---\n\n'.join(texts)

    # Fetch assignment name + due date for submission hover text
    hw_texts = {}
    all_hw_ids = [src for ids in hw_source_ids.values() for src in ids]
    if all_hw_ids:
        # Step 1: map submission source_id → assignment_id via submission caches.
        # Submission cache entries contain arrays of objects with 'id' and
        # 'assignment_id'.  Filter to rows that have an assignment_id and
        # whose id matches one of our submission source IDs.
        rows = db.session.execute(text("""
            SELECT (s->>'id')::bigint AS sub_id,
                   (s->>'assignment_id')::bigint AS assignment_id
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS s
            WHERE (s->>'assignment_id') IS NOT NULL
              AND (s->>'id')::bigint = ANY(:ids)
        """), {'ids': all_hw_ids}).fetchall()
        sub_to_assignment = {r.sub_id: r.assignment_id for r in rows if r.assignment_id}

        # Step 2: get assignment details from the assignments cache
        assignment_ids = set(sub_to_assignment.values())
        if assignment_ids:
            a_rows = db.session.execute(text("""
                SELECT (a->>'id')::bigint AS aid,
                       a->>'name' AS name,
                       a->>'due_at' AS due_at
                FROM canvas_cache,
                     jsonb_array_elements(response_json::jsonb) AS a
                WHERE (a->>'id')::bigint = ANY(:ids)
            """), {'ids': list(assignment_ids)}).fetchall()
            assignment_info = {}
            for r in a_rows:
                parts = [r.name] if r.name else []
                if r.due_at:
                    try:
                        due_dt = datetime.fromisoformat(r.due_at).astimezone(tz)
                        parts.append(f'Due {due_dt.month}/{due_dt.day}')
                    except (ValueError, TypeError):
                        pass
                assignment_info[r.aid] = ' — '.join(parts) if parts else ''

            hw_by_source = {}
            for sub_id, aid in sub_to_assignment.items():
                if aid in assignment_info:
                    hw_by_source[sub_id] = assignment_info[aid]

            for (sid, day), source_ids in hw_source_ids.items():
                texts = [hw_by_source[s] for s in source_ids if s in hw_by_source]
                if texts:
                    # Deduplicate — multiple submissions for same assignment on same day
                    hw_texts[(sid, day)] = '\n'.join(dict.fromkeys(texts))

    def _staleness(last_date):
        if last_date is None:
            return None, 'red'
        d = (today - last_date).days
        if d <= warn_days:
            return d, 'green'
        if d <= alert_days:
            return d, 'yellow'
        return d, 'red'

    students = []
    for enrollment in enrollments:
        user = enrollment.get('user', {})
        canvas_id = enrollment['user_id']

        last_date = last_by_student.get(canvas_id)
        days_since, staleness = _staleness(last_date)

        last_instr_date = last_instr_by_student.get(canvas_id)
        instr_days_since, instr_staleness = _staleness(last_instr_date)

        grades = enrollment.get('grades', {})
        current_score = grades.get('current_score')

        students.append({
            'canvas_id': canvas_id,
            'name': user.get('sortable_name') or user.get('name', f'Student {canvas_id}'),
            'last_date': last_date,
            'days_since': days_since,
            'staleness': staleness,
            'last_instr_date': last_instr_date,
            'instr_days_since': instr_days_since,
            'instr_staleness': instr_staleness,
            'active_days': active_days_by_student.get(canvas_id, {}),
            'score': current_score,
        })

    active_tab = request.args.get('tab', 'timeline')

    if active_tab == 'submissions':
        # Sort by instructor contact recency
        students.sort(key=lambda s: (s['last_instr_date'] is not None, s['last_instr_date'] or date.min))
    else:
        # Sort by student activity recency
        students.sort(key=lambda s: (s['last_date'] is not None, s['last_date'] or date.min))

    display_name_row = CourseDisplayName.query.filter_by(course_id=course_id).first()
    display_name = display_name_row.name if display_name_row else course_obj.get('name', '')

    return render_template('dashboard/course.html',
        course=course_obj,
        display_name=display_name,
        students=students,
        days=days,
        today=today,
        disc_messages=disc_messages,
        msg_texts=msg_texts,
        instr_disc_messages=instr_disc_messages,
        hw_texts=hw_texts,
        active_tab=active_tab,
    )


@bp.route('/course/<int:course_id>/student/<int:student_id>')
def student(course_id, student_id):
    client = CanvasClient()

    try:
        course_obj = client.get_course(course_id)
    except Exception as exc:
        flash(f'Could not load course info: {exc}')
        course_obj = {'name': f'Course {course_id}', 'course_code': '', 'id': course_id}

    try:
        enrollments = client.get_enrollments(course_id)
    except Exception as exc:
        flash(f'Could not load enrollments: {exc}')
        enrollments = []

    student_enrollment = next(
        (e for e in enrollments if e['user_id'] == student_id), None
    )
    if student_enrollment:
        user = student_enrollment.get('user', {})
        student_name = user.get('name', f'Student {student_id}')
        student_score = student_enrollment.get('grades', {}).get('current_score')
        is_dropped = False
    else:
        rec = StudentRecord.query.filter_by(
            course_id=course_id, student_canvas_id=student_id
        ).first()
        student_name = rec.name if rec else f'Student {student_id}'
        student_score = None
        is_dropped = rec.status == 'dropped' if rec else False

    tz = _get_tz()
    today = datetime.now(tz).date()
    days = [today - timedelta(days=i) for i in range(20, -1, -1)]
    window_start_dt = datetime.combine(days[0], time.min, tzinfo=tz)

    active_days = {}
    event_source_ids = {}  # {(day, event_type): [source_id, ...]}
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.student_canvas_id == student_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        day = event.occurred_at.astimezone(tz).date()
        active_days.setdefault(day, set()).add(event.event_type)
        event_source_ids.setdefault((day, event.event_type), []).append(event.source_id)

    # ── Fetch text content from canvas cache for drawer ───────
    disc_ids = [s for (d, et), ss in event_source_ids.items()
                if et in ('discussion_entry', 'discussion_reply') for s in ss]
    instr_disc_ids = [s for (d, et), ss in event_source_ids.items()
                      if et == 'discussion_instructor_reply' for s in ss]
    msg_ids = [s for (d, et), ss in event_source_ids.items()
               if et in ('conversation', 'group_conversation', 'student_message') for s in ss]

    disc_text_by_src = {}
    if disc_ids:
        rows = db.session.execute(text("""
            SELECT (e->>'id')::bigint AS sid, e->>'message' AS msg
            FROM canvas_cache, jsonb_array_elements(response_json::jsonb) AS e
            WHERE (e->>'id')::bigint = ANY(:ids)
            UNION ALL
            SELECT (r->>'id')::bigint, r->>'message'
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e,
                 jsonb_array_elements(CASE WHEN jsonb_typeof(e->'recent_replies')='array'
                     THEN e->'recent_replies' ELSE '[]'::jsonb END) AS r
            WHERE (r->>'id')::bigint = ANY(:ids)
        """), {'ids': disc_ids}).fetchall()
        disc_text_by_src = {r.sid: _strip_html(r.msg) for r in rows}

    instr_reply_text_by_src = {}
    if instr_disc_ids:
        # Only search recent_replies — real Canvas replies live there;
        # fixture source_ids point to top-level entries (student text, not reply).
        rows = db.session.execute(text("""
            SELECT (r->>'id')::bigint AS sid, r->>'message' AS msg
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS e,
                 jsonb_array_elements(CASE WHEN jsonb_typeof(e->'recent_replies')='array'
                     THEN e->'recent_replies' ELSE '[]'::jsonb END) AS r
            WHERE (r->>'id')::bigint = ANY(:ids)
        """), {'ids': instr_disc_ids}).fetchall()
        instr_reply_text_by_src = {r.sid: _strip_html(r.msg) for r in rows}

    conv_text_by_src = {}
    if msg_ids:
        rows = db.session.execute(text("""
            SELECT (e->>'id')::bigint AS sid,
                   e->>'subject' AS subject,
                   e->>'last_authored_message' AS authored,
                   e->>'last_message' AS last_msg
            FROM canvas_cache, jsonb_array_elements(response_json::jsonb) AS e
            WHERE (e->>'id')::bigint = ANY(:ids)
        """), {'ids': msg_ids}).fetchall()
        for r in rows:
            parts = [f'Subject: {r.subject}'] if r.subject else []
            body = r.authored or r.last_msg
            if body:
                parts.append(body)
            conv_text_by_src[r.sid] = '\n\n'.join(parts)

    # ── Build per-day drawer payload ──────────────────────────
    day_drawer = {}
    for day, types in active_days.items():
        sections = []

        disc_srcs = (event_source_ids.get((day, 'discussion_entry'), []) +
                     event_source_ids.get((day, 'discussion_reply'), []))
        if disc_srcs:
            texts = [disc_text_by_src[s] for s in disc_srcs if s in disc_text_by_src]
            sections.append({'label': 'Student Discussion',
                             'text': '\n\n---\n\n'.join(texts)})

        for s in event_source_ids.get((day, 'discussion_instructor_reply'), []):
            reply_text = instr_reply_text_by_src.get(s, '')
            sections.append({'label': 'Instructor Discussion Reply', 'text': reply_text})

        for s in event_source_ids.get((day, 'conversation'), []):
            sections.append({'label': 'Instructor Message',
                             'text': conv_text_by_src.get(s, '')})

        for s in event_source_ids.get((day, 'group_conversation'), []):
            sections.append({'label': 'Group Message',
                             'text': conv_text_by_src.get(s, '')})

        for s in event_source_ids.get((day, 'student_message'), []):
            sections.append({'label': 'Student Message',
                             'text': conv_text_by_src.get(s, '')})

        day_drawer[day.isoformat()] = {
            'date_label': day.strftime('%A') + ', ' + day.strftime('%B') + ' ' + str(day.day),
            'sections': sections,
        }

    pinned_row = PinnedDiscussion.query.filter_by(course_id=course_id).first()
    pinned_topic_id = pinned_row.topic_id if pinned_row else None
    pinned_post = None
    if pinned_topic_id:
        try:
            entries = client.get_discussion_entries(course_id, pinned_topic_id)
            entry = next((e for e in entries if e.get('user_id') == student_id), None)
            if entry:
                pinned_post = _strip_html(entry.get('message', ''))
        except Exception as exc:
            current_app.logger.warning('Could not load pinned discussion: %s', exc)

    now = datetime.now(timezone.utc)
    warn_days  = current_app.config['STALE_WARN_DAYS']
    alert_days = current_app.config['STALE_ALERT_DAYS']

    last_at = db.session.query(
        func.max(InteractionEvent.occurred_at)
    ).filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.student_canvas_id == student_id,
    ).scalar()

    if last_at is None:
        days_since = None
        staleness  = 'red'
    else:
        days_since = (now - last_at).days
        if days_since <= warn_days:
            staleness = 'green'
        elif days_since <= alert_days:
            staleness = 'yellow'
        else:
            staleness = 'red'

    note_row = StudentNote.query.filter_by(
        course_id=course_id, student_canvas_id=student_id
    ).first()
    note_content = note_row.content if note_row else ''

    cb_row = CheckBackDate.query.filter_by(
        course_id=course_id, student_canvas_id=student_id
    ).first()
    check_back_date = cb_row.date.isoformat() if cb_row else ''
    check_back_note = cb_row.note if cb_row else ''

    return render_template('dashboard/student.html',
        course=course_obj,
        student_id=student_id,
        student_name=student_name,
        student_score=student_score,
        days_since=days_since,
        staleness=staleness,
        pinned_post=pinned_post,
        pinned_topic_id=pinned_topic_id,
        days=days,
        today=today,
        active_days=active_days,
        day_drawer=day_drawer,
        note_content=note_content,
        check_back_date=check_back_date,
        check_back_note=check_back_note,
        is_dropped=is_dropped,
    )


@bp.route('/course/<int:course_id>/student/<int:student_id>/note', methods=['POST'])
def save_note(course_id, student_id):
    content = request.get_json(force=True).get('content', '')
    note = StudentNote.query.filter_by(
        course_id=course_id, student_canvas_id=student_id
    ).first()
    if note:
        note.content = content
        note.updated_at = datetime.now(timezone.utc)
    else:
        note = StudentNote(
            course_id=course_id,
            student_canvas_id=student_id,
            content=content,
        )
        db.session.add(note)
    db.session.commit()
    return {'ok': True}


@bp.route('/course/<int:course_id>/student/<int:student_id>/check-back', methods=['POST'])
def save_check_back(course_id, student_id):
    data = request.get_json(force=True)
    date_str = data.get('date', '').strip()

    note_str = data.get('note', '').strip()[:60]

    # Empty date = clear
    if not date_str:
        CheckBackDate.query.filter_by(
            course_id=course_id, student_canvas_id=student_id
        ).delete()
        db.session.commit()
        return {'ok': True, 'date': '', 'note': ''}

    try:
        parsed = date.fromisoformat(date_str)
    except ValueError:
        return {'ok': False, 'error': 'Invalid date format. Use YYYY-MM-DD.'}, 400

    row = CheckBackDate.query.filter_by(
        course_id=course_id, student_canvas_id=student_id
    ).first()
    if row:
        row.date = parsed
        row.note = note_str
    else:
        row = CheckBackDate(
            course_id=course_id,
            student_canvas_id=student_id,
            date=parsed,
            note=note_str,
        )
        db.session.add(row)
    db.session.commit()
    return {'ok': True, 'date': parsed.isoformat(), 'note': row.note}


@bp.route('/course/<int:course_id>/discussion-topics')
def discussion_topics(course_id):
    """Return discussion topics for the course as JSON (for the picker UI)."""
    client = CanvasClient()
    try:
        topics = client.get_discussion_topics(course_id)
    except Exception as exc:
        return {'ok': False, 'error': str(exc)}, 500
    return {'ok': True, 'topics': [
        {'id': t['id'], 'title': t.get('title', f'Topic {t["id"]}')}
        for t in topics
    ]}


@bp.route('/course/<int:course_id>/pinned-discussion', methods=['POST'])
def save_pinned_discussion(course_id):
    topic_id = request.get_json(force=True).get('topic_id')
    if not topic_id:
        return {'ok': False, 'error': 'topic_id is required'}, 400
    row = PinnedDiscussion.query.filter_by(course_id=course_id).first()
    if row:
        row.topic_id = topic_id
    else:
        row = PinnedDiscussion(course_id=course_id, topic_id=topic_id)
        db.session.add(row)
    db.session.commit()
    return {'ok': True, 'topic_id': row.topic_id}


@bp.route('/course/<int:course_id>/student/<int:student_id>/compose', methods=['GET', 'POST'])
def compose(course_id, student_id):
    client = CanvasClient()

    try:
        course_obj = client.get_course(course_id)
    except Exception as exc:
        flash(f'Could not load course info: {exc}')
        course_obj = {'name': f'Course {course_id}', 'course_code': '', 'id': course_id}

    try:
        enrollments = client.get_enrollments(course_id)
    except Exception as exc:
        flash(f'Could not load enrollments: {exc}')
        enrollments = []

    student_enrollment = next(
        (e for e in enrollments if e['user_id'] == student_id), None
    )
    if student_enrollment:
        student_name = student_enrollment['user'].get('name', f'Student {student_id}')
    else:
        rec = StudentRecord.query.filter_by(course_id=course_id, student_canvas_id=student_id).first()
        student_name = rec.name if rec else f'Student {student_id}'

    first_name = student_name.split()[0] if student_name else ''
    last_at = db.session.query(
        func.max(InteractionEvent.occurred_at)
    ).filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.student_canvas_id == student_id,
    ).scalar()
    now = datetime.now(timezone.utc)
    days_since = (now - last_at).days if last_at else None

    student_context = {'first_name': first_name, 'days_since': days_since}

    if request.method == 'POST':
        subject = fill_placeholders(request.form.get('subject', '').strip(), student_context)
        body    = fill_placeholders(request.form.get('body', '').strip(), student_context)
        try:
            result   = client.send_message(student_id, subject, body, course_id=course_id)
            now      = datetime.now(timezone.utc)
            new_conv = result[0] if isinstance(result, list) and result else None

            if new_conv:
                # Record the event directly — no re-fetch needed
                ts_str      = new_conv.get('last_authored_at') or new_conv.get('last_message_at')
                occurred_at = datetime.fromisoformat(ts_str) if ts_str else now
                stmt = pg_insert(InteractionEvent.__table__).values([{
                    'course_id': course_id,
                    'student_canvas_id': student_id,
                    'event_type': 'conversation',
                    'occurred_at': occurred_at,
                    'source_id': new_conv['id'],
                }])
                stmt = stmt.on_conflict_do_update(
                    constraint='uq_interaction_event_type_source_student',
                    set_={'occurred_at': stmt.excluded.occurred_at},
                )
                db.session.execute(stmt)

                # Prepend new conversation to the sent cache so syncs stay fast
                sent_key   = CanvasClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})
                sent_entry = CanvasCache.query.filter_by(cache_key=sent_key).first()
                if sent_entry:
                    sent_entry.response_json = [new_conv] + (sent_entry.response_json or [])
                    sent_entry.fetched_at    = now
                else:
                    db.session.add(CanvasCache(
                        cache_key=sent_key,
                        response_json=[new_conv],
                        fetched_at=now,
                        ttl_seconds=TTL_CONVERSATIONS,
                    ))
            else:
                # Canvas returned no conversation object — fall back to cache invalidation
                sent_key = CanvasClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})
                CanvasCache.query.filter_by(cache_key=sent_key).delete()

            db.session.commit()
            flash(f'Message sent to {student_name}.')
        except Exception as exc:
            current_app.logger.error('send_message failed: %s', exc)
            flash(f'Could not send message: {exc}')
        return redirect(url_for('dashboard.student', course_id=course_id, student_id=student_id))

    default_subject = 'Checking in'
    default_body    = f'Hi {first_name}, ' if first_name else ''

    templates = MessageTemplate.query.order_by(MessageTemplate.created_at.desc()).all()

    return render_template('dashboard/compose.html',
        course=course_obj,
        student_id=student_id,
        student_name=student_name,
        first_name=first_name,
        days_since=days_since,
        default_subject=default_subject,
        default_body=default_body,
        templates=templates,
    )


@bp.route('/course/<int:course_id>/group-compose', methods=['GET', 'POST'])
def group_compose(course_id):
    client = CanvasClient()

    try:
        course_obj = client.get_course(course_id)
    except Exception as exc:
        flash(f'Could not load course info: {exc}')
        course_obj = {'name': f'Course {course_id}', 'course_code': '', 'id': course_id}

    try:
        enrollments = client.get_enrollments(course_id)
    except Exception as exc:
        flash(f'Could not load enrollments: {exc}')
        enrollments = []

    enrollment_by_id = {e['user_id']: e for e in enrollments}

    # Parse student IDs from query param (GET) or hidden field (POST)
    raw_ids = request.args.get('students', '') if request.method == 'GET' else request.form.get('students', '')
    student_ids = []
    for part in raw_ids.split(','):
        part = part.strip()
        if part.isdigit():
            student_ids.append(int(part))

    if not student_ids:
        flash('No students selected.')
        return redirect(url_for('dashboard.course', course_id=course_id))

    # Build recipient list with names
    recipients = []
    for sid in student_ids:
        enr = enrollment_by_id.get(sid)
        if enr:
            name = enr['user'].get('name', f'Student {sid}')
        else:
            rec = StudentRecord.query.filter_by(course_id=course_id, student_canvas_id=sid).first()
            name = rec.name if rec else f'Student {sid}'
        recipients.append({'id': sid, 'name': name})

    if request.method == 'POST':
        subject = request.form.get('subject', '').strip()
        body_template = request.form.get('body', '').strip()
        gid = str(uuid.uuid4())
        now = datetime.now(timezone.utc)
        sent_count = 0
        events = []

        for recip in recipients:
            first_name = recip['name'].split()[0] if recip['name'] else ''
            last_at = db.session.query(
                func.max(InteractionEvent.occurred_at)
            ).filter(
                InteractionEvent.course_id == course_id,
                InteractionEvent.student_canvas_id == recip['id'],
            ).scalar()
            days_since = (now - last_at).days if last_at else None
            ctx = {'first_name': first_name, 'days_since': days_since}
            filled_subject = fill_placeholders(subject, ctx)
            filled_body = fill_placeholders(body_template, ctx)

            try:
                result = client.send_message(recip['id'], filled_subject, filled_body, course_id=course_id)
                new_conv = result[0] if isinstance(result, list) and result else None
                if new_conv:
                    ts_str = new_conv.get('last_authored_at') or new_conv.get('last_message_at')
                    occurred_at = datetime.fromisoformat(ts_str) if ts_str else now
                    events.append({
                        'course_id': course_id,
                        'student_canvas_id': recip['id'],
                        'event_type': 'group_conversation',
                        'occurred_at': occurred_at,
                        'source_id': new_conv['id'],
                        'group_id': gid,
                    })
                sent_count += 1
            except Exception as exc:
                current_app.logger.error('group send failed for %s: %s', recip['id'], exc)
                flash(f'Could not send to {recip["name"]}: {exc}')

        if events:
            stmt = pg_insert(InteractionEvent.__table__).values(events)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_interaction_event_type_source_student',
                set_={
                    'occurred_at': stmt.excluded.occurred_at,
                    'group_id': stmt.excluded.group_id,
                },
            )
            db.session.execute(stmt)

        # Invalidate conversation caches so next sync picks up new messages
        # and any replies that follow shortly after.
        sent_key = CanvasClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})
        inbox_key = CanvasClient._make_cache_key('/api/v1/conversations', {'scope': 'inbox'})
        CanvasCache.query.filter(
            CanvasCache.cache_key.in_([sent_key, inbox_key])
        ).delete()

        db.session.commit()
        flash(f'Message sent to {sent_count} of {len(recipients)} students.')
        return redirect(url_for('dashboard.course', course_id=course_id))

    display_name = CourseDisplayName.query.filter_by(course_id=course_id).first()
    display_name = display_name.name if display_name else course_obj.get('name', f'Course {course_id}')

    templates = MessageTemplate.query.order_by(MessageTemplate.created_at.desc()).all()

    return render_template('dashboard/group_compose.html',
        course=course_obj,
        display_name=display_name,
        recipients=recipients,
        student_ids=','.join(str(s) for s in student_ids),
        default_subject='Checking in',
        default_body='Hi <name>, ',
        templates=templates,
    )


@bp.route('/message-templates', methods=['POST'])
def save_message_template():
    data = request.get_json(force=True)
    name = data.get('name', '').strip()
    if not name:
        return {'ok': False, 'error': 'Name is required'}, 400
    tpl = MessageTemplate(
        name=name,
        subject=data.get('subject', '').strip(),
        body=data.get('body', '').strip(),
    )
    db.session.add(tpl)
    db.session.commit()
    return {'ok': True, 'template': {
        'id': tpl.id,
        'name': tpl.name,
        'subject': tpl.subject,
        'body': tpl.body,
        'created_at': tpl.created_at.strftime('%m/%d'),
    }}


@bp.route('/message-templates/<int:template_id>', methods=['DELETE'])
def delete_message_template(template_id):
    tpl = MessageTemplate.query.get_or_404(template_id)
    db.session.delete(tpl)
    db.session.commit()
    return {'ok': True}
