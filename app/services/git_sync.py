"""Move Discussion Helper data through the project's git repo (the buttons on
the Discussion Helper page). Only the data file is ever committed or read;
the rest of the working tree is never touched."""
import json
import os
import subprocess

from app.services.discussion_data import export_data, import_data

DATA_PATH = 'data/discussion_helper.json'


class GitSyncError(Exception):
    """A user-presentable failure."""


def _git(repo_dir, *args, check=True):
    env = dict(os.environ, GIT_TERMINAL_PROMPT='0')
    try:
        proc = subprocess.run(['git', *args], cwd=repo_dir, capture_output=True,
                              text=True, timeout=90, env=env)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitSyncError(f'git could not run: {exc}') from exc
    if check and proc.returncode != 0:
        detail = (proc.stderr or proc.stdout).strip().splitlines()
        raise GitSyncError(f"git {args[0]} failed: {detail[-1] if detail else 'unknown error'}")
    return proc


def commit_data(repo_dir):
    """Export to the data file, commit just that file and push. Returns a
    status message."""
    data = export_data()
    full = os.path.join(repo_dir, DATA_PATH)
    try:
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, 'w', encoding='utf-8') as f:
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.write('\n')
    except OSError as exc:
        raise GitSyncError(f'Could not write {DATA_PATH}: {exc.strerror or exc}') from exc

    _git(repo_dir, 'add', '--', DATA_PATH)
    if _git(repo_dir, 'diff', '--cached', '--quiet', '--', DATA_PATH, check=False).returncode == 0:
        return 'Data is already up to date in the repo; nothing to commit.'
    _git(repo_dir, 'commit', '-m',
         f"Update Discussion Helper data ({len(data['saved_posts'])} posts)",
         '--', DATA_PATH)
    try:
        _git(repo_dir, 'push')
    except GitSyncError as exc:
        raise GitSyncError(
            f'Committed locally, but the push failed ({exc}). If the repo has newer '
            'commits, pull them first, then try again.') from exc
    return f"Committed and pushed {len(data['saved_posts'])} posts."


def fetch_data(repo_dir):
    """Fetch from the remote and merge the data file from the upstream branch
    into the database (never deletes). Returns (message, counts)."""
    _git(repo_dir, 'fetch')
    proc = _git(repo_dir, 'show', f'@{{upstream}}:{DATA_PATH}', check=False)
    if proc.returncode != 0:
        raise GitSyncError('The repo has no Discussion Helper data file yet. '
                           'Use "Commit data to repo" on the other Mac first.')
    try:
        counts = import_data(json.loads(proc.stdout))
    except ValueError as exc:
        raise GitSyncError(str(exc)) from exc
    return f"Fetched {counts['posts']} posts from the repo.", counts
