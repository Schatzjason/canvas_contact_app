from datetime import datetime, timezone

from app import db


class SavedDiscussionPost(db.Model):
    """A discussion post saved from Canvas for reuse in other courses.
    One row per Canvas entry; saving the same entry again updates the row."""
    __tablename__ = 'saved_discussion_post'

    id          = db.Column(db.Integer, primary_key=True)
    course_id   = db.Column(db.BigInteger, nullable=False)
    topic_id    = db.Column(db.BigInteger, nullable=False)
    entry_id    = db.Column(db.BigInteger, nullable=False)
    body        = db.Column(db.Text, nullable=False)           # Canvas HTML
    board_name  = db.Column(db.String(255), nullable=False, default='')
    module_name = db.Column(db.String(255), nullable=True)   # Canvas's title (fallback)
    module_name_id = db.Column(db.Integer, db.ForeignKey('module_name.id'), nullable=True)
    module = db.relationship('ModuleName')
    saved_at    = db.Column(db.DateTime(timezone=True), nullable=False,
                            default=lambda: datetime.now(timezone.utc))

    __table_args__ = (
        db.UniqueConstraint('course_id', 'entry_id', name='uq_saved_discussion_post'),
    )
