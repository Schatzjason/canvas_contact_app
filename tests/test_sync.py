"""Tests for sync_course: verifies that the correct InteractionEvent rows are
created from Canvas API data, that the upsert is idempotent, and that edge
cases (no enrollments, old events, non-enrolled participants) are handled.

CanvasClient is fully mocked — no DB cache reads/writes happen via the client.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.interaction_event import InteractionEvent
from app.services.sync import sync_course

COURSE_ID = 99
STUDENT_A = 101
STUDENT_B = 102


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


def _make_client(enrollments=None, conversations=None, inbox=None,
                 topics=None, entries_by_topic=None):
    """Return a mock CanvasClient that yields controlled fixture data.

    conversations: sent (instructor) conversations (scope='sent')
    inbox:         received conversations (scope='inbox') for student messages
    """
    mock = MagicMock()
    mock.get_enrollments.return_value = enrollments or []

    sent_convs  = conversations or []
    inbox_convs = inbox or []

    def _stream(since=None, scope='sent'):
        convs = sent_convs if scope == 'sent' else inbox_convs
        return iter([(convs, False)] if convs else [])

    mock.stream_conversations.side_effect = _stream

    mock.get_discussion_topics.return_value = topics or []

    by_topic = entries_by_topic or {}
    mock.get_discussion_entries.side_effect = (
        lambda course_id, topic_id: by_topic.get(topic_id, [])
    )
    return mock


def _run(course_id=COURSE_ID):
    """Consume sync_course to completion and return all yielded messages."""
    return list(sync_course(course_id))


# ---------------------------------------------------------------------------
# Conversations
# ---------------------------------------------------------------------------

def test_sync_creates_conversation_event():
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        conversations=[{
            'id': 1001,
            'last_authored_at': _days_ago(2),
            'participants': [{'id': STUDENT_A}],
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 1
    assert events[0].event_type == 'conversation'
    assert events[0].student_canvas_id == STUDENT_A
    assert events[0].source_id == 1001


def test_sync_ignores_non_enrolled_participants():
    """Conversation participant not in enrollments → no event created."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        conversations=[{
            'id': 1001,
            'last_authored_at': _days_ago(2),
            'participants': [{'id': 999}],  # 999 is not enrolled
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


def test_sync_creates_one_event_per_enrolled_participant():
    """A conversation involving 2 enrolled students → 2 events."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}, {'user_id': STUDENT_B}],
        conversations=[{
            'id': 1001,
            'last_authored_at': _days_ago(2),
            'participants': [{'id': STUDENT_A}, {'id': STUDENT_B}],
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 2


def test_sync_skips_conversation_without_timestamp():
    """Conversation with no last_authored_at or last_message_at is skipped."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        conversations=[{
            'id': 1001,
            'last_authored_at': None,
            'last_message_at': None,
            'participants': [{'id': STUDENT_A}],
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


# ---------------------------------------------------------------------------
# Discussion entries and replies
# ---------------------------------------------------------------------------

def test_sync_creates_discussion_entry_event():
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [
            {'id': 301, 'user_id': STUDENT_A, 'created_at': _days_ago(3), 'recent_replies': []},
        ]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 1
    assert events[0].event_type == 'discussion_entry'
    assert events[0].source_id == 301


def test_sync_creates_discussion_reply_event():
    """A reply by an enrolled student creates a discussion_reply event."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_B}],
        topics=[{'id': 201}],
        entries_by_topic={201: [{
            'id': 301,
            'user_id': 999,  # non-enrolled author; entry itself is skipped
            'created_at': _days_ago(5),
            'recent_replies': [
                {'id': 401, 'user_id': STUDENT_B, 'created_at': _days_ago(2)},
            ],
        }]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 1
    assert events[0].event_type == 'discussion_reply'
    assert events[0].source_id == 401


def test_sync_excludes_old_discussion_entries():
    """Discussion entries older than 21 days are not created."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [
            {'id': 301, 'user_id': STUDENT_A, 'created_at': _days_ago(25), 'recent_replies': []},
        ]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    done = next(m for m in msgs if m['status'] == 'done')
    assert done['count'] == 0
    assert InteractionEvent.query.count() == 0


def test_sync_excludes_old_discussion_replies():
    """Discussion replies older than 21 days are not created."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [{
            'id': 301,
            'user_id': 999,
            'created_at': _days_ago(5),
            'recent_replies': [
                {'id': 401, 'user_id': STUDENT_A, 'created_at': _days_ago(25)},
            ],
        }]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


def test_sync_ignores_discussion_entries_by_non_students():
    """Entry authored by someone not enrolled is skipped."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [
            {'id': 301, 'user_id': 999, 'created_at': _days_ago(3), 'recent_replies': []},
        ]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


# ---------------------------------------------------------------------------
# Idempotency
# ---------------------------------------------------------------------------

def test_sync_upsert_is_idempotent():
    """Running sync twice with identical data produces the same row count."""
    enrollments = [{'user_id': STUDENT_A}]
    conversations = [{'id': 1001, 'last_authored_at': _days_ago(2), 'participants': [{'id': STUDENT_A}]}]

    client1 = _make_client(enrollments=enrollments, conversations=conversations)
    with patch('app.services.sync.CanvasClient', return_value=client1):
        _run()
    first_count = InteractionEvent.query.count()

    client2 = _make_client(enrollments=enrollments, conversations=conversations)
    with patch('app.services.sync.CanvasClient', return_value=client2):
        _run()

    assert InteractionEvent.query.count() == first_count


# ---------------------------------------------------------------------------
# Edge cases and output format
# ---------------------------------------------------------------------------

def test_sync_no_enrollments_returns_zero():
    client = _make_client(enrollments=[])
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    done = next(m for m in msgs if m['status'] == 'done')
    assert done['count'] == 0
    assert InteractionEvent.query.count() == 0


def test_sync_final_count_matches_events_created():
    enrollments = [{'user_id': STUDENT_A}, {'user_id': STUDENT_B}]
    conversations = [
        {'id': 1001, 'last_authored_at': _days_ago(1), 'participants': [{'id': STUDENT_A}]},
        {'id': 1002, 'last_authored_at': _days_ago(2), 'participants': [{'id': STUDENT_B}]},
    ]
    client = _make_client(enrollments=enrollments, conversations=conversations)
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    done = next(m for m in msgs if m['status'] == 'done')
    assert done['count'] == 2
    assert InteractionEvent.query.count() == 2


# ---------------------------------------------------------------------------
# Student messages (inbox)
# ---------------------------------------------------------------------------

def test_sync_creates_student_message_event():
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        inbox=[{
            'id': 2001,
            'last_message_at': _days_ago(2),
            'participants': [{'id': STUDENT_A}],
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 1
    assert events[0].event_type == 'student_message'
    assert events[0].student_canvas_id == STUDENT_A
    assert events[0].source_id == 2001


def test_sync_student_message_ignores_non_enrolled():
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        inbox=[{
            'id': 2001,
            'last_message_at': _days_ago(2),
            'participants': [{'id': 999}],  # not enrolled
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


def test_sync_student_message_skips_missing_timestamp():
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        inbox=[{
            'id': 2001,
            'last_message_at': None,
            'participants': [{'id': STUDENT_A}],
        }],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


def test_sync_sent_and_inbox_same_conversation_creates_two_event_types():
    """Same conversation ID can produce both 'conversation' and 'student_message' rows."""
    enrollments = [{'user_id': STUDENT_A}]
    client = _make_client(
        enrollments=enrollments,
        conversations=[{'id': 1001, 'last_authored_at': _days_ago(2), 'participants': [{'id': STUDENT_A}]}],
        inbox=[{'id': 1001, 'last_message_at': _days_ago(1), 'participants': [{'id': STUDENT_A}]}],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 2
    types = {e.event_type for e in events}
    assert types == {'conversation', 'student_message'}
