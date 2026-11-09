"""Recurring course names / module names and their links to Canvas ids."""
import re

from app import db
from app.models.course_catalog import (
    CanvasCourseLink, CanvasModuleLink, CourseName, ModuleName,
)

# "Module 6: OOP Part 1  [Starts 10/7]" -> "OOP Part 1"
_MODULE_TITLE = re.compile(r'^\s*Module\s*\w+\s*:\s*(.*?)\s*\[\s*Starts', re.IGNORECASE)


def parse_module_title(title):
    """The module name between "Module x:" and "[Starts ...", or None."""
    m = _MODULE_TITLE.match(title or '')
    return m.group(1).strip() if m and m.group(1).strip() else None


def get_or_create_course_name(name):
    row = CourseName.query.filter(db.func.lower(CourseName.name) == name.strip().lower()).first()
    if row is None:
        row = CourseName(name=name.strip())
        db.session.add(row)
        db.session.flush()
    return row


def link_canvas_course(canvas_course_id, course_name, modules, create_missing=False):
    """Link a Canvas course to a CourseName and match its modules to ModuleNames.

    `modules` is the Canvas modules list. With create_missing, module titles with
    no existing ModuleName are added (used for the reference course). Returns
    {'matched': [...], 'created': [...], 'unmatched': [titles]}.
    """
    course = get_or_create_course_name(course_name)
    link = CanvasCourseLink.query.filter_by(canvas_course_id=canvas_course_id).first()
    if link is None:
        db.session.add(CanvasCourseLink(canvas_course_id=canvas_course_id,
                                        course_name_id=course.id))
    else:
        link.course_name_id = course.id

    existing = {m.name.lower(): m for m in course.modules}
    result = {'matched': [], 'created': [], 'unmatched': []}
    for index, module in enumerate(modules):
        name = parse_module_title(module.get('name'))
        if name is None:
            result['unmatched'].append(module.get('name'))
            continue
        target = existing.get(name.lower())
        if target is None:
            if not create_missing:
                result['unmatched'].append(module.get('name'))
                continue
            target = ModuleName(course_name_id=course.id, name=name, position=index)
            db.session.add(target)
            db.session.flush()
            existing[name.lower()] = target
            result['created'].append(name)
        else:
            result['matched'].append(name)
        cm = CanvasModuleLink.query.filter_by(
            canvas_course_id=canvas_course_id, canvas_module_id=module['id']).first()
        if cm is None:
            db.session.add(CanvasModuleLink(canvas_course_id=canvas_course_id,
                                            canvas_module_id=module['id'],
                                            module_name_id=target.id))
        else:
            cm.module_name_id = target.id
    db.session.commit()
    return result


def module_name_for_canvas_module(canvas_course_id, canvas_module_id):
    """The linked ModuleName for a Canvas module, or None."""
    link = CanvasModuleLink.query.filter_by(
        canvas_course_id=canvas_course_id, canvas_module_id=canvas_module_id).first()
    return db.session.get(ModuleName, link.module_name_id) if link else None
