"""
Every equity page must actually render.

This suite exists because four screens shipped to production returning 500, and
nothing in the test suite or the deploy noticed.

The cause was url_for pointing at endpoints that do not exist:
equity.positions (a screen deliberately not adopted) and three CSV export routes
the client's templates linked to. url_for raises at RENDER time, not at import,
so the app started cleanly, served HTTP 200 on the index, passed 495 tests, and
still 500'd on four pages the moment anyone opened them.

The verification I had done was the near miss. I checked every fetch() URL in
those templates against the URL map, which is the runtime half, and never
checked the server-side url_for calls, which is the half that renders the page
at all. Checking a template's JavaScript is not checking the template.

So this does the only thing that actually proves a page works: it renders it.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-pages-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'pages.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'pages-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import User  # noqa: E402

# Every equity page a user can open from the sidebar.
PAGES = [
    '/equity/',
    '/equity/accounts',
    '/equity/watchlist',
    '/equity/place-order',
    '/equity/order-book',
    '/equity/trade-book',
    '/equity/holdings',
    '/equity/settings',
]


@pytest.fixture(scope='module')
def client():
    app = create_app('development')
    app.config['TESTING'] = True
    app.config['WTF_CSRF_ENABLED'] = False

    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username='pages', email='pages@example.com', is_admin=True)
        user.set_password('Pages#Test1')
        db.session.add(user)
        db.session.commit()
        user_id = user.id

    test_client = app.test_client()
    with test_client.session_transaction() as session:
        session['_user_id'] = str(user_id)
        session['_fresh'] = True

    yield test_client, app

    with app.app_context():
        db.session.remove()
        db.drop_all()


@pytest.mark.parametrize('url', PAGES)
def test_the_page_renders(client, url):
    """
    A 500 here means the template referenced something the app does not have.

    Rendering with an empty database is deliberate: no accounts, no orders, no
    holdings. That is a real state (a fresh install, or every account
    deactivated) and it is the state most likely to expose a template that
    assumes rows exist.
    """
    test_client, _ = client
    response = test_client.get(url)
    assert response.status_code == 200, (
        f'{url} returned {response.status_code}. '
        'Check the app log for the render-time exception; a missing url_for '
        'endpoint is the usual cause and does not fail at import.'
    )


def test_every_url_for_endpoint_in_every_template_exists():
    """
    The specific failure, caught statically as well as by rendering.

    Rendering only covers the branches a given fixture reaches. A url_for inside
    an `{% if accounts %}` block would not be exercised by the empty-database
    test above, so the endpoints are also checked without executing anything.
    """
    import re
    import glob

    _, app = None, None
    application = create_app('development')
    endpoints = {rule.endpoint for rule in application.url_map.iter_rules()}

    broken = []
    for path in sorted(glob.glob(str(REPO_ROOT / 'app' / 'templates' / '**' / '*.html'),
                                 recursive=True)):
        text = Path(path).read_text(encoding='utf-8')
        for match in re.finditer(r"""url_for\(\s*['"]([a-zA-Z0-9_.]+)['"]""", text):
            endpoint = match.group(1)
            if endpoint not in endpoints:
                line = text[:match.start()].count(chr(10)) + 1
                broken.append(f'{Path(path).name}:{line} -> {endpoint}')

    assert not broken, (
        'These url_for endpoints do not exist, so every render of those pages '
        'raises: ' + '; '.join(broken)
    )
