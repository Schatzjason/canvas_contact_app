import json
import re
from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, Response, current_app, flash, render_template, stream_with_context
from sqlalchemy import func, text

from flask import redirect, url_for

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient
from app.services.sync import run_sync, sync_course

bp = Blueprint('dashboard', __name__)

PINNED_DISCUSSION_TOPIC_ID = 1461939


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

    return render_template('dashboard/index.html',
        courses=courses,
        stats_by_course=stats_by_course,
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

    today = datetime.now(timezone.utc).date()
    # 21 columns: today-20 (oldest) through today (newest)
    days = [today - timedelta(days=i) for i in range(20, -1, -1)]
    window_start_dt = datetime.combine(days[0], time.min, tzinfo=timezone.utc)

    warn_days = current_app.config['STALE_WARN_DAYS']
    alert_days = current_app.config['STALE_ALERT_DAYS']

    # Last interaction date per student (all time, not just the window)
    last_by_student = {
        row.student_canvas_id: row.last_at.date()
        for row in db.session.query(
            InteractionEvent.student_canvas_id,
            func.max(InteractionEvent.occurred_at).label('last_at'),
        ).filter(
            InteractionEvent.course_id == course_id,
        ).group_by(InteractionEvent.student_canvas_id).all()
    }

    # Which days within the 21-day window had an interaction, per student
    # active_days_by_student: {student_id: {date: set(event_types)}}
    active_days_by_student = {}
    disc_source_ids = {}  # {(student_id, date): [source_id, ...]}
    msg_source_ids  = {}  # {(student_id, date): [source_id, ...]}
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        sid = event.student_canvas_id
        day = event.occurred_at.date()
        active_days_by_student.setdefault(sid, {}).setdefault(day, set()).add(event.event_type)
        if event.event_type in ('discussion_entry', 'discussion_reply'):
            disc_source_ids.setdefault((sid, day), []).append(event.source_id)
        if event.event_type in ('conversation', 'student_message'):
            msg_source_ids.setdefault((sid, day), []).append(event.source_id)

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
            WHERE e->>'subject' IS NOT NULL
              AND (e->>'id')::bigint = ANY(:ids)
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

    students = []
    for enrollment in enrollments:
        user = enrollment.get('user', {})
        canvas_id = enrollment['user_id']
        last_date = last_by_student.get(canvas_id)

        if last_date is None:
            days_since = None
            staleness = 'red'
        else:
            days_since = (today - last_date).days
            if days_since <= warn_days:
                staleness = 'green'
            elif days_since <= alert_days:
                staleness = 'yellow'
            else:
                staleness = 'red'

        students.append({
            'canvas_id': canvas_id,
            'name': user.get('sortable_name') or user.get('name', f'Student {canvas_id}'),
            'last_date': last_date,
            'days_since': days_since,
            'staleness': staleness,
            'active_days': active_days_by_student.get(canvas_id, {}),
        })

    # No interaction ever → first; then ascending by last interaction date
    students.sort(key=lambda s: (s['last_date'] is not None, s['last_date'] or date.min))

    return render_template('dashboard/course.html',
        course=course_obj,
        students=students,
        days=days,
        today=today,
        disc_messages=disc_messages,
        msg_texts=msg_texts,
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
    else:
        student_name = f'Student {student_id}'

    today = datetime.now(timezone.utc).date()
    days = [today - timedelta(days=i) for i in range(20, -1, -1)]
    window_start_dt = datetime.combine(days[0], time.min, tzinfo=timezone.utc)

    active_days = {}
    event_source_ids = {}  # {(day, event_type): [source_id, ...]}
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.student_canvas_id == student_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        day = event.occurred_at.date()
        active_days.setdefault(day, set()).add(event.event_type)
        event_source_ids.setdefault((day, event.event_type), []).append(event.source_id)

    # ── Fetch text content from canvas cache for drawer ───────
    disc_ids = [s for (d, et), ss in event_source_ids.items()
                if et in ('discussion_entry', 'discussion_reply') for s in ss]
    instr_disc_ids = [s for (d, et), ss in event_source_ids.items()
                      if et == 'discussion_instructor_reply' for s in ss]
    msg_ids = [s for (d, et), ss in event_source_ids.items()
               if et in ('conversation', 'student_message') for s in ss]

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
            WHERE e->>'subject' IS NOT NULL
              AND (e->>'id')::bigint = ANY(:ids)
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

        for s in event_source_ids.get((day, 'student_message'), []):
            sections.append({'label': 'Student Message',
                             'text': conv_text_by_src.get(s, '')})

        day_drawer[day.isoformat()] = {
            'date_label': day.strftime('%A') + ', ' + day.strftime('%B') + ' ' + str(day.day),
            'sections': sections,
        }

    pinned_post = None
    try:
        entries = client.get_discussion_entries(course_id, PINNED_DISCUSSION_TOPIC_ID)
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

    return render_template('dashboard/student.html',
        course=course_obj,
        student_id=student_id,
        student_name=student_name,
        days_since=days_since,
        staleness=staleness,
        pinned_post=pinned_post,
        days=days,
        today=today,
        active_days=active_days,
        day_drawer=day_drawer,
    )
