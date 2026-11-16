"""Export / import of Discussion Helper data (saved posts + course catalog) as
JSON, for moving it between machines. Students, notes and Canvas caches are
deliberately not included."""
from datetime import datetime

from app import db
from app.models.course_catalog import (
    CanvasCourseLink, CanvasModuleLink, CourseName, ModuleName,
)
from app.models.saved_discussion_post import SavedDiscussionPost

FORMAT_VERSION = 1


def export_data():
    courses = {c.id: c for c in CourseName.query.all()}
    modules = {m.id: m for m in ModuleName.query.all()}

    return {
        'version': FORMAT_VERSION,
        'course_names': [
            {'name': c.name,
             'modules': [{'name': m.name, 'position': m.position}
                         for m in sorted(c.modules, key=lambda m: (m.position, m.name))]}
            for c in sorted(courses.values(), key=lambda c: c.name.lower())
        ],
        'canvas_course_links': [
            {'canvas_course_id': l.canvas_course_id,
             'course_name': courses[l.course_name_id].name}
            for l in CanvasCourseLink.query.order_by(CanvasCourseLink.canvas_course_id)
        ],
        'canvas_module_links': [
            {'canvas_course_id': l.canvas_course_id,
             'canvas_module_id': l.canvas_module_id,
             'course_name': courses[modules[l.module_name_id].course_name_id].name,
             'module_name': modules[l.module_name_id].name}
            for l in CanvasModuleLink.query.order_by(
                CanvasModuleLink.canvas_course_id, CanvasModuleLink.canvas_module_id)
        ],
        'saved_posts': [
            {'course_id': p.course_id, 'topic_id': p.topic_id, 'entry_id': p.entry_id,
             'body': p.body, 'board_name': p.board_name, 'title': p.title,
             'canvas_module_name': p.module_name,
             'course_name': courses[modules[p.module_name_id].course_name_id].name
                            if p.module_name_id else None,
             'module_name': modules[p.module_name_id].name if p.module_name_id else None,
             'saved_at': p.saved_at.isoformat() if p.saved_at else None}
            for p in SavedDiscussionPost.query.order_by(
                SavedDiscussionPost.course_id, SavedDiscussionPost.entry_id)
        ],
    }


def _module(course_name, module_name, cache):
    key = (course_name.lower(), module_name.lower())
    if key not in cache:
        course = CourseName.query.filter(
            db.func.lower(CourseName.name) == course_name.lower()).first()
        cache[key] = ModuleName.query.filter(
            ModuleName.course_name_id == course.id,
            db.func.lower(ModuleName.name) == module_name.lower()).first() if course else None
    return cache[key]


def import_data(data):
    """Merge exported data into the database (upsert; never deletes). Returns
    counts. Raises ValueError for a file that isn't a recognised export."""
    if not isinstance(data, dict) or data.get('version') != FORMAT_VERSION:
        raise ValueError('Not a Discussion Helper export (unsupported or missing version).')
    counts = {'course_names': 0, 'modules': 0, 'course_links': 0,
              'module_links': 0, 'posts': 0}
    try:
        for c in data.get('course_names', []):
            course = CourseName.query.filter(
                db.func.lower(CourseName.name) == c['name'].lower()).first()
            if course is None:
                course = CourseName(name=c['name'])
                db.session.add(course)
                db.session.flush()
            counts['course_names'] += 1
            for m in c.get('modules', []):
                mod = ModuleName.query.filter(
                    ModuleName.course_name_id == course.id,
                    db.func.lower(ModuleName.name) == m['name'].lower()).first()
                if mod is None:
                    db.session.add(ModuleName(course_name_id=course.id, name=m['name'],
                                              position=m.get('position', 0)))
                else:
                    mod.position = m.get('position', mod.position)
                counts['modules'] += 1
        db.session.flush()

        for l in data.get('canvas_course_links', []):
            course = CourseName.query.filter(
                db.func.lower(CourseName.name) == l['course_name'].lower()).one()
            row = CanvasCourseLink.query.filter_by(canvas_course_id=l['canvas_course_id']).first()
            if row is None:
                db.session.add(CanvasCourseLink(canvas_course_id=l['canvas_course_id'],
                                                course_name_id=course.id))
            else:
                row.course_name_id = course.id
            counts['course_links'] += 1

        cache = {}
        for l in data.get('canvas_module_links', []):
            mod = _module(l['course_name'], l['module_name'], cache)
            if mod is None:
                raise ValueError(f"Unknown module {l['course_name']} / {l['module_name']}.")
            row = CanvasModuleLink.query.filter_by(
                canvas_course_id=l['canvas_course_id'],
                canvas_module_id=l['canvas_module_id']).first()
            if row is None:
                db.session.add(CanvasModuleLink(canvas_course_id=l['canvas_course_id'],
                                                canvas_module_id=l['canvas_module_id'],
                                                module_name_id=mod.id))
            else:
                row.module_name_id = mod.id
            counts['module_links'] += 1

        for p in data.get('saved_posts', []):
            mod = (_module(p['course_name'], p['module_name'], cache)
                   if p.get('course_name') and p.get('module_name') else None)
            post = SavedDiscussionPost.query.filter_by(
                course_id=p['course_id'], entry_id=p['entry_id']).first()
            if post is None:
                post = SavedDiscussionPost(course_id=p['course_id'], entry_id=p['entry_id'])
                db.session.add(post)
            post.topic_id = p['topic_id']
            post.body = p['body']
            post.board_name = p.get('board_name') or ''
            post.title = p.get('title') or None
            post.module_name = p.get('canvas_module_name')
            post.module_name_id = mod.id if mod else None
            if p.get('saved_at'):
                post.saved_at = datetime.fromisoformat(p['saved_at'])
            counts['posts'] += 1
        db.session.commit()
    except (KeyError, TypeError) as exc:
        db.session.rollback()
        raise ValueError(f'Malformed export file: {exc!r}') from exc
    except Exception:
        db.session.rollback()
        raise
    return counts
