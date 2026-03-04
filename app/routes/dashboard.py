import json
from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, Response, current_app, flash, render_template, stream_with_context
from sqlalchemy import func

from flask import redirect, url_for

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient
from app.services.sync import run_sync, sync_course

bp = Blueprint('dashboard', __name__)


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
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        sid = event.student_canvas_id
        day = event.occurred_at.date()
        active_days_by_student.setdefault(sid, {}).setdefault(day, set()).add(event.event_type)

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
    )
