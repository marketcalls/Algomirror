"""
Equity Stop Loss and Target Monitor

Watches every equity holding that carries a stop loss or a target and acts the
moment the live price goes through the level.

Why this module exists at all
-----------------------------
The F&O module enforces leg level stop loss and take profit inside a Server
Sent Events generator in app/trading/routes.py. That means the protection is
only alive while a browser tab is open on that page, and the always-on 5 second
risk manager does not implement those levels at all. Close the tab and the stop
loss stops existing. That defect must not be reproduced here, so this monitor:

- holds no browser state and reads nothing from a request,
- runs as a plain callable driven by the existing APScheduler in
  app/utils/background_service.py, the same way risk_manager.run_risk_checks is,
- keeps its heartbeat in the database (EquitySetting.monitor_last_run_at) so the
  Settings screen can prove it is running with nothing open.

This module never places an order itself. Placing an equity sell means claiming
the holding first (lock the row, re-check, commit the claim, only then call the
broker), and that sequence lives in exactly one place: the equity order engine's
shared claim-and-place helper. See EXIT_PLACER_CONTRACT below and
set_exit_placer(). Duplicating the claim sequence here is what the shared helper
exists to prevent.

Safety rules this module is built around
---------------------------------------
Refuse to act on unreliable data. app/utils/risk_manager.py bails out of every
check when prices_unreliable is set, and this monitor mirrors that discipline: a
holding with no fresh pushed price is skipped, never exited on a stale or
missing number, and never exited on the last_price column carried in the
database.

Act once per armed level. EquityHolding.record_breach returns True only the
first time a level is breached, and that return value is the de-duplication
guard. A holding that has already been recorded does not alert again on the next
tick and is not sold again.

Never retry into a duplicate order. A retry can only ever happen when the
database itself still shows the holding as ACTIVE with no broker order id, which
is only true when the previous attempt definitely did not reach the broker. If a
previous attempt claimed the row, submitted an order, or ended indeterminate,
the row is no longer monitorable and no retry is possible regardless of what
this module thinks happened.
"""

import inspect
import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime
from typing import Dict, List, Optional

import pytz
from flask import current_app, has_app_context
from sqlalchemy import or_

from app import db
from app.models import (
    ActivityLog,
    EquityHolding,
    EquitySetting,
    EQUITY_EXIT_MODE_AUTO,
    EQUITY_EXIT_MODE_CONFIRM,
    EQUITY_EXIT_REASON_STOP_LOSS,
    EQUITY_EXIT_REASON_TARGET,
    EQUITY_HOLDING_STATUS_ACTIVE,
    EQUITY_ORDER_SOURCE_STOP_LOSS,
    EQUITY_ORDER_SOURCE_TARGET,
)
from app.utils.equity_price_feed import equity_price_feed

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')


# ----------------------------------------------------------------------------
# Tuning
# ----------------------------------------------------------------------------

# What the scheduler job should be created with. The monitor does not register
# itself, the app factory does, so these are the numbers to wire it with.
SCHEDULER_JOB_ID = 'equity_exit_monitor'
SCHEDULER_INTERVAL_SECONDS = 5

# Per user pacing, read from EquitySetting.sl_monitor_interval_seconds and
# clamped into this range so a bad settings value cannot either hammer the
# feed or silently disable the monitor for hours.
MIN_MONITOR_INTERVAL_SECONDS = 1
MAX_MONITOR_INTERVAL_SECONDS = 300
DEFAULT_MONITOR_INTERVAL_SECONDS = 30

# Gate before an AUTO exit that definitely did not reach the broker is tried
# again, matching the 10 second guard risk_manager puts in front of a stuck
# trigger so a wedged attempt cannot be retried several times a second.
AUTO_EXIT_RETRY_SECONDS = 10

# How many times one holding's AUTO exit is attempted before the monitor stops
# trying and leaves it for the admin. Without a cap a permanently broken
# account would be retried every 10 seconds for the rest of the session.
MAX_AUTO_EXIT_ATTEMPTS = 5

# Exits are dispatched to a small bounded pool so one slow broker call cannot
# hold up the stop loss on every other holding.
MAX_EXIT_WORKERS = 4

# A pushed price this far away from the average cost is treated as a feed
# glitch (a decimal shift, a wrong symbol mapping) rather than a real move, and
# the holding is skipped instead of sold. The bounds are deliberately absurd:
# no real Indian equity travels through them in one session under circuit
# limits, so a genuine crash or a genuine gap still triggers normally.
PRICE_SANITY_MIN_MULTIPLE = 0.01
PRICE_SANITY_MAX_MULTIPLE = 100.0


# ----------------------------------------------------------------------------
# The exit placer: this module's one dependency on the order engine
# ----------------------------------------------------------------------------

EXIT_PLACER_CONTRACT = """
The exit placer is the equity order engine's shared claim-and-place helper. It
is called with keyword arguments and is expected to do all of the following,
because this monitor does none of it:

    EquityHolding.claim_for_exit(...)   lock, re-check, commit the claim
    the broker call                     placeorder, CNC, SELL, one account
    EquityHolding.mark_exit_submitted   the moment an order id is known
    EquityHolding.mark_exit_indeterminate  on a timeout or a dropped connection
    EquityHolding.release_exit_claim    on a definite rejection only

Keyword arguments offered. A placer only has to accept the ones it wants: this
module inspects the signature and passes the intersection, so adding an
argument here does not break an existing placer.

    holding_id      int, EquityHolding.id, always passed
    user_id         int, owner, always passed, keeps the placer ownership scoped
    account_id      int, the TradingAccount the holding sits in
    symbol          str
    exchange        str
    quantity        None, meaning claim and sell the whole sellable quantity.
                    Deliberately not a number read a moment earlier: the claim
                    reads the fresh sellable quantity under its own row lock.
    reason          EQUITY_EXIT_REASON_STOP_LOSS or EQUITY_EXIT_REASON_TARGET
    source          EQUITY_ORDER_SOURCE_STOP_LOSS or EQUITY_ORDER_SOURCE_TARGET
    breach_price    float, the live price that went through the level
    level_price     float, the stop loss or target that was breached

breach_price is named that way on purpose. The obvious name, trigger_price,
already means the GTT trigger on equity_order_engine.exit_holding, and matching
by name would have handed a market exit a GTT trigger it never asked for. Any
argument added here must not collide with an order parameter that means
something else, and test_the_real_order_engine_only_receives_reviewed_arguments
is there to catch it if one ever does.

Return value. Any of:
    {'success': True}                or {'status': 'success'}
    {'success': False, 'message': ...} or {'status': 'error', 'error': ...}
    True / False
    an object with a .success attribute
Anything else is recorded as a failed attempt. That is safe: a retry is gated
on the database still showing the holding ACTIVE with no broker order id, so a
misread return value cannot produce a second order.
"""

# Where the default placer is looked up when nothing has been injected with
# set_exit_placer(). Kept to the one real entry point rather than a list of
# plausible names: guessing which function to call is not a safe way to decide
# what places an order. Injecting one with set_exit_placer() is preferred.
EXIT_PLACER_CANDIDATES = (
    ('app.utils.equity_order_engine', 'exit_holding'),
)

_placer_lock = threading.Lock()
_injected_placer = None
_injected_placer_name = None


def set_exit_placer(placer, name=None):
    """
    Point the monitor at the order engine's claim-and-place helper.

    Call this from the order engine or from the app factory. Passing None
    restores the default lookup over EXIT_PLACER_CANDIDATES.

    Args:
        placer: callable following EXIT_PLACER_CONTRACT, or None to reset
        name: label for status() and logs, defaults to the callable's qualname
    """
    global _injected_placer, _injected_placer_name

    if placer is not None and not callable(placer):
        raise TypeError('exit placer must be callable, got %r' % (type(placer),))

    with _placer_lock:
        _injected_placer = placer
        if placer is None:
            _injected_placer_name = None
        else:
            _injected_placer_name = name or getattr(placer, '__qualname__', repr(placer))

    logger.info('[EQUITY_EXIT] Exit placer set to %s', _injected_placer_name or 'default lookup')


def reset_exit_placer():
    """Forget an injected placer and go back to the default lookup."""
    set_exit_placer(None)


def resolve_exit_placer():
    """
    Return (placer, name) for the callable that will place exits, or
    (None, None) when the order engine is not available yet.

    Never raises. A missing order engine is reported as a failed attempt, which
    is retried later, rather than crashing the scheduler tick.
    """
    with _placer_lock:
        placer = _injected_placer
        name = _injected_placer_name

    if placer is not None:
        return placer, name

    import importlib

    for module_name, attribute in EXIT_PLACER_CANDIDATES:
        try:
            module = importlib.import_module(module_name)
        except ImportError:
            continue
        except Exception as exc:
            logger.error('[EQUITY_EXIT] Failed importing %s: %s', module_name, exc)
            continue

        candidate = getattr(module, attribute, None)
        if callable(candidate):
            return candidate, '%s.%s' % (module_name, attribute)

    return None, None


def _placer_kwargs(placer, payload):
    """
    Narrow the offered keyword arguments to the ones this placer accepts.

    Done by inspecting the signature before the call, never by catching a
    TypeError afterwards: a TypeError raised inside the placer could arrive
    after an order was already sent, and retrying that would be a duplicate.

    Returns None when the callable cannot take holding_id and user_id, which
    means it is not a claim-and-place helper and must not be called.
    """
    try:
        signature = inspect.signature(placer)
    except (TypeError, ValueError):
        # Builtins and some C callables have no signature. Offer everything and
        # let the placer decide.
        return dict(payload)

    parameters = signature.parameters
    accepts_any = any(
        parameter.kind is inspect.Parameter.VAR_KEYWORD
        for parameter in parameters.values()
    )
    if accepts_any:
        return dict(payload)

    kwargs = {key: value for key, value in payload.items() if key in parameters}
    if 'holding_id' not in kwargs or 'user_id' not in kwargs:
        return None
    return kwargs


def _interpret_placer_result(result):
    """
    Reduce a placer return value to (success, message). See
    EXIT_PLACER_CONTRACT for the shapes accepted.
    """
    if result is None:
        return False, 'Exit placer returned no result'

    if isinstance(result, bool):
        return result, None if result else 'Exit placer reported failure'

    if isinstance(result, dict):
        if 'success' in result:
            success = bool(result.get('success'))
        elif 'status' in result:
            success = str(result.get('status')).lower() == 'success'
        else:
            return False, 'Exit placer returned an unrecognised result: %r' % (result,)
        message = result.get('message') or result.get('error') or result.get('error_message')
        return success, message

    success = getattr(result, 'success', None)
    if isinstance(success, bool):
        return success, getattr(result, 'message', None)

    return False, 'Exit placer returned an unrecognised result: %r' % (result,)


# ----------------------------------------------------------------------------
# Attempt ledger
# ----------------------------------------------------------------------------

class _ExitAttempt:
    """
    What this process knows about one holding's AUTO exit attempts.

    Kept in memory on purpose. It is the only thing that turns a recorded
    breach into a retry, and losing it on a restart means the monitor does
    nothing rather than guessing, which is the safe direction. The database
    cannot serve this role: a holding that is back to ACTIVE with its breach
    still recorded looks identical whether the previous attempt was rejected,
    partially filled, or declined by the admin, and re-selling a partially
    filled holding is exactly the mistake to avoid.
    """

    __slots__ = (
        'user_id', 'reason', 'attempts', 'last_attempt_at',
        'in_progress', 'succeeded', 'given_up', 'last_error',
    )

    def __init__(self, user_id, reason):
        self.user_id = user_id
        self.reason = reason
        self.attempts = 0
        self.last_attempt_at = 0.0
        self.in_progress = False
        self.succeeded = False
        self.given_up = False
        self.last_error = None


# ----------------------------------------------------------------------------
# The monitor
# ----------------------------------------------------------------------------

class EquityExitMonitor:
    """
    Singleton stop loss and target monitor for equity holdings.

    Public surface:
        start()      arm the monitor, create the exit dispatch pool
        stop()       disarm it and drain in-flight exits
        run_checks() one tick, the callable the scheduler drives
        status()     diagnostics, safe to call at any time
    """

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self.is_running = False

        # Run the exit dispatch on the calling thread instead of the pool.
        # Used by the tests, and usable as a diagnostic switch.
        self.inline_exits = False

        self._lock = threading.RLock()
        self._executor: Optional[ThreadPoolExecutor] = None

        # holding_id to _ExitAttempt
        self._attempts: Dict[int, _ExitAttempt] = {}
        # holding_id set, so the "breached but nothing in flight" warning is
        # logged once per holding rather than on every tick
        self._orphan_warned = set()

        self._last_run_at: Optional[datetime] = None
        self._last_run_duration_ms: Optional[float] = None
        self._last_error: Optional[str] = None
        self._last_skip_reason: Optional[str] = None

        self._stats = {
            'runs': 0,
            'runs_skipped_market_closed': 0,
            'users_evaluated': 0,
            'holdings_evaluated': 0,
            'holdings_without_price': 0,
            'holdings_price_rejected': 0,
            'holdings_misconfigured': 0,
            'breaches_recorded': 0,
            'confirm_alerts_raised': 0,
            'auto_exits_dispatched': 0,
            'auto_exits_placed': 0,
            'auto_exit_failures': 0,
            'auto_exits_given_up': 0,
        }
        # Snapshot of the last tick only, so status() shows the current picture
        # rather than a running total.
        self._last_tick = {
            'users_evaluated': 0,
            'holdings_evaluated': 0,
            'holdings_without_price': 0,
            'breaches_recorded': 0,
        }

        logger.debug('[EQUITY_EXIT] Monitor initialised')

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def start(self):
        """
        Arm the monitor and create the bounded exit dispatch pool.

        Registering the scheduler job is the app factory's business, not this
        module's. Until start() is called run_checks() returns immediately, so
        a job that is scheduled early simply does nothing.
        """
        with self._lock:
            if self.is_running:
                logger.debug('[EQUITY_EXIT] Monitor already running')
                return

            if self._executor is None and not self.inline_exits:
                self._executor = ThreadPoolExecutor(
                    max_workers=MAX_EXIT_WORKERS,
                    thread_name_prefix='equity-exit'
                )

            self.is_running = True
            self._last_error = None

        logger.info('[EQUITY_EXIT] Stop loss and target monitor started')

    def stop(self, wait=True):
        """
        Disarm the monitor.

        Args:
            wait: block until in-flight exits finish (the default). A worker
                that is mid-placement holds a claimed holding and has not yet
                written the broker order id back, so letting it finish is
                what keeps the claim and the broker in step.
        """
        with self._lock:
            if not self.is_running:
                return
            self.is_running = False
            executor = self._executor
            self._executor = None

        if executor is not None:
            try:
                executor.shutdown(wait=bool(wait))
            except Exception as exc:
                logger.error('[EQUITY_EXIT] Error shutting down exit pool: %s', exc)

        with self._lock:
            self._attempts.clear()
            self._orphan_warned.clear()

        logger.info('[EQUITY_EXIT] Stop loss and target monitor stopped')

    # ------------------------------------------------------------------
    # One tick
    # ------------------------------------------------------------------

    def run_checks(self):
        """
        Evaluate every monitorable holding once. This is the callable the
        scheduler drives, wrapped by the caller in a Flask app context exactly
        the way BackgroundService.run_risk_checks wraps risk_manager.

        Never raises: a scheduler job that throws is a scheduler job that
        eventually stops being scheduled.
        """
        if not self.is_running:
            return

        if not has_app_context():
            logger.error(
                '[EQUITY_EXIT] run_checks called with no Flask app context. '
                'Schedule it inside "with app.app_context():".'
            )
            return

        app = current_app._get_current_object()
        started = time.monotonic()
        self._last_skip_reason = None

        tick = {
            'users_evaluated': 0,
            'holdings_evaluated': 0,
            'holdings_without_price': 0,
            'breaches_recorded': 0,
            'stale_claims_recovered': 0,
        }

        try:
            # Force fresh reads. Another thread may have edited a level, or
            # claimed a holding, since this session last looked.
            db.session.expire_all()

            # Rescue anything a crash stranded in EXIT_PENDING before deciding
            # there is nothing to do. Those rows are invisible to
            # _monitorable_user_ids (only ACTIVE is monitorable), so a stranded
            # holding would otherwise sit unwatched for ever and an early return
            # below would skip the sweep that could have found it.
            try:
                recovered = EquityHolding.recover_stale_exit_claims()
                if recovered:
                    tick['stale_claims_recovered'] = recovered
                    logger.warning(
                        '[EQUITY_EXIT] Recovered %d holding(s) stranded in '
                        'EXIT_PENDING by a stopped process. Marked indeterminate '
                        'for review, never resent.', recovered
                    )
            except Exception as exc:
                logger.error(
                    '[EQUITY_EXIT] Stale exit claim recovery failed: %s', exc,
                    exc_info=True
                )
                self._safe_rollback()

            user_ids = self._monitorable_user_ids()
            if not user_ids:
                self._last_skip_reason = 'no holdings with a stop loss or target'
                return

            market_open = self._is_within_trading_hours()
            if not market_open:
                self._stats['runs_skipped_market_closed'] += 1
                self._last_skip_reason = 'outside trading hours'

            for user_id in user_ids:
                try:
                    self._check_user(app, user_id, market_open, tick)
                except Exception as exc:
                    logger.error(
                        '[EQUITY_EXIT] User %s pass failed: %s', user_id, exc,
                        exc_info=True
                    )
                    self._safe_rollback()
                    self._record_user_error(user_id, str(exc))

            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            logger.error('[EQUITY_EXIT] Monitor tick failed: %s', exc, exc_info=True)
            self._safe_rollback()
        finally:
            self._stats['runs'] += 1
            self._last_run_at = datetime.utcnow()
            self._last_run_duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            self._last_tick = tick

    def _check_user(self, app, user_id, market_open, tick):
        """
        One user's pass. Every query below is scoped on user_id, which is the
        application's only authorization mechanism.
        """
        settings = EquitySetting.get_or_create(user_id)
        if settings is None:
            logger.error('[EQUITY_EXIT] No equity settings for user %s', user_id)
            return

        if not settings.sl_monitor_enabled:
            return

        interval = self._interval_for(settings)
        if not self._is_due(settings, interval):
            return

        # The heartbeat is written before the work, not after, so the pacing
        # gate is consumed even if this pass throws. Settings can then show a
        # monitor that is alive but failing, instead of one that looks dead.
        note = None if market_open else 'Market closed, nothing evaluated'
        self._write_heartbeat(settings, note)

        if not market_open:
            return

        holdings = self._monitorable_holdings(user_id)
        tick['users_evaluated'] += 1
        self._stats['users_evaluated'] += 1

        if not holdings:
            self._prune_attempts(user_id, set())
            return

        live_ids = {holding.id for holding in holdings}
        self._prune_attempts(user_id, live_ids)

        prices = self._prices_for(holdings)

        for holding in holdings:
            try:
                self._check_holding(app, holding, prices, tick)
            except Exception as exc:
                logger.error(
                    '[EQUITY_EXIT] Holding %s (%s) check failed: %s',
                    holding.id, holding.symbol, exc, exc_info=True
                )
                self._safe_rollback()

    def _check_holding(self, app, holding, prices, tick):
        """
        Evaluate one holding against one live price and act on a breach.

        The holding arriving here is already ACTIVE, has at least one level set
        and has something sellable, because _monitorable_holdings filtered on
        exactly that.
        """
        tick['holdings_evaluated'] += 1
        self._stats['holdings_evaluated'] += 1

        # Belt and braces on the claim. A holding carrying a broker order id is
        # mid-exit whatever its status says, and must never be acted on.
        if holding.exit_broker_order_id:
            return

        stop_loss = holding.stop_loss
        target = holding.target

        # A stop loss at or above the target is not a level pair, it is bad
        # data, and acting on it would fire the wrong side. Skip and say so.
        if stop_loss is not None and target is not None and stop_loss >= target:
            self._stats['holdings_misconfigured'] += 1
            logger.warning(
                '[EQUITY_EXIT] Holding %s (%s) has stop loss %s at or above '
                'target %s, skipping until the levels are corrected',
                holding.id, holding.symbol, stop_loss, target
            )
            return

        key = (str(holding.symbol or '').strip().upper(),
               str(holding.exchange or 'NSE').strip().upper())
        price = prices.get(key)

        if price is None or price <= 0:
            # No fresh pushed price. This is the risk_manager prices_unreliable
            # discipline: a missing number is never a reason to sell, and the
            # stale last_price column is never used for a level decision.
            tick['holdings_without_price'] += 1
            self._stats['holdings_without_price'] += 1
            logger.debug(
                '[EQUITY_EXIT] Holding %s (%s:%s) has no live price, skipped',
                holding.id, key[1], key[0]
            )
            return

        if not self._price_is_sane(price, holding.avg_cost):
            self._stats['holdings_price_rejected'] += 1
            logger.warning(
                '[EQUITY_EXIT] Holding %s (%s) rejected price %s against '
                'average cost %s as a feed glitch, skipped',
                holding.id, holding.symbol, price, holding.avg_cost
            )
            return

        # Stop loss first: protecting capital outranks taking profit. With the
        # misconfiguration guard above, both levels can never breach at once.
        if stop_loss is not None and price <= float(stop_loss):
            self._handle_breach(
                app, holding, EQUITY_EXIT_REASON_STOP_LOSS,
                price, float(stop_loss), tick
            )
            return

        if target is not None and price >= float(target):
            self._handle_breach(
                app, holding, EQUITY_EXIT_REASON_TARGET,
                price, float(target), tick
            )

    def _handle_breach(self, app, holding, reason, price, level_price, tick):
        """
        A level is through. Record it once, then branch on the exit mode.
        """
        holding_id = holding.id
        user_id = holding.user_id
        account_id = holding.account_id
        symbol = holding.symbol
        exchange = holding.exchange
        exit_mode = holding.exit_mode

        # An unknown exit mode must never mean AUTO. A holding whose mode is
        # corrupt is treated as CONFIRM so it raises an alert instead of
        # selling itself.
        if exit_mode not in (EQUITY_EXIT_MODE_AUTO, EQUITY_EXIT_MODE_CONFIRM):
            logger.warning(
                '[EQUITY_EXIT] Holding %s (%s) has unknown exit mode %r, '
                'treating it as %s',
                holding_id, symbol, exit_mode, EQUITY_EXIT_MODE_CONFIRM
            )
            exit_mode = EQUITY_EXIT_MODE_CONFIRM

        is_auto = exit_mode == EQUITY_EXIT_MODE_AUTO

        # Expire the instance so the transition below genuinely re-reads the
        # row. SQLAlchemy hands a query back the copy it already holds, so a
        # status a request thread committed since this tick started would
        # otherwise be invisible to the re-check inside record_breach.
        try:
            db.session.expire(holding)
        except Exception:
            pass

        # Now read the refreshed row. A request thread can claim a holding
        # between the scan and here, and a claimed row is somebody else's sell.
        # The engine's own claim would refuse it, so this is belt to that
        # braces, but it also keeps the monitor from writing a breach onto a
        # row that is already on its way out.
        try:
            current_status = holding.exit_status
            current_order_id = holding.exit_broker_order_id
        except Exception as exc:
            logger.warning(
                '[EQUITY_EXIT] Holding %s vanished while it was being '
                'evaluated: %s', holding_id, exc
            )
            self._safe_rollback()
            return

        if current_status != EQUITY_HOLDING_STATUS_ACTIVE or current_order_id:
            logger.info(
                '[EQUITY_EXIT] Holding %s became %s while the tick was running, '
                'standing down', holding_id, current_status
            )
            return

        # record_breach returns True exactly once per armed level. That is the
        # whole idempotency guard: it commits the timestamp, and for a CONFIRM
        # mode holding it also moves the row into AWAITING_CONFIRM, which takes
        # the row out of the monitorable set entirely.
        recorded = EquityHolding.record_breach(holding_id, user_id, reason, price)

        if recorded:
            tick['breaches_recorded'] += 1
            self._stats['breaches_recorded'] += 1
            logger.warning(
                '[EQUITY_EXIT] %s breach on holding %s (%s:%s): price %s '
                'through level %s, exit mode %s',
                reason, holding_id, exchange, symbol, price, level_price, exit_mode
            )
            self._log_activity(
                user_id, account_id, reason, symbol, exchange,
                price, level_price, exit_mode
            )

            if not is_auto:
                self._stats['confirm_alerts_raised'] += 1
                # CONFIRM mode places nothing. record_breach has already parked
                # the holding in AWAITING_CONFIRM, which is the confirm queue
                # the Holdings screen reads. The admin approves or dismisses.
                return

            self._start_attempt(holding_id, user_id, reason)
            self._dispatch_exit(app, build_exit_payload(
                holding_id, user_id, account_id, symbol, exchange,
                reason, price, level_price
            ))
            return

        # The level was already recorded on an earlier tick.
        if not is_auto:
            # CONFIRM mode has already alerted, or the admin has dismissed it.
            # Either way, do not put the same alert back in front of them.
            return

        self._maybe_retry_auto_exit(app, holding, reason, price, level_price)

    def _maybe_retry_auto_exit(self, app, holding, reason, price, level_price):
        """
        Retry an AUTO exit that definitely did not reach the broker.

        Two independent gates have to agree before anything is sent again:

        The database. This holding is ACTIVE with no broker order id, which is
        only true when the previous attempt never claimed the row, or claimed
        it and had the claim released after a definite rejection. A claimed, a
        submitted or an indeterminate exit is not in the monitorable set at
        all, so it cannot reach this code.

        This process's attempt ledger. It must show that this monitor tried and
        failed. If there is no entry (a restart, a partial fill that reopened
        the row, a dismissed alert) nothing is retried, because in those cases
        the reason the row is back to ACTIVE is not a failure.
        """
        holding_id = holding.id

        with self._lock:
            attempt = self._attempts.get(holding_id)

            if attempt is None:
                if holding_id not in self._orphan_warned:
                    self._orphan_warned.add(holding_id)
                    logger.warning(
                        '[EQUITY_EXIT] Holding %s (%s) has a recorded %s breach '
                        'and %d sellable shares but no exit in flight and no '
                        'attempt on record in this process. Not auto-selling on '
                        'a guess: re-arm the level to act on it.',
                        holding_id, holding.symbol, reason, holding.sellable_quantity
                    )
                return

            if attempt.in_progress or attempt.succeeded or attempt.given_up:
                return

            if attempt.attempts >= MAX_AUTO_EXIT_ATTEMPTS:
                attempt.given_up = True
                self._stats['auto_exits_given_up'] += 1
                logger.error(
                    '[EQUITY_EXIT] Giving up on the AUTO exit for holding %s '
                    '(%s) after %d attempts. Last error: %s. The holding is '
                    'still open and needs an admin.',
                    holding_id, holding.symbol, attempt.attempts, attempt.last_error
                )
                return

            waited = time.monotonic() - attempt.last_attempt_at
            if waited < AUTO_EXIT_RETRY_SECONDS:
                return

            attempt.reason = reason

        logger.warning(
            '[EQUITY_EXIT] Retrying the AUTO exit for holding %s (%s), '
            'attempt %d of %d',
            holding_id, holding.symbol, attempt.attempts + 1, MAX_AUTO_EXIT_ATTEMPTS
        )
        self._dispatch_exit(app, build_exit_payload(
            holding_id, holding.user_id, holding.account_id,
            holding.symbol, holding.exchange, reason, price, level_price
        ))

    # ------------------------------------------------------------------
    # Exit dispatch
    # ------------------------------------------------------------------

    def _start_attempt(self, holding_id, user_id, reason):
        """Open a fresh ledger entry for a newly recorded breach."""
        with self._lock:
            self._attempts[holding_id] = _ExitAttempt(user_id, reason)
            self._orphan_warned.discard(holding_id)

    def _dispatch_exit(self, app, payload):
        """
        Hand one exit to the order engine's claim-and-place helper.

        Every value in the payload is a plain int, str or float read before the
        worker starts, so no ORM instance and no lazy load crosses the thread
        boundary. The worker opens its own app context, and therefore its own
        database session.
        """
        holding_id = payload['holding_id']

        with self._lock:
            attempt = self._attempts.get(holding_id)
            if attempt is None:
                attempt = _ExitAttempt(payload['user_id'], payload['reason'])
                self._attempts[holding_id] = attempt
            if attempt.in_progress:
                return
            attempt.in_progress = True
            attempt.attempts += 1
            attempt.last_attempt_at = time.monotonic()
            executor = self._executor

        self._stats['auto_exits_dispatched'] += 1

        if executor is None or self.inline_exits:
            self._run_exit(app, payload)
            return

        try:
            executor.submit(self._run_exit, app, payload)
        except RuntimeError as exc:
            # The pool was shut down between the read and the submit.
            self._finish_attempt(holding_id, False, 'Exit pool unavailable: %s' % exc)
            logger.error(
                '[EQUITY_EXIT] Could not dispatch the exit for holding %s: %s',
                holding_id, exc
            )

    def _run_exit(self, app, payload):
        """
        Worker body. Runs on the exit pool, or on the calling thread when
        inline_exits is set. Never raises out of the pool.
        """
        holding_id = payload['holding_id']
        success = False
        message = None

        try:
            with app.app_context():
                placer, placer_name = resolve_exit_placer()

                if placer is None:
                    message = (
                        'No equity exit placer is available. The order engine '
                        'has not registered its claim-and-place helper.'
                    )
                    logger.error('[EQUITY_EXIT] %s Holding %s not exited.',
                                 message, holding_id)
                else:
                    kwargs = _placer_kwargs(placer, payload)
                    if kwargs is None:
                        message = (
                            'Exit placer %s does not accept holding_id and '
                            'user_id, refusing to call it' % placer_name
                        )
                        logger.error('[EQUITY_EXIT] %s', message)
                    else:
                        result = placer(**kwargs)
                        success, message = _interpret_placer_result(result)
                        if success:
                            logger.info(
                                '[EQUITY_EXIT] Exit placed for holding %s (%s:%s) '
                                'via %s, reason %s',
                                holding_id, payload['exchange'], payload['symbol'],
                                placer_name, payload['reason']
                            )
                        else:
                            logger.error(
                                '[EQUITY_EXIT] Exit for holding %s (%s:%s) via %s '
                                'did not go through: %s',
                                holding_id, payload['exchange'], payload['symbol'],
                                placer_name, message
                            )
        except Exception as exc:
            # The placer owns the claim, so whatever state it left behind is
            # already in the database. If it claimed the row, the holding is no
            # longer monitorable and nothing here can retry it. If it never
            # claimed, the row is untouched and a retry is safe.
            success = False
            message = 'Exit placer raised: %s' % exc
            logger.error(
                '[EQUITY_EXIT] Exit placer raised for holding %s: %s',
                holding_id, exc, exc_info=True
            )
        finally:
            self._finish_attempt(holding_id, success, message)

    def _finish_attempt(self, holding_id, success, message):
        with self._lock:
            attempt = self._attempts.get(holding_id)
            if attempt is not None:
                attempt.in_progress = False
                attempt.succeeded = bool(success)
                attempt.last_error = message

        if success:
            self._stats['auto_exits_placed'] += 1
        else:
            self._stats['auto_exit_failures'] += 1

    def _prune_attempts(self, user_id, live_holding_ids):
        """
        Drop ledger entries for this user's holdings that are no longer
        monitorable, unless a worker is still inside one.

        Dropping an entry is the conservative move: without it a holding that
        comes back to ACTIVE (a partial fill, a dismissed alert) is never
        retried, which is exactly what mark_exit_completed's docstring asks
        for when it keeps the breach records after a partial exit.
        """
        with self._lock:
            stale = [
                holding_id
                for holding_id, attempt in self._attempts.items()
                if attempt.user_id == user_id
                and holding_id not in live_holding_ids
                and not attempt.in_progress
            ]
            for holding_id in stale:
                self._attempts.pop(holding_id, None)
                self._orphan_warned.discard(holding_id)

    # ------------------------------------------------------------------
    # Queries, all ownership scoped
    # ------------------------------------------------------------------

    @staticmethod
    def _monitorable_user_ids() -> List[int]:
        """
        Owners with at least one holding worth looking at.

        This is the one query that is not filtered on a user, because a
        background job has no current_user and has to discover whose rows to
        scan. It selects nothing but the owner column, and every query after it
        is scoped on the id it returns.
        """
        rows = db.session.query(EquityHolding.user_id).filter(
            EquityHolding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE,
            EquityHolding.quantity > 0,
            or_(
                EquityHolding.stop_loss.isnot(None),
                EquityHolding.target.isnot(None),
            ),
        ).distinct().all()

        return [row[0] for row in rows if row[0] is not None]

    @staticmethod
    def _monitorable_holdings(user_id) -> List[EquityHolding]:
        """
        This user's holdings that the monitor should evaluate on this tick.

        The status filter is what makes the whole module idempotent: a holding
        that is AWAITING_CONFIRM, EXIT_PENDING, EXIT_SUBMITTED,
        EXIT_INDETERMINATE or EXITED is not ACTIVE and never reaches an
        evaluation, so it can neither re-alert nor be sold twice.
        """
        # populate_existing() is not decoration. Without it a row this session
        # already holds is returned from the identity map with the values it had
        # when it was first loaded, and a status another thread committed in the
        # meantime is invisible. An exit decision taken on that stale copy is
        # exactly the mistake the claim exists to prevent.
        candidates = EquityHolding.query.filter(
            EquityHolding.user_id == user_id,
            EquityHolding.exit_status == EQUITY_HOLDING_STATUS_ACTIVE,
            EquityHolding.quantity > 0,
            or_(
                EquityHolding.stop_loss.isnot(None),
                EquityHolding.target.isnot(None),
            ),
        ).populate_existing().all()

        # sellable_quantity subtracts pledged shares, which is a Python side
        # property, so the last filter happens here.
        return [holding for holding in candidates if holding.is_monitorable]

    # ------------------------------------------------------------------
    # Prices
    # ------------------------------------------------------------------

    @staticmethod
    def _prices_for(holdings) -> Dict[tuple, float]:
        """
        Live prices for the symbols in this pass, from the shared WebSocket
        feed. No REST poll per symbol, and no broker call at all.

        A symbol the feed has no fresh price for is simply absent from the
        result, and the caller skips that holding. The feed already drops a
        price older than its own age ceiling, so an absent key means "not
        trustworthy" as well as "not known".
        """
        keys = sorted({
            (str(holding.symbol or '').strip().upper(),
             str(holding.exchange or 'NSE').strip().upper())
            for holding in holdings
            if holding.symbol
        })
        if not keys:
            return {}

        try:
            return equity_price_feed.prime(keys) or {}
        except Exception as exc:
            # A broken feed means no prices, which means no exits. That is the
            # correct failure direction.
            logger.error('[EQUITY_EXIT] Price feed unavailable: %s', exc)
            return {}

    @staticmethod
    def _price_is_sane(price, avg_cost) -> bool:
        """
        Reject a price that cannot be a real move on this holding.

        Guards against a decimal shift or a crossed symbol mapping in the feed
        turning into a stop loss sell. Skipped when there is no average cost to
        compare against.
        """
        try:
            price = float(price)
        except (TypeError, ValueError):
            return False

        if price <= 0:
            return False

        try:
            reference = float(avg_cost or 0)
        except (TypeError, ValueError):
            return True

        if reference <= 0:
            return True

        return (
            reference * PRICE_SANITY_MIN_MULTIPLE
            <= price
            <= reference * PRICE_SANITY_MAX_MULTIPLE
        )

    # ------------------------------------------------------------------
    # Trading hours
    # ------------------------------------------------------------------

    @staticmethod
    def _is_within_trading_hours() -> bool:
        """
        True inside a configured trading session, mirroring
        risk_manager._is_within_trading_hours including its fail-open on a
        database error: if the session tables cannot be read, nothing else in
        this tick would work either, so blocking the monitor buys nothing.
        """
        try:
            from app.models import MarketHoliday, TradingHoursTemplate, TradingSession

            now = datetime.now(IST)
            current_time = now.time()
            day_of_week = now.weekday()

            if MarketHoliday.query.filter_by(holiday_date=now.date()).first():
                return False

            sessions = TradingSession.query.join(TradingHoursTemplate).filter(
                TradingSession.day_of_week == day_of_week,
                TradingSession.is_active == True,
                TradingHoursTemplate.is_active == True
            ).all()

            for session in sessions:
                if session.start_time <= current_time <= session.end_time:
                    return True

            return False

        except Exception as exc:
            logger.error('[EQUITY_EXIT] Error checking trading hours: %s', exc)
            return True

    # ------------------------------------------------------------------
    # Settings, pacing and heartbeat
    # ------------------------------------------------------------------

    @staticmethod
    def _interval_for(settings) -> int:
        try:
            interval = int(settings.sl_monitor_interval_seconds or 0)
        except (TypeError, ValueError):
            interval = 0
        if interval <= 0:
            interval = DEFAULT_MONITOR_INTERVAL_SECONDS
        return max(MIN_MONITOR_INTERVAL_SECONDS,
                   min(MAX_MONITOR_INTERVAL_SECONDS, interval))

    @staticmethod
    def _is_due(settings, interval) -> bool:
        """
        The scheduler ticks on its own fixed interval, and each user asks for a
        pace of their own. This is that pace, taken from the stored heartbeat
        so it survives a restart.
        """
        last_run = settings.monitor_last_run_at
        if last_run is None:
            return True
        elapsed = (datetime.utcnow() - last_run).total_seconds()
        if elapsed < 0:
            # Clock went backwards. Run rather than sit out an unknown wait.
            return True
        return elapsed >= interval

    def _write_heartbeat(self, settings, note=None):
        """Prove the monitor ran, with no browser tab involved."""
        try:
            settings.monitor_last_run_at = datetime.utcnow()
            settings.monitor_last_error = note
            db.session.commit()
        except Exception as exc:
            logger.error('[EQUITY_EXIT] Could not write the monitor heartbeat: %s', exc)
            self._safe_rollback()

    def _record_user_error(self, user_id, message):
        """Put a failed pass where the Settings screen can see it."""
        try:
            settings = EquitySetting.get_or_create(user_id)
            if settings is None:
                return
            settings.monitor_last_error = str(message)[:1000]
            db.session.commit()
        except Exception as exc:
            logger.error('[EQUITY_EXIT] Could not record the monitor error: %s', exc)
            self._safe_rollback()

    @staticmethod
    def _log_activity(user_id, account_id, reason, symbol, exchange,
                      price, level_price, exit_mode):
        """
        Audit row for the breach. Written once per armed level, because it sits
        behind record_breach, not once per tick.

        A failure here must never stop an exit, so it is swallowed.
        """
        try:
            entry = ActivityLog(
                user_id=user_id,
                account_id=account_id,
                action=(
                    'equity_auto_exit_triggered'
                    if exit_mode == EQUITY_EXIT_MODE_AUTO
                    else 'equity_exit_confirmation_required'
                ),
                details={
                    'symbol': symbol,
                    'exchange': exchange,
                    'reason': reason,
                    'breach_price': price,
                    'level_price': level_price,
                    'exit_mode': exit_mode,
                    'source': 'equity_exit_monitor',
                },
                status='warning'
            )
            db.session.add(entry)
            db.session.commit()
        except Exception as exc:
            logger.error('[EQUITY_EXIT] Could not write the breach activity log: %s', exc)
            try:
                db.session.rollback()
            except Exception:
                pass

    @staticmethod
    def _safe_rollback():
        try:
            db.session.rollback()
        except Exception:
            pass

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict:
        """
        Snapshot for the Settings screen and for support. Never raises.
        """
        _, placer_name = resolve_exit_placer()

        with self._lock:
            pending = sum(1 for attempt in self._attempts.values() if attempt.in_progress)
            tracked = len(self._attempts)
            has_pool = self._executor is not None

        try:
            feed = equity_price_feed.status()
        except Exception:
            feed = {}

        return {
            'is_running': self.is_running,
            'inline_exits': bool(self.inline_exits),
            'exit_pool_active': has_pool,
            'exit_placer': placer_name,
            'scheduler_job_id': SCHEDULER_JOB_ID,
            'scheduler_interval_seconds': SCHEDULER_INTERVAL_SECONDS,
            'auto_exit_retry_seconds': AUTO_EXIT_RETRY_SECONDS,
            'max_auto_exit_attempts': MAX_AUTO_EXIT_ATTEMPTS,
            'last_run_at': self._last_run_at.isoformat() if self._last_run_at else None,
            'last_run_duration_ms': self._last_run_duration_ms,
            'last_error': self._last_error,
            'last_skip_reason': self._last_skip_reason,
            'exits_in_flight': pending,
            'holdings_tracked': tracked,
            'last_tick': dict(self._last_tick),
            'totals': dict(self._stats),
            'price_feed': {
                'available': feed.get('available'),
                'authenticated': feed.get('authenticated'),
                'subscribed': feed.get('subscribed'),
                'priced': feed.get('priced'),
                'last_tick_age_seconds': feed.get('last_tick_age_seconds'),
            },
        }


def _source_for_reason(reason):
    """Map an exit reason onto the EquityOrder.source the order engine records."""
    if reason == EQUITY_EXIT_REASON_TARGET:
        return EQUITY_ORDER_SOURCE_TARGET
    return EQUITY_ORDER_SOURCE_STOP_LOSS


def build_exit_payload(holding_id, user_id, account_id, symbol, exchange,
                       reason, breach_price, level_price):
    """
    Everything the monitor offers a placer, as plain values.

    Built in one place so both the first attempt and a retry send exactly the
    same thing, and so a test can assert which of these keys actually reach the
    real order engine. See EXIT_PLACER_CONTRACT for what each one means.
    """
    return {
        'holding_id': holding_id,
        'user_id': user_id,
        'account_id': account_id,
        'symbol': symbol,
        'exchange': exchange,
        # None on purpose: the claim reads the fresh sellable quantity under
        # its own row lock rather than trusting a number read a moment ago.
        'quantity': None,
        'reason': reason,
        'source': _source_for_reason(reason),
        'breach_price': breach_price,
        'level_price': level_price,
    }


# Module level singleton, in the same shape as risk_manager and
# equity_price_feed.
equity_exit_monitor = EquityExitMonitor()


def run_equity_exit_checks():
    """
    The callable to schedule.

    Wire it into the existing APScheduler exactly the way
    BackgroundService.run_risk_checks wires risk_manager, that is inside
    "with flask_app.app_context():", with max_instances=1 so a slow tick is
    skipped rather than overlapped:

        self.scheduler.add_job(
            func=self.run_equity_exit_checks,
            trigger='interval',
            seconds=SCHEDULER_INTERVAL_SECONDS,
            id=SCHEDULER_JOB_ID,
            replace_existing=True,
            max_instances=1,
            misfire_grace_time=10,
        )

    and call equity_exit_monitor.start() once the app is up, since the monitor
    does nothing until it is armed.
    """
    equity_exit_monitor.run_checks()
