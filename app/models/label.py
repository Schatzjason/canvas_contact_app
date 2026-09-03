from datetime import datetime, timezone

from app import db


class Label(db.Model):
    __tablename__ = 'label'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(50), nullable=False, unique=True)
    color      = db.Column(db.String(7), nullable=False)  # hex, e.g. '#e8705a'
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
