"""
Tests for the GTT lifecycle reconciler (app/utils/equity_gtt_reconciler.py).

This module decides that a resting trigger is finished, and in one case that a
trigger fired and produced a real order. Both answers move money-bearing state,
so the tests below are mostly about the module refusing to act rather than
acting: a GTT book that could not be read must settle nothing, a fired trigger
whose child order cannot be identified must stay open, and two equally good
candidate orders must produce no answer at all rather than the wrong one.

No broker is ever contacted. Every read goes through the module's one seam,
client_factory, and every test passes a fake.
"""

import os
import sys
import tempfile
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-equity-gtt-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'gtt.sqlite').replace('\\', '/')
os.environ['SECRET_KEY'] = 'equity-gtt-test-key-not-for-production'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    EquityOrder,
    EquityOrderSplit,
    TradingAccount,
    User,
    EQUITY_SIDE_BUY,
    EQUITY_ORDER_TYPE_GTT,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_REJECTED,
)
from app.utils import equity_gtt_reconciler as mod  # noqa: E402
from app.utils.equity_gtt_reconciler import EquityGttReconciler  # noqa: E402


TRIGGER_ID = '23132604291205'


class FakeBroker:
    """Serves a canned GTT book and order book, and counts the reads."""

    def __init__(self, gtt_rows=None, order_rows=None, gtt_response=None):
        self.gtt_rows = gtt_rows if gtt_rows is not None else []
        self.order_rows = order_rows if order_rows is not None else []
        self.gtt_response = gtt_response
        self.gtt_calls = []
        self.orderbook_calls = 0

    def factory(self, credential):
        return self

    def gttorderbook(self, status=None, **kwargs):
        self.gtt_calls.append(status)
        if self.gtt_response is not None:
            return self.gtt_response
        return {'status': 'success', 'data': list(self.gtt_rows)}

    def orderbook(self):
        self.orderbook_calls += 1
        return {'status': 'success', 'data': {'orders': list(self.order_rows)}}


def gtt_row(status, trigger_id=TRIGGER_ID, updated_at=''):
    return {
        'trigger_id': trigger_id,
        'trigger_type': 'single',
        'status': status,
        'symbol': 'RELIANCE',
        'exchange': 'NSE',
        'trigger_prices': [1450.0],
        'updated_at': updated_at,
        'legs': [{'action': 'BUY', 'quantity': 10, 'price': 1449.0,
                  'pricetype': 'LIMIT', 'product': 'CNC'}],
    }


def order_row(order_id='OID-1', status='complete', quantity=10,
              symbol='RELIANCE', action='BUY', timestamp=''):
    return {
        'orderid': order_id, 'status': status, 'quantity': quantity,
        'symbol': symbol, 'action': action, 'timestamp': timestamp,
        'exchange': 'NSE',
    }


# --------------------------------------------------------------------- setup

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
def split(ctx):
    """One resting GTT split on one active account."""
    user = User(username='admin', email='admin@example.com', is_admin=True)
    user.set_password('GttTest#1')
    db.session.add(user)
    db.session.commit()

    account = TradingAccount(
        user_id=user.id, account_name='mps', broker_name='zerodha',
        host_url='http://127.0.0.1:5001', websocket_url='ws://127.0.0.1:8766',
        is_active=True,
    )
    account.set_api_key('api-key-mps')
    db.session.add(account)
    db.session.flush()

    order = EquityOrder(
        user_id=user.id, symbol='RELIANCE', exchange='NSE',
        side=EQUITY_SIDE_BUY, order_type=EQUITY_ORDER_TYPE_GTT,
        total_quantity=10, price=1449.0, trigger_price=1450.0,
    )
    db.session.add(order)
    db.session.flush()

    row = EquityOrderSplit(
        equity_order_id=order.id, account_id=account.id, quantity=10,
        broker_gtt_id=TRIGGER_ID, fill_status=EQUITY_SPLIT_STATUS_PENDING,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def reconciler():
    r = EquityGttReconciler()
    r.start()
    r._stats = {'runs': 0, 'settled': 0, 'triggered': 0, 'unresolved': 0}
    yield r
    r.client_factory = mod.default_client_factory


def sweep(reconciler, broker):
    reconciler.client_factory = broker.factory
    reconciler.run_checks()
    return reconciler._last_tick


# ------------------------------------------------------------- pure helpers

@pytest.mark.parametrize('response', [
    None, 'nonsense', {}, {'status': 'error', 'message': 'nope'},
    {'status': 'success'}, {'status': 'success', 'data': 'not-a-list'},
])
def test_rows_from_rejects_unusable_responses(response):
    assert mod._rows_from(response) is None


def test_rows_from_accepts_a_plain_list():
    assert mod._rows_from({'status': 'success', 'data': [gtt_row('active')]}) != None


def test_rows_from_unwraps_a_nested_payload():
    """Adapters differ on whether data is the list or wraps it."""
    nested = {'status': 'success', 'data': {'orders': [gtt_row('active')]}}
    assert len(mod._rows_from(nested)) == 1


@pytest.mark.parametrize('raw,expected_year', [
    ('2026-04-29 12:18:42', 2026),
    ('2026-04-29T12:18:42', 2026),
    ('29-Apr-2026 12:18:42', 2026),
])
def test_parse_broker_time_handles_the_documented_shapes(raw, expected_year):
    parsed = mod._parse_broker_time(raw)
    assert parsed is not None and parsed.year == expected_year


@pytest.mark.parametrize('raw', ['', None, 'not a date', '0'])
def test_parse_broker_time_returns_none_rather_than_guessing(raw):
    assert mod._parse_broker_time(raw) is None


# ------------------------------------------------------------- resting state

def test_active_trigger_is_left_alone(split, reconciler):
    broker = FakeBroker(gtt_rows=[gtt_row('active')])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.gtt_status == 'active'
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert split.gtt_synced_at is not None


def test_transit_is_treated_as_resting(split, reconciler):
    """Fyers reports a newly accepted trigger as transit, not active."""
    broker = FakeBroker(gtt_rows=[gtt_row('transit')])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_the_book_is_read_with_status_all(split, reconciler):
    """The default of active-only is what hid every terminal state."""
    broker = FakeBroker(gtt_rows=[gtt_row('active')])
    sweep(reconciler, broker)

    assert broker.gtt_calls == ['all']


# ------------------------------------------------------------ terminal states

@pytest.mark.parametrize('status,expected', [
    ('cancelled', EQUITY_SPLIT_STATUS_CANCELLED),
    ('expired', EQUITY_SPLIT_STATUS_CANCELLED),
    ('rejected', EQUITY_SPLIT_STATUS_REJECTED),
])
def test_terminal_states_settle_the_split(split, reconciler, status, expected):
    broker = FakeBroker(gtt_rows=[gtt_row(status)])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.gtt_status == status
    assert split.fill_status == expected
    assert status in (split.error_message or '')


def test_a_settled_split_does_not_call_the_order_book(split, reconciler):
    """Only a fired trigger needs a child order looked up."""
    broker = FakeBroker(gtt_rows=[gtt_row('cancelled')])
    sweep(reconciler, broker)

    assert broker.orderbook_calls == 0


# ---------------------------------------------------------------- triggered

def test_triggered_resolves_the_child_order(split, reconciler):
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(order_id='OID-77')],
    )
    tick = sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.gtt_status == 'triggered'
    assert split.broker_order_id == 'OID-77'
    assert split.gtt_triggered_at is not None
    assert tick['child_orders_matched'] == 1


def test_triggered_leaves_the_split_open_for_the_order_pipeline(split, reconciler):
    """A fired trigger produced a real order; the fill is not this module's job."""
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row()],
    )
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_unmatched_child_order_leaves_the_split_open(split, reconciler):
    """The order exists somewhere. Closing the split would hide it."""
    broker = FakeBroker(gtt_rows=[gtt_row('triggered')], order_rows=[])
    tick = sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id is None
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert tick['child_orders_unresolved'] == 1


def test_two_candidate_orders_refuse_to_resolve(split, reconciler):
    """Attaching the wrong order to a trigger is worse than attaching none."""
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(order_id='OID-1'), order_row(order_id='OID-2')],
    )
    tick = sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id is None
    assert tick['child_orders_unresolved'] == 1


def test_a_different_quantity_is_not_the_child_order(split, reconciler):
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(quantity=999)],
    )
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id is None


def test_a_different_side_is_not_the_child_order(split, reconciler):
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(action='SELL')],
    )
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id is None


def test_trigger_pending_counts_as_a_child_order(split, reconciler):
    """OpenAlgo emits the two-word 'trigger pending', never bare 'pending'.

    Matching the documented-but-wrong 'pending' is one of the ways this search
    silently found nothing, and a fired GTT lands in exactly this state.
    """
    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(status='trigger pending', order_id='OID-TP')],
    )
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id == 'OID-TP'


def test_an_already_matched_split_is_not_rematched(split, reconciler):
    split.broker_order_id = 'OID-EXISTING'
    db.session.commit()

    broker = FakeBroker(
        gtt_rows=[gtt_row('triggered')],
        order_rows=[order_row(order_id='OID-OTHER')],
    )
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.broker_order_id == 'OID-EXISTING'
    assert broker.orderbook_calls == 0


# ------------------------------------------------------------ failure modes

def test_an_unreadable_book_settles_nothing(split, reconciler):
    """A broker with no gtt_api answers 501 here. That says nothing about the trigger."""
    broker = FakeBroker(gtt_response={
        'status': 'error', 'code': 501,
        'message': "GTT orders are not supported for broker 'x' yet",
    })
    tick = sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert split.gtt_status is None
    assert tick['books_unavailable'] == 1


def test_a_missing_trigger_is_unknown_not_dead(split, reconciler):
    """Upstox reports every row as active, so absence proves nothing."""
    broker = FakeBroker(gtt_rows=[gtt_row('active', trigger_id='SOMEONE-ELSE')])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.gtt_status == 'unknown'
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_a_failing_order_book_does_not_abort_the_sweep(split, reconciler):
    class Exploding(FakeBroker):
        def orderbook(self):
            raise RuntimeError('broker down')

    broker = Exploding(gtt_rows=[gtt_row('triggered')])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.gtt_status == 'triggered'
    assert split.broker_order_id is None


def test_run_checks_is_inert_until_started(split):
    r = EquityGttReconciler()
    r.stop()
    broker = FakeBroker(gtt_rows=[gtt_row('cancelled')])
    r.client_factory = broker.factory
    r.run_checks()

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert broker.gtt_calls == []
    r.start()


def test_an_inactive_account_is_skipped(split, reconciler):
    account = TradingAccount.query.get(split.account_id)
    account.is_active = False
    db.session.commit()

    broker = FakeBroker(gtt_rows=[gtt_row('cancelled')])
    sweep(reconciler, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert broker.gtt_calls == []


def test_a_settled_split_is_not_swept_again(split, reconciler):
    broker = FakeBroker(gtt_rows=[gtt_row('cancelled')])
    sweep(reconciler, broker)
    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_CANCELLED

    second = FakeBroker(gtt_rows=[gtt_row('cancelled')])
    tick = sweep(reconciler, second)
    assert tick['splits_examined'] == 0
    assert second.gtt_calls == []


def test_status_reports_the_last_sweep(split, reconciler):
    broker = FakeBroker(gtt_rows=[gtt_row('active')])
    sweep(reconciler, broker)

    report = reconciler.status()
    assert report['running'] is True
    assert report['last_error'] is None
    assert report['last_run_at'] is not None
    assert report['last_tick']['accounts_read'] == 1
