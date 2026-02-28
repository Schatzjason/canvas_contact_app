import hashlib
import json
from datetime import datetime, timezone

import requests
from flask import current_app

from app import db
from app.models.canvas_cache import CanvasCache

# TTL constants (seconds)
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

    def _get(self, path, params=None, ttl=None):
        """GET path, using the DB cache when ttl is provided."""
        if ttl is not None:
            cache_key = self._make_cache_key(path, params)
            try:
                entry = CanvasCache.query.filter_by(cache_key=cache_key).first()
                if entry and entry.is_fresh():
                    return entry.response_json
            except Exception as exc:
                current_app.logger.warning('Cache read failed: %s', exc)

        resp = requests.get(
            f'{self.base_url}{path}',
            headers=self._auth_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        data = resp.json()

        if ttl is not None:
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

        return data

    def get_courses(self):
        """Return active teacher courses — always fetched live, never cached."""
        return self._get('/api/v1/courses', params={
            'enrollment_type': 'teacher',
            'enrollment_state': 'active',
            'state[]': 'available',
            'per_page': 50,
        })
