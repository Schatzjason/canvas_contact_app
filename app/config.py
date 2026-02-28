import os


class Config:
    SECRET_KEY = os.environ.get('FLASK_SECRET_KEY', 'dev-secret-change-me')
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL')
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    CANVAS_BASE_URL = os.environ.get('CANVAS_BASE_URL', 'https://ccsf.instructure.com')
    CANVAS_API_TOKEN = os.environ.get('CANVAS_API_TOKEN')

    STALE_WARN_DAYS = int(os.environ.get('STALE_WARN_DAYS', 7))
    STALE_ALERT_DAYS = int(os.environ.get('STALE_ALERT_DAYS', 14))
