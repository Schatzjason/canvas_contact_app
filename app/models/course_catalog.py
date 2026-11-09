from app import db


class CourseName(db.Model):
    """A course that recurs semester to semester, e.g. "CS111B"."""
    __tablename__ = 'course_name'

    id   = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(100), nullable=False, unique=True)

    modules = db.relationship('ModuleName', backref='course_name',
                              order_by='ModuleName.position', cascade='all, delete-orphan')


class ModuleName(db.Model):
    """A module of a CourseName, e.g. "Loops" in CS111B. Persists across semesters."""
    __tablename__ = 'module_name'

    id             = db.Column(db.Integer, primary_key=True)
    course_name_id = db.Column(db.Integer, db.ForeignKey('course_name.id'), nullable=False)
    name           = db.Column(db.String(255), nullable=False)
    position       = db.Column(db.Integer, nullable=False, default=0)

    __table_args__ = (
        db.UniqueConstraint('course_name_id', 'name', name='uq_module_name'),
    )


class CanvasCourseLink(db.Model):
    """Which CourseName a Canvas course (one per semester) is an instance of."""
    __tablename__ = 'canvas_course_link'

    id               = db.Column(db.Integer, primary_key=True)
    canvas_course_id = db.Column(db.BigInteger, nullable=False, unique=True)
    course_name_id   = db.Column(db.Integer, db.ForeignKey('course_name.id'), nullable=False)


class CanvasModuleLink(db.Model):
    """Which ModuleName a Canvas module id in a Canvas course corresponds to."""
    __tablename__ = 'canvas_module_link'

    id               = db.Column(db.Integer, primary_key=True)
    canvas_course_id = db.Column(db.BigInteger, nullable=False)
    canvas_module_id = db.Column(db.BigInteger, nullable=False)
    module_name_id   = db.Column(db.Integer, db.ForeignKey('module_name.id'), nullable=False)

    __table_args__ = (
        db.UniqueConstraint('canvas_course_id', 'canvas_module_id', name='uq_canvas_module_link'),
    )
