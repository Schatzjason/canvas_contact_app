from app import db


class CheckBackDate(db.Model):
    __tablename__ = 'check_back_date'

    id                = db.Column(db.Integer, primary_key=True)
    course_id         = db.Column(db.BigInteger, nullable=False)
    student_canvas_id = db.Column(db.BigInteger, nullable=False)
    date              = db.Column(db.Date, nullable=False)
    note              = db.Column(db.String(60), nullable=False, default='')

    __table_args__ = (
        db.UniqueConstraint('course_id', 'student_canvas_id', name='uq_check_back_date'),
    )
