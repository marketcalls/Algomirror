"""
Tests for the equity fill poller (app/utils/equity_fill_poller.py).

This module is what makes Trade Book real, so its failure modes are all about
quantity: booking the same fill twice doubles a position on screen, and failing
to book one leaves a filled order looking open. The de-duplication tests are
therefore the important ones, and they exercise both paths, the unique index on
(split_id, broker_trade_id) and the fallback matching used for brokers that
return no trade id at all.

The other theme is refusing to interpret. OpenAlgo emits 'open', 'complete',
'rejected', 'cancelled', 'trigger pending' and 'unknown'. The published table
lists 'pending', which is never emitted, so a poller written against the docs
matches nothing. Anything unrecognised leaves the split open rather than
guessing at a terminal state.

No broker is contacted. Every read goes through client_factory, and every test
passes a fake.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-equity-fill-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'fill.sqlite').replace('\\', '/')
os.environ['SECRET_KEY'] = 'equity-fill-test-key-not-for-production'
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
    EquityTrade,
    TradingAccount,
    User,
    EQUITY_SIDE_BUY,
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_PARTIAL,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
)
from app.utils import equity_fill_poller as mod  # noqa: E402
from app.utils.equity_fill_poller import EquityFillPoller  # noqa: E402

ORDER_ID = 'OID-1'


class FakeBroker:
    def __init__(self, status='complete', trades=None, extra=None,
                 status_response=None, tradebook_raises=False):
        self.status = status
        self.trades = trades if trades is not None else []
        self.extra = extra or {}
        self.status_response = status_response
        self.tradebook_raises = tradebook_raises
        self.orderstatus_calls = 0
        self.tradebook_calls = 0

    def factory(self, credential):
        return self

    def orderstatus(self, order_id=None, **kwargs):
        self.orderstatus_calls += 1
        if self.status_response is not None:
            return self.status_response
        data = {'orderid': order_id, 'order_status': self.status, 'quantity': 10}
        data.update(self.extra)
        return {'status': 'success', 'data': data}

    def tradebook(self):
        self.tradebook_calls += 1
        if self.tradebook_raises:
            raise RuntimeError('broker down')
        return {'status': 'success', 'data': list(self.trades)}


def trade(trade_id='T1', quantity=10, price=100.0, order_id=ORDER_ID):
    return {
        'tradeid': trade_id, 'orderid': order_id, 'quantity': quantity,
        'average_price': price, 'exchange': 'NSE', 'symbol': 'RELIANCE',
    }


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
    user = User(username='admin', email='admin@example.com', is_admin=True)
    user.set_password('FillTest#1')
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
        side=EQUITY_SIDE_BUY, order_type=EQUITY_ORDER_TYPE_MARKET,
        total_quantity=10, price=100.0,
    )
    db.session.add(order)
    db.session.flush()

    row = EquityOrderSplit(
        equity_order_id=order.id, account_id=account.id, quantity=10,
        broker_order_id=ORDER_ID, fill_status=EQUITY_SPLIT_STATUS_PENDING,
    )
    db.session.add(row)
    db.session.commit()
    return row


@pytest.fixture
def poller():
    p = EquityFillPoller()
    p.start()
    p._stats = {'runs': 0, 'settled': 0, 'fills_booked': 0}
    yield p
    p.client_factory = mod.default_client_factory


def sweep(poller, broker):
    poller.client_factory = broker.factory
    poller.run_checks()
    return poller._last_tick


def trades_for(split):
    return EquityTrade.query.filter_by(split_id=split.id).all()


# ------------------------------------------------------------- pure helpers

@pytest.mark.parametrize('raw,expected', [
    ('10', 10), (10, 10), (10.0, 10), ('10.0', 10), ('', 0), (None, 0), ('abc', 0),
])
def test_to_int_survives_the_type_soup(raw, expected):
    """orderbook quantities are strings, tradebook quantities are numbers."""
    assert mod._to_int(raw) == expected


@pytest.mark.parametrize('response', [
    None, 'nope', {'status': 'error', 'message': 'x'},
])
def test_payload_rejects_failures(response):
    assert mod._payload(response) is None


def test_rows_unwraps_either_shape():
    assert len(mod._rows({'status': 'success', 'data': [1, 2]}, 'trades')) == 2
    assert len(mod._rows({'status': 'success', 'data': {'trades': [1]}}, 'trades')) == 1


# ------------------------------------------------------------- working states

@pytest.mark.parametrize('status', ['open', 'trigger pending'])
def test_a_working_order_stays_open(split, poller, status):
    broker = FakeBroker(status=status)
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert split.broker_order_status == status
    assert split.last_synced_at is not None


def test_the_never_emitted_pending_is_not_interpreted(split, poller):
    """The published table says 'pending'. OpenAlgo never sends it.

    A poller written against the docs would match nothing here. Treating an
    unrecognised status as terminal would be worse, so the split stays open.
    """
    broker = FakeBroker(status='pending')
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_unknown_status_leaves_the_split_open(split, poller):
    broker = FakeBroker(status='unknown')
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING


def test_a_partial_fill_on_a_working_order_reads_as_partial(split, poller):
    broker = FakeBroker(status='open', trades=[trade(quantity=4)])
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PARTIAL
    assert split.filled_quantity == 4


# ----------------------------------------------------------- terminal states

def test_complete_settles_and_books_the_fill(split, poller):
    broker = FakeBroker(status='complete', trades=[trade(quantity=10, price=101.5)])
    tick = sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_COMPLETED
    assert split.filled_quantity == 10
    assert split.avg_fill_price == 101.5
    assert len(trades_for(split)) == 1
    assert tick['fills_booked'] == 1


def test_rejected_records_the_reason(split, poller):
    broker = FakeBroker(status='rejected', extra={'rejection_reason': 'Insufficient funds'})
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_REJECTED
    assert split.error_message == 'Insufficient funds'


@pytest.mark.parametrize('status', ['cancelled', 'expired'])
def test_cancelled_and_expired_both_settle_as_cancelled(split, poller, status):
    broker = FakeBroker(status=status)
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_CANCELLED


def test_several_fills_average_by_weight(split, poller):
    broker = FakeBroker(status='complete', trades=[
        trade(trade_id='T1', quantity=6, price=100.0),
        trade(trade_id='T2', quantity=4, price=110.0),
    ])
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.filled_quantity == 10
    # (6*100 + 4*110) / 10
    assert split.avg_fill_price == 104.0
    assert len(trades_for(split)) == 2


# ------------------------------------------------------------ de-duplication

def test_the_same_fill_is_not_booked_twice(split, poller):
    """The regression that matters: a repeated poll doubling the quantity."""
    broker = FakeBroker(status='open', trades=[trade(quantity=10)])
    sweep(poller, broker)
    assert len(trades_for(split)) == 1

    second = FakeBroker(status='open', trades=[trade(quantity=10)])
    sweep(poller, second)

    db.session.refresh(split)
    assert len(trades_for(split)) == 1
    assert split.filled_quantity == 10


def test_a_broker_with_no_trade_id_still_de_duplicates(split, poller):
    """No trade id means no unique index, so quantity and price carry it."""
    row = trade(quantity=10, price=100.0)
    row.pop('tradeid')
    broker = FakeBroker(status='open', trades=[row])
    sweep(poller, broker)
    assert len(trades_for(split)) == 1

    row2 = trade(quantity=10, price=100.0)
    row2.pop('tradeid')
    sweep(poller, FakeBroker(status='open', trades=[row2]))

    assert len(trades_for(split)) == 1


def test_a_genuinely_new_fill_is_added_to_an_existing_one(split, poller):
    sweep(poller, FakeBroker(status='open', trades=[trade(trade_id='T1', quantity=4)]))
    assert len(trades_for(split)) == 1

    sweep(poller, FakeBroker(status='complete', trades=[
        trade(trade_id='T1', quantity=4),
        trade(trade_id='T2', quantity=6),
    ]))

    db.session.refresh(split)
    assert len(trades_for(split)) == 2
    assert split.filled_quantity == 10


def test_a_zero_quantity_row_is_ignored(split, poller):
    sweep(poller, FakeBroker(status='open', trades=[trade(quantity=0)]))
    assert trades_for(split) == []


def test_a_fill_for_another_order_is_not_booked(split, poller):
    broker = FakeBroker(status='open', trades=[trade(order_id='SOMEONE-ELSE')])
    sweep(poller, broker)

    assert trades_for(split) == []


# ------------------------------------------------------------ failure modes

def test_a_failed_status_read_changes_nothing(split, poller):
    broker = FakeBroker(status_response={'status': 'error', 'message': 'boom'})
    tick = sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert tick['reads_failed'] == 1


def test_a_failing_trade_book_does_not_abort_the_sweep(split, poller):
    broker = FakeBroker(status='complete', tradebook_raises=True)
    sweep(poller, broker)

    db.session.refresh(split)
    # The header still settles the split; only the per-fill detail is missing.
    assert split.fill_status == EQUITY_SPLIT_STATUS_COMPLETED


def test_the_trade_book_is_read_once_per_account_not_per_order(split, poller):
    broker = FakeBroker(status='complete', trades=[trade()])
    sweep(poller, broker)

    assert broker.tradebook_calls == 1


def test_an_inactive_account_is_skipped(split, poller):
    account = db.session.get(TradingAccount, split.account_id)
    account.is_active = False
    db.session.commit()

    broker = FakeBroker(status='complete')
    sweep(poller, broker)

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert broker.orderstatus_calls == 0


def test_a_settled_split_is_not_polled_again(split, poller):
    sweep(poller, FakeBroker(status='complete', trades=[trade()]))
    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_COMPLETED

    second = FakeBroker(status='complete')
    tick = sweep(poller, second)
    assert tick['splits_examined'] == 0
    assert second.orderstatus_calls == 0


def test_run_checks_is_inert_until_started(split):
    p = EquityFillPoller()
    p.stop()
    broker = FakeBroker(status='complete')
    p.client_factory = broker.factory
    p.run_checks()

    db.session.refresh(split)
    assert split.fill_status == EQUITY_SPLIT_STATUS_PENDING
    assert broker.orderstatus_calls == 0
    p.start()


def test_a_split_with_no_order_id_is_not_polled(split, poller):
    split.broker_order_id = None
    db.session.commit()

    broker = FakeBroker(status='complete')
    tick = sweep(poller, broker)
    assert tick['splits_examined'] == 0


# ---------------------------------------------------------- parent rollup

def test_the_parent_order_status_follows_the_split(split, poller):
    broker = FakeBroker(status='complete', trades=[trade()])
    sweep(poller, broker)

    order = db.session.get(EquityOrder, split.equity_order_id)
    assert order.status == EQUITY_SPLIT_STATUS_COMPLETED


def test_status_reports_the_last_sweep(split, poller):
    sweep(poller, FakeBroker(status='open'))

    report = poller.status()
    assert report['running'] is True
    assert report['last_error'] is None
    assert report['last_tick']['accounts_polled'] == 1
