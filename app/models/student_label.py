from datetime import datetime, timezone

from app import db


class StudentLabel(db.Model):
    __tablename__ = 'student_label'

    id                = db.Column(db.Integer, primary_key=True)
    course_id         = db.Column(db.BigInteger, nullable=False)
    student_canvas_id = db.Column(db.BigInteger, nullable=False)
    label_id          = db.Column(db.Integer, db.ForeignKey('label.id', ondelete='CASCADE'), nullable=False)
    created_at        = db.Column(db.DateTime(timezone=True), nullable=False,
                                  default=lambda: datetime.now(timezone.utc))

    label = db.relationship('Label')

    __table_args__ = (
        # A student can carry several labels, but not the same one twice.
        db.UniqueConstraint('course_id', 'student_canvas_id', 'label_id', name='uq_student_label'),
        db.Index('ix_student_label_course_student', 'course_id', 'student_canvas_id'),
    )
