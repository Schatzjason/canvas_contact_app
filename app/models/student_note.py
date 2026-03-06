from datetime import datetime, timezone

from app import db


class StudentNote(db.Model):
    __tablename__ = 'student_note'

    id                = db.Column(db.Integer, primary_key=True)
    course_id         = db.Column(db.BigInteger, nullable=False)
    student_canvas_id = db.Column(db.BigInteger, nullable=False)
    content           = db.Column(db.Text, nullable=False, default='')
    updated_at        = db.Column(db.DateTime(timezone=True), nullable=False,
                                  default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('course_id', 'student_canvas_id', name='uq_student_note'),
    )
