import os

import pytest

# Override DATABASE_URL before app is imported.
# load_dotenv() (called inside app/__init__.py) will not overwrite env vars
# that are already set, so this wins over any value in .env.
_test_db_url = os.environ.get(
    'DATABASE_TEST_URL',
    'postgresql://localhost/canvas_contact_app_test',
)
os.environ['DATABASE_URL'] = _test_db_url
os.environ.setdefault('CANVAS_API_TOKEN', 'test-token')

from app import create_app, db as _db  # noqa: E402


@pytest.fixture(scope='session')
def app():
    application = create_app()
    application.config['TESTING'] = True
    with application.app_context():
        _db.create_all()
        yield application
        _db.drop_all()


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture(autouse=True)
def clean_db(app):
    """Delete all rows from every table after each test for isolation."""
    yield
    _db.session.rollback()
    for table in reversed(_db.metadata.sorted_tables):
        _db.session.execute(table.delete())
    _db.session.commit()
