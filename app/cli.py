import click
from datetime import datetime, timezone

from sqlalchemy import distinct
from sqlalchemy.dialects.postgresql import insert as pg_insert


def register_commands(app):

    @app.cli.command('backfill-student-records')
    def backfill_student_records():
        """One-time backfill: create StudentRecord rows for all students.

        For each course:
        1. Fetch active enrollments → create 'active' records.
        2. Fetch completed/inactive enrollments → match against InteractionEvent
           student IDs that have no record yet → create 'dropped' records.
        3. Any remaining orphan student IDs (no enrollment data at all) get a
           placeholder 'dropped' record.
        """
        from app import db
        from app.models.check_back_date import CheckBackDate
        from app.models.interaction_event import InteractionEvent
        from app.models.student_record import StudentRecord
        from app.services.canvas_client import CanvasClient

        client = CanvasClient()

        click.echo('Fetching courses...')
        courses = client.get_courses()
        click.echo(f'Found {len(courses)} courses.')

        for course in courses:
            cid = course['id']
            code = course.get('course_code', cid)
            click.echo(f'\n── {code} (id={cid}) ──')

            # Step 1: active enrollments
            try:
                active = client.get_enrollments(cid)
            except Exception as exc:
                click.echo(f'  ERROR fetching active enrollments: {exc}')
                active = []

            active_ids = set()
            if active:
                records = []
                for e in active:
                    u = e.get('user', {})
                    sid = e['user_id']
                    active_ids.add(sid)
                    records.append({
                        'course_id': cid,
                        'student_canvas_id': sid,
                        'name': u.get('name', f'Student {sid}'),
                        'sortable_name': u.get('sortable_name') or u.get('name', f'Student {sid}'),
                        'status': 'active',
                        'dropped_at': None,
                    })
                stmt = pg_insert(StudentRecord.__table__).values(records)
                stmt = stmt.on_conflict_do_update(
                    constraint='uq_student_record',
                    set_={
                        'name': stmt.excluded.name,
                        'sortable_name': stmt.excluded.sortable_name,
                        'status': 'active',
                        'dropped_at': None,
                    },
                )
                db.session.execute(stmt)
                db.session.commit()
                click.echo(f'  {len(active)} active students recorded.')

            # Step 2: find student IDs from InteractionEvent that have no StudentRecord
            all_event_sids = {
                row[0] for row in db.session.query(
                    distinct(InteractionEvent.student_canvas_id)
                ).filter(InteractionEvent.course_id == cid).all()
            }
            existing_sids = {
                row[0] for row in db.session.query(
                    StudentRecord.student_canvas_id
                ).filter(StudentRecord.course_id == cid).all()
            }
            orphan_sids = all_event_sids - existing_sids

            if not orphan_sids:
                click.echo('  No dropped students to backfill.')
                continue

            # Step 3: try to get names from completed/inactive enrollments
            name_map = {}
            for state in ('completed', 'inactive', 'deleted'):
                try:
                    enrs = client._get_all_pages(
                        f'/api/v1/courses/{cid}/enrollments',
                        params={'type[]': 'StudentEnrollment', 'state[]': state},
                    )
                    for e in enrs:
                        sid = e['user_id']
                        if sid in orphan_sids and sid not in name_map:
                            u = e.get('user', {})
                            name_map[sid] = {
                                'name': u.get('name', f'Student {sid}'),
                                'sortable_name': u.get('sortable_name') or u.get('name', f'Student {sid}'),
                            }
                except Exception:
                    pass

            # Step 4: create dropped records
            now = datetime.now(timezone.utc)
            dropped_records = []
            for sid in orphan_sids:
                info = name_map.get(sid, {
                    'name': f'Student {sid}',
                    'sortable_name': f'Student {sid}',
                })
                dropped_records.append({
                    'course_id': cid,
                    'student_canvas_id': sid,
                    'name': info['name'],
                    'sortable_name': info['sortable_name'],
                    'status': 'dropped',
                    'dropped_at': now,
                })

            stmt = pg_insert(StudentRecord.__table__).values(dropped_records)
            stmt = stmt.on_conflict_do_update(
                constraint='uq_student_record',
                set_={
                    'name': stmt.excluded.name,
                    'sortable_name': stmt.excluded.sortable_name,
                    'status': stmt.excluded.status,
                    'dropped_at': stmt.excluded.dropped_at,
                },
            )
            db.session.execute(stmt)

            # Remove check-back dates for dropped students
            dropped_ids = [sid for sid in orphan_sids]
            deleted_cb = CheckBackDate.query.filter(
                CheckBackDate.course_id == cid,
                CheckBackDate.student_canvas_id.in_(dropped_ids),
            ).delete(synchronize_session=False)

            db.session.commit()

            named = sum(1 for sid in orphan_sids if sid in name_map)
            unnamed = len(orphan_sids) - named
            click.echo(f'  {len(orphan_sids)} dropped students flagged ({named} with names, {unnamed} as placeholders).')
            if deleted_cb:
                click.echo(f'  {deleted_cb} check-back date(s) removed.')

        total = StudentRecord.query.count()
        active_total = StudentRecord.query.filter_by(status='active').count()
        dropped_total = StudentRecord.query.filter_by(status='dropped').count()
        click.echo(f'\nDone. {total} total records: {active_total} active, {dropped_total} dropped.')
