from datetime import datetime, timezone

from app import db


class StudentRecord(db.Model):
    __tablename__ = 'student_record'

    id                = db.Column(db.Integer, primary_key=True)
    course_id         = db.Column(db.BigInteger, nullable=False)
    student_canvas_id = db.Column(db.BigInteger, nullable=False)
    name              = db.Column(db.String(255), nullable=False)
    sortable_name     = db.Column(db.String(255), nullable=False)
    status            = db.Column(db.String(16), nullable=False, default='active')
    dropped_at        = db.Column(db.DateTime(timezone=True), nullable=True)

    __table_args__ = (
        db.UniqueConstraint('course_id', 'student_canvas_id', name='uq_student_record'),
        db.Index('ix_student_record_course_status', 'course_id', 'status'),
    )
