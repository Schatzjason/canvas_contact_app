from datetime import datetime, timezone

from app import db


class MessageTemplate(db.Model):
    __tablename__ = 'message_template'

    id         = db.Column(db.Integer, primary_key=True)
    name       = db.Column(db.String(120), nullable=False)
    subject    = db.Column(db.String(255), nullable=False, default='')
    body       = db.Column(db.Text, nullable=False, default='')
    created_at = db.Column(db.DateTime(timezone=True), nullable=False,
                           default=lambda: datetime.now(timezone.utc))
