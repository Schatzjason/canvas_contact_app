from app import db


class CourseModule(db.Model):
    __tablename__ = 'course_module'

    id               = db.Column(db.Integer, primary_key=True)
    course_id        = db.Column(db.BigInteger, nullable=False, index=True)
    canvas_module_id = db.Column(db.BigInteger, nullable=False)
    name             = db.Column(db.String(255), nullable=False)
    position         = db.Column(db.Integer, nullable=False)
    start_date       = db.Column(db.Date, nullable=False)
    end_date         = db.Column(db.Date, nullable=False)

    __table_args__ = (
        db.UniqueConstraint('course_id', 'canvas_module_id', name='uq_course_module'),
        db.Index('ix_course_module_course_dates', 'course_id', 'start_date', 'end_date'),
    )
