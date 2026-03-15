"""Tests for dashboard routes.

Canvas API calls (CanvasClient) and sync are mocked.  InteractionEvent rows
are seeded directly into the test DB so we can verify the route's staleness
logic, sort order, and timeline rendering without needing a real Canvas token.
"""
import json as _json
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.course_display_name import CourseDisplayName
from app.models.interaction_event import InteractionEvent
from app.models.student_note import StudentNote

COURSE_ID = 99
STUDENT_A = 101
STUDENT_B = 102


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_client(enrollments=None):
    """Return a pre-configured CanvasClient mock."""
    mock = MagicMock()
    mock.get_course.return_value = {
        'id': COURSE_ID, 'name': 'Test Course', 'course_code': 'CS101',
    }
    mock.get_enrollments.return_value = enrollments or [
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'}},
    ]
    return mock


def _seed_event(days_ago, student_id=STUDENT_A, source_id=1001, event_type='conversation'):
    occurred_at = datetime.now(timezone.utc) - timedelta(days=days_ago)
    db.session.add(InteractionEvent(
        course_id=COURSE_ID,
        student_canvas_id=student_id,
        event_type=event_type,
        occurred_at=occurred_at,
        source_id=source_id,
    ))
    db.session.commit()


# ---------------------------------------------------------------------------
# Index
# ---------------------------------------------------------------------------

def test_index_200(client):
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.return_value = []
        response = client.get('/')
    assert response.status_code == 200


def test_index_shows_course_name(client):
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.return_value = [
            {'id': COURSE_ID, 'name': 'Intro to Python', 'course_code': 'CS101', 'term': None},
        ]
        response = client.get('/')
    assert b'Intro to Python' in response.data


def test_index_canvas_error_returns_200(client):
    """Canvas API failure should show the page (with flash), not crash."""
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.side_effect = Exception('network error')
        response = client.get('/')
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Course page
# ---------------------------------------------------------------------------

def test_course_page_200(client):
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert response.status_code == 200


def test_course_page_shows_student_name(client):
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'Smith, Alice' in response.data


# ---------------------------------------------------------------------------
# Course page tabs
# ---------------------------------------------------------------------------

def test_course_submissions_tab_200(client):
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}?tab=submissions')
    assert response.status_code == 200
    assert b'Submissions view coming soon' in response.data
    assert b'<table class="timeline">' not in response.data


def test_course_analytics_tab_200(client):
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}?tab=analytics')
    assert response.status_code == 200
    assert b'Analytics view coming soon' in response.data
    assert b'<table class="timeline">' not in response.data


# ---------------------------------------------------------------------------
# Staleness coloring  (class names come from tr class="row-{{ student.staleness }}")
# ---------------------------------------------------------------------------

def test_staleness_green(client):
    _seed_event(days_ago=3)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-ok' in response.data


def test_staleness_yellow(client):
    _seed_event(days_ago=10)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-warm' in response.data


def test_staleness_red_over_threshold(client):
    _seed_event(days_ago=20)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-hot' in response.data


def test_staleness_never_contacted_is_red(client):
    # No events seeded → student has never been contacted
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-hot' in response.data
    assert b'no contact' in response.data


def test_staleness_boundary_exactly_at_warn(client):
    """Exactly STALE_WARN_DAYS (7) days ago → still green (≤ threshold)."""
    _seed_event(days_ago=7)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-ok' in response.data


def test_staleness_one_day_over_warn(client):
    """8 days ago → yellow (> STALE_WARN_DAYS, ≤ STALE_ALERT_DAYS)."""
    _seed_event(days_ago=8)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-warm' in response.data


def test_staleness_boundary_exactly_at_alert(client):
    """Exactly STALE_ALERT_DAYS (14) days ago → still yellow (≤ threshold)."""
    _seed_event(days_ago=14)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-warm' in response.data


def test_staleness_one_day_over_alert(client):
    """15 days ago → red (> STALE_ALERT_DAYS)."""
    _seed_event(days_ago=15)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'tier-hot' in response.data


# ---------------------------------------------------------------------------
# Sort order
# ---------------------------------------------------------------------------

def test_sort_order_never_contacted_before_recent(client):
    """Students with no contact appear before those with a recent interaction."""
    _seed_event(days_ago=3, student_id=STUDENT_A, source_id=1001)
    # STUDENT_B has no events → never contacted

    enrollments = [
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'}},
        {'user_id': STUDENT_B, 'user': {'sortable_name': 'Jones, Bob'}},
    ]
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client(enrollments=enrollments)):
        response = client.get(f'/course/{COURSE_ID}')

    html = response.data.decode()
    assert html.index('Jones, Bob') < html.index('Smith, Alice')


def test_sort_order_older_contact_before_newer(client):
    """Among contacted students, the one contacted longest ago comes first."""
    _seed_event(days_ago=10, student_id=STUDENT_A, source_id=1001)
    _seed_event(days_ago=3,  student_id=STUDENT_B, source_id=1002)

    enrollments = [
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'}},
        {'user_id': STUDENT_B, 'user': {'sortable_name': 'Jones, Bob'}},
    ]
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client(enrollments=enrollments)):
        response = client.get(f'/course/{COURSE_ID}')

    html = response.data.decode()
    # Smith (10 days ago) should appear before Jones (3 days ago)
    assert html.index('Smith, Alice') < html.index('Jones, Bob')


# ---------------------------------------------------------------------------
# Student detail page
# ---------------------------------------------------------------------------

def _mock_client_with_name(name='Alice Smith'):
    mock = _mock_client(enrollments=[
        {'user_id': STUDENT_A, 'user': {'name': name, 'sortable_name': name}},
    ])
    mock.get_discussion_entries.return_value = []
    return mock


def test_student_page_200(client):
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert response.status_code == 200


def test_student_page_shows_name(client):
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name('Alice Smith')):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'Alice Smith' in response.data


def test_student_page_unknown_student_still_200(client):
    """Requesting a student_id not in the enrollment list should not crash."""
    mock = _mock_client()
    mock.get_discussion_entries.return_value = []
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        response = client.get(f'/course/{COURSE_ID}/student/99999')
    assert response.status_code == 200


# ---------------------------------------------------------------------------
# Notes (save_note endpoint)
# ---------------------------------------------------------------------------

def test_save_note_creates_note(client):
    """POST to save_note creates a StudentNote row in the DB."""
    response = client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/note',
        data=_json.dumps({'content': 'Good progress'}),
        content_type='application/json',
    )
    assert response.status_code == 200
    note = StudentNote.query.filter_by(
        course_id=COURSE_ID, student_canvas_id=STUDENT_A
    ).first()
    assert note is not None
    assert note.content == 'Good progress'


def test_save_note_updates_existing(client):
    """A second POST to save_note overwrites the first — exactly one row remains."""
    for content in ('First note', 'Updated note'):
        client.post(
            f'/course/{COURSE_ID}/student/{STUDENT_A}/note',
            data=_json.dumps({'content': content}),
            content_type='application/json',
        )
    assert StudentNote.query.count() == 1
    assert StudentNote.query.first().content == 'Updated note'


# ---------------------------------------------------------------------------
# Compose page
# ---------------------------------------------------------------------------

def test_compose_get_200(client):
    """GET /compose returns 200 and includes the student's name."""
    with patch('app.routes.dashboard.CanvasClient',
               return_value=_mock_client_with_name('Alice Smith')):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}/compose')
    assert response.status_code == 200
    assert b'Alice Smith' in response.data


def _compose_post(client, conv_response=None):
    """Helper: POST to compose with a mocked send_message return value."""
    from app.services.canvas_client import CanvasClient as _RealClient
    if conv_response is None:
        conv_response = [{'id': 9999, 'last_authored_at': '2026-03-06T12:00:00+00:00'}]
    with patch('app.routes.dashboard.CanvasClient') as MockClass:
        MockClass.return_value = _mock_client_with_name('Alice Smith')
        MockClass.return_value.send_message.return_value = conv_response
        MockClass._make_cache_key.side_effect = _RealClient._make_cache_key
        return client.post(
            f'/course/{COURSE_ID}/student/{STUDENT_A}/compose',
            data={'subject': 'Checking in', 'body': 'Hi Alice'},
        )


def test_compose_post_redirects_to_student(client):
    """POST /compose redirects back to the student detail page."""
    response = _compose_post(client)
    assert response.status_code == 302
    assert f'/course/{COURSE_ID}/student/{STUDENT_A}' in response.location


def test_compose_post_writes_interaction_event(client):
    """A successful send creates an InteractionEvent directly in the DB."""
    _compose_post(client)
    event = InteractionEvent.query.filter_by(
        course_id=COURSE_ID,
        student_canvas_id=STUDENT_A,
        event_type='conversation',
        source_id=9999,
    ).first()
    assert event is not None


def test_compose_post_prepends_to_sent_cache(client):
    """Sending prepends the new conv to the existing sent cache without replacing it."""
    from app.services.canvas_client import CanvasClient as _RealClient
    sent_key = _RealClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})
    db.session.add(CanvasCache(
        cache_key=sent_key,
        response_json=[{'id': 1}],
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=1800,
    ))
    db.session.commit()

    _compose_post(client)

    entry = CanvasCache.query.filter_by(cache_key=sent_key).first()
    assert entry is not None
    assert entry.response_json[0]['id'] == 9999  # new conv first
    assert entry.response_json[1]['id'] == 1     # old conv preserved


def test_compose_post_creates_sent_cache_when_missing(client):
    """If there is no existing sent cache, a new entry is created."""
    from app.services.canvas_client import CanvasClient as _RealClient
    sent_key = _RealClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})

    _compose_post(client)

    entry = CanvasCache.query.filter_by(cache_key=sent_key).first()
    assert entry is not None
    assert entry.response_json[0]['id'] == 9999


# ---------------------------------------------------------------------------
# flush-cache
# ---------------------------------------------------------------------------

# ---------------------------------------------------------------------------
# Display name
# ---------------------------------------------------------------------------

def test_save_display_name_creates_row(client):
    response = client.post(
        f'/course/{COURSE_ID}/display-name',
        data=_json.dumps({'name': 'My Custom Name'}),
        content_type='application/json',
    )
    assert response.status_code == 200
    row = CourseDisplayName.query.filter_by(course_id=COURSE_ID).first()
    assert row is not None
    assert row.name == 'My Custom Name'


def test_save_display_name_updates_existing(client):
    for name in ('First Name', 'Updated Name'):
        client.post(
            f'/course/{COURSE_ID}/display-name',
            data=_json.dumps({'name': name}),
            content_type='application/json',
        )
    assert CourseDisplayName.query.count() == 1
    assert CourseDisplayName.query.first().name == 'Updated Name'


def test_save_display_name_rejects_empty(client):
    response = client.post(
        f'/course/{COURSE_ID}/display-name',
        data=_json.dumps({'name': '  '}),
        content_type='application/json',
    )
    assert response.status_code == 400


def test_course_page_shows_custom_display_name(client):
    db.session.add(CourseDisplayName(course_id=COURSE_ID, name='My Renamed Course'))
    db.session.commit()
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'My Renamed Course' in response.data


def test_course_page_falls_back_to_canvas_name(client):
    """Without a custom display name, the Canvas course name is shown."""
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'Test Course' in response.data


def test_index_shows_custom_display_name(client):
    db.session.add(CourseDisplayName(course_id=COURSE_ID, name='Index Custom Name'))
    db.session.commit()
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.return_value = [
            {'id': COURSE_ID, 'name': 'Intro to Python', 'course_code': 'CS101', 'term': None},
        ]
        response = client.get('/')
    html = response.data.decode()
    assert 'class="card-title">Index Custom Name<' in html


# ---------------------------------------------------------------------------
# flush-cache
# ---------------------------------------------------------------------------

def test_flush_cache_deletes_entries_and_redirects(client):
    db.session.add(CanvasCache(
        cache_key='abc123',
        response_json={},
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=3600,
    ))
    db.session.commit()

    response = client.post(f'/course/{COURSE_ID}/flush-cache')

    assert response.status_code == 302
    assert CanvasCache.query.count() == 0
