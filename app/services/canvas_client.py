import requests
from flask import current_app


class CanvasClient:
    # Accepts an explicit token so phase 2 (OAuth) is a one-line swap:
    # pass the User's stored access_token instead of reading from config.
    def __init__(self, token=None):
        self.base_url = current_app.config['CANVAS_BASE_URL'].rstrip('/')
        self._token = token or current_app.config['CANVAS_API_TOKEN']

    def _auth_headers(self):
        return {'Authorization': f'Bearer {self._token}'}

    def _get(self, path, params=None):
        resp = requests.get(
            f'{self.base_url}{path}',
            headers=self._auth_headers(),
            params=params,
            timeout=10,
        )
        resp.raise_for_status()
        return resp.json()

    def get_courses(self):
        """Return active courses where the instructor is enrolled as teacher."""
        return self._get('/api/v1/courses', params={
            'enrollment_type': 'teacher',
            'enrollment_state': 'active',
            'state[]': 'available',
            'per_page': 50,
        })
