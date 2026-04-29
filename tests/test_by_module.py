"""Tests for the By Module section.

Covers the build_by_module_view helper and the rendered student page. Canvas
API calls are mocked; cache rows and CourseModule rows are seeded directly.
"""
import hashlib
import json
from datetime import date, datetime, time, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.course_module import CourseModule
from app.models.interaction_event import InteractionEvent
from app.services.by_module import build_by_module_view

COURSE_ID = 99
STUDENT_A = 101


# ---------------------------------------------------------------------------
# Fixture helpers
# ---------------------------------------------------------------------------

def _cache_key(path, params):
    payload = json.dumps({'path': path, 'params': sorted((params or {}).items())})
    return hashlib.sha256(payload.encode()).hexdigest()


def _seed_cache(path, params, response_json):
    db.session.add(CanvasCache(
        cache_key=_cache_key(path, params),
        response_json=response_json,
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=0,
    ))
    db.session.commit()


def _seed_module(canvas_id, name, position, start, end, course_id=COURSE_ID):
    m = CourseModule(
        course_id=course_id,
        canvas_module_id=canvas_id,
        name=name,
        position=position,
        start_date=start,
        end_date=end,
    )
    db.session.add(m)
    db.session.commit()
    return m


def _seed_event(event_type, source_id, occurred_at,
                student_id=STUDENT_A, course_id=COURSE_ID):
    db.session.add(InteractionEvent(
        course_id=course_id,
        student_canvas_id=student_id,
        event_type=event_type,
        occurred_at=occurred_at,
        source_id=source_id,
    ))
    db.session.commit()


def _seed_assignment_groups(groups):
    """groups: list of {id, name, position}."""
    _seed_cache(f'/api/v1/courses/{COURSE_ID}/assignment_groups', None, groups)


def _seed_assignments(assignments):
    """assignments: list of {id, name, due_at, assignment_group_id}."""
    _seed_cache(
        f'/api/v1/courses/{COURSE_ID}/assignments',
        {'order_by': 'due_at'},
        assignments,
    )


def _seed_submissions(assignment_id, submissions):
    """submissions: list of {id, assignment_id, late, ...}."""
    _seed_cache(
        f'/api/v1/courses/{COURSE_ID}/assignments/{assignment_id}/submissions',
        None,
        submissions,
    )


def _seed_conversations(conversations, scope='sent'):
    _seed_cache('/api/v1/conversations', {'scope': scope}, conversations)


def _mock_client_with_name(name='Alice Smith'):
    mock = MagicMock()
    mock.get_course.return_value = {
        'id': COURSE_ID, 'name': 'Test Course', 'course_code': 'CS101',
    }
    mock.get_enrollments.return_value = [
        {'user_id': STUDENT_A, 'user': {'name': name, 'sortable_name': name}},
    ]
    mock.get_discussion_entries.return_value = []
    return mock


# ---------------------------------------------------------------------------
# Helper unit tests
# ---------------------------------------------------------------------------

def test_empty_course_returns_no_modules(app):
    """A course with no CourseModule rows returns has_modules=False."""
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    assert view['has_modules'] is False
    assert view['rows'] == []


def test_module_with_no_events_renders_empty_row(app):
    """A module with zero matching events still appears in rows; cells are empty."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([
        {'id': 10, 'name': 'Readings', 'position': 1},
    ])
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    assert view['has_modules'] is True
    assert len(view['rows']) == 1
    # 1 messages + 1 group = 2 cells, all empty
    cells = view['rows'][0]['cells']
    assert len(cells) == 2
    assert all(c['events'] == [] for c in cells)


def test_columns_messages_then_groups_in_position_order(app):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([
        {'id': 20, 'name': 'Projects', 'position': 2},
        {'id': 10, 'name': 'Readings', 'position': 1},
    ])
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    keys = [c['key'] for c in view['columns']]
    labels = [c['label'] for c in view['columns']]
    assert keys == ['messages', '10', '20']
    assert labels == ['Messages', 'Readings', 'Projects']


def test_modules_sorted_by_position_descending(app):
    _seed_module(1, 'Module 0', 0, date(2026, 1, 1), date(2026, 1, 14))
    _seed_module(2, 'Module 5', 5, date(2026, 4, 1), date(2026, 4, 14))
    _seed_module(3, 'Module 2', 2, date(2026, 2, 1), date(2026, 2, 14))
    _seed_assignment_groups([])
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    positions = [r['module']['position'] for r in view['rows']]
    assert positions == [5, 2, 0]


def test_message_attribution_by_module_date_range(app):
    """A message that falls in module 1's date range goes into module 1's cell only."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_module(2, 'Module 2', 2, date(2026, 4, 15), date(2026, 4, 28))
    _seed_assignment_groups([])
    # Inside module 1
    _seed_event('conversation', 5001,
                datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))
    # Inside module 2
    _seed_event('student_message', 5002,
                datetime(2026, 4, 20, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)

    # Rows are position DESC → module 2 first, module 1 second
    m2_msg_cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == 'messages')
    m1_msg_cell = next(c for c in view['rows'][1]['cells'] if c['column']['key'] == 'messages')
    assert [e['source_id'] for e in m2_msg_cell['events']] == [5002]
    assert [e['source_id'] for e in m1_msg_cell['events']] == [5001]


def test_late_submission_gets_late_class(app):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([
        {'id': 10, 'name': 'Readings', 'position': 1},
    ])
    _seed_assignments([
        {'id': 500, 'name': 'Reading 1', 'due_at': '2026-04-10T23:59:00Z',
         'assignment_group_id': 10},
    ])
    _seed_submissions(500, [
        {'id': 9001, 'assignment_id': 500, 'late': True},
    ])
    _seed_event('submission', 9001,
                datetime(2026, 4, 11, 1, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == '10')
    assert len(cell['events']) == 1
    assert 'late' in cell['events'][0]['icon_class']
    assert cell['events'][0]['late'] is True


def test_on_time_submission_no_late_class(app):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 10, 'name': 'Readings', 'position': 1}])
    _seed_assignments([
        {'id': 500, 'name': 'R1', 'due_at': '2026-04-10T23:59:00Z',
         'assignment_group_id': 10},
    ])
    _seed_submissions(500, [{'id': 9001, 'assignment_id': 500, 'late': False}])
    _seed_event('submission', 9001,
                datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == '10')
    assert cell['events'][0]['icon_class'] == 'icon-sub'
    assert cell['events'][0]['late'] is False


def test_mixed_event_cell_orders_newest_first(app):
    """Messages cell with multiple types stacks newest on the front (index 0)."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([])
    _seed_event('conversation', 1,
                datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc))
    _seed_event('student_message', 2,
                datetime(2026, 4, 6, 9, 0, tzinfo=timezone.utc))
    _seed_event('group_conversation', 3,
                datetime(2026, 4, 4, 9, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == 'messages')
    assert [e['source_id'] for e in cell['events']] == [2, 1, 3]
    # Front (newest) icon class corresponds to student message
    assert cell['events'][0]['icon_class'] == 'icon-msg student'


def test_overflow_indicator_for_4_plus_events(app):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([])
    base = datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc)
    for i in range(5):
        _seed_event('conversation', 1000 + i, base + timedelta(hours=i))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == 'messages')
    assert len(cell['events']) == 5
    assert cell['overflow'] == 2


def test_submission_outside_module_date_range_is_dropped(app):
    """If due_at is outside every module range, the submission appears nowhere."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 10, 'name': 'Readings', 'position': 1}])
    _seed_assignments([
        {'id': 500, 'name': 'R1', 'due_at': '2026-05-10T23:59:00Z',
         'assignment_group_id': 10},
    ])
    _seed_submissions(500, [{'id': 9001, 'assignment_id': 500, 'late': False}])
    _seed_event('submission', 9001,
                datetime(2026, 5, 9, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == '10')
    assert cell['events'] == []


def test_missing_caches_render_gracefully(app):
    """Modules + events but no assignment_groups cache → placeholder column, no crash."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_event('submission', 9001,
                datetime(2026, 4, 5, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    keys = [c['key'] for c in view['columns']]
    assert keys == ['messages', 'placeholder']
    # Submission has no group → not in any cell
    for cell in view['rows'][0]['cells']:
        assert cell['events'] == []


def test_future_modules_are_hidden(app):
    """Modules whose start_date is after today should not appear."""
    today = datetime.now(timezone.utc).date()
    _seed_module(1, 'Past Module', 1, today - timedelta(days=14), today - timedelta(days=1))
    _seed_module(2, 'Future Module', 2, today + timedelta(days=1), today + timedelta(days=14))
    _seed_assignment_groups([])
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    names = [r['module']['name'] for r in view['rows']]
    assert names == ['Past Module']


def test_module_starting_today_is_visible(app):
    today = datetime.now(timezone.utc).date()
    _seed_module(1, 'Today Module', 1, today, today + timedelta(days=14))
    _seed_assignment_groups([])
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    assert [r['module']['name'] for r in view['rows']] == ['Today Module']


def test_graded_discussion_appears_in_group_column(app):
    """A discussion_entry on a graded discussion shows in its assignment group's column."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 30, 'name': 'Discussions', 'position': 1}])
    # Graded discussion = assignment with a discussion_topic field
    _seed_assignments([
        {'id': 700, 'name': 'D1', 'due_at': '2026-04-10T23:59:00Z',
         'assignment_group_id': 30,
         'discussion_topic': {'id': 7777}},
    ])
    # Topic entries cache: a top-level entry by the student
    _seed_cache(
        f'/api/v1/courses/{COURSE_ID}/discussion_topics/7777/entries',
        None,
        [{'id': 33001, 'message': 'My post', 'recent_replies': []}],
    )
    _seed_event('discussion_entry', 33001,
                datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == '30')
    assert len(cell['events']) == 1
    assert cell['events'][0]['icon_class'] == 'icon-sub'
    assert cell['events'][0]['event_type'] == 'graded_discussion'


def test_graded_discussion_reply_also_attributed(app):
    """A discussion_reply nested under a graded topic also lands in the group column."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 30, 'name': 'Discussions', 'position': 1}])
    _seed_assignments([
        {'id': 700, 'name': 'D1', 'due_at': '2026-04-10T23:59:00Z',
         'assignment_group_id': 30,
         'discussion_topic': {'id': 7777}},
    ])
    _seed_cache(
        f'/api/v1/courses/{COURSE_ID}/discussion_topics/7777/entries',
        None,
        [{'id': 33001, 'message': 'Top', 'recent_replies': [
            {'id': 44001, 'message': 'My reply'},
        ]}],
    )
    _seed_event('discussion_reply', 44001,
                datetime(2026, 4, 9, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    cell = next(c for c in view['rows'][0]['cells'] if c['column']['key'] == '30')
    assert len(cell['events']) == 1


def test_ungraded_discussion_does_not_appear_in_table(app):
    """A discussion_entry whose topic is not tied to a graded assignment is dropped."""
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 30, 'name': 'Discussions', 'position': 1}])
    # No discussion-type assignments seeded → no mapping
    _seed_event('discussion_entry', 33001,
                datetime(2026, 4, 8, 12, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    for cell in view['rows'][0]['cells']:
        assert cell['events'] == []


def test_drawer_payload_only_for_non_empty_cells(app):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 10, 'name': 'Readings', 'position': 1}])
    _seed_event('conversation', 5001,
                datetime(2026, 4, 5, 9, 0, tzinfo=timezone.utc))
    with app.app_context():
        view = build_by_module_view(COURSE_ID, STUDENT_A, timezone.utc)
    # One drawer entry (messages cell), nothing for the empty Readings cell
    assert len(view['drawer_payload']) == 1
    payload = next(iter(view['drawer_payload'].values()))
    assert 'Module 1' in payload['header']
    assert 'Messages' in payload['header']


# ---------------------------------------------------------------------------
# Rendered page tests (integration)
# ---------------------------------------------------------------------------

def test_student_page_shows_by_module_section(client):
    _seed_module(1, 'Module 1', 1, date(2026, 4, 1), date(2026, 4, 14))
    _seed_assignment_groups([{'id': 10, 'name': 'Readings', 'position': 1}])
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    html = response.data.decode()
    assert response.status_code == 200
    assert '>By Module<' in html
    assert 'Readings' in html


def test_student_page_renames_last_21_days(client):
    """Last 21 Days header should be replaced with Recent Activity."""
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    html = response.data.decode()
    assert 'Recent Activity' in html
    assert 'Last 21 Days' not in html


def test_student_page_shows_module_placeholder_when_no_modules(client):
    """No CourseModule rows → documented placeholder text."""
    with patch('app.routes.dashboard.CanvasClient', return_value=_mock_client_with_name()):
        response = client.get(f'/course/{COURSE_ID}/student/{STUDENT_A}')
    assert b'No modules synced for this course yet.' in response.data
