from datetime import datetime, timezone

from app import db


class CourseDisplayName(db.Model):
    __tablename__ = 'course_display_name'

    id          = db.Column(db.Integer, primary_key=True)
    course_id   = db.Column(db.BigInteger, nullable=False, unique=True)
    name        = db.Column(db.String(255), nullable=False)
    updated_at  = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))
