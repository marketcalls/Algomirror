"""
Regression tests for the equity security review.

Three findings, all confirmed by tracing the code rather than by pattern
matching, and all fixed here. These tests exist so the fixes cannot quietly
come undone.

  1. Denial of service through SSE thread exhaustion. Gunicorn runs one worker
     with 16 threads and an SSE generator holds its thread for the life of the
     connection, so 16 open equity tabs stopped the whole application serving
     anything, F&O and login included. Worse, it deadlocked itself: a change
     fires, the browser fetches its payload, and no thread is left to answer.

  2. Cross-site scripting through showToast. It built its markup with innerHTML
     and interpolated the message, and six equity call sites pass server text
     that includes broker responses and exception strings, which originate
     outside this application.

  3. Double-booked fills. The stream books a fill from a running total with no
     broker trade id; the reconciler books the same execution with the broker's
     real trade id. The unique index is on (split_id, broker_trade_id) and NULL
     never collides, so both rows survived and the Trade Book counted every
     filled share twice. This fired on every reconnect and every manual check,
     not in some edge case.
"""

import os
import re
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-equity-sec-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'sec.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'equity-security-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    EquityOrder, EquityOrderSplit, EquityTrade, TradingAccount, User,
    EQUITY_SIDE_BUY, EQUITY_ORDER_TYPE_MARKET, EQUITY_SPLIT_STATUS_PENDING,
)
from app.utils import equity_events as events  # noqa: E402
from app.utils.equity_order_stream import (  # noqa: E402
    EquityOrderStream, PROVISIONAL_TRADE_PREFIX,
)

BASE_HTML = REPO_ROOT / 'app' / 'templates' / 'base.html'
COMMON_JS = REPO_ROOT / 'app' / 'static' / 'js' / 'equity_common.js'


# ------------------------------------------------- 1. SSE thread exhaustion

class TestStreamConcurrencyCap:
    """
    An SSE generator holds a gunicorn thread for the life of the connection, so
    the number of them has to be bounded below the thread count. Without this
    the application stops serving every route, not just the equity ones.
    """

    def teardown_method(self):
        # Never leave a slot held: a leak here would break every later test.
        while events.active_streams():
            slot = events.StreamSlot()
            slot.acquired = True
            slot.__exit__(None, None, None)

    def test_the_cap_is_below_the_thread_count(self):
        """16 threads in production. A cap at or above that protects nothing."""
        assert events.MAX_CONCURRENT_STREAMS < 16
        assert events.MAX_CONCURRENT_STREAMS >= 2

    def test_slots_are_handed_out_up_to_the_cap(self):
        held = []
        try:
            for _ in range(events.MAX_CONCURRENT_STREAMS):
                slot = events.StreamSlot()
                slot.__enter__()
                held.append(slot)
            assert all(s.acquired for s in held)
            assert events.active_streams() == events.MAX_CONCURRENT_STREAMS
        finally:
            for s in held:
                s.__exit__(None, None, None)

    def test_the_next_connection_is_refused_not_queued(self):
        """
        Queueing would hold the very thread the cap exists to protect, so a
        refusal has to be immediate.
        """
        held = []
        try:
            for _ in range(events.MAX_CONCURRENT_STREAMS):
                slot = events.StreamSlot()
                slot.__enter__()
                held.append(slot)

            started = time.monotonic()
            with events.StreamSlot() as extra:
                elapsed = time.monotonic() - started
                assert extra.acquired is False
            assert elapsed < 0.5, 'refusal blocked instead of returning'
        finally:
            for s in held:
                s.__exit__(None, None, None)

    def test_a_slot_is_returned_when_the_generator_raises(self):
        """A client disconnect surfaces as an exception inside the generator."""
        before = events.active_streams()
        with pytest.raises(RuntimeError):
            with events.StreamSlot() as slot:
                assert slot.acquired
                raise RuntimeError('client went away')
        assert events.active_streams() == before

    def test_a_released_slot_is_reusable(self):
        with events.StreamSlot() as first:
            assert first.acquired
        with events.StreamSlot() as second:
            assert second.acquired

    def test_double_release_does_not_corrupt_the_count(self):
        slot = events.StreamSlot()
        slot.__enter__()
        slot.__exit__(None, None, None)
        slot.__exit__(None, None, None)
        assert events.active_streams() == 0

    def test_the_route_refuses_rather_than_holding_the_thread(self):
        """The generator must yield a busy frame and return, not block."""
        source = (REPO_ROOT / 'app' / 'equity' / 'routes.py').read_text(encoding='utf-8')
        assert 'StreamSlot()' in source
        assert 'event: busy' in source


# --------------------------------------------------------------- 2. XSS

class TestToastIsNotAnHtmlSink:
    """
    showToast is in base.html and is called from six equity screens with
    server text. That text includes broker responses and str(exception), which
    originate outside this application, so it must never be parsed as markup.
    """

    def test_the_message_is_not_interpolated_into_markup(self):
        html = BASE_HTML.read_text(encoding='utf-8')
        body = html[html.find('function showToast'):]
        body = body[:body.find('\n        }')]
        # The comment explains what was removed and why, which is not a call.
        code = re.sub(r'//[^\n]*', '', body)

        assert '${message}' not in code, 'the message is interpolated into markup'
        assert 'innerHTML' not in code, 'showToast still builds markup with innerHTML'

    def test_the_message_is_set_as_text(self):
        html = BASE_HTML.read_text(encoding='utf-8')
        body = html[html.find('function showToast'):]
        body = body[:body.find('\n        }')]
        assert 'textContent' in body

    def test_no_equity_template_or_script_uses_an_html_sink(self):
        """The equity screens build DOM with createElement and textContent."""
        targets = list((REPO_ROOT / 'app' / 'templates' / 'equity').glob('*.html'))
        targets += list((REPO_ROOT / 'app' / 'static' / 'js').glob('equity_*.js'))
        for path in targets:
            text = path.read_text(encoding='utf-8')
            for sink in ('innerHTML', 'insertAdjacentHTML', 'outerHTML',
                         'document.write', 'eval(', 'new Function'):
                assert sink not in text, f'{path.name} uses {sink}'

    def test_no_equity_template_disables_jinja_escaping(self):
        for path in (REPO_ROOT / 'app' / 'templates' / 'equity').glob('*.html'):
            text = path.read_text(encoding='utf-8')
            assert '|safe' not in text, f'{path.name} bypasses autoescaping'
            assert 'autoescape off' not in text, f'{path.name} disables autoescaping'


# --------------------------------------------------- 3. double-booked fills

@pytest.fixture(scope='session')
def app():
    application = create_app('development')
    application.config['TESTING'] = True
    return application


@pytest.fixture
def ctx(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        yield app
        db.session.remove()
        db.drop_all()


@pytest.fixture
def world(ctx):
    user = User(username='sec', email='sec@example.com', is_admin=True)
    user.set_password('Sec#Test1')
    db.session.add(user)
    db.session.commit()

    account = TradingAccount(
        user_id=user.id, account_name='mps', broker_name='zerodha',
        host_url='http://127.0.0.1:5001', websocket_url='ws://127.0.0.1:8766',
        is_active=True,
    )
    account.set_api_key('key')
    db.session.add(account)
    db.session.flush()

    order = EquityOrder(
        user_id=user.id, symbol='RELIANCE', exchange='NSE', side=EQUITY_SIDE_BUY,
        order_type=EQUITY_ORDER_TYPE_MARKET, total_quantity=10, price=100.0,
    )
    db.session.add(order)
    db.session.flush()

    split = EquityOrderSplit(
        equity_order_id=order.id, account_id=account.id, quantity=10,
        broker_order_id='OID-1', fill_status=EQUITY_SPLIT_STATUS_PENDING,
    )
    db.session.add(split)
    db.session.commit()
    return {'account': account, 'order': order, 'split': split}


@pytest.fixture
def stream(ctx):
    s = EquityOrderStream()
    s._app = ctx
    for key in s._stats:
        s._stats[key] = 0
    return s


def push(stream, world, filled, status='open', price=100.0):
    stream._apply(world['account'].id, {
        'orderid': 'OID-1', 'symbol': 'RELIANCE', 'exchange': 'NSE',
        'action': 'BUY', 'quantity': 10, 'order_status': status,
        'filled_quantity': filled, 'average_price': price,
    })
    db.session.commit()


def trades(split):
    return EquityTrade.query.filter_by(split_id=split.id).all()


class TestFillsAreNotDoubleBooked:

    def test_a_stream_fill_is_marked_provisional(self, stream, world):
        """
        A NULL trade id would not de-duplicate: both databases allow repeated
        NULLs in a unique index, so the rows would pile up silently.
        """
        push(stream, world, filled=10)
        rows = trades(world['split'])
        assert len(rows) == 1
        assert rows[0].broker_trade_id.startswith(PROVISIONAL_TRADE_PREFIX)

    def test_the_reconciler_replaces_provisional_rows(self, stream, world):
        """
        The regression: the same execution booked twice, once from the running
        total and once from the broker's record.
        """
        push(stream, world, filled=10, status='complete')
        assert len(trades(world['split'])) == 1

        from app.utils.equity_fill_poller import equity_fill_poller
        tick = {'fills_booked': 0}
        equity_fill_poller._book_fills(
            world['split'],
            [{'tradeid': 'T1', 'quantity': 10, 'average_price': 100.0,
              'exchange': 'NSE'}],
            tick,
        )
        db.session.commit()

        rows = trades(world['split'])
        assert len(rows) == 1, 'the execution was booked twice'
        assert rows[0].broker_trade_id == 'T1', 'the broker record must win'
        assert sum(r.executed_quantity for r in rows) == 10

    def test_a_repeated_stream_event_books_one_row(self, stream, world):
        """The synthetic id has to be deterministic or the index cannot help."""
        push(stream, world, filled=10)
        push(stream, world, filled=10)
        push(stream, world, filled=10)
        assert len(trades(world['split'])) == 1

    def test_partial_then_complete_books_each_increment_once(self, stream, world):
        push(stream, world, filled=4)
        push(stream, world, filled=10, status='complete')

        rows = trades(world['split'])
        assert sorted(r.executed_quantity for r in rows) == [4, 6]
        assert sum(r.executed_quantity for r in rows) == 10

    def test_the_broker_record_survives_a_second_reconcile(self, stream, world):
        """Reconciling twice must not delete and re-add, nor duplicate."""
        from app.utils.equity_fill_poller import equity_fill_poller
        rows_in = [{'tradeid': 'T1', 'quantity': 10, 'average_price': 100.0}]
        tick = {'fills_booked': 0}

        equity_fill_poller._book_fills(world['split'], rows_in, tick)
        db.session.commit()
        equity_fill_poller._book_fills(world['split'], rows_in, tick)
        db.session.commit()

        rows = trades(world['split'])
        assert len(rows) == 1
        assert rows[0].broker_trade_id == 'T1'
