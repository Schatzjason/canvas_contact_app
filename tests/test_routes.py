"""Tests for dashboard routes.

Canvas API calls (CanvasClient) and sync are mocked.  InteractionEvent rows
are seeded directly into the test DB so we can verify the route's staleness
logic, sort order, and timeline rendering without needing a real Canvas token.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent

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
# Staleness coloring  (class names come from tr class="row-{{ student.staleness }}")
# ---------------------------------------------------------------------------

def test_staleness_green(client):
    _seed_event(days_ago=3)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-green' in response.data


def test_staleness_yellow(client):
    _seed_event(days_ago=10)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-yellow' in response.data


def test_staleness_red_over_threshold(client):
    _seed_event(days_ago=20)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-red' in response.data


def test_staleness_never_contacted_is_red(client):
    # No events seeded → student has never been contacted
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-red' in response.data
    assert b'no contact' in response.data


def test_staleness_boundary_exactly_at_warn(client):
    """Exactly STALE_WARN_DAYS (7) days ago → still green (≤ threshold)."""
    _seed_event(days_ago=7)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-green' in response.data


def test_staleness_one_day_over_warn(client):
    """8 days ago → yellow (> STALE_WARN_DAYS, ≤ STALE_ALERT_DAYS)."""
    _seed_event(days_ago=8)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-yellow' in response.data


def test_staleness_boundary_exactly_at_alert(client):
    """Exactly STALE_ALERT_DAYS (14) days ago → still yellow (≤ threshold)."""
    _seed_event(days_ago=14)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-yellow' in response.data


def test_staleness_one_day_over_alert(client):
    """15 days ago → red (> STALE_ALERT_DAYS)."""
    _seed_event(days_ago=15)
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'row-red' in response.data


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
