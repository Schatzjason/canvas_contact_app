from datetime import date, datetime, time, timedelta, timezone

from flask import Blueprint, current_app, flash, render_template
from sqlalchemy import func

from app import db
from app.models.interaction_event import InteractionEvent
from app.services.canvas_client import CanvasClient
from app.services.sync import sync_course

bp = Blueprint('dashboard', __name__)


@bp.route('/')
def index():
    client = CanvasClient()
    try:
        courses = client.get_courses()
    except Exception as exc:
        flash(f'Could not load courses from Canvas: {exc}')
        courses = []
    return render_template('dashboard/index.html', courses=courses)


@bp.route('/course/<int:course_id>')
def course(course_id):
    client = CanvasClient()

    # Sync Canvas data first — caching means this is cheap on repeat visits
    try:
        sync_course(course_id)
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
    active_days_by_student = {}
    for event in InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.occurred_at >= window_start_dt,
    ).all():
        sid = event.student_canvas_id
        active_days_by_student.setdefault(sid, set()).add(event.occurred_at.date())

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
            'active_days': active_days_by_student.get(canvas_id, set()),
        })

    # No interaction ever → first; then ascending by last interaction date
    students.sort(key=lambda s: (s['last_date'] is not None, s['last_date'] or date.min))

    return render_template('dashboard/course.html',
        course=course_obj,
        students=students,
        days=days,
        today=today,
    )
