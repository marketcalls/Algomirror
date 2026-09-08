"""
Est. Costs on the Trade Book (PRD 7.8).

The Trade Book was structurally empty until the fill poller landed, so costs
there were moot. Now that fills are real, PRD 7.8 requires the per-account rates
configured in Settings to drive Est. Costs here as well as on Holdings.

The property worth pinning is the side. STT and stamp duty differ between a buy
and a sell and DP charges apply per scrip on a sell only, so costing every fill
as a sell (the shortcut Holdings can take, since a holding is always eventually
sold) would overstate a buy.
"""

import os
import sys
import tempfile
from datetime import datetime
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

_TEST_DIR = tempfile.mkdtemp(prefix='algomirror-trade-costs-')
os.environ['DATABASE_URL'] = 'sqlite:///' + os.path.join(_TEST_DIR, 'tc.sqlite').replace(chr(92), '/')
os.environ['SECRET_KEY'] = 'trade-costs-test-key'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = os.path.join(_TEST_DIR, 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'ERROR'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

import pytest  # noqa: E402

from app import create_app  # noqa: E402
from app.equity.routes import _trade_payload  # noqa: E402
from app.utils.equity_costs import BrokerageRates  # noqa: E402


class Row:
    def __init__(self, **kw):
        self.__dict__.update(kw)


@pytest.fixture(scope='session')
def app():
    application = create_app('development')
    application.config['TESTING'] = True
    return application


@pytest.fixture
def ctx(app):
    with app.app_context():
        yield app


def build(side='BUY', rates=None, quantity=10, price=100.0):
    trade = Row(id=1, split_id=1, executed_quantity=quantity, execution_price=price,
                exchange='NSE', executed_at=datetime(2026, 9, 8, 10, 0, 0),
                broker_trade_id='T1')
    split = Row(id=1, account_id=7, broker_order_id='OID-1')
    order = Row(id=3, symbol='RELIANCE', exchange='NSE', side=side,
                order_type='MARKET', product='CNC', source='MANUAL',
                trade_nature_id=None, trade_nature=None, status='COMPLETED',
                placed_at=datetime(2026, 9, 8, 9, 59, 0))
    by_account = {7: rates} if rates is not None else None
    return _trade_payload(trade, split, order, {7: {'account_name': 'mps'}}, by_account)


def test_a_fill_carries_its_estimated_cost(ctx):
    rates = BrokerageRates(brokerage_per_order=20.0, gst_pct=18.0)
    row = build(side='BUY', rates=rates)

    assert row['trade_value'] == 1000.0
    assert row['est_costs'] > 0


def test_no_configured_rates_means_no_invented_cost(ctx):
    """A missing rate must not become a fabricated charge."""
    row = build(side='BUY')
    assert row['est_costs'] == 0.0


def test_a_buy_and_a_sell_do_not_cost_the_same(ctx):
    """STT, stamp duty and DP charges are side dependent."""
    rates = BrokerageRates(
        brokerage_per_order=20.0, stt_pct=0.1, stamp_duty_pct=0.015,
        dp_amc_charge=13.0, gst_pct=18.0,
    )
    buy = build(side='BUY', rates=rates)
    sell = build(side='SELL', rates=rates)

    assert buy['est_costs'] != sell['est_costs']


def test_net_value_moves_the_right_way_for_each_side(ctx):
    """A buy costs more than the turnover; a sell realises less."""
    rates = BrokerageRates(brokerage_per_order=20.0, gst_pct=18.0)

    buy = build(side='BUY', rates=rates)
    assert buy['net_value'] > buy['trade_value']

    sell = build(side='SELL', rates=rates)
    assert sell['net_value'] < sell['trade_value']


def test_the_payload_keeps_the_fields_the_screen_reads(ctx):
    row = build(side='BUY', rates=BrokerageRates(brokerage_per_order=20.0))
    for key in ('trade_value', 'est_costs', 'net_value', 'executed_quantity',
                'execution_price', 'account_name', 'symbol', 'side'):
        assert key in row, f'{key} missing from the trade payload'
