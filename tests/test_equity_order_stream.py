"""
Tests for the event driven order stream (app/utils/equity_order_stream.py).

This replaced a ten second reconciliation timer. OpenAlgo pushes order updates,
so nothing here asks repeatedly for something the broker will tell us, and the
worker BLOCKS on a queue rather than waking on an interval.

The properties worth pinning are the ones a push stream makes easy to get wrong.
filled_quantity arrives cumulative rather than as a delta, so booking it naively
doubles a position on the second event for the same order. An order id we do not
recognise is either a GTT of ours that just fired under a fresh id or somebody
else's trade, and confusing those two either loses a fill or invents one. And
the reader thread must never see an exception, because that takes the whole
stream down.
"""

import os
import sys
import tempfile
import threading
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-order-stream-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'os.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'order-stream-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    EquityExternalTrade, EquityOrder, EquityOrderSplit, EquityTrade,
    TradingAccount, User,
    EQUITY_SIDE_BUY, EQUITY_ORDER_TYPE_MARKET, EQUITY_ORDER_TYPE_GTT,
    EQUITY_SPLIT_STATUS_CANCELLED, EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_PARTIAL, EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
)
from app.utils.equity_order_stream import EquityOrderStream  # noqa: E402

ORDER_ID = 'OID-1'


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
    user = User(username='admin', email='a@example.com', is_admin=True)
    user.set_password('Stream#Test1')
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
        broker_order_id=ORDER_ID, fill_status=EQUITY_SPLIT_STATUS_PENDING,
    )
    db.session.add(split)
    db.session.commit()
    return {'user': user, 'account': account, 'order': order, 'split': split}


@pytest.fixture
def stream(ctx):
    s = EquityOrderStream()
    s._app = ctx
    for key in s._stats:
        s._stats[key] = 0
    return s


def event(**kw):
    base = {
        'type': 'order_update', 'orderid': ORDER_ID, 'symbol': 'RELIANCE',
        'exchange': 'NSE', 'action': 'BUY', 'quantity': 10,
        'pricetype': 'MARKET', 'product': 'CNC', 'order_status': 'open',
        'filled_quantity': 0, 'pending_quantity': 10, 'average_price': 0,
    }
    base.update(kw)
    return base


def apply(stream, world, **kw):
    stream._apply(world['account'].id, event(**kw))
    db.session.commit()
    db.session.refresh(world['split'])
    return world['split']


# --------------------------------------------------------------- no polling

def test_the_module_defines_no_poll_interval():
    """The whole point: nothing here wakes on a clock."""
    import app.utils.equity_order_stream as mod
    source = Path(mod.__file__).read_text(encoding='utf-8')
    assert 'add_job' not in source
    assert 'trigger=' not in source
    # The only wait is the queue timeout that keeps stop() responsive.
    assert mod.QUEUE_WAIT_SECONDS <= 5


def test_the_worker_blocks_rather_than_spinning(stream):
    """An idle day must cost nothing."""
    stream._running = True
    worker = threading.Thread(target=stream._drain, daemon=True)
    worker.start()
    try:
        time.sleep(0.3)
        assert stream._stats['events'] == 0
    finally:
        stream._running = False
        worker.join(timeout=5)


# ------------------------------------------------------------ status moves

@pytest.mark.parametrize('status', ['open', 'trigger pending'])
def test_a_working_order_stays_open(stream, world, status):
    split = apply(stream, world, order_status=status)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert split.broker_order_status == status


def test_the_never_emitted_pending_is_not_interpreted(stream, world):
    """The published table says 'pending'. OpenAlgo never sends it."""
    split = apply(stream, world, order_status='pending')
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_complete_settles_the_split(stream, world):
    split = apply(stream, world, order_status='complete',
                  filled_quantity=10, average_price=101.0)
    assert split.fill_status == EQUITY_SPLIT_STATUS_COMPLETED
    assert split.filled_quantity == 10
    assert split.avg_fill_price == 101.0


def test_rejected_records_the_reason(stream, world):
    split = apply(stream, world, order_status='rejected',
                  rejection_reason='Insufficient funds')
    assert split.fill_status == EQUITY_SPLIT_STATUS_REJECTED
    assert split.error_message == 'Insufficient funds'


def test_cancelled_settles(stream, world):
    split = apply(stream, world, order_status='cancelled')
    assert split.fill_status == EQUITY_SPLIT_STATUS_CANCELLED


def test_a_partial_fill_reads_as_partial(stream, world):
    split = apply(stream, world, order_status='open',
                  filled_quantity=4, average_price=100.0)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PARTIAL
    assert split.filled_quantity == 4


# ------------------------------------------------- cumulative, not a delta

def test_a_repeated_quantity_is_not_booked_twice(stream, world):
    """
    filled_quantity is cumulative on every update. Booking it as a delta each
    time is how one fill becomes two and a position doubles on screen.
    """
    apply(stream, world, order_status='open', filled_quantity=4, average_price=100.0)
    apply(stream, world, order_status='open', filled_quantity=4, average_price=100.0)

    trades = EquityTrade.query.filter_by(split_id=world['split'].id).all()
    assert len(trades) == 1
    assert sum(t.executed_quantity for t in trades) == 4


def test_only_the_increase_is_booked(stream, world):
    apply(stream, world, order_status='open', filled_quantity=4, average_price=100.0)
    apply(stream, world, order_status='complete', filled_quantity=10, average_price=101.0)

    trades = EquityTrade.query.filter_by(split_id=world['split'].id).all()
    assert sorted(t.executed_quantity for t in trades) == [4, 6]
    db.session.refresh(world['split'])
    assert world['split'].filled_quantity == 10


def test_a_lower_quantity_never_reduces_the_fill(stream, world):
    """An out of order event must not un-fill an order."""
    apply(stream, world, order_status='open', filled_quantity=10, average_price=100.0)
    split = apply(stream, world, order_status='open', filled_quantity=4, average_price=100.0)
    assert split.filled_quantity == 10


# ------------------------------------------------------------- GTT firing

def test_a_fired_gtt_claims_its_child_order(ctx, stream):
    """The broker gives the released order a fresh id and no link back."""
    user = User(username='g', email='g@example.com', is_admin=True)
    user.set_password('Gtt#Test1')
    db.session.add(user)
    db.session.commit()

    account = TradingAccount(
        user_id=user.id, account_name='a', broker_name='zerodha',
        host_url='http://h', websocket_url='ws://w', is_active=True)
    account.set_api_key('k')
    db.session.add(account)
    db.session.flush()

    order = EquityOrder(
        user_id=user.id, symbol='INFY', exchange='NSE', side=EQUITY_SIDE_BUY,
        order_type=EQUITY_ORDER_TYPE_GTT, total_quantity=5, price=1400.0,
        trigger_price=1450.0)
    db.session.add(order)
    db.session.flush()

    split = EquityOrderSplit(
        equity_order_id=order.id, account_id=account.id, quantity=5,
        broker_gtt_id='TRIG-1', fill_status=EQUITY_SPLIT_STATUS_PENDING)
    db.session.add(split)
    db.session.commit()

    stream._apply(account.id, {
        'orderid': 'FRESH-OID', 'symbol': 'INFY', 'action': 'BUY',
        'quantity': 5, 'order_status': 'open', 'filled_quantity': 0,
    })
    db.session.commit()
    db.session.refresh(split)

    assert split.broker_order_id == 'FRESH-OID'
    assert split.gtt_status == 'triggered'
    assert split.gtt_triggered_at is not None
    assert EquityExternalTrade.query.count() == 0


def test_an_unknown_order_is_recorded_as_external(stream, world):
    """Not ours and matching no resting GTT: somebody else placed it."""
    stream._apply(world['account'].id, event(
        orderid='NOT-OURS', symbol='TCS', action='SELL', quantity=3))
    db.session.commit()

    rows = EquityExternalTrade.query.all()
    assert len(rows) == 1
    assert rows[0].broker_order_id == 'NOT-OURS'
    assert rows[0].symbol == 'TCS'


def test_an_external_order_is_recorded_once(stream, world):
    for _ in range(3):
        stream._apply(world['account'].id, event(orderid='NOT-OURS', symbol='TCS'))
        db.session.commit()
    assert EquityExternalTrade.query.count() == 1


# ---------------------------------------------------------- failure modes

def test_a_malformed_event_does_not_raise(stream, world):
    """This runs on the reader thread; an exception kills the stream."""
    for bad in (None, 'nonsense', {}, {'orderid': ''}, {'symbol': 'X'}):
        stream._apply(world['account'].id, bad)


def test_the_callback_never_raises_on_a_full_queue(stream, world):
    """Backpressure must not propagate into the SDK reader thread."""
    import queue as _queue
    stream._queue = _queue.Queue(maxsize=1)
    stream._on_event(world['account'].id, event())
    stream._on_event(world['account'].id, event())
    stream._on_event(world['account'].id, event())
    assert stream._stats['dropped'] >= 1


def test_status_reports_the_stream(stream, world):
    apply(stream, world, order_status='open')
    report = stream.status()
    assert report['stats']['events'] >= 1
    assert 'queue_depth' in report
    assert report['last_event_at'] is not None
