"""
The three Phase 0 guards, each of which exists to stop a duplicate order.

1. ProductionConfig refuses to boot on SQLite. Every exit claim in the equity
   module and every risk check in F&O guards against double execution with
   SELECT ... FOR UPDATE. SQLAlchemy emits nothing for that on SQLite, so the
   lock silently becomes an unlocked read and two workers can both sell the
   same holding. Production runs PostgreSQL today; this makes that a
   requirement rather than a happy accident.

2. The background service lock lets exactly one process own the monitors.
   They are started unconditionally in create_app(), so a second gunicorn
   worker would otherwise get its own risk manager, its own pollers and its
   own exit monitors.

3. Subscription tracking de-duplicates. The tracked list is replayed on
   reconnect and grew without bound when two callers subscribed the same
   symbol, replaying duplicates every time.
"""

import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


# ---------------------------------------------------------------- config guard

class _FakeApp:
    """Minimal stand-in: the guard only ever reads app.config."""

    def __init__(self, uri):
        self.config = {'SQLALCHEMY_DATABASE_URI': uri}


def test_production_refuses_sqlite():
    from config import ProductionConfig

    with pytest.raises(RuntimeError) as excinfo:
        ProductionConfig.init_app(_FakeApp('sqlite:///instance/algomirror.db'))

    message = str(excinfo.value)
    assert 'SQLite' in message
    # The message has to say what breaks, not just that it refused.
    assert 'duplicate' in message.lower()


def test_production_refuses_absolute_sqlite_path():
    from config import ProductionConfig

    with pytest.raises(RuntimeError):
        ProductionConfig.init_app(_FakeApp('sqlite:////var/python/algomirror/a.db'))


@pytest.mark.parametrize('uri', [
    'postgresql+psycopg://user:pw@localhost:5432/algomirror',
    'postgresql://user:pw@localhost:5432/algomirror',
])
def test_production_accepts_postgres(uri):
    from config import ProductionConfig

    ProductionConfig.init_app(_FakeApp(uri))


def test_development_still_allows_sqlite():
    """Developers must keep working on SQLite; only production is pinned."""
    from config import DevelopmentConfig

    DevelopmentConfig.init_app(_FakeApp('sqlite:///instance/algomirror.db'))


def test_missing_uri_does_not_crash_the_guard():
    from config import ProductionConfig

    ProductionConfig.init_app(_FakeApp(None))


# ------------------------------------------------------------- service lock

@pytest.fixture
def clean_lock(tmp_path):
    """Reset the module globals so each test starts unowned."""
    from app.utils import service_lock

    service_lock._pg_connection = None
    service_lock._pid_file_path = None
    yield service_lock
    service_lock.release()


class _LockApp:
    def __init__(self, instance_path, uri='sqlite:///x.db'):
        self.instance_path = str(instance_path)
        self.config = {'SQLALCHEMY_DATABASE_URI': uri}
        self.logger = _NullLogger()


class _NullLogger:
    def info(self, *a, **k):
        pass

    def warning(self, *a, **k):
        pass

    def error(self, *a, **k):
        pass


def test_first_process_claims_the_lock(clean_lock, tmp_path):
    app = _LockApp(tmp_path)
    assert clean_lock.acquire(app, db=None) is True
    assert (tmp_path / 'background_services.pid').exists()


def test_second_live_process_is_declined(clean_lock, tmp_path):
    """A pid file naming a different live process means someone else owns them.

    The stand-in has to be a real, live, *other* pid: the lock deliberately
    treats its own pid as re-entry rather than contention, so os.getpid() would
    test the wrong branch.
    """
    import subprocess

    other = subprocess.Popen(
        [sys.executable, '-c', 'import time; time.sleep(30)'],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        pid_file = tmp_path / 'background_services.pid'
        pid_file.parent.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(other.pid))

        app = _LockApp(tmp_path)
        assert clean_lock.acquire(app, db=None) is False
        # The other worker's claim must survive our refusal.
        assert pid_file.read_text().strip() == str(other.pid)
    finally:
        other.kill()
        other.wait(timeout=10)


def test_reacquire_by_the_same_process_is_allowed(clean_lock, tmp_path):
    """Re-entry is not contention: a restart that reuses our pid must not deadlock."""
    pid_file = tmp_path / 'background_services.pid'
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text(str(os.getpid()))

    app = _LockApp(tmp_path)
    assert clean_lock.acquire(app, db=None) is True


def test_stale_pid_file_is_taken_over(clean_lock, tmp_path):
    """A crashed worker must not strand the monitors for ever."""
    pid_file = tmp_path / 'background_services.pid'
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text('999999999')  # not a live pid

    app = _LockApp(tmp_path)
    assert clean_lock.acquire(app, db=None) is True
    assert pid_file.read_text().strip() == str(os.getpid())


def test_unreadable_pid_file_is_treated_as_stale(clean_lock, tmp_path):
    pid_file = tmp_path / 'background_services.pid'
    pid_file.parent.mkdir(parents=True, exist_ok=True)
    pid_file.write_text('not-a-pid')

    app = _LockApp(tmp_path)
    assert clean_lock.acquire(app, db=None) is True


def test_lock_failure_never_raises(clean_lock, tmp_path):
    """A broken lock must cost the monitors, never the whole application."""
    app = _LockApp(tmp_path, uri='postgresql://nope')

    class _ExplodingDb:
        @property
        def engine(self):
            raise RuntimeError('no database here')

    assert clean_lock.acquire(app, db=_ExplodingDb()) is False


def test_the_postgres_path_works_without_an_ambient_app_context(clean_lock, tmp_path):
    """
    The regression this test exists for, found in production.

    create_app() calls acquire() OUTSIDE any app context. Flask-SQLAlchemy
    resolves db.engine through the app context, so reading it there raised
    "Working outside of application context", the guard's own except reported
    that the lock could not be evaluated, and EVERY background service stayed
    down: the risk manager, the pollers, the option chain feed and the equity
    stop loss monitor.

    The earlier tests all took the PID-file path with db=None, so none of them
    ever touched the branch that actually runs in production.
    """
    import flask

    class ContextRequiringDb:
        """Behaves like Flask-SQLAlchemy: .engine needs an app context."""

        @property
        def engine(self):
            if not flask.has_app_context():
                raise RuntimeError('Working outside of application context.')
            return _FakeEngine()

    class _FakeEngine:
        def connect(self):
            return _FakeConnection()

    class _FakeConnection:
        closed = False

        def execute(self, statement, params=None):
            return _FakeResult()

        def close(self):
            self.closed = True

    class _FakeResult:
        @staticmethod
        def scalar():
            return True

    app = flask.Flask(__name__)
    app.config['SQLALCHEMY_DATABASE_URI'] = 'postgresql+psycopg://u:p@localhost/db'
    app.logger.disabled = True

    # Deliberately NOT inside `with app.app_context():`, exactly as create_app calls it.
    assert flask.has_app_context() is False
    assert clean_lock.acquire(app, db=ContextRequiringDb()) is True


def test_release_is_idempotent(clean_lock, tmp_path):
    app = _LockApp(tmp_path)
    assert clean_lock.acquire(app, db=None) is True
    clean_lock.release()
    clean_lock.release()
    assert not (tmp_path / 'background_services.pid').exists()


# ------------------------------------------------------- subscription tracking

@pytest.fixture
def manager():
    from app.utils.websocket_manager import ProfessionalWebSocketManager
    return ProfessionalWebSocketManager()


def test_tracking_records_new_instruments(manager):
    added = manager._track_subscriptions('ltp', [
        {'symbol': 'RELIANCE', 'exchange': 'NSE'},
        {'symbol': 'INFY', 'exchange': 'NSE'},
    ])
    assert len(added) == 2
    assert len(manager.subscriptions['ltp']) == 2


def test_tracking_ignores_a_repeat(manager):
    """Two callers wanting the same symbol must not store or replay it twice."""
    manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])
    added = manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])

    assert added == []
    assert len(manager.subscriptions['ltp']) == 1


def test_tracking_deduplicates_within_one_call(manager):
    added = manager._track_subscriptions('ltp', [
        {'symbol': 'RELIANCE', 'exchange': 'NSE'},
        {'symbol': 'RELIANCE', 'exchange': 'NSE'},
    ])
    assert len(added) == 1


def test_same_symbol_on_two_exchanges_is_not_a_duplicate(manager):
    """NSE:RELIANCE and BSE:RELIANCE are different instruments."""
    manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])
    added = manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'BSE'}])

    assert len(added) == 1
    assert len(manager.subscriptions['ltp']) == 2


def test_modes_are_tracked_separately(manager):
    """An LTP subscription does not satisfy a depth subscriber."""
    manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])
    added = manager._track_subscriptions('depth', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])

    assert len(added) == 1
    assert len(manager.subscriptions['ltp']) == 1
    assert len(manager.subscriptions['depth']) == 1


def test_repeated_tracking_does_not_grow_without_bound(manager):
    """The regression this guards: the list grew for the life of the process."""
    for _ in range(500):
        manager._track_subscriptions('ltp', [{'symbol': 'RELIANCE', 'exchange': 'NSE'}])

    assert len(manager.subscriptions['ltp']) == 1
