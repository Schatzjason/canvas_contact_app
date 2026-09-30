"""Tests for the single-student refresh: sync_student, the staleness rule that
triggers it on page load, and the POST /refresh endpoint. CanvasClient is mocked."""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

import pytest

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent
from app.services import sync as sync_mod
from app.services.sync import (
    _get_sync_marker, _set_sync_marker, last_student_refresh, student_needs_refresh,
    sync_student,
)

COURSE_ID = 99
STUDENT = 101
OTHER = 102
INSTRUCTOR = 500


def _ago(**kw):
    return (datetime.now(timezone.utc) - timedelta(**kw)).isoformat()


def _client(sent=(), inbox=(), details=None, submissions=(), topics=(), entries=None):
    mock = MagicMock()
    mock.get_course.return_value = {'id': COURSE_ID, 'start_at': _ago(days=30)}
    mock.get_current_user.return_value = {'id': INSTRUCTOR}
    mock.get_student_conversations.side_effect = (
        lambda student_id, scope: list(sent if scope == 'sent' else inbox))
    mock.get_conversation.side_effect = lambda cid, **_: (details or {})[cid]
    mock.get_student_submissions.return_value = list(submissions)
    mock.get_discussion_topics.return_value = list(topics)
    mock.get_discussion_entries.side_effect = lambda c, t, **_: (entries or {}).get(t, [])
    return mock


def _events():
    return {(e.event_type, e.source_id, e.student_canvas_id)
            for e in InteractionEvent.query.all()}


def test_sync_student_records_all_kinds_and_only_this_students_data():
    client = _client(
        sent=[{'id': 1, 'last_message_at': _ago(days=1)}],
        inbox=[{'id': 2, 'last_message_at': _ago(hours=5)}],
        details={
            1: {'messages': [
                {'id': 11, 'created_at': _ago(days=2), 'author_id': INSTRUCTOR},
                # the student's reply inside a thread the instructor started
                {'id': 12, 'created_at': _ago(days=1), 'author_id': STUDENT},
                {'id': 13, 'created_at': _ago(days=1), 'author_id': OTHER}]},
            2: {'messages': [{'id': 21, 'created_at': _ago(hours=5), 'author_id': STUDENT}]},
        },
        submissions=[
            {'id': 31, 'attempt': 1, 'submitted_at': _ago(days=3)},
            {'id': 32, 'attempt': None, 'submitted_at': None},                # grade only
            {'id': 33, 'attempt': 1, 'submitted_at': _ago(days=3),
             'submission_type': 'discussion_topic'},                         # counted as a post
        ],
        topics=[{'id': 7, 'title': 'D1'}],
        entries={7: [
            {'id': 41, 'user_id': STUDENT, 'created_at': _ago(days=2),
             'recent_replies': [{'id': 42, 'user_id': INSTRUCTOR, 'created_at': _ago(days=1)}]},
            {'id': 43, 'user_id': OTHER, 'created_at': _ago(days=2), 'recent_replies': []},
        ]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        count = sync_student(COURSE_ID, STUDENT)

    assert _events() == {
        ('conversation', 11, STUDENT), ('student_message', 12, STUDENT),
        ('student_message', 21, STUDENT), ('submission', 31, STUDENT),
        ('discussion_entry', 41, STUDENT), ('discussion_instructor_reply', 42, STUDENT),
    }
    assert count == 6
    # discussions must be re-fetched, not served from the 2 h cache
    client.get_discussion_entries.assert_called_with(COURSE_ID, 7, refresh=True)


def test_sync_student_is_idempotent():
    client = _client(submissions=[{'id': 31, 'attempt': 1, 'submitted_at': _ago(days=3)}])
    with patch('app.services.sync.CanvasClient', return_value=client):
        sync_student(COURSE_ID, STUDENT)
        sync_student(COURSE_ID, STUDENT)
    assert InteractionEvent.query.count() == 1


def test_sync_student_leaves_course_sync_markers_alone():
    """Advancing the course-wide markers would make the next full sync skip
    every other student's recent messages."""
    _set_sync_marker(COURSE_ID, 'conv_inbox')
    before = _get_sync_marker(COURSE_ID, 'conv_inbox')
    with patch('app.services.sync.CanvasClient', return_value=_client()):
        sync_student(COURSE_ID, STUDENT)
    assert _get_sync_marker(COURSE_ID, 'conv_inbox') == before
    assert _get_sync_marker(COURSE_ID, 'conv_sent') is None


def test_failed_refresh_raises_and_is_not_marked_fresh():
    client = _client()
    client.get_student_submissions.side_effect = RuntimeError('canvas down')
    with patch('app.services.sync.CanvasClient', return_value=client):
        with pytest.raises(RuntimeError):
            sync_student(COURSE_ID, STUDENT)
    assert student_needs_refresh(COURSE_ID, STUDENT)


def test_discussion_failure_is_not_swallowed():
    client = _client()
    client.get_discussion_topics.side_effect = RuntimeError('boom')
    with patch('app.services.sync.CanvasClient', return_value=client):
        with pytest.raises(RuntimeError):
            sync_student(COURSE_ID, STUDENT)
    assert student_needs_refresh(COURSE_ID, STUDENT)


# ---------------------------------------------------------------------------
# Staleness rule
# ---------------------------------------------------------------------------

def _backdate(scope, minutes):
    key = f'sync_marker:{scope}:{COURSE_ID}'
    entry = CanvasCache.query.filter_by(cache_key=key).first()
    entry.fetched_at = datetime.now(timezone.utc) - timedelta(minutes=minutes)
    db.session.commit()


def test_never_refreshed_needs_refresh():
    assert last_student_refresh(COURSE_ID, STUDENT) is None
    assert student_needs_refresh(COURSE_ID, STUDENT)


def test_recent_refresh_does_not_need_refresh():
    _set_sync_marker(COURSE_ID, f'student_{STUDENT}')
    _backdate(f'student_{STUDENT}', 59)
    assert not student_needs_refresh(COURSE_ID, STUDENT)


def test_refresh_older_than_an_hour_needs_refresh():
    _set_sync_marker(COURSE_ID, f'student_{STUDENT}')
    _backdate(f'student_{STUDENT}', 61)
    assert student_needs_refresh(COURSE_ID, STUDENT)


def test_recent_full_course_sync_counts_as_a_refresh():
    _set_sync_marker(COURSE_ID, 'conv_inbox')
    assert not student_needs_refresh(COURSE_ID, STUDENT)


def test_other_students_refresh_does_not_count():
    _set_sync_marker(COURSE_ID, f'student_{OTHER}')
    assert student_needs_refresh(COURSE_ID, STUDENT)


# ---------------------------------------------------------------------------
# Endpoint + page
# ---------------------------------------------------------------------------

def test_refresh_endpoint_ok(client):
    with patch('app.routes.dashboard.sync_student', return_value=3) as m:
        resp = client.post(f'/course/{COURSE_ID}/student/{STUDENT}/refresh')
    assert resp.status_code == 200
    assert resp.get_json() == {'ok': True, 'count': 3}
    m.assert_called_once_with(COURSE_ID, STUDENT)


def test_refresh_endpoint_reports_failure(client):
    with patch('app.routes.dashboard.sync_student', side_effect=RuntimeError('nope')):
        resp = client.post(f'/course/{COURSE_ID}/student/{STUDENT}/refresh')
    assert resp.status_code == 502
    assert resp.get_json()['ok'] is False


def test_refresh_endpoint_is_post_only(client):
    assert client.get(f'/course/{COURSE_ID}/student/{STUDENT}/refresh').status_code == 405


def _page(client):
    mock = MagicMock()
    mock.get_course.return_value = {'id': COURSE_ID, 'name': 'C', 'course_code': 'C1'}
    mock.get_enrollments.return_value = [
        {'user_id': STUDENT, 'user': {'name': 'Al'}, 'grades': {'current_score': None}}]
    mock.get_discussion_entries.return_value = []
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        return client.get(f'/course/{COURSE_ID}/student/{STUDENT}').data.decode()


def test_student_page_has_refresh_button_marked_stale_when_never_refreshed(client):
    html = _page(client)
    assert 'id="refresh-btn"' in html
    assert 'data-stale="true"' in html


def test_student_page_not_stale_after_recent_refresh(client):
    _set_sync_marker(COURSE_ID, f'student_{STUDENT}')
    assert 'data-stale="false"' in _page(client)
