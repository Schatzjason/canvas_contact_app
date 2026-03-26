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
from app.models.check_back_date import CheckBackDate
from app.models.interaction_event import InteractionEvent
from app.models.message_template import MessageTemplate
from app.models.pinned_discussion import PinnedDiscussion
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
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'},
         'grades': {'current_score': None}},
    ]
    mock.get_discussion_entries.return_value = []
    mock.get_courses.return_value = [
        {'id': COURSE_ID, 'name': 'Test Course', 'course_code': 'CS101', 'term': None},
    ]
    return mock


def _seed_event(days_ago, student_id=STUDENT_A, source_id=1001, event_type='student_message'):
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


def test_index_shows_check_back_rows(client):
    """Check-back dates appear on the index page with student name and formatted date."""
    from datetime import date as date_cls
    db.session.add(CheckBackDate(
        course_id=COURSE_ID, student_canvas_id=STUDENT_A,
        date=date_cls(2026, 3, 22), note='Follow up',
    ))
    db.session.commit()
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.return_value = [
            {'id': COURSE_ID, 'name': 'Test Course', 'course_code': 'CS101', 'term': None},
        ]
        MockClient.return_value.get_enrollments.return_value = [
            {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'}},
        ]
        response = client.get('/')
    html = response.data.decode()
    assert 'Check Back' in html
    assert 'Smith, Alice' in html
    assert 'Sunday 3/22' in html
    assert 'Follow up' in html


def test_index_embeds_search_students(client):
    """The index page embeds student search data as JSON for the nav search."""
    with patch('app.routes.dashboard.CanvasClient') as MockClient:
        MockClient.return_value.get_courses.return_value = [
            {'id': COURSE_ID, 'name': 'Test Course', 'course_code': 'CS101-932', 'term': None},
        ]
        MockClient.return_value.get_enrollments.return_value = [
            {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'},
             'grades': {'current_score': None}},
        ]
        response = client.get('/')
    html = response.data.decode()
    # The student name should appear in the embedded JSON
    assert 'Smith, Alice' in html
    # Section extracted from course_code after last hyphen
    assert '932' in html


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


def test_course_page_shows_student_score(client):
    enrollments = [
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'},
         'grades': {'current_score': 91.2}},
    ]
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client(enrollments=enrollments)):
        response = client.get(f'/course/{COURSE_ID}')
    assert b'91.2%' in response.data


# ---------------------------------------------------------------------------
# Course page tabs
# ---------------------------------------------------------------------------

def test_course_instructor_contact_tab_200(client):
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client()):
        response = client.get(f'/course/{COURSE_ID}?tab=submissions')
    assert response.status_code == 200
    assert b'Message sent' in response.data
    assert b'Discussion reply' in response.data


def test_course_instructor_tab_sorts_by_instructor_contact(client):
    """On the instructor contact tab, students with no instructor contact appear first."""
    # STUDENT_A has an instructor conversation, STUDENT_B has none
    _seed_event(days_ago=3, student_id=STUDENT_A, source_id=2001, event_type='conversation')

    enrollments = [
        {'user_id': STUDENT_A, 'user': {'sortable_name': 'Smith, Alice'}},
        {'user_id': STUDENT_B, 'user': {'sortable_name': 'Jones, Bob'}},
    ]
    with patch('app.routes.dashboard.run_sync', return_value=0), \
         patch('app.routes.dashboard.CanvasClient', return_value=_mock_client(enrollments=enrollments)):
        response = client.get(f'/course/{COURSE_ID}?tab=submissions')

    html = response.data.decode()
    # Jones (never contacted by instructor) should appear before Smith
    assert html.index('Jones, Bob') < html.index('Smith, Alice')


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


def test_student_page_shows_score(client):
    mock = _mock_client(enrollments=[
        {'user_id': STUDENT_A, 'user': {'name': 'Alice Smith', 'sortable_name': 'Alice Smith'},
         'grades': {'current_score': 87.5}},
    ])
    mock.get_discussion_entries.return_value = []
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'87.5%' in response.data


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
# Check-back date
# ---------------------------------------------------------------------------

def test_save_check_back_creates_row(client):
    response = client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': '2026-04-15'}),
        content_type='application/json',
    )
    assert response.status_code == 200
    row = CheckBackDate.query.filter_by(
        course_id=COURSE_ID, student_canvas_id=STUDENT_A
    ).first()
    assert row is not None
    assert row.date.isoformat() == '2026-04-15'


def test_save_check_back_updates_existing(client):
    for d in ('2026-04-15', '2026-05-01'):
        client.post(
            f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
            data=_json.dumps({'date': d}),
            content_type='application/json',
        )
    assert CheckBackDate.query.count() == 1
    assert CheckBackDate.query.first().date.isoformat() == '2026-05-01'


def test_save_check_back_clear(client):
    client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': '2026-04-15'}),
        content_type='application/json',
    )
    assert CheckBackDate.query.count() == 1

    response = client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': ''}),
        content_type='application/json',
    )
    assert response.status_code == 200
    assert CheckBackDate.query.count() == 0


def test_save_check_back_rejects_invalid_date(client):
    response = client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': 'not-a-date'}),
        content_type='application/json',
    )
    assert response.status_code == 400


def test_save_check_back_with_note(client):
    response = client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': '2026-04-15', 'note': 'Follow up on essay'}),
        content_type='application/json',
    )
    assert response.status_code == 200
    row = CheckBackDate.query.first()
    assert row.note == 'Follow up on essay'
    data = _json.loads(response.data)
    assert data['note'] == 'Follow up on essay'


def test_save_check_back_note_truncated_to_60(client):
    long_note = 'x' * 100
    client.post(
        f'/course/{COURSE_ID}/student/{STUDENT_A}/check-back',
        data=_json.dumps({'date': '2026-04-15', 'note': long_note}),
        content_type='application/json',
    )
    row = CheckBackDate.query.first()
    assert len(row.note) == 60


def test_student_page_shows_check_back_date(client):
    from datetime import date
    db.session.add(CheckBackDate(
        course_id=COURSE_ID, student_canvas_id=STUDENT_A,
        date=date(2026, 4, 15),
    ))
    db.session.commit()
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'2026-04-15' in response.data


def test_student_page_shows_check_back_note(client):
    from datetime import date
    db.session.add(CheckBackDate(
        course_id=COURSE_ID, student_canvas_id=STUDENT_A,
        date=date(2026, 4, 15), note='Ask about project',
    ))
    db.session.commit()
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'Ask about project' in response.data


# ---------------------------------------------------------------------------
# Pinned discussion
# ---------------------------------------------------------------------------

def test_save_pinned_discussion(client):
    response = client.post(
        f'/course/{COURSE_ID}/pinned-discussion',
        data=_json.dumps({'topic_id': 201}),
        content_type='application/json',
    )
    assert response.status_code == 200
    row = PinnedDiscussion.query.filter_by(course_id=COURSE_ID).first()
    assert row is not None
    assert row.topic_id == 201


def test_save_pinned_discussion_updates_existing(client):
    for tid in (201, 202):
        client.post(
            f'/course/{COURSE_ID}/pinned-discussion',
            data=_json.dumps({'topic_id': tid}),
            content_type='application/json',
        )
    assert PinnedDiscussion.query.count() == 1
    assert PinnedDiscussion.query.first().topic_id == 202


def test_save_pinned_discussion_rejects_empty(client):
    response = client.post(
        f'/course/{COURSE_ID}/pinned-discussion',
        data=_json.dumps({}),
        content_type='application/json',
    )
    assert response.status_code == 400


def test_student_page_shows_set_button_when_no_pinned(client):
    """When no pinned discussion is set, the page shows a 'Set' button."""
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'pin-set-btn' in response.data
    assert b'No discussion board selected' in response.data


def test_student_page_shows_edit_button_when_pinned(client):
    """When a pinned discussion is set, the page shows a pencil edit button."""
    db.session.add(PinnedDiscussion(course_id=COURSE_ID, topic_id=201))
    db.session.commit()
    mock = _mock_client_with_name()
    mock.get_discussion_entries.return_value = []
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'id="pin-edit-btn"' in response.data
    assert b'id="pin-set-btn"' not in response.data


def test_discussion_topics_endpoint(client):
    mock = _mock_client_with_name()
    mock.get_discussion_topics = MagicMock(return_value=[
        {'id': 201, 'title': 'Week 1'},
        {'id': 202, 'title': 'Week 2'},
    ])
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        response = client.get(f'/course/{COURSE_ID}/discussion-topics')
    data = _json.loads(response.data)
    assert data['ok'] is True
    assert len(data['topics']) == 2
    assert data['topics'][0]['title'] == 'Week 1'


def test_discussion_topics_canvas_error(client):
    mock = _mock_client_with_name()
    mock.get_discussion_topics = MagicMock(side_effect=Exception('Canvas down'))
    with patch('app.routes.dashboard.CanvasClient', return_value=mock):
        response = client.get(f'/course/{COURSE_ID}/discussion-topics')
    assert response.status_code == 500
    data = _json.loads(response.data)
    assert data['ok'] is False


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
# Course stats endpoint
# ---------------------------------------------------------------------------

def test_course_stats_returns_badge(client):
    _seed_event(days_ago=2)
    response = client.get(f'/course/{COURSE_ID}/stats')
    assert response.status_code == 200
    data = _json.loads(response.data)
    assert 'badge_text' in data
    assert 'badge_class' in data
    assert 'active_count' in data
    assert data['active_count'] >= 1


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


# ---------------------------------------------------------------------------
# Message templates
# ---------------------------------------------------------------------------

def test_save_template_creates_row(client):
    response = client.post(
        '/message-templates',
        data=_json.dumps({'name': 'Weekly check-in', 'subject': 'Hi', 'body': 'How are you?'}),
        content_type='application/json',
    )
    assert response.status_code == 200
    data = _json.loads(response.data)
    assert data['ok'] is True
    assert data['template']['name'] == 'Weekly check-in'
    assert data['template']['subject'] == 'Hi'
    assert data['template']['body'] == 'How are you?'
    assert 'created_at' in data['template']
    assert MessageTemplate.query.count() == 1


def test_save_template_rejects_empty_name(client):
    response = client.post(
        '/message-templates',
        data=_json.dumps({'name': '  ', 'subject': 'Hi', 'body': 'body'}),
        content_type='application/json',
    )
    assert response.status_code == 400
    data = _json.loads(response.data)
    assert data['ok'] is False


def test_delete_template(client):
    tpl = MessageTemplate(name='Old', subject='S', body='B')
    db.session.add(tpl)
    db.session.commit()
    tpl_id = tpl.id

    response = client.delete(f'/message-templates/{tpl_id}')
    assert response.status_code == 200
    data = _json.loads(response.data)
    assert data['ok'] is True
    assert MessageTemplate.query.count() == 0


def test_delete_template_404(client):
    response = client.delete('/message-templates/99999')
    assert response.status_code == 404


def test_compose_page_shows_templates(client):
    db.session.add(MessageTemplate(name='My Template', subject='Subj', body='Body text'))
    db.session.commit()
    with patch('app.routes.dashboard.CanvasClient',
               return_value=_mock_client_with_name('Alice Smith')):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}/compose')
    assert response.status_code == 200
    assert b'My Template' in response.data
    assert b'Body text' in response.data


# ---------------------------------------------------------------------------
# fill_placeholders
# ---------------------------------------------------------------------------

def test_fill_placeholders_replaces_name():
    from app.routes.dashboard import fill_placeholders
    result = fill_placeholders('Hi <name>, how are you?', {'first_name': 'Alice'})
    assert result == 'Hi Alice, how are you?'


def test_fill_placeholders_replaces_time():
    from app.routes.dashboard import fill_placeholders
    result = fill_placeholders('It has been <time> since we spoke.', {'days_since': 5})
    assert result == 'It has been 5 days since we spoke.'


def test_fill_placeholders_replaces_both():
    from app.routes.dashboard import fill_placeholders
    result = fill_placeholders(
        'Hi <name>, it has been <time>.',
        {'first_name': 'Bob', 'days_since': 3},
    )
    assert result == 'Hi Bob, it has been 3 days.'


def test_fill_placeholders_leaves_unknown_tokens():
    from app.routes.dashboard import fill_placeholders
    result = fill_placeholders('Hi <name>, check <other>.', {'first_name': 'Zoe'})
    assert result == 'Hi Zoe, check <other>.'


def test_fill_placeholders_no_context():
    from app.routes.dashboard import fill_placeholders
    result = fill_placeholders('Hi <name>, <time> ago.', {})
    assert result == 'Hi <name>, <time> ago.'


def test_compose_post_fills_placeholders(client):
    """POST /compose replaces <name> and <time> placeholders before sending."""
    _seed_event(days_ago=5)
    from app.services.canvas_client import CanvasClient as _RealClient
    with patch('app.routes.dashboard.CanvasClient') as MockClass:
        MockClass.return_value = _mock_client_with_name('Alice Smith')
        MockClass.return_value.send_message.return_value = [
            {'id': 9999, 'last_authored_at': '2026-03-06T12:00:00+00:00'}
        ]
        MockClass._make_cache_key.side_effect = _RealClient._make_cache_key
        client.post(
            f'/course/{COURSE_ID}/student/{STUDENT_A}/compose',
            data={'subject': 'Hey <name>', 'body': 'Hi <name>, it has been <time>.'},
        )
        args = MockClass.return_value.send_message.call_args
    sent_subject = args[0][1]
    sent_body = args[0][2]
    assert '<name>' not in sent_subject
    assert 'Alice' in sent_subject
    assert '<name>' not in sent_body
    assert 'Alice' in sent_body
    assert '<time>' not in sent_body
    assert '5 days' in sent_body
