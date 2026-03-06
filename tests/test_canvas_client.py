"""Tests for CanvasClient: cache key generation, TTL freshness, link-header
pagination, and the _get / _get_all_pages plumbing.

All HTTP calls are mocked — no real Canvas API is touched.
"""
from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch

from app import db
from app.models.canvas_cache import CanvasCache
from app.services.canvas_client import CanvasClient


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(data, link_header=None):
    resp = MagicMock()
    resp.json.return_value = data
    resp.raise_for_status.return_value = None
    resp.headers.get.return_value = link_header
    return resp


# ---------------------------------------------------------------------------
# Cache key
# ---------------------------------------------------------------------------

def test_make_cache_key_is_deterministic():
    k1 = CanvasClient._make_cache_key('/api/v1/test', {'a': 1, 'b': 2})
    k2 = CanvasClient._make_cache_key('/api/v1/test', {'a': 1, 'b': 2})
    assert k1 == k2


def test_make_cache_key_varies_by_path():
    k1 = CanvasClient._make_cache_key('/api/v1/foo', {})
    k2 = CanvasClient._make_cache_key('/api/v1/bar', {})
    assert k1 != k2


def test_make_cache_key_param_order_independent():
    k1 = CanvasClient._make_cache_key('/api/v1/test', {'a': 1, 'b': 2})
    k2 = CanvasClient._make_cache_key('/api/v1/test', {'b': 2, 'a': 1})
    assert k1 == k2


def test_make_cache_key_none_params_same_as_empty():
    k1 = CanvasClient._make_cache_key('/api/v1/test', None)
    k2 = CanvasClient._make_cache_key('/api/v1/test', {})
    # Both sort to the same empty list — they should produce the same key
    # (implementation detail: both end up with sorted([]) == [])
    assert k1 == k2


# ---------------------------------------------------------------------------
# Link-header parsing
# ---------------------------------------------------------------------------

def test_parse_next_url_extracts_url():
    header = (
        '<https://canvas.example.com/api/v1/items?page=2&per_page=100>; rel="next", '
        '<https://canvas.example.com/api/v1/items?page=5>; rel="last"'
    )
    assert CanvasClient._parse_next_url(header) == (
        'https://canvas.example.com/api/v1/items?page=2&per_page=100'
    )


def test_parse_next_url_returns_none_when_no_next():
    header = '<https://canvas.example.com/api/v1/items?page=5>; rel="last"'
    assert CanvasClient._parse_next_url(header) is None


def test_parse_next_url_returns_none_for_none_input():
    assert CanvasClient._parse_next_url(None) is None


# ---------------------------------------------------------------------------
# _get: caching behaviour
# ---------------------------------------------------------------------------

def test_get_cache_miss_fetches_and_writes_cache():
    with patch('requests.get', return_value=_mock_response([{'id': 1}])) as mock_get:
        client = CanvasClient()
        result = client._get('/api/v1/test', ttl=300)

    assert result == [{'id': 1}]
    mock_get.assert_called_once()
    entry = CanvasCache.query.first()
    assert entry is not None
    assert entry.response_json == [{'id': 1}]


def test_get_cache_hit_skips_http():
    cache_key = CanvasClient._make_cache_key('/api/v1/test', None)
    db.session.add(CanvasCache(
        cache_key=cache_key,
        response_json=[{'id': 'cached'}],
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=3600,
    ))
    db.session.commit()

    with patch('requests.get') as mock_get:
        client = CanvasClient()
        result = client._get('/api/v1/test', ttl=3600)

    assert result == [{'id': 'cached'}]
    mock_get.assert_not_called()


def test_get_stale_cache_refetches():
    cache_key = CanvasClient._make_cache_key('/api/v1/test', None)
    db.session.add(CanvasCache(
        cache_key=cache_key,
        response_json=[{'id': 'stale'}],
        fetched_at=datetime.now(timezone.utc) - timedelta(seconds=7200),
        ttl_seconds=3600,  # age 7200 > ttl 3600 → stale
    ))
    db.session.commit()

    with patch('requests.get', return_value=_mock_response([{'id': 'fresh'}])) as mock_get:
        client = CanvasClient()
        result = client._get('/api/v1/test', ttl=3600)

    assert result == [{'id': 'fresh'}]
    mock_get.assert_called_once()


def test_get_without_ttl_never_touches_cache():
    with patch('requests.get', return_value=_mock_response([{'id': 1}])) as mock_get:
        client = CanvasClient()
        result = client._get('/api/v1/test')  # no ttl kwarg

    assert result == [{'id': 1}]
    mock_get.assert_called_once()
    assert CanvasCache.query.count() == 0


# ---------------------------------------------------------------------------
# _get_all_pages: pagination
# ---------------------------------------------------------------------------

def test_get_all_pages_follows_link_header():
    resp1 = _mock_response(
        [{'id': 1}],
        link_header='<https://canvas.test/api/v1/items?page=2>; rel="next"',
    )
    resp2 = _mock_response([{'id': 2}])

    with patch('requests.get', side_effect=[resp1, resp2]) as mock_get:
        client = CanvasClient()
        result = client._get_all_pages('/api/v1/items')

    assert result == [{'id': 1}, {'id': 2}]
    assert mock_get.call_count == 2


def test_get_all_pages_single_page():
    with patch('requests.get', return_value=_mock_response([{'id': 1}, {'id': 2}])):
        client = CanvasClient()
        result = client._get_all_pages('/api/v1/items')

    assert result == [{'id': 1}, {'id': 2}]


def test_stream_conversations_cache_hit_ignores_since():
    """stream_conversations uses a scope-only cache key; passing a different
    `since` date must still hit the same cache entry, not trigger a new fetch."""
    cache_key = CanvasClient._make_cache_key('/api/v1/conversations', {'scope': 'sent'})
    db.session.add(CanvasCache(
        cache_key=cache_key,
        response_json=[{'id': 42}],
        fetched_at=datetime.now(timezone.utc),
        ttl_seconds=3600,
    ))
    db.session.commit()

    with patch('requests.get') as mock_get:
        client = CanvasClient()
        results = list(client.stream_conversations(
            since=datetime.now(timezone.utc) - timedelta(days=3),
            scope='sent',
        ))

    mock_get.assert_not_called()
    assert results == [([{'id': 42}], True)]


def test_enrollment_cache_key_matches_canvas_client():
    """_enrollment_cache_key in sync.py must produce the same digest as
    CanvasClient._make_cache_key with the same path and params — they must
    stay in sync or enrollment cache detection silently breaks."""
    from app.services.sync import _enrollment_cache_key

    cid = 99
    client_key = CanvasClient._make_cache_key(
        f'/api/v1/courses/{cid}/enrollments',
        {'type[]': 'StudentEnrollment', 'state[]': 'active'},
    )
    sync_key = _enrollment_cache_key(cid)
    assert sync_key == client_key


def test_get_all_pages_caches_combined_result():
    resp1 = _mock_response(
        [{'id': 1}],
        link_header='<https://canvas.test/api/v1/items?page=2>; rel="next"',
    )
    resp2 = _mock_response([{'id': 2}])

    with patch('requests.get', side_effect=[resp1, resp2]):
        client = CanvasClient()
        client._get_all_pages('/api/v1/items', ttl=300)

    entry = CanvasCache.query.first()
    assert entry is not None
    assert entry.response_json == [{'id': 1}, {'id': 2}]
