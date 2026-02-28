from flask import Blueprint, flash, render_template

from app.services.canvas_client import CanvasClient

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
