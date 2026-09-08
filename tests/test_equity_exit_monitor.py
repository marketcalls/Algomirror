"""
Behaviour tests for the equity stop loss and target monitor
(app/utils/equity_exit_monitor.py).

These boot the real Flask app against a throwaway SQLite file, because the
monitor is built out of EquityHolding's committed state transitions and there is
nothing worth testing about it with those mocked away. Nothing here touches a
broker, a WebSocket or a scheduler:

    the price feed is a fake with a dict of prices,
    the exit placer is a fake that records the calls it was given,
    the scheduler is replaced by calling run_checks() directly.

Run with the scratchpad virtual environment, which carries Flask and
SQLAlchemy:

    PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 \
    PYTHONPATH="<scratchpad>/vtest/Lib/site-packages" \
    python -m pytest tests/test_equity_exit_monitor.py -o addopts=""
"""

import os
import sys
import tempfile
import threading
from datetime import datetime, time as dt_time, timedelta
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
_TMP = Path(tempfile.mkdtemp(prefix='algomirror-exit-monitor-'))

# config.py reads these at import time, so they have to be set before the app
# package is imported. The user's own instance/algomirror.db is never touched.
os.environ['DATABASE_URL'] = 'sqlite:///' + str(_TMP / 'exit_monitor.sqlite').replace('\\', '/')
os.environ['SECRET_KEY'] = 'equity-exit-monitor-test-key-not-for-production'
os.environ['FLASK_ENV'] = 'development'
os.environ['SESSION_TYPE'] = 'filesystem'
os.environ['SESSION_FILE_DIR'] = str(_TMP / 'session')
os.environ['PING_MONITORING_ENABLED'] = 'false'
os.environ['LOG_LEVEL'] = 'CRITICAL'
os.environ.setdefault('ENCRYPTION_KEY', 'PmB4Zy7bnE3IiiZ2n7xkEcHXmFqI1IqRxnkKYIlHRTk=')

sys.path.insert(0, str(REPO))

from app import create_app, db  # noqa: E402
from app.models import (  # noqa: E402
    ActivityLog,
    EquityHolding,
    EquitySetting,
    MarketHoliday,
    TradingAccount,
    TradingHoursTemplate,
    TradingSession,
    User,
    EQUITY_EXIT_MODE_AUTO,
    EQUITY_EXIT_MODE_CONFIRM,
    EQUITY_EXIT_REASON_STOP_LOSS,
    EQUITY_EXIT_REASON_TARGET,
    EQUITY_HOLDING_STATUS_ACTIVE,
    EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
    EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE,
    EQUITY_HOLDING_STATUS_EXIT_SUBMITTED,
    EQUITY_ORDER_SOURCE_STOP_LOSS,
    EQUITY_ORDER_SOURCE_TARGET,
)
from app.utils import equity_exit_monitor as monitor_module  # noqa: E402
from app.utils.equity_exit_monitor import (  # noqa: E402
    AUTO_EXIT_RETRY_SECONDS,
    MAX_AUTO_EXIT_ATTEMPTS,
    EquityExitMonitor,
    equity_exit_monitor,
    reset_exit_placer,
    resolve_exit_placer,
    run_equity_exit_checks,
    set_exit_placer,
)

IST = monitor_module.IST


# ---------------------------------------------------------------------------
# Fakes
# ---------------------------------------------------------------------------

class FakeFeed:
    """
    Stands in for equity_price_feed. Holds whatever prices a test puts in it
    and never talks to a WebSocket.
    """

    def __init__(self):
        self.prices = {}
        self.primed = []

    def prime(self, keys):
        keys = list(keys)
        self.primed.append(keys)
        return {key: self.prices[key] for key in keys if key in self.prices}

    def status(self):
        return {
            'available': True,
            'authenticated': True,
            'subscribed': len(self.prices),
            'priced': len(self.prices),
            'last_tick_age_seconds': 0.1,
        }


class FakePlacer:
    """
    Stands in for the order engine's shared claim-and-place helper.

    The realistic default takes the claim and marks the holding submitted, the
    way the real helper would, so the tests exercise the state machine the
    monitor actually depends on.
    """

    def __init__(self, result=None, raises=None, act=True):
        self.calls = []
        self.result = {'success': True} if result is None else result
        self.raises = raises
        self.act = act

    def __call__(self, holding_id=None, user_id=None, account_id=None,
                 symbol=None, exchange=None, quantity=None, reason=None,
                 source=None, breach_price=None, level_price=None):
        self.calls.append({
            'holding_id': holding_id,
            'user_id': user_id,
            'account_id': account_id,
            'symbol': symbol,
            'exchange': exchange,
            'quantity': quantity,
            'reason': reason,
            'source': source,
            'breach_price': breach_price,
            'level_price': level_price,
        })

        if self.raises is not None:
            raise self.raises

        succeeded = (
            self.result.get('success') is True
            if isinstance(self.result, dict) else bool(self.result)
        )
        if self.act and succeeded:
            claimed, message = EquityHolding.claim_for_exit(
                holding_id, user_id, reason, quantity=quantity
            )
            if claimed is None:
                return {'success': False, 'message': message}
            EquityHolding.mark_exit_submitted(
                holding_id, user_id, 'BROKER-%s' % holding_id
            )

        return self.result


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture(scope='session')
def flask_app():
    application = create_app('development')
    application.config['TESTING'] = True
    with application.app_context():
        db.create_all()
    return application


@pytest.fixture
def ctx(flask_app):
    """A clean database and a monitor with no state carried over."""
    with flask_app.app_context():
        # The monitor is a singleton, so every test starts by wiping it.
        equity_exit_monitor.stop(wait=True)
        equity_exit_monitor.inline_exits = True
        equity_exit_monitor._attempts.clear()
        equity_exit_monitor._orphan_warned.clear()
        for key in equity_exit_monitor._stats:
            equity_exit_monitor._stats[key] = 0
        reset_exit_placer()

        for model in (ActivityLog, EquityHolding, EquitySetting,
                      TradingSession, TradingHoursTemplate, MarketHoliday,
                      TradingAccount, User):
            model.query.delete()
        db.session.commit()

        user = User(username='monitor', email='monitor@example.com', is_admin=True)
        user.set_password('MonitorTest#1')
        db.session.add(user)
        db.session.commit()

        account = TradingAccount(
            user_id=user.id,
            account_name='mps',
            broker_name='Zerodha',
            host_url='http://127.0.0.1:5000',
            websocket_url='ws://127.0.0.1:8765',
        )
        account.set_api_key('not-a-real-key')
        db.session.add(account)
        db.session.commit()

        settings = EquitySetting.get_or_create(user.id)
        settings.sl_monitor_enabled = True
        settings.sl_monitor_interval_seconds = 1
        db.session.commit()

        _open_the_market()

        feed = FakeFeed()
        original_feed = monitor_module.equity_price_feed
        monitor_module.equity_price_feed = feed

        equity_exit_monitor.start()

        yield {
            'app': flask_app,
            'user_id': user.id,
            'account_id': account.id,
            'feed': feed,
        }

        monitor_module.equity_price_feed = original_feed
        equity_exit_monitor.stop(wait=True)
        reset_exit_placer()


def _open_the_market():
    """A trading session covering the whole of today, in IST."""
    template = TradingHoursTemplate(name='Test NSE', market='NSE', is_active=True)
    db.session.add(template)
    db.session.commit()

    session = TradingSession(
        template_id=template.id,
        session_name='normal',
        day_of_week=datetime.now(IST).weekday(),
        start_time=dt_time(0, 0, 0),
        end_time=dt_time(23, 59, 59),
        session_type='normal',
        is_active=True,
    )
    db.session.add(session)
    db.session.commit()


def _close_the_market():
    TradingSession.query.delete()
    db.session.commit()


def _make_holding(ctx, symbol='RELIANCE', exchange='NSE', quantity=100,
                  avg_cost=1000.0, stop_loss=None, target=None,
                  exit_mode=EQUITY_EXIT_MODE_AUTO, pledged_quantity=0):
    holding = EquityHolding(
        user_id=ctx['user_id'],
        account_id=ctx['account_id'],
        symbol=symbol,
        exchange=exchange,
        quantity=quantity,
        pledged_quantity=pledged_quantity,
        avg_cost=avg_cost,
        stop_loss=stop_loss,
        target=target,
        exit_mode=exit_mode,
    )
    db.session.add(holding)
    db.session.commit()
    return holding


def _tick(ctx):
    """
    One monitor pass. The per user pacing gate is cleared first so a test can
    run two passes back to back without waiting out the configured interval.
    """
    settings = EquitySetting.get_or_create(ctx['user_id'])
    settings.monitor_last_run_at = None
    db.session.commit()
    run_equity_exit_checks()


def _reload(holding_id):
    db.session.expire_all()
    return EquityHolding.query.filter_by(id=holding_id).populate_existing().first()


def _fresh():
    """
    Drop this session's cached copies before driving a model transition.

    The monitor dispatches its exits inside their own app context, which means
    their own SQLAlchemy session. A test that then calls a transition from the
    outer session would otherwise hand the transition the row as it looked
    before the exit ran, and the transition's re-check would agree with itself
    rather than with the database. Production callers get this from the tick's
    own expire_all.
    """
    db.session.expire_all()


# ---------------------------------------------------------------------------
# The required cases
# ---------------------------------------------------------------------------

def test_stop_loss_breach_in_auto_mode_places_the_exit_exactly_once(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)

    assert len(placer.calls) == 1
    call = placer.calls[0]
    assert call['holding_id'] == holding.id
    assert call['user_id'] == ctx['user_id']
    assert call['reason'] == EQUITY_EXIT_REASON_STOP_LOSS
    assert call['source'] == EQUITY_ORDER_SOURCE_STOP_LOSS
    assert call['breach_price'] == 940.0
    assert call['level_price'] == 950.0
    # None means "claim and sell the whole sellable quantity", read fresh under
    # the claim's own row lock rather than from a number the monitor cached.
    assert call['quantity'] is None

    refreshed = _reload(holding.id)
    assert refreshed.sl_hit_at is not None
    assert refreshed.sl_hit_price == 940.0
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED


def test_the_same_holding_is_not_exited_again_on_the_next_tick(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    assert len(placer.calls) == 1

    for _ in range(4):
        _tick(ctx)

    assert len(placer.calls) == 1, 'a claimed holding must never be sold twice'


def test_a_successful_exit_is_not_repeated_even_if_the_row_stays_active(ctx):
    """
    The claim is the real guard, but the monitor must not lean on it alone. A
    placer that reports success without moving the row leaves the holding
    ACTIVE and breached, and even then nothing is sent again.
    """
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer(act=False)
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    _tick(ctx)
    _tick(ctx)

    assert len(placer.calls) == 1


def test_confirm_mode_breach_places_nothing_and_awaits_the_admin(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_CONFIRM)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)

    assert placer.calls == [], 'CONFIRM mode must never place an order'

    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM
    assert refreshed.exit_reason == EQUITY_EXIT_REASON_STOP_LOSS
    assert refreshed.sl_hit_at is not None
    assert refreshed.quantity == 100, 'nothing was sold'

    alerts = ActivityLog.query.filter_by(
        action='equity_exit_confirmation_required'
    ).all()
    assert len(alerts) == 1


def test_confirm_mode_does_not_re_alert_on_every_tick(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_CONFIRM)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0
    set_exit_placer(FakePlacer(), name='fake')

    for _ in range(5):
        _tick(ctx)

    assert ActivityLog.query.filter_by(
        action='equity_exit_confirmation_required'
    ).count() == 1

    # And a declined alert stays declined instead of coming straight back.
    _fresh()
    assert EquityHolding.dismiss_exit_confirm(holding.id, ctx['user_id']) is True
    for _ in range(3):
        _tick(ctx)

    assert ActivityLog.query.filter_by(
        action='equity_exit_confirmation_required'
    ).count() == 1
    assert _reload(holding.id).exit_status == EQUITY_HOLDING_STATUS_ACTIVE


def test_a_holding_with_no_live_price_is_never_exited(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    # The feed knows nothing about this symbol. The stale last_price column is
    # deliberately set through the level to prove it is not consulted.
    holding.last_price = 900.0
    holding.last_price_updated = datetime.utcnow()
    db.session.commit()

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)

    assert placer.calls == []
    refreshed = _reload(holding.id)
    assert refreshed.sl_hit_at is None
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
    assert equity_exit_monitor.status()['last_tick']['holdings_without_price'] == 1


def test_a_holding_with_neither_level_is_ignored(ctx):
    holding = _make_holding(ctx, stop_loss=None, target=None)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)

    assert placer.calls == []
    assert equity_exit_monitor.status()['last_tick']['holdings_evaluated'] == 0
    assert _reload(holding.id).exit_status == EQUITY_HOLDING_STATUS_ACTIVE


def test_target_breach_behaves_the_same_way_as_a_stop_loss(ctx):
    holding = _make_holding(ctx, target=1200.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1250.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    _tick(ctx)

    assert len(placer.calls) == 1
    call = placer.calls[0]
    assert call['reason'] == EQUITY_EXIT_REASON_TARGET
    assert call['source'] == EQUITY_ORDER_SOURCE_TARGET
    assert call['level_price'] == 1200.0

    refreshed = _reload(holding.id)
    assert refreshed.tp_hit_at is not None
    assert refreshed.tp_hit_price == 1250.0
    assert refreshed.sl_hit_at is None
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED


def test_a_target_in_confirm_mode_awaits_the_admin(ctx):
    holding = _make_holding(ctx, target=1200.0, exit_mode=EQUITY_EXIT_MODE_CONFIRM)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1250.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM
    assert refreshed.exit_reason == EQUITY_EXIT_REASON_TARGET


def test_nothing_is_evaluated_outside_trading_hours(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _close_the_market()
    _tick(ctx)

    assert placer.calls == []
    assert ctx['feed'].primed == [], 'the feed is not even asked when the market is shut'

    refreshed = _reload(holding.id)
    assert refreshed.sl_hit_at is None
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_ACTIVE

    status = equity_exit_monitor.status()
    assert status['last_tick']['holdings_evaluated'] == 0
    assert status['last_skip_reason'] == 'outside trading hours'

    # The heartbeat still runs, which is how Settings can tell a monitor that
    # is idle from one that is dead.
    settings = EquitySetting.get_or_create(ctx['user_id'])
    assert settings.monitor_last_run_at is not None


def test_a_market_holiday_closes_the_market(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0
    set_exit_placer(FakePlacer(), name='fake')

    db.session.add(MarketHoliday(
        holiday_date=datetime.now(IST).date(),
        holiday_name='Test Holiday',
    ))
    db.session.commit()

    _tick(ctx)
    assert equity_exit_monitor.status()['last_skip_reason'] == 'outside trading hours'


# ---------------------------------------------------------------------------
# Retry discipline
# ---------------------------------------------------------------------------

def test_a_failed_auto_exit_is_not_retried_inside_the_cooldown(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer(result={'success': False, 'message': 'broker rejected'})
    set_exit_placer(placer, name='fake')

    for _ in range(5):
        _tick(ctx)

    assert len(placer.calls) == 1, 'the cooldown must hold off an immediate retry'


def test_a_failed_auto_exit_is_retried_once_the_cooldown_passes(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer(result={'success': False, 'message': 'broker rejected'})
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    assert len(placer.calls) == 1

    # Wind the attempt back past the cooldown instead of sleeping for it.
    attempt = equity_exit_monitor._attempts[holding.id]
    attempt.last_attempt_at -= (AUTO_EXIT_RETRY_SECONDS + 1)

    _tick(ctx)
    assert len(placer.calls) == 2

    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
    assert refreshed.exit_broker_order_id is None


def test_retries_stop_at_the_attempt_cap(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer(result={'success': False, 'message': 'broker rejected'})
    set_exit_placer(placer, name='fake')

    for _ in range(MAX_AUTO_EXIT_ATTEMPTS + 5):
        _tick(ctx)
        attempt = equity_exit_monitor._attempts.get(holding.id)
        if attempt is not None:
            attempt.last_attempt_at -= (AUTO_EXIT_RETRY_SECONDS + 1)

    assert len(placer.calls) == MAX_AUTO_EXIT_ATTEMPTS
    assert equity_exit_monitor._attempts[holding.id].given_up is True


def test_a_placer_that_raises_leaves_a_claimed_holding_alone(ctx):
    """
    An exception is not proof that nothing reached the broker. The real helper
    claims before it calls, so a raise after the claim leaves the row
    EXIT_PENDING, and a row that is not ACTIVE is never evaluated again.
    """
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    def claim_then_explode(holding_id=None, user_id=None, reason=None, **kwargs):
        EquityHolding.claim_for_exit(holding_id, user_id, reason)
        raise RuntimeError('connection reset while placing')

    calls = []

    def placer(**kwargs):
        calls.append(kwargs)
        return claim_then_explode(**kwargs)

    set_exit_placer(placer, name='exploding')

    _tick(ctx)
    attempt = equity_exit_monitor._attempts[holding.id]
    attempt.last_attempt_at -= (AUTO_EXIT_RETRY_SECONDS + 1)
    _tick(ctx)

    assert len(calls) == 1, 'a claimed holding is out of the monitorable set'


def test_an_indeterminate_holding_is_never_touched_again(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    _fresh()
    EquityHolding.mark_exit_indeterminate(
        holding.id, ctx['user_id'], 'timeout while placing'
    )

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    for _ in range(3):
        _tick(ctx)

    assert placer.calls == []
    assert _reload(holding.id).exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE


def test_a_partial_fill_does_not_fire_a_second_exit(ctx):
    """
    mark_exit_completed keeps the breach records on purpose, and puts the row
    back to ACTIVE with the shares that are left. The monitor must read that as
    "already acted on", not as "breached again".
    """
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    assert len(placer.calls) == 1

    _fresh()
    EquityHolding.mark_exit_completed(holding.id, ctx['user_id'], remaining_quantity=40)
    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
    assert refreshed.quantity == 40
    assert refreshed.sl_hit_at is not None

    for _ in range(3):
        _tick(ctx)

    assert len(placer.calls) == 1
    assert _reload(holding.id).quantity == 40


def test_re_arming_a_level_lets_it_fire_again(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    _tick(ctx)
    assert len(placer.calls) == 1

    _fresh()
    EquityHolding.mark_exit_completed(holding.id, ctx['user_id'], remaining_quantity=40)
    _fresh()
    EquityHolding.clear_breach(holding.id, ctx['user_id'])

    _tick(ctx)
    assert len(placer.calls) == 2


# ---------------------------------------------------------------------------
# Refusing to act on bad data
# ---------------------------------------------------------------------------

def test_a_price_nowhere_near_the_average_cost_is_rejected(ctx):
    holding = _make_holding(ctx, avg_cost=1000.0, stop_loss=950.0,
                            exit_mode=EQUITY_EXIT_MODE_AUTO)
    # A decimal shift in the feed, not a 99 percent crash.
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 9.4

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    assert _reload(holding.id).sl_hit_at is None


def test_a_stop_loss_above_the_target_is_treated_as_bad_data(ctx):
    holding = _make_holding(ctx, stop_loss=1200.0, target=900.0,
                            exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1000.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    refreshed = _reload(holding.id)
    assert refreshed.sl_hit_at is None
    assert refreshed.tp_hit_at is None


def test_a_fully_pledged_holding_is_not_exited(ctx):
    holding = _make_holding(ctx, quantity=100, pledged_quantity=100,
                            stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    assert _reload(holding.id).sl_hit_at is None


def test_an_unknown_exit_mode_is_treated_as_confirm(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode='SOMETHING_ELSE')
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == [], 'a corrupt exit mode must never mean AUTO'
    assert _reload(holding.id).sl_hit_at is not None


def test_a_broken_price_feed_exits_nothing(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)

    class BrokenFeed:
        def prime(self, keys):
            raise RuntimeError('websocket manager is gone')

        def status(self):
            return {}

    monitor_module.equity_price_feed = BrokenFeed()

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    assert _reload(holding.id).exit_status == EQUITY_HOLDING_STATUS_ACTIVE


def test_a_price_exactly_on_the_level_counts_as_a_breach(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 950.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert len(placer.calls) == 1


def test_a_price_above_the_stop_loss_is_left_alone(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, target=1200.0,
                            exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1000.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []
    refreshed = _reload(holding.id)
    assert refreshed.sl_hit_at is None
    assert refreshed.tp_hit_at is None


# ---------------------------------------------------------------------------
# Wiring, settings and diagnostics
# ---------------------------------------------------------------------------

def test_a_stopped_monitor_does_nothing(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    equity_exit_monitor.stop(wait=True)
    _tick(ctx)

    assert placer.calls == []
    assert equity_exit_monitor.status()['is_running'] is False


def test_the_monitor_can_be_switched_off_in_settings(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    settings = EquitySetting.get_or_create(ctx['user_id'])
    settings.sl_monitor_enabled = False
    db.session.commit()

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert placer.calls == []


def test_the_per_user_interval_paces_the_scheduler(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    settings = EquitySetting.get_or_create(ctx['user_id'])
    settings.sl_monitor_interval_seconds = 300
    settings.monitor_last_run_at = datetime.utcnow()
    db.session.commit()

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    # Straight through run_checks, without _tick clearing the gate.
    run_equity_exit_checks()

    assert placer.calls == [], 'a user asking for a 300 second pace is not run every tick'


def test_the_heartbeat_proves_the_monitor_ran(ctx):
    _make_holding(ctx, stop_loss=1.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 1000.0
    set_exit_placer(FakePlacer(), name='fake')

    before = datetime.utcnow() - timedelta(seconds=1)
    _tick(ctx)

    settings = EquitySetting.get_or_create(ctx['user_id'])
    assert settings.monitor_last_run_at is not None
    assert settings.monitor_last_run_at >= before
    assert settings.monitor_last_error is None


def test_the_default_lookup_finds_the_real_order_engine():
    """
    With nothing injected, the monitor has to land on the one function that
    owns the claim-and-place sequence.
    """
    reset_exit_placer()
    placer, name = resolve_exit_placer()

    assert name == 'app.utils.equity_order_engine.exit_holding'
    assert callable(placer)


def test_the_real_order_engine_only_receives_reviewed_arguments():
    """
    The monitor narrows its offered arguments to the ones the placer's
    signature accepts, which means a name it happens to share with an order
    parameter would be silently forwarded. trigger_price already means the GTT
    trigger on exit_holding, so the monitor calls its own value breach_price.

    If this fails, a newly added exit_holding parameter has started matching
    one of the monitor's keys. Read what that parameter means before widening
    the allow-list: it is the difference between a market exit and an order
    the monitor never asked for.
    """
    from app.utils import equity_order_engine

    payload = monitor_module.build_exit_payload(
        holding_id=7,
        user_id=1,
        account_id=3,
        symbol='RELIANCE',
        exchange='NSE',
        reason=EQUITY_EXIT_REASON_STOP_LOSS,
        breach_price=940.0,
        level_price=950.0,
    )
    kwargs = monitor_module._placer_kwargs(equity_order_engine.exit_holding, payload)

    assert set(kwargs) == {'holding_id', 'user_id', 'reason', 'quantity'}
    assert kwargs['quantity'] is None
    assert kwargs['reason'] == EQUITY_EXIT_REASON_STOP_LOSS


def test_a_missing_order_engine_places_nothing_and_is_reported(ctx, monkeypatch):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    reset_exit_placer()
    monkeypatch.setattr(monitor_module, 'EXIT_PLACER_CANDIDATES', ())
    assert resolve_exit_placer() == (None, None)

    _tick(ctx)

    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_ACTIVE
    assert refreshed.exit_broker_order_id is None
    assert refreshed.sl_hit_at is not None, 'the breach is still on record'

    attempt = equity_exit_monitor._attempts[holding.id]
    assert attempt.succeeded is False
    assert 'exit placer' in (attempt.last_error or '').lower()


def test_a_placer_that_cannot_take_a_holding_id_is_refused(ctx):
    _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    calls = []

    def wrong_shape(order_id=None):
        calls.append(order_id)
        return {'success': True}

    set_exit_placer(wrong_shape, name='wrong')
    _tick(ctx)

    assert calls == [], 'the monitor must not call a helper it cannot address'


def test_a_placer_taking_only_some_arguments_still_works(ctx):
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    seen = []

    def narrow(holding_id, user_id, reason):
        seen.append((holding_id, user_id, reason))
        return True

    set_exit_placer(narrow, name='narrow')
    _tick(ctx)

    assert seen == [(holding.id, ctx['user_id'], EQUITY_EXIT_REASON_STOP_LOSS)]


def test_set_exit_placer_rejects_a_non_callable(ctx):
    with pytest.raises(TypeError):
        set_exit_placer('app.utils.equity_order_engine.place')


def test_status_is_safe_before_the_monitor_has_ever_run(ctx):
    equity_exit_monitor.stop(wait=True)
    status = equity_exit_monitor.status()

    assert status['is_running'] is False
    assert status['scheduler_job_id'] == 'equity_exit_monitor'
    assert status['exits_in_flight'] == 0
    assert 'totals' in status and 'last_tick' in status


def test_run_checks_without_an_app_context_does_not_raise():
    """
    The scheduler job has to be wrapped in an app context by its caller. If it
    is not, the monitor says so rather than throwing out of the scheduler.
    """
    monitor = EquityExitMonitor()
    was_running = monitor.is_running
    monitor.is_running = True
    try:
        monitor.run_checks()
    finally:
        monitor.is_running = was_running


def test_two_holdings_are_handled_independently(ctx):
    first = _make_holding(ctx, symbol='RELIANCE', stop_loss=950.0,
                          exit_mode=EQUITY_EXIT_MODE_AUTO)
    second = _make_holding(ctx, symbol='TCS', avg_cost=3000.0, target=3500.0,
                           exit_mode=EQUITY_EXIT_MODE_CONFIRM)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0
    ctx['feed'].prices[('TCS', 'NSE')] = 3600.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')
    _tick(ctx)

    assert [call['holding_id'] for call in placer.calls] == [first.id]
    assert _reload(second.id).exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM


def test_one_broken_holding_does_not_stop_the_others(ctx):
    good = _make_holding(ctx, symbol='RELIANCE', stop_loss=950.0,
                         exit_mode=EQUITY_EXIT_MODE_AUTO)
    bad = _make_holding(ctx, symbol='TCS', avg_cost=3000.0, stop_loss=2900.0,
                        exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0
    ctx['feed'].prices[('TCS', 'NSE')] = 2800.0

    calls = []

    def flaky(holding_id=None, user_id=None, reason=None, **kwargs):
        calls.append(holding_id)
        if holding_id == bad.id:
            raise RuntimeError('this account is unreachable')
        EquityHolding.claim_for_exit(holding_id, user_id, reason)
        EquityHolding.mark_exit_submitted(holding_id, user_id, 'BROKER-OK')
        return {'success': True}

    set_exit_placer(flaky, name='flaky')
    _tick(ctx)

    assert sorted(calls) == sorted([good.id, bad.id])
    assert _reload(good.id).exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED


def test_the_real_dispatch_pool_places_the_exit_off_the_tick_thread(ctx):
    """
    Everything above runs the dispatch inline for determinism. This one uses
    the bounded pool the monitor actually ships with, so the app context per
    worker and the plain values crossing the thread boundary are exercised too.
    """
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    seen_threads = []

    def threaded_placer(holding_id=None, user_id=None, reason=None, **kwargs):
        seen_threads.append(threading.current_thread().name)
        EquityHolding.claim_for_exit(holding_id, user_id, reason)
        EquityHolding.mark_exit_submitted(holding_id, user_id, 'BROKER-T')
        return {'success': True}

    set_exit_placer(threaded_placer, name='threaded')

    equity_exit_monitor.stop(wait=True)
    equity_exit_monitor.inline_exits = False
    equity_exit_monitor.start()
    try:
        _tick(ctx)
        # stop(wait=True) drains the pool, which is how a shutdown lets a
        # worker finish writing its broker order id back.
        equity_exit_monitor.stop(wait=True)
    finally:
        equity_exit_monitor.inline_exits = True

    assert len(seen_threads) == 1
    assert seen_threads[0].startswith('equity-exit')
    assert seen_threads[0] != threading.current_thread().name

    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
    assert refreshed.exit_broker_order_id == 'BROKER-T'


def test_end_to_end_through_the_real_order_engine_with_a_fake_broker(ctx):
    """
    The monitor driving the real app.utils.equity_order_engine.exit_holding,
    with only the broker faked.

    This is the one that proves the two halves fit: the monitor detects and
    records, the engine claims, places and writes the order id back, and the
    holding ends up in a state the monitor will never act on again.
    """
    from functools import partial

    from app.utils import equity_order_engine

    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placed = []

    class FakeBroker:
        def __init__(self, credential):
            self.credential = credential

        def placeorder(self, **params):
            placed.append(params)
            return {'status': 'success', 'orderid': 'NSE-42'}

    set_exit_placer(
        partial(equity_order_engine.exit_holding, client_factory=FakeBroker),
        name='real engine, fake broker',
    )

    _tick(ctx)

    assert len(placed) == 1
    params = placed[0]
    assert params['action'] == 'SELL'
    assert params['product'] == 'CNC', 'equity delivery is always CNC'
    assert params['price_type'] == 'MARKET'
    assert params['symbol'] == 'RELIANCE'
    assert params['quantity'] == 100

    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
    assert refreshed.exit_broker_order_id == 'NSE-42'
    assert refreshed.exit_reason == EQUITY_EXIT_REASON_STOP_LOSS

    # And nothing sends a second order on the ticks that follow.
    for _ in range(3):
        _tick(ctx)
    assert len(placed) == 1


def test_a_holding_claimed_mid_tick_is_left_to_the_other_caller(ctx):
    """
    A manual Sell can claim a holding between the monitor's scan and its
    breach handling. The monitor has to stand down rather than record a breach
    onto a row that is already on its way out.
    """
    holding = _make_holding(ctx, stop_loss=950.0, exit_mode=EQUITY_EXIT_MODE_AUTO)
    ctx['feed'].prices[('RELIANCE', 'NSE')] = 940.0

    placer = FakePlacer()
    set_exit_placer(placer, name='fake')

    original = monitor_module.EquityExitMonitor._prices_for

    def claim_then_price(holdings):
        # Stand in for a request thread claiming the row while the tick runs.
        EquityHolding.claim_for_exit(
            holding.id, ctx['user_id'], EQUITY_EXIT_REASON_STOP_LOSS
        )
        EquityHolding.mark_exit_submitted(holding.id, ctx['user_id'], 'MANUAL-1')
        return original(holdings)

    monitor_module.EquityExitMonitor._prices_for = staticmethod(claim_then_price)
    try:
        _tick(ctx)
    finally:
        monitor_module.EquityExitMonitor._prices_for = staticmethod(original)

    assert placer.calls == []
    refreshed = _reload(holding.id)
    assert refreshed.exit_status == EQUITY_HOLDING_STATUS_EXIT_SUBMITTED
    assert refreshed.exit_broker_order_id == 'MANUAL-1'
    assert refreshed.sl_hit_at is None, 'no breach is written onto a claimed row'


# ---------------------------------------------------------------------------
# Recovery from a crash that stranded an exit claim.
# ---------------------------------------------------------------------------

class TestStaleExitClaimRecovery:
    """
    EXIT_PENDING means "claimed, committed, the broker call is running". If the
    process dies in that window nothing ever moves the row on, and the monitor
    only evaluates ACTIVE rows, so the holding's stop loss silently stops being
    watched. It is the quietest way to lose a stop.

    Recovery goes to EXIT_INDETERMINATE, never back to ACTIVE: the crashed
    process may have reached the broker before it died. Releasing to ACTIVE
    would let the monitor sell the same shares again, which is the exact failure
    the claim exists to prevent.
    """

    def test_a_stranded_claim_is_recovered(self, ctx):
        holding = _make_holding(ctx, stop_loss=900.0)
        from app.models import (
            EquityHolding,
            EQUITY_HOLDING_STATUS_EXIT_PENDING,
            EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE,
        )
        holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_claimed_at = datetime.utcnow() - timedelta(hours=2)
        holding.exit_broker_order_id = None
        db.session.commit()

        assert EquityHolding.recover_stale_exit_claims() == 1

        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE
        assert 'broker' in (holding.exit_error or '').lower()

    def test_recovery_never_returns_a_row_to_active(self, ctx):
        holding = _make_holding(ctx, stop_loss=900.0)
        """ACTIVE would re-arm the monitor against a possibly live order."""
        from app.models import (
            EquityHolding,
            EQUITY_HOLDING_STATUS_EXIT_PENDING,
            EQUITY_HOLDING_STATUS_ACTIVE,
        )
        holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_claimed_at = datetime.utcnow() - timedelta(hours=2)
        db.session.commit()

        EquityHolding.recover_stale_exit_claims()

        db.session.refresh(holding)
        assert holding.exit_status != EQUITY_HOLDING_STATUS_ACTIVE

    def test_a_fresh_claim_is_left_alone(self, ctx):
        holding = _make_holding(ctx, stop_loss=900.0)
        """A claim seconds old is a broker call that is genuinely running."""
        from app.models import EquityHolding, EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_claimed_at = datetime.utcnow()
        db.session.commit()

        assert EquityHolding.recover_stale_exit_claims() == 0

        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_PENDING

    def test_a_claim_that_reached_the_broker_is_left_alone(self, ctx):
        holding = _make_holding(ctx, stop_loss=900.0)
        """With an order id the fill poller owns it, not this."""
        from app.models import EquityHolding, EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_status = EQUITY_HOLDING_STATUS_EXIT_PENDING
        holding.exit_claimed_at = datetime.utcnow() - timedelta(hours=2)
        holding.exit_broker_order_id = 'OID-LIVE'
        db.session.commit()

        assert EquityHolding.recover_stale_exit_claims() == 0

    def test_an_active_holding_is_never_touched(self, ctx):
        holding = _make_holding(ctx, stop_loss=900.0)
        from app.models import EquityHolding, EQUITY_HOLDING_STATUS_ACTIVE
        holding.exit_status = EQUITY_HOLDING_STATUS_ACTIVE
        db.session.commit()

        assert EquityHolding.recover_stale_exit_claims() == 0

        db.session.refresh(holding)
        assert holding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE

    def test_the_threshold_is_far_above_any_real_broker_call(self):
        """30s is the broker order timeout; the default must not catch one."""
        import inspect as _inspect
        from app.models import EquityHolding
        default = _inspect.signature(
            EquityHolding.recover_stale_exit_claims
        ).parameters['older_than_seconds'].default
        assert default >= 300
