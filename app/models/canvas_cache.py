from datetime import datetime, timezone

from app import db


class CanvasCache(db.Model):
    __tablename__ = 'canvas_cache'

    id = db.Column(db.Integer, primary_key=True)
    # SHA-256 hex digest of path + sorted params
    cache_key = db.Column(db.String(64), unique=True, nullable=False, index=True)
    response_json = db.Column(db.JSON, nullable=False)
    fetched_at = db.Column(db.DateTime(timezone=True), nullable=False)
    ttl_seconds = db.Column(db.Integer, nullable=False)

    def is_fresh(self):
        age = (datetime.now(timezone.utc) - self.fetched_at).total_seconds()
        return age < self.ttl_seconds
