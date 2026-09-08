"""
The equity fan-out is bounded per OpenAlgo host, not globally.

OpenAlgo's rate limiter is per IP and in memory, and order writes share one
bucket of ten per second on each instance, with no Retry-After to push back
with. Production gives every account its own OpenAlgo instance, so every account
still runs in parallel. That is a property of the deployment, not of the code,
and the moment two accounts are pointed at one instance their writes share a
bucket. Firing them together would spend it on collisions.

These tests pin both halves: different hosts stay concurrent, and accounts
sharing a host are serialised. They also pin that results come back in the
caller's job order however the threads interleave, because the caller pairs
results with its own split rows positionally and a reordering would attach one
account's outcome to another account's row.
"""

import sys
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

import pytest  # noqa: E402


def _engine():
    """Imported lazily: config.py resolves DATABASE_URL at import time."""
    from app.utils import equity_order_engine
    return equity_order_engine


def job(account_id, host):
    return {'credential': {'account_id': account_id, 'host_url': host}}


class Recorder:
    """Counts how many workers are inside the call at once, per host."""

    def __init__(self, hold=0.05):
        self.hold = hold
        self.lock = threading.Lock()
        self.current = 0
        self.peak = 0
        self.peak_by_host = {}
        self.current_by_host = {}
        self.order = []

    def worker(self, app, j):
        host = j['credential']['host_url']
        with self.lock:
            self.current += 1
            self.peak = max(self.peak, self.current)
            self.current_by_host[host] = self.current_by_host.get(host, 0) + 1
            self.peak_by_host[host] = max(
                self.peak_by_host.get(host, 0), self.current_by_host[host]
            )
            self.order.append(j['credential']['account_id'])
        time.sleep(self.hold)
        with self.lock:
            self.current -= 1
            self.current_by_host[host] -= 1
        return {'account_id': j['credential']['account_id']}


def test_accounts_on_different_hosts_run_concurrently():
    """The production shape: five accounts, five OpenAlgo instances."""
    engine = _engine()
    rec = Recorder()
    jobs = [job(i, f'http://127.0.0.1:500{i}') for i in range(1, 6)]

    engine._run_jobs(None, jobs, rec.worker)

    assert rec.peak == 5


def test_accounts_sharing_a_host_are_serialised():
    """The invariant that matters: one bucket, one writer at a time."""
    engine = _engine()
    rec = Recorder()
    jobs = [job(i, 'http://127.0.0.1:5000') for i in range(1, 6)]

    engine._run_jobs(None, jobs, rec.worker)

    assert rec.peak == 1
    assert rec.peak_by_host['http://127.0.0.1:5000'] == 1


def test_mixed_hosts_parallelise_across_but_not_within():
    engine = _engine()
    rec = Recorder()
    jobs = [
        job(1, 'http://a:5000'), job(2, 'http://a:5000'),
        job(3, 'http://b:5000'), job(4, 'http://b:5000'),
    ]

    engine._run_jobs(None, jobs, rec.worker)

    assert rec.peak == 2  # one per host
    assert rec.peak_by_host['http://a:5000'] == 1
    assert rec.peak_by_host['http://b:5000'] == 1


def test_results_keep_the_callers_job_order():
    """The caller pairs results with split rows positionally."""
    engine = _engine()
    rec = Recorder(hold=0.0)
    jobs = [
        job(11, 'http://a:5000'), job(22, 'http://b:5000'),
        job(33, 'http://a:5000'), job(44, 'http://c:5000'),
    ]

    results = engine._run_jobs(None, jobs, rec.worker)

    assert [r['account_id'] for r in results] == [11, 22, 33, 44]


def test_a_single_job_still_runs_inline():
    """One account must not pay for a thread."""
    engine = _engine()
    seen = {}

    def worker(app, j):
        seen['thread'] = threading.current_thread().name
        return {'account_id': j['credential']['account_id']}

    results = engine._run_jobs(None, [job(1, 'http://a:5000')], worker)

    assert seen['thread'] == threading.current_thread().name
    assert results[0]['account_id'] == 1


def test_no_jobs_returns_nothing():
    assert _engine()._run_jobs(None, [], lambda app, j: None) == []


def test_a_crashing_worker_does_not_take_the_others_with_it():
    engine = _engine()

    def worker(app, j):
        if j['credential']['account_id'] == 2:
            raise RuntimeError('worker exploded')
        return {'account_id': j['credential']['account_id']}

    jobs = [job(1, 'http://a:5000'), job(2, 'http://b:5000'), job(3, 'http://c:5000')]
    results = engine._run_jobs(None, jobs, worker)

    assert len(results) == 3
    assert results[0]['account_id'] == 1
    assert results[2]['account_id'] == 3
    # The crashed account is indeterminate, never "failed": a crash mid-call
    # says nothing about whether the order reached the broker, and marking it
    # failed would make it eligible for a retry that could duplicate.
    assert results[1]['indeterminate'] is True
    assert results[1]['error_type'] == 'worker_crash'


def test_a_crash_does_not_stop_the_rest_of_that_hosts_queue():
    """Serialised does not mean fragile: one bad account, not one bad host."""
    engine = _engine()

    def worker(app, j):
        if j['credential']['account_id'] == 1:
            raise RuntimeError('boom')
        return {'account_id': j['credential']['account_id']}

    jobs = [job(1, 'http://a:5000'), job(2, 'http://a:5000')]
    results = engine._run_jobs(None, jobs, worker)

    assert results[0]['indeterminate'] is True
    assert results[1]['account_id'] == 2


@pytest.mark.parametrize('a,b', [
    ('http://a:5000', 'http://a:5000/'),
    ('http://A:5000', 'http://a:5000'),
    ('http://a:5000 ', 'http://a:5000'),
])
def test_host_grouping_ignores_trailing_slash_and_case(a, b):
    """Two spellings of one host are one bucket, so they must not run together."""
    engine = _engine()
    rec = Recorder()

    engine._run_jobs(None, [job(1, a), job(2, b)], rec.worker)

    assert rec.peak == 1


def test_a_missing_host_still_groups_rather_than_crashing():
    engine = _engine()
    rec = Recorder(hold=0.0)

    results = engine._run_jobs(
        None, [{'credential': {'account_id': 1}}, {'credential': {'account_id': 2}}],
        rec.worker
    )

    assert [r['account_id'] for r in results] == [1, 2]
