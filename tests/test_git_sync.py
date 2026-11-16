import json
import subprocess

import pytest

from app import db
from app.models.saved_discussion_post import SavedDiscussionPost
from app.services.git_sync import DATA_PATH, GitSyncError, commit_data, fetch_data


def git(cwd, *args):
    return subprocess.run(['git', *args], cwd=cwd, check=True, capture_output=True, text=True).stdout


@pytest.fixture()
def repos(tmp_path):
    """A bare 'remote' plus two clones: this machine and 'the other Mac'."""
    remote = tmp_path / 'remote.git'
    git(tmp_path, 'init', '--bare', '-b', 'main', str(remote))
    clones = []
    for name in ('here', 'other'):
        d = tmp_path / name
        git(tmp_path, 'clone', str(remote), str(d))
        git(d, 'config', 'user.email', 't@example.com')
        git(d, 'config', 'user.name', 'T')
        clones.append(d)
    here = clones[0]
    (here / 'README').write_text('x')
    git(here, 'add', '.')
    git(here, 'commit', '-m', 'init')
    git(here, 'push', '-u', 'origin', 'main')
    git(clones[1], 'pull', 'origin', 'main')
    git(clones[1], 'branch', '--set-upstream-to=origin/main', 'main')
    return clones


def _add_post(entry_id=900, title='T'):
    db.session.add(SavedDiscussionPost(course_id=5, topic_id=30, entry_id=entry_id,
                                       body='<p>x</p>', board_name='B', title=title))
    db.session.commit()


def test_commit_pushes_only_the_data_file(app, repos):
    here, other = repos
    (here / 'unrelated.txt').write_text('dirty')
    git(here, 'add', 'unrelated.txt')            # staged but must not be committed
    _add_post()
    assert 'Committed and pushed 1 posts' in commit_data(str(here))
    git(other, 'pull')
    assert json.loads((other / DATA_PATH).read_text())['saved_posts'][0]['entry_id'] == 900
    assert not (other / 'unrelated.txt').exists()
    assert 'unrelated.txt' in git(here, 'status', '--short')    # still staged, uncommitted


def test_commit_with_no_changes_does_nothing(app, repos):
    here, _ = repos
    _add_post()
    commit_data(str(here))
    head = git(here, 'rev-parse', 'HEAD')
    assert 'nothing to commit' in commit_data(str(here))
    assert git(here, 'rev-parse', 'HEAD') == head


def test_commit_reports_rejected_push(app, repos):
    here, other = repos
    (other / 'x.txt').write_text('1')
    git(other, 'add', '.')
    git(other, 'commit', '-m', 'ahead')
    git(other, 'push')
    _add_post()
    with pytest.raises(GitSyncError, match='pull them first'):
        commit_data(str(here))


def test_fetch_merges_data_from_the_remote_without_touching_files(app, repos):
    here, other = repos
    _add_post(901, 'From other Mac')
    commit_data(str(other))                       # 'other' shares this test DB; export it
    SavedDiscussionPost.query.delete()
    db.session.commit()
    _add_post(902, 'Local only')
    message, counts = fetch_data(str(here))
    assert counts['posts'] == 1 and 'Fetched 1 posts' in message
    assert {p.entry_id for p in SavedDiscussionPost.query} == {901, 902}   # merged, none deleted
    assert not (here / DATA_PATH).exists()        # working tree untouched


def test_fetch_without_data_file_in_repo(app, repos):
    here, _ = repos
    with pytest.raises(GitSyncError, match='no Discussion Helper data file'):
        fetch_data(str(here))


def test_endpoints(app, client, repos):
    here, _ = repos
    app.config['REPO_DIR'] = str(here)
    try:
        _add_post()
        resp = client.post('/api/discussion-data/commit')
        assert resp.get_json()['ok'] and 'Committed' in resp.get_json()['message']
        assert client.post('/api/discussion-data/fetch').get_json()['ok']
        app.config['REPO_DIR'] = str(here / 'README')       # not a git repo dir
        bad = client.post('/api/discussion-data/commit')
        assert bad.status_code == 502 and bad.get_json()['ok'] is False
    finally:
        app.config.pop('REPO_DIR', None)


def test_page_has_sync_buttons(client):
    page = client.get('/tools/discussion-helper').get_data(as_text=True)
    assert 'Commit data to repo' in page and 'Fetch data from repo' in page
