"""Single-instance guard for the background services.

Every background service (risk manager, order status poller, supertrend exit
service, equity exit monitor, option chain feed) is started unconditionally
inside create_app(). That is safe on exactly one worker and wrong on more than
one: each worker gets its own copy of every monitor, so a stop-loss breach is
seen twice and can be acted on twice.

Production pins ``-w 1``, so this is defence in depth rather than a live fix.
It exists so that containerising the app, or someone raising the worker count,
degrades to "only one worker runs the monitors" instead of "every worker places
duplicate exit orders".

Two backends, picked from the database URI:

* PostgreSQL: a session-level advisory lock. The lock lives on one connection
  held for the lifetime of the process, and PostgreSQL releases it automatically
  if the process dies, so a crashed worker never strands the lock.
* Anything else (SQLite in development): a PID file. Weaker, but it covers the
  same accident on a developer machine.

Neither backend is allowed to stop the app from booting. A worker that cannot
take the lock still serves requests, it just does not run the monitors.
"""

import os
from pathlib import Path

# Arbitrary but fixed. Any other application using advisory locks on the same
# PostgreSQL database must not reuse this number.
_ADVISORY_LOCK_KEY = 8531977412006001

# Held for the process lifetime. Module-global so the connection is never
# garbage collected, which would return it to the pool and drop the lock.
_pg_connection = None
_pid_file_path = None


def _acquire_postgres(app, db):
    """
    Take a session-level advisory lock on a dedicated connection.

    The app context is mandatory, not defensive. Flask-SQLAlchemy resolves
    db.engine through the application context, and create_app() calls acquire()
    outside one, so reading db.engine here raised "Working outside of
    application context". The caller's except then did exactly what it was
    written to do, reported that the lock could not be evaluated and returned
    False, and every background service stayed down: the risk manager, the
    pollers, the option chain feed and the equity stop loss monitor.

    That is the failure mode of a fail-safe default when the thing it is
    guarding against is a bug in the guard itself. The context is opened here so
    the engine resolves.

    The Connection outlives the context on purpose. Only resolving the engine
    needs the app context; the connection object does not, and it has to stay
    open because the advisory lock lives on that session.
    """
    global _pg_connection
    from sqlalchemy import text

    with app.app_context():
        connection = db.engine.connect()
        try:
            acquired = connection.execute(
                text('SELECT pg_try_advisory_lock(:key)'), {'key': _ADVISORY_LOCK_KEY}
            ).scalar()
        except Exception:
            connection.close()
            raise

    if not acquired:
        connection.close()
        return False

    # Keep the connection open. The lock is bound to this session and is
    # released by PostgreSQL when the connection closes or the process exits.
    _pg_connection = connection
    return True


def _acquire_pid_file(app):
    """Fall back to a PID file for non-PostgreSQL backends."""
    global _pid_file_path

    instance_dir = Path(app.instance_path)
    instance_dir.mkdir(parents=True, exist_ok=True)
    path = instance_dir / 'background_services.pid'

    if path.exists():
        try:
            existing = int(path.read_text().strip())
        except (ValueError, OSError):
            existing = None

        if existing is not None and existing != os.getpid() and _pid_is_running(existing):
            return False
        # Stale file: the recorded process is gone, so take it over.
        try:
            path.unlink()
        except OSError:
            return False

    try:
        # O_EXCL makes the create-and-claim atomic between racing workers.
        fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        return False
    except OSError:
        return False

    with os.fdopen(fd, 'w') as handle:
        handle.write(str(os.getpid()))
    _pid_file_path = path
    return True


def _pid_is_running(pid):
    """True if a process with this pid exists. Conservative: unknown means yes."""
    if os.name == 'nt':
        import ctypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = ctypes.windll.kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False
        ctypes.windll.kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return True
    return True


def acquire(app, db):
    """Claim the right to run background services in this process.

    Returns True for the one process that should run them. Never raises: a
    failure to evaluate the lock is reported as "not the owner" so that at worst
    the monitors do not start, rather than the application failing to boot.
    """
    uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''

    try:
        if uri.startswith('postgresql'):
            owner = _acquire_postgres(app, db)
            backend = 'postgres-advisory-lock'
        else:
            owner = _acquire_pid_file(app)
            backend = 'pid-file'
    except Exception as exc:
        app.logger.error(
            'Background service lock could not be evaluated, monitors will not '
            'start in this process: %s',
            exc,
            exc_info=True,
            extra={'event': 'service_lock_error'},
        )
        return False

    if owner:
        app.logger.info(
            'Background services claimed by pid %s (%s)',
            os.getpid(),
            backend,
            extra={'event': 'service_lock_acquired'},
        )
    else:
        app.logger.warning(
            'Background services already owned by another process, pid %s will '
            'serve requests only (%s)',
            os.getpid(),
            backend,
            extra={'event': 'service_lock_declined'},
        )
    return owner


def release():
    """Best-effort release. Normal shutdown relies on process exit instead."""
    global _pg_connection, _pid_file_path

    if _pg_connection is not None:
        try:
            _pg_connection.close()
        except Exception:
            pass
        _pg_connection = None

    if _pid_file_path is not None:
        try:
            _pid_file_path.unlink()
        except OSError:
            pass
        _pid_file_path = None
