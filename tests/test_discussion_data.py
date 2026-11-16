import json

import pytest

from app import db
from app.models.course_catalog import (
    CanvasCourseLink, CanvasModuleLink, CourseName, ModuleName,
)
from app.models.saved_discussion_post import SavedDiscussionPost
from app.services.course_catalog import link_canvas_course
from app.services.discussion_data import export_data, import_data


def _seed():
    link_canvas_course(
        5, 'CS111B',
        [{'id': 7, 'name': 'Module 0: Intro  [Starts: 8/31]'}], create_missing=True)
    mod = ModuleName.query.one()
    db.session.add(SavedDiscussionPost(
        course_id=5, topic_id=30, entry_id=900, body='<p>Hi ☃</p>',
        board_name='Introductions', title='Welcome', module_name='Module 0: Intro',
        module_name_id=mod.id))
    db.session.add(SavedDiscussionPost(
        course_id=5, topic_id=31, entry_id=901, body='<p>No module</p>',
        board_name='Other'))
    db.session.commit()


def _wipe():
    for model in (SavedDiscussionPost, CanvasModuleLink, CanvasCourseLink, ModuleName, CourseName):
        model.query.delete()
    db.session.commit()


def test_round_trip_restores_everything(app):
    _seed()
    exported = json.loads(json.dumps(export_data()))    # via real JSON
    _wipe()
    counts = import_data(exported)
    assert counts['posts'] == 2
    post = SavedDiscussionPost.query.filter_by(entry_id=900).one()
    assert (post.title, post.body, post.module.name) == ('Welcome', '<p>Hi ☃</p>', 'Intro')
    assert SavedDiscussionPost.query.filter_by(entry_id=901).one().module is None
    assert CanvasModuleLink.query.one().canvas_module_id == 7
    assert CanvasCourseLink.query.one().canvas_course_id == 5
    assert export_data() == exported                    # stable round trip


def test_import_is_idempotent_and_updates(app):
    _seed()
    data = export_data()
    data['saved_posts'][0]['title'] = 'Changed'
    import_data(data)
    import_data(data)
    assert SavedDiscussionPost.query.count() == 2
    assert ModuleName.query.count() == 1
    assert SavedDiscussionPost.query.filter_by(entry_id=900).one().title == 'Changed'


def test_import_never_deletes_existing_rows(app):
    _seed()
    data = export_data()
    data['saved_posts'] = data['saved_posts'][:1]
    import_data(data)
    assert SavedDiscussionPost.query.count() == 2


def test_import_rejects_bad_files(app):
    for bad in ({}, {'version': 99}, [], 'x'):
        with pytest.raises(ValueError):
            import_data(bad)
    with pytest.raises(ValueError):
        import_data({'version': 1, 'saved_posts': [{'course_id': 1}]})
    assert SavedDiscussionPost.query.count() == 0


def test_cli_export_then_import(app, tmp_path):
    _seed()
    path = str(tmp_path / 'out.json')
    runner = app.test_cli_runner()
    assert 'Wrote' in runner.invoke(args=['export-discussion-data', '--path', path]).output
    _wipe()
    result = runner.invoke(args=['import-discussion-data', '--path', path])
    assert result.exit_code == 0 and '2 posts' in result.output
    missing = runner.invoke(args=['import-discussion-data', '--path', str(tmp_path / 'nope.json')])
    assert missing.exit_code != 0
