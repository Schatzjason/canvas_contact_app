"""Tests for sync_course: verifies that the correct InteractionEvent rows are
created from Canvas API data, that the upsert is idempotent, and that edge
cases (no enrollments, old events, non-enrolled participants) are handled.

CanvasClient is fully mocked — no DB cache reads/writes happen via the client.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.interaction_event import InteractionEvent
from app.services.sync import sync_course

COURSE_ID = 99
STUDENT_A = 101
STUDENT_B = 102


def _days_ago(n):
    return (datetime.now(timezone.utc) - timedelta(days=n)).isoformat()


INSTRUCTOR_ID = 500


def _make_client(enrollments=None, conversations=None, inbox=None,
                 topics=None, entries_by_topic=None, instructor_id=INSTRUCTOR_ID):
    """Return a mock CanvasClient that yields controlled fixture data.

    conversations: sent (instructor) conversations (scope='sent')
    inbox:         received conversations (scope='inbox') for student messages
    instructor_id: Canvas user ID returned by get_current_user
    """
    mock = MagicMock()
    mock.get_enrollments.return_value = enrollments or []
    mock.get_current_user.return_value = {'id': instructor_id}

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


# ---------------------------------------------------------------------------
# Instructor discussion replies
# ---------------------------------------------------------------------------

def test_sync_creates_instructor_disc_reply_event():
    """Instructor reply to a student entry → discussion_instructor_reply on that student's row."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [{
            'id': 301,
            'user_id': STUDENT_A,
            'created_at': _days_ago(5),
            'recent_replies': [
                {'id': 401, 'user_id': INSTRUCTOR_ID, 'created_at': _days_ago(2)},
            ],
        }]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    events = InteractionEvent.query.all()
    assert len(events) == 2  # discussion_entry + discussion_instructor_reply
    types = {e.event_type for e in events}
    assert 'discussion_instructor_reply' in types
    reply_event = next(e for e in events if e.event_type == 'discussion_instructor_reply')
    assert reply_event.student_canvas_id == STUDENT_A
    assert reply_event.source_id == 401


def test_sync_instructor_reply_ignored_for_non_student_entry():
    """Instructor replying to their own (or non-enrolled) post → no event."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [{
            'id': 301,
            'user_id': 999,  # non-enrolled entry author
            'created_at': _days_ago(5),
            'recent_replies': [
                {'id': 401, 'user_id': INSTRUCTOR_ID, 'created_at': _days_ago(2)},
            ],
        }]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    types = {e.event_type for e in InteractionEvent.query.all()}
    assert 'discussion_instructor_reply' not in types


def test_sync_instructor_reply_outside_cutoff_ignored():
    """Instructor reply older than 21 days is not recorded."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201}],
        entries_by_topic={201: [{
            'id': 301,
            'user_id': STUDENT_A,
            'created_at': _days_ago(25),
            'recent_replies': [
                {'id': 401, 'user_id': INSTRUCTOR_ID, 'created_at': _days_ago(25)},
            ],
        }]},
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    assert InteractionEvent.query.count() == 0


# ---------------------------------------------------------------------------
# Sync markers (incremental fetch)
# ---------------------------------------------------------------------------

def test_sync_marker_written_after_live_fetch():
    """A live conversation fetch should write a sync_marker row to canvas_cache."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        conversations=[{'id': 1001, 'last_authored_at': _days_ago(2), 'participants': [{'id': STUDENT_A}]}],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        _run()

    marker = CanvasCache.query.filter_by(
        cache_key=f'sync_marker:conv_sent:{COURSE_ID}'
    ).first()
    assert marker is not None


def test_sync_uses_marker_as_since():
    """When a sync marker exists, stream_conversations receives it as `since`
    rather than the 21-day fallback cutoff."""
    marker_time = datetime.now(timezone.utc) - timedelta(hours=6)
    db.session.add(CanvasCache(
        cache_key=f'sync_marker:conv_sent:{COURSE_ID}',
        response_json=[],
        fetched_at=marker_time,
        ttl_seconds=10 * 365 * 24 * 3600,
    ))
    db.session.commit()

    mock = MagicMock()
    mock.get_enrollments.return_value = [{'user_id': STUDENT_A}]
    mock.get_current_user.return_value = {'id': INSTRUCTOR_ID}
    mock.stream_conversations.side_effect = lambda since=None, scope='sent': iter([])
    mock.get_discussion_topics.return_value = []

    with patch('app.services.sync.CanvasClient', return_value=mock):
        _run()

    sent_call = mock.stream_conversations.call_args_list[0]
    since_used = sent_call.kwargs['since']
    # Marker was 6 hours ago; 21-day cutoff would be ~21 days ago.
    # Verify the marker (not the cutoff) was used.
    assert (datetime.now(timezone.utc) - since_used).total_seconds() < 24 * 3600


def test_sync_falls_back_to_cutoff_when_no_marker():
    """Without a sync marker, stream_conversations receives the 21-day cutoff."""
    mock = MagicMock()
    mock.get_enrollments.return_value = [{'user_id': STUDENT_A}]
    mock.get_current_user.return_value = {'id': INSTRUCTOR_ID}
    mock.stream_conversations.side_effect = lambda since=None, scope='sent': iter([])
    mock.get_discussion_topics.return_value = []

    with patch('app.services.sync.CanvasClient', return_value=mock):
        _run()

    sent_call = mock.stream_conversations.call_args_list[0]
    since_used = sent_call.kwargs['since']
    # Cutoff is midnight(today) - 21 days, so age is between 21 and 22 days
    # depending on the time of day the test runs.
    age_days = (datetime.now(timezone.utc) - since_used).total_seconds() / 86400
    assert 21.0 <= age_days < 22.0


# ---------------------------------------------------------------------------
# New progress message shapes
# ---------------------------------------------------------------------------

def test_sync_yields_done_phase_for_all_five_phases():
    """sync_course must emit a done_phase for each of the 5 named phases."""
    client = _make_client(enrollments=[{'user_id': STUDENT_A}])
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    done_phases = {m['phase'] for m in msgs if m['status'] == 'done_phase'}
    assert done_phases == {'enrollments', 'conversations', 'student_messages', 'discussions', 'saving'}


def test_sync_done_message_has_students_count():
    """The final done message must carry a 'students' key with unique student count."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}, {'user_id': STUDENT_B}],
        conversations=[
            {'id': 1001, 'last_authored_at': _days_ago(2), 'participants': [{'id': STUDENT_A}]},
            {'id': 1002, 'last_authored_at': _days_ago(3), 'participants': [{'id': STUDENT_B}]},
        ],
    )
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    done = next(m for m in msgs if m['status'] == 'done')
    assert 'students' in done
    assert done['students'] == 2


# ---------------------------------------------------------------------------
# Per-phase error isolation
# ---------------------------------------------------------------------------

def test_sync_conversation_error_does_not_abort_discussions():
    """A failure in the conversations phase must not prevent discussions from running."""
    client = _make_client(
        enrollments=[{'user_id': STUDENT_A}],
        topics=[{'id': 201, 'title': 'Week 1'}],
        entries_by_topic={201: [
            {'id': 301, 'user_id': STUDENT_A, 'created_at': _days_ago(3), 'recent_replies': []},
        ]},
    )

    def raise_on_sent(since=None, scope='sent'):
        if scope == 'sent':
            raise RuntimeError('network blip')
        return iter([])

    client.stream_conversations.side_effect = raise_on_sent

    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    phases_with_errors = {m['phase'] for m in msgs if m['status'] == 'error'}
    assert 'conversations' in phases_with_errors
    # Discussions still ran — the entry must be in the DB
    assert InteractionEvent.query.count() == 1


def test_sync_enrollment_error_aborts():
    """An enrollment failure must stop the generator — no subsequent phases run."""
    mock = MagicMock()
    mock.get_enrollments.side_effect = RuntimeError('Canvas down')
    mock.get_current_user.return_value = {'id': INSTRUCTOR_ID}

    with patch('app.services.sync.CanvasClient', return_value=mock):
        msgs = _run()

    assert any(m['status'] == 'error' for m in msgs)
    # Generator returned early; no done_phase or done messages should appear
    assert not any(m['status'] == 'done_phase' for m in msgs)
    assert not any(m['status'] == 'done' for m in msgs)


# ---------------------------------------------------------------------------
# Enrollment cache detection
# ---------------------------------------------------------------------------

def test_sync_enrollment_cached_emits_cached_message():
    """When the enrollment cache is warm, sync emits status='cached' for enrollments."""
    from app.services.sync import _enrollment_cache_key

    db.session.add(CanvasCache(
        cache_key=_enrollment_cache_key(COURSE_ID),
        response_json=[{'user_id': STUDENT_A}],
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=3600,
    ))
    db.session.commit()

    client = _make_client(enrollments=[{'user_id': STUDENT_A}])
    with patch('app.services.sync.CanvasClient', return_value=client):
        msgs = _run()

    assert any(m['status'] == 'cached' and m['phase'] == 'enrollments' for m in msgs)
