from app.models.course_catalog import (
    CanvasCourseLink, CanvasModuleLink, CourseName, ModuleName,
)
from app.services.course_catalog import link_canvas_course, parse_module_title


def _mods(*pairs):
    return [{'id': i, 'name': n} for i, n in pairs]


def test_parse_module_title():
    assert parse_module_title('Module 1: Variables   [Starts: Wed 9/2]') == 'Variables'
    assert parse_module_title('Module 6: OOP Part 1  [Starts 10/7]') == 'OOP Part 1'
    assert parse_module_title('Module 14: Practice!    [Starts 12/9]') == 'Practice!'
    assert parse_module_title('Final Exam   [Starts: 12/16]') is None
    assert parse_module_title('Welcome to CS111B!') is None
    assert parse_module_title(None) is None


def test_reference_course_creates_names_and_links(app):
    result = link_canvas_course(
        100, 'CS111B',
        _mods((1, 'Welcome!'), (2, 'Module 0: Intro  [Starts: 8/31]'),
              (3, 'Module 1: Loops  [Starts: 9/2]')),
        create_missing=True)
    assert result['created'] == ['Intro', 'Loops']
    assert result['unmatched'] == ['Welcome!']
    assert CourseName.query.one().name == 'CS111B'
    assert [m.name for m in CourseName.query.one().modules] == ['Intro', 'Loops']
    assert CanvasModuleLink.query.count() == 2


def test_new_semester_matches_existing_names_without_creating(app):
    link_canvas_course(100, 'CS111B', _mods((2, 'Module 0: Intro  [Starts: 8/31]')),
                       create_missing=True)
    result = link_canvas_course(
        200, 'cs111b',
        _mods((20, 'Module 0: Intro  [Starts 1/20]'), (21, 'Module 1: New Topic  [Starts 1/27]')))
    assert result['matched'] == ['Intro']
    assert result['unmatched'] == ['Module 1: New Topic  [Starts 1/27]']
    assert CourseName.query.count() == 1
    assert ModuleName.query.count() == 1
    assert CanvasCourseLink.query.count() == 2
    assert CanvasModuleLink.query.filter_by(canvas_course_id=200).one().canvas_module_id == 20


def test_relinking_is_idempotent(app):
    mods = _mods((2, 'Module 0: Intro  [Starts: 8/31]'))
    link_canvas_course(100, 'CS111B', mods, create_missing=True)
    link_canvas_course(100, 'CS111B', mods, create_missing=True)
    assert CanvasModuleLink.query.count() == 1
    assert ModuleName.query.count() == 1
