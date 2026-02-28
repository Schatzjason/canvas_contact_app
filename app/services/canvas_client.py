import hashlib
import json
import re
from datetime import datetime, timezone

import requests
from flask import current_app

from app import db
from app.models.canvas_cache import CanvasCache

# Cache TTLs (seconds)
TTL_CONVERSATIONS = 15 * 60
TTL_DISCUSSION_ENTRIES = 15 * 60
TTL_ENROLLMENTS = 60 * 60


class CanvasClient:
    # Accepts an explicit token so phase 2 (OAuth) is a one-line swap:
    # pass the User's stored access_token instead of reading from config.
    def __init__(self, token=None):
        self.base_url = current_app.config['CANVAS_BASE_URL'].rstrip('/')
        self._token = token or current_app.config['CANVAS_API_TOKEN']

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self._token}'}

    @staticmethod
    def _make_cache_key(path, params):
        payload = json.dumps({'path': path, 'params': sorted((params or {}).items())})
        return hashlib.sha256(payload.encode()).hexdigest()

    def _cache_read(self, cache_key):
        try:
            entry = CanvasCache.query.filter_by(cache_key=cache_key).first()
            if entry and entry.is_fresh():
                return entry.response_json
        except Exception as exc:
            current_app.logger.warning('Cache read failed: %s', exc)
        return None

    def _cache_write(self, cache_key, data, ttl):
        try:
            entry = CanvasCache.query.filter_by(cache_key=cache_key).first()
            if entry:
                entry.response_json = data
                entry.fetched_at = datetime.now(timezone.utc)
                entry.ttl_seconds = ttl
            else:
                db.session.add(CanvasCache(
                    cache_key=cache_key,
                    response_json=data,
                    fetched_at=datetime.now(timezone.utc),
                    ttl_seconds=ttl,
                ))
            db.session.commit()
        except Exception as exc:
            current_app.logger.warning('Cache write failed: %s', exc)
            db.session.rollback()

    @staticmethod
    def _parse_next_url(link_header):
        """Extract the rel="next" URL from a Canvas Link header."""
        for part in (link_header or '').split(','):
            if 'rel="next"' in part:
                m = re.search(r'<([^>]+)>', part)
                if m:
                    return m.group(1)
        return None

    def _get(self, path, params=None, ttl=None):
        """Single-page GET, with optional DB caching."""
        if ttl is not None:
            cache_key = self._make_cache_key(path, params)
            cached = self._cache_read(cache_key)
            if cached is not None:
                return cached

        resp = requests.get(
            f'{self.base_url}{path}',
            headers=self._auth_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if ttl is not None:
            self._cache_write(cache_key, data, ttl)

        return data

    def _get_all_pages(self, path, params=None, ttl=None):
        """Paginated GET — follows Link: rel="next" headers. Combined result optionally cached."""
        if ttl is not None:
            cache_key = self._make_cache_key(path, params)
            cached = self._cache_read(cache_key)
            if cached is not None:
                return cached

        results = []
        next_url = f'{self.base_url}{path}'
        next_params = {'per_page': 100, **(params or {})}

        while next_url:
            resp = requests.get(
                next_url,
                headers=self._auth_headers(),
                params=next_params,
                timeout=10,
            )
            resp.raise_for_status()
            results.extend(resp.json())
            next_url = self._parse_next_url(resp.headers.get('Link'))
            next_params = {}  # full URL from Link header carries params for subsequent pages

        if ttl is not None:
            self._cache_write(cache_key, results, ttl)

        return results

    # ------------------------------------------------------------------ #
    # Public API methods                                                   #
    # ------------------------------------------------------------------ #

    def get_courses(self):
        """Return active teacher courses — always fetched live, never cached."""
        return self._get('/api/v1/courses', params={
            'enrollment_type': 'teacher',
            'enrollment_state': 'active',
            'state[]': 'available',
            'per_page': 50,
        })

    def get_enrollments(self, course_id):
        """Active student enrollments for a course (cached 60 min)."""
        return self._get_all_pages(
            f'/api/v1/courses/{course_id}/enrollments',
            params={'type[]': 'StudentEnrollment', 'state[]': 'active'},
            ttl=TTL_ENROLLMENTS,
        )

    def get_conversations(self):
        """All sent conversations (cached 15 min)."""
        return self._get_all_pages(
            '/api/v1/conversations',
            params={'scope': 'sent'},
            ttl=TTL_CONVERSATIONS,
        )

    def get_discussion_topics(self, course_id):
        """All discussion topics for a course (cached 15 min)."""
        return self._get_all_pages(
            f'/api/v1/courses/{course_id}/discussion_topics',
            ttl=TTL_DISCUSSION_ENTRIES,
        )

    def get_discussion_entries(self, course_id, topic_id):
        """All entries for a discussion topic, including recent_replies (cached 15 min)."""
        return self._get_all_pages(
            f'/api/v1/courses/{course_id}/discussion_topics/{topic_id}/entries',
            ttl=TTL_DISCUSSION_ENTRIES,
        )
