"""Builds the per-module table for the student page.

The view groups a student's interaction events into a grid of (module × column),
where columns are Messages plus one per Canvas assignment group. All cache
lookups are batched: one query for submission→assignment, one for assignment
metadata, one for conversation text. Missing cache rows render as empty cells
and emit a warn-log; they never crash the page.
"""
import hashlib
import json
from datetime import datetime, timezone

from flask import current_app
from sqlalchemy import text

from app import db
from app.models.canvas_cache import CanvasCache
from app.models.course_module import CourseModule
from app.models.interaction_event import InteractionEvent

MESSAGE_EVENT_TYPES = ('conversation', 'group_conversation', 'student_message')
DISCUSSION_STUDENT_TYPES = ('discussion_entry', 'discussion_reply')


def _cache_key(path, params):
    """Match CanvasClient._make_cache_key so we can read cache rows directly."""
    payload = json.dumps({'path': path, 'params': sorted((params or {}).items())})
    return hashlib.sha256(payload.encode()).hexdigest()


def _parse_canvas_dt(value, tz):
    if not value:
        return None
    try:
        dt = datetime.fromisoformat(value.replace('Z', '+00:00'))
    except (ValueError, TypeError):
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(tz)


def _format_short_date(d):
    """Apr 21 — month abbrev plus day-of-month, no leading zero."""
    return d.strftime('%b ') + str(d.day)


def _icon_for(event_type, late=False):
    """Return (icon_class, glyph_id) for an event type.

    icon_class: space-separated CSS classes appended to .icon-wrap.
    glyph_id: the <symbol id> in base.html (without the 'ico-' prefix).
    """
    if event_type == 'conversation':
        return ('icon-msg', 'msg')
    if event_type == 'group_conversation':
        return ('icon-grp', 'grp')
    if event_type == 'student_message':
        return ('icon-msg student', 'msg')
    if event_type == 'submission':
        return ('icon-sub late' if late else 'icon-sub', 'hw')
    return ('icon-msg', 'msg')


def _drawer_label(event_type):
    return {
        'conversation': 'Instructor Message',
        'group_conversation': 'Group Message',
        'student_message': 'Student Message',
        'submission': 'Submission',
        'graded_discussion': 'Graded Discussion',
    }.get(event_type, event_type)


def build_by_module_view(course_id, student_id, tz):
    """Returns the structured payload for the By Module section.

    {
        'modules':  [{'id', 'name', 'position', 'start_date', 'end_date', 'short_date'}],
        'columns':  [{'key': 'messages' | <group_id_str> | 'placeholder', 'label': str}],
        'rows':     [{'module': <mod>, 'cells': [{'column', 'events', 'cell_id'}]}],
        'drawer_payload': {cell_id: {'header': str, 'sections': [{'label', 'text'}]}},
        'has_modules': bool,
    }
    """
    log = current_app.logger

    today = datetime.now(tz).date()
    modules = (CourseModule.query
               .filter(CourseModule.course_id == course_id,
                       CourseModule.start_date <= today)
               .order_by(CourseModule.position.desc())
               .all())

    if not modules:
        return {
            'modules': [],
            'columns': [],
            'rows': [],
            'drawer_payload': {},
            'has_modules': False,
        }

    # ── Assignment groups (one cache row, list of group dicts) ────────────────
    ag_key = _cache_key(f'/api/v1/courses/{course_id}/assignment_groups', None)
    ag_row = CanvasCache.query.filter_by(cache_key=ag_key).first()
    groups = []
    if ag_row and isinstance(ag_row.response_json, list):
        groups = sorted(ag_row.response_json, key=lambda g: g.get('position') or 0)
    else:
        log.warning('by_module: assignment_groups cache missing for course %s', course_id)

    columns = [{'key': 'messages', 'label': 'Messages'}]
    if groups:
        for g in groups:
            columns.append({'key': str(g['id']), 'label': g.get('name') or '—'})
    else:
        columns.append({'key': 'placeholder', 'label': '—'})

    # ── All events for this student in this course ────────────────────────────
    events = InteractionEvent.query.filter(
        InteractionEvent.course_id == course_id,
        InteractionEvent.student_canvas_id == student_id,
    ).all()

    submission_events = [e for e in events if e.event_type == 'submission']
    msg_events = [e for e in events if e.event_type in MESSAGE_EVENT_TYPES]
    discussion_events = [e for e in events if e.event_type in DISCUSSION_STUDENT_TYPES]

    # ── Submission → assignment mapping (one batched query) ───────────────────
    sub_info = {}  # source_id (sub) → {'assignment_id', 'late'}
    if submission_events:
        sub_ids = [e.source_id for e in submission_events]
        rows = db.session.execute(text("""
            SELECT (s->>'id')::bigint AS sub_id,
                   (s->>'assignment_id')::bigint AS assignment_id,
                   (s->>'late')::boolean AS late
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS s
            WHERE (s->>'assignment_id') IS NOT NULL
              AND (s->>'id')::bigint = ANY(:ids)
        """), {'ids': sub_ids}).fetchall()
        for r in rows:
            if r.assignment_id is not None:
                sub_info[r.sub_id] = {
                    'assignment_id': r.assignment_id,
                    'late': bool(r.late),
                }

    # ── Assignment metadata (one batched query) ───────────────────────────────
    needed_aids = list({info['assignment_id'] for info in sub_info.values()})
    assignment_meta = {}  # aid → {'group_id', 'due_date', 'name'}
    if needed_aids:
        rows = db.session.execute(text("""
            SELECT (a->>'id')::bigint AS aid,
                   a->>'name' AS name,
                   a->>'due_at' AS due_at,
                   (a->>'assignment_group_id')::bigint AS group_id
            FROM canvas_cache,
                 jsonb_array_elements(response_json::jsonb) AS a
            WHERE (a->>'id')::bigint = ANY(:ids)
        """), {'ids': needed_aids}).fetchall()
        for r in rows:
            due_dt = _parse_canvas_dt(r.due_at, tz)
            assignment_meta[r.aid] = {
                'group_id': r.group_id,
                'due_date': due_dt.date() if due_dt else None,
                'due_dt': due_dt,
                'name': r.name or '',
            }

    if submission_events and not assignment_meta:
        log.warning('by_module: assignments cache empty for course %s; '
                    'submissions will not appear in any group column', course_id)

    # ── Graded discussions: entry_id/reply_id → group + due ───────────────────
    # Build topic_id → assignment metadata for graded discussion assignments,
    # then walk each topic's entries cache (one batched fetch) to map every
    # entry/reply id back to its assignment group.
    discussion_to_group = {}
    if discussion_events:
        topic_rows = db.session.execute(text("""
            SELECT a->>'name' AS name,
                   a->>'due_at' AS due_at,
                   (a->>'assignment_group_id')::bigint AS group_id,
                   ((a->'discussion_topic')->>'id')::bigint AS topic_id
            FROM canvas_cache, jsonb_array_elements(response_json::jsonb) AS a
            WHERE jsonb_typeof(a->'discussion_topic') = 'object'
              AND ((a->'discussion_topic')->>'id') IS NOT NULL
              AND (a->>'assignment_group_id') IS NOT NULL
        """)).fetchall()
        topic_to_meta = {}
        for r in topic_rows:
            if r.topic_id is None:
                continue
            due_dt = _parse_canvas_dt(r.due_at, tz)
            topic_to_meta[r.topic_id] = {
                'group_id': r.group_id,
                'due_date': due_dt.date() if due_dt else None,
                'due_dt': due_dt,
                'name': r.name or '',
            }

        if topic_to_meta:
            key_to_topic = {}
            for tid in topic_to_meta:
                k = _cache_key(
                    f'/api/v1/courses/{course_id}/discussion_topics/{tid}/entries',
                    None,
                )
                key_to_topic[k] = tid
            entry_rows = (CanvasCache.query
                          .filter(CanvasCache.cache_key.in_(list(key_to_topic.keys())))
                          .all())
            for row in entry_rows:
                tid = key_to_topic.get(row.cache_key)
                meta = topic_to_meta.get(tid) if tid else None
                if not meta or not isinstance(row.response_json, list):
                    continue
                for entry in row.response_json:
                    eid = entry.get('id')
                    if eid:
                        discussion_to_group[eid] = meta
                    for reply in entry.get('recent_replies', []) or []:
                        rid = reply.get('id')
                        if rid:
                            discussion_to_group[rid] = meta

    # ── Conversation text (one batched query) ─────────────────────────────────
    conv_text = {}
    if msg_events:
        msg_ids = [e.source_id for e in msg_events]
        rows = db.session.execute(text("""
            SELECT (e->>'id')::bigint AS sid,
                   e->>'subject' AS subject,
                   e->>'last_authored_message' AS authored,
                   e->>'last_message' AS last_msg
            FROM canvas_cache, jsonb_array_elements(response_json::jsonb) AS e
            WHERE (e->>'id')::bigint = ANY(:ids)
        """), {'ids': msg_ids}).fetchall()
        for r in rows:
            parts = [f'Subject: {r.subject}'] if r.subject else []
            body = r.authored or r.last_msg
            if body:
                parts.append(body)
            conv_text[r.sid] = '\n\n'.join(parts)

    # ── Build cells ───────────────────────────────────────────────────────────
    rows_out = []
    drawer_payload = {}

    for module in modules:
        cells = []
        for column in columns:
            cell_events = []

            if column['key'] == 'messages':
                for e in msg_events:
                    on_date = e.occurred_at.astimezone(tz).date()
                    if module.start_date <= on_date <= module.end_date:
                        icon_class, glyph = _icon_for(e.event_type)
                        cell_events.append({
                            'event_type': e.event_type,
                            'source_id': e.source_id,
                            'occurred_at': e.occurred_at,
                            'icon_class': icon_class,
                            'glyph': glyph,
                            'late': False,
                        })
            elif column['key'] == 'placeholder':
                pass
            else:
                group_id = int(column['key'])
                for e in submission_events:
                    info = sub_info.get(e.source_id)
                    if not info:
                        continue
                    meta = assignment_meta.get(info['assignment_id'])
                    if not meta or meta['group_id'] != group_id:
                        continue
                    due_date = meta['due_date']
                    if due_date is None:
                        continue
                    if not (module.start_date <= due_date <= module.end_date):
                        continue
                    icon_class, glyph = _icon_for('submission', late=info['late'])
                    cell_events.append({
                        'event_type': 'submission',
                        'source_id': e.source_id,
                        'occurred_at': e.occurred_at,
                        'icon_class': icon_class,
                        'glyph': glyph,
                        'late': info['late'],
                        'assignment_name': meta['name'],
                        'due_dt': meta['due_dt'],
                    })
                for e in discussion_events:
                    meta = discussion_to_group.get(e.source_id)
                    if not meta or meta['group_id'] != group_id:
                        continue
                    due_date = meta['due_date']
                    if due_date is None:
                        continue
                    if not (module.start_date <= due_date <= module.end_date):
                        continue
                    icon_class, glyph = _icon_for('submission', late=False)
                    cell_events.append({
                        'event_type': 'graded_discussion',
                        'source_id': e.source_id,
                        'occurred_at': e.occurred_at,
                        'icon_class': icon_class,
                        'glyph': glyph,
                        'late': False,
                        'assignment_name': meta['name'],
                        'due_dt': meta['due_dt'],
                    })

            # Newest first → front of stack.
            cell_events.sort(key=lambda d: d['occurred_at'], reverse=True)

            cell_id = f'm{module.id}-c{column["key"]}'
            cells.append({
                'column': column,
                'events': cell_events,
                'cell_id': cell_id,
                'overflow': max(0, len(cell_events) - 3),
            })

            if cell_events:
                drawer_payload[cell_id] = _build_drawer(
                    module, column, cell_events, conv_text, tz
                )

        rows_out.append({
            'module': {
                'id': module.id,
                'name': module.name,
                'position': module.position,
                'start_date': module.start_date,
                'end_date': module.end_date,
                'short_date': _format_short_date(module.start_date),
            },
            'cells': cells,
        })

    return {
        'modules': [r['module'] for r in rows_out],
        'columns': columns,
        'rows': rows_out,
        'drawer_payload': drawer_payload,
        'has_modules': True,
    }


def _build_drawer(module, column, cell_events, conv_text, tz):
    header = f'{module.name} — {column["label"]}'
    sections = []
    for ev in cell_events:
        et = ev['event_type']
        if et == 'submission':
            parts = []
            if ev.get('assignment_name'):
                parts.append(ev['assignment_name'])
            due_dt = ev.get('due_dt')
            if due_dt:
                parts.append(f'Due {due_dt.month}/{due_dt.day}')
            if ev.get('late'):
                parts.append('Late')
            sections.append({
                'label': _drawer_label(et),
                'text': ' — '.join(parts) if parts else '',
            })
        else:
            sections.append({
                'label': _drawer_label(et),
                'text': conv_text.get(ev['source_id'], ''),
            })
    return {'header': header, 'sections': sections}
