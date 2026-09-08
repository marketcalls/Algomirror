"""
Analyzer-mode detection for the equity screens.

In analyzer mode OpenAlgo simulates orders and never sends them to a broker.
Discovering that after placing a family-wide order is the worst possible moment,
so the screens carry a badge and this is what fills it.

Two properties matter enough to pin. It is HOST scoped, because analyzer mode is
application-wide per OpenAlgo instance and explicitly not per API key, so two
accounts on one instance must resolve to one answer and one read. And a host
that cannot be read reports "not in analyze mode" rather than a warning, because
a warning invented from a failed read teaches the admin to ignore the badge.
"""

import os
import sys
import tempfile
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-analyze-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'an.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'analyze-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.equity import routes as eq  # noqa: E402


@pytest.fixture(scope='session')
def app():
    application = create_app('development')
    application.config['TESTING'] = True
    return application


@pytest.fixture
def ctx(app):
    with app.app_context():
        with eq._CACHE_LOCK:
            eq._ANALYZE_STATUS.clear()
        yield app
        with eq._CACHE_LOCK:
            eq._ANALYZE_STATUS.clear()


class FakeClient:
    calls = 0

    def __init__(self, response):
        self.response = response

    def analyzerstatus(self, **kwargs):
        FakeClient.calls += 1
        if isinstance(self.response, Exception):
            raise self.response
        return self.response


@pytest.fixture
def patch_client(monkeypatch):
    def install(response):
        FakeClient.calls = 0
        monkeypatch.setattr(eq, 'ExtendedOpenAlgoAPI', lambda **kw: FakeClient(response))
    return install


def creds(*hosts):
    return [{'api_key': 'k', 'host_url': h} for h in hosts]


def test_live_mode_is_not_flagged(ctx, patch_client):
    patch_client({'status': 'success', 'data': {'analyze_mode': False, 'mode': 'live'}})
    assert eq._analyze_mode_for(creds('http://a:5000'))['analyze'] is False


def test_analyze_mode_is_flagged(ctx, patch_client):
    patch_client({'status': 'success', 'data': {'analyze_mode': True, 'mode': 'analyze'}})
    out = eq._analyze_mode_for(creds('http://a:5000'))
    assert out['analyze'] is True
    assert out['hosts'] == ['http://a:5000']


def test_the_mode_string_alone_is_enough(ctx, patch_client):
    """Some responses carry mode without the boolean."""
    patch_client({'status': 'success', 'data': {'mode': 'analyze'}})
    assert eq._analyze_mode_for(creds('http://a:5000'))['analyze'] is True


def test_one_host_in_analyze_flags_the_view(ctx, monkeypatch):
    """A family order is only as live as its least live account."""
    def factory(**kw):
        mode = 'analyze' if 'b' in kw.get('host', '') else 'live'
        return FakeClient({'status': 'success', 'data': {'mode': mode}})
    monkeypatch.setattr(eq, 'ExtendedOpenAlgoAPI', factory)

    out = eq._analyze_mode_for(creds('http://a:5000', 'http://b:5000'))
    assert out['analyze'] is True
    assert out['hosts'] == ['http://b:5000']


def test_a_failed_read_is_not_a_warning(ctx, patch_client):
    """An invented warning trains the admin to ignore the badge."""
    patch_client(RuntimeError('host down'))
    assert eq._analyze_mode_for(creds('http://a:5000'))['analyze'] is False


def test_an_error_response_is_not_a_warning(ctx, patch_client):
    patch_client({'status': 'error', 'message': 'nope'})
    assert eq._analyze_mode_for(creds('http://a:5000'))['analyze'] is False


def test_the_answer_is_cached_per_host(ctx, patch_client):
    """It changes rarely and every screen refresh would otherwise ask."""
    patch_client({'status': 'success', 'data': {'mode': 'live'}})
    eq._analyze_mode_for(creds('http://a:5000'))
    eq._analyze_mode_for(creds('http://a:5000'))
    assert FakeClient.calls == 1


def test_two_accounts_on_one_host_cost_one_read(ctx, patch_client):
    """Analyzer mode is per instance, so one instance is one question."""
    patch_client({'status': 'success', 'data': {'mode': 'live'}})
    eq._analyze_mode_for(creds('http://a:5000', 'http://a:5000/'))
    assert FakeClient.calls == 1


def test_the_cache_expires(ctx, patch_client, monkeypatch):
    patch_client({'status': 'success', 'data': {'mode': 'live'}})
    eq._analyze_mode_for(creds('http://a:5000'))
    assert FakeClient.calls == 1

    real = time.time
    monkeypatch.setattr(eq.time, 'time',
                        lambda: real() + eq.ANALYZE_STATUS_TTL_SECONDS + 1)
    eq._analyze_mode_for(creds('http://a:5000'))
    assert FakeClient.calls == 2


@pytest.mark.parametrize('a,b', [
    ('http://a:5000', 'http://a:5000/'),
    ('http://A:5000', 'http://a:5000'),
    ('  http://a:5000  ', 'http://a:5000'),
])
def test_host_keys_normalise(a, b):
    assert eq._analyze_host_key(a) == eq._analyze_host_key(b)


def test_no_credentials_is_not_analyze(ctx):
    assert eq._analyze_mode_for([])['analyze'] is False
    assert eq._analyze_mode_for(None)['analyze'] is False
