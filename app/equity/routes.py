"""
Equity (CNC delivery) module routes.

Increment 1 was read only. Increment 2 adds the transactional surface: M3 Watch
List, M4 Place Order, M4b Order Status, M5 Order Book, M6 Trade Book and the M7
Holdings stop loss, target and exit actions.

THIS MODULE STILL NEVER WRITES TO A BROKER ITSELF. Every order that is placed,
modified, cancelled or exited goes through app.utils.equity_order_engine, which
is the one place the safety rules are auditable and the one place a broker write
can happen. The only broker calls made here are reads: funds(), holdings(),
quotes(), multiquotes(), depth() and search(). Everything else this module
writes goes to AlgoMirror's own tables: the equity fund allocation, the
brokerage rate versions, the watch list, the trade natures, the tracked
holdings, the module preferences and the cached broker payloads that already
exist on TradingAccount.

Every business formula lives in the two pure engines, app.utils.equity_ratio and
app.utils.equity_costs. This module converts ORM rows and broker payloads into
plain numbers, calls the engines and serialises the result. No PRD formula is
reimplemented here.

Screens served: M1 Dashboard, M2 Accounts, M3 Watch List, M4 Place Order, M5
Order Book, M6 Trade Book, M7 Holdings and Settings.

WHERE THE DATA COMES FROM, and why the screens are not three round trips deep:
    Prices are event driven. They are read from app.utils.equity_price_feed,
        which is backed by the single shared OpenAlgo WebSocket manager that
        already serves the F&O screens. A warm feed costs zero broker calls. The
        REST quote helpers below are a bounded fallback for symbols that have
        not ticked yet, never the primary path.
    Funds and holdings stay on REST, because OpenAlgo does not push them, but
        they are off the critical path in two ways: the two reads for one
        account are issued concurrently rather than one after the other, and a
        payload cached inside BROKER_CACHE_TTL_SECONDS is served without calling
        the broker at all.
"""

import csv
import io
import math
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import date, datetime
from functools import wraps

from flask import Response, current_app, jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app import db
from app.equity import equity_bp
from app.models import (
    ActivityLog,
    EquityAccountAllocation,
    EquityBrokerageRate,
    EquityHolding,
    EquityOrder,
    EquityOrderSplit,
    EquitySetting,
    EquityTrade,
    EquityTradeNature,
    EquityWatchlistItem,
    TradingAccount,
    EQUITY_ALERT_DIRECTION_ABOVE,
    EQUITY_ALERT_DIRECTION_BELOW,
    EQUITY_EXIT_MODE_AUTO,
    EQUITY_EXIT_MODE_CONFIRM,
    EQUITY_EXIT_REASON_MANUAL,
    EQUITY_EXIT_REASON_STOP_LOSS,
    EQUITY_EXIT_REASON_TARGET,
    EQUITY_FUNDS_ACTION_ABORT,
    EQUITY_FUNDS_ACTION_SKIP,
    EQUITY_HOLDING_STATUS_ACTIVE,
    EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
    EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE,
    EQUITY_HOLDING_STATUSES_CLAIMABLE,
    EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT,
    EQUITY_ORDER_SOURCE_MANUAL,
    EQUITY_ORDER_STATUS_CANCELLED,
    EQUITY_ORDER_STATUS_COMPLETED,
    EQUITY_ORDER_STATUS_PARTIAL,
    EQUITY_ORDER_STATUS_PENDING,
    EQUITY_ORDER_TYPE_GTT,
    EQUITY_ORDER_TYPE_LIMIT,
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_PRODUCT_CNC,
    EQUITY_SIDE_BUY,
    EQUITY_SIDE_SELL,
    EQUITY_SPLIT_STATUS_FAILED,
    EQUITY_SPLIT_STATUS_SKIPPED,
)
from app.utils.equity_costs import (
    BrokerageRates,
    estimate_costs,
    gross_pnl,
    net_pnl,
    turnover,
)
from app.utils.equity_order_engine import (
    EquityOrderError,
    cancel_order,
    exit_holding,
    modify_order,
    place_multi_account_order,
    preview_order_split,
    summarise_splits,
)
from app.utils.equity_price_feed import equity_price_feed
from app.utils.equity_ratio import (
    collateral_from_margin,
    compute_order_qty_ratios,
    invested_percent,
    percent_of,
    pledge_percent,
    signed_percent_of,
    stake_percent_for_view,
    stock_at_cost,
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit

# Short timeout for every interactive broker read. The equity screens poll, so a
# slow broker must fail fast and fall back to the cached payload rather than
# holding the page open.
BROKER_TIMEOUT_SECONDS = 8

# Upper bound on the fan-out pool. One worker per account, capped.
MAX_FETCH_WORKERS = 10

# Funds and holdings for one account are issued together instead of one after
# the other, so an account costs one broker round trip rather than two. Only the
# second read is handed to a thread, the first runs on the account's own fan-out
# worker, so a request adds at most MAX_FETCH_WORKERS extra threads and the
# inner concurrency cannot multiply with the outer fan-out.
MAX_INNER_WORKERS = 1

# Freshness window for the broker payloads cached on TradingAccount. Inside this
# window the screens serve last_funds_data and last_holdings_data and do not
# call the broker at all. 30 seconds is the house precedent: the F&O funds
# screen gates the same columns on the same window (app/trading/routes.py). The
# equity screens poll on that cadence, so the extra readers around one poll (a
# second tab, a manual refresh, the CSV export) cost nothing at the broker, and
# a poll only reaches the broker once the cache has actually aged out.
BROKER_CACHE_TTL_SECONDS = 30

# Upper bound on symbols sent to multiquotes in one call, and on the per-symbol
# quote fallback that runs when multiquotes is unavailable.
MAX_QUOTE_SYMBOLS = 100
MAX_QUOTE_FALLBACK_SYMBOLS = 25
MAX_QUOTE_FALLBACK_WORKERS = 5

# Wall clock ceiling for the whole REST quote fallback in one request. Prices
# come from the push feed, so the fallback only ever covers symbols that have
# not ticked yet, and it must never dominate the request: 25 symbols against an
# 8 second per call timeout with 5 workers can reach 40 seconds, which is longer
# than the browser's own abort. Every stage is therefore bounded by this
# deadline as well as by the symbol counts above.
MAX_QUOTE_FALLBACK_SECONDS = 6.0

# A broker call with less than this left in the fallback budget is not started.
MIN_QUOTE_CALL_SECONDS = 0.5

# Exit mode tags shown next to the stop loss and target on the Holdings screen.
EXIT_MODE_TAGS = {
    EQUITY_EXIT_MODE_AUTO: 'AE',
    EQUITY_EXIT_MODE_CONFIRM: 'CE',
}

# Broker payload key aliases. Different OpenAlgo broker adapters spell these
# differently, so each value is resolved from the first key that carries a
# number.
_AVG_COST_KEYS = ('average_price', 'avgprice', 'avg_price', 'averageprice')
_LTP_KEYS = ('ltp', 'last_price', 'lastprice')
_PNL_PCT_KEYS = ('pnlpercent', 'pnl_percent', 'pnlpercentage', 'pnl_percentage')
_PLEDGED_KEYS = ('collateralquantity', 'collateral_quantity', 'pledgedquantity', 'pledged_quantity')
# Fallback for a broker adapter that reports a combined available margin rather
# than a separate collateral figure. Collateral is then margin minus raw cash.
_AVAILABLE_MARGIN_KEYS = ('availablemargin', 'available_margin', 'netmargin', 'net_margin')
_PREV_CLOSE_KEYS = ('prev_close', 'previous_close', 'prevclose', 'previousclose', 'close')

# Illustrative rates from the approved Settings mockup. They are offered to the
# form as a prefill only and are NEVER used in a cost calculation: an account
# with no saved rate row is costed at zero and flagged as unconfigured, so a
# number the admin never entered can never end up in a P&L figure.
SUGGESTED_RATE_DEFAULTS = {
    'brokerage_per_order': 20.0,
    'stt_pct': 0.1,
    'exchange_txn_pct': 0.00297,
    'sebi_pct': 0.0001,
    'stamp_duty_pct': 0.015,
    'gst_pct': 18.0,
    'dp_amc_charge': 13.5,
}

# Rules panel copy for M2 Accounts. Served from here so the screen and the
# behaviour implemented in this module cannot drift apart.
ALLOCATION_RULES = [
    'Order Qty Ratio is derived: an account ratio is its equity fund allocation '
    'divided by the total allocation of all active accounts.',
    'Equity Fund Allocation is the investable corpus you set by hand. It is '
    'independent of Available Cash and of the F&O module.',
    'On insufficient funds the default is to skip that account and continue with '
    'the rest. Aborting the whole order instead is a configuration option.',
    'Quantity is rounded down to the nearest tradable lot per account. The '
    'leftover is shown and is not carried over to another account.',
    'Accounts are added and connected in the main Accounts screen. This screen '
    'only sets how much of each account is earmarked for equity.',
    'Allocation changes are future dated only. Past orders keep the ratio and '
    'cash balance recorded against them and are never recalculated.',
]

# Est. Costs formula restated for the Settings footer.
COST_FORMULA_NOTES = [
    'Est. Costs = brokerage + STT on turnover + exchange transaction charge on '
    'turnover + SEBI charge on turnover + stamp duty on BUY turnover only + GST '
    'at the configured percent of (brokerage + exchange transaction charge) + '
    'DP/AMC on SELL only, per scrip.',
    'Gross P&L = (LTP - Avg Cost) x Qty. Net P&L = Gross P&L - Est. Costs.',
    'Percentage fields are percent values, so 0.10 means 0.10 percent.',
    'Saving rates inserts a new effective-dated version. Changes apply to future '
    'calculations only and past cost figures stay reproducible.',
]


# ---------------------------------------------------------------------------
# Process local caches
#
# All three are small, bounded and safe to lose. A fresh process simply makes
# one more broker read than it strictly had to, which is the safe way to be
# wrong. Under more than one worker process each worker keeps its own copy, so
# the worst case is one extra read per worker, never a wrong number.
# ---------------------------------------------------------------------------

# When each account's holdings payload was last refreshed from the broker.
# TradingAccount carries a single cache timestamp column, last_data_update, and
# the F&O funds screen reads it as the age of last_funds_data, so a holdings
# only read must not advance it (see _refresh_account_cache). Holdings therefore
# keep their own timestamp here rather than in a new column.
_HOLDINGS_REFRESHED_AT = {}
# Funds gets its own stamp for the same reason holdings does: the shared
# TradingAccount.last_data_update column is also advanced by the trading and
# accounts blueprints after a POSITIONS or HOLDINGS read, so trusting it here
# would serve arbitrarily old cash as fresh and never mark it stale.
_FUNDS_REFRESHED_AT = {}

# Previous close for the current trading day, keyed (symbol, exchange). The push
# feed subscribes in LTP mode and carries no previous close, but the value does
# not move during the day, so one REST quote per symbol per day is enough to
# keep Today's P&L alive. A recorded 0.0 means the broker was asked and reported
# nothing, which is what stops the fallback asking again on every poll.
# A previous close the broker would not answer is retried after this long
# rather than being written off for the day.
PREV_CLOSE_RETRY_SECONDS = 300.0
_PREV_CLOSE = {}
_PREV_CLOSE_DAY = None

# The symbol set the equity screens last asked the price feed to hold, so
# symbols that are no longer held can be unsubscribed instead of accumulating
# against the feed's own ceiling.
_FEED_SYMBOLS = set()

# One lock for all three. Every critical section below is a dict or set
# operation on plain values, so a single lock is cheaper than three.
_CACHE_LOCK = threading.Lock()


# ---------------------------------------------------------------------------
# Small conversion helpers
# ---------------------------------------------------------------------------

def _to_float(value, default=0.0):
    """Coerce a broker payload value (often a string) to a finite float."""
    if value is None or value == '':
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    if not math.isfinite(number):
        return default
    return number


def _to_int(value, default=0):
    """Coerce a value to a whole number, truncating toward zero."""
    number = _to_float(value, float(default))
    try:
        return int(number)
    except (TypeError, ValueError, OverflowError):
        return default


def _money(value):
    """Round a rupee figure for JSON output. Presentation only."""
    number = _to_float(value)
    return round(number, 2)


def _pct(value):
    """Round a percent figure for JSON output. Presentation only."""
    number = _to_float(value)
    return round(number, 2)


def _safe_divide(numerator, denominator):
    """
    Guarded division used only for display averages such as the blended average
    cost of a symbol held across several accounts. This is not one of the PRD
    formulas, those all live in the two engine modules.
    """
    denom = _to_float(denominator)
    if denom == 0.0:
        return 0.0
    return _to_float(numerator) / denom


def _iso(value):
    """Serialise a datetime or date, or None."""
    if value is None:
        return None
    return value.isoformat()


def _first_number(row, keys):
    """Read the first key in keys that carries a usable number."""
    for key in keys:
        if key in row:
            value = _to_float(row.get(key))
            if value:
                return value
    return 0.0


def _json_error(message, http_status=400):
    """Standard error envelope. The frontend always checks the status field."""
    return jsonify({'status': 'error', 'message': message}), http_status


# ---------------------------------------------------------------------------
# Cache freshness and the process local stores
# ---------------------------------------------------------------------------

def _cache_age_seconds(timestamp):
    """Age of a cache timestamp in seconds, or None when it was never written."""
    if timestamp is None:
        return None
    try:
        return (datetime.utcnow() - timestamp).total_seconds()
    except (TypeError, ValueError):
        return None


def _is_fresh(payload, timestamp):
    """
    True when a cached broker payload exists and is inside the freshness window.

    An empty payload is never fresh: there is nothing to serve from it, so the
    broker is called instead.
    """
    if not payload:
        return False
    age = _cache_age_seconds(timestamp)
    return age is not None and age < BROKER_CACHE_TTL_SECONDS


def _funds_refreshed_at(account_id):
    """When this account's funds payload was last read live, or None."""
    with _CACHE_LOCK:
        return _FUNDS_REFRESHED_AT.get(account_id)


def _mark_funds_refreshed(account_id, when):
    """Record a live funds read, which is what the funds freshness gate reads."""
    with _CACHE_LOCK:
        _FUNDS_REFRESHED_AT[account_id] = when


def _holdings_refreshed_at(account_id):
    """When this account's holdings payload was last read live, or None."""
    with _CACHE_LOCK:
        return _HOLDINGS_REFRESHED_AT.get(account_id)


def _mark_holdings_refreshed(account_id, when):
    """Record a live holdings read, which is what the freshness gate reads."""
    with _CACHE_LOCK:
        _HOLDINGS_REFRESHED_AT[account_id] = when


def _prev_close_cached(key):
    """
    Previous close remembered for today, 0.0 when it is not known.

    The store is reset when the calendar day rolls, so yesterday's close can
    never be reused as today's reference.
    """
    global _PREV_CLOSE_DAY
    today = date.today()
    with _CACHE_LOCK:
        if _PREV_CLOSE_DAY != today:
            _PREV_CLOSE.clear()
            _PREV_CLOSE_DAY = today
            return 0.0
        stored = _PREV_CLOSE.get(key)
        if isinstance(stored, tuple):
            # A retry marker, not a price.
            return 0.0
        return _to_float(stored)


def _prev_close_asked(key):
    """True when the REST fallback already asked for this symbol's close today."""
    with _CACHE_LOCK:
        stored = _PREV_CLOSE.get(key)
        if stored is None:
            return False
        if isinstance(stored, tuple):
            # Asked and unanswered. Counts as asked only until the
            # backoff expires, then the symbol is eligible again.
            return time.monotonic() < stored[1]
        return True


def _remember_prev_close(key, value):
    """
    Record a previous close the broker actually answered.

    A real close is remembered for the rest of the day, since it does not move
    and re-asking would cost a round trip per poll.

    A zero is NOT remembered as final. An unanswered close is usually transient,
    and treating one zero as permanent would silently disable Today's P&L for
    that symbol for the whole session. Zero is recorded as a retry-after stamp
    instead, so the question is asked again a little later rather than on every
    poll or never again.
    """
    global _PREV_CLOSE_DAY
    today = date.today()
    number = _to_float(value)
    with _CACHE_LOCK:
        if _PREV_CLOSE_DAY != today:
            _PREV_CLOSE.clear()
            _PREV_CLOSE_DAY = today
        if number > 0:
            _PREV_CLOSE[key] = number
        else:
            _PREV_CLOSE[key] = ('retry', time.monotonic() + PREV_CLOSE_RETRY_SECONDS)


# ---------------------------------------------------------------------------
# Ownership scoped lookups
# ---------------------------------------------------------------------------

def _active_accounts():
    """Every active trading account owned by the current user."""
    return TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).order_by(TradingAccount.id).all()


def _owned_account(account_id):
    """One account, scoped by BOTH id and owner. Returns None when not owned."""
    return TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id
    ).first()


def _get_or_create_allocations(accounts):
    """
    Return account_id to EquityAccountAllocation for the given accounts,
    creating a zero row for any account that does not have one yet. Idempotent,
    and it commits once only when something was actually added.
    """
    rows = EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
    by_account = {row.account_id: row for row in rows}

    created = False
    for account in accounts:
        if account.id not in by_account:
            row = EquityAccountAllocation(
                account_id=account.id,
                user_id=current_user.id,
                equity_fund_allocation=0.0
            )
            db.session.add(row)
            by_account[account.id] = row
            created = True

    if created:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(f'Could not seed equity allocations: {exc}')
            rows = EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
            by_account = {row.account_id: row for row in rows}

    return {account.id: by_account.get(account.id) for account in accounts}


def _allocation_amounts(accounts, allocation_rows):
    """
    Plain account_id to rupee allocation mapping for the ACTIVE equity accounts,
    in account order. This is the input to the ratio engine and the denominator
    for stake percent, so an account excluded here is excluded from both.

    Active means the trading account is active (already filtered by the caller)
    and its allocation row has not been deactivated.
    """
    amounts = {}
    for account in accounts:
        row = allocation_rows.get(account.id)
        if row is not None and row.is_active is False:
            continue
        amounts[account.id] = _to_float(row.equity_fund_allocation) if row is not None else 0.0
    return amounts


def _trade_natures():
    """
    Active trade natures for the current user, seeding the defaults the first
    time. Trade Nature is admin configurable, the four seeds are only seeds.
    """
    natures = EquityTradeNature.query.filter_by(
        user_id=current_user.id
    ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()

    if not natures:
        EquityTradeNature.get_or_create_defaults(current_user.id)
        natures = EquityTradeNature.query.filter_by(
            user_id=current_user.id
        ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()

    return [nature for nature in natures if nature.is_active is not False]


def _brokerage_rates_by_account(account_ids, on_date=None):
    """
    Resolve the effective BrokerageRates for each account.

    An account with no rate row in effect is costed at zero and reported in the
    second return value, so the UI can say the rates are not configured yet
    rather than showing a silently wrong Net P&L.
    """
    rates = {}
    unconfigured = []
    for account_id in account_ids:
        row = EquityBrokerageRate.get_effective_rate(current_user.id, account_id, on_date)
        if row is None:
            rates[account_id] = BrokerageRates()
            unconfigured.append(account_id)
            continue
        rates[account_id] = BrokerageRates(
            brokerage_per_order=_to_float(row.brokerage_per_order),
            stt_pct=_to_float(row.stt_pct),
            exchange_txn_pct=_to_float(row.exchange_txn_pct),
            sebi_pct=_to_float(row.sebi_pct),
            stamp_duty_pct=_to_float(row.stamp_duty_pct),
            gst_pct=_to_float(row.gst_pct),
            dp_amc_charge=_to_float(row.dp_amc_charge),
        )
    return rates, unconfigured


# ---------------------------------------------------------------------------
# Broker fan-out. Read only: funds, holdings and quotes.
# ---------------------------------------------------------------------------

def _account_credentials(accounts):
    """
    Extract plain credential and cache tuples BEFORE any thread is spawned. No
    ORM object and no lazy load ever crosses a thread boundary in this module.

    The two freshness flags are resolved here as well, for the same reason: the
    fan-out decides whether to call the broker at all from plain values, without
    touching an ORM row from a worker thread.
    """
    creds = []
    for account in accounts:
        try:
            api_key = account.get_api_key()
        except Exception as exc:
            current_app.logger.error(f'Could not read API key for account {account.id}: {exc}')
            api_key = None

        cached_funds = account.last_funds_data if isinstance(account.last_funds_data, dict) else None
        cached_holdings = account.last_holdings_data if isinstance(account.last_holdings_data, dict) else None

        creds.append({
            'account_id': account.id,
            'api_key': api_key,
            'host_url': account.host_url,
            'cached_funds': dict(cached_funds) if cached_funds else None,
            'cached_holdings': dict(cached_holdings) if cached_holdings else None,
            'funds_fresh': _is_fresh(cached_funds, _funds_refreshed_at(account.id)),
            'holdings_fresh': _is_fresh(
                cached_holdings,
                _holdings_refreshed_at(account.id)
            ),
        })
    return creds


def _new_snapshot(account_id):
    """An empty snapshot. Nothing read, nothing cached, nothing stale."""
    return {
        'account_id': account_id,
        'funds': None,
        'holdings_data': None,
        'funds_live': False,
        'holdings_live': False,
        'from_cache': False,
        'is_stale': False,
        'error': None,
    }


def _read_one_broker_call(app, cred, name):
    """
    Make one read only broker call by name and return its raw response.

    Never raises: a transport failure comes back as the same error envelope the
    broker itself would return, so the caller has one shape to handle. Each call
    builds its own client, so two concurrent reads never share an HTTP session.
    """
    with app.app_context():
        try:
            client = ExtendedOpenAlgoAPI(
                api_key=cred['api_key'],
                host=cred['host_url'],
                timeout=BROKER_TIMEOUT_SECONDS
            )
            return getattr(client, name)()
        except Exception as exc:
            return {'status': 'error', 'message': str(exc)}


def _read_broker_calls(app, cred, names):
    """
    Issue the wanted reads for one account CONCURRENTLY and return name to
    response.

    The first read runs on the caller's own thread and every other read gets one
    thread from a pool of at most MAX_INNER_WORKERS, so an account costs one
    round trip instead of two while the request adds at most one thread per
    account. If a thread cannot be started the reads simply run in sequence,
    which is slower and still correct.
    """
    if not names:
        return {}
    if len(names) == 1:
        return {names[0]: _read_one_broker_call(app, cred, names[0])}

    inline, deferred = names[0], names[1:]

    executor = None
    futures = {}
    try:
        executor = ThreadPoolExecutor(max_workers=min(MAX_INNER_WORKERS, len(deferred)))
        futures = {
            name: executor.submit(_read_one_broker_call, app, cred, name)
            for name in deferred
        }
    except Exception as exc:
        futures = {}
        current_app.logger.warning(
            f'Equity concurrent broker read unavailable, falling back to sequential: {exc}',
            extra={'event': 'equity_inner_pool_unavailable'}
        )

    responses = {inline: _read_one_broker_call(app, cred, inline)}
    for name in deferred:
        future = futures.get(name)
        if future is None:
            responses[name] = _read_one_broker_call(app, cred, name)
            continue
        try:
            responses[name] = future.result()
        except Exception as exc:
            responses[name] = {'status': 'error', 'message': str(exc)}

    if executor is not None:
        executor.shutdown(wait=False)

    return responses


def _fetch_account_snapshot(app, cred, want_funds, want_holdings):
    """
    Read funds and holdings for one account. Never raises: a broker failure
    degrades to the cached payload and marks the account stale, so one bad
    account cannot break the page.

    want_funds and want_holdings are what this account still has to fetch. A
    side already covered by a fresh cache is filled in by _apply_fresh_cache
    after the read, and is not stale.
    """
    snapshot = _new_snapshot(cred['account_id'])

    with app.app_context():
        try:
            if not cred.get('api_key'):
                raise ValueError('API key is not available for this account')

            names = []
            if want_funds:
                names.append('funds')
            if want_holdings:
                names.append('holdings')
            responses = _read_broker_calls(app, cred, names)

            if want_funds:
                response = responses.get('funds')
                if isinstance(response, dict) and response.get('status') == 'success':
                    data = response.get('data')
                    snapshot['funds'] = data if isinstance(data, dict) else {}
                    snapshot['funds_live'] = True
                else:
                    snapshot['error'] = (response or {}).get('message') or 'Failed to fetch funds'

            if want_holdings:
                response = responses.get('holdings')
                if isinstance(response, dict) and response.get('status') == 'success':
                    data = response.get('data')
                    snapshot['holdings_data'] = data if isinstance(data, dict) else {}
                    snapshot['holdings_live'] = True
                else:
                    snapshot['error'] = (
                        snapshot['error']
                        or (response or {}).get('message')
                        or 'Failed to fetch holdings'
                    )
        except Exception as exc:
            snapshot['error'] = str(exc)
            current_app.logger.error(
                f'Equity snapshot failed for account {cred["account_id"]}: {exc}'
            )

    # Degrade to the cached payload for anything that did not come back live.
    if want_funds and snapshot['funds'] is None:
        snapshot['funds'] = cred.get('cached_funds') or {}
        snapshot['is_stale'] = True
    if want_holdings and snapshot['holdings_data'] is None:
        snapshot['holdings_data'] = cred.get('cached_holdings') or {}
        snapshot['is_stale'] = True

    return snapshot


def _apply_fresh_cache(snapshot, cred, want_funds, want_holdings):
    """
    Fill the sides that were served from the freshness window.

    This is NOT the stale path. The payload is inside BROKER_CACHE_TTL_SECONDS,
    so it is current data that simply did not need a broker call, and the
    account is not flagged stale for it.
    """
    if want_funds and cred.get('funds_fresh') and snapshot.get('funds') is None:
        snapshot['funds'] = cred.get('cached_funds') or {}
        snapshot['from_cache'] = True
    if want_holdings and cred.get('holdings_fresh') and snapshot.get('holdings_data') is None:
        snapshot['holdings_data'] = cred.get('cached_holdings') or {}
        snapshot['from_cache'] = True
    return snapshot


def _fan_out(creds, want_funds=False, want_holdings=False):
    """
    Read every account in parallel, skipping the broker for anything a fresh
    cache already answers. Returns account_id to snapshot.
    """
    snapshots = {}
    if not creds:
        return snapshots

    app = current_app._get_current_object()

    # Freshness gate first, so an account fully covered by cache never reaches a
    # thread, let alone the broker.
    live = []
    for cred in creds:
        needs_funds = bool(want_funds) and not cred.get('funds_fresh')
        needs_holdings = bool(want_holdings) and not cred.get('holdings_fresh')
        if not needs_funds and not needs_holdings:
            snapshots[cred['account_id']] = _apply_fresh_cache(
                _new_snapshot(cred['account_id']), cred, want_funds, want_holdings
            )
            continue
        live.append((cred, needs_funds, needs_holdings))

    if not live:
        current_app.logger.debug(
            f'Equity fan-out served {len(creds)} accounts from cache, no broker call',
            extra={'event': 'equity_fanout_cached'}
        )
        return snapshots

    if len(live) == 1:
        cred, needs_funds, needs_holdings = live[0]
        snapshot = _fetch_account_snapshot(app, cred, needs_funds, needs_holdings)
        _apply_fresh_cache(snapshot, cred, want_funds, want_holdings)
        snapshots[snapshot['account_id']] = snapshot
        return snapshots

    creds_by_account = {cred['account_id']: cred for cred, _, _ in live}
    with ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(live))) as executor:
        futures = [
            executor.submit(_fetch_account_snapshot, app, cred, needs_funds, needs_holdings)
            for cred, needs_funds, needs_holdings in live
        ]
        for future in as_completed(futures):
            try:
                snapshot = future.result()
            except Exception as exc:
                current_app.logger.error(f'Equity account fan-out worker failed: {exc}')
                continue
            cred = creds_by_account.get(snapshot['account_id'])
            if cred is not None:
                _apply_fresh_cache(snapshot, cred, want_funds, want_holdings)
            snapshots[snapshot['account_id']] = snapshot

    return snapshots


def _refresh_account_cache(accounts, snapshots):
    """
    Write live broker payloads back into the TradingAccount cache columns.

    last_data_update is advanced only when funds came back live, because the
    F&O funds screen treats that column as the age of last_funds_data. Writing
    it after a holdings-only read would make stale cash look fresh over there.
    A live holdings read is timestamped in _HOLDINGS_REFRESHED_AT instead, which
    is the only thing the holdings freshness gate reads.
    """
    changed = False
    now = datetime.utcnow()
    refreshed_holdings = []
    refreshed_funds = []

    for account in accounts:
        snapshot = snapshots.get(account.id)
        if not snapshot:
            continue
        if snapshot.get('funds_live') and isinstance(snapshot.get('funds'), dict):
            account.last_funds_data = snapshot['funds']
            account.last_data_update = now
            refreshed_funds.append(account.id)
            changed = True
        if snapshot.get('holdings_live') and isinstance(snapshot.get('holdings_data'), dict):
            account.last_holdings_data = snapshot['holdings_data']
            refreshed_holdings.append(account.id)
            changed = True

    if not changed:
        return

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f'Could not cache equity broker payloads: {exc}')
        return

    # Stamped only after the payload is actually on disk. Stamping a write that
    # rolled back would gate the next request onto the previous payload.
    for account_id in refreshed_holdings:
        _mark_holdings_refreshed(account_id, now)
    for account_id in refreshed_funds:
        _mark_funds_refreshed(account_id, now)


def _quote_credential(creds, snapshots):
    """
    Pick one account to read quotes through. Prefer an account that just
    answered live, otherwise any account with a usable key.
    """
    for cred in creds:
        snapshot = snapshots.get(cred['account_id'])
        if cred.get('api_key') and snapshot and not snapshot.get('is_stale'):
            return cred
    for cred in creds:
        if cred.get('api_key'):
            return cred
    return None


def _seconds_left(deadline):
    """Wall clock seconds left in a fallback budget, or None when unbounded."""
    if deadline is None:
        return None
    return max(0.0, deadline - time.monotonic())


def _call_timeout(deadline):
    """
    Per call timeout for a fallback broker read: the smaller of the interactive
    timeout and what is left of the budget. Returns 0.0 when there is too little
    left to be worth a call.
    """
    left = _seconds_left(deadline)
    if left is None:
        return float(BROKER_TIMEOUT_SECONDS)
    if left < MIN_QUOTE_CALL_SECONDS:
        return 0.0
    return min(float(BROKER_TIMEOUT_SECONDS), left)


def _fetch_quotes_individually(app, cred, symbol_keys, deadline=None):
    """
    Per-symbol quote fallback for brokers or SDKs without multiquotes.

    Bounded twice over: by MAX_QUOTE_FALLBACK_SYMBOLS and by the wall clock
    deadline. Whatever has answered when the budget runs out is returned and the
    rest is abandoned, because the caller has cheaper prices to fall back on and
    the browser has an abort of its own.
    """
    quotes = {}
    keys = list(symbol_keys)[:MAX_QUOTE_FALLBACK_SYMBOLS]
    if not keys:
        return quotes

    timeout = _call_timeout(deadline)
    if timeout <= 0:
        return quotes

    def fetch_one(key):
        symbol, exchange = key
        with app.app_context():
            try:
                client = ExtendedOpenAlgoAPI(
                    api_key=cred['api_key'],
                    host=cred['host_url'],
                    timeout=timeout
                )
                response = client.quotes(symbol=symbol, exchange=exchange)
            except Exception:
                return (key, None)
            if isinstance(response, dict) and response.get('status') == 'success':
                data = response.get('data')
                if isinstance(data, dict):
                    return (key, data)
            return (key, None)

    executor = ThreadPoolExecutor(max_workers=min(MAX_QUOTE_FALLBACK_WORKERS, len(keys)))
    try:
        futures = [executor.submit(fetch_one, key) for key in keys]
        try:
            for future in as_completed(futures, timeout=_seconds_left(deadline)):
                try:
                    key, data = future.result()
                except Exception:
                    continue
                if data:
                    quotes[key] = {
                        'ltp': _to_float(data.get('ltp')),
                        'prev_close': _to_float(data.get('prev_close')),
                    }
        except FuturesTimeoutError:
            current_app.logger.debug(
                f'Equity quote fallback stopped at its {MAX_QUOTE_FALLBACK_SECONDS}s '
                f'budget with {len(quotes)} of {len(keys)} symbols answered',
                extra={'event': 'equity_quote_fallback_timeout'}
            )
    finally:
        # Never wait here. Waiting would hand the request back exactly the wall
        # clock the deadline exists to prevent. Calls already running carry the
        # per call timeout above and end on their own.
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    return quotes


def _fetch_quotes(cred, symbol_keys, deadline=None):
    """
    REST FALLBACK ONLY. Read the last traded price and previous close for the
    symbols the push feed cannot answer yet.

    Pure read. Returns (symbol, exchange) to {'ltp', 'prev_close'}, and simply
    returns fewer entries when the broker is unavailable or the budget runs out.
    Callers fall back to the price implied by the holdings payload.
    """
    quotes = {}
    keys = list(symbol_keys)[:MAX_QUOTE_SYMBOLS]
    if not cred or not cred.get('api_key') or not keys:
        return quotes

    app = current_app._get_current_object()
    timeout = _call_timeout(deadline)
    if timeout <= 0:
        return quotes

    response = None
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=cred['api_key'],
            host=cred['host_url'],
            timeout=timeout
        )
        response = client.multiquotes(
            symbols=[{'symbol': symbol, 'exchange': exchange} for symbol, exchange in keys]
        )
    except Exception as exc:
        current_app.logger.debug(f'Equity multiquotes unavailable: {exc}')
        response = None

    if isinstance(response, dict) and response.get('status') == 'success':
        entries = response.get('results')
        if not isinstance(entries, list):
            entries = response.get('data')
        if isinstance(entries, list):
            for entry in entries:
                if not isinstance(entry, dict):
                    continue
                symbol = str(entry.get('symbol') or '').strip().upper()
                if not symbol:
                    continue
                exchange = str(entry.get('exchange') or 'NSE').strip().upper()
                payload = entry.get('data') if isinstance(entry.get('data'), dict) else entry
                quotes[(symbol, exchange)] = {
                    'ltp': _to_float(payload.get('ltp')),
                    'prev_close': _to_float(payload.get('prev_close')),
                }

    missing = [key for key in keys if key not in quotes]
    if missing:
        quotes.update(_fetch_quotes_individually(app, cred, missing, deadline))

    return quotes


# ---------------------------------------------------------------------------
# Prices. Pushed by the shared WebSocket feed, with REST as a bounded backstop.
# ---------------------------------------------------------------------------

def _prune_feed_subscriptions(keys):
    """
    Release the symbols the equity screens no longer hold.

    Without this the feed's subscription set only ever grows as holdings change,
    until it reaches its own ceiling and starts refusing new symbols. Only a
    view that covers every account may prune, because a filtered view has not
    seen the symbols it is about to release.

    The watch list symbols are pinned as well as the ones passed in. Without
    that, a dashboard poll would release every watch list symbol that is not
    held, the next watch list poll would resubscribe it, and the two screens
    would churn the subscription set against each other. Nothing is released at
    all if the pinned set cannot be read, since releasing on a partial view is
    the one mistake this function must not make.
    """
    current = set(keys)
    try:
        current |= _watchlist_symbol_keys()
    except Exception as exc:
        current_app.logger.warning(
            f'Equity price feed prune skipped, watch list unreadable: {exc}',
            extra={'event': 'equity_feed_prune_skipped'}
        )
        return

    with _CACHE_LOCK:
        stale = _FEED_SYMBOLS - current
        _FEED_SYMBOLS.clear()
        _FEED_SYMBOLS.update(current)

    if not stale:
        return

    try:
        released = equity_price_feed.release(sorted(stale))
    except Exception as exc:
        current_app.logger.warning(
            f'Equity price feed release failed: {exc}',
            extra={'event': 'equity_feed_release_failed'}
        )
        return

    if released:
        current_app.logger.debug(
            f'Equity price feed released {released} symbols that are no longer held',
            extra={'event': 'equity_feed_released'}
        )


def _feed_status_block(stats):
    """
    Compact feed health for the JSON payloads, so a screen can say whether the
    prices it is showing were pushed or fetched.

    source is 'websocket' when every symbol in view came from the push feed,
    'rest' when none did, 'mixed' in between and 'none' when nothing is held.
    """
    try:
        status = equity_price_feed.status()
    except Exception as exc:
        current_app.logger.debug(f'Equity price feed status unavailable: {exc}')
        status = {}

    requested = stats.get('requested', 0)
    from_feed = stats.get('from_feed', 0)

    if requested <= 0:
        source = 'none'
    elif from_feed >= requested:
        source = 'websocket'
    elif from_feed <= 0:
        source = 'rest'
    else:
        source = 'mixed'

    return {
        'available': bool(status.get('available')),
        'authenticated': bool(status.get('authenticated')),
        'live': bool(status.get('authenticated')) and from_feed > 0,
        'source': source,
        'subscribed': _to_int(status.get('subscribed')),
        'pending': _to_int(status.get('pending')),
        'priced': _to_int(status.get('priced')),
        'ticks': _to_int(status.get('ticks')),
        'last_tick_at': status.get('last_tick_at'),
        'last_tick_age_seconds': status.get('last_tick_age_seconds'),
        'symbols_requested': requested,
        'symbols_from_feed': from_feed,
        'symbols_from_rest': stats.get('from_rest', 0),
        'rest_fallback_symbols': stats.get('fallback_symbols', 0),
    }


def _resolve_prices(creds, snapshots, symbol_keys, row_closes=None,
                    want_prev_close=False, prune=False):
    """
    Resolve the price of every symbol in view, event driven first.

    The shared WebSocket feed is the primary source and costs no broker call:
    the symbols in view are subscribed once (idempotent, so this is cheap on
    every poll) and read straight out of the pushed cache. REST is a backstop
    for two gaps only, both bounded by MAX_QUOTE_FALLBACK_SECONDS:
        a symbol the feed has no price for yet, normally just the first poll
            after it was subscribed,
        a previous close that is not known for today, because the feed
            subscribes in LTP mode and does not carry one. It is asked for once
            per symbol per day and then remembered, so Today's P&L survives
            without a quote round trip on every poll.
    With a warm feed and the closes already known, this makes zero broker calls.

    Returns (quotes, feed_block), where quotes keeps the shape the callers
    already expect: (symbol, exchange) to {'ltp', 'prev_close'}.
    """
    keys = sorted(set(symbol_keys))
    stats = {
        'requested': len(keys),
        'from_feed': 0,
        'from_rest': 0,
        'fallback_symbols': 0,
    }

    if not keys:
        # An empty view is usually a broker read that failed rather than a
        # portfolio that emptied, so nothing is released on it.
        return {}, _feed_status_block(stats)

    if prune:
        _prune_feed_subscriptions(keys)

    feed_prices = {}
    try:
        equity_price_feed.ensure_subscribed(keys)
        feed_prices = equity_price_feed.get_prices(keys)
    except Exception as exc:
        current_app.logger.warning(
            f'Equity price feed unavailable, falling back to REST quotes: {exc}',
            extra={'event': 'equity_feed_unavailable'}
        )
        feed_prices = {}

    row_closes = row_closes or {}
    quotes = {}
    for key in keys:
        ltp = _to_float(feed_prices.get(key))
        if ltp > 0:
            stats['from_feed'] += 1

        prev_close = _prev_close_cached(key)
        if prev_close <= 0:
            # Last resort for the day's reference price: the close the broker
            # put on the holding row itself. Free, and better than showing no
            # Today's P&L at all.
            prev_close = _to_float(row_closes.get(key))

        quotes[key] = {'ltp': ltp, 'prev_close': prev_close}

    missing_ltp = [key for key in keys if quotes[key]['ltp'] <= 0]
    missing_close = []
    if want_prev_close:
        missing_close = [
            key for key in keys
            if quotes[key]['ltp'] > 0
            and quotes[key]['prev_close'] <= 0
            and not _prev_close_asked(key)
        ]

    # Prices first: a missing price is visible on every screen, a missing
    # previous close costs one KPI. Whatever does not fit the budget is retried
    # on the next poll.
    fallback_keys = missing_ltp + missing_close
    if not fallback_keys:
        return quotes, _feed_status_block(stats)

    stats['fallback_symbols'] = len(fallback_keys)
    fetched = _fetch_quotes(
        _quote_credential(creds, snapshots),
        fallback_keys,
        deadline=time.monotonic() + MAX_QUOTE_FALLBACK_SECONDS
    )

    for key, data in fetched.items():
        entry = quotes.get(key)
        if entry is None:
            continue
        ltp = _to_float(data.get('ltp'))
        if ltp > 0 and entry['ltp'] <= 0:
            entry['ltp'] = ltp
            stats['from_rest'] += 1
        prev_close = _to_float(data.get('prev_close'))
        if prev_close > 0:
            entry['prev_close'] = prev_close
        # Recorded even when it is zero: the broker answered, and asking again
        # on every poll for a close it does not publish is pure latency.
        _remember_prev_close(key, prev_close)

    return quotes, _feed_status_block(stats)


# ---------------------------------------------------------------------------
# Broker payload normalisation
# ---------------------------------------------------------------------------

def _normalise_broker_holdings(holdings_data):
    """
    Extract the CNC delivery rows from an OpenAlgo holdings payload.

    Holdings are delivery by definition, so a broker adapter that leaves the
    product field empty is kept. A row that explicitly reports a product other
    than CNC belongs to another module and is skipped.
    """
    if isinstance(holdings_data, dict):
        rows = holdings_data.get('holdings') or []
    elif isinstance(holdings_data, list):
        rows = holdings_data
    else:
        rows = []

    normalised = []
    for row in rows:
        if not isinstance(row, dict):
            continue

        product = str(row.get('product') or '').strip().upper()
        if product and product != EQUITY_PRODUCT_CNC:
            continue

        symbol = str(row.get('symbol') or '').strip().upper()
        quantity = _to_int(row.get('quantity'))
        if not symbol or quantity <= 0:
            continue

        avg_cost = _first_number(row, _AVG_COST_KEYS)
        pnl = _to_float(row.get('pnl'))
        pnl_percent = _first_number(row, _PNL_PCT_KEYS)

        # The documented OpenAlgo holdings row carries only symbol, exchange,
        # product, quantity, pnl and pnlpercent: no average price and no LTP.
        # Without a cost basis, Stake percent, Avg Cost, Investment Value and
        # Gross P&L would every one of them render as a real-looking 0.00, so
        # reconstruct it from the P&L pair the way the existing F&O holdings
        # screen already does (app/trading/routes.py, totalinvvalue).
        cost_basis_derived = False
        if avg_cost <= 0.0 and pnl_percent:
            try:
                invested = abs(pnl / (pnl_percent / 100.0))
            except (TypeError, ValueError, ZeroDivisionError):
                invested = 0.0
            if invested > 0.0:
                avg_cost = invested / quantity
                cost_basis_derived = True

        normalised.append({
            'symbol': symbol,
            'exchange': str(row.get('exchange') or 'NSE').strip().upper() or 'NSE',
            'quantity': quantity,
            'avg_cost': avg_cost,
            'broker_ltp': _first_number(row, _LTP_KEYS),
            # Previous close, when the broker adapter publishes one on the
            # holding row. The push feed subscribes in LTP mode and carries no
            # close, so this is a free source for Today's P&L. It is only ever
            # used when no quote has answered for the symbol today.
            'prev_close': _first_number(row, _PREV_CLOSE_KEYS),
            'pnl': pnl,
            'pnl_percent': pnl_percent,
            # True when the broker gave no average price and the figure above was
            # implied from pnl and pnlpercent rather than reported directly.
            'cost_basis_derived': cost_basis_derived,
            # True when no cost basis could be established at all. The screen must
            # show a dash for these rather than a zero that reads as a real value.
            'cost_basis_missing': avg_cost <= 0.0,
            'pledged_quantity': _to_int(_first_number(row, _PLEDGED_KEYS)),
        })

    return normalised


def _resolve_ltp(row, quote, fallback_price=0.0):
    """
    Resolve the last traded price for one holding row.

    Preference order: a live quote, then an LTP the broker put on the holding
    row, then the price implied by the P&L the broker already reported, then a
    stored fallback price.

    The third step is not a business formula. It reconstructs a number the
    broker itself published: it inverts the P&L the broker computed from its
    own last price, so the screen agrees with the broker instead of showing a
    blank.
    """
    if quote and _to_float(quote.get('ltp')) > 0:
        return _to_float(quote['ltp'])

    if _to_float(row.get('broker_ltp')) > 0:
        return _to_float(row['broker_ltp'])

    quantity = _to_int(row.get('quantity'))
    avg_cost = _to_float(row.get('avg_cost'))
    if quantity > 0 and avg_cost > 0:
        return avg_cost + (_to_float(row.get('pnl')) / quantity)

    if _to_float(fallback_price) > 0:
        return _to_float(fallback_price)

    return avg_cost


def _holding_meta_map(account_ids):
    """
    AlgoMirror's own side of a holding (trade nature, stop loss, target, exit
    mode, pledged quantity), keyed by (account_id, symbol, exchange).
    """
    if not account_ids:
        return {}

    rows = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.account_id.in_(list(account_ids))
    ).all()

    return {
        (row.account_id, (row.symbol or '').strip().upper(), (row.exchange or 'NSE').strip().upper()): row
        for row in rows
    }


# ---------------------------------------------------------------------------
# Payload builders shared by the JSON endpoints, the pages and the CSV export
# ---------------------------------------------------------------------------

def _account_context(fetch_funds=False, fetch_holdings=False, fetch_account_ids=None):
    """
    Load accounts, allocations, ratios and (optionally) live broker data once,
    so every builder below works from the same snapshot.

    Allocations always cover every active account, because the ratio and the
    stake denominator are defined across all of them. fetch_account_ids narrows
    only the broker fan-out, so a Holdings view filtered to one account does not
    poll the other brokers.
    """
    accounts = _active_accounts()
    allocation_rows = _get_or_create_allocations(accounts)
    allocation_amounts = _allocation_amounts(accounts, allocation_rows)
    ratios = compute_order_qty_ratios(allocation_amounts)

    snapshots = {}
    creds = []
    if accounts and (fetch_funds or fetch_holdings):
        wanted = accounts
        if fetch_account_ids is not None:
            wanted = [account for account in accounts if account.id in set(fetch_account_ids)]
        creds = _account_credentials(wanted)
        snapshots = _fan_out(creds, want_funds=fetch_funds, want_holdings=fetch_holdings)
        _refresh_account_cache(wanted, snapshots)

    return {
        'accounts': accounts,
        'allocation_rows': allocation_rows,
        'allocation_amounts': allocation_amounts,
        'ratios': ratios,
        'snapshots': snapshots,
        'creds': creds,
    }


def _available_cash(snapshot):
    """Raw broker cash balance. Never an input to any equity formula."""
    funds = (snapshot or {}).get('funds') or {}
    return _to_float(funds.get('availablecash'))


def _build_accounts_payload(fetch_live=True):
    """
    M2 Accounts: one entry per account with live cash, the rupee allocation and
    the derived Order Qty Ratio, plus the footer totals.
    """
    context = _account_context(fetch_funds=fetch_live)
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_rows = context['allocation_rows']
    allocation_amounts = context['allocation_amounts']
    ratios = context['ratios']

    entries = []
    for account in accounts:
        snapshot = snapshots.get(account.id)
        row = allocation_rows.get(account.id)
        entries.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'connection_status': account.connection_status,
            'is_active': bool(account.is_active),
            'is_equity_active': True if row is None else row.is_active is not False,
            'available_cash': _money(_available_cash(snapshot)) if fetch_live else None,
            'is_stale': bool(snapshot.get('is_stale')) if snapshot else True,
            'error': (snapshot or {}).get('error'),
            'equity_fund_allocation': _money(allocation_amounts.get(account.id, 0.0)),
            'order_qty_ratio_pct': _pct(ratios.get(account.id, 0.0)),
        })

    total_allocation = sum(allocation_amounts.values())
    ratio_total = sum(ratios.values())

    return {
        'accounts': entries,
        'totals': {
            'total_equity_fund_allocation': _money(total_allocation),
            'order_qty_ratio_pct_total': _pct(ratio_total),
            'active_accounts': len(allocation_amounts),
            'total_available_cash': _money(
                sum(_available_cash(snapshots.get(account.id)) for account in accounts)
            ) if fetch_live else None,
        },
        'live_cash': bool(fetch_live),
        'rules': ALLOCATION_RULES,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_dashboard_payload():
    """
    M1 Dashboard.

    KPI DEFINITIONS. The approved mockup's KPI strip does not reconcile with its
    own account cards (Total Portfolio Value 224.60L against card holdings
    summing to 32.20L, Available Cash 105.60L against card cash summing to
    182.84L), so those figures are treated as illustrative sample data. Every
    KPI below is the honest sum of the per-account values actually shown on the
    cards:
        total_portfolio_value = sum of each card's Holdings Value (LTP x qty of
            the CNC delivery holdings).
        available_cash        = sum of each card's Available Cash.
        unrealised_pnl        = sum of each card's Unrealised P&L.
        todays_pnl            = sum of each card's Today's P&L.
        active_accounts       = number of active equity accounts included below.
        open_orders           = equity orders still PENDING or PARTIAL. Always
            zero in increment 1 because nothing can place an order yet.
    Correct these definitions here if the owner intended something else.
    """
    context = _account_context(fetch_funds=True, fetch_holdings=True)
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_amounts = context['allocation_amounts']
    ratios = context['ratios']

    per_account_rows = {}
    symbol_keys = set()
    row_closes = {}
    for account in accounts:
        rows = _normalise_broker_holdings((snapshots.get(account.id) or {}).get('holdings_data'))
        per_account_rows[account.id] = rows
        for row in rows:
            key = (row['symbol'], row['exchange'])
            symbol_keys.add(key)
            close = _to_float(row.get('prev_close'))
            if close > 0 and key not in row_closes:
                row_closes[key] = close

    # Prices come from the pushed feed. The dashboard sees every account, so it
    # is also the view that may release symbols that are no longer held.
    quotes, price_feed = _resolve_prices(
        context['creds'],
        snapshots,
        symbol_keys,
        row_closes=row_closes,
        want_prev_close=True,
        prune=True
    )
    meta_map = _holding_meta_map([account.id for account in accounts])

    cards = []
    stale_account_ids = []
    for account in accounts:
        snapshot = snapshots.get(account.id) or {}
        rows = per_account_rows.get(account.id, [])

        holdings_value = 0.0
        unrealised = 0.0
        todays = 0.0
        todays_known = False
        pledged_quantity = 0

        for row in rows:
            key = (row['symbol'], row['exchange'])
            meta = meta_map.get((account.id, row['symbol'], row['exchange']))
            quote = quotes.get(key)
            ltp = _resolve_ltp(row, quote, meta.last_price if meta else 0.0)
            quantity = row['quantity']

            # Fall back to AlgoMirror's own cost basis when the broker adapter
            # does not report an average price. Without this an account with a
            # live quote and no average price would read as pure profit.
            avg_cost = row['avg_cost']
            if avg_cost <= 0 and meta is not None:
                avg_cost = _to_float(meta.avg_cost)

            holdings_value += turnover(ltp, quantity)
            unrealised += gross_pnl(ltp, avg_cost, quantity) if avg_cost > 0 else 0.0

            prev_close = _to_float((quote or {}).get('prev_close'))
            if prev_close > 0:
                # Today's move times quantity. Structurally the same calculation
                # as gross P&L, with the previous close standing in for the
                # average cost, so the engine is reused rather than duplicated.
                todays += gross_pnl(ltp, prev_close, quantity)
                todays_known = True

            pledged = row['pledged_quantity']
            if pledged <= 0 and meta is not None:
                pledged = _to_int(meta.pledged_quantity)
            pledged_quantity += max(pledged, 0)

        allocation = allocation_amounts.get(account.id, 0.0)
        if snapshot.get('is_stale'):
            stale_account_ids.append(account.id)

        # Pledge percent. Pledged stock is lodged as collateral at a haircut, so
        # the collateral the broker reports is smaller than the stock behind it.
        # Collateral is the account's available margin minus its raw cash, which
        # is exactly how those two figures are quoted on the F&O dashboard.
        funds = snapshot.get('funds') or {}
        cash = _available_cash(snapshot)
        collateral = _to_float(funds.get('collateral'))
        if collateral <= 0:
            # Some broker adapters report an available margin instead of a
            # separate collateral figure. Derive it from the pair in that case.
            collateral = collateral_from_margin(
                _first_number(funds, _AVAILABLE_MARGIN_KEYS), cash
            )

        cards.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'connection_status': account.connection_status,
            'order_qty_ratio_pct': _pct(ratios.get(account.id, 0.0)),
            'equity_fund_allocation': _money(allocation),
            'available_cash': _money(_available_cash(snapshot)),
            'holdings_value': _money(holdings_value),
            'invested_pct': _pct(invested_percent(holdings_value, allocation)),
            'pledged_quantity': pledged_quantity,
            'pledge_pct': _pct(pledge_percent(collateral, holdings_value)),
            'collateral': _money(collateral),
            'unrealised_pnl': _money(unrealised),
            'todays_pnl': _money(todays),
            'todays_pnl_available': todays_known,
            'holdings_count': len(rows),
            'is_stale': bool(snapshot.get('is_stale')),
            'error': snapshot.get('error'),
        })

    open_orders = EquityOrder.query.filter(
        EquityOrder.user_id == current_user.id,
        EquityOrder.status.in_([EQUITY_ORDER_STATUS_PENDING, EQUITY_ORDER_STATUS_PARTIAL])
    ).count()

    kpi = {
        'active_accounts': len(allocation_amounts),
        'connected_accounts': sum(1 for card in cards if not card['is_stale']),
        'total_portfolio_value': _money(sum(card['holdings_value'] for card in cards)),
        'available_cash': _money(sum(card['available_cash'] for card in cards)),
        'unrealised_pnl': _money(sum(card['unrealised_pnl'] for card in cards)),
        'todays_pnl': _money(sum(card['todays_pnl'] for card in cards)),
        'todays_pnl_available': any(card['todays_pnl_available'] for card in cards),
        'open_orders': open_orders,
        'total_equity_fund_allocation': _money(sum(allocation_amounts.values())),
    }

    return {
        'kpi': kpi,
        'accounts': cards,
        'todays_orders': _build_todays_orders(),
        'stale_account_ids': stale_account_ids,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_todays_orders():
    """
    Today's equity orders for the dashboard, newest first.

    Deliberately today only, with no carry-over of an older resting GTT: this
    list is headed Today's Orders and a yesterday order in it would be a lie.
    The Order Status panel and the Order Book do carry an older open GTT, see
    _order_window.

    The day boundary is UTC. Indian market hours (09:15 to 15:30 IST) map to
    03:45 to 10:00 UTC on the same calendar date, so a trading day never
    straddles the UTC boundary.

    Every key increment 1 published is still published. The extra keys come
    from _order_payload, which the new screens share.
    """
    orders = EquityOrder.query.filter(
        EquityOrder.user_id == current_user.id,
        EquityOrder.placed_at >= _today_start()
    ).order_by(EquityOrder.placed_at.desc(), EquityOrder.id.desc()).all()

    directory = _account_directory()
    return [
        _order_payload(order, order.splits.all(), directory)
        for order in orders
    ]


def _selected_account_id():
    """
    Read the account filter. Returns (account_id_or_None, error_message_or_None).
    An unknown or unowned id is an error, never silently widened to all
    accounts.
    """
    raw = (request.args.get('account') or '').strip()
    if not raw or raw.lower() == 'all':
        return None, None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        return None, 'Invalid account filter'
    account = TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id,
        is_active=True
    ).first()
    if not account:
        return None, 'Account not found'
    return account_id, None


def _selected_trade_nature_id():
    """Read the trade nature filter. Returns (nature_id_or_None, error_or_None)."""
    raw = (request.args.get('trade_nature') or '').strip()
    if not raw or raw.lower() == 'all':
        return None, None
    try:
        nature_id = int(raw)
    except (TypeError, ValueError):
        return None, 'Invalid trade nature filter'
    nature = EquityTradeNature.query.filter_by(
        id=nature_id,
        user_id=current_user.id
    ).first()
    if not nature:
        return None, 'Trade nature not found'
    return nature_id, None


def _build_holdings_payload(account_filter, nature_filter):
    """
    M7 Holdings.

    Rows are aggregated by (symbol, exchange, trade nature) across the accounts
    in view. Grouping by trade nature as well as symbol means a symbol held
    under two different natures is shown honestly as two rows, each with its own
    stop loss, target and exit mode, instead of one row picking a winner. In the
    normal case, where a symbol carries one nature, this is exactly one row per
    symbol.

    Est. Costs is the cost of exiting the position: side SELL, one order and one
    scrip per contributing account, priced at the current LTP. That is what has
    to be paid to realise the P&L shown next to it.
    """
    context = _account_context(
        fetch_holdings=True,
        fetch_account_ids=None if account_filter is None else [account_filter]
    )
    accounts = context['accounts']
    snapshots = context['snapshots']
    allocation_amounts = context['allocation_amounts']

    accounts_in_view = [
        account for account in accounts
        if account_filter is None or account.id == account_filter
    ]
    account_ids = [account.id for account in accounts_in_view]
    account_names = {account.id: account.account_name for account in accounts_in_view}
    broker_names = {account.id: account.broker_name for account in accounts_in_view}

    natures = _trade_natures()
    nature_names = {nature.id: nature.name for nature in natures}
    nature_order = {nature.id: index for index, nature in enumerate(natures)}

    meta_map = _holding_meta_map(account_ids)
    rates_by_account, unconfigured_rate_accounts = _brokerage_rates_by_account(account_ids)

    per_account_rows = {}
    symbol_keys = set()
    for account in accounts_in_view:
        rows = _normalise_broker_holdings((snapshots.get(account.id) or {}).get('holdings_data'))
        per_account_rows[account.id] = rows
        for row in rows:
            symbol_keys.add((row['symbol'], row['exchange']))

    # context['creds'] already covers exactly the accounts in view. Prices come
    # from the pushed feed, and only an unfiltered view may release symbols,
    # because a view filtered to one account has not seen the rest.
    quotes, price_feed = _resolve_prices(
        context['creds'],
        snapshots,
        symbol_keys,
        prune=account_filter is None
    )

    buckets = {}
    stale_account_ids = []
    for account in accounts_in_view:
        snapshot = snapshots.get(account.id) or {}
        if snapshot.get('is_stale'):
            stale_account_ids.append(account.id)

        for row in per_account_rows.get(account.id, []):
            meta = meta_map.get((account.id, row['symbol'], row['exchange']))
            nature_id = meta.trade_nature_id if meta is not None else None

            if nature_filter is not None and nature_id != nature_filter:
                continue

            key = (row['symbol'], row['exchange'], nature_id)
            bucket = buckets.get(key)
            if bucket is None:
                bucket = {
                    'symbol': row['symbol'],
                    'exchange': row['exchange'],
                    'trade_nature_id': nature_id,
                    'trade_nature': nature_names.get(nature_id) if nature_id else None,
                    'total_quantity': 0,
                    'pledged_quantity': 0,
                    'at_cost': 0.0,
                    'current_value': 0.0,
                    'gross_pnl': 0.0,
                    'est_costs': 0.0,
                    'ltp': 0.0,
                    'accounts': [],
                    'levels': set(),
                    'is_stale': False,
                }
                buckets[key] = bucket

            quantity = row['quantity']
            avg_cost = row['avg_cost']
            if avg_cost <= 0 and meta is not None:
                avg_cost = _to_float(meta.avg_cost)

            quote = quotes.get((row['symbol'], row['exchange']))
            ltp = _resolve_ltp(row, quote, meta.last_price if meta is not None else 0.0)

            at_cost = stock_at_cost(avg_cost, quantity)
            value = turnover(ltp, quantity)
            # With no cost basis at all there is no P&L to report. Reporting one
            # would make the whole market value look like profit.
            gross = gross_pnl(ltp, avg_cost, quantity) if avg_cost > 0 else 0.0
            costs = estimate_costs(
                value,
                EQUITY_SIDE_SELL,
                rates_by_account.get(account.id, BrokerageRates()),
                scrip_count=1
            )

            pledged = row['pledged_quantity']
            if pledged <= 0 and meta is not None:
                pledged = _to_int(meta.pledged_quantity)
            pledged = max(pledged, 0)

            exit_mode = (meta.exit_mode if meta is not None and meta.exit_mode else EQUITY_EXIT_MODE_CONFIRM)
            stop_loss = _to_float(meta.stop_loss) if meta is not None and meta.stop_loss is not None else None
            target = _to_float(meta.target) if meta is not None and meta.target is not None else None

            bucket['total_quantity'] += quantity
            bucket['pledged_quantity'] += pledged
            bucket['at_cost'] += at_cost
            bucket['current_value'] += value
            bucket['gross_pnl'] += gross
            bucket['est_costs'] += costs.total
            bucket['ltp'] = ltp
            bucket['levels'].add((stop_loss, target, exit_mode))
            bucket['is_stale'] = bucket['is_stale'] or bool(snapshot.get('is_stale'))
            bucket['accounts'].append({
                'account_id': account.id,
                'account_name': account_names.get(account.id),
                'broker_name': broker_names.get(account.id),
                'quantity': quantity,
                'avg_cost': _money(avg_cost),
                'pledged_quantity': pledged,
                'stop_loss': _money(stop_loss) if stop_loss is not None else None,
                'target': _money(target) if target is not None else None,
                'exit_mode': exit_mode,
                'est_costs': _money(costs.total),
            })

    rows = []
    for bucket in buckets.values():
        quantity = bucket['total_quantity']
        at_cost = bucket['at_cost']

        # The stop loss, target and exit mode shown on the aggregated row come
        # from the largest contributing account. levels_mixed says the
        # contributors do not agree, which the per-account breakdown spells out.
        primary = max(bucket['accounts'], key=lambda entry: entry['quantity']) if bucket['accounts'] else {}
        exit_mode = primary.get('exit_mode') or EQUITY_EXIT_MODE_CONFIRM

        rows.append({
            'symbol': bucket['symbol'],
            'exchange': bucket['exchange'],
            'trade_nature_id': bucket['trade_nature_id'],
            'trade_nature': bucket['trade_nature'] or 'Unassigned',
            'total_quantity': quantity,
            'stake_pct': _pct(stake_percent_for_view(at_cost, allocation_amounts, account_filter)),
            'avg_cost': _money(_safe_divide(at_cost, quantity)),
            'ltp': _money(bucket['ltp']),
            # Gross P&L is signed, so this must keep the sign. percent_of would
            # clamp a loss to 0.0 and every losing row would read as flat.
            'pnl_pct': _pct(signed_percent_of(bucket['gross_pnl'], at_cost)),
            'stop_loss': primary.get('stop_loss'),
            'target': primary.get('target'),
            'exit_mode': exit_mode,
            'exit_mode_tag': EXIT_MODE_TAGS.get(exit_mode, EXIT_MODE_TAGS[EQUITY_EXIT_MODE_CONFIRM]),
            'levels_mixed': len(bucket['levels']) > 1,
            'pledged_quantity': bucket['pledged_quantity'],
            'pledged_pct': _pct(percent_of(bucket['pledged_quantity'], quantity)),
            'investment_value': _money(at_cost),
            'current_value': _money(bucket['current_value']),
            'gross_pnl': _money(bucket['gross_pnl']),
            'est_costs': _money(bucket['est_costs']),
            'net_pnl': _money(net_pnl(bucket['gross_pnl'], bucket['est_costs'])),
            'accounts': bucket['accounts'],
            'is_stale': bucket['is_stale'],
        })

    rows.sort(key=lambda row: (
        nature_order.get(row['trade_nature_id'], len(nature_order)),
        row['symbol']
    ))

    grouped = nature_filter is None
    groups = []
    for row in rows:
        if groups and groups[-1]['trade_nature_id'] == row['trade_nature_id']:
            groups[-1]['rows'].append(row)
        else:
            groups.append({
                'trade_nature_id': row['trade_nature_id'],
                'trade_nature': row['trade_nature'],
                'rows': [row],
            })

    total_investment = sum(row['investment_value'] for row in rows)
    total_current = sum(row['current_value'] for row in rows)
    total_gross = sum(row['gross_pnl'] for row in rows)
    total_costs = sum(row['est_costs'] for row in rows)

    return {
        'kpi': {
            'total_holdings': len(rows),
            'total_investment': _money(total_investment),
            'current_value': _money(total_current),
            'gross_pnl': _money(total_gross),
            'est_costs': _money(total_costs),
            'net_pnl': _money(net_pnl(total_gross, total_costs)),
        },
        'holdings': rows,
        'groups': groups,
        'grouped': grouped,
        'filters': {
            'account': account_filter if account_filter is not None else 'all',
            'trade_nature': nature_filter if nature_filter is not None else 'all',
        },
        'accounts': [
            {
                'account_id': account.id,
                'account_name': account.account_name,
                'broker_name': account.broker_name,
            }
            for account in accounts
        ],
        'trade_natures': [
            {'id': nature.id, 'name': nature.name}
            for nature in natures
        ],
        'stake_denominator': _money(
            sum(allocation_amounts.values()) if account_filter is None
            else allocation_amounts.get(account_filter, 0.0)
        ),
        'stale_account_ids': stale_account_ids,
        'accounts_missing_rates': unconfigured_rate_accounts,
        'exit_mode_tags': EXIT_MODE_TAGS,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_rates_payload():
    """Settings: the rate version in effect today for each account."""
    accounts = _active_accounts()
    today = date.today()

    entries = []
    for account in accounts:
        row = EquityBrokerageRate.get_effective_rate(current_user.id, account.id, today)
        entries.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'rate_id': row.id if row else None,
            'is_configured': row is not None,
            'effective_from': _iso(row.effective_from) if row else None,
            'brokerage_per_order': _money(row.brokerage_per_order) if row else 0.0,
            'stt_pct': _to_float(row.stt_pct) if row else 0.0,
            'exchange_txn_pct': _to_float(row.exchange_txn_pct) if row else 0.0,
            'sebi_pct': _to_float(row.sebi_pct) if row else 0.0,
            'stamp_duty_pct': _to_float(row.stamp_duty_pct) if row else 0.0,
            'gst_pct': _to_float(row.gst_pct) if row else 0.0,
            'dp_amc_charge': _money(row.dp_amc_charge) if row else 0.0,
        })

    return {
        'rates': entries,
        'today': _iso(today),
        'suggested_defaults': SUGGESTED_RATE_DEFAULTS,
        'notes': COST_FORMULA_NOTES,
        'generated_at': _iso(datetime.utcnow()),
    }


def _log_activity(action, details=None, account_id=None):
    """Audit trail entry. Never lets a logging failure break the request."""
    try:
        entry = ActivityLog(
            user_id=current_user.id,
            account_id=account_id,
            action=action,
            details=details,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            status='success'
        )
        db.session.add(entry)
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.warning(f'Could not write equity activity log: {exc}')


# ---------------------------------------------------------------------------
# Page routes
# ---------------------------------------------------------------------------

@equity_bp.route('/')
@login_required
def dashboard():
    """M1 Dashboard. Data is loaded by the browser from /equity/api/dashboard."""
    return render_template(
        'equity/dashboard.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/accounts')
@login_required
def accounts():
    """M2 Accounts. Read only against the broker, it writes only the allocation."""
    account_rows = _active_accounts()
    _get_or_create_allocations(account_rows)
    return render_template(
        'equity/accounts.html',
        accounts=account_rows,
        trade_natures=_trade_natures(),
        allocation_rules=ALLOCATION_RULES
    )


@equity_bp.route('/holdings')
@login_required
def holdings():
    """M7 Holdings."""
    return render_template(
        'equity/holdings.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/settings')
@login_required
def settings():
    """Settings: brokerage and statutory charges, versioned by effective date."""
    return render_template(
        'equity/settings.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures(),
        cost_formula_notes=COST_FORMULA_NOTES
    )


# ---------------------------------------------------------------------------
# JSON routes
# ---------------------------------------------------------------------------

@equity_bp.route('/api/dashboard')
@login_required
@heavy_rate_limit()
def api_dashboard():
    """
    KPI strip plus one card per account.

    Prices come from the pushed WebSocket feed and funds and holdings from the
    account cache, so a poll inside the freshness window makes no broker call at
    all. The price_feed block in the response says how many of the prices in
    view were pushed and how many fell back to REST.
    """
    try:
        payload = _build_dashboard_payload()
    except Exception as exc:
        current_app.logger.error(f'Equity dashboard failed: {exc}')
        return _json_error(f'Failed to build equity dashboard: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/accounts')
@login_required
@heavy_rate_limit()
def api_accounts():
    """Allocations with the derived Order Qty Ratio and live Available Cash."""
    try:
        payload = _build_accounts_payload(fetch_live=True)
    except Exception as exc:
        current_app.logger.error(f'Equity accounts failed: {exc}')
        return _json_error(f'Failed to load equity accounts: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/accounts/allocation', methods=['POST'])
@login_required
@api_rate_limit()
def api_save_allocation():
    """
    Save the rupee equity fund allocation per account and recompute the ratios.

    Writes to AlgoMirror's own table only, no broker call. The change is future
    dated: it re-derives the ratio used from now on and never touches the ratio
    already recorded against a past order.

    Request body, either form is accepted:
        {"allocations": [{"account_id": 1, "equity_fund_allocation": 2000000}]}
        {"allocations": {"1": 2000000, "2": 1000000}}
    """
    data = request.get_json(silent=True) or {}
    raw = data.get('allocations')

    if isinstance(raw, dict):
        items = [
            {'account_id': key, 'equity_fund_allocation': value}
            for key, value in raw.items()
        ]
    elif isinstance(raw, list):
        items = raw
    else:
        return _json_error('No allocations supplied')

    if not items:
        return _json_error('No allocations supplied')

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            return _json_error('Each allocation must be an object')
        try:
            account_id = int(item.get('account_id'))
        except (TypeError, ValueError):
            return _json_error('Invalid account id in allocations')

        amount = item.get('equity_fund_allocation')
        if amount is None:
            amount = item.get('allocation')
        try:
            amount = float(amount)
        except (TypeError, ValueError):
            return _json_error(f'Invalid allocation amount for account {account_id}')
        if not math.isfinite(amount) or amount < 0:
            return _json_error(f'Allocation must be zero or more for account {account_id}')

        # Ownership scoping: both the id and the owner, never the id alone.
        account = _owned_account(account_id)
        if not account:
            return _json_error(f'Account {account_id} not found', 404)

        parsed.append((account, amount))

    try:
        existing = {
            row.account_id: row
            for row in EquityAccountAllocation.query.filter_by(user_id=current_user.id).all()
        }
        changes = []
        for account, amount in parsed:
            row = existing.get(account.id)
            if row is None:
                row = EquityAccountAllocation(
                    account_id=account.id,
                    user_id=current_user.id,
                    equity_fund_allocation=amount
                )
                db.session.add(row)
            else:
                previous = _to_float(row.equity_fund_allocation)
                if previous != amount:
                    changes.append({
                        'account_id': account.id,
                        'account_name': account.account_name,
                        'from': previous,
                        'to': amount,
                    })
                row.equity_fund_allocation = amount
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity allocation save failed: {exc}')
        return _json_error(f'Failed to save equity allocation: {exc}', 500)

    if changes:
        _log_activity('equity_allocation_updated', {'changes': changes})

    # Rebuild without a broker call so saving stays fast. The available_cash
    # fields come back null and live_cash is false, so the screen keeps the cash
    # figures it already has and refreshes only the allocation and the ratio.
    payload = _build_accounts_payload(fetch_live=False)
    payload['status'] = 'success'
    payload['message'] = 'Equity allocation saved and ratios recomputed'
    return jsonify(payload)


@equity_bp.route('/api/holdings')
@login_required
@heavy_rate_limit()
def api_holdings():
    """Holdings with stake percent, gross P&L, estimated costs and net P&L."""
    account_filter, error = _selected_account_id()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    nature_filter, error = _selected_trade_nature_id()
    if error:
        return _json_error(error, 404 if error == 'Trade nature not found' else 400)

    try:
        payload = _build_holdings_payload(account_filter, nature_filter)
    except Exception as exc:
        current_app.logger.error(f'Equity holdings failed: {exc}')
        return _json_error(f'Failed to load equity holdings: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


@equity_bp.route('/api/holdings/export')
@login_required
@heavy_rate_limit()
def api_holdings_export():
    """
    CSV export of the Holdings screen. A pure read: it runs the same builder as
    /equity/api/holdings and serialises the result.
    """
    account_filter, error = _selected_account_id()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    nature_filter, error = _selected_trade_nature_id()
    if error:
        return _json_error(error, 404 if error == 'Trade nature not found' else 400)

    try:
        payload = _build_holdings_payload(account_filter, nature_filter)
    except Exception as exc:
        current_app.logger.error(f'Equity holdings export failed: {exc}')
        return _json_error(f'Failed to export equity holdings: {exc}', 500)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'Symbol', 'Exchange', 'Trade Nature', 'Total Qty', 'Stake %', 'Avg Cost',
        'LTP', 'P&L %', 'Stop Loss', 'Target', 'Exit Mode', 'Pledged Qty',
        'Pledged %', 'Investment', 'Current Value', 'Gross P&L', 'Est. Costs',
        'Net P&L'
    ])
    for row in payload['holdings']:
        writer.writerow([
            row['symbol'],
            row['exchange'],
            row['trade_nature'],
            row['total_quantity'],
            row['stake_pct'],
            row['avg_cost'],
            row['ltp'],
            row['pnl_pct'],
            '' if row['stop_loss'] is None else row['stop_loss'],
            '' if row['target'] is None else row['target'],
            row['exit_mode_tag'],
            row['pledged_quantity'],
            row['pledged_pct'],
            row['investment_value'],
            row['current_value'],
            row['gross_pnl'],
            row['est_costs'],
            row['net_pnl'],
        ])

    kpi = payload['kpi']
    writer.writerow([])
    writer.writerow([
        'TOTAL', '', '', '', '', '', '', '', '', '', '', '', '',
        kpi['total_investment'], kpi['current_value'], kpi['gross_pnl'],
        kpi['est_costs'], kpi['net_pnl']
    ])

    filename = f'equity_holdings_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@equity_bp.route('/api/settings/rates')
@login_required
@api_rate_limit()
def api_settings_rates():
    """The brokerage and statutory rate version currently in effect per account."""
    try:
        payload = _build_rates_payload()
    except Exception as exc:
        current_app.logger.error(f'Equity rates load failed: {exc}')
        return _json_error(f'Failed to load brokerage rates: {exc}', 500)

    payload['status'] = 'success'
    payload['message'] = ''
    return jsonify(payload)


def _parse_rate_field(item, field, maximum=None):
    """Read one rate field as a non-negative finite number. Raises ValueError."""
    value = item.get(field, 0)
    if value is None or value == '':
        value = 0
    try:
        number = float(value)
    except (TypeError, ValueError):
        raise ValueError(f'Invalid value for {field}')
    if not math.isfinite(number) or number < 0:
        raise ValueError(f'{field} must be zero or more')
    if maximum is not None and number > maximum:
        raise ValueError(f'{field} must not exceed {maximum}')
    return number


@equity_bp.route('/api/settings/rates', methods=['POST'])
@login_required
@api_rate_limit()
def api_save_settings_rates():
    """
    Save brokerage and statutory rates as a NEW effective-dated version.

    A historical row is never updated in place, so past cost figures stay
    reproducible. The only row this can overwrite is one whose effective_from
    equals the requested date: that row is the version being authored for that
    date, not history, and the unique constraint on (account_id, effective_from)
    allows only one of them. Backdating is rejected, because a rate change
    applies to future calculations only.

    Request body:
        {"effective_from": "2026-08-25",
         "rates": [{"account_id": 1, "brokerage_per_order": 20,
                    "stt_pct": 0.1, "exchange_txn_pct": 0.00297,
                    "sebi_pct": 0.0001, "stamp_duty_pct": 0.015,
                    "gst_pct": 18, "dp_amc_charge": 13.5}]}
    """
    data = request.get_json(silent=True) or {}
    items = data.get('rates')
    if not isinstance(items, list) or not items:
        return _json_error('No rates supplied')

    today = date.today()
    raw_date = (data.get('effective_from') or '').strip()
    if raw_date:
        try:
            effective_from = datetime.strptime(raw_date, '%Y-%m-%d').date()
        except (TypeError, ValueError):
            return _json_error('Invalid effective_from, expected YYYY-MM-DD')
        if effective_from < today:
            return _json_error(
                'Rates can be dated today or later only. Rate changes apply to '
                'future calculations, past cost figures are never rewritten.'
            )
    else:
        effective_from = today

    parsed = []
    for item in items:
        if not isinstance(item, dict):
            return _json_error('Each rate entry must be an object')
        try:
            account_id = int(item.get('account_id'))
        except (TypeError, ValueError):
            return _json_error('Invalid account id in rates')

        # Ownership scoping: both the id and the owner.
        account = _owned_account(account_id)
        if not account:
            return _json_error(f'Account {account_id} not found', 404)

        try:
            values = {
                'brokerage_per_order': _parse_rate_field(item, 'brokerage_per_order'),
                'stt_pct': _parse_rate_field(item, 'stt_pct', maximum=100),
                'exchange_txn_pct': _parse_rate_field(item, 'exchange_txn_pct', maximum=100),
                'sebi_pct': _parse_rate_field(item, 'sebi_pct', maximum=100),
                'stamp_duty_pct': _parse_rate_field(item, 'stamp_duty_pct', maximum=100),
                'gst_pct': _parse_rate_field(item, 'gst_pct', maximum=100),
                'dp_amc_charge': _parse_rate_field(item, 'dp_amc_charge'),
            }
        except ValueError as exc:
            return _json_error(f'Account {account.account_name}: {exc}')

        parsed.append((account, values))

    try:
        saved = []
        for account, values in parsed:
            existing = EquityBrokerageRate.query.filter_by(
                user_id=current_user.id,
                account_id=account.id,
                effective_from=effective_from
            ).first()

            if existing is None:
                row = EquityBrokerageRate(
                    user_id=current_user.id,
                    account_id=account.id,
                    broker_name=account.broker_name,
                    effective_from=effective_from,
                    is_active=True,
                    **values
                )
                db.session.add(row)
            else:
                existing.broker_name = account.broker_name
                existing.is_active = True
                for field, value in values.items():
                    setattr(existing, field, value)

            saved.append({'account_id': account.id, 'account_name': account.account_name})

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity rate save failed: {exc}')
        return _json_error(f'Failed to save brokerage rates: {exc}', 500)

    _log_activity('equity_brokerage_rates_saved', {
        'effective_from': effective_from.isoformat(),
        'accounts': saved
    })

    payload = _build_rates_payload()
    payload['status'] = 'success'
    payload['message'] = (
        f'Rates saved as a new version effective {effective_from.isoformat()}. '
        'Existing versions are unchanged.'
    )
    return jsonify(payload)


# ===========================================================================
# INCREMENT 2: the transactional surface
#
# Everything below this line can move real money across five live accounts at
# once. Three rules decide its shape.
#
# 1. Every broker WRITE goes through app.utils.equity_order_engine. Nothing in
#    this module calls placeorder, modifyorder or cancelorder, and nothing here
#    posts a GTT endpoint. The only broker calls made here are reads: funds,
#    holdings, quotes, multiquotes, depth and search.
#
# 2. Every SELL is claimed before it is sent. A sell against a tracked holding
#    is an exit, and the background stop loss monitor can decide to sell the
#    same shares in the same second. equity_order_engine.exit_holding is the
#    single helper that locks the row, commits the claim and only then calls
#    the broker, so the manual path and the monitor meet at the database rather
#    than at the broker. That is why Place Order routes a SELL through the exit
#    helper instead of through plain placement.
#
# 3. A partial failure is a normal outcome, not an error. Three accounts
#    placing while two fail is a success for those three. Nothing here rolls
#    back an order that reached a broker, and one account's failure never stops
#    another account's order.
# ===========================================================================

# Upper bound on the exit fan-out started by one request. One worker per
# account, capped, exactly as the read fan-out above is capped.
MAX_EXIT_WORKERS = 5

# Symbol search. NSE and BSE cash segments only: this module trades CNC
# delivery, so a futures or options contract is never a valid result.
MAX_SEARCH_RESULTS = 40
SEARCH_EXCHANGES = ('NSE', 'BSE')
# Instrument type fragments that mark a derivative or an index rather than a
# tradable equity or ETF. Matched as substrings of the upper cased type.
NON_EQUITY_INSTRUMENT_FRAGMENTS = ('FUT', 'OPT', 'IDX', 'INDEX')

# Market depth. The PRD asks for five levels of bid and offer.
DEPTH_LEVELS = 5

# Watch list ceiling. The shared price feed has its own subscription limit and
# the holdings screens need room inside it, so the watch list is bounded well
# below it rather than being allowed to consume the whole budget.
MAX_WATCHLIST_ITEMS = 100

# Order Status sorting, from PRD M4b: open orders surface first.
ORDER_STATUS_SORT_RANK = {
    EQUITY_ORDER_STATUS_PENDING: 0,
    EQUITY_ORDER_STATUS_PARTIAL: 1,
    EQUITY_ORDER_STATUS_COMPLETED: 2,
    EQUITY_ORDER_STATUS_CANCELLED: 3,
}

# Parent order statuses that still count as working.
OPEN_ORDER_STATUSES = (EQUITY_ORDER_STATUS_PENDING, EQUITY_ORDER_STATUS_PARTIAL)

# Accepted request values, validated rather than trusted.
VALID_SIDES = (EQUITY_SIDE_BUY, EQUITY_SIDE_SELL)
VALID_ORDER_TYPES = (
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_ORDER_TYPE_LIMIT,
    EQUITY_ORDER_TYPE_GTT,
)
VALID_ORDER_STATUSES = (
    EQUITY_ORDER_STATUS_PENDING,
    EQUITY_ORDER_STATUS_PARTIAL,
    EQUITY_ORDER_STATUS_COMPLETED,
    EQUITY_ORDER_STATUS_CANCELLED,
)
VALID_EXIT_MODES = (EQUITY_EXIT_MODE_AUTO, EQUITY_EXIT_MODE_CONFIRM)
VALID_FUNDS_ACTIONS = (EQUITY_FUNDS_ACTION_SKIP, EQUITY_FUNDS_ACTION_ABORT)
VALID_ALERT_DIRECTIONS = (EQUITY_ALERT_DIRECTION_ABOVE, EQUITY_ALERT_DIRECTION_BELOW)

# Bounds on the stop loss monitor interval, matched to the monitor's own clamp.
MIN_MONITOR_INTERVAL_SECONDS = 1
MAX_MONITOR_INTERVAL_SECONDS = 300

# Broker payload key aliases for the depth panel. Adapters differ, so each
# value is resolved from the first key that carries a number.
_DEPTH_QTY_KEYS = ('quantity', 'qty', 'volume')
_DEPTH_ORDERS_KEYS = ('orders', 'no_of_orders', 'numberoforders', 'ordercount', 'order_count')
_TOTAL_BUY_KEYS = ('totalbuyqty', 'total_buy_qty', 'totalbuyquantity', 'totalbuyquantity')
_TOTAL_SELL_KEYS = ('totalsellqty', 'total_sell_qty', 'totalsellquantity')
_UPPER_CIRCUIT_KEYS = ('upper_circuit', 'uppercircuit', 'upper_circuit_limit', 'ucl')
_LOWER_CIRCUIT_KEYS = ('lower_circuit', 'lowercircuit', 'lower_circuit_limit', 'lcl')
_LTQ_KEYS = ('ltq', 'last_quantity', 'lasttradequantity', 'last_trade_quantity')
_VOLUME_KEYS = ('volume', 'totaltradedvolume', 'total_traded_volume', 'vol')
_LTT_KEYS = ('ltt', 'last_trade_time', 'lasttradetime', 'timestamp')
_INSTRUMENT_TYPE_KEYS = ('instrumenttype', 'instrument_type', 'instrument', 'segment')
_SEARCH_NAME_KEYS = ('name', 'company', 'companyname', 'description', 'symbol_name')


class _BadRequest(ValueError):
    """
    A request the caller got wrong.

    Raised by the readers below and turned into a 400 by the routes, so every
    endpoint validates its input in one place instead of each one inventing its
    own error shape.
    """


# ---------------------------------------------------------------------------
# Request readers. Nothing below this point trusts a request value.
# ---------------------------------------------------------------------------

def _body():
    """The JSON request body as a dict. An empty body is an empty dict."""
    data = request.get_json(silent=True)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise _BadRequest('The request body must be a JSON object')
    return data


def _read_text(data, field, maximum=50, required=True, default=''):
    """Read a trimmed string field."""
    value = data.get(field)
    value = '' if value is None else str(value).strip()
    if not value:
        if required:
            raise _BadRequest(f'{field} is required')
        return default
    if len(value) > maximum:
        raise _BadRequest(f'{field} must be {maximum} characters or fewer')
    return value


def _read_symbol(data, field='symbol', required=True):
    """Read a trading symbol, upper cased."""
    value = _read_text(data, field, maximum=50, required=required)
    return value.upper() if value else value


def _read_exchange(data, field='exchange', default='NSE'):
    """Read an exchange code, upper cased, defaulting to NSE."""
    value = _read_text(data, field, maximum=20, required=False, default=default)
    return (value or default).upper()


def _read_int(data, field, minimum=None, maximum=None, default=None, required=False):
    """Read a whole number field."""
    raw = data.get(field)
    if raw is None or raw == '':
        if required:
            raise _BadRequest(f'{field} is required')
        return default
    try:
        number = int(str(raw).strip())
    except (TypeError, ValueError):
        raise _BadRequest(f'{field} must be a whole number')
    if minimum is not None and number < minimum:
        raise _BadRequest(f'{field} must be {minimum} or more')
    if maximum is not None and number > maximum:
        raise _BadRequest(f'{field} must be {maximum} or less')
    return number


def _read_price(data, field, required=False):
    """
    Read a positive rupee price, or None when the field is absent or blank.

    A blank price is not the same as a zero price: zero is rejected, because a
    zero limit price is an order nobody meant to place.
    """
    raw = data.get(field)
    if raw is None or raw == '':
        if required:
            raise _BadRequest(f'{field} is required')
        return None
    try:
        number = float(raw)
    except (TypeError, ValueError):
        raise _BadRequest(f'{field} must be a number')
    if not math.isfinite(number) or number <= 0:
        raise _BadRequest(f'{field} must be a positive number')
    return round(number, 4)


def _read_bool(data, field, default=None):
    """Read a boolean field, accepting the usual string spellings."""
    if field not in data or data.get(field) is None:
        return default
    value = data.get(field)
    if isinstance(value, bool):
        return value
    text = str(value).strip().lower()
    if text in ('1', 'true', 'yes', 'on'):
        return True
    if text in ('0', 'false', 'no', 'off'):
        return False
    raise _BadRequest(f'{field} must be true or false')


def _read_choice(data, field, allowed, required=True, default=None):
    """Read an upper cased value that has to be one of a fixed set."""
    raw = data.get(field)
    value = '' if raw is None else str(raw).strip().upper()
    if not value:
        if required:
            raise _BadRequest(f'{field} is required')
        return default
    if value not in allowed:
        raise _BadRequest(f'{field} must be one of {", ".join(allowed)}')
    return value


def _read_account_ids(data, field='account_ids'):
    """
    Read the ticked accounts.

    Ownership scoped here as well as inside the engine: an id from another
    user is refused before any broker call is prepared, never silently ignored
    and never widened to every account.
    """
    raw = data.get(field)
    if raw is None:
        raise _BadRequest('Select at least one account')
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise _BadRequest(f'{field} must be a list of account ids')

    ids = []
    for value in raw:
        try:
            account_id = int(value)
        except (TypeError, ValueError):
            raise _BadRequest(f'Invalid account id in {field}')
        if account_id not in ids:
            ids.append(account_id)
    if not ids:
        raise _BadRequest('Select at least one account')

    owned = {row.id for row in TradingAccount.query.filter_by(user_id=current_user.id).all()}
    unknown = [str(account_id) for account_id in ids if account_id not in owned]
    if unknown:
        raise _BadRequest(f'Account {", ".join(unknown)} not found')
    return ids


def _read_quantity_overrides(data, field='quantity_overrides'):
    """Read the per-account Qty overrides from the split table."""
    raw = data.get(field)
    if raw in (None, '', {}):
        return None
    if not isinstance(raw, dict):
        raise _BadRequest(f'{field} must be an object of account id to quantity')

    overrides = {}
    for key, value in raw.items():
        if value is None or value == '':
            continue
        try:
            account_id = int(key)
            quantity = int(str(value).strip())
        except (TypeError, ValueError):
            raise _BadRequest('A quantity override must be a whole number of shares')
        if quantity < 0:
            raise _BadRequest('A quantity override cannot be negative')
        overrides[account_id] = quantity
    return overrides or None


def _read_trade_nature_id(data, field='trade_nature_id'):
    """Read an optional trade nature, ownership scoped."""
    raw = data.get(field)
    if raw is None or raw == '' or str(raw).strip().lower() in ('all', 'none'):
        return None
    try:
        nature_id = int(raw)
    except (TypeError, ValueError):
        raise _BadRequest('Invalid trade nature')
    if _owned_trade_nature(nature_id) is None:
        raise _BadRequest('Trade nature not found')
    return nature_id


def _read_holding_id(data, field='holding_id'):
    """Read a holding id and resolve it, ownership scoped."""
    holding_id = _read_int(data, field, minimum=1, required=True)
    holding = _owned_holding(holding_id)
    if holding is None:
        raise _BadRequest('Holding not found')
    return holding


def _arg(name, default=''):
    """One trimmed query string argument."""
    return (request.args.get(name) or default).strip()


def _arg_choice(name, allowed, label=None):
    """
    One upper cased query string filter that has to be in a fixed set.

    Returns (value_or_None, error_or_None). Blank and 'all' both mean no
    filter.
    """
    raw = _arg(name).upper()
    if not raw or raw == 'ALL':
        return None, None
    if raw not in allowed:
        return None, f'Invalid {label or name} filter'
    return raw, None


def _arg_symbol(name='symbol'):
    """One symbol filter, upper cased. Blank means no filter."""
    value = _arg(name).upper()
    if not value or value == 'ALL':
        return None
    return value[:50]


def _arg_date(name):
    """
    One YYYY-MM-DD query string filter.

    Returns (date_or_None, error_or_None).
    """
    raw = _arg(name)
    if not raw:
        return None, None
    try:
        return datetime.strptime(raw, '%Y-%m-%d').date(), None
    except (TypeError, ValueError):
        return None, f'Invalid {name} date, expected YYYY-MM-DD'


def _filter_account_id():
    """
    Account filter for the order and trade books.

    Deliberately NOT _selected_account_id: a book has to stay filterable by an
    account that has since been deactivated, otherwise its history becomes
    unreachable. Ownership is still scoped on both the id and the owner.

    Returns (account_id_or_None, error_or_None).
    """
    raw = _arg('account')
    if not raw or raw.lower() == 'all':
        return None, None
    try:
        account_id = int(raw)
    except (TypeError, ValueError):
        return None, 'Invalid account filter'
    if _owned_account(account_id) is None:
        return None, 'Account not found'
    return account_id, None


# ---------------------------------------------------------------------------
# Ownership scoped lookups for the increment 2 tables
# ---------------------------------------------------------------------------

def _owned_watchlist_item(item_id):
    """One watch list row, scoped by BOTH id and owner."""
    return EquityWatchlistItem.query.filter_by(
        id=item_id, user_id=current_user.id
    ).first()


def _owned_trade_nature(nature_id):
    """One trade nature, scoped by BOTH id and owner."""
    return EquityTradeNature.query.filter_by(
        id=nature_id, user_id=current_user.id
    ).first()


def _owned_holding(holding_id):
    """One tracked holding, scoped by BOTH id and owner."""
    return EquityHolding.query.filter_by(
        id=holding_id, user_id=current_user.id
    ).first()


def _owned_order(order_id):
    """One parent equity order, scoped by BOTH id and owner."""
    return EquityOrder.query.filter_by(
        id=order_id, user_id=current_user.id
    ).first()


def _all_trade_natures():
    """
    Every trade nature this user has, active and inactive, in display order.

    Settings needs the inactive ones so a nature can be brought back. The
    dropdowns keep using _trade_natures(), which is the active set.
    """
    natures = EquityTradeNature.query.filter_by(
        user_id=current_user.id
    ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()

    if not natures:
        EquityTradeNature.get_or_create_defaults(current_user.id)
        natures = EquityTradeNature.query.filter_by(
            user_id=current_user.id
        ).order_by(EquityTradeNature.display_order, EquityTradeNature.id).all()
    return natures


def _account_directory():
    """
    Every account this user owns, active or not, keyed by id.

    Order and trade book rows can reference an account that was deactivated
    after the order was placed, so the directory is deliberately wider than
    _active_accounts().
    """
    rows = TradingAccount.query.filter_by(user_id=current_user.id).all()
    return {
        row.id: {
            'account_name': row.account_name,
            'broker_name': row.broker_name,
            'is_active': bool(row.is_active),
        }
        for row in rows
    }


def _equity_settings():
    """This user's equity preferences, created with defaults on first use."""
    return EquitySetting.get_or_create(current_user.id)


def _today_start():
    """
    Start of today in the same clock EquityOrder.placed_at is written in.

    UTC, matching _build_todays_orders. Indian market hours map to 03:45 to
    10:00 UTC on the same calendar date, so a trading day never straddles the
    boundary.
    """
    return datetime.combine(datetime.utcnow().date(), datetime.min.time())


def _watchlist_symbol_keys():
    """(symbol, exchange) for every watch list row of the current user."""
    rows = EquityWatchlistItem.query.filter_by(user_id=current_user.id).all()
    return {
        ((row.symbol or '').strip().upper(), (row.exchange or 'NSE').strip().upper())
        for row in rows
        if row.symbol
    }


# ---------------------------------------------------------------------------
# Serialisers shared by Order Status, Order Book, Trade Book and the split view
# ---------------------------------------------------------------------------

def _split_payload(split, directory=None):
    """
    One account's share of an order, as JSON.

    Every figure here is the point-in-time snapshot taken when the order was
    created (PRD 9.1). Nothing on this row is recalculated from today's
    allocations or today's cash.
    """
    directory = directory or {}
    account = directory.get(split.account_id) or {}
    return {
        'split_id': split.id,
        'order_id': split.equity_order_id,
        'account_id': split.account_id,
        'account_name': account.get('account_name'),
        'broker_name': account.get('broker_name'),
        'qty_ratio': _pct(split.qty_ratio_at_order),
        'ratio_quantity': _to_int(split.ratio_quantity),
        'quantity': _to_int(split.quantity),
        'qty_overridden': bool(split.qty_overridden),
        'est_value': _money(split.est_value) if split.est_value is not None else None,
        'cash_balance': (
            _money(split.cash_balance_at_order)
            if split.cash_balance_at_order is not None else None
        ),
        'fill_status': split.fill_status,
        'filled_quantity': _to_int(split.filled_quantity),
        'avg_fill_price': (
            _money(split.avg_fill_price) if split.avg_fill_price is not None else None
        ),
        'broker_order_id': split.broker_order_id,
        'broker_gtt_id': split.broker_gtt_id,
        'broker_order_status': split.broker_order_status,
        'error_message': split.error_message,
        'error_type': split.error_type,
        'attempt_count': _to_int(split.attempt_count),
        'placed_at': _iso(split.placed_at),
        'last_synced_at': _iso(split.last_synced_at),
        'is_open': bool(split.is_open),
        'is_terminal': bool(split.is_terminal),
        'is_safe_to_retry': bool(split.is_safe_to_retry),
    }


def _skipped_split_payload(account_id, directory, quantity, ratio_quantity,
                           qty_ratio, est_value, reason,
                           fill_status=None):
    """
    A split-shaped row for an account that never reached the broker.

    An account skipped before placement has no EquityOrderSplit of its own on
    the claim-backed sell path, but the screen still has to show it in the same
    table as the accounts that did place. Keys match _split_payload exactly so
    the template binds once.
    """
    account = (directory or {}).get(account_id) or {}
    return {
        'split_id': None,
        'order_id': None,
        'account_id': account_id,
        'account_name': account.get('account_name'),
        'broker_name': account.get('broker_name'),
        'qty_ratio': _pct(qty_ratio),
        'ratio_quantity': _to_int(ratio_quantity),
        'quantity': _to_int(quantity),
        'qty_overridden': False,
        'est_value': _money(est_value) if est_value is not None else None,
        'cash_balance': None,
        'fill_status': fill_status or EQUITY_SPLIT_STATUS_SKIPPED,
        'filled_quantity': 0,
        'avg_fill_price': None,
        'broker_order_id': None,
        'broker_gtt_id': None,
        'broker_order_status': None,
        'error_message': reason,
        'error_type': None,
        'attempt_count': 0,
        'placed_at': None,
        'last_synced_at': None,
        'is_open': False,
        'is_terminal': True,
        'is_safe_to_retry': False,
    }


def _order_payload(order, splits, directory=None, include_splits=False):
    """
    One parent order, as JSON, with the M4b Accounts count.

    Two counts are published because they answer different questions:
        accounts_placed  reached the broker and is either working or filled.
                         This is the numerator in the "4/5" the PRD asks for.
        accounts_filled  actually completed.
    status_reason is the short explanation shown next to PARTIAL, for example
    "1 failed", and comes from the engine so the wording cannot drift.
    """
    splits = list(splits or [])
    counts = summarise_splits(splits)
    placed = counts['open'] + counts['filled']
    total = counts['total']

    payload = {
        'order_id': order.id,
        'symbol': order.symbol,
        'exchange': order.exchange,
        'side': order.side,
        'order_type': order.order_type,
        'product': order.product,
        'total_quantity': _to_int(order.total_quantity),
        'filled_quantity': sum(_to_int(split.filled_quantity) for split in splits),
        'leftover_quantity': _to_int(order.leftover_quantity),
        'price': _money(order.price) if order.price is not None else None,
        'trigger_price': (
            _money(order.trigger_price) if order.trigger_price is not None else None
        ),
        'stop_loss': _money(order.stop_loss) if order.stop_loss is not None else None,
        'target': _money(order.target) if order.target is not None else None,
        'status': order.status,
        'status_reason': counts['reason'],
        'source': order.source,
        'trade_nature_id': order.trade_nature_id,
        'trade_nature': order.trade_nature.name if order.trade_nature else None,
        'insufficient_funds_action': order.insufficient_funds_action,
        'error_message': order.error_message,
        'placed_at': _iso(order.placed_at),
        'cancelled_at': _iso(order.cancelled_at),
        'updated_at': _iso(order.updated_at),
        # accounts_count is the increment 1 name and is kept so the dashboard
        # keeps working. accounts_selected is the same number under the name
        # the new screens use.
        'accounts_count': total,
        'accounts_selected': total,
        'accounts_placed': placed,
        'accounts_filled': counts['filled'],
        'accounts_open': counts['open'],
        # PRD 7.1 and 7.6 both ask for filled over selected ("5/5 fully filled,
        # 4/5 partial"), which was impossible while nothing booked a fill. The
        # fill poller now does, so this is the filled count. accounts_placed
        # stays available separately for anyone who wants "reached the broker".
        'accounts_label': f"{counts['filled']}/{total}",
        'counts': counts,
        'is_open': bool(order.is_open),
        'can_modify': bool(order.is_open),
        'can_cancel': bool(order.is_open),
    }
    if include_splits:
        payload['splits'] = [_split_payload(split, directory) for split in splits]
    return payload


def _trade_payload(trade, split, order, directory=None):
    """One fill, with the parent order it belongs to."""
    account = (directory or {}).get(split.account_id) or {}
    quantity = _to_int(trade.executed_quantity)
    price = _to_float(trade.execution_price)
    return {
        'trade_id': trade.id,
        'split_id': split.id,
        'order_id': order.id,
        'account_id': split.account_id,
        'account_name': account.get('account_name'),
        'broker_name': account.get('broker_name'),
        'symbol': order.symbol,
        'exchange': (trade.exchange or order.exchange),
        'side': order.side,
        'order_type': order.order_type,
        'product': order.product,
        'source': order.source,
        'trade_nature_id': order.trade_nature_id,
        'trade_nature': order.trade_nature.name if order.trade_nature else None,
        'execution_price': _money(price),
        'executed_quantity': quantity,
        'trade_value': _money(turnover(price, quantity)),
        'executed_at': _iso(trade.executed_at),
        'broker_trade_id': trade.broker_trade_id,
        'broker_order_id': split.broker_order_id,
        'order_status': order.status,
        'order_placed_at': _iso(order.placed_at),
    }


# ---------------------------------------------------------------------------
# Tracked holdings
#
# The Holdings screen reads the broker payload directly, but the stop loss and
# target monitor and the exit claim both work on EquityHolding rows. A row with
# a stale quantity is a row the monitor could sell the wrong number of shares
# against, so the quantity is refreshed from the broker at the two moments that
# matter: when a level is armed and when a sell is prepared.
# ---------------------------------------------------------------------------

def _holding_key(account_id, symbol, exchange):
    """The key EquityHolding is unique on."""
    return (
        account_id,
        (symbol or '').strip().upper(),
        (exchange or 'NSE').strip().upper(),
    )


def _sync_holding_rows(accounts, snapshots, symbol=None, exchange=None):
    """
    Upsert this user's EquityHolding rows from the broker holdings payload.

    What it writes: quantity, avg_cost and pledged_quantity, which are the
    broker's facts.

    What it NEVER touches: exit_status and the whole exit claim, stop_loss,
    target, exit_mode, trade_nature_id and the breach records. Those are
    AlgoMirror's own state and a broker read must not be able to disarm a stop
    loss or reopen a claim.

    A tracked row the broker no longer reports has been sold, so its quantity
    is zeroed. That is only ever done from a payload actually in hand (live or
    inside the freshness window) and never against a row with a sell already in
    flight, whose quantity is settled at claim time.

    Returns {(account_id, SYMBOL, EXCHANGE): EquityHolding}.
    """
    accounts = list(accounts or [])
    if not accounts:
        return {}

    wanted_symbol = (symbol or '').strip().upper() or None
    wanted_exchange = (exchange or '').strip().upper() or None
    account_ids = [account.id for account in accounts]

    tracked = {}
    for row in EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.account_id.in_(account_ids)
    ).all():
        tracked[_holding_key(row.account_id, row.symbol, row.exchange)] = row

    settings = _equity_settings()
    default_exit_mode = (
        settings.default_exit_mode if settings else EQUITY_EXIT_MODE_CONFIRM
    )

    changed = False
    for account in accounts:
        snapshot = snapshots.get(account.id) or {}
        # from_cache means the payload came out of the freshness window, which
        # is current data that simply did not need a broker call.
        payload_usable = bool(
            snapshot.get('holdings_live') or snapshot.get('from_cache')
        )
        seen = set()

        for broker_row in _normalise_broker_holdings(snapshot.get('holdings_data')):
            if wanted_symbol and broker_row['symbol'] != wanted_symbol:
                continue
            if wanted_exchange and broker_row['exchange'] != wanted_exchange:
                continue

            key = _holding_key(account.id, broker_row['symbol'], broker_row['exchange'])
            seen.add(key)

            holding = tracked.get(key)
            if holding is None:
                holding = EquityHolding(
                    user_id=current_user.id,
                    account_id=account.id,
                    symbol=broker_row['symbol'],
                    exchange=broker_row['exchange'],
                    quantity=0,
                    exit_mode=default_exit_mode,
                    exit_status=EQUITY_HOLDING_STATUS_ACTIVE,
                )
                db.session.add(holding)
                tracked[key] = holding
                changed = True

            quantity = max(_to_int(broker_row['quantity']), 0)
            if _to_int(holding.quantity) != quantity:
                holding.quantity = quantity
                changed = True

            avg_cost = _to_float(broker_row['avg_cost'])
            if avg_cost > 0 and _to_float(holding.avg_cost) != avg_cost:
                holding.avg_cost = avg_cost
                changed = True

            pledged = max(_to_int(broker_row['pledged_quantity']), 0)
            if _to_int(holding.pledged_quantity) != pledged:
                holding.pledged_quantity = pledged
                changed = True

        if not payload_usable:
            continue

        for key, holding in tracked.items():
            if key[0] != account.id or key in seen:
                continue
            if wanted_symbol and key[1] != wanted_symbol:
                continue
            if wanted_exchange and key[2] != wanted_exchange:
                continue
            if holding.is_exit_in_flight:
                continue
            if _to_int(holding.quantity) != 0:
                holding.quantity = 0
                holding.pledged_quantity = 0
                changed = True

    if changed:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.error(f'Could not sync equity holding rows: {exc}')
            tracked = {}
            for row in EquityHolding.query.filter(
                EquityHolding.user_id == current_user.id,
                EquityHolding.account_id.in_(account_ids)
            ).all():
                tracked[_holding_key(row.account_id, row.symbol, row.exchange)] = row

    return tracked


def _holding_payload(holding, directory=None, ltp=0.0):
    """One tracked holding row, including its exit claim state."""
    account = (directory or {}).get(holding.account_id) or {}
    quantity = _to_int(holding.quantity)
    avg_cost = _to_float(holding.avg_cost)
    price = _to_float(ltp) or _to_float(holding.last_price)
    return {
        'holding_id': holding.id,
        'account_id': holding.account_id,
        'account_name': account.get('account_name'),
        'broker_name': account.get('broker_name'),
        'symbol': holding.symbol,
        'exchange': holding.exchange,
        'quantity': quantity,
        'pledged_quantity': _to_int(holding.pledged_quantity),
        'sellable_quantity': _to_int(holding.sellable_quantity),
        'avg_cost': _money(avg_cost),
        'ltp': _money(price),
        'gross_pnl': _money(gross_pnl(price, avg_cost, quantity)) if avg_cost > 0 else None,
        'trade_nature_id': holding.trade_nature_id,
        'trade_nature': holding.trade_nature.name if holding.trade_nature else None,
        'stop_loss': _money(holding.stop_loss) if holding.stop_loss is not None else None,
        'target': _money(holding.target) if holding.target is not None else None,
        'exit_mode': holding.exit_mode,
        'exit_mode_tag': EXIT_MODE_TAGS.get(
            holding.exit_mode, EXIT_MODE_TAGS[EQUITY_EXIT_MODE_CONFIRM]
        ),
        'exit_status': holding.exit_status,
        'exit_reason': holding.exit_reason,
        'exit_quantity': _to_int(holding.exit_quantity),
        'exit_broker_order_id': holding.exit_broker_order_id,
        'exit_split_id': holding.exit_split_id,
        'exit_error': holding.exit_error,
        'exit_claimed_at': _iso(holding.exit_claimed_at),
        'exit_submitted_at': _iso(holding.exit_submitted_at),
        'exit_completed_at': _iso(holding.exit_completed_at),
        'is_exit_in_flight': bool(holding.is_exit_in_flight),
        'is_monitorable': bool(holding.is_monitorable),
        'has_exit_levels': bool(holding.has_exit_levels),
        'sl_hit_at': _iso(holding.sl_hit_at),
        'sl_hit_price': _money(holding.sl_hit_price) if holding.sl_hit_price else None,
        'tp_hit_at': _iso(holding.tp_hit_at),
        'tp_hit_price': _money(holding.tp_hit_price) if holding.tp_hit_price else None,
        'last_monitored_at': _iso(holding.last_monitored_at),
        'awaiting_confirm': holding.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
        'needs_reconciliation': (
            holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE
        ),
    }


# ---------------------------------------------------------------------------
# The exit fan-out
#
# Every job here goes through equity_order_engine.exit_holding, which is the
# ONE helper that claims a holding before selling it (safety rule 1). This
# function only supplies concurrency and failure isolation, it never places
# anything itself.
# ---------------------------------------------------------------------------

def _exit_error_result(holding_id, message, account_id=None):
    """The shape exit_holding returns, for a failure that never reached it."""
    return {
        'status': 'error',
        'holding_id': holding_id,
        'account_id': account_id,
        'message': message,
        'claimed': False,
        'indeterminate': False,
        'broker_order_id': None,
        'order_id': None,
        'split_id': None,
        'quantity': 0,
        'attempts': 0,
    }


def _exit_worker(app, user_id, holding_id, kwargs):
    """
    One account's claim-and-place exit on its own thread.

    Rule 8: the app object and every plain value are captured before the thread
    starts, the body runs inside its own app context with its own session, and
    a crash is contained to this account.
    """
    with app.app_context():
        try:
            return exit_holding(user_id=user_id, holding_id=holding_id, **kwargs)
        except Exception as exc:
            current_app.logger.error(
                f'Equity exit worker failed for holding {holding_id}: {exc}'
            )
            return _exit_error_result(holding_id, f'Exit failed unexpectedly: {exc}')
        finally:
            db.session.remove()


def _fan_out_exits(jobs, **common):
    """
    Run one engine exit per holding, concurrently and independently.

    Args:
        jobs: list of dicts with holding_id and an optional quantity. A job
            without a quantity sells the whole sellable quantity, resolved
            under the claim's own row lock rather than from a number this
            request read earlier.
        **common: forwarded to exit_holding (reason, order_type, price,
            trigger_price, allow_from, gtt_trigger_leg).

    Returns the list of exit_holding results, in job order.
    """
    jobs = list(jobs or [])
    if not jobs:
        return []

    app = current_app._get_current_object()
    user_id = current_user.id

    prepared = []
    for job in jobs:
        kwargs = dict(common)
        if 'quantity' in job:
            kwargs['quantity'] = job['quantity']
        prepared.append((job['holding_id'], kwargs))

    if len(prepared) == 1:
        holding_id, kwargs = prepared[0]
        try:
            return [exit_holding(user_id=user_id, holding_id=holding_id, **kwargs)]
        except Exception as exc:
            current_app.logger.error(f'Equity exit failed for holding {holding_id}: {exc}')
            return [_exit_error_result(holding_id, f'Exit failed unexpectedly: {exc}')]

    results = []
    bound = min(MAX_EXIT_WORKERS, len(prepared))
    with ThreadPoolExecutor(max_workers=bound) as executor:
        futures = [
            (holding_id, executor.submit(_exit_worker, app, user_id, holding_id, kwargs))
            for holding_id, kwargs in prepared
        ]
        for holding_id, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                current_app.logger.error(
                    f'Equity exit crashed for holding {holding_id}: {exc}'
                )
                results.append(
                    _exit_error_result(holding_id, f'Exit crashed: {exc}')
                )
    return results


def _exit_counts(results):
    """Count exit outcomes and describe them in one line."""
    placed = sum(1 for result in results if result['status'] == 'success')
    skipped = sum(1 for result in results if result['status'] == 'skipped')
    indeterminate = sum(1 for result in results if result['status'] == 'indeterminate')
    failed = sum(1 for result in results if result['status'] == 'error')

    if not results:
        status = 'error'
    elif placed == 0:
        status = 'error'
    elif placed < len(results):
        status = 'partial'
    else:
        status = 'success'

    parts = [f'{placed} of {len(results)} accounts placed']
    if failed:
        parts.append(f'{failed} failed')
    if skipped:
        parts.append(f'{skipped} skipped')
    if indeterminate:
        parts.append(f'{indeterminate} unconfirmed, verify at the broker')

    return {
        'status': status,
        'message': ', '.join(parts),
        'accounts_placed': placed,
        'accounts_skipped': skipped,
        'accounts_failed': failed,
        'accounts_indeterminate': indeterminate,
        'accounts_selected': len(results),
    }


# ---------------------------------------------------------------------------
# Broker reads added by increment 2: market depth and symbol search
#
# Both are pure reads. They build their own client the same way the quote
# fallback above does, and neither can write anything at a broker.
# ---------------------------------------------------------------------------

def _read_credential():
    """
    One account to read shared market data through.

    Depth and symbol search are not per account, so any connected account
    answers them. Prefer the account the caller asked for, otherwise the first
    one whose API key can be read.

    Returns (credential_or_None, error_or_None).
    """
    raw = _arg('account')
    accounts = _active_accounts()
    if not accounts:
        return None, 'No active trading account is configured'

    if raw and raw.lower() != 'all':
        try:
            account_id = int(raw)
        except (TypeError, ValueError):
            return None, 'Invalid account filter'
        accounts = [account for account in accounts if account.id == account_id]
        if not accounts:
            return None, 'Account not found'

    creds = _account_credentials(accounts)
    credential = _quote_credential(creds, {})
    if credential is None:
        return None, 'No account with a readable API key is available'
    return credential, None


def _depth_side(rows, side_total):
    """
    Five levels of one side of the book, padded so the panel always has five.

    fill_pct is the level's share of the five level total on its own side,
    which is what the proportion bar in the mockup draws.
    """
    levels = []
    for index in range(DEPTH_LEVELS):
        row = rows[index] if index < len(rows) else {}
        if not isinstance(row, dict):
            row = {}
        quantity = _to_int(_first_number(row, _DEPTH_QTY_KEYS))
        levels.append({
            'level': index + 1,
            'price': _money(row.get('price')),
            'quantity': quantity,
            'orders': _to_int(_first_number(row, _DEPTH_ORDERS_KEYS)),
            'fill_pct': _pct(percent_of(quantity, side_total)),
        })
    return levels


def _normalise_depth(data, symbol, exchange):
    """
    Turn an OpenAlgo depth payload into the Market Depth panel's shape.

    Adapters spell the surrounding figures differently, so each one is resolved
    from an alias list rather than a single key. A figure the broker does not
    publish comes back as 0 and the panel shows a dash for it.
    """
    data = data if isinstance(data, dict) else {}

    bids_raw = data.get('bids')
    if not isinstance(bids_raw, list):
        bids_raw = data.get('buy') if isinstance(data.get('buy'), list) else []
    asks_raw = data.get('asks')
    if not isinstance(asks_raw, list):
        asks_raw = data.get('sell') if isinstance(data.get('sell'), list) else []

    bids_raw = [row for row in bids_raw if isinstance(row, dict)][:DEPTH_LEVELS]
    asks_raw = [row for row in asks_raw if isinstance(row, dict)][:DEPTH_LEVELS]

    bid_total_5 = sum(_to_int(_first_number(row, _DEPTH_QTY_KEYS)) for row in bids_raw)
    ask_total_5 = sum(_to_int(_first_number(row, _DEPTH_QTY_KEYS)) for row in asks_raw)

    ltp = _to_float(data.get('ltp'))
    prev_close = _first_number(data, _PREV_CLOSE_KEYS)
    change = ltp - prev_close if ltp > 0 and prev_close > 0 else 0.0

    total_buy = _to_int(_first_number(data, _TOTAL_BUY_KEYS)) or bid_total_5
    total_sell = _to_int(_first_number(data, _TOTAL_SELL_KEYS)) or ask_total_5

    return {
        'symbol': symbol,
        'exchange': exchange,
        'bids': _depth_side(bids_raw, bid_total_5),
        'asks': _depth_side(asks_raw, ask_total_5),
        'totals': {
            'bid_quantity': total_buy,
            'ask_quantity': total_sell,
            'bid_quantity_5': bid_total_5,
            'ask_quantity_5': ask_total_5,
        },
        'ohlc': {
            'open': _money(data.get('open')),
            'high': _money(data.get('high')),
            'low': _money(data.get('low')),
            'close': _money(prev_close),
        },
        'ltp': _money(ltp),
        'prev_close': _money(prev_close),
        'change': _money(change),
        'change_pct': _pct(signed_percent_of(change, prev_close)),
        'volume': _to_int(_first_number(data, _VOLUME_KEYS)),
        'ltq': _to_int(_first_number(data, _LTQ_KEYS)),
        'ltt': data.get('ltt') or data.get('last_trade_time') or data.get('timestamp'),
        'oi': _to_int(data.get('oi')),
        'upper_circuit': _money(_first_number(data, _UPPER_CIRCUIT_KEYS)),
        'lower_circuit': _money(_first_number(data, _LOWER_CIRCUIT_KEYS)),
    }


def _is_cash_instrument(entry):
    """
    True when a search result is a tradable NSE or BSE equity or ETF.

    This module trades CNC delivery only, so a futures or options contract or
    an index is never a valid result. Excluding the derivative shapes is safer
    than allow-listing instrument type codes, which differ per adapter: a code
    this function has never seen is still admitted as long as it is not marked
    as a derivative and carries no expiry or strike.
    """
    exchange = str(entry.get('exchange') or '').strip().upper()
    if exchange not in SEARCH_EXCHANGES:
        return False

    instrument = str(_first_text(entry, _INSTRUMENT_TYPE_KEYS) or '').upper()
    if any(fragment in instrument for fragment in NON_EQUITY_INSTRUMENT_FRAGMENTS):
        return False

    if str(entry.get('expiry') or '').strip():
        return False
    if _to_float(entry.get('strike')) > 0:
        return False
    return True


def _first_text(row, keys):
    """Read the first key in keys that carries a non-empty string."""
    for key in keys:
        value = row.get(key)
        if value is None:
            continue
        text = str(value).strip()
        if text:
            return text
    return ''


def _normalise_search_results(response):
    """Extract the tradable cash instruments from an OpenAlgo search response."""
    if not isinstance(response, dict) or response.get('status') != 'success':
        return []

    entries = response.get('data')
    if not isinstance(entries, list):
        entries = response.get('results')
    if not isinstance(entries, list):
        return []

    seen = set()
    results = []
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        symbol = str(entry.get('symbol') or '').strip().upper()
        if not symbol:
            continue
        exchange = str(entry.get('exchange') or '').strip().upper()
        if not _is_cash_instrument(entry):
            continue
        key = (symbol, exchange)
        if key in seen:
            continue
        seen.add(key)
        results.append({
            'symbol': symbol,
            'exchange': exchange,
            'name': _first_text(entry, _SEARCH_NAME_KEYS),
            'token': str(entry.get('token') or '').strip(),
            'instrument_type': str(_first_text(entry, _INSTRUMENT_TYPE_KEYS) or '').upper(),
            'lot_size': _to_int(entry.get('lotsize') or entry.get('lot_size'), 1) or 1,
            'tick_size': _to_float(entry.get('ticksize') or entry.get('tick_size')),
        })
        if len(results) >= MAX_SEARCH_RESULTS:
            break
    return results


# ---------------------------------------------------------------------------
# Prices for one symbol
# ---------------------------------------------------------------------------

def _live_price(symbol, exchange):
    """
    Best available last traded price for one symbol.

    Pushed feed first, the bounded REST fallback second, exactly like every
    other price on these screens. Returns 0.0 when nothing answered, which the
    engine reads as "the funds check could not be performed" rather than as a
    price of zero.
    """
    try:
        creds = _account_credentials(_active_accounts())
        quotes, _feed = _resolve_prices(creds, {}, [(symbol, exchange)])
    except Exception as exc:
        current_app.logger.warning(
            f'Equity live price unavailable for {symbol} {exchange}: {exc}'
        )
        return 0.0
    return _to_float((quotes.get((symbol, exchange)) or {}).get('ltp'))


# ---------------------------------------------------------------------------
# M3 Watch List
# ---------------------------------------------------------------------------

def _watchlist_rows():
    """This user's watch list, alphabetical."""
    return EquityWatchlistItem.query.filter_by(
        user_id=current_user.id
    ).order_by(EquityWatchlistItem.symbol, EquityWatchlistItem.id).all()


def _watchlist_item_payload(item, quote=None, nature_names=None):
    """
    One watch list row.

    variance_pct is the PRD's Variance column: how far the live price is from
    the target, as a signed percent of the target. signed_percent_of keeps the
    sign, so a stock trading below its target reads as negative instead of
    being clamped to zero.
    """
    quote = quote or {}
    nature_names = nature_names or {}
    ltp = _to_float(quote.get('ltp'))
    prev_close = _to_float(quote.get('prev_close'))
    target = _to_float(item.target_price)
    change = ltp - prev_close if ltp > 0 and prev_close > 0 else 0.0
    variance = ltp - target if ltp > 0 and target > 0 else 0.0

    return {
        'id': item.id,
        'symbol': item.symbol,
        'exchange': item.exchange,
        'trade_nature_id': item.trade_nature_id,
        'trade_nature': nature_names.get(item.trade_nature_id),
        'target_price': _money(target) if item.target_price is not None else None,
        'alert_price': (
            _money(item.alert_price) if item.alert_price is not None else None
        ),
        'alert_direction': item.alert_direction,
        'price_alert_enabled': bool(item.price_alert_enabled),
        'alert_triggered_at': _iso(item.alert_triggered_at),
        'alert_triggered_price': (
            _money(item.alert_triggered_price)
            if item.alert_triggered_price is not None else None
        ),
        'alert_armed': bool(
            item.price_alert_enabled
            and item.alert_price
            and item.alert_triggered_at is None
        ),
        'ltp': _money(ltp),
        'has_ltp': ltp > 0,
        'prev_close': _money(prev_close),
        'change': _money(change),
        'change_pct': _pct(signed_percent_of(change, prev_close)),
        'variance': _money(variance),
        'variance_pct': _pct(signed_percent_of(variance, target)),
        'has_variance': ltp > 0 and target > 0,
        'created_at': _iso(item.created_at),
        'updated_at': _iso(item.updated_at),
    }


def _clear_watchlist_alert(item):
    """
    Re-arm a watch list alert.

    Every write that changes alert_price, alert_direction or
    price_alert_enabled must call this, otherwise an alert that already fired
    stays silent for good. See the EquityWatchlistItem docstring.
    """
    item.alert_triggered_at = None
    item.alert_triggered_price = None


def _evaluate_watchlist_alerts(items, quotes):
    """
    Fire the price alerts whose level has been crossed.

    Two guards, both in the schema. alert_triggered_at is the de-duplication
    guard: an alert fires only while it is NULL, and setting it is what marks
    it delivered. alert_direction says which way the price has to cross, and is
    resolved lazily from the first price actually seen when the admin did not
    choose one, because an alert price on its own is ambiguous.

    Note the difference from the stop loss monitor: this runs in the request
    that polls the quotes, so a watch list alert needs the screen open. The
    stop loss and target monitor, which is the one that can sell, deliberately
    does not (PRD 8.3) and runs in the background scheduler instead.

    Returns the list of alerts fired in this pass.
    """
    settings = _equity_settings()
    if settings is not None and not settings.price_alerts_enabled:
        return []

    fired = []
    changed = False

    for item in items:
        if not item.price_alert_enabled:
            continue
        alert_price = _to_float(item.alert_price)
        if alert_price <= 0:
            continue

        quote = quotes.get((item.symbol, item.exchange)) or {}
        ltp = _to_float(quote.get('ltp'))
        if ltp <= 0:
            continue

        if not item.alert_direction:
            # First price seen decides which side we started on. No alert is
            # raised on this pass: an alert on the tick that defines the
            # direction would fire on a level that was never actually crossed.
            item.alert_direction = (
                EQUITY_ALERT_DIRECTION_ABOVE if alert_price > ltp
                else EQUITY_ALERT_DIRECTION_BELOW
            )
            changed = True
            continue

        if item.alert_triggered_at is not None:
            continue

        crossed = (
            ltp >= alert_price
            if item.alert_direction == EQUITY_ALERT_DIRECTION_ABOVE
            else ltp <= alert_price
        )
        if not crossed:
            continue

        item.alert_triggered_at = datetime.utcnow()
        item.alert_triggered_price = ltp
        changed = True
        fired.append({
            'id': item.id,
            'symbol': item.symbol,
            'exchange': item.exchange,
            'alert_price': _money(alert_price),
            'alert_direction': item.alert_direction,
            'ltp': _money(ltp),
            'triggered_at': _iso(item.alert_triggered_at),
            'message': (
                f'{item.symbol} traded {"at or above" if item.alert_direction == EQUITY_ALERT_DIRECTION_ABOVE else "at or below"} '
                f'{_money(alert_price)} (last {_money(ltp)})'
            ),
        })

    if changed:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.warning(f'Could not record equity price alerts: {exc}')
            return []

    return fired


def _build_watchlist_payload(with_prices=True):
    """
    M3 Watch List.

    Prices come from the shared push feed, so the 10 second refresh the PRD
    asks for costs no broker call once the feed is warm.
    """
    items = _watchlist_rows()
    natures = _trade_natures()
    nature_names = {nature.id: nature.name for nature in natures}

    quotes = {}
    price_feed = _feed_status_block({'requested': 0, 'from_feed': 0, 'from_rest': 0,
                                     'fallback_symbols': 0})
    alerts = []

    if items and with_prices:
        keys = [(item.symbol, item.exchange) for item in items]
        creds = _account_credentials(_active_accounts())
        quotes, price_feed = _resolve_prices(creds, {}, keys, want_prev_close=True)
        alerts = _evaluate_watchlist_alerts(items, quotes)

    settings = _equity_settings()
    return {
        'items': [
            _watchlist_item_payload(item, quotes.get((item.symbol, item.exchange)), nature_names)
            for item in items
        ],
        'alerts': alerts,
        'trade_natures': [
            {'id': nature.id, 'name': nature.name} for nature in natures
        ],
        'price_alerts_enabled': bool(settings.price_alerts_enabled) if settings else True,
        'max_items': MAX_WATCHLIST_ITEMS,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_trade_natures_payload():
    """Settings: every trade nature, active and inactive, in display order."""
    natures = _all_trade_natures()
    return {
        'trade_natures': [
            {
                'id': nature.id,
                'name': nature.name,
                'display_order': _to_int(nature.display_order),
                'is_active': nature.is_active is not False,
                'created_at': _iso(nature.created_at),
                'updated_at': _iso(nature.updated_at),
            }
            for nature in natures
        ],
        'generated_at': _iso(datetime.utcnow()),
    }


# ---------------------------------------------------------------------------
# M4 Place Order
#
# The preview and the submit share one instruction reader, so what the admin is
# shown in the split table and what is actually sent cannot drift apart.
# ---------------------------------------------------------------------------

def _read_instruction(data, require_accounts=True):
    """
    Read one equity instruction from a request body.

    Product is never read from the request. Equity is CNC delivery, always, so
    it is a constant here rather than something a caller can influence.
    """
    instruction = {
        'symbol': _read_symbol(data),
        'exchange': _read_exchange(data),
        'side': _read_choice(data, 'side', VALID_SIDES),
        'order_type': _read_choice(
            data, 'order_type', VALID_ORDER_TYPES,
            required=False, default=EQUITY_ORDER_TYPE_MARKET
        ),
        'total_quantity': _read_int(data, 'total_quantity', minimum=1, required=True),
        'price': _read_price(data, 'price'),
        'trigger_price': _read_price(data, 'trigger_price'),
        'quantity_overrides': _read_quantity_overrides(data),
        'account_ids': _read_account_ids(data) if require_accounts else None,
        'reference_price': _read_price(data, 'reference_price'),
        'insufficient_funds_action': _read_choice(
            data, 'insufficient_funds_action', VALID_FUNDS_ACTIONS, required=False
        ),
        'product': EQUITY_PRODUCT_CNC,
    }

    if instruction['order_type'] == EQUITY_ORDER_TYPE_LIMIT and not instruction['price']:
        raise _BadRequest('A LIMIT order needs a price')
    if instruction['order_type'] == EQUITY_ORDER_TYPE_GTT:
        if not instruction['price']:
            raise _BadRequest('A GTT order needs a limit price')
        if not instruction['trigger_price']:
            raise _BadRequest('A GTT order needs a trigger price')

    # A MARKET order has no price of its own, so Est. Value and the cash check
    # need the live last traded price. Resolved here rather than trusted from
    # the browser, which could send anything.
    if (instruction['order_type'] == EQUITY_ORDER_TYPE_MARKET
            and not instruction['reference_price']):
        price = _live_price(instruction['symbol'], instruction['exchange'])
        instruction['reference_price'] = price if price > 0 else None

    return instruction


def _annotate_sell_capacity(rows, tracked, symbol, exchange):
    """
    Add what each account can actually deliver to the split rows, and flag the
    ones that cannot cover their share.

    A CNC sell of shares the account does not hold is a short delivery, which
    is an auction and a penalty rather than a trade, so an account with nothing
    sellable is flagged rather than sent.
    """
    for row in rows:
        holding = tracked.get(_holding_key(row['account_id'], symbol, exchange))
        sellable = _to_int(holding.sellable_quantity) if holding is not None else 0
        wanted = _to_int(row.get('quantity'))

        row['holding_id'] = holding.id if holding is not None else None
        row['holding_quantity'] = _to_int(holding.quantity) if holding is not None else 0
        row['pledged_quantity'] = (
            _to_int(holding.pledged_quantity) if holding is not None else 0
        )
        row['sellable_quantity'] = sellable
        row['exit_status'] = holding.exit_status if holding is not None else None
        row['sell_quantity'] = min(wanted, sellable) if wanted > 0 else 0

        if not row['check_ok']:
            continue

        if holding is None or sellable <= 0:
            row['check_ok'] = False
            row['check_reason'] = (
                f'This account holds no deliverable {symbol}. A CNC sell '
                'without the shares would be a short delivery.'
            )
        elif holding.is_exit_in_flight:
            row['check_ok'] = False
            row['check_reason'] = (
                'A sell against this holding is already in flight. It has to '
                'settle before another one can be claimed.'
            )
        elif holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE:
            row['check_ok'] = False
            row['check_reason'] = (
                'The previous exit on this holding was never confirmed. Verify '
                'it at the broker and resolve it before selling again.'
            )
        elif wanted > sellable:
            row['check_ok'] = False
            row['check_reason'] = (
                f'This account holds {sellable} deliverable shares, short of '
                f'the {wanted} its share of the order needs.'
            )
    return rows


def _sell_context(instruction):
    """
    Prepare a SELL: refresh the tracked holdings the claim will lock.

    The claim reads the quantity off the EquityHolding row, so that row has to
    say what the broker says before anything is claimed. The read is served
    from the 30 second cache when it is warm, so this normally costs nothing.

    Returns (accounts_in_view, tracked_holdings).
    """
    wanted = set(instruction['account_ids'] or [])
    context = _account_context(
        fetch_holdings=True,
        fetch_account_ids=sorted(wanted)
    )
    accounts = [account for account in context['accounts'] if account.id in wanted]
    tracked = _sync_holding_rows(
        accounts,
        context['snapshots'],
        symbol=instruction['symbol'],
        exchange=instruction['exchange'],
    )
    return accounts, tracked


def _build_order_preview(instruction):
    """
    The M4 ACCOUNT-WISE ORDER SPLIT table. Writes nothing, places nothing.

    For a SELL the tracked holdings are refreshed first and every row is
    annotated with the deliverable quantity, because for a sell the binding
    constraint is stock rather than cash.
    """
    tracked = {}
    if instruction['side'] == EQUITY_SIDE_SELL:
        _accounts, tracked = _sell_context(instruction)

    preview = preview_order_split(
        user_id=current_user.id,
        symbol=instruction['symbol'],
        exchange=instruction['exchange'],
        side=instruction['side'],
        total_quantity=instruction['total_quantity'],
        order_type=instruction['order_type'],
        price=instruction['price'],
        trigger_price=instruction['trigger_price'],
        account_ids=instruction['account_ids'],
        quantity_overrides=instruction['quantity_overrides'],
        reference_price=instruction['reference_price'],
        insufficient_funds_action=instruction['insufficient_funds_action'],
    )
    if preview.get('status') != 'success':
        raise _BadRequest(preview.get('message') or 'The order split could not be worked out')

    directory = _account_directory()
    rows = preview['rows']
    for row in rows:
        account = directory.get(row['account_id']) or {}
        row['broker_name'] = account.get('broker_name')
        row['est_value'] = _money(row['est_value']) if row['est_value'] is not None else None
        row['cash_balance'] = (
            _money(row['cash_balance']) if row['cash_balance'] is not None else None
        )
        row['required_cash'] = (
            _money(row['required_cash']) if row['required_cash'] is not None else None
        )
        row['qty_ratio'] = _pct(row['qty_ratio'])

    if instruction['side'] == EQUITY_SIDE_SELL:
        rows = _annotate_sell_capacity(
            rows, tracked, instruction['symbol'], instruction['exchange']
        )

    flagged = [row for row in rows if not row['check_ok']]
    preview['rows'] = rows
    preview['accounts_ok'] = len(rows) - len(flagged)
    preview['accounts_flagged'] = len(flagged)
    preview['claim_backed'] = instruction['side'] == EQUITY_SIDE_SELL
    preview['generated_at'] = _iso(datetime.utcnow())
    return preview


def _placement_response(instruction, message, status):
    """The empty shell every placement answer is filled into."""
    return {
        'status': status,
        'message': message,
        'claim_backed': instruction['side'] == EQUITY_SIDE_SELL,
        'order_id': None,
        'order_ids': [],
        'parent_status': None,
        'symbol': instruction['symbol'],
        'exchange': instruction['exchange'],
        'side': instruction['side'],
        'order_type': instruction['order_type'],
        'product': EQUITY_PRODUCT_CNC,
        'price': instruction['price'],
        'trigger_price': instruction['trigger_price'],
        'total_quantity': instruction['total_quantity'],
        'placed_quantity': 0,
        'leftover_quantity': 0,
        'ratio_leftover': 0,
        'insufficient_funds_action': instruction['insufficient_funds_action'],
        'error_message': None,
        'accounts_selected': 0,
        'accounts_placed': 0,
        'accounts_failed': 0,
        'accounts_skipped': 0,
        'accounts_indeterminate': 0,
        'accounts_unsupported': 0,
        'counts': {},
        'splits': [],
        'generated_at': _iso(datetime.utcnow()),
    }


def _place_buy(instruction, stop_loss, target, trade_nature_id, gtt_trigger_leg):
    """
    A BUY goes straight through the engine's multi-account placement.

    There is nothing to claim: a buy cannot collide with the stop loss monitor,
    which only ever sells.
    """
    result = place_multi_account_order(
        user_id=current_user.id,
        symbol=instruction['symbol'],
        exchange=instruction['exchange'],
        side=instruction['side'],
        total_quantity=instruction['total_quantity'],
        order_type=instruction['order_type'],
        price=instruction['price'],
        trigger_price=instruction['trigger_price'],
        stop_loss=stop_loss,
        target=target,
        account_ids=instruction['account_ids'],
        quantity_overrides=instruction['quantity_overrides'],
        trade_nature_id=trade_nature_id,
        source=EQUITY_ORDER_SOURCE_MANUAL,
        reference_price=instruction['reference_price'],
        insufficient_funds_action=instruction['insufficient_funds_action'],
        gtt_trigger_leg=gtt_trigger_leg,
    )

    response = _placement_response(
        instruction, result.get('message') or '', result.get('status') or 'error'
    )
    for key in (
        'order_id', 'parent_status', 'placed_quantity', 'leftover_quantity',
        'ratio_leftover', 'insufficient_funds_action', 'error_message',
        'accounts_selected', 'accounts_placed', 'accounts_failed',
        'accounts_skipped', 'accounts_indeterminate', 'accounts_unsupported',
        'counts',
    ):
        if key in result:
            response[key] = result[key]

    order_id = result.get('order_id')
    if order_id:
        response['order_ids'] = [order_id]
        order = _owned_order(order_id)
        if order is not None:
            directory = _account_directory()
            response['splits'] = [
                _split_payload(split, directory)
                for split in order.splits.order_by(EquityOrderSplit.account_id).all()
            ]
    return response


def _place_sell(instruction, gtt_trigger_leg):
    """
    A SELL is an exit, so every account goes through the claim.

    The stop loss and target monitor runs in the background scheduler and can
    decide to sell the same shares in the same second. Both paths therefore
    meet at EquityHolding.claim_for_exit, which locks the row, re-checks it and
    commits the claim BEFORE a broker is called. Losing that race is reported
    as a skipped account, not as an error, and never as a second order.

    One consequence worth knowing: the engine's claim helper creates one parent
    order per account, because the claim belongs to one holding. A five account
    sell therefore appears in the order book as five orders of one account
    each, and order_ids carries all of them.
    """
    accounts, tracked = _sell_context(instruction)
    if not accounts:
        raise _BadRequest('Select at least one active account')

    preview = preview_order_split(
        user_id=current_user.id,
        symbol=instruction['symbol'],
        exchange=instruction['exchange'],
        side=instruction['side'],
        total_quantity=instruction['total_quantity'],
        order_type=instruction['order_type'],
        price=instruction['price'],
        trigger_price=instruction['trigger_price'],
        account_ids=instruction['account_ids'],
        quantity_overrides=instruction['quantity_overrides'],
        reference_price=instruction['reference_price'],
    )
    if preview.get('status') != 'success':
        raise _BadRequest(preview.get('message') or 'The order split could not be worked out')

    rows = _annotate_sell_capacity(
        preview['rows'], tracked, instruction['symbol'], instruction['exchange']
    )

    # Honour the ABORT policy BEFORE anything is claimed or sent. The buy path
    # already does this; without it here the sell path silently downgraded ABORT
    # to SKIP while the confirmation dialog promised the opposite, which is the
    # worst kind of disagreement between what a screen says and what it does.
    if instruction['insufficient_funds_action'] == EQUITY_FUNDS_ACTION_ABORT:
        blocked = [row for row in rows if not row.get('check_ok')]
        if blocked:
            names = ', '.join(
                str(row.get('account_name') or row.get('account_id')) for row in blocked
            )
            raise _BadRequest(
                'Nothing was placed. %d of %d accounts cannot sell this holding (%s), '
                'and the insufficient funds policy for this order is ABORT. '
                'Switch the policy to SKIP to place the remaining accounts.'
                % (len(blocked), len(rows), names)
            )

    directory = _account_directory()
    jobs = []
    row_by_holding = {}
    skipped_rows = []
    for row in rows:
        if not row['check_ok'] or not row.get('holding_id') or row['sell_quantity'] <= 0:
            skipped_rows.append(row)
            continue
        jobs.append({'holding_id': row['holding_id'], 'quantity': row['sell_quantity']})
        row_by_holding[row['holding_id']] = row

    results = _fan_out_exits(
        jobs,
        reason=EQUITY_EXIT_REASON_MANUAL,
        order_type=instruction['order_type'],
        price=instruction['price'],
        trigger_price=instruction['trigger_price'],
        gtt_trigger_leg=gtt_trigger_leg,
    )

    counts = _exit_counts(results)
    splits = []
    order_ids = []
    placed_quantity = 0

    for result in results:
        row = row_by_holding.get(result.get('holding_id')) or {}
        if result.get('order_id'):
            order_ids.append(result['order_id'])
        if result.get('status') == 'success':
            placed_quantity += _to_int(result.get('quantity'))

        split = None
        if result.get('split_id'):
            # Scoped through the parent order's owner as well as the id, so
            # this stays an owner-scoped query rather than an id lookup.
            split = EquityOrderSplit.query.join(
                EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
            ).filter(
                EquityOrderSplit.id == result['split_id'],
                EquityOrder.user_id == current_user.id
            ).first()
        if split is not None:
            payload = _split_payload(split, directory)
        else:
            payload = _skipped_split_payload(
                row.get('account_id') or result.get('account_id'),
                directory,
                quantity=row.get('sell_quantity', 0),
                ratio_quantity=row.get('ratio_quantity', 0),
                qty_ratio=row.get('qty_ratio', 0.0),
                est_value=row.get('est_value'),
                reason=result.get('message'),
                fill_status=(
                    EQUITY_SPLIT_STATUS_SKIPPED if result.get('status') == 'skipped'
                    else EQUITY_SPLIT_STATUS_FAILED
                ),
            )
        payload['holding_id'] = result.get('holding_id')
        payload['exit_status'] = result.get('status')
        payload['exit_message'] = result.get('message')
        # qty_ratio on a claim-backed split is 100, because the split really is
        # the whole of its own single account parent order. plan_qty_ratio is
        # the allocation ratio the split table showed before submit, so the
        # screen can keep rendering the same Qty Ratio column.
        payload['plan_qty_ratio'] = _pct(row.get('qty_ratio', 0.0))
        payload['plan_ratio_quantity'] = _to_int(row.get('ratio_quantity', 0))
        splits.append(payload)

    for row in skipped_rows:
        payload = _skipped_split_payload(
            row['account_id'], directory,
            quantity=0,
            ratio_quantity=row.get('ratio_quantity', 0),
            qty_ratio=row.get('qty_ratio', 0.0),
            est_value=row.get('est_value'),
            reason=row.get('check_reason') or 'Skipped before any order was sent',
        )
        payload['holding_id'] = row.get('holding_id')
        payload['exit_status'] = 'skipped'
        payload['exit_message'] = row.get('check_reason')
        payload['plan_qty_ratio'] = _pct(row.get('qty_ratio', 0.0))
        payload['plan_ratio_quantity'] = _to_int(row.get('ratio_quantity', 0))
        splits.append(payload)

    splits.sort(key=lambda row: row['account_id'] or 0)

    selected = len(rows)
    skipped_total = counts['accounts_skipped'] + len(skipped_rows)
    if counts['accounts_placed'] == 0:
        status = 'error'
    elif counts['accounts_placed'] < selected:
        status = 'partial'
    else:
        status = 'success'

    parts = [f'{counts["accounts_placed"]} of {selected} accounts placed']
    if counts['accounts_failed']:
        parts.append(f'{counts["accounts_failed"]} failed')
    if skipped_total:
        parts.append(f'{skipped_total} skipped')
    if counts['accounts_indeterminate']:
        parts.append(
            f'{counts["accounts_indeterminate"]} unconfirmed, verify at the broker'
        )
    if preview.get('leftover_quantity'):
        parts.append(
            f'{preview["leftover_quantity"]} shares left over after rounding down'
        )

    response = _placement_response(instruction, ', '.join(parts), status)
    response['order_ids'] = order_ids
    response['order_id'] = order_ids[0] if len(order_ids) == 1 else None
    response['placed_quantity'] = placed_quantity
    response['leftover_quantity'] = _to_int(preview.get('leftover_quantity'))
    response['ratio_leftover'] = _to_int(preview.get('ratio_leftover'))
    response['accounts_selected'] = selected
    response['accounts_placed'] = counts['accounts_placed']
    response['accounts_failed'] = counts['accounts_failed']
    response['accounts_skipped'] = skipped_total
    response['accounts_indeterminate'] = counts['accounts_indeterminate']
    response['splits'] = splits
    response['counts'] = {
        'total': selected,
        'placed': counts['accounts_placed'],
        'failed': counts['accounts_failed'],
        'skipped': skipped_total,
        'indeterminate': counts['accounts_indeterminate'],
    }
    return response


# ---------------------------------------------------------------------------
# M4b Order Status, M5 Order Book, M6 Trade Book
# ---------------------------------------------------------------------------

def _order_window(query, carry_open_gtt=True, date_from=None, date_to=None):
    """
    Restrict an order query to the window the PRD asks for.

    With no date filter the books show TODAY's orders, plus a GTT placed on an
    earlier day that is still working. A resting GTT is an open instruction: it
    has to stay reachable to be cancelled, which is exactly what would be lost
    by a plain "placed today" filter.
    """
    if date_from is not None or date_to is not None:
        if date_from is not None:
            query = query.filter(
                EquityOrder.placed_at >= datetime.combine(date_from, datetime.min.time())
            )
        if date_to is not None:
            query = query.filter(
                EquityOrder.placed_at <= datetime.combine(date_to, datetime.max.time())
            )
        return query

    start = _today_start()
    if not carry_open_gtt:
        return query.filter(EquityOrder.placed_at >= start)

    return query.filter(or_(
        EquityOrder.placed_at >= start,
        and_(
            EquityOrder.order_type == EQUITY_ORDER_TYPE_GTT,
            EquityOrder.status.in_(OPEN_ORDER_STATUSES),
        ),
    ))


def _order_filters(query, account_id=None, symbol=None, side=None, status=None,
                   order_type=None, source=None, nature_id=None):
    """Apply the shared Order Book and Trade Book filters."""
    if account_id is not None:
        query = query.filter(
            EquityOrder.splits.any(EquityOrderSplit.account_id == account_id)
        )
    if symbol:
        query = query.filter(EquityOrder.symbol == symbol)
    if side:
        query = query.filter(EquityOrder.side == side)
    if status:
        query = query.filter(EquityOrder.status == status)
    if order_type:
        query = query.filter(EquityOrder.order_type == order_type)
    if source:
        query = query.filter(EquityOrder.source == source)
    if nature_id is not None:
        query = query.filter(EquityOrder.trade_nature_id == nature_id)
    return query


def _sort_by_prd_status(rows):
    """
    PRD M4b ordering: PENDING, then PARTIAL, then COMPLETED, then CANCELLED, so
    open orders surface first. Python's sort is stable, so the newest-first
    ordering inside each group is the one the query already produced.
    """
    rows.sort(key=lambda row: ORDER_STATUS_SORT_RANK.get(
        row['status'], len(ORDER_STATUS_SORT_RANK)
    ))
    return rows


def _filter_options():
    """The filter dropdown contents every book screen needs."""
    return {
        'accounts': [
            {
                'account_id': account_id,
                'account_name': entry['account_name'],
                'broker_name': entry['broker_name'],
                'is_active': entry['is_active'],
            }
            for account_id, entry in sorted(_account_directory().items())
        ],
        'trade_natures': [
            {'id': nature.id, 'name': nature.name} for nature in _trade_natures()
        ],
        'sides': list(VALID_SIDES),
        'statuses': list(VALID_ORDER_STATUSES),
        'order_types': list(VALID_ORDER_TYPES),
    }


def _read_book_filters():
    """
    Read every Order Book and Trade Book filter from the query string.

    Returns (filters_dict, error_or_None).
    """
    account_id, error = _filter_account_id()
    if error:
        return None, error

    side, error = _arg_choice('side', VALID_SIDES, 'side')
    if error:
        return None, error

    status, error = _arg_choice('status', VALID_ORDER_STATUSES, 'status')
    if error:
        return None, error

    order_type, error = _arg_choice('order_type', VALID_ORDER_TYPES, 'order type')
    if error:
        return None, error

    date_from, error = _arg_date('from')
    if error:
        return None, error

    date_to, error = _arg_date('to')
    if error:
        return None, error

    if date_from and date_to and date_from > date_to:
        return None, 'The from date must not be after the to date'

    nature_id, error = _selected_trade_nature_id()
    if error:
        return None, error

    return {
        'account_id': account_id,
        'symbol': _arg_symbol(),
        'side': side,
        'status': status,
        'order_type': order_type,
        'trade_nature_id': nature_id,
        'date_from': date_from,
        'date_to': date_to,
    }, None


def _filters_echo(filters):
    """The filters as the screen sent them, echoed back for the controls."""
    return {
        'account': filters['account_id'] if filters['account_id'] is not None else 'all',
        'symbol': filters['symbol'] or '',
        'side': filters['side'] or 'all',
        'status': filters['status'] or 'all',
        'order_type': filters['order_type'] or 'all',
        'trade_nature': (
            filters['trade_nature_id'] if filters['trade_nature_id'] is not None else 'all'
        ),
        'from': filters['date_from'].isoformat() if filters['date_from'] else '',
        'to': filters['date_to'].isoformat() if filters['date_to'] else '',
    }


def _build_order_book(filters, carry_open_gtt=True, include_splits=False,
                      sort_by_status=False):
    """M5 Order Book, and the M4b Order Status list, which share one query."""
    query = EquityOrder.query.filter(EquityOrder.user_id == current_user.id)
    query = _order_filters(
        query,
        account_id=filters['account_id'],
        symbol=filters['symbol'],
        side=filters['side'],
        status=filters['status'],
        order_type=filters['order_type'],
        nature_id=filters['trade_nature_id'],
    )
    query = _order_window(
        query,
        carry_open_gtt=carry_open_gtt,
        date_from=filters['date_from'],
        date_to=filters['date_to'],
    )
    orders = query.order_by(EquityOrder.placed_at.desc(), EquityOrder.id.desc()).all()

    directory = _account_directory()
    start = _today_start()
    rows = []
    carried = 0
    for order in orders:
        splits = order.splits.order_by(EquityOrderSplit.account_id).all()
        if filters['account_id'] is not None:
            # An account filtered view shows only that account's share, so the
            # Accounts count stays honest instead of counting accounts the
            # admin filtered out.
            splits = [split for split in splits if split.account_id == filters['account_id']]
        payload = _order_payload(order, splits, directory, include_splits=include_splits)
        payload['is_carried_gtt'] = bool(
            order.placed_at is not None and order.placed_at < start
        )
        if payload['is_carried_gtt']:
            carried += 1
        rows.append(payload)

    if sort_by_status:
        _sort_by_prd_status(rows)

    return {
        'orders': rows,
        'totals': {
            'orders': len(rows),
            'quantity': sum(row['total_quantity'] for row in rows),
            'filled_quantity': sum(row['filled_quantity'] for row in rows),
            'open_orders': sum(1 for row in rows if row['is_open']),
            'carried_gtt_orders': carried,
        },
        'filters': _filters_echo(filters),
        'options': _filter_options(),
        'window': {
            'today_only': filters['date_from'] is None and filters['date_to'] is None,
            'carries_open_gtt': carry_open_gtt,
            'today': _iso(datetime.utcnow().date()),
        },
        'sort_order': list(VALID_ORDER_STATUSES) if sort_by_status else 'placed_at_desc',
        'generated_at': _iso(datetime.utcnow()),
    }


def _build_trade_book(filters):
    """
    M6 Trade Book: every fill, with its execution price and its parent order.

    Fills are written by the order status reconciliation, which is not part of
    this increment, so this list is empty until that lands. The screen has to
    render an empty state rather than assume rows.
    """
    query = db.session.query(EquityTrade, EquityOrderSplit, EquityOrder).join(
        EquityOrderSplit, EquityTrade.split_id == EquityOrderSplit.id
    ).join(
        EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
    ).filter(EquityOrder.user_id == current_user.id)

    if filters['account_id'] is not None:
        query = query.filter(EquityOrderSplit.account_id == filters['account_id'])
    if filters['symbol']:
        query = query.filter(EquityOrder.symbol == filters['symbol'])
    if filters['side']:
        query = query.filter(EquityOrder.side == filters['side'])
    if filters['status']:
        query = query.filter(EquityOrder.status == filters['status'])
    if filters['order_type']:
        query = query.filter(EquityOrder.order_type == filters['order_type'])
    if filters['trade_nature_id'] is not None:
        query = query.filter(EquityOrder.trade_nature_id == filters['trade_nature_id'])

    if filters['date_from'] is not None:
        query = query.filter(
            EquityTrade.executed_at >= datetime.combine(
                filters['date_from'], datetime.min.time()
            )
        )
    if filters['date_to'] is not None:
        query = query.filter(
            EquityTrade.executed_at <= datetime.combine(
                filters['date_to'], datetime.max.time()
            )
        )
    if filters['date_from'] is None and filters['date_to'] is None:
        query = query.filter(EquityTrade.executed_at >= _today_start())

    records = query.order_by(
        EquityTrade.executed_at.desc(), EquityTrade.id.desc()
    ).all()

    directory = _account_directory()
    rows = [
        _trade_payload(trade, split, order, directory)
        for trade, split, order in records
    ]

    return {
        'trades': rows,
        'totals': {
            'trades': len(rows),
            'quantity': sum(row['executed_quantity'] for row in rows),
            'value': _money(sum(row['trade_value'] for row in rows)),
        },
        'filters': _filters_echo(filters),
        'options': _filter_options(),
        'window': {
            'today_only': filters['date_from'] is None and filters['date_to'] is None,
            'today': _iso(datetime.utcnow().date()),
        },
        'generated_at': _iso(datetime.utcnow()),
    }


# ---------------------------------------------------------------------------
# Equity preferences
# ---------------------------------------------------------------------------

def _build_preferences_payload():
    """
    Settings: the module wide switches, plus proof that the stop loss monitor
    is actually running.

    monitor_last_run_at is written by the background scheduler job, not by a
    browser, so a recent heartbeat is what tells the admin the monitor is alive
    with every tab closed.
    """
    settings = _equity_settings()

    monitor = {'available': False}
    try:
        from app.utils.equity_exit_monitor import equity_exit_monitor
        monitor = equity_exit_monitor.status()
        monitor['available'] = True
    except Exception as exc:
        current_app.logger.debug(f'Equity exit monitor status unavailable: {exc}')

    return {
        'settings': {
            'insufficient_funds_action': settings.insufficient_funds_action,
            'default_exit_mode': settings.default_exit_mode,
            'sl_monitor_enabled': bool(settings.sl_monitor_enabled),
            'sl_monitor_interval_seconds': _to_int(settings.sl_monitor_interval_seconds),
            'price_alerts_enabled': bool(settings.price_alerts_enabled),
            'monitor_last_run_at': _iso(settings.monitor_last_run_at),
            'monitor_last_error': settings.monitor_last_error,
            'updated_at': _iso(settings.updated_at),
        },
        'monitor': monitor,
        'options': {
            'insufficient_funds_actions': list(VALID_FUNDS_ACTIONS),
            'exit_modes': list(VALID_EXIT_MODES),
            'monitor_interval_seconds': {
                'minimum': MIN_MONITOR_INTERVAL_SECONDS,
                'maximum': MAX_MONITOR_INTERVAL_SECONDS,
            },
        },
        'exit_mode_tags': EXIT_MODE_TAGS,
        'generated_at': _iso(datetime.utcnow()),
    }


def _json_route(view):
    """
    Standard JSON error handling for every increment 2 endpoint.

    A _BadRequest is the caller's mistake and comes back as a 400 with the
    message. Anything else is logged and comes back as a 500 envelope, so no
    endpoint can hand a traceback to the browser and every failure has one
    shape the frontend can read.

    Every refusal rolls the session back first. A handler that validates its
    entries one at a time can be part way through applying them when the next
    one turns out to be invalid, and those half-applied changes must not
    survive into whatever the request does next. A rollback cannot undo work
    that already committed, so an order that reached a broker is unaffected.
    """
    @wraps(view)
    def wrapper(*args, **kwargs):
        def discard():
            try:
                db.session.rollback()
            except Exception:
                pass

        try:
            return view(*args, **kwargs)
        except _BadRequest as exc:
            discard()
            return _json_error(str(exc))
        except EquityOrderError as exc:
            discard()
            return _json_error(str(exc))
        except Exception as exc:
            discard()
            current_app.logger.error(
                f'Equity endpoint {request.endpoint} failed: {exc}'
            )
            return _json_error(f'Request failed: {exc}', 500)
    return wrapper


def _ok(payload, message=''):
    """The success envelope every endpoint here returns."""
    payload['status'] = 'success'
    payload['message'] = message
    return jsonify(payload)


# ---------------------------------------------------------------------------
# Page routes added by increment 2
# ---------------------------------------------------------------------------

@equity_bp.route('/watchlist')
@login_required
def watchlist():
    """M3 Watch List. Data is loaded by the browser from /equity/api/watchlist."""
    return render_template(
        'equity/watchlist.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/place-order')
@login_required
def place_order():
    """M4 Place Order. The split table comes from /equity/api/order/preview."""
    return render_template(
        'equity/place_order.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/order-book')
@login_required
def order_book():
    """M5 Order Book."""
    return render_template(
        'equity/order_book.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


@equity_bp.route('/trade-book')
@login_required
def trade_book():
    """M6 Trade Book."""
    return render_template(
        'equity/trade_book.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures()
    )


# ---------------------------------------------------------------------------
# M3 Watch List
# ---------------------------------------------------------------------------

@equity_bp.route('/api/watchlist')
@login_required
@heavy_rate_limit()
@_json_route
def api_watchlist():
    """
    The watch list with a live price on every row.

    Prices come from the shared push feed, so a warm feed costs no broker call.
    The REST quote fallback can run for a symbol that has not ticked yet, which
    is why this is on the heavy limit.
    """
    return _ok(_build_watchlist_payload())


@equity_bp.route('/api/watchlist/quotes')
@login_required
@heavy_rate_limit()
@_json_route
def api_watchlist_quotes():
    """
    The 10 second price refresh for the watch list rows.

    Returns the same row shape as /equity/api/watchlist so the screen can
    replace rows in place, plus any price alerts that fired on this pass.
    """
    return _ok(_build_watchlist_payload())


@equity_bp.route('/api/watchlist', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_add_watchlist_item():
    """
    Add a stock to the watch list.

    Request body:
        {"symbol": "RELIANCE", "exchange": "NSE", "trade_nature_id": 1,
         "target_price": 1450.0, "alert_price": 1400.0,
         "alert_direction": "BELOW", "price_alert_enabled": true}

    alert_direction is optional. Left out, it is resolved from the first live
    price the row sees, because an alert price on its own does not say which
    way the price has to cross it.
    """
    data = _body()
    symbol = _read_symbol(data)
    exchange = _read_exchange(data)
    nature_id = _read_trade_nature_id(data)
    target_price = _read_price(data, 'target_price')
    alert_price = _read_price(data, 'alert_price')
    direction = _read_choice(
        data, 'alert_direction', VALID_ALERT_DIRECTIONS, required=False
    )
    enabled = _read_bool(data, 'price_alert_enabled', default=alert_price is not None)

    if enabled and alert_price is None:
        raise _BadRequest('A price alert needs an alert price')

    existing = EquityWatchlistItem.query.filter_by(
        user_id=current_user.id, symbol=symbol, exchange=exchange
    ).first()
    if existing is not None:
        raise _BadRequest(f'{symbol} is already on the watch list')

    count = EquityWatchlistItem.query.filter_by(user_id=current_user.id).count()
    if count >= MAX_WATCHLIST_ITEMS:
        raise _BadRequest(
            f'The watch list holds at most {MAX_WATCHLIST_ITEMS} stocks. '
            'Remove one before adding another.'
        )

    item = EquityWatchlistItem(
        user_id=current_user.id,
        symbol=symbol,
        exchange=exchange,
        trade_nature_id=nature_id,
        target_price=target_price,
        alert_price=alert_price,
        alert_direction=direction if alert_price is not None else None,
        price_alert_enabled=bool(enabled),
    )
    db.session.add(item)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise _BadRequest(f'{symbol} is already on the watch list')

    _log_activity('equity_watchlist_added', {
        'symbol': symbol, 'exchange': exchange, 'item_id': item.id
    })

    payload = _build_watchlist_payload()
    payload['item_id'] = item.id
    return _ok(payload, f'{symbol} added to the watch list')


@equity_bp.route('/api/watchlist/<int:item_id>', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_update_watchlist_item(item_id):
    """
    Update one watch list row: trade nature, target price and the price alert.

    Only the keys present in the body are changed. A key sent as null clears
    that field, so target_price: null removes the target while leaving the
    alert alone.

    Any change to alert_price, alert_direction or price_alert_enabled re-arms
    the alert, otherwise an alert that already fired would stay silent for
    good.

    Request body (all optional):
        {"trade_nature_id": 2, "target_price": 1500, "alert_price": 1400,
         "alert_direction": "BELOW", "price_alert_enabled": true,
         "rearm_alert": true}
    """
    item = _owned_watchlist_item(item_id)
    if item is None:
        return _json_error('Watch list item not found', 404)

    data = _body()
    alert_changed = bool(_read_bool(data, 'rearm_alert', default=False))

    if 'trade_nature_id' in data:
        item.trade_nature_id = _read_trade_nature_id(data)

    if 'target_price' in data:
        item.target_price = _read_price(data, 'target_price')

    if 'alert_price' in data:
        item.alert_price = _read_price(data, 'alert_price')
        alert_changed = True
        if item.alert_price is None:
            item.alert_direction = None
            item.price_alert_enabled = False

    if 'alert_direction' in data:
        item.alert_direction = _read_choice(
            data, 'alert_direction', VALID_ALERT_DIRECTIONS, required=False
        )
        alert_changed = True

    if 'price_alert_enabled' in data:
        enabled = _read_bool(data, 'price_alert_enabled', default=False)
        if enabled and item.alert_price is None:
            raise _BadRequest('A price alert needs an alert price')
        item.price_alert_enabled = bool(enabled)
        alert_changed = True

    if alert_changed:
        _clear_watchlist_alert(item)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity watch list update failed: {exc}')
        return _json_error(f'Failed to update the watch list row: {exc}', 500)

    _log_activity('equity_watchlist_updated', {
        'item_id': item.id, 'symbol': item.symbol, 'alert_rearmed': alert_changed
    })

    payload = _build_watchlist_payload()
    payload['item_id'] = item.id
    return _ok(payload, f'{item.symbol} updated')


@equity_bp.route('/api/watchlist/<int:item_id>', methods=['DELETE'])
@login_required
@api_rate_limit()
@_json_route
def api_delete_watchlist_item(item_id):
    """Remove one stock from the watch list."""
    item = _owned_watchlist_item(item_id)
    if item is None:
        return _json_error('Watch list item not found', 404)

    symbol = item.symbol
    db.session.delete(item)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity watch list delete failed: {exc}')
        return _json_error(f'Failed to remove the watch list row: {exc}', 500)

    _log_activity('equity_watchlist_removed', {'item_id': item_id, 'symbol': symbol})

    payload = _build_watchlist_payload()
    return _ok(payload, f'{symbol} removed from the watch list')


# ---------------------------------------------------------------------------
# Trade natures. Admin configurable, the four seeded values are only seeds.
# ---------------------------------------------------------------------------

@equity_bp.route('/api/trade-natures')
@login_required
@api_rate_limit()
@_json_route
def api_trade_natures():
    """Every trade nature, active and inactive, in display order."""
    return _ok(_build_trade_natures_payload())


@equity_bp.route('/api/trade-natures', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_create_trade_nature():
    """
    Create a trade nature.

    Request body: {"name": "Positional"}
    """
    data = _body()
    name = _read_text(data, 'name', maximum=50)

    existing = EquityTradeNature.query.filter_by(
        user_id=current_user.id, name=name
    ).first()
    if existing is not None:
        raise _BadRequest(f'A trade nature named {name} already exists')

    highest = db.session.query(
        db.func.max(EquityTradeNature.display_order)
    ).filter_by(user_id=current_user.id).scalar()

    nature = EquityTradeNature(
        user_id=current_user.id,
        name=name,
        display_order=_to_int(highest) + 1,
        is_active=True,
    )
    db.session.add(nature)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise _BadRequest(f'A trade nature named {name} already exists')

    _log_activity('equity_trade_nature_created', {'id': nature.id, 'name': name})

    payload = _build_trade_natures_payload()
    payload['trade_nature_id'] = nature.id
    return _ok(payload, f'Trade nature {name} created')


@equity_bp.route('/api/trade-natures/<int:nature_id>', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_update_trade_nature(nature_id):
    """
    Rename a trade nature, or activate and deactivate it.

    Request body (both optional): {"name": "Swing Trade", "is_active": false}

    A nature is never hard deleted: watch list rows, orders and holdings point
    at it, and removing it would break their history. Deactivating takes it out
    of the dropdowns and leaves every past reference intact.
    """
    nature = _owned_trade_nature(nature_id)
    if nature is None:
        return _json_error('Trade nature not found', 404)

    data = _body()
    changes = {}

    if 'name' in data:
        name = _read_text(data, 'name', maximum=50)
        if name != nature.name:
            clash = EquityTradeNature.query.filter(
                EquityTradeNature.user_id == current_user.id,
                EquityTradeNature.name == name,
                EquityTradeNature.id != nature.id
            ).first()
            if clash is not None:
                raise _BadRequest(f'A trade nature named {name} already exists')
            changes['name'] = {'from': nature.name, 'to': name}
            nature.name = name

    if 'is_active' in data:
        is_active = _read_bool(data, 'is_active', default=True)
        if bool(nature.is_active) != bool(is_active):
            changes['is_active'] = {'from': bool(nature.is_active), 'to': bool(is_active)}
            nature.is_active = bool(is_active)

    if 'display_order' in data:
        nature.display_order = _read_int(data, 'display_order', minimum=0, default=0)

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise _BadRequest('A trade nature with that name already exists')

    if changes:
        _log_activity('equity_trade_nature_updated', {'id': nature.id, 'changes': changes})

    return _ok(_build_trade_natures_payload(), f'Trade nature {nature.name} saved')


@equity_bp.route('/api/trade-natures/<int:nature_id>', methods=['DELETE'])
@login_required
@api_rate_limit()
@_json_route
def api_deactivate_trade_nature(nature_id):
    """
    Deactivate a trade nature.

    DELETE is a deactivation, not a removal, for the reason in the update
    endpoint: existing rows point at this nature and their history has to stay
    readable. Send is_active true through the update endpoint to bring it back.
    """
    nature = _owned_trade_nature(nature_id)
    if nature is None:
        return _json_error('Trade nature not found', 404)

    nature.is_active = False
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity trade nature deactivate failed: {exc}')
        return _json_error(f'Failed to deactivate the trade nature: {exc}', 500)

    _log_activity('equity_trade_nature_deactivated', {
        'id': nature.id, 'name': nature.name
    })
    return _ok(
        _build_trade_natures_payload(),
        f'Trade nature {nature.name} deactivated. Existing rows keep it.'
    )


@equity_bp.route('/api/trade-natures/reorder', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_reorder_trade_natures():
    """
    Set the order the trade natures appear in.

    Request body: {"order": [3, 1, 2]}

    Any nature not in the list keeps its place after the ones that are, so a
    partial list cannot silently drop a nature out of the ordering.
    """
    data = _body()
    raw = data.get('order')
    if not isinstance(raw, list) or not raw:
        raise _BadRequest('order must be a list of trade nature ids')

    wanted = []
    for value in raw:
        try:
            nature_id = int(value)
        except (TypeError, ValueError):
            raise _BadRequest('Invalid trade nature id in order')
        if nature_id not in wanted:
            wanted.append(nature_id)

    natures = {nature.id: nature for nature in _all_trade_natures()}
    unknown = [str(nature_id) for nature_id in wanted if nature_id not in natures]
    if unknown:
        raise _BadRequest(f'Trade nature {", ".join(unknown)} not found')

    position = 1
    for nature_id in wanted:
        natures[nature_id].display_order = position
        position += 1
    for nature_id, nature in sorted(natures.items()):
        if nature_id in wanted:
            continue
        nature.display_order = position
        position += 1

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity trade nature reorder failed: {exc}')
        return _json_error(f'Failed to reorder the trade natures: {exc}', 500)

    _log_activity('equity_trade_natures_reordered', {'order': wanted})
    return _ok(_build_trade_natures_payload(), 'Trade nature order saved')


# ---------------------------------------------------------------------------
# Shared market data: symbol search and market depth. Broker READS only.
# ---------------------------------------------------------------------------

@equity_bp.route('/api/symbol-search')
@login_required
@heavy_rate_limit()
@_json_route
def api_symbol_search():
    """
    Search NSE and BSE for an equity or an ETF.

    Query string:
        q         the search text, at least two characters. Required.
        exchange  NSE or BSE to narrow the search. Optional.
        account   read the search through this account. Optional, any
                  connected account answers it.

    Derivatives and indices are filtered out: this module trades CNC delivery,
    so a futures or options contract is never a valid answer.

    Response: {"status", "message", "query", "exchange", "results": [
        {"symbol", "exchange", "name", "token", "instrument_type",
         "lot_size", "tick_size"}], "count"}
    """
    query = _arg('q') or _arg('query')
    if len(query) < 2:
        raise _BadRequest('Enter at least two characters to search')
    if len(query) > 50:
        raise _BadRequest('The search text must be 50 characters or fewer')

    exchange = _arg('exchange').upper()
    if exchange and exchange not in SEARCH_EXCHANGES:
        raise _BadRequest(f'exchange must be one of {", ".join(SEARCH_EXCHANGES)}')

    credential, error = _read_credential()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    try:
        client = ExtendedOpenAlgoAPI(
            api_key=credential['api_key'],
            host=credential['host_url'],
            timeout=BROKER_TIMEOUT_SECONDS
        )
        response = client.search(query=query, exchange=exchange or None)
    except Exception as exc:
        current_app.logger.warning(f'Equity symbol search failed: {exc}')
        return _json_error(f'Symbol search is unavailable: {exc}', 502)

    if not isinstance(response, dict) or response.get('status') != 'success':
        message = (response or {}).get('message') or 'Symbol search returned no result'
        return _json_error(message, 502)

    results = _normalise_search_results(response)
    return _ok({
        'query': query,
        'exchange': exchange or 'all',
        'results': results,
        'count': len(results),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/depth')
@login_required
@heavy_rate_limit()
@_json_route
def api_depth():
    """
    Five level market depth for one symbol, for the Place Order Depth panel.

    Query string:
        symbol    required
        exchange  defaults to NSE
        account   read the depth through this account. Optional.

    Response: {"status", "message", "depth": {
        "symbol", "exchange",
        "bids": [{"level", "price", "quantity", "orders", "fill_pct"} x5],
        "asks": [ ... x5 ],
        "totals": {"bid_quantity", "ask_quantity", "bid_quantity_5",
                   "ask_quantity_5"},
        "ohlc": {"open", "high", "low", "close"},
        "ltp", "prev_close", "change", "change_pct", "volume", "ltq", "ltt",
        "oi", "upper_circuit", "lower_circuit"}}
    """
    symbol = _arg('symbol').upper()
    if not symbol:
        raise _BadRequest('symbol is required')
    exchange = (_arg('exchange') or 'NSE').upper()

    credential, error = _read_credential()
    if error:
        return _json_error(error, 404 if error == 'Account not found' else 400)

    try:
        client = ExtendedOpenAlgoAPI(
            api_key=credential['api_key'],
            host=credential['host_url'],
            timeout=BROKER_TIMEOUT_SECONDS
        )
        response = client.depth(symbol=symbol, exchange=exchange)
    except Exception as exc:
        current_app.logger.warning(f'Equity depth read failed for {symbol}: {exc}')
        return _json_error(f'Market depth is unavailable: {exc}', 502)

    if not isinstance(response, dict) or response.get('status') != 'success':
        message = (response or {}).get('message') or 'Market depth returned no result'
        return _json_error(message, 502)

    return _ok({
        'depth': _normalise_depth(response.get('data'), symbol, exchange),
        'account_id': credential.get('account_id'),
        'generated_at': _iso(datetime.utcnow()),
    })


# ---------------------------------------------------------------------------
# M4 Place Order
# ---------------------------------------------------------------------------

@equity_bp.route('/api/order/preview', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_order_preview():
    """
    The ACCOUNT-WISE ORDER SPLIT table. Places NOTHING and writes no order.

    Request body:
        {"symbol": "RELIANCE", "exchange": "NSE", "side": "BUY",
         "order_type": "MARKET", "total_quantity": 100,
         "price": null, "trigger_price": null,
         "account_ids": [1, 2, 3],
         "quantity_overrides": {"2": 15},
         "reference_price": null,
         "insufficient_funds_action": "SKIP"}

    reference_price is only a hint. Left out on a MARKET order the live last
    traded price is resolved here, because Est. Value and the cash check need a
    price and the browser is not trusted to supply one.

    Response:
        {"status", "message", "symbol", "exchange", "side", "order_type",
         "product", "price", "trigger_price", "total_quantity",
         "rows": [...], "leftover_quantity", "ratio_leftover",
         "allocated_quantity", "reference_price", "total_est_value",
         "accounts_selected", "accounts_ok", "accounts_flagged",
         "insufficient_funds_action", "claim_backed", "generated_at"}

    Each row:
        {"account_id", "account_name", "broker_name", "qty_ratio",
         "ratio_quantity", "quantity", "qty_overridden", "est_value",
         "cash_balance", "funds_checked", "required_cash",
         "check_ok", "check_reason"}
    and for a SELL, additionally:
        {"holding_id", "holding_quantity", "pledged_quantity",
         "sellable_quantity", "sell_quantity", "exit_status"}
    """
    instruction = _read_instruction(_body())
    return _ok(_build_order_preview(instruction))


@equity_bp.route('/api/order/place', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_place_order():
    """
    Place one equity instruction across the ticked accounts, simultaneously.

    Request body: everything /equity/api/order/preview takes, plus
        {"stop_loss": 1350, "target": 1600, "trade_nature_id": 1,
         "gtt_trigger_leg": "SL"}

    HOW A SELL DIFFERS. A sell against a tracked holding is an exit, and the
    background stop loss monitor can decide to sell the same shares in the same
    second. Every SELL therefore goes through the engine's claim-and-place
    helper, which locks the holding row, commits the claim and only then calls
    the broker. Two consequences the screen has to know about:
        claim_backed is true, and the engine creates one parent order per
            account, so order_ids carries one id per account that placed and
            order_id is null unless exactly one account placed.
        an account with nothing deliverable is SKIPPED with a reason rather
            than sent, because a CNC sell without the shares is a short
            delivery.
    A BUY has nothing to claim and goes straight through multi-account
    placement as one parent order.

    Product is always CNC. stop_loss and target are AlgoMirror's own levels,
    recorded on the order for the monitor and never sent to the broker. They
    are ignored on a SELL, which is itself an exit.

    Response:
        {"status": "success" | "partial" | "error", "message",
         "claim_backed", "order_id", "order_ids", "parent_status",
         "symbol", "exchange", "side", "order_type", "product",
         "price", "trigger_price", "total_quantity", "placed_quantity",
         "leftover_quantity", "ratio_leftover", "insufficient_funds_action",
         "error_message",
         "accounts_selected", "accounts_placed", "accounts_failed",
         "accounts_skipped", "accounts_indeterminate", "accounts_unsupported",
         "counts", "splits": [...], "generated_at"}

    Each split carries the keys documented on
    /equity/api/orders/<order_id>/splits, and on a claim-backed sell also
    holding_id, exit_status and exit_message.

    status is 'partial' when some accounts placed and some did not. That is a
    normal outcome, not a failure: the accounts that placed keep their orders.
    """
    data = _body()
    instruction = _read_instruction(data)
    gtt_trigger_leg = _read_choice(
        data, 'gtt_trigger_leg', ('SL', 'TG'), required=False
    )

    if instruction['side'] == EQUITY_SIDE_SELL:
        response = _place_sell(instruction, gtt_trigger_leg)
    else:
        response = _place_buy(
            instruction,
            stop_loss=_read_price(data, 'stop_loss'),
            target=_read_price(data, 'target'),
            trade_nature_id=_read_trade_nature_id(data),
            gtt_trigger_leg=gtt_trigger_leg,
        )

    _log_activity('equity_order_placed', {
        'symbol': instruction['symbol'],
        'exchange': instruction['exchange'],
        'side': instruction['side'],
        'order_type': instruction['order_type'],
        'total_quantity': instruction['total_quantity'],
        'claim_backed': response['claim_backed'],
        'order_ids': response['order_ids'],
        'accounts_placed': response['accounts_placed'],
        'accounts_selected': response['accounts_selected'],
        'result': response['status'],
    })

    # A partial fan-out is reported as HTTP 200 with status 'partial'. The
    # orders that were placed are real, and an error status code would invite
    # the browser to retry an instruction that is already at the broker.
    return jsonify(response)


# ---------------------------------------------------------------------------
# M4b Order Status
# ---------------------------------------------------------------------------

@equity_bp.route('/api/orders/status')
@login_required
@api_rate_limit()
@_json_route
def api_order_status():
    """
    Today's orders for the Order Status panel, open ones first.

    Sorted PENDING, then PARTIAL, then COMPLETED, then CANCELLED, newest first
    inside each group. A GTT placed on an earlier day that is still working is
    included, because a resting instruction has to stay reachable to be
    cancelled.

    Query string: the same filters as /equity/api/order-book, all optional.

    Response: the /equity/api/order-book shape, with splits included on every
    row so the View Split panel needs no second call.
    """
    filters, error = _read_book_filters()
    if error:
        return _json_error(error, 404 if 'not found' in error else 400)

    payload = _build_order_book(
        filters, carry_open_gtt=True, include_splits=True, sort_by_status=True
    )
    return _ok(payload)


@equity_bp.route('/api/orders/<int:order_id>')
@login_required
@api_rate_limit()
@_json_route
def api_order_detail(order_id):
    """One order with its per-account splits."""
    order = _owned_order(order_id)
    if order is None:
        return _json_error('Order not found', 404)

    directory = _account_directory()
    splits = order.splits.order_by(EquityOrderSplit.account_id).all()
    return _ok({
        'order': _order_payload(order, splits, directory, include_splits=True),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/orders/<int:order_id>/splits')
@login_required
@api_rate_limit()
@_json_route
def api_order_splits(order_id):
    """
    View Split: the per-account breakdown of one order.

    Response: {"status", "message", "order": {...}, "splits": [...],
               "leftover_quantity", "generated_at"}

    Each split:
        {"split_id", "order_id", "account_id", "account_name", "broker_name",
         "qty_ratio", "ratio_quantity", "quantity", "qty_overridden",
         "est_value", "cash_balance", "fill_status", "filled_quantity",
         "avg_fill_price", "broker_order_id", "broker_gtt_id",
         "broker_order_status", "error_message", "error_type",
         "attempt_count", "placed_at", "last_synced_at",
         "is_open", "is_terminal", "is_safe_to_retry"}

    qty_ratio_at_order and cash_balance_at_order are point-in-time snapshots
    taken when the order was created and are never recalculated (PRD 9.1).
    """
    order = _owned_order(order_id)
    if order is None:
        return _json_error('Order not found', 404)

    directory = _account_directory()
    splits = order.splits.order_by(EquityOrderSplit.account_id).all()
    return _ok({
        'order': _order_payload(order, splits, directory),
        'splits': [_split_payload(split, directory) for split in splits],
        'leftover_quantity': _to_int(order.leftover_quantity),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/orders/<int:order_id>/modify', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_modify_order(order_id):
    """
    Modify an order that is still PENDING or PARTIAL, account by account.

    Request body (all optional):
        {"price": 1425.5, "trigger_price": 1400, "total_quantity": 120,
         "quantity_overrides": {"2": 30}, "account_ids": [1, 2]}

    A new total quantity is re-split on the ratio ALREADY RECORDED against each
    split, never on today's allocations: the snapshot is point in time and a
    modify does not rewrite history.

    Response: {"status": "success" | "partial" | "error", "message",
               "order_id", "parent_status", "accounts_total", "accounts_ok",
               "accounts_failed", "accounts_indeterminate",
               "results": [{"account_id", "ok", "indeterminate",
                            "unsupported", "error_message", "error_type",
                            "action"}],
               "order": {...}, "splits": [...]}
    """
    order = _owned_order(order_id)
    if order is None:
        return _json_error('Order not found', 404)
    if not order.is_open:
        raise _BadRequest(
            f'This order is {order.status} and can no longer be modified. '
            'Only a PENDING or PARTIAL order can be changed.'
        )

    data = _body()
    result = modify_order(
        user_id=current_user.id,
        order_id=order_id,
        price=_read_price(data, 'price'),
        trigger_price=_read_price(data, 'trigger_price'),
        total_quantity=_read_int(data, 'total_quantity', minimum=1),
        quantity_overrides=_read_quantity_overrides(data),
        account_ids=data.get('account_ids') or None,
    )

    _log_activity('equity_order_modified', {
        'order_id': order_id,
        'result': result.get('status'),
        'accounts_ok': result.get('accounts_ok'),
    })

    order = _owned_order(order_id)
    if order is not None:
        directory = _account_directory()
        splits = order.splits.order_by(EquityOrderSplit.account_id).all()
        result['order'] = _order_payload(order, splits, directory)
        result['splits'] = [_split_payload(split, directory) for split in splits]
    return jsonify(result)


@equity_bp.route('/api/orders/<int:order_id>/cancel', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_cancel_order(order_id):
    """
    Cancel an order that is still PENDING or PARTIAL, account by account.

    Request body (optional): {"account_ids": [1, 2]} to cancel only those
    accounts. Left out, every account still working is cancelled.

    A split is marked CANCELLED only on an explicit broker confirmation. A
    cancel whose answer never arrived leaves the split as it was with the
    reason recorded: we do not know whether that order is still live, and
    guessing either way loses it.

    Response: the /equity/api/orders/<order_id>/modify shape, with action
    'cancel' on each result.
    """
    order = _owned_order(order_id)
    if order is None:
        return _json_error('Order not found', 404)
    if not order.is_open:
        raise _BadRequest(
            f'This order is {order.status} and can no longer be cancelled. '
            'Only a PENDING or PARTIAL order can be cancelled.'
        )

    data = _body()
    result = cancel_order(
        user_id=current_user.id,
        order_id=order_id,
        account_ids=data.get('account_ids') or None,
    )

    _log_activity('equity_order_cancelled', {
        'order_id': order_id,
        'result': result.get('status'),
        'accounts_ok': result.get('accounts_ok'),
    })

    order = _owned_order(order_id)
    if order is not None:
        directory = _account_directory()
        splits = order.splits.order_by(EquityOrderSplit.account_id).all()
        result['order'] = _order_payload(order, splits, directory)
        result['splits'] = [_split_payload(split, directory) for split in splits]
    return jsonify(result)


# ---------------------------------------------------------------------------
# M5 Order Book and M6 Trade Book
# ---------------------------------------------------------------------------

@equity_bp.route('/api/order-book')
@login_required
@api_rate_limit()
@_json_route
def api_order_book():
    """
    M5 Order Book.

    Query string, all optional:
        account       account id, or 'all'
        symbol        exact symbol
        side          BUY or SELL
        status        PENDING, PARTIAL, COMPLETED or CANCELLED
        order_type    MARKET, LIMIT or GTT
        trade_nature  trade nature id, or 'all'
        from, to      YYYY-MM-DD

    With no date filter the book shows TODAY's orders, plus a GTT placed on an
    earlier day that is still pending. Sending from or to switches to that
    window exactly, with no GTT carry-over, because an explicit range is an
    explicit question.

    Response:
        {"status", "message",
         "orders": [ ... one per order, see below ... ],
         "totals": {"orders", "quantity", "filled_quantity", "open_orders",
                    "carried_gtt_orders"},
         "filters": {...echoed...},
         "options": {"accounts", "trade_natures", "sides", "statuses",
                     "order_types"},
         "window": {"today_only", "carries_open_gtt", "today"},
         "sort_order", "generated_at"}

    Each order:
        {"order_id", "symbol", "exchange", "side", "order_type", "product",
         "total_quantity", "filled_quantity", "leftover_quantity",
         "price", "trigger_price", "stop_loss", "target",
         "status", "status_reason", "source",
         "trade_nature_id", "trade_nature", "insufficient_funds_action",
         "error_message", "placed_at", "cancelled_at", "updated_at",
         "accounts_count", "accounts_selected", "accounts_placed",
         "accounts_filled", "accounts_open", "accounts_label",
         "counts", "is_open", "can_modify", "can_cancel", "is_carried_gtt"}

    accounts_label is a FILLED over selected count, for example "4/5", as PRD
    7.1 and 7.6 specify. It was a placed-over-selected count while nothing
    booked a fill; the equity fill poller now writes EquityTrade rows, so the
    filled count is real. accounts_placed still carries "reached the broker"
    separately, so the two are not conflated. status_reason is the short
    explanation next to PARTIAL, for example "1 failed".
    """
    filters, error = _read_book_filters()
    if error:
        return _json_error(error, 404 if 'not found' in error else 400)

    include_splits = _arg('splits').lower() in ('1', 'true', 'yes')
    payload = _build_order_book(
        filters, carry_open_gtt=True, include_splits=include_splits
    )
    return _ok(payload)


@equity_bp.route('/api/trade-book')
@login_required
@api_rate_limit()
@_json_route
def api_trade_book():
    """
    M6 Trade Book: one row per fill, linked back to its parent order.

    Query string: the same filters as /equity/api/order-book. With no date
    filter it shows today's fills.

    Response:
        {"status", "message",
         "trades": [{"trade_id", "split_id", "order_id", "account_id",
                     "account_name", "broker_name", "symbol", "exchange",
                     "side", "order_type", "product", "source",
                     "trade_nature_id", "trade_nature",
                     "execution_price", "executed_quantity", "trade_value",
                     "executed_at", "broker_trade_id", "broker_order_id",
                     "order_status", "order_placed_at"}],
         "totals": {"trades", "quantity", "value"},
         "filters", "options", "window", "generated_at"}

    order_id is the link back to the parent order, which the screen resolves
    through /equity/api/orders/<order_id>/splits.

    Fills are written by the order status reconciliation, which is not part of
    this increment, so this list is empty until that lands. The screen must
    render an empty state rather than assume rows.
    """
    filters, error = _read_book_filters()
    if error:
        return _json_error(error, 404 if 'not found' in error else 400)

    return _ok(_build_trade_book(filters))


# ---------------------------------------------------------------------------
# M7 Holdings: stop loss, target, exit mode, the manual sell and the confirm
# queue.
#
# Every sell below goes through equity_order_engine.exit_holding, which is the
# one helper that claims a holding before it sells it.
# ---------------------------------------------------------------------------

def _read_level_entries(data):
    """
    Read the level editor payload.

    Accepts one entry as a flat object or many under "levels", so the Holdings
    screen can arm one account or every account holding a symbol in one call.
    """
    raw = data.get('levels')
    if raw is None:
        entries = [data]
    elif isinstance(raw, list):
        entries = raw
    else:
        raise _BadRequest('levels must be a list of holdings')

    entries = [entry for entry in entries if isinstance(entry, dict)]
    if not entries:
        raise _BadRequest('No holding was supplied')
    if len(entries) > 50:
        raise _BadRequest('At most 50 holdings can be edited in one request')
    return entries


def _resolve_level_targets(entries):
    """
    Resolve every entry to an account, before any broker read.

    Returns (account_ids, resolved) where resolved is one dict per entry with
    either a holding_id or the account, symbol and exchange the holding will be
    looked up by after the sync.
    """
    account_ids = set()
    resolved = []

    for entry in entries:
        if entry.get('holding_id') not in (None, ''):
            holding = _read_holding_id(entry)
            account_ids.add(holding.account_id)
            resolved.append({
                'entry': entry,
                'holding_id': holding.id,
                'key': _holding_key(holding.account_id, holding.symbol, holding.exchange),
            })
            continue

        account_id = _read_int(entry, 'account_id', minimum=1, required=True)
        if _owned_account(account_id) is None:
            raise _BadRequest(f'Account {account_id} not found')
        symbol = _read_symbol(entry)
        exchange = _read_exchange(entry)
        account_ids.add(account_id)
        resolved.append({
            'entry': entry,
            'holding_id': None,
            'key': _holding_key(account_id, symbol, exchange),
        })

    return sorted(account_ids), resolved


def _apply_levels(target, tracked):
    """
    Apply one level edit to one tracked holding, in memory.

    Returns (holding_or_None, breach_kind_or_sentinel, result_dict). The caller
    commits once for every entry and then re-arms the breach records, which has
    to happen after the commit because clear_breach takes its own row lock.
    """
    entry = target['entry']
    holding = tracked.get(target['key'])
    account_id, symbol, exchange = target['key']

    refusal = {
        'account_id': account_id,
        'symbol': symbol,
        'exchange': exchange,
        'holding_id': target['holding_id'],
        'ok': False,
        'message': '',
    }

    if holding is None:
        refusal['message'] = (
            f'This account does not hold {symbol}, so there is nothing to set '
            'a stop loss or a target on.'
        )
        return None, None, refusal

    if holding.exit_status not in EQUITY_HOLDING_STATUSES_CLAIMABLE:
        refusal['holding_id'] = holding.id
        refusal['message'] = (
            f'This holding is {holding.exit_status}. Resolve the exit before '
            'changing its levels.'
        )
        return None, None, refusal

    stop_loss = holding.stop_loss
    target_price = holding.target
    if 'stop_loss' in entry:
        stop_loss = _read_price(entry, 'stop_loss')
    if 'target' in entry:
        target_price = _read_price(entry, 'target')

    if stop_loss is not None and target_price is not None and stop_loss >= target_price:
        raise _BadRequest(
            f'{symbol}: the stop loss must be below the target. A stop loss at '
            'or above the target is treated as bad data and the monitor skips '
            'the row entirely.'
        )

    sl_changed = 'stop_loss' in entry and _to_float(holding.stop_loss) != _to_float(stop_loss)
    tp_changed = 'target' in entry and _to_float(holding.target) != _to_float(target_price)

    holding.stop_loss = stop_loss
    holding.target = target_price

    if 'exit_mode' in entry:
        holding.exit_mode = _read_choice(
            entry, 'exit_mode', VALID_EXIT_MODES, required=False,
            default=holding.exit_mode
        )
    if 'trade_nature_id' in entry:
        holding.trade_nature_id = _read_trade_nature_id(entry)

    if sl_changed and tp_changed:
        breach_kind = 'BOTH'
    elif sl_changed:
        breach_kind = EQUITY_EXIT_REASON_STOP_LOSS
    elif tp_changed:
        breach_kind = EQUITY_EXIT_REASON_TARGET
    else:
        breach_kind = None

    return holding, breach_kind, {
        'account_id': account_id,
        'symbol': symbol,
        'exchange': exchange,
        'holding_id': holding.id,
        'ok': True,
        'message': '',
    }


@equity_bp.route('/api/holdings/sync', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_sync_holdings():
    """
    Refresh the tracked holding rows from the broker.

    Why this exists: the Holdings screen reads the broker payload directly, but
    the stop loss and target monitor works on EquityHolding rows. A row with a
    stale quantity is a row the monitor could sell the wrong number of shares
    against, so the Holdings screen should call this when it loads and after a
    fill. A row the broker no longer reports has its quantity zeroed, which
    takes it out of the monitor's scan.

    The broker read is served from the 30 second cache when it is warm.

    Request body (optional): {"account_ids": [1, 2]}. Left out, every active
    account is refreshed.

    Response: {"status", "message", "accounts": [ids], "holdings": [...],
               "tracked", "monitorable", "generated_at"}
    """
    data = _body()
    account_ids = None
    if data.get('account_ids'):
        account_ids = _read_account_ids(data)

    context = _account_context(fetch_holdings=True, fetch_account_ids=account_ids)
    accounts = context['accounts']
    if account_ids is not None:
        wanted = set(account_ids)
        accounts = [account for account in accounts if account.id in wanted]

    tracked = _sync_holding_rows(accounts, context['snapshots'])
    directory = _account_directory()
    rows = [_holding_payload(holding, directory) for holding in tracked.values()]
    rows.sort(key=lambda row: (row['symbol'], row['account_id']))

    return _ok({
        'accounts': [account.id for account in accounts],
        'holdings': rows,
        'tracked': len(rows),
        'monitorable': sum(1 for row in rows if row['is_monitorable']),
        'stale_account_ids': [
            account.id for account in accounts
            if (context['snapshots'].get(account.id) or {}).get('is_stale')
        ],
        'generated_at': _iso(datetime.utcnow()),
    }, f'{len(rows)} holdings tracked')


@equity_bp.route('/api/holdings/levels', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_set_holding_levels():
    """
    Set the stop loss, target, exit mode and trade nature on one holding or on
    every account holding a symbol.

    Request body, either form:
        {"account_id": 1, "symbol": "RELIANCE", "exchange": "NSE",
         "stop_loss": 1350, "target": 1600, "exit_mode": "AUTO",
         "trade_nature_id": 2}
        {"levels": [ {...}, {...} ]}

    A holding can also be addressed by {"holding_id": 12} instead of the
    account and symbol.

    Only the keys present are changed. A key sent as null clears that level, so
    stop_loss: null removes the stop loss and leaves the target alone.

    Two things happen here that matter:
        The tracked holdings are refreshed from the broker first, so the
            monitor arms against the quantity the broker actually reports.
        Any change to stop_loss or target calls EquityHolding.clear_breach,
            which re-arms the level. Without it a level that already fired
            would stay silent for good.

    An exit mode of AUTO means a breach sells immediately. CONFIRM raises an
    alert and waits for approval, which is what /equity/api/holdings/exit-queue
    lists.

    Response: {"status", "message", "results": [{"account_id", "symbol",
               "exchange", "holding_id", "ok", "message"}],
               "updated", "skipped", "holdings": [...], "generated_at"}
    """
    data = _body()
    entries = _read_level_entries(data)
    account_ids, targets = _resolve_level_targets(entries)

    context = _account_context(fetch_holdings=True, fetch_account_ids=account_ids)
    wanted = set(account_ids)
    accounts = [account for account in context['accounts'] if account.id in wanted]
    tracked = _sync_holding_rows(accounts, context['snapshots'])

    results = []
    breaches = []
    changed = []
    for target in targets:
        holding, breach_kind, result = _apply_levels(target, tracked)
        results.append(result)
        if holding is None:
            continue
        changed.append(holding)
        if breach_kind is not None:
            breaches.append((holding.id, breach_kind))

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity holding level save failed: {exc}')
        return _json_error(f'Failed to save the levels: {exc}', 500)

    # After the commit, never before: clear_breach takes its own row lock and
    # commits, and it has to see the levels that were just written. The session
    # is expired first so the lock re-check reads the database rather than a
    # copy this request loaded earlier.
    db.session.expire_all()
    rearm_failed = []
    for holding_id, kind in breaches:
        try:
            if not EquityHolding.clear_breach(
                holding_id, current_user.id,
                kind=None if kind == 'BOTH' else kind
            ):
                rearm_failed.append(holding_id)
        except Exception as exc:
            db.session.rollback()
            rearm_failed.append(holding_id)
            current_app.logger.error(
                f'Could not re-arm the breach records on holding {holding_id}: {exc}'
            )

    updated = sum(1 for result in results if result['ok'])
    skipped = len(results) - updated
    _log_activity('equity_holding_levels_saved', {
        'updated': updated, 'skipped': skipped,
        'rearm_failed': rearm_failed,
        'holdings': [result['holding_id'] for result in results if result['ok']],
    })

    directory = _account_directory()
    holdings = [
        _holding_payload(holding, directory)
        for holding in EquityHolding.query.filter(
            EquityHolding.user_id == current_user.id,
            EquityHolding.id.in_([result['holding_id'] for result in results if result['ok']] or [0])
        ).all()
    ]

    message = f'{updated} holding{"" if updated == 1 else "s"} updated'
    if skipped:
        message += f', {skipped} skipped'
    if rearm_failed:
        # A level that was saved but not re-armed is a level the monitor will
        # stay silent on, so it is said out loud rather than only logged.
        message += (
            f', {len(rearm_failed)} could not be re-armed and will not alert '
            'until the level is saved again'
        )
    return _ok({
        'results': results,
        'updated': updated,
        'skipped': skipped,
        'rearm_failed': rearm_failed,
        'holdings': holdings,
        'generated_at': _iso(datetime.utcnow()),
    }, message)


def _read_sell_targets(data):
    """
    Resolve a manual sell request to holdings.

    Accepts:
        {"holdings": [{"holding_id": 5, "quantity": 10}, ...]}
        {"holding_ids": [5, 6], "quantity": 10}
        {"symbol": "RELIANCE", "exchange": "NSE", "account_ids": [1, 2],
         "quantity": 10}

    quantity is optional everywhere. Left out, the whole sellable quantity is
    sold, and it is resolved under the claim's own row lock rather than from a
    number this request read a moment earlier.
    """
    jobs = []

    raw = data.get('holdings')
    if isinstance(raw, list) and raw:
        for entry in raw:
            if not isinstance(entry, dict):
                raise _BadRequest('Each holding must be an object')
            holding = _read_holding_id(entry)
            job = {'holding_id': holding.id, 'account_id': holding.account_id}
            if 'quantity' in entry and entry.get('quantity') not in (None, ''):
                job['quantity'] = _read_int(entry, 'quantity', minimum=1, required=True)
            jobs.append(job)
        return jobs

    shared_quantity = None
    if data.get('quantity') not in (None, ''):
        shared_quantity = _read_int(data, 'quantity', minimum=1, required=True)

    raw_ids = data.get('holding_ids')
    if isinstance(raw_ids, list) and raw_ids:
        for value in raw_ids:
            holding = _read_holding_id({'holding_id': value})
            job = {'holding_id': holding.id, 'account_id': holding.account_id}
            if shared_quantity is not None:
                job['quantity'] = shared_quantity
            jobs.append(job)
        return jobs

    symbol = _read_symbol(data)
    exchange = _read_exchange(data)
    account_ids = None
    if data.get('account_ids'):
        account_ids = set(_read_account_ids(data))

    query = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.symbol == symbol,
        EquityHolding.exchange == exchange,
    )
    for holding in query.order_by(EquityHolding.account_id).all():
        if account_ids is not None and holding.account_id not in account_ids:
            continue
        job = {'holding_id': holding.id, 'account_id': holding.account_id}
        if shared_quantity is not None:
            job['quantity'] = shared_quantity
        jobs.append(job)

    if not jobs:
        raise _BadRequest(f'No tracked holding of {symbol} was found to sell')
    return jobs


@equity_bp.route('/api/holdings/sell', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_sell_holdings():
    """
    Sell against one or more tracked holdings, through the claim.

    Every account goes through equity_order_engine.exit_holding, which locks
    the holding row, re-checks that it is still claimable and carries no broker
    order id, commits the claim and only then calls the broker. That is what
    stops this and the background stop loss monitor selling the same shares
    twice.

    Request body, any of:
        {"holdings": [{"holding_id": 5, "quantity": 10}]}
        {"holding_ids": [5, 6], "quantity": 10}
        {"symbol": "RELIANCE", "exchange": "NSE", "account_ids": [1, 2]}
    plus optionally:
        {"order_type": "LIMIT", "price": 1420, "trigger_price": null,
         "gtt_trigger_leg": "SL"}

    quantity is optional. Left out, the whole sellable quantity is sold, which
    is the pledged shares subtracted from the holding.

    Response:
        {"status": "success" | "partial" | "error", "message",
         "accounts_selected", "accounts_placed", "accounts_failed",
         "accounts_skipped", "accounts_indeterminate",
         "results": [{"status", "holding_id", "account_id", "account_name",
                      "symbol", "exchange", "quantity", "order_id", "split_id",
                      "broker_order_id", "attempts", "claimed",
                      "indeterminate", "message"}],
         "order_ids", "generated_at"}

    A result status of 'skipped' means no broker call was made, usually because
    another exit was already in flight. 'indeterminate' means the outcome is
    unknown and the holding is parked as EXIT_INDETERMINATE for a human: it is
    never retried automatically.
    """
    data = _body()
    jobs = _read_sell_targets(data)

    order_type = _read_choice(
        data, 'order_type', VALID_ORDER_TYPES, required=False,
        default=EQUITY_ORDER_TYPE_MARKET
    )
    price = _read_price(data, 'price')
    trigger_price = _read_price(data, 'trigger_price')
    gtt_trigger_leg = _read_choice(data, 'gtt_trigger_leg', ('SL', 'TG'), required=False)

    if order_type == EQUITY_ORDER_TYPE_LIMIT and not price:
        raise _BadRequest('A LIMIT sell needs a price')
    if order_type == EQUITY_ORDER_TYPE_GTT and (not price or not trigger_price):
        raise _BadRequest('A GTT sell needs both a limit price and a trigger price')

    # The claim reads the quantity off the holding row, so refresh it from the
    # broker before anything is claimed.
    account_ids = sorted({job['account_id'] for job in jobs})
    context = _account_context(fetch_holdings=True, fetch_account_ids=account_ids)
    wanted = set(account_ids)
    accounts = [account for account in context['accounts'] if account.id in wanted]
    _sync_holding_rows(accounts, context['snapshots'])
    db.session.expire_all()

    results = _fan_out_exits(
        jobs,
        reason=EQUITY_EXIT_REASON_MANUAL,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        gtt_trigger_leg=gtt_trigger_leg,
    )

    directory = _account_directory()
    holdings = {
        holding.id: holding
        for holding in EquityHolding.query.filter(
            EquityHolding.user_id == current_user.id,
            EquityHolding.id.in_([job['holding_id'] for job in jobs])
        ).all()
    }
    for result in results:
        holding = holdings.get(result.get('holding_id'))
        account = directory.get(result.get('account_id')) or {}
        result['account_name'] = account.get('account_name')
        result['symbol'] = holding.symbol if holding is not None else None
        result['exchange'] = holding.exchange if holding is not None else None
        result['exit_status'] = holding.exit_status if holding is not None else None

    payload = _exit_counts(results)
    payload['results'] = results
    payload['order_ids'] = [
        result['order_id'] for result in results if result.get('order_id')
    ]
    payload['generated_at'] = _iso(datetime.utcnow())

    _log_activity('equity_holdings_sold', {
        'holdings': [job['holding_id'] for job in jobs],
        'order_type': order_type,
        'accounts_placed': payload['accounts_placed'],
        'accounts_selected': payload['accounts_selected'],
        'result': payload['status'],
    })

    return jsonify(payload)


@equity_bp.route('/api/holdings/exit-queue')
@login_required
@api_rate_limit()
@_json_route
def api_exit_queue():
    """
    The holdings waiting on a human.

    Three groups, and they are three different problems:
        awaiting_confirm  a CONFIRM mode holding whose stop loss or target was
            breached. The monitor alerted and stopped. Approve it with
            /equity/api/holdings/<id>/confirm-exit or decline it with
            /equity/api/holdings/<id>/dismiss-exit.
        in_flight         a sell that is claimed or already at the broker.
            Nothing to do but wait.
        indeterminate     a sell whose outcome was never confirmed. It is NEVER
            retried automatically. Check the broker order book, then clear it
            with /equity/api/holdings/<id>/resolve-exit.

    Response: {"status", "message", "awaiting_confirm": [...],
               "in_flight": [...], "indeterminate": [...],
               "counts": {...}, "monitor": {...}, "generated_at"}

    Every holding entry has the keys documented on
    /equity/api/holdings/sync.
    """
    statuses = (
        (EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,)
        + EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT
        + (EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE,)
    )
    rows = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.exit_status.in_(statuses)
    ).order_by(EquityHolding.symbol, EquityHolding.account_id).all()

    directory = _account_directory()
    awaiting = []
    in_flight = []
    indeterminate = []
    for holding in rows:
        payload = _holding_payload(holding, directory)
        if holding.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM:
            awaiting.append(payload)
        elif holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE:
            indeterminate.append(payload)
        else:
            in_flight.append(payload)

    settings = _equity_settings()
    return _ok({
        'awaiting_confirm': awaiting,
        'in_flight': in_flight,
        'indeterminate': indeterminate,
        'counts': {
            'awaiting_confirm': len(awaiting),
            'in_flight': len(in_flight),
            'indeterminate': len(indeterminate),
        },
        'monitor': {
            'enabled': bool(settings.sl_monitor_enabled) if settings else True,
            'last_run_at': _iso(settings.monitor_last_run_at) if settings else None,
            'last_error': settings.monitor_last_error if settings else None,
        },
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/holdings/<int:holding_id>/confirm-exit', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_confirm_exit(holding_id):
    """
    Approve the exit on a CONFIRM mode holding whose level was breached.

    The claim is taken from AWAITING_CONFIRM only, so an approval cannot fire
    against a holding that was never alerted. The exit reason recorded by the
    monitor is preserved, so the order book still says whether this was a stop
    loss or a target.

    Request body (optional): {"order_type": "LIMIT", "price": 1420,
                              "quantity": 10}

    Response: the exit_holding result, with account_name, symbol and exchange
    added. See /equity/api/holdings/sell for the shape and for what each status
    means.
    """
    holding = _owned_holding(holding_id)
    if holding is None:
        return _json_error('Holding not found', 404)
    if holding.exit_status != EQUITY_HOLDING_STATUS_AWAITING_CONFIRM:
        raise _BadRequest(
            f'This holding is {holding.exit_status} and is not waiting for an '
            'exit to be approved.'
        )

    data = _body()
    order_type = _read_choice(
        data, 'order_type', VALID_ORDER_TYPES, required=False,
        default=EQUITY_ORDER_TYPE_MARKET
    )
    price = _read_price(data, 'price')
    trigger_price = _read_price(data, 'trigger_price')
    quantity = _read_int(data, 'quantity', minimum=1)

    if order_type == EQUITY_ORDER_TYPE_LIMIT and not price:
        raise _BadRequest('A LIMIT sell needs a price')
    if order_type == EQUITY_ORDER_TYPE_GTT and (not price or not trigger_price):
        raise _BadRequest('A GTT sell needs both a limit price and a trigger price')

    reason = holding.exit_reason or EQUITY_EXIT_REASON_MANUAL
    symbol = holding.symbol
    exchange = holding.exchange
    account_id = holding.account_id

    # The transitions re-check under a row lock, so the session is expired
    # first and the lock reads the database rather than the copy loaded above.
    db.session.expire_all()

    result = exit_holding(
        user_id=current_user.id,
        holding_id=holding_id,
        reason=reason,
        quantity=quantity,
        order_type=order_type,
        price=price,
        trigger_price=trigger_price,
        allow_from=(EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,),
    )
    directory = _account_directory()
    result['account_name'] = (directory.get(account_id) or {}).get('account_name')
    result['symbol'] = symbol
    result['exchange'] = exchange

    _log_activity('equity_exit_confirmed', {
        'holding_id': holding_id, 'symbol': symbol, 'reason': reason,
        'result': result.get('status'), 'order_id': result.get('order_id'),
    }, account_id=account_id)

    return jsonify(result)


@equity_bp.route('/api/holdings/<int:holding_id>/dismiss-exit', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_dismiss_exit(holding_id):
    """
    Decline the exit on a CONFIRM mode holding whose level was breached.

    The holding goes back to ACTIVE and the breach record STAYS, so the monitor
    does not raise the same alert again ten seconds later while the price is
    still through the level. Editing the stop loss or the target re-arms it,
    which goes through /equity/api/holdings/levels.

    Response: {"status", "message", "holding": {...}}
    """
    holding = _owned_holding(holding_id)
    if holding is None:
        return _json_error('Holding not found', 404)

    symbol = holding.symbol
    db.session.expire_all()

    if not EquityHolding.dismiss_exit_confirm(holding_id, current_user.id):
        raise _BadRequest(
            'This holding is not waiting for an exit to be approved, so there '
            'is nothing to decline.'
        )

    _log_activity('equity_exit_dismissed', {'holding_id': holding_id, 'symbol': symbol})

    holding = _owned_holding(holding_id)
    return _ok({
        'holding': _holding_payload(holding, _account_directory()) if holding else None,
        'generated_at': _iso(datetime.utcnow()),
    }, f'Exit alert on {symbol} declined. The level stays set and will not alert again until it is edited.')


@equity_bp.route('/api/holdings/<int:holding_id>/resolve-exit', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_resolve_exit(holding_id):
    """
    Clear a holding parked as EXIT_INDETERMINATE, after a human checked the
    broker and confirmed no order is live.

    An indeterminate exit is never retried automatically, because the order may
    already be at the broker even though the answer never came back. This is
    the deliberate human step that reopens the holding.

    Request body (optional): {"note": "checked Zerodha, no order"}

    Response: {"status", "message", "holding": {...}}
    """
    holding = _owned_holding(holding_id)
    if holding is None:
        return _json_error('Holding not found', 404)

    data = _body()
    note = _read_text(data, 'note', maximum=500, required=False, default='')
    symbol = holding.symbol
    db.session.expire_all()

    if not EquityHolding.resolve_exit_indeterminate(
        holding_id, current_user.id, note=note or None
    ):
        raise _BadRequest(
            'This holding is not parked as an unconfirmed exit, so there is '
            'nothing to reconcile.'
        )

    _log_activity('equity_exit_reconciled', {
        'holding_id': holding_id, 'symbol': symbol, 'note': note
    })

    holding = _owned_holding(holding_id)
    return _ok({
        'holding': _holding_payload(holding, _account_directory()) if holding else None,
        'generated_at': _iso(datetime.utcnow()),
    }, f'{symbol} reopened. The monitor can watch it again.')


# ---------------------------------------------------------------------------
# Settings: the module wide switches
# ---------------------------------------------------------------------------

@equity_bp.route('/api/settings/preferences')
@login_required
@api_rate_limit()
@_json_route
def api_settings_preferences():
    """
    The equity module preferences, plus the stop loss monitor heartbeat.

    Response:
        {"status", "message",
         "settings": {"insufficient_funds_action", "default_exit_mode",
                      "sl_monitor_enabled", "sl_monitor_interval_seconds",
                      "price_alerts_enabled", "monitor_last_run_at",
                      "monitor_last_error", "updated_at"},
         "monitor": {...the exit monitor's own status block...},
         "options": {"insufficient_funds_actions", "exit_modes",
                     "monitor_interval_seconds": {"minimum", "maximum"}},
         "exit_mode_tags", "generated_at"}

    monitor_last_run_at is written by the background scheduler job, so a recent
    heartbeat is what proves the monitor runs with every browser tab closed.
    """
    return _ok(_build_preferences_payload())


@equity_bp.route('/api/settings/preferences', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_save_settings_preferences():
    """
    Save the equity module preferences.

    Request body (all optional, only the keys present are changed):
        {"insufficient_funds_action": "SKIP" | "ABORT",
         "default_exit_mode": "AUTO" | "CONFIRM",
         "sl_monitor_enabled": true,
         "sl_monitor_interval_seconds": 30,
         "price_alerts_enabled": true}

    insufficient_funds_action is the PRD default in force for a new order: SKIP
    lets every other account through, ABORT places nothing at all. It is
    snapshotted onto each order, so changing it here never rewrites what a past
    order did.

    default_exit_mode is the exit mode a newly tracked holding starts with. It
    does not change any holding that already exists.

    Response: the /equity/api/settings/preferences read shape.
    """
    data = _body()
    settings = _equity_settings()
    changes = {}

    if 'insufficient_funds_action' in data:
        value = _read_choice(data, 'insufficient_funds_action', VALID_FUNDS_ACTIONS)
        if value != settings.insufficient_funds_action:
            changes['insufficient_funds_action'] = {
                'from': settings.insufficient_funds_action, 'to': value
            }
            settings.insufficient_funds_action = value

    if 'default_exit_mode' in data:
        value = _read_choice(data, 'default_exit_mode', VALID_EXIT_MODES)
        if value != settings.default_exit_mode:
            changes['default_exit_mode'] = {
                'from': settings.default_exit_mode, 'to': value
            }
            settings.default_exit_mode = value

    if 'sl_monitor_enabled' in data:
        value = bool(_read_bool(data, 'sl_monitor_enabled', default=True))
        if value != bool(settings.sl_monitor_enabled):
            changes['sl_monitor_enabled'] = {
                'from': bool(settings.sl_monitor_enabled), 'to': value
            }
            settings.sl_monitor_enabled = value

    if 'sl_monitor_interval_seconds' in data:
        value = _read_int(
            data, 'sl_monitor_interval_seconds',
            minimum=MIN_MONITOR_INTERVAL_SECONDS,
            maximum=MAX_MONITOR_INTERVAL_SECONDS,
            required=True
        )
        if value != _to_int(settings.sl_monitor_interval_seconds):
            changes['sl_monitor_interval_seconds'] = {
                'from': _to_int(settings.sl_monitor_interval_seconds), 'to': value
            }
            settings.sl_monitor_interval_seconds = value

    if 'price_alerts_enabled' in data:
        value = bool(_read_bool(data, 'price_alerts_enabled', default=True))
        if value != bool(settings.price_alerts_enabled):
            changes['price_alerts_enabled'] = {
                'from': bool(settings.price_alerts_enabled), 'to': value
            }
            settings.price_alerts_enabled = value

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity preferences save failed: {exc}')
        return _json_error(f'Failed to save the equity preferences: {exc}', 500)

    if changes:
        _log_activity('equity_preferences_saved', {'changes': changes})

    return _ok(_build_preferences_payload(), 'Equity preferences saved')
