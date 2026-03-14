from app import db


class InteractionEvent(db.Model):
    __tablename__ = 'interaction_event'

    id = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.BigInteger, nullable=False, index=True)
    student_canvas_id = db.Column(db.BigInteger, nullable=False, index=True)
    # 'conversation' | 'discussion_entry' | 'discussion_reply' | 'submission'
    event_type = db.Column(db.String(32), nullable=False)
    occurred_at = db.Column(db.DateTime(timezone=True), nullable=False)
    # Canvas object ID — combined with event_type to deduplicate on upsert
    source_id = db.Column(db.BigInteger, nullable=False)

    __table_args__ = (
        # Includes student_canvas_id so one conversation touching N students
        # produces N deduplicable rows rather than a conflict on upsert.
        db.UniqueConstraint(
            'event_type', 'source_id', 'student_canvas_id',
            name='uq_interaction_event_type_source_student',
        ),
    )
