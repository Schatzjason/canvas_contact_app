from app import db


class PinnedDiscussion(db.Model):
    __tablename__ = 'pinned_discussion'

    id        = db.Column(db.Integer, primary_key=True)
    course_id = db.Column(db.BigInteger, nullable=False, unique=True)
    topic_id  = db.Column(db.BigInteger, nullable=False)
