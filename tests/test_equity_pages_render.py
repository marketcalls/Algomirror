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


# ---------------------------------------------------------------------------
# The JSON endpoints behind those pages.
#
# A page rendering 200 says nothing about the endpoint that fills it: the
# template is served first and the data is fetched separately. _build_watchlist_
# payload carried a NameError for several commits, so /equity/watchlist rendered
# perfectly and then showed no data, and every page test still passed.
#
# The cause was a blanket edit that added the same line to three payload
# builders. Two of them bound `context`; the watch list bound `creds`. Nothing
# imports or type-checks its way to that, and only calling the function finds it.
# ---------------------------------------------------------------------------

# Every GET endpoint that needs no query parameters. The list is asserted
# complete below, so a new endpoint cannot be added without being covered.
API_ENDPOINTS = [
    '/equity/api/accounts',
    '/equity/api/dashboard',
    '/equity/api/external-activity',
    '/equity/api/holdings',
    '/equity/api/holdings/exit-queue',
    '/equity/api/order-book',
    '/equity/api/orders/status',
    '/equity/api/settings/preferences',
    '/equity/api/settings/rates',
    '/equity/api/trade-book',
    '/equity/api/trade-natures',
    '/equity/api/watchlist',
    '/equity/api/watchlist/quotes',
]

EXPORTS = [
    '/equity/api/holdings/export',
    '/equity/api/order-book/export',
    '/equity/api/trade-book/export',
    '/equity/api/watchlist/export',
]

# Excluded, with the reason, so the completeness check below stays honest.
#   depth, quote, symbol-search  require a query parameter
#   stream                       an SSE connection that never returns
NEEDS_PARAMS = {
    '/equity/api/depth', '/equity/api/quote', '/equity/api/symbol-search',
    '/equity/api/stream',
}


@pytest.mark.parametrize('url', API_ENDPOINTS)
def test_the_api_endpoint_answers(client, url):
    """A 500 here means the payload builder raised."""
    test_client, _ = client
    response = test_client.get(url)
    assert response.status_code == 200, (
        f'{url} returned {response.status_code}. The page that reads it will '
        'render fine and then show nothing.'
    )


@pytest.mark.parametrize('url', EXPORTS)
def test_the_csv_export_answers(client, url):
    test_client, _ = client
    response = test_client.get(url)
    assert response.status_code == 200, f'{url} returned {response.status_code}'
    assert 'text/csv' in response.headers.get('Content-Type', '')


def test_every_payload_builder_runs(client):
    """
    Call the builders directly, so a branch the empty-database fixture does not
    reach is still executed.

    The watch list bug lived in the return statement, which every call reaches,
    but the credentials it needed were bound inside `if items and with_prices`.
    An empty watch list and a populated one take different paths through it.
    """
    _, app = client
    from app.equity import routes as eq

    with app.test_request_context('/'):
        from flask_login import login_user
        from app.models import User
        login_user(User.query.first())

        for name in ('_build_dashboard_payload', '_build_holdings_payload',
                     '_build_watchlist_payload', '_build_trade_natures_payload'):
            builder = getattr(eq, name)
            try:
                if name == '_build_holdings_payload':
                    payload = builder(None, None)
                else:
                    payload = builder()
            except Exception as exc:
                raise AssertionError(f'{name} raised {type(exc).__name__}: {exc}')
            assert isinstance(payload, dict), f'{name} returned {type(payload)}'


def test_the_endpoint_list_above_is_complete(client):
    """
    A new GET endpoint must be covered, or explicitly excluded with a reason.

    Without this the lists rot: the next endpoint gets added, nobody adds it
    here, and it ships untested exactly like the watch list payload did.
    """
    _, app = client
    covered = set(API_ENDPOINTS) | set(EXPORTS) | NEEDS_PARAMS

    live = {
        str(rule.rule) for rule in app.url_map.iter_rules()
        if 'GET' in rule.methods
        and str(rule.rule).startswith('/equity/api/')
        and '<' not in str(rule.rule)
    }

    uncovered = sorted(live - covered)
    assert not uncovered, (
        'These equity GET endpoints are not exercised by any test. Add them to '
        'API_ENDPOINTS or EXPORTS, or to NEEDS_PARAMS with the reason: '
        + ', '.join(uncovered)
    )
