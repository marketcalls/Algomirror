"""
Order Qty Ratio: the standing figure and the applied figure are different numbers.

PRD 9.1 defines the ratio as an account's allocation over the total across all
ACTIVE accounts. Read literally that breaks the split: with 50L across five
accounts and only two ticked (20L and 10L), it gives 40 percent and 20 percent,
so a 100 share order places 60 shares and silently drops 40. Total Quantity on
M4 cannot mean that.

So both exist. The split normalises over the PARTICIPATING accounts, which is
what actually decides quantities and what is recorded as qty_ratio_at_order. The
standing ratio is the PRD 9.1 figure the M2 Accounts screen shows. These tests
pin that they agree when every account is ticked, diverge when a subset is, and
that the split always distributes the full quantity.
"""

import os
import sys
import tempfile
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-standing-ratio-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'ratio.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'standing-ratio-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    EquityAccountAllocation, TradingAccount, User,
    EQUITY_SIDE_BUY, EQUITY_ORDER_TYPE_MARKET,
)
from app.utils.equity_order_engine import preview_order_split  # noqa: E402

# The PRD's own worked example: MPS on 20L inside a 50L family corpus.
SEED = [('mps', 2000000.0), ('sathya', 1000000.0), ('suji', 1000000.0),
        ('patsen', 500000.0), ('unicorp', 500000.0)]


@pytest.fixture(scope='session')
def app():
    application = create_app('development')
    application.config['TESTING'] = True
    return application


@pytest.fixture
def world(app):
    with app.app_context():
        db.drop_all()
        db.create_all()
        user = User(username='admin', email='a@example.com', is_admin=True)
        user.set_password('Ratio#Test1')
        db.session.add(user)
        db.session.commit()

        ids = []
        for index, (name, allocation) in enumerate(SEED, start=1):
            account = TradingAccount(
                user_id=user.id, account_name=name, broker_name='zerodha',
                host_url=f'http://127.0.0.1:{5000 + index}',
                websocket_url=f'ws://127.0.0.1:{8765 + index}', is_active=True,
            )
            account.set_api_key(f'key-{name}')
            db.session.add(account)
            db.session.flush()
            db.session.add(EquityAccountAllocation(
                account_id=account.id, user_id=user.id,
                equity_fund_allocation=allocation, is_active=True,
            ))
            ids.append(account.id)
        db.session.commit()
        yield {'user_id': user.id, 'ids': ids}
        db.session.remove()
        db.drop_all()


def preview(world, account_ids, quantity=100):
    return preview_order_split(
        user_id=world['user_id'], symbol='RELIANCE', exchange='NSE',
        side=EQUITY_SIDE_BUY, total_quantity=quantity,
        order_type=EQUITY_ORDER_TYPE_MARKET, account_ids=account_ids,
        reference_price=100.0, cash_balances={i: 10_000_000.0 for i in world['ids']},
    )


def rows_of(result):
    return {r['account_name']: r for r in result['rows']}


def test_all_accounts_ticked_makes_the_two_ratios_agree(world):
    rows = rows_of(preview(world, world['ids']))

    for row in rows.values():
        assert row['qty_ratio'] == pytest.approx(row['standing_qty_ratio'])
    # The PRD worked example: 20L of 50L is 40 percent.
    assert rows['mps']['standing_qty_ratio'] == pytest.approx(40.0)


def test_a_subset_diverges_and_the_applied_ratio_is_the_one_used(world):
    """mps 20L and sathya 10L ticked out of a 50L corpus."""
    rows = rows_of(preview(world, world['ids'][:2]))

    # Standing: unchanged, still measured against the whole 50L.
    assert rows['mps']['standing_qty_ratio'] == pytest.approx(40.0)
    assert rows['sathya']['standing_qty_ratio'] == pytest.approx(20.0)
    # Applied: renormalised over the 30L actually participating.
    assert rows['mps']['qty_ratio'] == pytest.approx(200.0 / 3.0)
    assert rows['sathya']['qty_ratio'] == pytest.approx(100.0 / 3.0)


def test_a_subset_places_all_but_the_rounding_leftover(world):
    """The bug the literal PRD reading would cause: 40 of 100 shares dropped.

    The applied ratio drops only what rounding cannot place. Each account is
    floored to a whole lot and the remainder is reported as leftover rather than
    handed to whichever account happens to be first, which would quietly trade
    one member's money on another member's behalf.
    """
    result = preview(world, world['ids'][:2], quantity=100)
    placed = sum(row['quantity'] for row in result['rows'])

    # 66.67 percent and 33.33 percent of 100, each floored: 66 + 33.
    assert placed == 99
    assert result['leftover_quantity'] == 1

    # What the literal PRD reading would have produced instead: 40 + 20.
    literal = sum(
        int(100 * rows_of(result)[name]['standing_qty_ratio'] / 100.0)
        for name in ('mps', 'sathya')
    )
    assert literal == 60


def test_all_accounts_ticked_places_the_whole_quantity(world):
    """With no renormalising needed the ratios divide 100 exactly."""
    result = preview(world, world['ids'], quantity=100)

    assert sum(row['quantity'] for row in result['rows']) == 100
    assert result['leftover_quantity'] == 0


def test_the_standing_ratios_total_one_hundred(world):
    rows = rows_of(preview(world, world['ids']))
    assert sum(r['standing_qty_ratio'] for r in rows.values()) == pytest.approx(100.0)


def test_the_applied_ratios_total_one_hundred_on_any_subset(world):
    rows = rows_of(preview(world, world['ids'][:3]))
    assert sum(r['qty_ratio'] for r in rows.values()) == pytest.approx(100.0)


def test_a_deactivated_allocation_leaves_the_standing_ratio_of_the_others(world):
    """Deactivating an account changes the family denominator for everyone."""
    row = EquityAccountAllocation.query.filter_by(
        account_id=world['ids'][0], user_id=world['user_id']
    ).first()
    row.is_active = False
    db.session.commit()

    rows = rows_of(preview(world, world['ids'][1:]))
    # 10L of the remaining 30L.
    assert rows['sathya']['standing_qty_ratio'] == pytest.approx(100.0 / 3.0)
