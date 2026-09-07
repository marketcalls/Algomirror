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
import hashlib
import io
import math
import re
import threading
import time
from concurrent.futures import (
    ThreadPoolExecutor,
    TimeoutError as FuturesTimeoutError,
    as_completed,
)
from datetime import date, datetime, timedelta
from functools import wraps

from flask import (
    Response, current_app, jsonify, redirect, render_template, request, url_for
)
from flask_login import current_user, login_required
from sqlalchemy import and_, or_
from sqlalchemy.exc import IntegrityError

from app import db
from app.equity import equity_bp
from app.models import (
    ActivityLog,
    EquityAccountAllocation,
    EquityAlertEvent,
    EquityBrokerageRate,
    EquityHolding,
    EquityIntradayShort,
    EquityOrder,
    EquityStockNote,
    EquityStockNoteVersion,
    EquityOrderSplit,
    EquitySetting,
    EquityTrade,
    EquityTradeNature,
    EquityWatchlist,
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
    EQUITY_ORDER_TYPE_SL_M,
    EQUITY_PRODUCT_CNC,
    EQUITY_PRODUCT_MIS,
    EQUITY_SHORT_STATUS_OPEN,
    EQUITY_SHORT_STATUSES_CLAIMABLE,
    EQUITY_SHORT_STATUSES_IN_FLIGHT,
    EQUITY_SIDE_BUY,
    EQUITY_SIDE_SELL,
    EQUITY_SPLIT_STATUS_FAILED,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUS_SKIPPED,
    EQUITY_SPLIT_STATUS_UNSUPPORTED,
    EQUITY_STOP_STATUS_FAILED,
    EQUITY_STOP_STATUS_NONE,
    EQUITY_STOP_STATUS_RESTING,
    EquityHoldingNotice,
    EQUITY_NOTICE_SHARES_ARRIVED,
    EQUITY_NOTICE_SHARES_LEFT,
    EQUITY_NOTICE_HOLDING_NEW,
    EQUITY_NOTICE_HOLDING_CLOSED,
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
    split_quantity_by_ratio,
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
    'turnover + SEBI charge on turnover + stamp duty on BUY turnover only + '
    'DP/AMC on SELL only, per scrip + GST at the configured percent of the '
    'service charges (brokerage + exchange transaction charge + SEBI charge + '
    'DP/AMC).',
    'GST falls on the service charges only. STT and stamp duty are taxes in '
    'their own right and carry no GST. Enter DP/AMC net of GST - the formula '
    'adds it.',
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

# Today's broker order and trade book, per account, with the moment each was
# read. Only the dashboard serves from here; the Order Book and Trade Book
# screens always read live, because they are the authority on what the broker
# holds and a summary widget is not.
_BOOKS_CACHE = {}
_BOOKS_UNREADABLE = set()
_BOOKS_REFRESHED_AT = {}

# Matches BROKER_CACHE_TTL_SECONDS, and for the same reason: the warmer runs
# inside this window, so a poll arriving at any moment finds the books fresh.
BOOKS_CACHE_TTL_SECONDS = 30

# When an equity screen last asked for account data. The background cache
# warmer reads this so it can go quiet outside market hours when nobody is
# looking, instead of calling the broker every twenty seconds all night.
_LAST_EQUITY_VIEW_AT = [0.0]

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

# The last price the REST backstop fetched for a symbol, keyed the same way.
#
# It exists for the case where the push feed has nothing: outside market hours,
# and during a feed outage. There the feed is empty on EVERY poll, so every
# poll was paying a broker round trip for a price that had not moved - about a
# second of the dashboard's evening load, on every single refresh.
#
# The window is the feed's own MAX_PRICE_AGE_SECONDS, deliberately. That number
# already encodes this system's answer to "how old is too old for a price", and
# having two different answers to that question would be worse than either.
# When the feed is alive this store is barely consulted, because the feed
# answers first.
REST_LTP_TTL_SECONDS = 90.0

# How far back to look for the buy that revived a holding, when there is no
# record of when the position closed. Settlement is T+1, so anything older than
# a few days cannot be part of the purchase that just brought the row back;
# looser than one day only to cover a long weekend with a holiday on the end.
UNSETTLED_LOOKBACK_DAYS = 5
_REST_LTP = {}

# The symbol set the equity screens last asked the price feed to hold, so
# symbols that are no longer held can be unsubscribed instead of accumulating
# against the feed's own ceiling.
_FEED_SYMBOLS = set()

# Which symbols the broker recognises, so a row carrying a name instead of a
# ticker can be told apart from one merely waiting for a price.
#
#     (symbol, exchange) -> True  the broker answered and knows it
#                        -> False the broker answered and does NOT know it
#                        absent  never asked, or the broker could not be reached
#
# Deliberately in memory rather than the database. A symbol master changes -
# WOCKHARDT became WOCKPHARMA - and a "not found" written to disk would outlive
# the truth. This is rebuilt on restart and re-asked once a day.
_SYMBOL_KNOWN = {}
_SYMBOL_KNOWN_DAY = [None]

# How many unrecognised-looking symbols one screen refresh may ask about, and
# how long it may spend. A watch list with forty new stocks must not turn one
# poll into forty broker calls.
MAX_SYMBOL_CHECKS_PER_PASS = 4
MAX_SYMBOL_CHECK_SECONDS = 4.0

# One lock for all three. Every critical section below is a dict or set
# operation on plain values, so a single lock is cheaper than three.
_CACHE_LOCK = threading.Lock()


def _symbol_known_cache():
    """
    Today's answers, emptied when the day turns.

    A symbol the broker did not know yesterday may be listed today, and a
    "not found" that never expires is how a screen ends up permanently wrong
    about a stock that exists.
    """
    today = (datetime.utcnow() + timedelta(hours=5, minutes=30)).date()
    with _CACHE_LOCK:
        if _SYMBOL_KNOWN_DAY[0] != today:
            _SYMBOL_KNOWN.clear()
            _SYMBOL_KNOWN_DAY[0] = today
        return dict(_SYMBOL_KNOWN)


def _remember_symbol_known(key, known):
    with _CACHE_LOCK:
        _SYMBOL_KNOWN[key] = bool(known)


def _broker_knows_symbol(credential, symbol, exchange):
    """
    Ask the broker whether this symbol exists. Returns True, False or None.

    None means the question could not be ASKED - no account, no answer, an
    error. That is not the same as "not found", and the difference is the whole
    point: marking a row as a bad symbol because the broker was briefly
    unreachable would be worse than saying nothing.
    """
    if credential is None:
        return None
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=credential['api_key'],
            host=credential['host_url'],
            timeout=BROKER_TIMEOUT_SECONDS
        )
        response = client.search(query=symbol, exchange=exchange or None)
    except Exception as exc:
        current_app.logger.debug(
            f'Symbol check for {symbol} could not be made: {exc}'
        )
        return None

    if not isinstance(response, dict) or response.get('status') != 'success':
        return None

    wanted = (symbol.upper(), (exchange or 'NSE').upper())
    for row in _normalise_search_results(response, symbol):
        if (row['symbol'].upper(), row['exchange'].upper()) == wanted:
            return True
    # The broker answered and this exact symbol was not among the answers.
    return False


def _check_unknown_symbols(creds, candidates):
    """
    Look up the symbols that have no price, a few at a time.

    Only asked about a symbol showing NO price: one that is quoting is
    self-evidently known, and asking would be a broker call to learn something
    the price already said.

    Bounded twice over - a count and a clock - because a freshly uploaded list
    of forty stocks would otherwise turn one screen refresh into forty broker
    calls. Whatever does not fit is asked on the next pass.
    """
    known = _symbol_known_cache()
    pending = [key for key in candidates if key not in known]
    if not pending:
        return known

    credential = _quote_credential(creds, {})
    if credential is None:
        return known

    deadline = time.monotonic() + MAX_SYMBOL_CHECK_SECONDS
    for key in pending[:MAX_SYMBOL_CHECKS_PER_PASS]:
        if time.monotonic() > deadline:
            break
        verdict = _broker_knows_symbol(credential, key[0], key[1])
        if verdict is None:
            # Unreachable, not unknown. Nothing is recorded, so the next pass
            # asks again rather than inheriting a guess.
            continue
        _remember_symbol_known(key, verdict)
        known[key] = verdict
        if verdict is False:
            current_app.logger.warning(
                f'Equity watch list carries {key[0]} on {key[1]}, which the '
                f'broker does not recognise'
            )
    return known


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


def _json_error(message, http_status=400, extra=None):
    """
    Standard error envelope. The frontend always checks the status field.

    `extra` carries detail that survives the failure - the per-account reasons
    behind a buy-back that mostly did not work, say. An error is exactly when
    the person most needs to know WHY, and a bare message throws that away.
    """
    payload = {'status': 'error', 'message': message}
    if extra:
        payload.update(extra)
    return jsonify(payload), http_status


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


def _rest_ltp_cached(key):
    """The REST backstop's last price for this symbol, or 0.0 if none is fresh."""
    with _CACHE_LOCK:
        stored = _REST_LTP.get(key)
    if not stored:
        return 0.0
    price, fetched_at = stored
    if (time.monotonic() - fetched_at) > REST_LTP_TTL_SECONDS:
        return 0.0
    return _to_float(price)


def _remember_rest_ltp(key, value):
    """Record a price the REST backstop answered with."""
    number = _to_float(value)
    if number <= 0:
        return
    with _CACHE_LOCK:
        _REST_LTP[key] = (number, time.monotonic())


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

def _account_credentials(accounts, force_refresh=False):
    """
    Extract plain credential and cache tuples BEFORE any thread is spawned. No
    ORM object and no lazy load ever crosses a thread boundary in this module.

    The two freshness flags are resolved here as well, for the same reason: the
    fan-out decides whether to call the broker at all from plain values, without
    touching an ORM row from a worker thread.

    force_refresh reports everything as stale, so the caller reads the broker
    whatever the cache says. Only the background warmer passes it, and it has
    to: the warmer goes through the same freshness gate as a screen, so on a
    tick where the cache was still fresh it skipped the broker AND therefore
    did not renew the freshness stamp. The cache then aged out on its own and
    whichever request arrived next paid for the read - which is what put a
    1.3 second account stage back on one dashboard poll in three.
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
            'funds_fresh': (not force_refresh) and _is_fresh(
                cached_funds, _funds_refreshed_at(account.id)
            ),
            'holdings_fresh': (not force_refresh) and _is_fresh(
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
                        'ltp': _first_number(data, _LTP_KEYS),
                        'prev_close': _first_number(data, _PREV_CLOSE_KEYS),
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
                # Read through the same key lists the rest of the module uses.
                # These two sites asked for the literal 'prev_close' alone,
                # while _PREV_CLOSE_KEYS exists precisely because brokers also
                # spell it prevclose, previous_close and close. That is why the
                # watch list, which reads through the list, showed a previous
                # close and the dashboard's Today's P&L sat at n/a for ever.
                quotes[(symbol, exchange)] = {
                    'ltp': _first_number(payload, _LTP_KEYS),
                    'prev_close': _first_number(payload, _PREV_CLOSE_KEYS),
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
        if want_prev_close:
            current_app.logger.debug(
                '[EQUITY_PRICE] keys=0 - empty symbol set, nothing to price'
            )
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
        else:
            # Nothing pushed for this symbol. Reuse the last price the REST
            # backstop fetched, if it is recent enough to still be worth
            # showing. 'live' below stays False either way, so a screen can
            # still tell a pushed price from a fetched one.
            ltp = _rest_ltp_cached(key)

        prev_close = _prev_close_cached(key)
        if prev_close <= 0:
            # Last resort for the day's reference price: the close the broker
            # put on the holding row itself. Free, and better than showing no
            # Today's P&L at all.
            prev_close = _to_float(row_closes.get(key))

        # 'live' means this price came from the push feed on THIS pass, which
        # is the only thing the alert monitor will act on. A price the REST
        # backstop supplied is good enough to show and not good enough to fire
        # an alert against, and the two have to be tellable apart or a screen
        # ends up claiming a liveness it does not have.
        quotes[key] = {'ltp': ltp, 'prev_close': prev_close, 'live': ltp > 0}

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
    fetched = {}

    # No early return here. An earlier version returned when `fallback_keys`
    # was empty, which is also the pass on which the diagnostic below was most
    # needed - the one where every symbol already looks resolved but the card
    # still reads n/a. A return that skips the instrumentation is the third
    # time the same mistake has been made on this function; the fetch is now
    # conditional instead, and the log is on the single exit path.
    if fallback_keys:
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
            if ltp > 0:
                _remember_rest_ltp(key, ltp)
            if ltp > 0 and entry['ltp'] <= 0:
                entry['ltp'] = ltp
                # 'live' is deliberately left False: this is a REST quote.
                stats['from_rest'] += 1
            prev_close = _to_float(data.get('prev_close'))
            if prev_close > 0:
                entry['prev_close'] = prev_close
            # Recorded even when it is zero: the broker answered, and asking
            # again on every poll for a close it does not publish is pure
            # latency.
            _remember_prev_close(key, prev_close)

    # One line per pass whenever a previous close was wanted, on every exit
    # path, with the per-symbol figures spelled out. A count alone cannot tell
    # "the close is missing" from "the close is present and the card is still
    # wrong", and those need opposite fixes.
    if want_prev_close:
        _feed_ltp = sum(1 for key in keys if _to_float(feed_prices.get(key)) > 0)
        _fetched_with_close = sum(
            1 for data in fetched.values() if _to_float(data.get('prev_close')) > 0
        )
        _final_with_close = sum(1 for key in keys if quotes[key]['prev_close'] > 0)
        _rows = '; '.join(
            f"{key[0]}@{key[1]} ltp={quotes[key]['ltp']} "
            f"close={quotes[key]['prev_close']}"
            for key in keys
        )
        # DEBUG on a clean pass, INFO when something is actually unresolved.
        #
        # This line was written to chase a missing previous close and then left
        # at INFO, where it wrote every ten seconds - and the Windows log
        # handler does not rotate, so it was the largest single contributor to
        # a file that only grows. The condition is about the DATA, not about
        # which branch of the code ran: a pass where every symbol has a price
        # and a close has nothing to report, and one where something is missing
        # still says so at INFO without anyone turning DEBUG on.
        _unresolved = len(missing_ltp) > 0 or _final_with_close < len(keys)
        _price_log = (
            current_app.logger.info if _unresolved else current_app.logger.debug
        )
        _price_log(
            f'[EQUITY_PRICE] keys={len(keys)} feed_ltp={_feed_ltp} '
            f'missing_ltp={len(missing_ltp)} missing_close={len(missing_close)} '
            f'asked={len(fallback_keys)} fetched={len(fetched)} '
            f'fetched_with_close={_fetched_with_close} '
            f'row_closes={len(row_closes)} final_with_close={_final_with_close} '
            f'rows={_rows}'
        )

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


def _sold_parcel_row(account, row, meta, sale, quantity, account_names,
                     broker_names):
    """
    One parcel of shares sold today that the broker still reports as held.

    It is history, not a position, so it carries a REALISED figure: what the
    shares were sold for against what they cost. The Holdings screen was
    showing an unrealised P&L against the live price instead, which on 4
    September read LT as a 90 rupee loss on a trade that had in fact made about
    166 - the wrong number and the wrong sign, on a position that no longer
    existed.

    The average sale price is taken over the shares that came back priced. When
    none of them did the parcel still lists, with no P&L rather than a
    fabricated one: the shares are genuinely pending and saying so is the whole
    point of the row.
    """
    sale = sale or {}
    priced = _to_int(sale.get('priced'))
    proceeds = _to_float(sale.get('proceeds'))
    sale_price = (proceeds / priced) if priced > 0 else 0.0

    avg_cost = _to_float(row.get('avg_cost'))
    if avg_cost <= 0 and meta is not None:
        avg_cost = _to_float(meta.avg_cost)

    realised = (
        (sale_price - avg_cost) * quantity
        if sale_price > 0 and avg_cost > 0 else None
    )

    return {
        'account_id': account.id,
        'account_name': account_names.get(account.id),
        'broker_name': broker_names.get(account.id),
        'symbol': row['symbol'],
        'exchange': row['exchange'],
        'quantity': quantity,
        'avg_cost': _money(avg_cost) if avg_cost > 0 else None,
        'sale_price': _money(sale_price) if sale_price > 0 else None,
        'proceeds': _money(sale_price * quantity) if sale_price > 0 else None,
        'realised_pnl': _money(realised) if realised is not None else None,
        'realised_pct': (
            _pct(signed_percent_of(realised, avg_cost * quantity))
            if realised is not None and avg_cost > 0 else None
        ),
        'sold_at': _iso(sale.get('last_at')) if sale.get('last_at') else None,
        'trade_nature': (
            meta.trade_nature.name if meta is not None and meta.trade_nature
            else None
        ),
    }


def _merge_sold_parcels(parcels):
    """
    One stock, one line, with the accounts behind it - as every other table on
    these screens now reads.
    """
    merged = {}
    order = []
    for parcel in parcels:
        key = (parcel['symbol'], parcel['exchange'])
        target = merged.get(key)
        if target is None:
            target = dict(parcel)
            target['accounts'] = []
            merged[key] = target
            order.append(key)

        target['accounts'].append({
            'account_id': parcel['account_id'],
            'account_name': parcel['account_name'],
            'broker_name': parcel['broker_name'],
            'quantity': parcel['quantity'],
            'sale_price': parcel['sale_price'],
            'realised_pnl': parcel['realised_pnl'],
        })

        if target is not parcel and len(target['accounts']) > 1:
            target['quantity'] += parcel['quantity']
            for field in ('proceeds', 'realised_pnl'):
                if parcel[field] is not None:
                    target[field] = _money((target[field] or 0) + parcel[field])
            # Weighted by shares, because two accounts rarely fill at the same
            # tick. Averaging the two averages would be wrong the moment the
            # split is uneven, which it usually is.
            priced = [
                entry for entry in target['accounts']
                if entry['sale_price'] is not None
            ]
            total = sum(entry['quantity'] for entry in priced)
            if total:
                target['sale_price'] = _money(sum(
                    entry['sale_price'] * entry['quantity'] for entry in priced
                ) / total)
            # A row speaking for more than one account names none of them.
            target['account_name'] = None
            target['broker_name'] = None
            target['account_id'] = None

    rows = [merged[key] for key in order]
    for parcel in rows:
        parcel['accounts_count'] = len(parcel['accounts'])
        cost = _to_float(parcel['avg_cost']) * parcel['quantity']
        parcel['realised_pct'] = (
            _pct(signed_percent_of(parcel['realised_pnl'], cost))
            if parcel['realised_pnl'] is not None and cost > 0 else None
        )
    rows.sort(key=lambda parcel: parcel['symbol'])
    return rows


def _unsettled_holding_rows(account_id, broker_rows, meta_map):
    """
    NOT CALLED since 6 September. Kept because the reasoning is worth having
    and reversing the decision is one line at the call site in
    `_build_holdings_payload`.

    The owner's rule now: a stock bought today is a POSITION until it settles,
    and it belongs on the Positions screen alone. What this function existed to
    protect - being able to change the stop loss on today's buy - is done by
    the Edit button on Positions instead.

    Rows for shares bought but not yet delivered, in the broker's own shape.

    The Holdings screen is built from the broker's holdings book, and that book
    does not list a delivery buy until it settles on T+1. Everything AlgoMirror
    knows about such a position - its quantity, its cost, and above all the
    stop loss now being watched on it - would therefore be invisible on the one
    screen where those levels are read and changed.

    Only rows the broker has NOT reported are added, so nothing is ever
    double-counted: the moment the broker starts listing the stock, its own row
    wins and this one is not produced.

    Marked ``is_unsettled`` so the screen can say plainly that these shares are
    bought and not yet delivered, rather than presenting them as an ordinary
    holding the broker has confirmed.
    """
    reported = {(row['symbol'], row['exchange']) for row in broker_rows}
    extra = []
    for key, holding in meta_map.items():
        if key[0] != account_id:
            continue
        if getattr(holding, 'is_settled', True):
            continue
        quantity = _to_int(holding.quantity)
        if quantity <= 0:
            continue
        if (key[1], key[2]) in reported:
            continue

        avg_cost = _to_float(holding.avg_cost)
        extra.append({
            'symbol': key[1],
            'exchange': key[2],
            'quantity': quantity,
            'avg_cost': avg_cost,
            'broker_ltp': 0.0,
            'prev_close': 0.0,
            'pnl': 0.0,
            'pnl_percent': 0.0,
            'cost_basis_derived': False,
            'cost_basis_missing': avg_cost <= 0.0,
            'pledged_quantity': 0,
            # Bought today, not delivered yet. The broker cannot confirm these
            # shares until settlement.
            'is_unsettled': True,
        })
    return extra


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
    _LAST_EQUITY_VIEW_AT[0] = time.monotonic()
    _ctx_started = time.monotonic()

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
        live = sum(
            1 for cred in creds
            if (fetch_funds and not cred.get('funds_fresh'))
            or (fetch_holdings and not cred.get('holdings_fresh'))
        )
        snapshots = _fan_out(creds, want_funds=fetch_funds, want_holdings=fetch_holdings)
        _refresh_account_cache(wanted, snapshots)
        current_app.logger.info(
            f'[EQUITY_TIMING] account context {time.monotonic() - _ctx_started:.2f}s '
            f'({len(creds)} accounts, {live} went to the broker)'
        )

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
    _build_started = time.monotonic()
    context = _account_context(fetch_funds=True, fetch_holdings=True)
    _after_context = time.monotonic()
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
    _after_prices = time.monotonic()
    meta_map = _holding_meta_map([account.id for account in accounts])

    cards = []
    stale_account_ids = []
    for account in accounts:
        snapshot = snapshots.get(account.id) or {}
        rows = per_account_rows.get(account.id, [])

        holdings_value = 0.0
        unrealised = 0.0
        unrealised_to_close = 0.0
        todays = 0.0
        # Starts TRUE and is falsified by a row that has no previous close,
        # rather than starting false and being confirmed by the first row that
        # has one. Two bugs came out of the old direction:
        #
        #   an account holding nothing reported Today's P&L as unknown, and the
        #       KPI takes all() across the cards, so one empty account took the
        #       whole strip to n/a - which is exactly what it was doing,
        #   a card with three rows and one previous close reported a Today's
        #       P&L covering that one row and called it known, because the
        #       other two silently added zero.
        #
        # Nothing is unknown about an account that holds nothing: its Today's
        # P&L is zero, and it is zero for certain.
        todays_known = True
        # True only while EVERY row has a previous close. A split where some
        # rows are measured to yesterday and others to cost is not a split, it
        # is two different questions added together.
        split_complete = True
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

            # Two figures that ADD UP rather than overlap, which is what the
            # product owner asked for and is how a set of books should read:
            #
            #   Unrealised P&L   profit up to yesterday's close
            #   Today's P&L      the move since yesterday's close
            #   together         the whole profit since purchase
            #
            # Buy at 1200, yesterday's close 1250, trading at 1280: 500 and 300,
            # totalling 800. The old pair reported 800 and 300 - the 800 already
            # contained the 300, so they could not be added or compared.
            #
            # Every row here is a HOLDING, and a holding exists only after T+1
            # settlement, so it was necessarily held at yesterday's close. The
            # same-day case that would otherwise need care - attributing profit
            # to a day the shares were not owned - cannot arise on this screen.
            prev_close = _to_float((quote or {}).get('prev_close'))

            if avg_cost > 0:
                unrealised += gross_pnl(ltp, avg_cost, quantity)
                if prev_close > 0:
                    unrealised_to_close += gross_pnl(prev_close, avg_cost, quantity)
                else:
                    split_complete = False
            elif prev_close <= 0:
                split_complete = False

            if prev_close > 0:
                # Structurally the same calculation as gross P&L, with the
                # previous close standing in for the average cost, so the engine
                # is reused rather than duplicated.
                todays += gross_pnl(ltp, prev_close, quantity)
            else:
                # This row cannot be measured against yesterday, so the card's
                # Today's P&L would be short by exactly this row. Better to say
                # so than to publish a total that quietly omits it.
                todays_known = False
                split_complete = False

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
            # Split when every row could be split, otherwise the whole profit
            # since purchase. The screen is told which, so the label can say so
            # rather than the number quietly changing meaning.
            'unrealised_pnl': _money(unrealised_to_close if split_complete else unrealised),
            'unrealised_to_previous_close': split_complete,
            'unrealised_total': _money(unrealised),
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

    shorts = _open_shorts_summary()

    kpi = {
        'active_accounts': len(allocation_amounts),
        'connected_accounts': sum(1 for card in cards if not card['is_stale']),
        'total_portfolio_value': _money(sum(card['holdings_value'] for card in cards)),
        'available_cash': _money(sum(card['available_cash'] for card in cards)),
        # ALL, not any: one account that could not be split makes the total a
        # mixture of two different measures, and a mixture must not be labelled
        # as either one.
        'unrealised_pnl': _money(sum(
            (card['unrealised_pnl'] if all(c['unrealised_to_previous_close'] for c in cards)
             else card['unrealised_total'])
            for card in cards
        )),
        'unrealised_to_previous_close': all(
            card['unrealised_to_previous_close'] for card in cards
        ) and bool(cards),
        'todays_pnl': _money(sum(card['todays_pnl'] for card in cards)),
        'todays_pnl_available': all(card['todays_pnl_available'] for card in cards) and bool(cards),
        'open_orders': open_orders,
        'total_equity_fund_allocation': _money(sum(allocation_amounts.values())),
    }

    _todays_orders_rows, _todays_orders_age = _timed_todays_orders(
        _build_started, _after_context, _after_prices
    )

    return {
        'kpi': kpi,
        # Not a KPI. A KPI is a measure of how things are going; this is a job
        # that has to be done before the close, and it belongs at the top of
        # the screen where a job belongs - not in a row of tiles a person's eye
        # skims past.
        'shorts': shorts,
        'accounts': cards,
        'todays_orders': _todays_orders_rows,
        'todays_orders_book_age_seconds': _todays_orders_age,
        'stale_account_ids': stale_account_ids,
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    }


def _open_shorts_summary():
    """
    The shorts still owed, for the top of the Dashboard.

    Every other number on that screen describes how the portfolio is doing. This
    one describes something that MUST be done today, and it was the only place
    in the application where an open short was invisible - which is exactly the
    screen a person looks at first.

    Never raises. A Dashboard that will not load because of this block would be
    a worse outcome than a Dashboard that does not mention it.
    """
    empty = {
        'count': 0, 'quantity': 0, 'symbols': [], 'unprotected': 0,
        'squareoff_at': None, 'minutes_to_squareoff': None, 'past_squareoff': False,
    }
    try:
        rows = EquityIntradayShort.query.filter(
            EquityIntradayShort.user_id == current_user.id,
            EquityIntradayShort.status.in_(EQUITY_SHORT_STATUSES_UNSETTLED),
            EquityIntradayShort.quantity > 0,
        ).all()
    except Exception as exc:
        current_app.logger.debug(f'Could not read open shorts for the dashboard: {exc}')
        return empty

    if not rows:
        cutoff, squareoff, _on = _intraday_rules()
        empty['squareoff_at'] = '%02d:%02d' % (squareoff // 60, squareoff % 60)
        return empty

    _cutoff, squareoff, _monitor_on = _intraday_rules()
    minutes_left = squareoff - _ist_minute_now()

    symbols = []
    for row in rows:
        symbol = (row.symbol or '').strip().upper()
        if symbol and symbol not in symbols:
            symbols.append(symbol)

    return {
        'count': len(rows),
        'quantity': sum(_to_int(row.quantity) for row in rows),
        'symbols': symbols[:6],
        # A short with no order resting at the broker is only protected while
        # this machine is running, which is a different and worse situation
        # than one that is - so it is counted separately rather than folded in.
        'unprotected': sum(
            1 for row in rows if row.stop_status != EQUITY_STOP_STATUS_RESTING
        ),
        # In flight means a buy-back has been claimed or sent and has not come
        # back yet. Still owed, but nothing more should be done about it.
        'in_flight': sum(
            1 for row in rows if row.status != EQUITY_SHORT_STATUS_OPEN
        ),
        'squareoff_at': '%02d:%02d' % (squareoff // 60, squareoff % 60),
        'minutes_to_squareoff': minutes_left,
        'past_squareoff': minutes_left <= 0,
    }


def _timed_todays_orders(build_started, after_context, after_prices):
    """
    Today's Orders, with one line saying where the dashboard's time actually
    went. TEMPORARY, while the load time is being brought down; the stages are
    accounts (broker funds and holdings), prices, and books (the broker order
    and trade book behind Today's Orders).
    """
    started = time.monotonic()
    rows, age = _build_todays_orders()
    finished = time.monotonic()
    current_app.logger.info(
        f'[EQUITY_TIMING] dashboard total {finished - build_started:.2f}s = '
        f'accounts {after_context - build_started:.2f}s + '
        f'prices {after_prices - after_context:.2f}s + '
        f'cards {started - after_prices:.2f}s + '
        f'books {finished - started:.2f}s (book age {age:.1f}s)'
    )
    return rows, age


def _build_todays_orders():
    """
    Today's equity orders for the dashboard, newest first.

    Delegates to the Order Book rather than querying orders itself. It used to
    build its own list, and that list quietly behaved differently from every
    other book on the site: two exits placed a second apart on two accounts
    showed as two rows where the Order Book showed one, and an order placed at
    the broker terminal did not appear at all - so the dashboard reported three
    orders on a day with five. One builder, one behaviour.

    Delegating also brings the rest for free: the NOT AT BROKER marking, and an
    account that could not be read being reported rather than assumed empty.

    Deliberately today only, with no carry-over of an older resting GTT: this
    list is headed Today's Orders and a yesterday order in it would be a lie.
    The Order Book does carry an older open GTT, see _order_window.

    The day boundary is UTC. Indian market hours (09:15 to 15:30 IST) map to
    03:45 to 10:00 UTC on the same calendar date, so a trading day never
    straddles the UTC boundary.

    Serves the broker book from the cache the background warmer keeps, rather
    than paying two broker round trips on every dashboard poll. That read was
    the whole of the dashboard's remaining load time once funds and holdings
    were taken off the request path.

    What that costs, precisely: an order placed at the BROKER TERMINAL can take
    up to BOOKS_CACHE_TTL_SECONDS to appear here. An order placed through
    AlgoMirror appears at once, because it comes from AlgoMirror's own tables
    and never depended on the broker book. The age of the book is returned with
    the rows so the screen can state it instead of implying the list is live.

    The Order Book screen itself still reads live, and is the place to look
    when the exact broker position matters.

    Returns (rows, broker_book_age_seconds).
    """
    payload = _build_order_book(
        {
            'account_id': None,
            'symbol': None,
            'side': None,
            'status': None,
            'order_type': None,
            'trade_nature_id': None,
            'date_from': None,
            'date_to': None,
        },
        carry_open_gtt=False,
        include_splits=False,
        sort_by_status=False,
        merge_broker=True,
        books_may_be_cached=True,
    )
    age = (payload.get('window') or {}).get('broker_book_age_seconds') or 0.0
    return payload.get('orders') or [], age


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
    ratios = context['ratios']

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
        # Holdings is the broker's holdings book and nothing else.
        #
        # Until 6 September this list was topped up with `_unsettled_holding_rows`
        # - AlgoMirror's own reconstruction of a delivery buy that has filled but
        # not yet been delivered, which the broker does not report until T+1. It
        # was added for one reason: the stop loss on this morning's buy had to be
        # readable and changeable somewhere, and Holdings was the only screen
        # that could change it.
        #
        # The owner removed it: a stock bought today is a POSITION until it
        # settles, and putting it here as well meant one purchase appearing on
        # two screens and counted in the totals of both. The Positions screen
        # gained an Edit of its own in the same step, so nothing is lost - see
        # `can_set_levels` in api_positions.
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

    # Read once for the whole screen. Says what AlgoMirror sold today; corrects
    # nothing.
    sold_today = _algomirror_sold_today()

    buckets = {}
    sold_parcels = []
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

            # Shares sold through AlgoMirror today that the broker has not yet
            # removed from its holdings book. They are not a holding: the stake
            # they represent is gone, the P&L on them is realised rather than
            # unrealised, and the cost of exiting them has already been paid.
            # Leaving them in the list made every one of those figures wrong.
            #
            # Capped at what the broker actually still reports. A broker that
            # HAS already removed them - which a real one does on fill - leaves
            # nothing to cap, so nothing is subtracted and no parcel appears.
            # That is the arithmetic protecting itself against subtracting the
            # same sale twice.
            sale = sold_today.get((account.id, row['symbol'], row['exchange']))
            pending_sold = min(_to_int((sale or {}).get('quantity')), row['quantity'])
            pending_sold = max(pending_sold, 0)

            if pending_sold > 0:
                sold_parcels.append(_sold_parcel_row(
                    account, row, meta, sale, pending_sold,
                    account_names, broker_names
                ))

            # What is left is the holding. Everything below works on this
            # figure and never on the broker's raw count.
            deliverable = row['quantity'] - pending_sold
            if deliverable <= 0:
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
                    'is_unsettled': False,
                }
                buckets[key] = bucket

            # True when ANY contributing row is a buy the broker has not yet
            # delivered. Aggregated rows can mix the two - hold a stock and buy
            # more of it today - and the screen has to say so rather than let
            # the settled half speak for both.
            if row.get('is_unsettled'):
                bucket['is_unsettled'] = True

            quantity = deliverable
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

            # A holding parked in the confirm queue is NOT being watched. The
            # monitor only evaluates rows whose status is ACTIVE, so an armed
            # level and an exit mode on a parked row describe an intention, not
            # a thing that will happen. The screen has to say so, or it reads as
            # armed when it is inert.
            exit_status = (
                meta.exit_status if meta is not None and meta.exit_status
                else EQUITY_HOLDING_STATUS_ACTIVE
            )
            awaiting_confirm = exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM
            exit_in_flight = bool(meta is not None and meta.is_exit_in_flight)

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
                'exit_status': exit_status,
                'awaiting_confirm': awaiting_confirm,
                'exit_in_flight': exit_in_flight,
                # Whether a level has already been BREACHED on this account,
                # and whether the monitor is still watching it. Both are the
                # holding's own facts - only it knows a level was hit, and only
                # it knows whether it is being evaluated on the next tick.
                'sl_hit_at': _iso(meta.sl_hit_at) if meta is not None else None,
                'tp_hit_at': _iso(meta.tp_hit_at) if meta is not None else None,
                'is_watched': bool(meta is not None and meta.is_monitorable),
                # Now zero on every row that has one, because a sold parcel
                # is no longer part of the holding - it is listed separately
                # below. Kept so the per-account breakdown can still say so on
                # a PARTIAL sale, where some of the stock really was sold and
                # the rest is genuinely still held.
                'sold_today': pending_sold,
                'est_costs': _money(costs.total),
            })

    # What the ratio SAYS each account should be holding of this stock.
    #
    # The same split Place Order would produce for the quantity actually held:
    # the whole holding divided by the accounts' Order Qty Ratios, rounded the
    # same way. Var Qty is then the gap between the plan and the fact.
    #
    # Computed across every account IN VIEW rather than only those holding the
    # stock, so an account that should be carrying forty shares and carries
    # none appears with a variance of forty rather than not appearing at all.
    # That is the case the column exists to show.
    view_ratios = {
        account.id: ratios.get(account.id, 0.0) for account in accounts_in_view
    }
    for bucket in buckets.values():
        planned = split_quantity_by_ratio(
            bucket['total_quantity'], view_ratios
        ).quantities
        held = {entry['account_id'] for entry in bucket['accounts']}
        for account in accounts_in_view:
            if account.id in held:
                continue
            if _to_int(planned.get(account.id)) <= 0:
                continue
            # Holds none of it, and the ratio says it should. Listed with a
            # quantity of zero: the variance is the whole point.
            bucket['accounts'].append({
                'account_id': account.id,
                'account_name': account_names.get(account.id),
                'broker_name': broker_names.get(account.id),
                'quantity': 0,
                'avg_cost': 0.0,
                'pledged_quantity': 0,
                'stop_loss': None,
                'target': None,
                'exit_mode': None,
                'exit_status': None,
                'awaiting_confirm': False,
                'exit_in_flight': False,
                'sl_hit_at': None,
                'tp_hit_at': None,
                'is_watched': False,
                'sold_today': 0,
                'est_costs': 0.0,
            })
        for entry in bucket['accounts']:
            plan = _to_int(planned.get(entry['account_id']))
            entry['planned_quantity'] = plan
            entry['var_quantity'] = plan - _to_int(entry['quantity'])
            # signed_percent_of, NOT percent_of. percent_of clamps a negative
            # numerator to 0.0, so an account holding MORE than the ratio calls
            # for read as exactly on plan - which is the one reading this
            # column must never give. Caught by the harness before it shipped.
            entry['var_quantity_pct'] = (
                _pct(signed_percent_of(plan - _to_int(entry['quantity']), plan))
                if plan > 0 else None
            )
        bucket['accounts'].sort(
            key=lambda entry: (-_to_int(entry['quantity']), entry['account_id'])
        )

    rows = []
    for bucket in buckets.values():
        quantity = bucket['total_quantity']
        at_cost = bucket['at_cost']

        # The stop loss, target and exit mode shown on the aggregated row come
        # from the largest contributing account. levels_mixed says the
        # contributors do not agree, which the per-account breakdown spells out.
        primary = max(bucket['accounts'], key=lambda entry: entry['quantity']) if bucket['accounts'] else {}
        exit_mode = primary.get('exit_mode') or EQUITY_EXIT_MODE_CONFIRM

        # Counted rather than inferred from the primary account: one account can
        # be parked awaiting a decision while another is still being watched,
        # and a row that mentioned only the largest holder would be lying about
        # the rest.
        waiting = [
            entry for entry in bucket['accounts'] if entry.get('awaiting_confirm')
        ]
        in_flight = [
            entry for entry in bucket['accounts'] if entry.get('exit_in_flight')
        ]
        # Accounts carrying a breach record that is still standing.
        #
        # A breach record is what makes the monitor go quiet on a level. It is
        # set when the level fires and cleared only by approving, declining or
        # saving the levels afresh from Edit. It OUTLIVES the confirm queue: a
        # part exit returns the row to ACTIVE with the smaller quantity and
        # deliberately keeps the record, so the remainder sits with a stop loss
        # that will never fire again while the screen showed it as healthy.
        # That gap is why this count exists - awaiting_confirm alone was too
        # narrow to describe it.
        breached = [
            entry for entry in bucket['accounts']
            if entry.get('sl_hit_at') or entry.get('tp_hit_at')
        ]

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
            'is_unsettled': bucket['is_unsettled'],
            'awaiting_confirm_count': len(waiting),
            'exit_in_flight_count': len(in_flight),
            # The level alert, rolled up.
            #
            # Counted rather than reduced to one flag, because the accounts can
            # legitimately disagree: a stop loss can be breached on the account
            # that holds sixty and untouched on the one that holds forty, and a
            # row that said only "triggered" would be wrong about half of it.
            'sl_hit_count': sum(
                1 for entry in bucket['accounts'] if entry.get('sl_hit_at')
            ),
            'tp_hit_count': sum(
                1 for entry in bucket['accounts'] if entry.get('tp_hit_at')
            ),
            'sl_hit_at': max(
                [entry['sl_hit_at'] for entry in bucket['accounts']
                 if entry.get('sl_hit_at')] or [None]
            ),
            'tp_hit_at': max(
                [entry['tp_hit_at'] for entry in bucket['accounts']
                 if entry.get('tp_hit_at')] or [None]
            ),
            # Accounts carrying a level at all, and accounts the monitor is
            # actually evaluating. A level that is set but not watched is the
            # state the screen has to be able to say out loud.
            'levels_set_count': sum(
                1 for entry in bucket['accounts']
                if entry.get('stop_loss') is not None or entry.get('target') is not None
            ),
            'watched_with_level_count': sum(
                1 for entry in bucket['accounts']
                if entry.get('is_watched')
                and (entry.get('stop_loss') is not None or entry.get('target') is not None)
            ),
            'breached_count': len(breached),
            # Accounts the monitor will actually raise something on. A standing
            # breach record has to come out of this: the row is monitorable by
            # every other measure, and record_breach still refuses to raise it
            # a second time, so counting it as watched is the screen asserting
            # a level is armed when it is not.
            'watched_count': sum(
                1 for entry in bucket['accounts']
                if entry.get('is_watched')
                and not entry.get('awaiting_confirm')
                and not entry.get('exit_in_flight')
                and not entry.get('sl_hit_at')
                and not entry.get('tp_hit_at')
            ),
            'sold_today': sum(
                entry.get('sold_today') or 0 for entry in bucket['accounts']
            ),
        })

    # One query for the whole table, so the Notes button can say whether there
    # is anything behind it. Done here rather than per row: a holdings screen
    # with forty stocks would otherwise ask forty times.
    noted = _notes_present([(row['symbol'], row['exchange']) for row in rows])
    for row in rows:
        row['has_note'] = _note_key(row['symbol'], row['exchange']) in noted

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
        # Sold today, still sitting in the broker's holdings book until
        # settlement. Deliberately NOT part of 'holdings' and NOT counted in
        # the KPIs above: they are no longer a stake, their P&L is realised
        # rather than unrealised, and the cost of exiting them has been paid
        # once already.
        'sold_parcels': _merge_sold_parcels(sold_parcels),
        'sold_realised': _money(sum(
            parcel['realised_pnl'] or 0 for parcel in sold_parcels
        )),
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
    """Settings: watch lists, trade natures, and brokerage charges."""
    _default_watchlist()
    return render_template(
        'equity/settings.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures(),
        watchlists=_all_watchlists(),
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
    # A report, not a form. The upload it was briefly shaped for was removed
    # on 6 September, so the "(read only)" markers went with it - nothing reads
    # this file back any more.
    #
    # The column ORDER stayed: the key, then AlgoMirror's own side of the stock
    # (levels, exit mode, nature, note), then the figures. That reads better
    # than the old order did, and the notes are worth having in the file
    # whatever else changes.
    notes = _notes_by_key(
        [(row['symbol'], row['exchange']) for row in payload['holdings']]
    )
    writer.writerow([
        'Symbol', 'Exchange',
        'Stop Loss', 'Target', 'Exit Mode', 'Trade Nature',
        'Thesis', 'Risk', 'To Watch',
        'Total Qty', 'Stake %', 'Avg Cost',
        'LTP', 'P&L %',
        'Pledged Qty', 'Pledged %',
        'Investment', 'Current Value',
        'Gross P&L', 'Est. Costs', 'Net P&L'
    ])
    for row in payload['holdings']:
        note = notes.get(_note_key(row['symbol'], row['exchange']))
        writer.writerow([
            row['symbol'],
            row['exchange'],
            '' if row['stop_loss'] is None else row['stop_loss'],
            '' if row['target'] is None else row['target'],
            row['exit_mode_tag'],
            row['trade_nature'] or '',
            (note.thesis or '') if note is not None else '',
            (note.risk or '') if note is not None else '',
            (note.to_watch or '') if note is not None else '',
            row['total_quantity'],
            row['stake_pct'],
            row['avg_cost'],
            row['ltp'],
            row['pnl_pct'],
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
        'TOTAL', '', '', '', '', '', '', '', '', '', '', '', '', '', '', '',
        kpi['total_investment'], kpi['current_value'], kpi['gross_pnl'],
        kpi['est_costs'], kpi['net_pnl']
    ])

    filename = f'equity_holdings_{datetime.utcnow().strftime("%Y%m%d_%H%M%S")}.csv'
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


def _no_filters():
    """
    The Order Book and Trade Book carry no filters any more: both screens show
    one day, whole. The builders still take a filter dict, so this is the empty
    one - stated once rather than written out at each call site.
    """
    return {
        'account_id': None, 'symbol': None, 'side': None, 'status': None,
        'order_type': None, 'trade_nature_id': None,
        'date_from': None, 'date_to': None,
    }


def _csv_response(buffer, stem):
    """One CSV download, named for what it holds and when it was taken."""
    filename = '%s_%s.csv' % (
        stem, datetime.utcnow().strftime('%Y%m%d_%H%M%S')
    )
    return Response(
        buffer.getvalue(),
        mimetype='text/csv',
        headers={'Content-Disposition': 'attachment; filename=%s' % filename}
    )


def _csv_money(value):
    """A number for a spreadsheet: the figure itself, or blank."""
    return '' if value is None else value


@equity_bp.route('/api/order-book/export')
@login_required
@heavy_rate_limit()
def api_order_book_export():
    """
    CSV of the Order Book. A pure read: the same builder the screen uses, so
    the file and the screen cannot disagree.

    ONE flat table, not two. The screen shows regular orders and resting GTTs
    separately because they answer different questions; a spreadsheet wants one
    block it can sort and filter, with a column saying which list a row came
    from.
    """
    try:
        payload = _build_order_book(
            _no_filters(), carry_open_gtt=True, include_splits=False,
            merge_broker=True, split_gtt=True
        )
    except Exception as exc:
        current_app.logger.error(f'Equity order book export failed: {exc}')
        return _json_error(f'Failed to export the order book: {exc}', 500)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'List', 'Placed At', 'Symbol', 'Exchange', 'Action', 'Type',
        'Qty', 'Filled', 'Price', 'Trigger', 'Avg Fill', 'Stop Loss', 'Target',
        'Trade Nature', 'Accounts Placed', 'Accounts Selected', 'Status',
        'Status Reason', 'Placed Outside AlgoMirror', 'Order Id',
    ])

    def write(rows, listing):
        for row in rows:
            writer.writerow([
                listing,
                row.get('placed_at') or '',
                row.get('symbol') or '',
                row.get('exchange') or '',
                row.get('side') or '',
                row.get('order_type') or '',
                row.get('total_quantity'),
                row.get('filled_quantity'),
                _csv_money(row.get('price')),
                _csv_money(row.get('trigger_price')),
                _csv_money(row.get('avg_price')),
                _csv_money(row.get('stop_loss')),
                _csv_money(row.get('target')),
                row.get('trade_nature') or 'Unassigned',
                row.get('accounts_placed'),
                row.get('accounts_selected'),
                row.get('status') or '',
                row.get('status_reason') or '',
                'Yes' if row.get('placed_outside') else 'No',
                row.get('order_id'),
            ])

    write(payload.get('orders') or [], 'Regular')
    write(payload.get('gtt_orders') or [], 'GTT')

    totals = payload.get('totals') or {}
    writer.writerow([])
    writer.writerow([
        'TOTALS', '', '', '', '', '', '', '', '', '', '', '', '', '',
        '', '', '', '', '', ''
    ])
    writer.writerow(['Buy Orders', totals.get('buy_orders')])
    writer.writerow(['Sell Orders', totals.get('sell_orders')])
    writer.writerow(['Completed', totals.get('completed_orders')])
    writer.writerow(['Open', totals.get('open_orders')])
    writer.writerow(['Cancelled', totals.get('cancelled_orders')])
    writer.writerow(['GTT Orders', totals.get('gtt_orders')])

    return _csv_response(buffer, 'equity_order_book')


@equity_bp.route('/api/trade-book/export')
@login_required
@heavy_rate_limit()
def api_trade_book_export():
    """
    CSV of the Trade Book. A pure read, running the same builder as the screen.

    Accounts is written as two columns rather than the "2/2" the screen shows:
    a spreadsheet cannot add up a string.
    """
    try:
        payload = _build_trade_book(_no_filters())
    except Exception as exc:
        current_app.logger.error(f'Equity trade book export failed: {exc}')
        return _json_error(f'Failed to export the trade book: {exc}', 500)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow([
        'Executed At', 'Symbol', 'Exchange', 'Side', 'Order Type',
        'Accounts Filled', 'Accounts On Order', 'Account',
        'Qty', 'Execution Price', 'Value', 'Stop Loss', 'Target',
        'Trade Nature', 'Placed Outside AlgoMirror', 'Order Id',
    ])

    for row in payload.get('trades') or []:
        writer.writerow([
            row.get('executed_at') or '',
            row.get('symbol') or '',
            row.get('exchange') or '',
            row.get('side') or '',
            row.get('order_type') or '',
            row.get('accounts_count') or 1,
            row.get('order_accounts') or 1,
            # Named only where the row speaks for one account. A folded row
            # covers several and naming one of them would be wrong.
            (row.get('account_name') or '') if (row.get('accounts_count') or 1) == 1 else '',
            row.get('executed_quantity'),
            _csv_money(row.get('execution_price')),
            _csv_money(row.get('trade_value')),
            _csv_money(row.get('stop_loss')),
            _csv_money(row.get('target')),
            row.get('trade_nature') or 'Unassigned',
            'Yes' if row.get('placed_outside') else 'No',
            row.get('order_id') or '',
        ])

    totals = payload.get('totals') or {}
    writer.writerow([])
    writer.writerow(['Accounts', totals.get('accounts')])
    writer.writerow(['Buy Trades', totals.get('buy_fills')])
    writer.writerow(['Sell Trades', totals.get('sell_fills')])
    writer.writerow(['Quantity', totals.get('quantity')])
    writer.writerow(['Value', totals.get('value')])

    return _csv_response(buffer, 'equity_trade_book')


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
MAX_SEARCH_RESULTS = 60
SEARCH_EXCHANGES = ('NSE', 'BSE')
# Instrument type fragments that mark a derivative or an index rather than a
# tradable equity or ETF. Matched as substrings of the upper cased type.
NON_EQUITY_INSTRUMENT_FRAGMENTS = ('FUT', 'OPT', 'IDX', 'INDEX')
# Values a broker adapter sends in the expiry field of a cash instrument to
# mean "this contract has no expiry". They are placeholders, not real dates:
# OpenAlgo sends "-1" for every NSE and BSE equity, so an emptiness test alone
# would discard the whole cash segment.
EMPTY_EXPIRY_PLACEHOLDERS = frozenset(
    {'', '-1', '0', '0.0', '-1.0', 'NA', 'N/A', 'NAN', 'NONE', 'NULL', '--'}
)

# What a search result actually is: a company share, an exchange traded fund,
# a mutual fund unit, a debt instrument or a rights entitlement. NSE and BSE
# send every one of these with the same instrument type of "EQ", so the broker
# cannot answer the question and the two clues below have to.
KIND_STOCK = 'STOCK'
KIND_ETF = 'ETF'
KIND_MF = 'MF'
KIND_BOND = 'BOND'
KIND_RIGHTS = 'RIGHTS'

INSTRUMENT_KIND_LABELS = {
    KIND_STOCK: 'Share',
    KIND_ETF: 'ETF',
    KIND_MF: 'Fund',
    KIND_BOND: 'Bond',
    KIND_RIGHTS: 'Rights',
}

# Order the kinds appear in when two results match a search equally well.
INSTRUMENT_KIND_ORDER = {
    KIND_STOCK: 0,
    KIND_ETF: 1,
    KIND_MF: 2,
    KIND_RIGHTS: 3,
    KIND_BOND: 4,
}

# Clue one: the NSE series. OpenAlgo writes it onto the symbol after a dash
# whenever it is not the ordinary EQ series, so HDFC2638RG-MF is a mutual fund
# unit and IRFC-N1 is a debenture. A series absent from this map and not
# matching the debt pattern below leaves the row to be judged on its name.
SERIES_KINDS = {
    'MF': KIND_MF,      # mutual fund units listed on the exchange
    'SF': KIND_MF,      # scheme series carried by the same board
    'SG': KIND_BOND,    # state government securities
    'GS': KIND_BOND,    # government securities
    'GB': KIND_BOND,    # government bonds
    'TB': KIND_BOND,    # treasury bills
    'BE': KIND_STOCK,   # trade to trade
    'BZ': KIND_STOCK,   # trade to trade under surveillance
    'BT': KIND_STOCK,   # trade to trade, BSE
    'B': KIND_STOCK,    # BSE group B
    'SM': KIND_STOCK,   # SME board
    'ST': KIND_STOCK,   # SME board, trade to trade
    'RE': KIND_RIGHTS,
    'RR': KIND_RIGHTS,
    'RT': KIND_RIGHTS,
}

# Debentures and bonds run through the whole N and Y series ranges, which are
# far too many to list one by one.
DEBT_SERIES_PATTERN = re.compile(r'^[NY][0-9A-Z]$')

# Clue two: the security name. A fund house product is named for its scheme,
# never for a company, so any of these inside the name or the symbol settles it.
ETF_NAME_FRAGMENTS = ('ETF', 'BEES', 'EXCHANGE TRADED', 'MUTUAL FUND')

# A fund house writes its products as "<house> - <scheme code>": HDFCAMC -
# HDFCNIMEG, MIRAEAMC - MAFANG, BFAM - BANK10BETF. The share of the fund house
# itself is named as a company, "HDFC AMC LIMITED", and so does not match.
FUND_HOUSE_PATTERN = re.compile(r'^([A-Z0-9&.]{3,})\s*-\s*([A-Z0-9]{2,})$')
FUND_HOUSE_SUFFIXES = ('AMC', 'MF', 'FUND', 'FAM', 'BNP')

# Market depth. The PRD asks for five levels of bid and offer.
DEPTH_LEVELS = 5

# Watch list ceiling. The shared price feed has its own subscription limit and
# the holdings screens need room inside it, so the watch list is bounded well
# below it rather than being allowed to consume the whole budget.
MAX_WATCHLIST_ITEMS = 100

# Named watch lists per user. Generous, but bounded: the selector on the Watch
# List screen stops being a selector somewhere past this.
MAX_WATCHLISTS = 20

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

# Bounds on the order timeout, matched to the order engine's own clamp.
MIN_ORDER_TIMEOUT_SECONDS = 10
MAX_ORDER_TIMEOUT_SECONDS = 180

# Still owed: not yet bought back, whether or not a buy-back is in the air.
# Built from the two vocabularies in models.py rather than written out, so a
# status added there cannot quietly fall out of this one.
EQUITY_SHORT_STATUSES_UNSETTLED = (
    tuple(EQUITY_SHORT_STATUSES_CLAIMABLE) + tuple(EQUITY_SHORT_STATUSES_IN_FLIGHT)
)

# Bounds on the intraday short times.
#
# The square-off must land BEFORE OpenAlgo's own MIS square-off, which runs at
# 15:15 on NSE and BSE. Behind it, ours would never get to run and could never
# be proven to work. The floor is the opening bell: a time before the market
# opens is not a setting, it is a typo.
MIN_INTRADAY_MINUTE = 9 * 60 + 15
MAX_SQUAREOFF_MINUTE = 15 * 60 + 14

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
        'accounts_label': f'{placed}/{total}',
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
        # The levels set on the order this fill came from. A fill is the moment
        # a decision became a position, so what was meant to protect it belongs
        # on the row: the Trade Book was the one screen where you could see the
        # purchase and nothing of the stop loss behind it.
        'stop_loss': _money(order.stop_loss) if order.stop_loss is not None else None,
        'target': _money(order.target) if order.target is not None else None,
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


def _nature_for_new_holding(account_id, symbol, exchange, quantity):
    """
    The trade nature to give a holding row the moment it first appears.

    A holding inherits a tag only when AlgoMirror's own tagged buys account for
    every share of it. Buy a hundred shares as Swing and hold a hundred and one
    and the odd share came from somewhere this application never saw, so the row
    stays Unassigned rather than claim a purpose for shares it cannot vouch for.
    Two different natures behind the same holding leave it Unassigned for the
    same reason: there is no single honest answer to give.

    Matched on this account actually having taken part, so a stock bought for
    one member does not tag another member's holding of the same stock. The
    admin can always set the tag by hand afterwards; this only decides what the
    row starts out as.
    """
    quantity = _to_int(quantity)
    if quantity <= 0:
        return None

    try:
        rows = db.session.query(
            EquityOrder.trade_nature_id,
            EquityOrderSplit.filled_quantity,
            EquityOrderSplit.quantity,
            EquityOrderSplit.fill_status,
        ).join(
            EquityOrderSplit, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == current_user.id,
            EquityOrder.symbol == symbol,
            EquityOrder.exchange == exchange,
            EquityOrder.side == EQUITY_SIDE_BUY,
            EquityOrder.trade_nature_id.isnot(None),
            EquityOrderSplit.account_id == account_id,
            EquityOrderSplit.fill_status.in_(
                (EQUITY_ORDER_STATUS_COMPLETED, EQUITY_ORDER_STATUS_PARTIAL)
            ),
        ).all()
    except Exception as exc:
        current_app.logger.debug(
            f'Could not read a trade nature for {symbol}: {exc}'
        )
        return None

    covered = 0
    natures = set()
    for nature_id, filled, ordered, fill_status in rows:
        # filled_quantity is the truth. A split marked COMPLETED before the
        # reconciler wrote a fill back is trusted for its ordered quantity,
        # which is what completed means; anything else contributes nothing.
        shares = _to_int(filled)
        if shares <= 0 and fill_status == EQUITY_ORDER_STATUS_COMPLETED:
            shares = _to_int(ordered)
        if shares <= 0:
            continue
        covered += shares
        natures.add(nature_id)

    if not natures:
        current_app.logger.debug(
            f'{symbol} on account {account_id} starts Unassigned: '
            f'no tagged buy of this stock on this account.'
        )
        return None

    if len(natures) > 1:
        current_app.logger.debug(
            f'{symbol} on account {account_id} starts Unassigned: '
            f'{len(natures)} different trade natures were used to buy it.'
        )
        return None

    if covered < quantity:
        current_app.logger.debug(
            f'{symbol} on account {account_id} starts Unassigned: tagged buys '
            f'cover {covered} of {quantity} shares, the rest came from '
            f'elsewhere.'
        )
        return None

    return natures.pop()


def _levels_for_new_holding(account_id, symbol, exchange, quantity, since=None):
    """
    The stop loss and target to give a holding row the moment it first appears.

    Exactly the rule the trade nature already uses, and for the same reason: a
    level is inherited only when AlgoMirror's own buys account for every share
    of the holding and only ONE pair of levels sits behind them. Two different
    stop losses behind the same holding have no single honest answer, and a
    holding part of which came from somewhere this application never saw must
    not have a level attached to shares it cannot vouch for.

    Returns (stop_loss, target), either of which may be None.

    Why this exists. Until 2026-09-04 the stop loss and target set on a buy
    order were written to the order and stopped there: the holding appeared at
    settlement with both blank, and the monitor - which works only on holdings -
    had nothing to watch. LT was bought on 2 September with a stop loss of 3900
    and a target of 4040, and the holding that appeared two days later carried
    neither.

    Only ever called when a row is first created or revived. A later broker
    read must never rewrite a level the admin has since set for themselves.

    ``since`` limits the buys considered to those placed after a moment, and is
    what makes this safe to use on a REVIVED row. Without it the search runs
    over every buy of that stock ever placed on that account, and a position
    bought again with no levels at all would come back wearing the stop loss
    that belonged to shares sold months ago - the one outcome revival exists to
    prevent.
    """
    quantity = _to_int(quantity)
    if quantity <= 0:
        return (None, None)

    try:
        query = db.session.query(
            EquityOrder.stop_loss,
            EquityOrder.target,
            EquityOrderSplit.filled_quantity,
            EquityOrderSplit.quantity,
            EquityOrderSplit.fill_status,
        ).join(
            EquityOrderSplit, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == current_user.id,
            EquityOrder.symbol == symbol,
            EquityOrder.exchange == exchange,
            EquityOrder.side == EQUITY_SIDE_BUY,
            EquityOrderSplit.account_id == account_id,
            EquityOrderSplit.fill_status.in_(
                (EQUITY_ORDER_STATUS_COMPLETED, EQUITY_ORDER_STATUS_PARTIAL)
            ),
        )
        if since is not None:
            query = query.filter(EquityOrder.placed_at >= since)
        rows = query.all()
    except Exception as exc:
        current_app.logger.debug(
            f'Could not read exit levels for {symbol}: {exc}'
        )
        return (None, None)

    covered = 0
    levels = set()
    for stop_loss, target, filled, ordered, fill_status in rows:
        shares = _to_int(filled)
        if shares <= 0 and fill_status == EQUITY_ORDER_STATUS_COMPLETED:
            shares = _to_int(ordered)
        if shares <= 0:
            continue
        covered += shares
        if stop_loss is not None or target is not None:
            levels.add((
                _to_float(stop_loss) or None,
                _to_float(target) or None,
            ))

    if not levels:
        return (None, None)

    if len(levels) > 1:
        current_app.logger.debug(
            f'{symbol} on account {account_id} starts with no levels: '
            f'{len(levels)} different stop loss / target pairs were used to '
            f'buy it.'
        )
        return (None, None)

    if covered < quantity:
        current_app.logger.debug(
            f'{symbol} on account {account_id} starts with no levels: our own '
            f'buys cover {covered} of {quantity} shares, the rest came from '
            f'elsewhere.'
        )
        return (None, None)

    return levels.pop()


def _algomirror_net_position(account_id, symbol, exchange):
    """
    Shares this application actually put into one account, net of what it took
    out: filled buys minus filled sells for that account and stock.

    Net, so it needs no time window. A running total of every order AlgoMirror
    ever placed for a stock is a complete answer at any moment, where "orders in
    the last N days" would quietly go wrong the day N was too small.

    Returns 0 when nothing matches, which is the honest answer for a stock this
    application has never traded.
    """
    try:
        rows = db.session.query(
            EquityOrder.side,
            EquityOrderSplit.filled_quantity,
            EquityOrderSplit.quantity,
            EquityOrderSplit.fill_status,
        ).join(
            EquityOrderSplit, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == current_user.id,
            EquityOrder.symbol == symbol,
            EquityOrder.exchange == exchange,
            EquityOrderSplit.account_id == account_id,
            EquityOrderSplit.fill_status.in_(
                (EQUITY_ORDER_STATUS_COMPLETED, EQUITY_ORDER_STATUS_PARTIAL)
            ),
        ).all()
    except Exception as exc:
        current_app.logger.debug(
            f'Could not total AlgoMirror orders for {symbol}: {exc}'
        )
        return None

    net = 0
    for side, filled, ordered, fill_status in rows:
        shares = _to_int(filled)
        if shares <= 0 and fill_status == EQUITY_ORDER_STATUS_COMPLETED:
            shares = _to_int(ordered)
        if shares <= 0:
            continue
        net += -shares if side == EQUITY_SIDE_SELL else shares

    return net


def _algomirror_sold_today():
    """
    Shares AlgoMirror sold TODAY, keyed by (account_id, symbol, exchange).

    This does not correct anything and it is not netted against anything. The
    broker owns what is held - D10a settles that, and subtracting our own sales
    from the broker's figure is the mistake that rule exists to prevent. This is
    only so the Holdings screen can SAY what it knows.

    Why it matters. Every sell is sized against the broker's own count, which is
    right. But a broker whose holdings lag - OpenAlgo's sandbox settles on T+1,
    and a real broker can lag for seconds - will keep reporting shares that have
    already gone. The sell would then be permitted a second time. Naming what
    was sold today costs nothing and changes no number, which is the whole
    reason it is safe to do.

    One query for the whole screen rather than one per row. Returns {} on any
    failure: this decorates a screen and must never be the reason it breaks.

    Returns {(account_id, SYMBOL, EXCHANGE): {quantity, proceeds, priced,
    last_at}}. proceeds over priced gives the average sale price, which is what
    a realised P&L is worked out from; last_at is when the most recent of those
    sells went out.
    """
    since = _today_start()

    try:
        rows = db.session.query(
            EquityOrderSplit.account_id,
            EquityOrder.symbol,
            EquityOrder.exchange,
            EquityOrderSplit.filled_quantity,
            EquityOrderSplit.quantity,
            EquityOrderSplit.avg_fill_price,
            EquityOrderSplit.fill_status,
            EquityOrderSplit.placed_at,
        ).join(
            EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == current_user.id,
            EquityOrder.side == EQUITY_SIDE_SELL,
            EquityOrder.created_at >= since,
            EquityOrderSplit.fill_status.in_(
                (EQUITY_ORDER_STATUS_COMPLETED, EQUITY_ORDER_STATUS_PARTIAL)
            ),
        ).all()
    except Exception as exc:
        current_app.logger.debug(f'Could not total today\'s sells: {exc}')
        return {}

    sold = {}
    for (account_id, symbol, exchange, filled, ordered, fill_price,
         fill_status, placed_at) in rows:
        shares = _to_int(filled)
        if shares <= 0 and fill_status == EQUITY_ORDER_STATUS_COMPLETED:
            shares = _to_int(ordered)
        if shares <= 0:
            continue
        key = (account_id, symbol, exchange)
        entry = sold.setdefault(key, {
            'quantity': 0, 'proceeds': 0.0, 'priced': 0, 'last_at': None
        })
        entry['quantity'] += shares
        # Proceeds accumulate only over shares that came back WITH a price. A
        # split marked COMPLETED before the reconciler wrote its fill price
        # back would otherwise drag the average towards zero and turn a
        # realised profit into a loss on the screen.
        price = _to_float(fill_price)
        if price > 0:
            entry['proceeds'] += price * shares
            entry['priced'] += shares
        if placed_at is not None and (
            entry['last_at'] is None or placed_at > entry['last_at']
        ):
            entry['last_at'] = placed_at
    return sold


def _has_order_in_flight(account_id, symbol, exchange):
    """
    True when an AlgoMirror order for this stock is still working at the broker.

    While one is open the broker's share count and this application's record of
    it are legitimately out of step - the fill has happened and has not been
    written back yet - so a difference measured now would be noise. Nothing is
    compared and no baseline is moved until the books agree again.
    """
    try:
        return db.session.query(
            db.session.query(EquityOrderSplit.id).join(
                EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
            ).filter(
                EquityOrder.user_id == current_user.id,
                EquityOrder.symbol == symbol,
                EquityOrder.exchange == exchange,
                EquityOrderSplit.account_id == account_id,
                EquityOrderSplit.fill_status.in_(
                    (EQUITY_ORDER_STATUS_PENDING, EQUITY_ORDER_STATUS_PARTIAL)
                ),
            ).exists()
        ).scalar()
    except Exception as exc:
        current_app.logger.debug(
            f'Could not check for orders in flight on {symbol}: {exc}'
        )
        # Unknown means do nothing. A notice raised on a guess is worse than a
        # notice raised one refresh later.
        return True


def _raise_holding_notice(holding, kind, before, after, delta, message,
                          armed=None):
    """
    Record that a holding moved for a reason AlgoMirror did not cause.

    Written but never committed here: the caller is already inside the holdings
    sync and commits everything together, so a notice can never be stored for a
    quantity change that was then rolled back.

    A failure to write a notice must not stop the sync. The share counts are
    the important part; the notice is the courtesy.
    """
    # Passed in when the caller has already cleared the levels, so a closed
    # holding still records that it HAD one - which is the whole reason that
    # notice is worth reading.
    if armed is None:
        armed = holding.stop_loss is not None or holding.target is not None
    try:
        db.session.add(EquityHoldingNotice(
            user_id=current_user.id,
            account_id=holding.account_id,
            symbol=holding.symbol,
            exchange=holding.exchange,
            kind=kind,
            quantity_before=before,
            quantity_after=after,
            quantity_delta=delta,
            had_armed_level=armed,
            message=message[:255],
        ))
    except Exception as exc:
        current_app.logger.error(
            f'Could not record a holding notice for {holding.symbol}: {exc}'
        )
        return

    current_app.logger.warning(
        '[EQUITY_HOLDING] %s on account %s: %s',
        kind, holding.account_id, message
    )


def _retire_holding(holding):
    """
    Close a holding down to nothing.

    Levels and breach markers are cleared because they described a position
    that no longer exists. Leaving a stop loss behind is how an old level ends
    up governing shares bought months later, and a stale breach marker is how a
    genuine alert on a new position gets swallowed.

    exit_mode and trade_nature_id survive on purpose. They are preferences
    rather than facts about the shares, and they make a sensible default if the
    same stock is bought again.
    """
    holding.quantity = 0
    holding.pledged_quantity = 0
    holding.stop_loss = None
    holding.target = None
    holding.sl_hit_at = None
    holding.sl_hit_price = None
    holding.tp_hit_at = None
    holding.tp_hit_price = None


def _revive_holding(holding, quantity):
    """
    A holding that had gone to zero has shares again, so it is a NEW position.

    Nothing from the old one is allowed to carry over. The stop loss you set on
    shares you have since sold has no claim on shares you bought this morning,
    and a breach marker left over from the old position would swallow a genuine
    alert on the new one.

    The trade nature is inherited afresh, under the same coverage rule that
    governs a brand new row. When the new purchase does not explain the whole
    holding the previous tag is left alone: it is a reasonable default, and the
    alternative is throwing away a decision the admin already made.

    The levels are inherited afresh too, under that same rule. The old ones are
    cleared first and unconditionally: if the new buy carried no levels, or
    carried two different pairs, or does not account for every share, the row
    comes back BARE rather than wearing a stop loss that belonged to shares
    already sold.
    """
    holding.stop_loss = None
    holding.target = None
    holding.sl_hit_at = None
    holding.sl_hit_price = None
    holding.tp_hit_at = None
    holding.tp_hit_price = None

    nature = _nature_for_new_holding(
        holding.account_id, holding.symbol, holding.exchange, quantity
    )
    if nature is not None:
        holding.trade_nature_id = nature

    # Only buys since the position last closed. Without that limit a stock
    # bought again with no levels at all would come back wearing the stop loss
    # that belonged to the shares already sold. When there is no record of the
    # close, a few days is used instead: settlement is T+1, so anything older
    # than that cannot be part of the purchase that just revived this row.
    since = holding.exit_completed_at or (
        datetime.utcnow() - timedelta(days=UNSETTLED_LOOKBACK_DAYS)
    )
    holding.stop_loss, holding.target = _levels_for_new_holding(
        holding.account_id, holding.symbol, holding.exchange, quantity,
        since=since
    )


def _reconcile_external_quantity(holding, quantity, previous_quantity,
                                 is_new_row, pending_sold=0):
    """
    Compare the broker's share count with what AlgoMirror's own orders explain,
    and say so out loud when the difference moves.

    external = the broker's quantity minus AlgoMirror's net filled position.
    For most holdings it is a fixed number and completely uninteresting: shares
    bought elsewhere, or held before this application existed. What matters is
    when it CHANGES, because a change means shares moved at the broker with no
    order from here - an emergency sell from the broker's own app being the
    case this was written for.

    The first measurement of a row is stored silently. History is not the
    admin's fault and does not need announcing.

    Returns True when the row was modified.
    """
    # A sell of our own is in flight, so the count is expected to move.
    if holding.is_exit_in_flight:
        return False

    # An order of ours is still working. The fill may already have happened at
    # the broker without having been written back here yet, so any difference
    # measured now is noise. Wait until the books agree.
    if _has_order_in_flight(holding.account_id, holding.symbol, holding.exchange):
        return False

    net = _algomirror_net_position(
        holding.account_id, holding.symbol, holding.exchange
    )
    if net is None:
        return False

    # Shares AlgoMirror sold today that the broker has not yet removed from its
    # holdings book. They are counted in `quantity` and they are NOT counted in
    # `net`, because net has already taken the sale off. Left uncorrected the
    # subtraction reports them as shares that appeared from nowhere - which is
    # exactly what happened on 4 September 2026, forty seconds after AlgoMirror
    # itself placed the sell.
    #
    # Capped at what the broker still reports, so a broker that HAS already
    # removed them - which a real one does on fill - has nothing subtracted and
    # this line does nothing. That cap is what stops the same sale being taken
    # off twice.
    external_now = quantity - net - max(min(_to_int(pending_sold), quantity), 0)
    stored = holding.external_quantity

    if stored is None:
        holding.external_quantity = external_now
        # A row appearing for the first time with shares this application never
        # bought is worth one line, because it has no trade nature, no stop
        # loss and no target, and nothing else on the screen says so.
        if is_new_row and external_now > 0 and quantity > 0:
            _raise_holding_notice(
                holding, EQUITY_NOTICE_HOLDING_NEW, 0, quantity, quantity,
                f'{holding.symbol} appeared with {quantity:,} shares. AlgoMirror '
                f'did not buy them, so it has no trade nature, stop loss or '
                f'target.'
            )
        return True

    if external_now == stored:
        return False

    delta = external_now - stored
    holding.external_quantity = external_now

    armed = holding.stop_loss is not None or holding.target is not None
    still_armed = ' The stop loss or target is still armed on what remains.' if (
        armed and quantity > 0
    ) else ''

    if delta < 0:
        _raise_holding_notice(
            holding, EQUITY_NOTICE_SHARES_LEFT,
            previous_quantity, quantity, delta,
            f'{holding.symbol} went from {previous_quantity:,} to {quantity:,} '
            f'shares. {abs(delta):,} left this account without an order from '
            f'AlgoMirror.{still_armed}'
        )
    else:
        _raise_holding_notice(
            holding, EQUITY_NOTICE_SHARES_ARRIVED,
            previous_quantity, quantity, delta,
            f'{holding.symbol} went from {previous_quantity:,} to {quantity:,} '
            f'shares. {delta:,} arrived without an order from AlgoMirror.'
        )

    return True


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

    # Read once for the whole sync. The drift detector below has to know what
    # AlgoMirror itself sold today before it calls a difference unexplained -
    # without it, a broker that has not yet removed sold shares looks exactly
    # like shares arriving from nowhere. That is not hypothetical: on
    # 4 September 2026 a sale of 67 LT was announced back as 67 shares arriving
    # without an order, forty seconds after AlgoMirror placed the sell itself.
    sold_today = _algomirror_sold_today()

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
            is_new_row = holding is None
            if holding is None:
                # Carried from the buy that created it, on the same rule as the
                # trade nature below: one pair of levels behind every share, or
                # none at all. Before this, the stop loss set on the order was
                # written to the order and stopped there - the holding appeared
                # at settlement with both boxes empty and the monitor had
                # nothing to act on.
                inherited_sl, inherited_tp = _levels_for_new_holding(
                    account.id,
                    broker_row['symbol'],
                    broker_row['exchange'],
                    broker_row['quantity'],
                )
                holding = EquityHolding(
                    user_id=current_user.id,
                    account_id=account.id,
                    symbol=broker_row['symbol'],
                    exchange=broker_row['exchange'],
                    quantity=0,
                    stop_loss=inherited_sl,
                    target=inherited_tp,
                    exit_mode=default_exit_mode,
                    exit_status=EQUITY_HOLDING_STATUS_ACTIVE,
                    # Carried from the order that bought it. The admin already
                    # said why they were buying at the point where the decision
                    # was made; asking again once the shares settle is asking
                    # the same question twice. Only ever set here, when the row
                    # is first created - a later broker read must never rewrite
                    # a tag the admin has since chosen for themselves.
                    trade_nature_id=_nature_for_new_holding(
                        account.id,
                        broker_row['symbol'],
                        broker_row['exchange'],
                        broker_row['quantity'],
                    ),
                )
                db.session.add(holding)
                tracked[key] = holding
                changed = True

            # The broker's figure, taken as given. A real broker reduces its
            # holdings the moment a delivery sale fills - sell one of eighty and
            # it reports seventy-nine straight away - so netting anything off
            # here would subtract the same sale twice. OpenAlgo's sandbox
            # settles on T+1 instead and lags for the rest of the day; that is
            # the simulator differing from the market, and the fix for it is not
            # to make this application disagree with real brokers.
            quantity = max(_to_int(broker_row['quantity']), 0)
            previous_quantity = _to_int(holding.quantity)

            # Frozen while a sell of ours is at the broker, and this is load
            # bearing rather than tidy. A real broker reduces its holdings the
            # moment the sale fills, so a screen refresh landing mid-flight
            # would write the POST-sale figure here - and the reconciler, which
            # closes the claim by taking the filled quantity off this number,
            # would then subtract the same sale a second time. Sell 60 of 100,
            # refresh while it works, and the row settles at zero with 40 shares
            # still owned. The whole exit lives on this number not moving.
            #
            # The zero-out loop below already refuses in-flight rows; this loop
            # did not, and the invariant is only worth anything if both hold.
            if holding.is_exit_in_flight:
                continue

            # The broker is now reporting these shares, so the position has
            # settled into a holding and every ordinary rule applies to it from
            # here - including the retirement sweep below, which it was exempt
            # from while the broker could not yet see it.
            if not holding.is_settled:
                holding.is_settled = True
                changed = True
                current_app.logger.info(
                    f'[EQUITY_HOLDING] {holding.symbol} on account '
                    f'{account.id} has settled: {quantity} shares now reported '
                    f'by the broker.'
                )

            if previous_quantity != quantity:
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

            # A row that had gone to zero and now has shares is a new position,
            # not the old one waking up. Nothing from the old one carries over.
            if not is_new_row and previous_quantity == 0 and quantity > 0:
                _revive_holding(holding, quantity)
                changed = True

            sale = sold_today.get(
                (account.id, broker_row['symbol'], broker_row['exchange'])
            )
            pending_sold = max(
                min(_to_int((sale or {}).get('quantity')), quantity), 0
            )

            if _reconcile_external_quantity(
                holding, quantity, previous_quantity, is_new_row, pending_sold
            ):
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
            if not holding.is_settled:
                # Bought today, not delivered yet. The broker's holdings book
                # will not list it until settlement, so its absence here says
                # nothing at all - retiring it would clear a stop loss that has
                # only just been armed. The reconciler keeps this row's
                # quantity in step from our own fills until the broker takes
                # over, and the row settles itself in the loop above the first
                # time the broker does report it.
                continue
            if _to_int(holding.quantity) != 0:
                # The broker no longer reports these shares, so the position is
                # closed. Retiring it clears the levels and the breach markers,
                # which is what stops an old stop loss governing a stock bought
                # again months later.
                was = _to_int(holding.quantity)
                had_levels = (
                    holding.stop_loss is not None or holding.target is not None
                )
                _retire_holding(holding)
                changed = True

                if had_levels:
                    _raise_holding_notice(
                        holding, EQUITY_NOTICE_HOLDING_CLOSED, was, 0, -was,
                        f'{holding.symbol} is no longer held. Its stop loss and '
                        f'target have been cleared.',
                        armed=True
                    )

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
        # False while the shares are bought but not yet delivered. The row is
        # watched exactly like any other - that is the point of it - but it is
        # not something the broker will confirm until tomorrow, so the screens
        # say so rather than letting it pass as a settled holding.
        'is_settled': bool(holding.is_settled),
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

    if not _expiry_is_absent(entry.get('expiry')):
        return False
    if _to_float(entry.get('strike')) > 0:
        return False
    return True


def _expiry_is_absent(value):
    """
    True when an expiry field carries no real expiry date.

    A cash instrument has no expiry, but adapters express that in different
    ways: an empty string, a null, or a placeholder such as "-1". Only a value
    outside the placeholder set counts as a genuine expiry and therefore as
    evidence that the row is a derivative.
    """
    if value is None:
        return True
    text = str(value).strip().upper()
    return text in EMPTY_EXPIRY_PLACEHOLDERS


def _instrument_kind(symbol, name):
    """
    Decide whether one cash segment result is a share, an ETF, a fund unit,
    a debt instrument or a rights entitlement.

    The broker cannot be asked. NSE and BSE both label every cash line "EQ",
    so HDFC BANK LTD and HDFC GOLD ETF arrive indistinguishable. Two other
    things do carry the answer, and they are read in that order because the
    first is exact and the second is a reading of prose:

      1. The NSE series, which OpenAlgo writes onto the symbol after a dash
         whenever it is not the plain EQ series. HDFC2638RG-MF is a mutual
         fund unit; IRFC-N1 is a debenture; ELECTCAST-BE is an ordinary share
         moved to trade to trade.
      2. The security name. A fund house names its products for the scheme,
         either in words ("HDFC GOLD ETF", "CPSE ETF") or as house and code
         ("HDFCAMC - HDFCNIMEG"). A company is named as a company.

    Anything left over is a share, which is the safe default: it is the kind
    the module is built to trade, so a misread here shows the row rather than
    hiding it.
    """
    symbol = str(symbol or '').strip().upper()
    text = str(name or '').strip().upper()

    if '-' in symbol:
        series = symbol.rsplit('-', 1)[1]
        mapped = SERIES_KINDS.get(series)
        if mapped:
            return mapped
        if DEBT_SERIES_PATTERN.match(series):
            return KIND_BOND

    if any(fragment in text for fragment in ETF_NAME_FRAGMENTS):
        return KIND_ETF
    if 'ETF' in symbol or 'BEES' in symbol:
        return KIND_ETF
    if _is_fund_house_name(text):
        return KIND_ETF
    return KIND_STOCK


def _is_fund_house_name(text):
    """True when a security name reads as "<fund house> - <scheme code>"."""
    if not text:
        return False
    compact = ' '.join(text.split())
    match = FUND_HOUSE_PATTERN.match(compact)
    if not match:
        # A few rows omit the dash: "HDFCAMC HDFCPSUBK".
        parts = compact.split()
        if len(parts) != 2:
            return False
        house = parts[0]
    else:
        house = match.group(1)
    return house.endswith(FUND_HOUSE_SUFFIXES)


def _search_sort_key(result, query):
    """
    Order search results the way a person reads them.

    An exact match first, then symbols that begin with what was typed, then
    names that begin with it, then anything that merely contains it. Shares
    come before funds inside each of those bands, because this module trades
    shares and a share is what the typist usually meant. Typing HDFC therefore
    reaches HDFCBANK before it reaches the two dozen HDFC index products.
    """
    symbol = result.get('symbol') or ''
    name = (result.get('name') or '').upper()
    wanted = (query or '').strip().upper()

    if not wanted:
        band = 5
    elif symbol == wanted:
        band = 0
    elif symbol.startswith(wanted):
        band = 1
    elif name.startswith(wanted):
        band = 2
    elif wanted in symbol:
        band = 3
    else:
        band = 4

    kind_rank = INSTRUMENT_KIND_ORDER.get(result.get('kind'), 9)
    return (band, kind_rank, len(symbol), symbol)


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


def _normalise_search_results(response, query=''):
    """
    Extract the tradable cash instruments from an OpenAlgo search response.

    Every surviving row is labelled with its kind, and the whole set is ranked
    before it is capped. Ranking first matters: capping first would let two
    dozen index products crowd out the share the typist was reaching for.
    """
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
        name = _first_text(entry, _SEARCH_NAME_KEYS)
        kind = _instrument_kind(symbol, name)
        results.append({
            'symbol': symbol,
            'exchange': exchange,
            'name': name,
            'token': str(entry.get('token') or '').strip(),
            'instrument_type': str(_first_text(entry, _INSTRUMENT_TYPE_KEYS) or '').upper(),
            'kind': kind,
            'kind_label': INSTRUMENT_KIND_LABELS.get(kind, 'Share'),
            'lot_size': _to_int(entry.get('lotsize') or entry.get('lot_size'), 1) or 1,
            'tick_size': _to_float(entry.get('ticksize') or entry.get('tick_size')),
        })

    results.sort(key=lambda row: _search_sort_key(row, query))
    return results[:MAX_SEARCH_RESULTS]


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

def _all_watchlists():
    """Every named watch list this user keeps, in display order."""
    return EquityWatchlist.query.filter_by(
        user_id=current_user.id
    ).order_by(EquityWatchlist.sort_order, EquityWatchlist.id).all()


def _default_watchlist():
    """
    The list a stock lands in when none is named, and the one the screen opens
    on.

    Self healing on purpose. A user with lists but none marked default gets the
    first one promoted, and a user with no lists at all gets one created. The
    Watch List screen must never be unusable because a flag went missing.
    """
    lists = _all_watchlists()

    if not lists:
        created = EquityWatchlist(
            user_id=current_user.id,
            name='Core Watchlist',
            is_default=True,
            sort_order=0,
        )
        db.session.add(created)
        db.session.commit()
        return created

    for entry in lists:
        if entry.is_default:
            return entry

    lists[0].is_default = True
    db.session.commit()
    return lists[0]


def _owned_watchlist(watchlist_id):
    """One of this user's lists, refusing anything that is not theirs."""
    entry = EquityWatchlist.query.filter_by(
        id=watchlist_id, user_id=current_user.id
    ).first()
    if entry is None:
        raise _BadRequest('Watch list not found')
    return entry


def _resolve_watchlist(data, field='watchlist_id'):
    """
    The list a request is talking about: the one it names, or the default.

    Falling back to the default is what keeps every existing caller working,
    including a bookmarked Watch List screen that predates named lists.
    """
    raw = (data or {}).get(field)
    if raw in (None, '', 'null'):
        return _default_watchlist()
    try:
        watchlist_id = int(raw)
    except (TypeError, ValueError):
        raise _BadRequest('Invalid watch list id')
    return _owned_watchlist(watchlist_id)


def _watchlist_item_counts():
    """How many stocks sit in each of this user's lists."""
    rows = db.session.query(
        EquityWatchlistItem.watchlist_id,
        db.func.count(EquityWatchlistItem.id)
    ).filter(
        EquityWatchlistItem.user_id == current_user.id
    ).group_by(EquityWatchlistItem.watchlist_id).all()
    return {watchlist_id: count for watchlist_id, count in rows}


def _build_watchlists_payload():
    """Settings: every watch list, with how many stocks it holds."""
    counts = _watchlist_item_counts()
    return {
        'watchlists': [
            {
                'id': entry.id,
                'name': entry.name,
                'is_default': entry.is_default is True,
                'sort_order': _to_int(entry.sort_order),
                'item_count': _to_int(counts.get(entry.id)),
                'created_at': _iso(entry.created_at),
                'updated_at': _iso(entry.updated_at),
            }
            for entry in _all_watchlists()
        ],
        'max_watchlists': MAX_WATCHLISTS,
        'generated_at': _iso(datetime.utcnow()),
    }


# Asked for every list at once, rather than any one of them. A string, so it
# can never collide with a real list id however the ids are generated.
WATCHLIST_ALL = 'all'


def _query_watchlist_id():
    """
    The list named in the query string.

    None for the default, WATCHLIST_ALL for every list at once, otherwise the
    id of a list this user actually owns.
    """
    raw = request.args.get('watchlist_id')
    if raw in (None, '', 'null'):
        return None
    if str(raw).strip().lower() == WATCHLIST_ALL:
        return WATCHLIST_ALL
    try:
        return _owned_watchlist(int(raw)).id
    except (TypeError, ValueError):
        raise _BadRequest('Invalid watch list id')


def _held_symbol_keys():
    """
    Every (SYMBOL, EXCHANGE) this user currently HOLDS.

    One stock, one home. From 6 September a stock in Holdings does not also sit
    on a Watch List: the owner's rule, and it ends a real duplication - the same
    stock could carry a watch list target and a holding target that disagreed,
    with nothing on either screen saying the other existed.

    Derived, never recorded. A holding row with shares in it means held; the
    same row at zero means sold. So nothing has to be kept in step, nothing can
    drift, and a stock that comes back does so by itself.
    """
    keys = set()
    try:
        rows = EquityHolding.query.with_entities(
            EquityHolding.symbol, EquityHolding.exchange, EquityHolding.quantity
        ).filter(EquityHolding.user_id == current_user.id).all()
    except Exception as exc:
        # A watch list that cannot read the holdings shows everything rather
        # than nothing. Hiding rows on a failed read would be the worse error.
        current_app.logger.error(f'Could not read holdings for the watch list: {exc}')
        return keys
    for symbol, exchange, quantity in rows:
        if _to_int(quantity) <= 0:
            continue
        keys.add((
            str(symbol or '').strip().upper(),
            str(exchange or 'NSE').strip().upper(),
        ))
    return keys


def _return_sold_stocks_to_watchlist():
    """
    Put a stock back on a watch list once the last share is sold.

    One stock, one home. A held stock is hidden from the watch list; when it is
    sold out it belongs back on one.

    A stock that WAS on a watch list needs nothing done here - its row was only
    hidden and reappears by itself the moment the holding reads zero. This
    handles the other case: a stock BOUGHT and sold that was never watched, and
    therefore has no row to unhide. One is created on the default list, with
    the trade nature it was held under and NO alert.

    `returned_to_watchlist` is why a row you then DELETE stays deleted. Without
    it this would be recomputed on every refresh and a deleted row would come
    straight back, which would make the list impossible to curate.

    Runs on the watch list read. Idempotent by that flag, so it is safe there,
    and it is the only screen where the answer matters.

    Returns the symbols added, for the screen to say so.
    """
    added = []
    try:
        holdings = EquityHolding.query.filter(
            EquityHolding.user_id == current_user.id
        ).all()
    except Exception as exc:
        current_app.logger.error(f'Could not read holdings for the watch list return: {exc}')
        return added

    # Held again: the flag is cleared so the NEXT sale returns it once more.
    # Done first, and for every row, because a stock can be bought and sold
    # many times and each sale is a fresh return.
    changed = False
    sold_out = []
    for holding in holdings:
        if _to_int(holding.quantity) > 0:
            if getattr(holding, 'returned_to_watchlist', False):
                holding.returned_to_watchlist = False
                changed = True
            continue
        if getattr(holding, 'returned_to_watchlist', False):
            continue
        # A row that never held anything is not a sale. Nothing to return.
        if _to_float(holding.avg_cost) <= 0:
            continue
        sold_out.append(holding)

    if sold_out:
        on_a_list = {
            (str(item.symbol or '').strip().upper(),
             str(item.exchange or 'NSE').strip().upper())
            for item in EquityWatchlistItem.query.filter_by(
                user_id=current_user.id
            ).all()
        }
        target_list = _default_watchlist()
        used = EquityWatchlistItem.query.filter_by(
            watchlist_id=target_list.id
        ).count()

        seen = set()
        for holding in sold_out:
            key = (
                str(holding.symbol or '').strip().upper(),
                str(holding.exchange or 'NSE').strip().upper(),
            )
            # Held in two accounts and sold from both: one stock, one row.
            if key in seen:
                holding.returned_to_watchlist = True
                changed = True
                continue
            seen.add(key)

            if key in on_a_list:
                # Already watched somewhere. Its row was only hidden and is
                # back on its own; nothing to create, but the flag is set so
                # this is not reconsidered every refresh.
                holding.returned_to_watchlist = True
                changed = True
                continue

            if used >= MAX_WATCHLIST_ITEMS:
                # The list is full. Left unflagged on purpose, so it is
                # returned once there is room rather than lost silently.
                current_app.logger.info(
                    f'[EQUITY_WATCHLIST] {key[0]} was sold out but '
                    f'{target_list.name} is full, so it was not added back.'
                )
                continue

            db.session.add(EquityWatchlistItem(
                user_id=current_user.id,
                watchlist_id=target_list.id,
                symbol=key[0],
                exchange=key[1],
                # Carried from the holding. The reason it was bought is the
                # reason it is worth watching, and asking again would be
                # asking the same question twice.
                trade_nature_id=holding.trade_nature_id,
                target_price=None,
                alert_price=None,
                alert_direction=None,
                price_alert_enabled=False,
            ))
            holding.returned_to_watchlist = True
            used += 1
            added.append(key[0])
            changed = True
            current_app.logger.info(
                f'[EQUITY_WATCHLIST] {key[0]} was sold out and has been added '
                f'back to {target_list.name}.'
            )

    if changed:
        try:
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            current_app.logger.error(f'Could not return sold stocks to the watch list: {exc}')
            return []

    return sorted(set(added))


def _watchlist_rows(watchlist_id=None):
    """
    The stocks in one list, alphabetical.

    Passing no list means the default one, so a caller that has never heard of
    named lists still sees a sensible screen rather than nothing at all.
    """
    if watchlist_id == WATCHLIST_ALL:
        # Every list, still alphabetical. The same stock can sit in two lists
        # and legitimately appear twice, with a different target in each, so
        # nothing is de-duplicated here - the Watch List column is what tells
        # the two rows apart.
        return EquityWatchlistItem.query.filter_by(
            user_id=current_user.id
        ).order_by(EquityWatchlistItem.symbol, EquityWatchlistItem.id).all()

    if watchlist_id is None:
        watchlist_id = _default_watchlist().id
    return EquityWatchlistItem.query.filter_by(
        user_id=current_user.id, watchlist_id=watchlist_id
    ).order_by(EquityWatchlistItem.symbol, EquityWatchlistItem.id).all()


def _watchlist_item_payload(item, quote=None, nature_names=None,
                            list_names=None, noted=None, symbol_known=None):
    """
    One watch list row.

    variance_pct is the PRD's Variance column: how far the live price is from
    the target, as a signed percent of the target. signed_percent_of keeps the
    sign, so a stock trading below its target reads as negative instead of
    being clamped to zero.
    """
    quote = quote or {}
    nature_names = nature_names or {}
    list_names = list_names or {}
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
        'watchlist_id': item.watchlist_id,
        'watchlist_name': list_names.get(item.watchlist_id, ''),
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
        # True when the broker says this symbol does not exist; False is not
        # used - a symbol that has not been asked about, or could not be, is
        # None. "We do not know" and "it is not there" are different answers
        # and a screen that conflates them would accuse a stock of not existing
        # every time a broker was slow.
        'symbol_unknown': (symbol_known or {}).get(
            (str(item.symbol or '').upper(),
             str(item.exchange or 'NSE').upper())
        ) is False,
        'alert_armed': bool(
            item.price_alert_enabled
            and item.alert_price
            and item.alert_triggered_at is None
            # An alert on a symbol the broker does not know can never fire, so
            # it is not armed however the row is configured. Saying otherwise
            # is the worst thing this screen could claim.
            and (symbol_known or {}).get(
                (str(item.symbol or '').upper(),
                 str(item.exchange or 'NSE').upper())
            ) is not False
        ),
        'ltp': _money(ltp),
        'has_ltp': ltp > 0,
        # Whether the push feed had this price on this pass. The alert monitor
        # reads only that feed, so an armed alert on a symbol with no live
        # price is armed and not actually being evaluated - which the screen
        # now says rather than implies.
        'price_is_live': bool(quote.get('live')),
        'prev_close': _money(prev_close),
        'change': _money(change),
        'change_pct': _pct(signed_percent_of(change, prev_close)),
        'variance': _money(variance),
        'variance_pct': _pct(signed_percent_of(variance, target)),
        'has_variance': ltp > 0 and target > 0,
        'has_note': _note_key(item.symbol, item.exchange) in (noted or set()),
        'created_at': _iso(item.created_at),
        'updated_at': _iso(item.updated_at),
    }


# ---------------------------------------------------------------------------
# The investment note
# ---------------------------------------------------------------------------

# Long enough for a real thesis, short enough that a runaway paste cannot fill
# the database. Per box, not per note.
MAX_NOTE_CHARS = 8000

# The boxes a note is made of, in one place. Every loop below walks this rather
# than naming them again, so a fourth box is one line here and nowhere else.
NOTE_FIELDS = ('thesis', 'risk', 'to_watch')

# How many earlier versions the dialog carries back. Ten, at the owner's
# instruction on 6 September.
#
# The rest stay on disk and are NEVER deleted. This bounds what one request
# sends and what the History panel shows, nothing more: a record costs almost
# nothing to keep and cannot be recovered once it stops being kept, so the
# eleventh version is out of sight rather than gone.
MAX_NOTE_VERSIONS = 10


def _note_key(symbol, exchange):
    return (
        str(symbol or '').strip().upper(),
        str(exchange or 'NSE').strip().upper(),
    )


def _notes_present(keys):
    """
    Which of these stocks have a note behind them.

    One query for the whole table draw, rather than one per row. Returns a set
    of (symbol, exchange), so a screen can mark the rows it has written about
    without asking what any of them say.

    Never raises: a marker is worth having and is not worth failing a table for.
    """
    wanted = {_note_key(symbol, exchange) for symbol, exchange in (keys or [])}
    if not wanted:
        return set()
    try:
        rows = EquityStockNote.query.filter(
            EquityStockNote.user_id == current_user.id,
            EquityStockNote.symbol.in_(sorted({key[0] for key in wanted})),
        ).all()
    except Exception as exc:
        current_app.logger.debug(f'Could not read note markers: {exc}')
        return set()

    present = set()
    for row in rows:
        key = _note_key(row.symbol, row.exchange)
        if key not in wanted:
            continue
        # An empty note is not a note. Every box cleared is how you delete
        # one, so the marker has to go with the words.
        if any((getattr(row, field, None) or '').strip()
               for field in NOTE_FIELDS):
            present.add(key)
    return present


def _note_payload(note, versions):
    """The current note and everything it used to say."""
    payload = {
        'symbol': note.symbol if note is not None else None,
        'exchange': note.exchange if note is not None else None,
        'has_note': bool(
            note is not None
            and any((getattr(note, field, None) or '').strip()
                    for field in NOTE_FIELDS)
        ),
        'created_at': _iso(note.created_at) if note is not None else None,
        'updated_at': _iso(note.updated_at) if note is not None else None,
        # Still sent, though the dialog no longer draws them. The record is
        # cheap to carry and impossible to recover once it stops being kept, so
        # it keeps being kept - showing it again is one change to the screen.
        'versions': [
            dict(
                {field: getattr(version, field, None) or ''
                 for field in NOTE_FIELDS},
                id=version.id,
                saved_at=_iso(version.saved_at),
            )
            for version in versions
        ],
    }
    for field in NOTE_FIELDS:
        payload[field] = (
            (getattr(note, field, None) or '') if note is not None else ''
        )
    return payload


def _read_note_text(data, field):
    """One box of the note, bounded and stripped of trailing whitespace."""
    raw = data.get(field)
    if raw is None:
        return ''
    if not isinstance(raw, str):
        raise _BadRequest('%s must be text' % field)
    value = raw.strip()
    if len(value) > MAX_NOTE_CHARS:
        raise _BadRequest(
            'The %s is %d characters. The limit is %d.'
            % (field, len(value), MAX_NOTE_CHARS)
        )
    return value


def _clear_watchlist_alert(item):
    """
    Re-arm a watch list alert.

    Every write that changes alert_price, alert_direction or
    price_alert_enabled must call this, otherwise an alert that already fired
    stays silent for good. See the EquityWatchlistItem docstring.
    """
    item.alert_triggered_at = None
    item.alert_triggered_price = None


# Watch list price alerts are no longer evaluated here.
#
# They used to be worked out inside the request that refreshed the prices, which
# made an alert conditional on somebody having the screen open. They now run in
# app.utils.equity_alert_monitor, on the background tick that already drives the
# stop loss and target monitor, so an alert fires for as long as AlgoMirror is
# running. A fired alert is written to equity_alert_events and collected from
# there by any equity screen through /equity/api/alerts/pending.
#
# _clear_watchlist_alert above is unchanged and still matters: every write that
# touches alert_price, alert_direction or price_alert_enabled must call it, or
# the background monitor will never look at that row again.


def _build_watchlist_payload(with_prices=True, watchlist_id=None):
    """
    M3 Watch List.

    Prices come from the shared push feed, so the 10 second refresh the PRD
    asks for costs no broker call once the feed is warm.
    """
    showing_all = watchlist_id == WATCHLIST_ALL
    active = None
    if not showing_all:
        active = (_default_watchlist() if watchlist_id is None
                  else _owned_watchlist(watchlist_id))

    # Before the rows are read, not after: a stock returned by this sweep must
    # appear on the very screen that returned it, not one refresh later.
    returned = _return_sold_stocks_to_watchlist()

    items = _watchlist_rows(WATCHLIST_ALL if showing_all else active.id)

    # A stock you HOLD is not shown here. Its stop loss, target, exit mode,
    # trade nature and note are changed on Holdings, one stock at a time, and
    # this screen would otherwise offer a second place to change two of them.
    #
    # HIDDEN, not deleted. The row keeps its list, its trade nature and its
    # target price untouched, so when the last share is sold it comes back
    # exactly as it was - which is the whole reason this is a filter and not a
    # delete.
    held = _held_symbol_keys()
    hidden = [
        item for item in items
        if (str(item.symbol or '').strip().upper(),
            str(item.exchange or 'NSE').strip().upper()) in held
    ]
    if hidden:
        items = [item for item in items if item not in hidden]

    natures = _trade_natures()
    nature_names = {nature.id: nature.name for nature in natures}

    # Every row carries its own list's name in the All view, so the column can
    # be shown and sorted without the screen having to join anything.
    lists = _all_watchlists()
    list_names = {entry.id: entry.name for entry in lists}

    quotes = {}
    price_feed = _feed_status_block({'requested': 0, 'from_feed': 0, 'from_rest': 0,
                                     'fallback_symbols': 0})
    symbol_known = {}

    if items and with_prices:
        keys = [(item.symbol, item.exchange) for item in items]
        creds = _account_credentials(_active_accounts())
        quotes, price_feed = _resolve_prices(creds, {}, keys, want_prev_close=True)

        # A row with no price is either waiting for one or carrying something
        # that is not a ticker at all - a company name typed by hand, or a
        # symbol the exchange has since renamed. Those two look identical on
        # screen and are completely different problems, so the ones with no
        # price get asked about.
        silent = [
            key for key in keys
            if _to_float((quotes.get(key) or {}).get('ltp')) <= 0
        ]
        if silent:
            symbol_known = _check_unknown_symbols(creds, silent)

    # One query for the whole table, so the Notes button can show whether
    # there is anything behind it without opening every row to find out.
    noted = _notes_present([(item.symbol, item.exchange) for item in items])

    settings = _equity_settings()
    return {
        'items': [
            _watchlist_item_payload(
                item, quotes.get((item.symbol, item.exchange)), nature_names,
                list_names, noted, symbol_known
            )
            for item in items
        ],
        'trade_natures': [
            {'id': nature.id, 'name': nature.name} for nature in natures
        ],
        'price_alerts_enabled': bool(settings.price_alerts_enabled) if settings else True,
        # No longer shown. The amber line that named these stock by stock was
        # removed on 6 September at the owner's instruction; the closing line
        # under the table now states the rule once instead. The field is kept
        # because it is the only place the count of hidden rows is available,
        # and it costs one set over rows already in hand.
        'held_hidden': sorted({item.symbol for item in hidden}),
        # Sold out since you last looked, and put back on the default list.
        'returned_to_watchlist': returned,
        'max_items': MAX_WATCHLIST_ITEMS,
        'watchlist_id': WATCHLIST_ALL if showing_all else active.id,
        'watchlist_name': 'All Watch Lists' if showing_all else active.name,
        'showing_all': showing_all,
        'watchlists': [
            {'id': entry.id, 'name': entry.name, 'is_default': entry.is_default is True}
            for entry in lists
        ],
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

    Product is never read from the request. Equity is CNC delivery unless a
    sell runs out of shares, and THAT decision is made here from what the
    broker reports - never from anything the caller sends. A browser cannot ask
    for an intraday short; it can only ask to sell more than it owns and be
    told what that means.
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
        # Read on a SELL as well as a BUY now. On a buy these are AlgoMirror's
        # own levels for the holding that results. On a sell they mean nothing
        # UNLESS part of it is a short - and then the stop loss is required,
        # goes to the broker as a resting order, and is the only thing standing
        # between the position and an unlimited loss.
        'stop_loss': _read_price(data, 'stop_loss'),
        'target': _read_price(data, 'target'),
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


def _ist_minute_now():
    """
    Minutes past midnight, IST. The same clock the square-off runs on.

    Derived from UTC rather than the machine's local time, so the answer does
    not change if this ever runs on a server set to something else - and every
    decision that hangs off it decides whether a position can be opened or must
    be closed.
    """
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    return now.hour * 60 + now.minute


def _intraday_rules():
    """
    The three things that decide whether a short may be opened at all.

    Returns (cutoff_minute, squareoff_minute, monitor_enabled).
    """
    settings = _equity_settings()
    cutoff = _to_int(getattr(settings, 'intraday_cutoff_minute', 0)) or (15 * 60)
    squareoff = _to_int(getattr(settings, 'intraday_squareoff_minute', 0)) or (15 * 60 + 12)
    enabled = bool(getattr(settings, 'intraday_monitor_enabled', True))
    return cutoff, squareoff, enabled


def _short_refusal(instruction, reference_price):
    """
    Why this sell may NOT open an intraday short, or None when it may.

    Five gates, and every one of them is a refusal rather than a warning. A
    short is the only position in this module that must be closed today, so
    the questions asked before opening one are not advisory.
    """
    cutoff, squareoff, enabled = _intraday_rules()

    if not enabled:
        return (
            'The square-off monitor is switched off, so nothing would buy this '
            'back before the close. Turn it on in Equity Settings first.'
        )

    # A GTT is an instruction that waits for a price, possibly for weeks. A
    # short is an obligation that has to be closed by this afternoon. Waiting
    # is the one thing it cannot do, so the two do not go together.
    if instruction.get('order_type') == EQUITY_ORDER_TYPE_GTT:
        return (
            'A GTT waits for a price and can rest for days, so it cannot open '
            'a short that has to be bought back today. Use MARKET or LIMIT for '
            'the part you do not own.'
        )

    minute_now = _ist_minute_now()
    if minute_now >= cutoff:
        return (
            'It is past %02d:%02d, so no new short can be opened today. A short '
            'opened now would have to be bought back at %02d:%02d whatever the '
            'price by then.' % (
                cutoff // 60, cutoff % 60, squareoff // 60, squareoff % 60
            )
        )

    stop_loss = _to_float(instruction.get('stop_loss'))
    if stop_loss <= 0:
        return (
            'A short needs a stop loss. It is the only protection between here '
            'and an unlimited loss, and unlike everything else on this screen '
            'it is placed at your broker, so it works even if this machine is '
            'off.'
        )

    # A short is closed by BUYING, so its stop sits ABOVE the entry. Below the
    # entry is a target wearing the wrong label, and it would never fire as a
    # stop - it would simply sit there while the loss ran the other way.
    price = _to_float(reference_price)
    if price > 0 and stop_loss <= price:
        return (
            'A short is closed by buying it back, so its stop loss has to be '
            'ABOVE the price you sell at. %s is at or below %s.' % (
                _money(stop_loss), _money(price)
            )
        )

    return None


def _annotate_sell_capacity(rows, tracked, symbol, exchange, deliverable=None,
                            instruction=None, reference_price=None):
    """
    Add what each account can actually deliver to the split rows, and flag the
    ones that cannot cover their share.

    A CNC sell of shares the account does not hold is a short delivery, which
    is an auction and a penalty rather than a trade, so an account with nothing
    sellable is flagged rather than sent.

    `deliverable` is the broker's own answer - settled holdings plus net
    position - and is what decides this. The EquityHolding row is still read,
    because the exit claim locks it and its status matters, but its QUANTITY is
    no longer what sizes the sell. Holdings alone said you held nothing of a
    stock you had bought that morning.

    An account MISSING from `deliverable` could not be read, and is flagged
    rather than treated as holding nothing. Unknown is not zero: the next
    decision is whether to sell.
    """
    deliverable = {} if deliverable is None else deliverable
    short_refusal = None if instruction is None else _short_refusal(
        instruction, reference_price
    )

    for row in rows:
        holding = tracked.get(_holding_key(row['account_id'], symbol, exchange))
        unreadable = row['account_id'] not in deliverable
        sellable = max(_to_int(deliverable.get(row['account_id'])), 0)
        wanted = _to_int(row.get('quantity'))

        # The shares this account owns go as delivery; only the rest is a
        # short. Sell 100 holding 60 and 60 is CNC, 40 is MIS - the smallest
        # short the instruction can be honoured with.
        row['cnc_quantity'] = min(wanted, sellable) if wanted > 0 else 0
        row['short_quantity'] = max(wanted - row['cnc_quantity'], 0)

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

        if unreadable:
            row['check_ok'] = False
            row['check_reason'] = (
                'This account could not be read, so what it can deliver is '
                'unknown. Nothing is sent to an account we cannot check.'
            )
        elif sellable <= 0 and short_refusal is not None:
            row['check_ok'] = False
            row['check_reason'] = (
                f'This account holds no deliverable {symbol}, so this would be '
                f'a short. {short_refusal}'
            )
        elif holding is not None and holding.is_exit_in_flight:
            row['check_ok'] = False
            row['check_reason'] = (
                'A sell against this holding is already in flight. It has to '
                'settle before another one can be claimed.'
            )
        elif holding is not None and holding.exit_status == EQUITY_HOLDING_STATUS_EXIT_INDETERMINATE:
            row['check_ok'] = False
            row['check_reason'] = (
                'The previous exit on this holding was never confirmed. Verify '
                'it at the broker and resolve it before selling again.'
            )
        elif wanted > sellable and short_refusal is not None:
            row['check_ok'] = False
            row['check_reason'] = (
                f'This account holds {sellable} deliverable shares, short of '
                f'the {wanted} its share of the order needs. {short_refusal}'
            )
        elif row['short_quantity'] > 0:
            # Allowed, and said plainly. The row still passes - this is a
            # statement about what is about to happen, not a problem with it.
            row['check_note'] = (
                '%d of these %d are not owned and go as an INTRADAY SHORT. '
                'Bought back automatically before the close.' % (
                    row['short_quantity'], wanted
                )
            )

    return rows


def _deliverable_by_account(creds, tracked, symbol, exchange):
    """
    How many shares each account can actually deliver TODAY.

        deliverable = settled holdings + net position

    Both books, because neither one alone answers the question:

      - The HOLDINGS book knows what settled before today and is blind to what
        you bought this morning: a delivery buy is a position until T+1.
      - The POSITION book knows today and only today.

    Reading holdings alone is what made an ordinary sale look like a short.
    Buy 100 INFY at 10:00 and sell it at 14:00 and the holdings book says you
    hold none - so the sell would have been sent as an intraday SHORT against
    shares you already owned. Worse for a purchase made at the broker's own
    terminal, where AlgoMirror has no record of it at all.

    The arithmetic is self-correcting, which is the point of it:

        held from before today   100 holdings +   0 position = 100
        bought this morning        0 holdings + 100 position = 100
        held, and sold today     100 holdings - 100 position =   0
        genuinely short            0 holdings -  40 position = -40

    The third line matters beyond the short case. The broker's holdings book
    lags a sale - OpenAlgo's sandbox by a whole day - and the position book
    carries the correction. Asking the broker beats working it out from our own
    record of what we sold.

    UNSETTLED holding rows are deliberately excluded. Those are AlgoMirror's own
    count of today's fills, and today's fills are already in the position book;
    counting both would double every share bought this morning.

    Returns {account_id: deliverable}. An account whose position book could not
    be read is ABSENT rather than zero - the caller must not read silence as
    "this account holds nothing", because the next decision is whether to sell.
    """
    positions = _fetch_positions(creds)

    want_symbol = (symbol or '').strip().upper()
    want_exchange = (exchange or 'NSE').strip().upper()

    deliverable = {}
    for cred in creds or []:
        account_id = cred.get('account_id')
        rows = positions.get(account_id)
        if rows is None:
            # Unreadable. Left out entirely; see the docstring.
            continue

        net_position = 0
        for entry in rows:
            if not isinstance(entry, dict):
                continue
            product = str(entry.get('product') or '').strip().upper()
            if product and product != EQUITY_PRODUCT_CNC:
                # An intraday line is not deliverable stock. A short shows here
                # too, and it is counted by the square-off, not by this.
                continue
            if str(entry.get('symbol') or '').strip().upper() != want_symbol:
                continue
            row_exchange = str(entry.get('exchange') or '').strip().upper()
            if want_exchange and row_exchange and row_exchange != want_exchange:
                continue
            net_position += _to_int(entry.get('quantity'))

        holding = tracked.get(_holding_key(account_id, symbol, exchange))
        settled = 0
        if holding is not None and getattr(holding, 'is_settled', True):
            settled = _to_int(holding.quantity)

        # PLEDGED SHARES ARE STILL YOURS AND STILL SELLABLE.
        #
        # This used to subtract them. It was wrong, and wrong in the dangerous
        # direction: shares pledged for F&O margin are sold every day, the
        # broker raises the unpledge itself, and treating them as undeliverable
        # turned a covered sale into a naked SHORT. The owner's point, 7
        # September, and he is right.
        #
        # The two failures are not symmetrical, which is what decides it. If a
        # broker really will not release a pledged share, the order comes back
        # rejected - visible, and nothing has happened. An accidental short is
        # a live position with no ceiling on the loss. Between a rejection and
        # an unintended short, take the rejection every time.
        #
        # Kept on the row and shown on the screen, because it is worth knowing
        # that selling these will release F&O margin.
        deliverable[account_id] = settled + net_position

    return deliverable


def _sell_context(instruction):
    """
    Prepare a SELL: refresh the tracked holdings the claim will lock, and work
    out what each account can actually deliver.

    The claim reads the quantity off the EquityHolding row, so that row has to
    say what the broker says before anything is claimed. The read is served
    from the 30 second cache when it is warm, so this normally costs nothing.

    Returns (accounts_in_view, tracked_holdings, deliverable_by_account).
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
    creds = [
        cred for cred in (context.get('creds') or [])
        if cred.get('account_id') in wanted
    ]
    deliverable = _deliverable_by_account(
        creds, tracked, instruction['symbol'], instruction['exchange']
    )
    return accounts, tracked, deliverable


def _build_order_preview(instruction):
    """
    The M4 ACCOUNT-WISE ORDER SPLIT table. Writes nothing, places nothing.

    For a SELL the tracked holdings are refreshed first and every row is
    annotated with the deliverable quantity, because for a sell the binding
    constraint is stock rather than cash.
    """
    tracked = {}
    deliverable = None
    if instruction['side'] == EQUITY_SIDE_SELL:
        _accounts, tracked, deliverable = _sell_context(instruction)

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
        # A sell is split by the stock, not by the allocation ratio. Read from
        # the broker a moment ago by _sell_context, so the sale comes out of
        # the accounts that actually have the shares.
        sell_capacity=deliverable,
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
            rows, tracked, instruction['symbol'], instruction['exchange'],
            deliverable=deliverable, instruction=instruction,
            reference_price=preview.get('reference_price')
        )

        # What the screen needs to know before anything is sent: how much of
        # this sell is a short, and if it cannot be one, why.
        cutoff, squareoff, monitor_on = _intraday_rules()
        preview['short_quantity'] = sum(
            _to_int(row.get('short_quantity')) for row in rows
        )
        preview['cnc_quantity'] = sum(
            _to_int(row.get('cnc_quantity')) for row in rows
        )
        preview['short_accounts'] = sum(
            1 for row in rows if _to_int(row.get('short_quantity')) > 0
        )
        # ONLY WHEN THERE IS ACTUALLY A SHORT.
        #
        # This used to be computed for every sell, and the screen disables
        # Review whenever it is set. That was invisible while every multi
        # account sell produced a short by accident: the refusal was always
        # there and there was always a short to justify it. Splitting a sell by
        # the stock removed the accidental shorts and left the refusal behind,
        # so a perfectly ordinary sale of shares he owns came up with Review
        # greyed out and a warning about unlimited loss underneath it.
        #
        # A reason a short may not be opened is not a reason a SALE may not be
        # placed. No short, no refusal.
        preview['short_refusal'] = _short_refusal(
            instruction, preview.get('reference_price')
        ) if _to_int(preview['short_quantity']) > 0 else None
        # True when a stop loss is the ONLY thing missing. The screen uses this
        # to open the box and ask for one, rather than just refusing.
        preview['needs_stop_loss'] = bool(
            _to_int(preview['short_quantity']) > 0
            and _to_float(instruction.get('stop_loss')) <= 0
        )
        preview['squareoff_at'] = '%02d:%02d' % (squareoff // 60, squareoff % 60)
        preview['short_cutoff_at'] = '%02d:%02d' % (cutoff // 60, cutoff % 60)
        preview['intraday_monitor_enabled'] = monitor_on
    else:
        preview['short_quantity'] = 0
        preview['cnc_quantity'] = _to_int(preview.get('allocated_quantity'))
        preview['short_accounts'] = 0
        preview['short_refusal'] = None
        preview['needs_stop_loss'] = False

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
        # Every answer carries these, a BUY included, so the screen never has
        # to ask whether the key is there before asking what it says.
        'shorts': [],
        'short_quantity': 0,
        'short_accounts': 0,
        'short_warnings': [],
        'squareoff_at': None,
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


# ---------------------------------------------------------------------------
# The intraday short leg of a sell
# ---------------------------------------------------------------------------

def _place_short_legs(instruction, rows, reference_price):
    """
    Send the part of a sell that is NOT owned, as an intraday short.

    A sell of 100 against 60 owned is two different trades wearing one name.
    The 60 is delivery and goes down the claim path with everything else. The
    40 is an obligation: shares sold that have to be bought back before the
    close, and this is where those 40 are sent.

    Three things happen per account, in this order, and the order is the point:

        1. the SELL itself, product MIS, so the broker treats it as intraday
           and not as a short delivery that ends in an auction;
        2. an EquityIntradayShort row - the obligation, with its own claim, so
           the square-off monitor and a person pressing Cover cannot both buy
           the same shares back;
        3. a resting SL-M buy at the broker, at the stop the admin typed.

    Step 3 is last because it needs the row from step 2 to hang off, and it is
    NOT skipped when it fails: a short that could not be protected is reported
    as such, loudly, because the only thing between it and an unlimited loss is
    a monitor on a machine that can lose power.

    Returns one outcome per account that had a short in it. Never raises: a
    failure here must not unwind a delivery sell that already went through.
    """
    outcomes = []
    legs = [row for row in rows if _to_int(row.get('short_quantity')) > 0]
    if not legs:
        return outcomes

    stop_loss = _to_float(instruction.get('stop_loss'))
    refusal = _short_refusal(instruction, reference_price)

    for row in legs:
        quantity = _to_int(row.get('short_quantity'))
        account_id = row.get('account_id')
        outcome = {
            'account_id': account_id,
            'account_name': row.get('account_name'),
            'quantity': quantity,
            'status': 'skipped',
            'message': '',
            'order_id': None,
            'broker_order_id': None,
            'short_id': None,
            'stop_status': EQUITY_STOP_STATUS_NONE,
            'stop_trigger_price': stop_loss if stop_loss > 0 else None,
            'stop_message': None,
        }

        # Checked once more, per account, on the path that sends. The preview
        # asked the same question and so did _place_sell; asking again costs
        # nothing and closes the window between them.
        if refusal:
            outcome['message'] = refusal
            outcomes.append(outcome)
            continue

        try:
            result = place_multi_account_order(
                user_id=current_user.id,
                symbol=instruction['symbol'],
                exchange=instruction['exchange'],
                side=EQUITY_SIDE_SELL,
                total_quantity=quantity,
                order_type=instruction['order_type'],
                price=instruction['price'],
                trigger_price=None,
                account_ids=[account_id],
                quantity_overrides={account_id: quantity},
                source=EQUITY_ORDER_SOURCE_MANUAL,
                reference_price=reference_price,
                insufficient_funds_action=instruction['insufficient_funds_action'],
                product=EQUITY_PRODUCT_MIS,
            )
        except Exception as exc:
            db.session.rollback()
            outcome['status'] = 'error'
            outcome['message'] = f'The short could not be sent: {exc}'
            current_app.logger.error(
                f'Equity short leg failed for account {account_id} '
                f'{instruction["symbol"]}: {exc}'
            )
            outcomes.append(outcome)
            continue

        outcome['status'] = result.get('status') or 'error'
        outcome['message'] = result.get('message') or ''
        outcome['order_id'] = result.get('order_id')

        split = None
        for candidate in (result.get('splits') or []):
            if candidate.get('account_id') == account_id:
                split = candidate
                break
        outcome['broker_order_id'] = (split or {}).get('broker_order_id')

        # Recorded on anything that was actually SENT, including a response the
        # broker never confirmed. An unconfirmed short may still be a short,
        # and the square-off verifies against the position book before it acts
        # - so a row that turns out to be nothing is closed harmlessly, while a
        # row that was never written is a real short nothing is watching.
        fill_status = (split or {}).get('fill_status')
        not_sent = (
            split is None
            or fill_status in (
                EQUITY_SPLIT_STATUS_SKIPPED,
                EQUITY_SPLIT_STATUS_FAILED,
                EQUITY_SPLIT_STATUS_REJECTED,
                EQUITY_SPLIT_STATUS_UNSUPPORTED,
            )
        )
        if not_sent:
            outcome['status'] = 'error'
            outcome['message'] = (
                (split or {}).get('error_message')
                or outcome['message']
                or 'Nothing was sent for the short part of this sell.'
            )
            outcomes.append(outcome)
            continue

        short = _record_intraday_short(
            instruction=instruction,
            row=row,
            quantity=_to_int(split.get('quantity')) or quantity,
            result=result,
            split=split,
            reference_price=reference_price,
            stop_loss=stop_loss,
        )
        if short is None:
            # The worst case in this whole feature: the shares ARE sold and
            # nothing in AlgoMirror knows it. The square-off works off the
            # record, so there is nothing to square off, and the Dashboard's
            # count reads zero because it counts the same rows.
            #
            # So this is announced the way an unclosed short is announced -
            # through the notice that pops up on every equity screen - rather
            # than only being returned to whoever happened to be looking at
            # this one response.
            _announce_unrecorded_short(instruction, row, quantity, split)
            outcome['status'] = 'error'
            outcome['message'] = (
                'The short was sent but could NOT be recorded, so nothing will '
                'buy it back automatically. Close it at the broker yourself.'
            )
            outcomes.append(outcome)
            continue

        outcome['short_id'] = short.id
        stop_status, stop_message = _place_protective_stop(short, stop_loss)
        outcome['stop_status'] = stop_status
        outcome['stop_message'] = stop_message
        outcomes.append(outcome)

    return outcomes


def _announce_unrecorded_short(instruction, row, quantity, split):
    """
    Interrupt about shares that were sold short and never written down.

    Everything else about a short is watched by reading its row: the square-off
    reads it, the Dashboard counts it, Positions draws it. When the row is the
    thing that failed, all three of those go quiet at once and the position is
    invisible - while being the only kind of position that MUST be closed today.

    So it is announced through the holding notice, which pops up on every equity
    screen rather than only on the one that happened to send the order. There is
    no row to hang an "already alerted" flag on, so this can only ever fire once
    anyway: it is raised at the moment the write failed, and never again.

    Never raises. It is already handling a failure.
    """
    broker_order_id = (split or {}).get('broker_order_id')
    try:
        db.session.add(EquityHoldingNotice(
            user_id=current_user.id,
            account_id=row.get('account_id'),
            symbol=instruction['symbol'],
            exchange=instruction['exchange'],
            kind=EQUITY_NOTICE_SHARES_LEFT,
            quantity_before=_to_int(quantity),
            quantity_after=_to_int(quantity),
            quantity_delta=0,
            had_armed_level=False,
            message=(
                '%s: %d shares were sold SHORT and AlgoMirror could NOT record '
                'it. Nothing here will buy them back - not the square-off, not '
                'a stop. Close this at your broker yourself.%s'
                % (
                    instruction['symbol'], _to_int(quantity),
                    ' Broker order %s.' % broker_order_id if broker_order_id else ''
                )
            ),
        ))
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            f'Could not announce an unrecorded short in '
            f'{instruction["symbol"]}: {exc}'
        )

    current_app.logger.error(
        f'UNRECORDED SHORT: {_to_int(quantity)} {instruction["symbol"]} sold on '
        f'account {row.get("account_id")}, broker order {broker_order_id}. '
        f'Nothing will buy this back automatically.'
    )


def _record_intraday_short(instruction, row, quantity, result, split,
                           reference_price, stop_loss):
    """
    Write the obligation down, before anything protective is placed.

    Deliberately its own commit. If the protective stop then fails, the short
    is still on disk and the square-off monitor will find it - which is the
    difference between a position that is watched and one that is not.

    Returns the row, or None when it could not be written.
    """
    price = _to_float(instruction.get('price'))
    if price <= 0:
        price = _to_float(reference_price)

    try:
        short = EquityIntradayShort(
            user_id=current_user.id,
            account_id=row.get('account_id'),
            symbol=instruction['symbol'],
            exchange=instruction['exchange'],
            quantity=_to_int(quantity),
            entry_price=price if price > 0 else None,
            opening_order_id=result.get('order_id'),
            opening_split_id=split.get('split_id'),
            opened_at=datetime.utcnow(),
            # The IST trading day, not the UTC one. India keeps a fixed +05:30
            # and no daylight saving, so the offset is exact rather than close.
            trade_date=(datetime.utcnow() + timedelta(hours=5, minutes=30)).date(),
            status=EQUITY_SHORT_STATUS_OPEN,
            stop_trigger_price=stop_loss if stop_loss > 0 else None,
            stop_status=EQUITY_STOP_STATUS_NONE,
        )
        db.session.add(short)
        db.session.commit()
        current_app.logger.warning(
            f'Equity INTRADAY SHORT opened: {short.quantity} {short.symbol} on '
            f'account {short.account_id}, short id {short.id}, must be bought '
            f'back today'
        )
        return short
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            f'Equity short was SENT but could not be recorded for account '
            f'{row.get("account_id")} {instruction["symbol"]}: {exc}'
        )
        return None


def _place_protective_stop(short, stop_loss):
    """
    Put a resting SL-M buy at the broker, behind one short.

    This is the half of the protection that does not depend on this machine.
    AlgoMirror's own stop loss lives in a monitor here and dies with a power
    cut; this one sits in the exchange's stop-loss book and fires regardless.

    SL-M rather than SL: an SL becomes a limit order once triggered and can sit
    unfilled while the price runs away from it, which on a short is the exact
    scenario the stop exists for.

    Returns (stop_status, message). Never raises - a stop that could not be
    placed is a warning to carry back to the screen, not a reason to lose the
    short that is already open.
    """
    quantity = _to_int(short.quantity)
    trigger = _to_float(stop_loss)
    if quantity <= 0 or trigger <= 0:
        return EQUITY_STOP_STATUS_NONE, None

    try:
        result = place_multi_account_order(
            user_id=short.user_id,
            symbol=short.symbol,
            exchange=short.exchange,
            side=EQUITY_SIDE_BUY,
            total_quantity=quantity,
            order_type=EQUITY_ORDER_TYPE_SL_M,
            price=None,
            trigger_price=trigger,
            account_ids=[short.account_id],
            quantity_overrides={short.account_id: quantity},
            source=EQUITY_ORDER_SOURCE_MANUAL,
            reference_price=trigger,
            insufficient_funds_action=EQUITY_FUNDS_ACTION_SKIP,
            product=EQUITY_PRODUCT_MIS,
        )
    except Exception as exc:
        db.session.rollback()
        return _mark_stop_failed(short, f'{exc}')

    split = None
    for candidate in (result.get('splits') or []):
        if candidate.get('account_id') == short.account_id:
            split = candidate
            break

    broker_order_id = (split or {}).get('broker_order_id')
    if not broker_order_id:
        reason = (
            (split or {}).get('error_message')
            or result.get('message')
            or 'The broker did not return an order id for the stop.'
        )
        return _mark_stop_failed(short, reason)

    try:
        short.stop_order_id = result.get('order_id')
        short.stop_broker_order_id = str(broker_order_id)
        short.stop_trigger_price = trigger
        short.stop_status = EQUITY_STOP_STATUS_RESTING
        short.stop_error = None
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        # The order IS at the broker; only our note of it failed. Said plainly,
        # because the square-off cancels the resting stop by that id and cannot
        # cancel what it was never told about.
        current_app.logger.error(
            f'Equity protective stop {broker_order_id} is resting at the broker '
            f'for short {short.id} but could not be recorded: {exc}'
        )
        return EQUITY_STOP_STATUS_FAILED, (
            'A stop was placed at the broker but AlgoMirror could not record '
            'it. Check it at the broker before the square-off runs.'
        )

    current_app.logger.info(
        f'Equity protective stop resting at the broker for short {short.id}: '
        f'BUY {quantity} {short.symbol} SL-M at {trigger}'
    )
    return EQUITY_STOP_STATUS_RESTING, None


def _mark_stop_failed(short, reason):
    """Record that the short has no protection at the broker, and say so."""
    try:
        short.stop_status = EQUITY_STOP_STATUS_FAILED
        short.stop_error = str(reason)[:1000]
        db.session.commit()
    except Exception:
        db.session.rollback()
    current_app.logger.error(
        f'Equity short {short.id} has NO protective stop at the broker: {reason}'
    )
    return EQUITY_STOP_STATUS_FAILED, (
        f'No stop could be placed at your broker for this short, so it is only '
        f'protected while AlgoMirror is running: {reason}'
    )


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
    accounts, tracked, deliverable = _sell_context(instruction)
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
        # The same basis the screen was shown. Without this the preview would
        # split by stock and the placement by ratio, which is the one way this
        # fix could be worse than the fault.
        sell_capacity=deliverable,
    )
    if preview.get('status') != 'success':
        raise _BadRequest(preview.get('message') or 'The order split could not be worked out')

    rows = _annotate_sell_capacity(
        preview['rows'], tracked, instruction['symbol'], instruction['exchange'],
        deliverable=deliverable, instruction=instruction,
        reference_price=preview.get('reference_price')
    )

    # Checked again here, on the path that actually sends. The preview refuses
    # a short it cannot allow, but a preview is a screen and this is the
    # broker: the gate that matters is the one nothing can go round.
    short_total = sum(_to_int(row.get('short_quantity')) for row in rows)
    if short_total > 0:
        refusal = _short_refusal(instruction, preview.get('reference_price'))
        if refusal:
            raise _BadRequest(
                'Nothing was placed. %d of these %d shares are not owned and '
                'would be an intraday short. %s' % (
                    short_total, _to_int(instruction['total_quantity']), refusal
                )
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

    # The part of this sell that is NOT owned. Sent after the delivery legs and
    # kept entirely separate from them: a different product, a different
    # obligation, and a failure in one must not unwind the other.
    short_outcomes = _place_short_legs(
        instruction, rows, preview.get('reference_price')
    )
    shorts_by_account = {
        outcome['account_id']: outcome for outcome in short_outcomes
    }
    short_quantity = sum(
        _to_int(outcome.get('quantity')) for outcome in short_outcomes
        if outcome.get('short_id')
    )
    short_accounts = sum(1 for outcome in short_outcomes if outcome.get('short_id'))

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
        # An account that owns none of this stock has no holding to claim, so
        # it lands here with nothing sold as delivery - but a short may well
        # have gone through for it. Saying "skipped" then would be a plain
        # untruth on the one row where the truth matters most.
        short = shorts_by_account.get(row['account_id'])
        if short is not None and short.get('short_id'):
            reason = (
                '%d shares went as an INTRADAY SHORT rather than delivery, '
                'because this account owns none of them. They are bought back '
                'automatically before the close.' % _to_int(short.get('quantity'))
            )
            exit_status = 'short'
        elif short is not None:
            reason = (
                short.get('message')
                or 'The short part of this sell was not sent'
            )
            exit_status = 'skipped'
        else:
            reason = row.get('check_reason') or 'Skipped before any order was sent'
            exit_status = 'skipped'

        payload = _skipped_split_payload(
            row['account_id'], directory,
            quantity=0,
            ratio_quantity=row.get('ratio_quantity', 0),
            qty_ratio=row.get('qty_ratio', 0.0),
            est_value=row.get('est_value'),
            reason=reason,
        )
        payload['holding_id'] = row.get('holding_id')
        payload['exit_status'] = exit_status
        payload['exit_message'] = reason
        payload['short_quantity'] = (
            _to_int(short.get('quantity')) if short is not None and short.get('short_id')
            else 0
        )
        payload['plan_qty_ratio'] = _pct(row.get('qty_ratio', 0.0))
        payload['plan_ratio_quantity'] = _to_int(row.get('ratio_quantity', 0))
        splits.append(payload)

    splits.sort(key=lambda row: row['account_id'] or 0)

    selected = len(rows)

    # An account that got a short got something, so it is neither skipped nor
    # a reason to call the whole order an error. Counted as a SET rather than a
    # sum, because one account can take a delivery leg and a short leg at once
    # and adding those two would report more accounts served than exist.
    served = set()
    for result in results:
        if result.get('status') == 'success':
            row = row_by_holding.get(result.get('holding_id')) or {}
            if row.get('account_id'):
                served.add(row['account_id'])
    for outcome in short_outcomes:
        if outcome.get('short_id'):
            served.add(outcome['account_id'])

    short_only = sum(
        1 for row in skipped_rows
        if (shorts_by_account.get(row['account_id']) or {}).get('short_id')
    )
    skipped_total = counts['accounts_skipped'] + len(skipped_rows) - short_only
    if not served:
        status = 'error'
    elif len(served) < selected:
        status = 'partial'
    else:
        status = 'success'

    parts = [f'{counts["accounts_placed"]} of {selected} accounts placed']
    if short_accounts:
        parts.append(
            f'{short_quantity} sold short across {short_accounts} '
            f'{"account" if short_accounts == 1 else "accounts"}'
        )
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

    # What went short, and everything that is wrong with it. Warnings rather
    # than a status, because none of these unwind anything: the shares are
    # already sold and the only useful thing left to do is say so clearly.
    cutoff, squareoff, _monitor_on = _intraday_rules()
    warnings = []
    for outcome in short_outcomes:
        name = outcome.get('account_name') or f'account {outcome.get("account_id")}'
        if not outcome.get('short_id'):
            warnings.append(
                f'{name}: the {outcome.get("quantity")} shares this account does '
                f'not own were NOT sold. {outcome.get("message") or ""}'.strip()
            )
        elif outcome.get('stop_status') != EQUITY_STOP_STATUS_RESTING:
            warnings.append(
                f'{name}: {outcome.get("quantity")} shares are short with NO '
                f'stop resting at your broker. '
                f'{outcome.get("stop_message") or ""}'.strip()
            )

    response['shorts'] = short_outcomes
    response['short_quantity'] = short_quantity
    response['short_accounts'] = short_accounts
    response['short_warnings'] = warnings
    response['squareoff_at'] = '%02d:%02d' % (squareoff // 60, squareoff % 60)
    response['placed_quantity'] = placed_quantity + short_quantity
    if short_quantity:
        response['product'] = 'CNC + MIS' if placed_quantity else EQUITY_PRODUCT_MIS
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


def _like_contains(text):
    """
    A LIKE pattern matching anywhere in a value, with the user's own wildcards
    taken literally.

    The Stock box is free text. Somebody typing "nifty" means "the stocks with
    nifty in the name", and somebody typing "50%" means the three characters
    5, 0 and a percent sign - not "anything beginning 50".
    """
    escaped = (
        str(text)
        .replace('\\', '\\\\')
        .replace('%', '\\%')
        .replace('_', '\\_')
    )
    return '%' + escaped + '%'


def _symbol_matches(needle, symbol):
    """The Stock filter applied to a row rather than to a query."""
    if not needle:
        return True
    return str(needle).upper() in str(symbol or '').upper()


def _external_matches(row, filters):
    """
    Whether a row the BROKER supplied survives the screen's filters.

    AlgoMirror's own rows are filtered by the database. An order or a fill
    placed outside AlgoMirror never went through it, so it is grafted on after
    that query has already run and has to be filtered here by hand. It is
    exactly this second step that was missing: a stock filter narrowed the
    stored rows and left every outside row on screen, so filtering for one
    stock could return a different one.

    A trade nature filter removes them all. Nothing placed outside AlgoMirror
    ever carried a nature, and a row listed under "Positional" would be
    claiming something nobody ever said.
    """
    if not _symbol_matches(filters.get('symbol'), row.get('symbol')):
        return False
    if filters.get('side') and row.get('side') != filters['side']:
        return False
    if filters.get('order_type') and row.get('order_type') != filters['order_type']:
        return False
    if filters.get('status'):
        # The order book carries status; the trade book carries the status of
        # the order the fill belongs to.
        if (row.get('status') or row.get('order_status')) != filters['status']:
            return False
    if filters.get('trade_nature_id') is not None:
        return False
    return True


def _order_filters(query, account_id=None, symbol=None, side=None, status=None,
                   order_type=None, source=None, nature_id=None):
    """Apply the shared Order Book and Trade Book filters."""
    if account_id is not None:
        query = query.filter(
            EquityOrder.splits.any(EquityOrderSplit.account_id == account_id)
        )
    if symbol:
        # Contains, not equals. The box is free text with no picker behind it,
        # so "nifty" has to find NIFTYBEES or the filter reads as broken.
        query = query.filter(
            EquityOrder.symbol.ilike(_like_contains(symbol), escape='\\')
        )
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


# ---------------------------------------------------------------------------
# The broker's own books
#
# AlgoMirror's tables record what it was told to do. The broker records what
# actually happened, including orders placed from a terminal that never came
# through here at all. For TODAY the broker is the spine and these helpers
# graft its rows onto the stored ones. For earlier days the broker will not
# serve a book at all, so the stored record is the whole record.
# ---------------------------------------------------------------------------

def _remember_books(accounts, books, unreadable):
    """Record a book read so a later reader inside the window can reuse it."""
    now = time.monotonic()
    for account in accounts:
        book = (books or {}).get(account.id)
        if book is None:
            _BOOKS_CACHE.pop(account.id, None)
            _BOOKS_UNREADABLE.add(account.id)
        else:
            _BOOKS_CACHE[account.id] = book
            _BOOKS_UNREADABLE.discard(account.id)
        _BOOKS_REFRESHED_AT[account.id] = now


def _cached_books(accounts):
    """
    The cached books for these accounts, or None when any of them is missing
    or has aged out.

    All or nothing on purpose. A screen served half from cache and half from a
    live read would be reporting two different moments as one, and the reader
    has no way to see the seam.
    """
    now = time.monotonic()
    books = {}
    unreadable = []
    oldest = 0.0
    for account in accounts:
        stamp = _BOOKS_REFRESHED_AT.get(account.id)
        if stamp is None or (now - stamp) > BOOKS_CACHE_TTL_SECONDS:
            return None
        oldest = max(oldest, now - stamp)
        if account.id in _BOOKS_UNREADABLE:
            unreadable.append(account.id)
        else:
            books[account.id] = _BOOKS_CACHE[account.id]
    return books, unreadable, oldest


def _books_for_screen(account_id=None, allow_cached=False):
    """
    Today's order and trade books from every account in scope.

    Returns (books, unreadable, accounts, age_seconds). An empty result is not
    an error: it means no account could be read, and the caller falls back to
    the stored record and says so. age_seconds is how old the books are, 0.0
    for a live read.

    allow_cached is off by default, so the Order Book and Trade Book screens
    keep reading live and nothing about them changes. The dashboard turns it
    on: its Today's Orders is a summary, and two broker round trips per poll
    for a summary is what made that screen slow. Orders placed through
    AlgoMirror come from its own tables either way and are never stale; only an
    order placed at the broker terminal can lag, by at most the window, and the
    screen is told the age so it can say so rather than imply it is live.
    """
    accounts = _active_accounts()
    if account_id is not None:
        accounts = [account for account in accounts if account.id == account_id]
    if not accounts:
        return {}, [], [], 0.0

    if allow_cached:
        cached = _cached_books(accounts)
        if cached is not None:
            books, unreadable, age = cached
            return books, unreadable, accounts, age

    try:
        from app.utils.equity_fill_reconciler import read_broker_books
        books, unreadable = read_broker_books(
            accounts, timeout=_broker_read_timeout()
        )
    except Exception as exc:
        # A screen must never fail because a broker did. Everything falls back
        # to the stored record, marked unverified.
        current_app.logger.warning(f'Could not read the broker books: {exc}')
        return {}, [account.id for account in accounts], accounts, 0.0

    _remember_books(accounts, books, unreadable)
    return books, unreadable, accounts, 0.0


def _broker_order_index(books):
    """{(account_id, broker order id): row} across every account that answered."""
    index = {}
    for account_id, book in (books or {}).items():
        for entry in book.get('orders') or []:
            order_id = str(entry.get('orderid') or entry.get('order_id') or '').strip()
            if order_id:
                index[(account_id, order_id)] = entry
    return index


# Broker order status text mapped onto the vocabulary the Order Book already
# speaks. Anything unrecognised is shown as the broker worded it rather than
# forced into a box it may not belong in.
_EXTERNAL_STATUS_MAP = {
    'complete': EQUITY_ORDER_STATUS_COMPLETED,
    'completed': EQUITY_ORDER_STATUS_COMPLETED,
    'filled': EQUITY_ORDER_STATUS_COMPLETED,
    'executed': EQUITY_ORDER_STATUS_COMPLETED,
    'open': EQUITY_ORDER_STATUS_PENDING,
    'pending': EQUITY_ORDER_STATUS_PENDING,
    'trigger pending': EQUITY_ORDER_STATUS_PENDING,
    'cancelled': EQUITY_ORDER_STATUS_CANCELLED,
    'canceled': EQUITY_ORDER_STATUS_CANCELLED,
    'rejected': EQUITY_ORDER_STATUS_CANCELLED,
}


def _external_group_key(entry):
    """
    What makes two broker orders the same instruction for display.

    Symbol, exchange, side, order type and product. NOT price and NOT quantity:
    the same instruction placed on two accounts is routinely a different size,
    and a limit may be a paise apart. Those differences belong in the split,
    which is exactly where the rest of this screen puts them.
    """
    return (
        str(entry.get('symbol') or '').strip().upper(),
        str(entry.get('exchange') or '').strip().upper(),
        str(entry.get('action') or entry.get('side') or '').strip().upper(),
        str(entry.get('pricetype') or entry.get('order_type') or '').strip().upper(),
        str(entry.get('product') or '').strip().upper(),
    )


def _external_fill_price(books, account_id, broker_order_id):
    """
    What an order AlgoMirror never placed actually filled at, from the broker's
    own trade book.

    The average price on an AlgoMirror order comes from its EquityTrade rows.
    An external order has none - no order, no split, no fills of ours - so that
    lookup returns nothing and a market order fell back to reading "Market"
    even after it had filled. The broker's trade book is where its executions
    are, and it is already loaded for the merge.

    Weighted by quantity, so an order filled in parts reads correctly.
    """
    trades = ((books or {}).get(account_id) or {}).get('trades') or []
    wanted = str(broker_order_id or '').strip()
    if not wanted:
        return None

    value = 0.0
    shares = 0
    for entry in trades:
        if not isinstance(entry, dict):
            continue
        found = str(
            entry.get('orderid') or entry.get('order_id') or ''
        ).strip()
        if found != wanted:
            continue
        price = _to_float(
            entry.get('average_price')
            if entry.get('average_price') is not None
            else entry.get('price')
        )
        quantity = _to_int(entry.get('quantity'))
        if price <= 0 or quantity <= 0:
            continue
        value += price * quantity
        shares += quantity

    return _money(value / shares) if shares else None


def _external_order_row(key, members, directory, books=None):
    """
    One instruction the broker reports that AlgoMirror never placed, with every
    account that carries it folded into a single row.

    The rest of this book shows one row per instruction with the accounts
    behind it, reached through View Split. An external order that listed itself
    once per account would be the only thing on the screen not doing that, so
    it is grouped the same way and its split is carried inline - there is no
    AlgoMirror order id to fetch one with.

    Deliberately hollow where AlgoMirror would have had something to say: no
    trade nature, no allocation ratio, no levels. Nobody ever told this
    application why the order existed, and inventing an answer would be worse
    than leaving it blank.
    """
    from app.utils.equity_fill_reconciler import broker_time_to_utc

    symbol, exchange, side, order_type, product = key

    total = 0
    filled_total = 0
    splits = []
    statuses = []
    placed_at = None
    fill_value = 0.0
    fill_shares = 0

    for account_id, entry in members:
        account = (directory or {}).get(account_id) or {}
        quantity = _to_int(entry.get('quantity'))
        filled = _to_int(
            entry.get('filled_quantity')
            if entry.get('filled_quantity') is not None
            else entry.get('filledshares')
        )
        raw_status = str(
            entry.get('order_status') or entry.get('status') or ''
        ).strip()
        mapped = _EXTERNAL_STATUS_MAP.get(raw_status.lower())
        statuses.append(mapped)

        total += quantity
        filled_total += filled

        # The broker reports IST and marks nothing. Stored and rendered as UTC
        # it would read five and a half hours late, so it is converted here,
        # and compared as a datetime rather than as text.
        stamp = broker_time_to_utc(entry.get('timestamp'))
        if stamp and (placed_at is None or stamp < placed_at):
            placed_at = stamp

        # What it filled at, from the broker's own trade book. Weighted across
        # accounts, because an external order grouped from two accounts is one
        # instruction and its price is the average of what actually traded.
        member_price = _external_fill_price(
            books, account_id,
            entry.get('orderid') or entry.get('order_id')
        )
        member_filled = _to_int(
            entry.get('filled_quantity')
            if entry.get('filled_quantity') is not None
            else entry.get('filledshares')
        )
        if member_price and member_filled > 0:
            fill_value += member_price * member_filled
            fill_shares += member_filled

        splits.append({
            'split_id': None,
            'account_id': account_id,
            'account_name': account.get('account_name'),
            'broker_name': account.get('broker_name'),
            # No ratio: this order was never split by one, and a percentage
            # here would suggest AlgoMirror decided the size.
            'qty_ratio': None,
            'quantity': quantity,
            'ratio_quantity': None,
            'qty_overridden': False,
            'est_value': _money(_to_float(entry.get('price')) * quantity) or None,
            'cash_balance': None,
            'fill_status': mapped or (raw_status.upper() or 'UNKNOWN'),
            'filled_quantity': filled,
            'avg_fill_price': _money(_to_float(
                entry.get('average_price')
                if entry.get('average_price') is not None
                else entry.get('price')
            )),
            'broker_order_id': str(
                entry.get('orderid') or entry.get('order_id') or ''
            ).strip(),
            'broker_gtt_id': None,
            'error_message': None,
            'placed_outside': True,
        })

    known = [status for status in statuses if status]
    if known and all(status == EQUITY_ORDER_STATUS_COMPLETED for status in known):
        status = EQUITY_ORDER_STATUS_COMPLETED
    elif known and all(status == EQUITY_ORDER_STATUS_CANCELLED for status in known):
        status = EQUITY_ORDER_STATUS_CANCELLED
    elif any(status == EQUITY_ORDER_STATUS_PENDING for status in known):
        status = (EQUITY_ORDER_STATUS_PARTIAL if len(set(known)) > 1
                  else EQUITY_ORDER_STATUS_PENDING)
    else:
        status = EQUITY_ORDER_STATUS_PENDING

    open_accounts = sum(
        1 for entry in statuses if entry == EQUITY_ORDER_STATUS_PENDING
    )
    filled_accounts = sum(
        1 for entry in statuses if entry == EQUITY_ORDER_STATUS_COMPLETED
    )
    count = len(members)

    # A synthetic id so View Split can find this row. Namespaced so it can
    # never collide with a real EquityOrder id.
    row_id = 'ext:%s' % '|'.join(key)

    return {
        'order_id': row_id,
        'broker_order_id': None,
        'source_of_row': 'broker',
        'placed_outside': True,
        'not_at_broker': False,
        'symbol': symbol,
        'exchange': exchange,
        'side': side,
        'order_type': order_type,
        'product': product,
        'total_quantity': total,
        'filled_quantity': filled_total,
        'leftover_quantity': 0,
        'price': _money(_to_float(members[0][1].get('price'))),
        'trigger_price': _money(_to_float(members[0][1].get('trigger_price'))),
        'stop_loss': None,
        'target': None,
        'status': status,
        'status_reason': 'Placed outside AlgoMirror',
        'source': None,
        'trade_nature_id': None,
        'trade_nature': None,
        'insufficient_funds_action': None,
        'error_message': None,
        'avg_price': _money(fill_value / fill_shares) if fill_shares else None,
        'placed_at': _iso(placed_at) if placed_at else None,
        'cancelled_at': None,
        'updated_at': None,
        'accounts_count': count,
        'accounts_selected': count,
        'accounts_placed': count,
        'accounts_filled': filled_accounts,
        'accounts_open': open_accounts,
        'accounts_label': '%d/%d' % (count, count),
        'counts': {'total': count, 'indeterminate': 0},
        'splits': splits,
        # Not modifiable or cancellable from here: AlgoMirror did not place it
        # and has no order of its own to act on.
        'is_open': status == EQUITY_ORDER_STATUS_PENDING,
        'can_modify': False,
        'can_cancel': False,
        'is_carried_gtt': False,
    }


def _text_or_none(value):
    text = str(value or '').strip()
    return text or None


# How far apart two orders from one action can be placed and still be shown as
# one row. The exit monitor dispatches its accounts within the same second and
# a manual multi-account sell fans out just as fast, so this is generous; it
# exists so a slow broker on one account cannot split the row.
SAME_ACTION_SECONDS = 120


def _same_action_key(row):
    """
    What makes two AlgoMirror orders one action for display.

    An exit is created per HOLDING, and a holding belongs to one account, so
    the monitor breaching the same stock on two accounts produces two parent
    orders - as does a manual Sell across two accounts. They are one thing the
    admin did, and the book shows one row per thing the admin did.

    Time is deliberately not in the key; it is checked separately, because two
    orders being the same kind is a different question from their being the
    same moment.
    """
    return (
        row.get('symbol'),
        row.get('exchange'),
        row.get('side'),
        row.get('order_type'),
        row.get('source'),
        row.get('trade_nature_id'),
    )


def _merge_same_action(rows):
    """
    Fold per-account orders from one action into a single row.

    Three conditions, all required:

      same key            symbol, exchange, side, order type, source, nature
      close in time       within SAME_ACTION_SECONDS of the group's first
      DISJOINT accounts   no account appears in two of the merged orders

    The last one is the guard that matters. Two exits on the SAME account
    minutes apart are two separate events - a partial fill that reopened the
    row, say - and merging them would list that account twice in the split and
    double its quantity. Disjointness is what tells "one action across two
    accounts" from "two actions on one account".
    """
    merged = []
    groups = {}

    for row in rows:
        if row.get('placed_outside'):
            # Already grouped on its own terms, and it has no AlgoMirror
            # identity to group by.
            merged.append(row)
            continue

        key = _same_action_key(row)
        accounts = row.get('_accounts') or set()
        stamp = row.get('placed_at') or ''

        target = None
        for candidate in groups.get(key, []):
            if candidate['_accounts'] & accounts:
                continue
            first = candidate['placed_at'] or ''
            if first and stamp:
                try:
                    gap = abs(
                        (datetime.fromisoformat(stamp)
                         - datetime.fromisoformat(first)).total_seconds()
                    )
                except ValueError:
                    gap = 0
                if gap > SAME_ACTION_SECONDS:
                    continue
            target = candidate
            break

        if target is None:
            row['_merged_order_ids'] = [row.get('order_id')]
            groups.setdefault(key, []).append(row)
            merged.append(row)
            continue

        _absorb_order_row(target, row)

    return merged


def _absorb_order_row(target, row):
    """Fold one order's numbers and splits into the row already on screen."""
    # Before the quantities are summed, because the average is weighted on the
    # quantity each row brought. Two accounts rarely fill at the same tick, so
    # averaging the averages would be wrong whenever their sizes differ.
    priced = [
        (seen.get('avg_price'), _to_int(seen.get('filled_quantity')))
        for seen in (target, row)
        if seen.get('avg_price') and _to_int(seen.get('filled_quantity')) > 0
    ]
    shares = sum(quantity for _price, quantity in priced)
    if shares:
        target['avg_price'] = _money(
            sum(price * quantity for price, quantity in priced) / shares
        )
    elif not target.get('avg_price'):
        target['avg_price'] = row.get('avg_price')

    target['total_quantity'] += row.get('total_quantity') or 0
    target['filled_quantity'] += row.get('filled_quantity') or 0
    target['leftover_quantity'] += row.get('leftover_quantity') or 0

    for field in ('accounts_count', 'accounts_selected', 'accounts_placed',
                  'accounts_filled', 'accounts_open'):
        target[field] = (target.get(field) or 0) + (row.get(field) or 0)

    counts = dict(target.get('counts') or {})
    for field, value in (row.get('counts') or {}).items():
        if isinstance(value, (int, float)):
            counts[field] = (counts.get(field) or 0) + value
    # 'reason' is prose from the engine and must not be summed.
    if isinstance((row.get('counts') or {}).get('reason'), str):
        counts['reason'] = (row.get('counts') or {}).get('reason')
    target['counts'] = counts

    target['accounts_label'] = '%d/%d' % (
        target.get('accounts_placed') or 0, target.get('accounts_selected') or 0
    )

    if 'splits' in target or 'splits' in row:
        target['splits'] = list(target.get('splits') or []) + list(row.get('splits') or [])

    target['_accounts'] = (target.get('_accounts') or set()) | (row.get('_accounts') or set())
    target['_merged_order_ids'] = (
        list(target.get('_merged_order_ids') or []) + [row.get('order_id')]
    )

    # The earliest placement is when the action happened.
    if row.get('placed_at') and (
        not target.get('placed_at') or row['placed_at'] < target['placed_at']
    ):
        target['placed_at'] = row['placed_at']

    # A row is open if any of its orders is, and modifiable only when there is
    # exactly one order behind it - there is no single thing to modify once two
    # have been folded together.
    target['is_open'] = bool(target.get('is_open') or row.get('is_open'))
    target['can_modify'] = False
    target['can_cancel'] = bool(target.get('is_open'))

    # Status rolls up the way a parent order's does across its splits.
    statuses = {target.get('status'), row.get('status')}
    statuses.discard(None)
    if len(statuses) > 1:
        target['status'] = EQUITY_ORDER_STATUS_PARTIAL
        target['status_reason'] = 'Accounts differ'

    target['not_at_broker'] = bool(
        target.get('not_at_broker') and row.get('not_at_broker')
    )


def _avg_fill_price_by_order(orders):
    """
    The price each order actually filled at, keyed by order id.

    Weighted by quantity across every fill of every account, because that is
    the only figure that answers "what did this cost me". A market order has no
    price of its own, and the split's stored avg_fill_price is taken from the
    broker's ORDER book, which carries the instruction price rather than the
    execution - so for a market order it is empty and for a limit order it is
    the limit, not the fill. The trade rows are where the executions are.

    One query for the whole screen rather than one per row.
    """
    order_ids = [order.id for order in orders]
    if not order_ids:
        return {}

    try:
        rows = db.session.query(
            EquityOrderSplit.equity_order_id,
            EquityTrade.execution_price,
            EquityTrade.executed_quantity,
        ).join(
            EquityTrade, EquityTrade.split_id == EquityOrderSplit.id
        ).filter(
            EquityOrderSplit.equity_order_id.in_(order_ids)
        ).all()
    except Exception as exc:
        current_app.logger.debug(f'Could not read fill prices: {exc}')
        return {}

    totals = {}
    for order_id, price, quantity in rows:
        price = _to_float(price)
        quantity = _to_int(quantity)
        if price <= 0 or quantity <= 0:
            continue
        value, shares = totals.get(order_id, (0.0, 0))
        totals[order_id] = (value + price * quantity, shares + quantity)

    return {
        order_id: _money(value / shares)
        for order_id, (value, shares) in totals.items() if shares
    }


def _is_resting_gtt(row):
    """
    Whether this row is a standing instruction rather than something that
    happened today.

    A GTT still PENDING or PARTIAL is waiting for a price that may never come,
    and it can sit there for weeks. It belongs in its own list. A GTT that has
    triggered or been cancelled is finished business and stays in the day's
    list with everything else that finished.
    """
    return (
        row.get('order_type') == EQUITY_ORDER_TYPE_GTT
        and row.get('status') in OPEN_ORDER_STATUSES
    )


def _build_order_book(filters, carry_open_gtt=True, include_splits=False,
                      sort_by_status=False, merge_broker=False,
                      books_may_be_cached=False, split_gtt=False):
    """
    M5 Order Book, and the M4b Order Status list, which share one query.

    merge_broker grafts today's live broker book onto the stored rows: orders
    placed outside AlgoMirror appear, and a stored order the broker has never
    heard of is marked rather than left looking live. It costs two broker calls
    per account, so the Order Status list - which is polled - leaves it off and
    the Order Book screen turns it on.

    books_may_be_cached lets a caller accept a book read moments ago instead of
    paying for a fresh one. Off by default: the Order Book screen is the
    authority on what the broker holds and always reads live. The dashboard
    turns it on for its Today's Orders summary, and is handed the age of what
    it got so it can show it.

    split_gtt lifts every GTT still working out of the main list and returns it
    under 'gtt_orders'. A GTT is a standing instruction, not something that
    happened today, and mixing the two made the day's list read as longer than
    the day actually was. A GTT that has since triggered or been cancelled is
    history and stays in the day's list where it belongs.
    """
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
    avg_prices = _avg_fill_price_by_order(orders)
    rows = []
    carried = 0
    # (account_id, broker order id) pairs AlgoMirror already accounts for, so
    # the same order is never listed twice.
    claimed = set()
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
        payload['avg_price'] = avg_prices.get(order.id)
        payload['source_of_row'] = 'algomirror'
        payload['placed_outside'] = False
        payload['not_at_broker'] = False
        if payload['is_carried_gtt']:
            carried += 1

        pairs = set()
        for split in splits:
            broker_id = (split.broker_order_id or '').strip()
            if broker_id:
                pairs.add((split.account_id, broker_id))
        payload['_broker_pairs'] = pairs
        payload['_accounts'] = {split.account_id for split in splits}
        # A GTT is held in the broker's GTT book, not its order book, and so
        # carries a trigger id where a regular order carries an order id.
        # Looking for it among the orders and not finding it proves nothing,
        # and the screen was saying NOT AT BROKER about instructions that were
        # sitting at the broker exactly as placed.
        payload['_gtt_accounts'] = {
            split.account_id for split in splits
            if (split.broker_gtt_id or '').strip()
        }
        claimed |= pairs
        rows.append(payload)

    today_only = filters['date_from'] is None and filters['date_to'] is None
    unreadable = []
    external = 0
    not_at_broker = 0
    book_age = 0.0

    # Only TODAY is merged. No broker will serve last Tuesday's order book, so
    # for any explicit earlier window the stored record is the whole record and
    # asking would only add latency and a misleading 'unverified' badge.
    if today_only and merge_broker:
        books, unreadable, _accounts, book_age = _books_for_screen(
            filters['account_id'], allow_cached=books_may_be_cached
        )
        index = _broker_order_index(books)
        readable = set(books.keys())

        for row in rows:
            pairs = row.pop('_broker_pairs', set())
            # An account whose leg is a GTT is accounted for. It is excluded
            # from the judgement rather than counted as present, so a mixed
            # order is still judged on the legs that ARE regular orders.
            touched = (row.get('_accounts') or set()) - (
                row.pop('_gtt_accounts', set()) or set()
            )
            # Only judge an order against accounts that actually answered.
            checkable = {pair for pair in pairs if pair[0] in readable}
            if checkable:
                row['not_at_broker'] = not any(pair in index for pair in checkable)
            elif pairs:
                row['not_at_broker'] = False
            else:
                # No broker order id anywhere. Nothing was ever accepted, which
                # is a fact AlgoMirror already knows, so say it plainly rather
                # than letting it read as live.
                row['not_at_broker'] = bool(touched & readable)
            if row['not_at_broker']:
                not_at_broker += 1

        groups = {}
        for (account_id, broker_id), entry in index.items():
            if (account_id, broker_id) in claimed:
                continue
            groups.setdefault(_external_group_key(entry), []).append(
                (account_id, entry)
            )
        for key, members in groups.items():
            row = _external_order_row(key, members, directory, books)
            # Filtered here because the query above could not see this row.
            if not _external_matches(row, filters):
                continue
            rows.append(row)
            external += 1

        rows.sort(key=lambda row: (row.get('placed_at') or ''), reverse=True)
    else:
        for row in rows:
            row.pop('_broker_pairs', None)
            row.pop('_gtt_accounts', None)

    # One action, one row. An exit is created per holding and a holding belongs
    # to one account, so the monitor breaching a stock on two accounts makes two
    # parent orders - and so does a manual Sell across two accounts. The book
    # shows one row per thing the admin did, with the accounts behind View
    # Split, and this is where that is put back together.
    rows = _merge_same_action(rows)
    for row in rows:
        row.pop('_accounts', None)
        row.pop('_gtt_accounts', None)

    if sort_by_status:
        _sort_by_prd_status(rows)

    gtt_rows = []
    if split_gtt:
        resting = [row for row in rows if _is_resting_gtt(row)]
        if resting:
            gtt_rows = resting
            rows = [row for row in rows if not _is_resting_gtt(row)]

    return {
        'orders': rows,
        'gtt_orders': gtt_rows,
        'totals': {
            'orders': len(rows),
            'quantity': sum(row['total_quantity'] for row in rows),
            'filled_quantity': sum(row['filled_quantity'] for row in rows),
            'open_orders': sum(1 for row in rows if row['is_open']),
            # The five the F&O module shows, counted the same way so the two
            # modules cannot disagree. Buy and Sell partition the list; the
            # three statuses do not, because PARTIAL is its own thing and is
            # counted as Open, which is what it is.
            'buy_orders': sum(
                1 for row in rows if row.get('side') == EQUITY_SIDE_BUY
            ),
            'sell_orders': sum(
                1 for row in rows if row.get('side') == EQUITY_SIDE_SELL
            ),
            'completed_orders': sum(
                1 for row in rows
                if row.get('status') == EQUITY_ORDER_STATUS_COMPLETED
            ),
            'cancelled_orders': sum(
                1 for row in rows
                if row.get('status') == EQUITY_ORDER_STATUS_CANCELLED
            ),
            'carried_gtt_orders': carried,
            'gtt_orders': len(gtt_rows),
            'placed_outside': external,
            'not_at_broker': not_at_broker,
        },
        'filters': _filters_echo(filters),
        'options': _filter_options(),
        'window': {
            'today_only': today_only,
            'carries_open_gtt': carry_open_gtt,
            'today': _iso(datetime.utcnow().date()),
            'broker_merged': bool(today_only and merge_broker),
            'unverified_accounts': sorted(unreadable),
            # How old the broker book behind this list is, in seconds. Zero
            # means it was read for this request. A caller that accepts a
            # cached book is told what it accepted.
            'broker_book_age_seconds': round(book_age, 1),
        },
        'sort_order': list(VALID_ORDER_STATUSES) if sort_by_status else 'placed_at_desc',
        'generated_at': _iso(datetime.utcnow()),
    }


def _merge_same_fill(rows):
    """
    Fold fills from one action into a single row.

    Same rule as the Order Book's: same stock, side, type, source and nature,
    close in time, and DISJOINT accounts. That last condition is what keeps two
    genuine fills on one account - a partial that filled in two goes - as two
    rows, which they are.

    Each merged row keeps the accounts that made it up, so the screen can show
    who traded without a second request.
    """
    merged = []
    groups = {}

    for row in rows:
        key = (
            row.get('symbol'), row.get('exchange'), row.get('side'),
            row.get('order_type'), row.get('source'), row.get('trade_nature_id'),
            bool(row.get('placed_outside')),
            # In the key, not merely carried on the row. Two orders for the
            # same stock a second apart with different stop losses are two
            # different decisions, and folding them into one line would show
            # one of the two stop losses and silently drop the other.
            row.get('stop_loss'), row.get('target'),
        )
        accounts = row.get('_accounts') or set()
        stamp = row.get('executed_at') or ''

        target = None
        for candidate in groups.get(key, []):
            if candidate['_accounts'] & accounts:
                continue
            first = candidate.get('executed_at') or ''
            if first and stamp:
                try:
                    gap = abs(
                        (datetime.fromisoformat(stamp)
                         - datetime.fromisoformat(first)).total_seconds()
                    )
                except ValueError:
                    gap = 0
                if gap > SAME_ACTION_SECONDS:
                    continue
            target = candidate
            break

        if target is None:
            row['accounts'] = [{
                'account_id': row.get('account_id'),
                'account_name': row.get('account_name'),
                'broker_name': row.get('broker_name'),
                'executed_quantity': row.get('executed_quantity'),
                'execution_price': row.get('execution_price'),
                'broker_order_id': row.get('broker_order_id'),
            }]
            row['accounts_count'] = 1
            groups.setdefault(key, []).append(row)
            merged.append(row)
            continue

        quantity = row.get('executed_quantity') or 0
        target['executed_quantity'] = (target.get('executed_quantity') or 0) + quantity
        target['trade_value'] = _money(
            (target.get('trade_value') or 0) + (row.get('trade_value') or 0)
        )
        target['_accounts'] |= accounts
        target['accounts'].append({
            'account_id': row.get('account_id'),
            'account_name': row.get('account_name'),
            'broker_name': row.get('broker_name'),
            'executed_quantity': quantity,
            'execution_price': row.get('execution_price'),
            'broker_order_id': row.get('broker_order_id'),
        })
        target['accounts_count'] = len(target['accounts'])

        # The row now speaks for more than one account, so the single account
        # name on it would be a half truth.
        target['account_name'] = None
        target['broker_name'] = None
        target['account_id'] = None

        # A weighted average, because two accounts rarely fill at the same tick.
        total = target['executed_quantity']
        if total:
            target['execution_price'] = _money(
                sum(
                    (entry.get('execution_price') or 0)
                    * (entry.get('executed_quantity') or 0)
                    for entry in target['accounts']
                ) / total
            )

        if row.get('executed_at') and (
            not target.get('executed_at') or row['executed_at'] < target['executed_at']
        ):
            target['executed_at'] = row['executed_at']

    return merged


def _external_trade_row(account_id, entry, directory):
    """
    One fill the broker reports against an order AlgoMirror never placed.

    Hollow where AlgoMirror would have known something, for the same reason an
    external order row is: nobody told this application why the trade happened,
    and a guessed trade nature is worse than a blank one.
    """
    from app.utils.equity_fill_reconciler import broker_time_to_utc

    account = (directory or {}).get(account_id) or {}
    quantity = _to_int(entry.get('quantity'))
    price = _to_float(
        entry.get('average_price')
        if entry.get('average_price') is not None
        else entry.get('price')
    )
    # IST from the broker, unmarked, converted to the UTC every screen expects.
    executed_at = broker_time_to_utc(
        entry.get('timestamp') or entry.get('filltime')
    )
    return {
        'trade_id': None,
        'split_id': None,
        'order_id': None,
        'source_of_row': 'broker',
        'placed_outside': True,
        'account_id': account_id,
        'account_name': account.get('account_name'),
        'broker_name': account.get('broker_name'),
        'symbol': str(entry.get('symbol') or '').strip().upper(),
        'exchange': str(entry.get('exchange') or '').strip().upper(),
        'side': str(entry.get('action') or entry.get('side') or '').strip().upper(),
        'order_type': str(entry.get('pricetype') or '').strip().upper(),
        'product': str(entry.get('product') or '').strip().upper(),
        'source': None,
        'trade_nature_id': None,
        'trade_nature': None,
        # Placed outside AlgoMirror, so there is no order of ours behind it and
        # no levels to show. Blank, not zero.
        'stop_loss': None,
        'target': None,
        'execution_price': _money(price),
        'executed_quantity': quantity,
        'trade_value': _money(turnover(price, quantity)),
        'executed_at': _iso(executed_at) if executed_at else None,
        'broker_trade_id': None,
        'broker_order_id': str(
            entry.get('orderid') or entry.get('order_id') or ''
        ).strip(),
        # Spelled as AlgoMirror spells it. The broker says COMPLETE, and a
        # column reading COMPLETE on one row and COMPLETED on the next is one
        # word describing one thing; it also has to match what the Status
        # filter sends.
        'order_status': EQUITY_ORDER_STATUS_COMPLETED,
        'order_placed_at': None,
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
        query = query.filter(
            EquityOrder.symbol.ilike(_like_contains(filters['symbol']), escape='\\')
        )
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

    # How many accounts the parent order was split across. The fill can then
    # say "2 of 2" the way the Order Book does, instead of naming one account
    # and leaving the reader to wonder whether that was all of them.
    order_ids = {order.id for _trade, _split, order in records}
    order_accounts = {}
    if order_ids:
        counted = db.session.query(
            EquityOrderSplit.equity_order_id,
            db.func.count(EquityOrderSplit.id)
        ).filter(
            EquityOrderSplit.equity_order_id.in_(order_ids)
        ).group_by(EquityOrderSplit.equity_order_id).all()
        order_accounts = {order_id: count for order_id, count in counted}

    rows = []
    for trade, split, order in records:
        payload = _trade_payload(trade, split, order, directory)
        payload['source_of_row'] = 'algomirror'
        payload['placed_outside'] = False
        payload['order_accounts'] = order_accounts.get(order.id, 1)
        payload['_accounts'] = {split.account_id}
        rows.append(payload)

    today_only = filters['date_from'] is None and filters['date_to'] is None
    unreadable = []
    external = 0

    if today_only:
        # Every broker order id AlgoMirror can account for today. Built from
        # the splits rather than from the fills above, because a split that has
        # not been filled yet still owns its order id and its fills must not be
        # listed as somebody else's.
        claimed = set()
        split_rows = db.session.query(
            EquityOrderSplit.account_id, EquityOrderSplit.broker_order_id
        ).join(
            EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == current_user.id,
            EquityOrderSplit.broker_order_id.isnot(None),
        ).all()
        for account_id, broker_id in split_rows:
            text = (broker_id or '').strip()
            if text:
                claimed.add((account_id, text))

        books, unreadable, _accounts, _book_age = _books_for_screen(
            filters['account_id']
        )
        for account_id, book in books.items():
            for entry in book.get('trades') or []:
                broker_id = str(
                    entry.get('orderid') or entry.get('order_id') or ''
                ).strip()
                if broker_id and (account_id, broker_id) in claimed:
                    continue
                row = _external_trade_row(account_id, entry, directory)
                # Stock and Side were already honoured here; Order Type, Status
                # and Trade Nature were not, so a narrowed screen still showed
                # outside fills it had been asked to hide.
                if not _external_matches(row, filters):
                    continue
                # Nothing placed it through AlgoMirror, so there is no parent
                # order and no number of accounts it was meant to cover.
                row['order_accounts'] = 1
                row['_accounts'] = {account_id}
                rows.append(row)
                external += 1

        rows.sort(key=lambda row: (row.get('executed_at') or ''), reverse=True)

    # One fill event, one row. The exit monitor sells each account separately,
    # so a single exit produces one fill per account seconds apart. Folded the
    # same way the Order Book folds the orders behind them, with the accounts
    # in the row's own detail.
    rows = _merge_same_fill(rows)

    # Counted before the working key is dropped. Accounts is how many accounts
    # actually traded today, taken from the fills themselves rather than from
    # the rows - the rows are folded one per action, so counting them would
    # undercount an action that reached two accounts.
    traded_accounts = set()
    for row in rows:
        traded_accounts |= (row.get('_accounts') or set())

    for row in rows:
        row.pop('_accounts', None)

    return {
        'trades': rows,
        'totals': {
            'trades': len(rows),
            'quantity': sum(row['executed_quantity'] for row in rows),
            'value': _money(sum(row['trade_value'] for row in rows)),
            # Buy and Sell count the rows, which is what is on the screen: one
            # row is one thing that was done, whatever number of accounts it
            # reached.
            'accounts': len(traded_accounts),
            'buy_fills': sum(
                1 for row in rows if row.get('side') == EQUITY_SIDE_BUY
            ),
            'sell_fills': sum(
                1 for row in rows if row.get('side') == EQUITY_SIDE_SELL
            ),
            'placed_outside': external,
        },
        'filters': _filters_echo(filters),
        'options': _filter_options(),
        'window': {
            'today_only': today_only,
            'today': _iso(datetime.utcnow().date()),
            'broker_merged': bool(today_only),
            'unverified_accounts': sorted(unreadable),
        },
        'generated_at': _iso(datetime.utcnow()),
    }


# ---------------------------------------------------------------------------
# Equity preferences
# ---------------------------------------------------------------------------

def _minute_to_clock(minute):
    """Minutes past midnight as HH:MM, which is how a person reads a time."""
    value = _to_int(minute)
    if value < 0:
        value = 0
    return '%02d:%02d' % (value // 60, value % 60)


def _read_clock(data, key, minimum, maximum):
    """
    Read an HH:MM from a request body and return minutes past midnight.

    Refuses anything it cannot read rather than falling back to a default. A
    square-off time that quietly became something else is not a setting the
    admin can trust, and this one decides when a position closes.
    """
    raw = str(data.get(key) or '').strip()
    if not raw:
        raise _BadRequest('%s is required as HH:MM' % key)

    parts = raw.split(':')
    if len(parts) != 2:
        raise _BadRequest('%s must look like 15:12' % key)
    try:
        hour, minute = int(parts[0]), int(parts[1])
    except (TypeError, ValueError):
        raise _BadRequest('%s must look like 15:12' % key)
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise _BadRequest('%s is not a real time of day' % key)

    value = hour * 60 + minute
    if value < minimum or value > maximum:
        raise _BadRequest(
            '%s must be between %s and %s' % (
                key, _minute_to_clock(minimum), _minute_to_clock(maximum)
            )
        )
    return value


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

    intraday = {'available': False}
    try:
        from app.utils.equity_intraday_monitor import equity_intraday_monitor
        intraday = equity_intraday_monitor.status()
        intraday['available'] = True
    except Exception as exc:
        current_app.logger.debug(f'Equity intraday monitor status unavailable: {exc}')

    # How many shorts are open RIGHT NOW. On the settings page because this is
    # where the square-off is turned off, and turning it off with something
    # open is a decision that should be taken with the number in front of you.
    open_shorts = 0
    try:
        open_shorts = EquityIntradayShort.query.filter(
            EquityIntradayShort.user_id == current_user.id,
            EquityIntradayShort.status == EQUITY_SHORT_STATUS_OPEN,
            EquityIntradayShort.quantity > 0,
        ).count()
    except Exception as exc:
        current_app.logger.debug(f'Could not count open shorts: {exc}')

    return {
        'settings': {
            'insufficient_funds_action': settings.insufficient_funds_action,
            'default_exit_mode': settings.default_exit_mode,
            'sl_monitor_enabled': bool(settings.sl_monitor_enabled),
            'sl_monitor_interval_seconds': _to_int(settings.sl_monitor_interval_seconds),
            'order_timeout_seconds': _to_int(settings.order_timeout_seconds),
            'price_alerts_enabled': bool(settings.price_alerts_enabled),
            'monitor_last_run_at': _iso(settings.monitor_last_run_at),
            'monitor_last_error': settings.monitor_last_error,
            # The intraday short controls. Kept as HH:MM here rather than as
            # minutes past midnight, because these are the two numbers on this
            # page that decide when a position CLOSES, and a person should be
            # able to read them without doing arithmetic.
            'intraday_monitor_enabled': bool(
                getattr(settings, 'intraday_monitor_enabled', True)
            ),
            'intraday_cutoff_at': _minute_to_clock(
                getattr(settings, 'intraday_cutoff_minute', 15 * 60)
            ),
            'intraday_squareoff_at': _minute_to_clock(
                getattr(settings, 'intraday_squareoff_minute', 15 * 60 + 12)
            ),
            'intraday_last_run_at': _iso(
                getattr(settings, 'intraday_last_run_at', None)
            ),
            'intraday_last_error': getattr(settings, 'intraday_last_error', None),
            'updated_at': _iso(settings.updated_at),
        },
        'monitor': monitor,
        'intraday_monitor': intraday,
        'open_shorts': open_shorts,
        'options': {
            'insufficient_funds_actions': list(VALID_FUNDS_ACTIONS),
            'exit_modes': list(VALID_EXIT_MODES),
            'monitor_interval_seconds': {
                'minimum': MIN_MONITOR_INTERVAL_SECONDS,
                'maximum': MAX_MONITOR_INTERVAL_SECONDS,
            },
            'order_timeout_seconds': {
                'minimum': MIN_ORDER_TIMEOUT_SECONDS,
                'maximum': MAX_ORDER_TIMEOUT_SECONDS,
            },
            'intraday_times': {
                'minimum': _minute_to_clock(MIN_INTRADAY_MINUTE),
                'maximum': _minute_to_clock(MAX_SQUAREOFF_MINUTE),
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
    active = _default_watchlist()
    return render_template(
        'equity/watchlist.html',
        accounts=_active_accounts(),
        trade_natures=_trade_natures(),
        watchlists=_all_watchlists(),
        active_watchlist=active
    )


@equity_bp.route('/alerts')
@login_required
def alerts():
    """
    Kept only so an old link or bookmark still lands somewhere useful.

    This was a screen of its own with two tabs. The alerts you have set are now
    on the Watch List, which is where the stocks they belong to already were.
    The log of what has happened is under Settings, as Notifications, because
    two of its three sources are about holdings rather than watch lists and all
    of them answer a question asked after the fact.

    Redirected rather than deleted: a route that vanishes gives whoever
    followed the link a 404 and no idea where the screen went.

    equity/alerts.html is no longer rendered by anything.
    """
    return redirect(url_for('equity.settings'))


@equity_bp.route('/positions')
@login_required
def positions():
    """
    Today's open positions, the step between placing an order and holding stock.

    Data is loaded by the browser from /equity/api/positions.
    """
    return render_template('equity/positions.html', accounts=_active_accounts())


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

    ?watchlist_id= selects a named list. Left out, the default list is shown.
    """
    return _ok(_build_watchlist_payload(watchlist_id=_query_watchlist_id()))


@equity_bp.route('/api/watchlist/quotes')
@login_required
@heavy_rate_limit()
@_json_route
def api_watchlist_quotes():
    """
    The 10 second price refresh for the watch list rows.

    Returns the same row shape as /equity/api/watchlist so the screen can
    replace rows in place, plus any price alerts that fired on this pass.

    ?watchlist_id= keeps the refresh on whichever list the screen is showing.
    """
    return _ok(_build_watchlist_payload(watchlist_id=_query_watchlist_id()))


# ---------------------------------------------------------------------------
# Fired price alerts. Written by the background monitor, collected from here by
# whichever equity screen happens to be open.
# ---------------------------------------------------------------------------

# How fresh an alert has to be to interrupt with a popup. This is deliberately
# short. An alert that fired two hours ago is not news any more, and popping a
# morning's worth of them the moment the module is opened in the afternoon
# means four visible at a time and the rest scrolling past unread. Anything
# older stays unread and is counted on the Alerts badge instead, which is what
# the Log is for.
ALERT_POPUP_WINDOW_MINUTES = 5

# The most alerts one poll will hand over.
MAX_PENDING_ALERTS = 20

# How much of the Log is kept on screen.
ALERT_LOG_DAYS = 30
MAX_LOG_ROWS = 200

# The three states an alert can be in, matching what the Alerts screen shows.
ALERT_STATUS_ACTIVE = 'ACTIVE'
ALERT_STATUS_TRIGGERED = 'TRIGGERED'
ALERT_STATUS_PAUSED = 'PAUSED'


def _notice_row(notice):
    """
    One holding notice, shaped like an alert row so the same screen, the same
    pop-up and the same badge can carry it without knowing the difference.

    'source' is what tells them apart. A price alert is a statement about a
    price you asked to watch; a notice is a statement that shares moved without
    an order from here. They belong in the same feed because the admin should
    have one place to look, and they carry different fields because they are
    different events.
    """
    return {
        'id': notice.id,
        'source': 'holding',
        'kind': notice.kind,
        'symbol': notice.symbol,
        'exchange': notice.exchange,
        'account_id': notice.account_id,
        'quantity_before': _to_int(notice.quantity_before),
        'quantity_after': _to_int(notice.quantity_after),
        'quantity_delta': _to_int(notice.quantity_delta),
        'had_armed_level': bool(notice.had_armed_level),
        # Kept so a screen written for alerts does not have to special-case
        # every field it reads.
        'alert_price': None,
        'alert_direction': None,
        'ltp': None,
        'message': notice.message,
        'created_at': _iso(notice.created_at),
    }


def _alert_event_row(event):
    """One fired price alert, in the shape the feed carries."""
    return {
        'id': event.id,
        'source': 'alert',
        'kind': None,
        'symbol': event.symbol,
        'exchange': event.exchange,
        'account_id': None,
        'alert_price': _money(event.alert_price),
        'alert_direction': event.alert_direction,
        'ltp': _money(event.ltp),
        'message': event.message,
        'created_at': _iso(event.created_at),
    }


@equity_bp.route('/api/alerts/pending')
@login_required
@api_rate_limit()
@_json_route
def api_pending_alerts():
    """
    Price alerts fresh enough to interrupt with a popup.

    Every equity screen polls this, which is what makes an alert reach the admin
    whatever page they happen to be on. Only the last few minutes are offered:
    an older alert is not news, and is left unread so it is counted on the
    Alerts badge and read in the Log instead of fighting for the corner.

    The rows are not marked here. A poll that is answered but never arrives
    would lose the alert in silence, so the screen acknowledges what it actually
    displayed.

    Response: {"status", "message", "alerts": [
        {"id", "symbol", "exchange", "alert_price", "alert_direction", "ltp",
         "message", "created_at"}], "count"}
    """
    cutoff = datetime.utcnow() - timedelta(minutes=ALERT_POPUP_WINDOW_MINUTES)

    events = EquityAlertEvent.query.filter(
        EquityAlertEvent.user_id == current_user.id,
        EquityAlertEvent.notified_at.is_(None),
        EquityAlertEvent.created_at >= cutoff,
    ).order_by(EquityAlertEvent.created_at.asc()).limit(MAX_PENDING_ALERTS).all()

    notices = EquityHoldingNotice.query.filter(
        EquityHoldingNotice.user_id == current_user.id,
        EquityHoldingNotice.notified_at.is_(None),
        EquityHoldingNotice.created_at >= cutoff,
    ).order_by(EquityHoldingNotice.created_at.asc()).limit(MAX_PENDING_ALERTS).all()

    rows = [_alert_event_row(event) for event in events]
    rows += [_notice_row(notice) for notice in notices]
    # Oldest first, so a burst arrives in the order it happened.
    rows.sort(key=lambda row: row['created_at'] or '')
    rows = rows[:MAX_PENDING_ALERTS]

    return _ok({
        'alerts': rows,
        'count': len(rows),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/alerts/acknowledge', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_acknowledge_alerts():
    """
    Mark alerts as shown, so they are not raised again on the next poll.

    Body: {"ids": [1, 2, 3], "notice_ids": [4, 5]}

    The two lists are separate because they are separate tables. An id is only
    meaningful alongside the source it came from, and quietly merging them would
    mark the wrong rows read.

    Ids that are not this admin's, or are already acknowledged, are ignored
    rather than refused: two screens open at once will both display the same
    alert and both acknowledge it, and that is not an error.
    """
    data = _body()

    def read_ids(field):
        raw = data.get(field)
        if raw is None:
            return []
        if not isinstance(raw, list):
            raise _BadRequest(f'{field} must be a list of ids')
        if len(raw) > MAX_PENDING_ALERTS:
            raise _BadRequest(f'No more than {MAX_PENDING_ALERTS} ids at a time')
        out = []
        for value in raw:
            try:
                out.append(int(value))
            except (TypeError, ValueError):
                raise _BadRequest('Every id must be a whole number')
        return out

    ids = read_ids('ids')
    notice_ids = read_ids('notice_ids')

    if not ids and not notice_ids:
        return _ok({'acknowledged': 0})

    stamp = datetime.utcnow()
    marked = 0

    if ids:
        marked += EquityAlertEvent.query.filter(
            EquityAlertEvent.user_id == current_user.id,
            EquityAlertEvent.id.in_(ids),
            EquityAlertEvent.notified_at.is_(None),
        ).update({'notified_at': stamp}, synchronize_session=False) or 0

    if notice_ids:
        marked += EquityHoldingNotice.query.filter(
            EquityHoldingNotice.user_id == current_user.id,
            EquityHoldingNotice.id.in_(notice_ids),
            EquityHoldingNotice.notified_at.is_(None),
        ).update({'notified_at': stamp}, synchronize_session=False) or 0

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity alert acknowledge failed: {exc}')
        return _json_error('The alerts could not be marked as seen', 500)

    return _ok({'acknowledged': int(marked)})


@equity_bp.route('/api/alerts/unread')
@login_required
@api_rate_limit()
@_json_route
def api_unread_alert_count():
    """
    How many fired alerts have not been read yet, for the badge in the menu.

    An alert counts as read once it has either popped up on a screen or been
    seen in the Log, which is the same notified_at marker in both cases.
    """
    cutoff = datetime.utcnow() - timedelta(days=ALERT_LOG_DAYS)

    count = EquityAlertEvent.query.filter(
        EquityAlertEvent.user_id == current_user.id,
        EquityAlertEvent.notified_at.is_(None),
        EquityAlertEvent.created_at >= cutoff,
    ).count()

    count += EquityHoldingNotice.query.filter(
        EquityHoldingNotice.user_id == current_user.id,
        EquityHoldingNotice.notified_at.is_(None),
        EquityHoldingNotice.created_at >= cutoff,
    ).count()

    # Holdings waiting for a confirm decision, for the badge on the Holdings
    # menu. Answered on this poll rather than a second one: both badges are
    # refreshed on the same tick, and one round trip is enough for two counts.
    #
    # Not time limited, unlike the alert counts above. An unread alert stops
    # being news; a breached holding waiting for an answer does not, and it
    # stays on the badge until it is actually dealt with.
    confirm_pending = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.exit_status == EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,
    ).count()

    # Alerts that have fired and are still sitting fired, across EVERY watch
    # list, for the badge on the Watch List menu. Same shape as the confirm
    # badge above and for the same reason: it is a queue of rows waiting for a
    # decision - re-arm it, move it, or leave it - not a notification, so it is
    # not time limited and does not clear itself by being read.
    alerts_triggered = EquityWatchlistItem.query.filter(
        EquityWatchlistItem.user_id == current_user.id,
        EquityWatchlistItem.alert_triggered_at.isnot(None),
    ).count()

    # Stop losses and targets that have been breached, for the two badges on
    # the Holdings menu - red for a stop loss, green for a target, the owner's
    # pairing everywhere in this module.
    #
    # Counted BY STOCK, not by account row, and a stock whose stop loss fired
    # is counted ONLY as a stop loss even if a target fired on a different
    # account. Both of those match exactly how the chips above the Holdings
    # table count, so the badge and the chip can never quote two numbers for
    # the same thing.
    #
    # Not time limited, like the confirm count above and for the same reason: a
    # breached level does not stop being true by being looked at. It leaves the
    # badge when the level is saved afresh, which is what clears the stamp.
    sl_hits = set()
    tp_hits = set()
    for symbol, exchange, sl_at, tp_at in EquityHolding.query.with_entities(
        EquityHolding.symbol,
        EquityHolding.exchange,
        EquityHolding.sl_hit_at,
        EquityHolding.tp_hit_at,
    ).filter(
        EquityHolding.user_id == current_user.id,
        EquityHolding.quantity > 0,
    ).all():
        key = (
            str(symbol or '').strip().upper(),
            str(exchange or 'NSE').strip().upper(),
        )
        if sl_at is not None:
            sl_hits.add(key)
        elif tp_at is not None:
            tp_hits.add(key)

    # A stock with a stop loss hit on one account and a target hit on another
    # belongs to the stop loss and to nothing else. Done as a second pass
    # because the rows arrive in no particular order, so a target seen first
    # would otherwise claim a stock a later stop loss also claims.
    tp_hits -= sl_hits

    return _ok({
        'unread': int(count),
        'confirm_pending': int(confirm_pending),
        'alerts_triggered': int(alerts_triggered),
        'sl_hit': len(sl_hits),
        'tp_hit': len(tp_hits),
    })


def _alert_status(item):
    """
    Which of the three states one alert is in.

    TRIGGERED wins over PAUSED, and that is a REVERSAL of what this function
    used to do. The old order was right when it was written: an alert switched
    off after firing had been switched off by a person, and calling it
    triggered would have implied it was waiting to be re-armed.

    Firing now switches the alert off by itself. So every triggered alert is
    also disabled, testing disabled first matched every one of them, and the
    Triggered filter could only ever report zero - which is exactly what it did
    on a screen showing an alert that had plainly fired.

    Being off is now a CONSEQUENCE of firing rather than a separate choice, so
    the fact worth reporting is the firing. An alert switched off by hand and
    never fired still reads as paused, because its triggered stamp is empty.
    """
    if item.alert_triggered_at is not None:
        return ALERT_STATUS_TRIGGERED
    if not item.price_alert_enabled:
        return ALERT_STATUS_PAUSED
    return ALERT_STATUS_ACTIVE


def _alert_row_payload(item, list_names):
    return {
        'item_id': item.id,
        'symbol': item.symbol,
        'exchange': item.exchange,
        'watchlist_id': item.watchlist_id,
        'watchlist_name': list_names.get(item.watchlist_id, ''),
        'alert_price': _money(item.alert_price) if item.alert_price is not None else None,
        'alert_direction': item.alert_direction,
        'target_price': (
            _money(item.target_price) if item.target_price is not None else None
        ),
        'enabled': bool(item.price_alert_enabled),
        'status': _alert_status(item),
        'triggered_at': _iso(item.alert_triggered_at),
        'triggered_price': (
            _money(item.alert_triggered_price)
            if item.alert_triggered_price is not None else None
        ),
        'created_at': _iso(item.created_at),
    }


@equity_bp.route('/api/alerts')
@login_required
@api_rate_limit()
@_json_route
def api_alerts():
    """
    Every alert this admin has set, across every watch list.

    The Watch List screen shows one list at a time, so an alert set on a list
    that is not open is invisible there. This is the one place that answers
    "what am I actually watching for", which is what the screen is for.

    Response: {"status", "message", "alerts": [...], "counts": {...}}
    """
    lists = _all_watchlists()
    list_names = {row.id: row.name for row in lists}

    items = EquityWatchlistItem.query.filter(
        EquityWatchlistItem.user_id == current_user.id,
        EquityWatchlistItem.alert_price.isnot(None),
    ).order_by(EquityWatchlistItem.symbol.asc()).all()

    rows = [_alert_row_payload(item, list_names) for item in items]

    counts = {
        ALERT_STATUS_ACTIVE: 0,
        ALERT_STATUS_TRIGGERED: 0,
        ALERT_STATUS_PAUSED: 0,
    }
    for row in rows:
        counts[row['status']] = counts.get(row['status'], 0) + 1

    return _ok({
        'alerts': rows,
        'counts': counts,
        'count': len(rows),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/alerts/<int:item_id>/rearm', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_rearm_alert(item_id):
    """
    Put a triggered alert back on watch at the same price.

    An alert fires once and then stops, so this is what makes it live again. It
    is the same clearing that any edit to the alert performs, done on its own so
    a level that is still the right level does not have to be retyped.
    """
    item = _owned_watchlist_item(item_id)
    if item is None:
        return _json_error('Watch list item not found', 404)
    if item.alert_price is None:
        raise _BadRequest('That row has no alert price to re-arm')

    _clear_watchlist_alert(item)
    item.price_alert_enabled = True

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity alert re-arm failed: {exc}')
        return _json_error('The alert could not be re-armed', 500)

    _log_activity('equity_alert_rearmed', {
        'item_id': item.id, 'symbol': item.symbol
    })
    return _ok({'alert': _alert_row_payload(item, {})},
               f'{item.symbol} alert is watching again')


@equity_bp.route('/api/alerts/<int:item_id>/enabled', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_set_alert_enabled(item_id):
    """
    Pause or resume one alert.

    Body: {"enabled": true|false}

    Resuming re-arms as well. An alert switched back on is one the admin wants
    watching, and leaving it on with a triggered stamp still set would be a
    switch that appears to do nothing.
    """
    item = _owned_watchlist_item(item_id)
    if item is None:
        return _json_error('Watch list item not found', 404)
    if item.alert_price is None:
        raise _BadRequest('That row has no alert price')

    data = _body()
    if 'enabled' not in data:
        raise _BadRequest('enabled is required')
    enabled = bool(data.get('enabled'))

    item.price_alert_enabled = enabled
    if enabled:
        _clear_watchlist_alert(item)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity alert pause failed: {exc}')
        return _json_error('The alert could not be changed', 500)

    return _ok(
        {'alert': _alert_row_payload(item, {})},
        f'{item.symbol} alert {"resumed" if enabled else "paused"}'
    )


@equity_bp.route('/api/alerts/log')
@login_required
@api_rate_limit()
@_json_route
def api_alert_log():
    """
    Every alert that has actually fired, newest first.

    This is the record the popup is not: a popup is gone in a few seconds and
    was never going to be there at all if the module was closed when the level
    was crossed.

    Response: {"status", "message", "entries": [...], "unread"}
    """
    cutoff = datetime.utcnow() - timedelta(days=ALERT_LOG_DAYS)

    events = EquityAlertEvent.query.filter(
        EquityAlertEvent.user_id == current_user.id,
        EquityAlertEvent.created_at >= cutoff,
    ).order_by(EquityAlertEvent.created_at.desc()).limit(MAX_LOG_ROWS).all()

    notices = EquityHoldingNotice.query.filter(
        EquityHoldingNotice.user_id == current_user.id,
        EquityHoldingNotice.created_at >= cutoff,
    ).order_by(EquityHoldingNotice.created_at.desc()).limit(MAX_LOG_ROWS).all()

    entries = []
    for event in events:
        row = _alert_event_row(event)
        row['unread'] = event.notified_at is None
        entries.append(row)
    for notice in notices:
        row = _notice_row(notice)
        row['unread'] = notice.notified_at is None
        entries.append(row)

    # Newest first across both, then capped, so the log reads as one history
    # rather than two lists stapled together.
    entries.sort(key=lambda row: row['created_at'] or '', reverse=True)
    entries = entries[:MAX_LOG_ROWS]

    return _ok({
        'entries': entries,
        'count': len(entries),
        'days': ALERT_LOG_DAYS,
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/alerts/log/seen', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_mark_alert_log_seen():
    """Mark everything in the Log as read, which clears the badge."""
    cutoff = datetime.utcnow() - timedelta(days=ALERT_LOG_DAYS)

    stamp = datetime.utcnow()

    marked = EquityAlertEvent.query.filter(
        EquityAlertEvent.user_id == current_user.id,
        EquityAlertEvent.notified_at.is_(None),
        EquityAlertEvent.created_at >= cutoff,
    ).update({'notified_at': stamp}, synchronize_session=False) or 0

    marked += EquityHoldingNotice.query.filter(
        EquityHoldingNotice.user_id == current_user.id,
        EquityHoldingNotice.notified_at.is_(None),
        EquityHoldingNotice.created_at >= cutoff,
    ).update({'notified_at': stamp, 'seen_at': stamp},
             synchronize_session=False) or 0

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity alert log mark failed: {exc}')
        return _json_error('The log could not be marked as read', 500)

    return _ok({'marked': int(marked)})


# ---------------------------------------------------------------------------
# The watch list as a spreadsheet
# ---------------------------------------------------------------------------
#
# Download, edit in Excel, upload back. CSV rather than a real .xlsx, on the
# owner's call: Excel opens and saves it natively and it costs no new library.
#
# Two rules shape everything below, and both are about the upload rather than
# the download.
#
# 1. NOTHING IS WRITTEN UNTIL THE PLAN HAS BEEN SEEN. The upload is two steps -
#    preview, then apply - because a file can carry fifty changes and a person
#    should read what those are before any of them happen.
#
# 2. AN UPLOAD CAN ADD AND CHANGE. IT CANNOT REMOVE. A stock missing from the
#    file is left alone, deliberately: download, filter to five rows in Excel,
#    edit one, upload - the honest reading of that is "update these five", not
#    "delete the other forty". Removing a stock stays a deliberate act on the
#    screen, one at a time, where it already is.

# Symbol, Exchange and Watch List identify the row rather than being edited:
# together they are the same key the screen enforces. Change one and you have
# named a different stock, which is an ADD, not an edit.
WATCHLIST_KEY_COLUMNS = ('symbol', 'exchange', 'watch list')

# What a person may actually change in the file.
WATCHLIST_EDIT_COLUMNS = (
    'trade nature', 'target price', 'alert price', 'alert direction',
    'alert on', 'thesis', 'risk', 'to watch',
)

WATCHLIST_IMPORT_COLUMNS = WATCHLIST_KEY_COLUMNS + WATCHLIST_EDIT_COLUMNS

# The note columns, and the field each one writes. Absent from the file, the
# note is not touched at all; present and blank, it is cleared - which is the
# same rule every other column follows and is stated on the screen because a
# cleared note cannot be retyped from memory.
NOTE_IMPORT_COLUMNS = (
    ('thesis', 'thesis'),
    ('risk', 'risk'),
    ('to watch', 'to_watch'),
)

# What the download writes. The live figures carry "(read only)" in their own
# heading, so the file says which half of itself matters.
WATCHLIST_EXPORT_HEADERS = (
    'Symbol', 'Exchange', 'Watch List', 'Trade Nature', 'Target Price',
    'Alert Price', 'Alert Direction', 'Alert On',
    'Thesis', 'Risk', 'To Watch',
    'LTP (read only)', 'Change % (read only)', 'Variance % (read only)',
    'Alert Status (read only)',
)

# A bound on what one upload may carry, so a wrong file cannot become a long
# job. Comfortably above the per-list cap.
MAX_IMPORT_ROWS = 500


def _import_header_key(text):
    """
    Match a column heading however it comes back from Excel.

    Case, spacing, punctuation and the "(read only)" suffix are all discarded,
    so a heading that survived a round trip through a spreadsheet still lands
    on the same field.
    """
    value = str(text or '').strip().lower()
    value = value.split('(')[0]
    keep = []
    for char in value:
        keep.append(char if (char.isalnum() or char.isspace()) else ' ')
    return ' '.join(''.join(keep).split())


def _import_flag(text, default=None):
    """YES / NO / TRUE / FALSE / 1 / 0, as a person actually types them."""
    value = str(text or '').strip().lower()
    if not value:
        return default
    if value in ('y', 'yes', 'true', 't', '1', 'on'):
        return True
    if value in ('n', 'no', 'false', 'f', '0', 'off'):
        return False
    return None


def _import_price(text):
    """
    A price from a spreadsheet cell.

    Returns (value, error). A blank cell is (None, None) and means "no price",
    which is different from a bad one. Rupee signs and thousands commas are
    stripped, because that is what Excel puts there.
    """
    value = str(text or '').strip()
    if not value:
        return None, None
    for junk in ('\u20b9', 'Rs.', 'Rs', ',', ' '):
        value = value.replace(junk, '')
    if not value:
        return None, None
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None, 'is not a number'
    if not math.isfinite(number) or number <= 0:
        return None, 'must be more than zero'
    return round(number, 4), None


def _notes_by_key(keys):
    """The full note text for these stocks, keyed by (symbol, exchange)."""
    wanted = {_note_key(symbol, exchange) for symbol, exchange in (keys or [])}
    if not wanted:
        return {}
    try:
        rows = EquityStockNote.query.filter(
            EquityStockNote.user_id == current_user.id,
            EquityStockNote.symbol.in_(sorted({key[0] for key in wanted})),
        ).all()
    except Exception as exc:
        current_app.logger.debug(f'Could not read notes for the export: {exc}')
        return {}
    found = {}
    for row in rows:
        key = _note_key(row.symbol, row.exchange)
        if key in wanted:
            found[key] = row
    return found


def _watchlist_export_rows(watchlist_id=None):
    """The rows the download writes, in the order the screen shows them."""
    payload = _build_watchlist_payload(watchlist_id=watchlist_id)
    notes = _notes_by_key(
        [(item['symbol'], item['exchange']) for item in payload['items']]
    )
    rows = []
    for item in payload['items']:
        note = notes.get(_note_key(item['symbol'], item['exchange']))
        alert_status = 'Not set'
        if item['alert_price']:
            if not item['price_alert_enabled']:
                alert_status = 'Paused'
            elif item['alert_triggered_at']:
                alert_status = 'Fired'
            else:
                alert_status = 'Active'
        rows.append([
            item['symbol'],
            item['exchange'],
            item['watchlist_name'],
            item['trade_nature'] or '',
            '' if item['target_price'] is None else item['target_price'],
            '' if item['alert_price'] is None else item['alert_price'],
            item['alert_direction'] or '',
            'YES' if item['price_alert_enabled'] else 'NO',
            (note.thesis or '') if note is not None else '',
            (note.risk or '') if note is not None else '',
            (note.to_watch or '') if note is not None else '',
            item['ltp'] if item['has_ltp'] else '',
            item['change_pct'],
            item['variance_pct'] if item['has_variance'] else '',
            alert_status,
        ])
    return payload, rows


def _plan_watchlist_import(csv_text, default_watchlist_id=None):
    """
    Work out what an uploaded file would do, without doing any of it.

    Returns a plan: one entry per row, each saying ADD, CHANGE, UNCHANGED or
    SKIP with the reason. Nothing here writes, so the same call runs for the
    preview and again for the apply - and the apply acts on what it plans
    itself rather than on anything the browser hands back.

    A row is matched to an existing stock by (watch list, symbol, exchange).
    That is the same key the screen enforces, so a round trip of an unedited
    file plans nothing but UNCHANGED, which is the single best test this
    feature has.
    """
    text = str(csv_text or '')
    if not text.strip():
        raise _BadRequest('That file is empty')

    # Excel writes a byte order mark on a CSV saved as UTF-8, and it lands on
    # the first heading. Left there, the Symbol column is never recognised.
    if text.startswith('\ufeff'):
        text = text[1:]

    try:
        reader = csv.reader(io.StringIO(text))
        raw_rows = list(reader)
    except Exception as exc:
        raise _BadRequest('That file could not be read as a CSV: %s' % exc)

    raw_rows = [row for row in raw_rows if any(str(cell).strip() for cell in row)]
    if not raw_rows:
        raise _BadRequest('That file has no rows in it')

    headers = [_import_header_key(cell) for cell in raw_rows[0]]
    index = {}
    for position, name in enumerate(headers):
        if name in WATCHLIST_IMPORT_COLUMNS and name not in index:
            index[name] = position
    if 'symbol' not in index:
        raise _BadRequest(
            'That file has no Symbol column. Download the watch list first and '
            'edit that file, so the headings are the ones this expects.'
        )

    body = raw_rows[1:]
    if len(body) > MAX_IMPORT_ROWS:
        raise _BadRequest(
            'That file has %d rows. The most one upload may carry is %d.'
            % (len(body), MAX_IMPORT_ROWS)
        )

    lists_by_name = {}
    for entry in _all_watchlists():
        lists_by_name[str(entry.name or '').strip().lower()] = entry

    # There is no watch list zero. _to_int returns 0 for a missing value, and
    # the screen sends nothing at all while it is showing All - so a blank and
    # a zero both have to mean "use the default list", not "find list 0".
    default_list = None
    wanted_id = _to_int(default_watchlist_id)
    if default_watchlist_id not in (None, '', WATCHLIST_ALL) and wanted_id > 0:
        default_list = _owned_watchlist(wanted_id)
    if default_list is None:
        default_list = _default_watchlist()

    natures_by_name = {
        str(nature.name or '').strip().lower(): nature
        for nature in _trade_natures()
    }

    existing = {}
    for item in EquityWatchlistItem.query.filter_by(user_id=current_user.id).all():
        existing[(item.watchlist_id,
                  str(item.symbol or '').upper(),
                  str(item.exchange or 'NSE').upper())] = item

    # A file naming a stock you hold is told so and skipped. The watch list
    # does not carry held stocks from 6 September, and a bulk path must not be
    # the one way back in.
    held_now = _held_symbol_keys()

    # Which note columns the file actually carries. A column that is not there
    # means "leave the note alone"; a column that is there and blank means
    # "clear it", the same rule every other column follows.
    note_columns = [
        (heading, field) for heading, field in NOTE_IMPORT_COLUMNS
        if heading in index
    ]
    def raw_cell(row, name):
        position = index.get(name)
        if position is None or position >= len(row):
            return ''
        return str(row[position]).strip()

    existing_notes = {}
    if note_columns:
        # One query for the whole file rather than one per row, from the stocks
        # the file actually names.
        existing_notes = _notes_by_key([
            (raw_cell(row, 'symbol').upper(),
             (raw_cell(row, 'exchange') or 'NSE').upper())
            for row in body if raw_cell(row, 'symbol')
        ])

    counts = EquityWatchlistItem.query.with_entities(
        EquityWatchlistItem.watchlist_id, db.func.count(EquityWatchlistItem.id)
    ).filter_by(user_id=current_user.id).group_by(
        EquityWatchlistItem.watchlist_id
    ).all()
    # How full each list already is, so an ADD can be refused before it is
    # planned rather than after it is half applied.
    used = {watchlist_id: total for watchlist_id, total in counts}

    plan = []
    seen = set()
    # The note belongs to the STOCK, so the same stock on two watch lists shares
    # one note and appears twice in the file. Two rows asking for different note
    # text is a question with no honest answer, so both are refused rather than
    # one quietly winning.
    note_wanted = {}

    cell = raw_cell

    for offset, row in enumerate(body):
        line = offset + 2   # the heading is line 1, as Excel numbers it
        entry = {
            'line': line, 'action': 'SKIP', 'reason': '',
            'symbol': '', 'exchange': '', 'watchlist_name': '',
            'changes': [], 'arms_alert': False,
        }

        symbol = cell(row, 'symbol').upper()
        if not symbol:
            entry['reason'] = 'no symbol in this row'
            plan.append(entry)
            continue
        if len(symbol) > 50:
            entry['symbol'] = symbol[:50]
            entry['reason'] = 'that symbol is too long to be one'
            plan.append(entry)
            continue
        entry['symbol'] = symbol

        exchange = (cell(row, 'exchange') or 'NSE').upper()
        if exchange not in SEARCH_EXCHANGES:
            entry['reason'] = ('exchange %s is not one of %s'
                               % (exchange, ', '.join(SEARCH_EXCHANGES)))
            plan.append(entry)
            continue
        entry['exchange'] = exchange

        list_name = cell(row, 'watch list')
        if list_name:
            target_list = lists_by_name.get(list_name.strip().lower())
            if target_list is None:
                # Refused rather than created. One typo should not produce a
                # phantom watch list nobody meant to make.
                entry['reason'] = ('there is no watch list called "%s"'
                                   % list_name)
                plan.append(entry)
                continue
        else:
            target_list = default_list
        entry['watchlist_name'] = target_list.name
        entry['watchlist_id'] = target_list.id

        if (symbol, exchange) in held_now:
            entry['reason'] = (
                'you hold this stock, so it is on Holdings rather than a watch '
                'list until the last share is sold'
            )
            plan.append(entry)
            continue

        key = (target_list.id, symbol, exchange)
        if key in seen:
            entry['reason'] = 'this stock appears twice in the file'
            plan.append(entry)
            continue
        seen.add(key)

        nature_name = cell(row, 'trade nature')
        nature = None
        if nature_name:
            nature = natures_by_name.get(nature_name.strip().lower())
            if nature is None:
                entry['reason'] = ('there is no trade nature called "%s"'
                                   % nature_name)
                plan.append(entry)
                continue

        target_price, error = _import_price(cell(row, 'target price'))
        if error:
            entry['reason'] = 'the target price %s' % error
            plan.append(entry)
            continue

        alert_price, error = _import_price(cell(row, 'alert price'))
        if error:
            entry['reason'] = 'the alert price %s' % error
            plan.append(entry)
            continue

        direction_text = cell(row, 'alert direction').upper()
        direction = None
        if direction_text:
            if direction_text not in VALID_ALERT_DIRECTIONS:
                entry['reason'] = ('the alert direction must be %s, not "%s"'
                                   % (' or '.join(VALID_ALERT_DIRECTIONS),
                                      direction_text))
                plan.append(entry)
                continue
            direction = direction_text

        enabled = _import_flag(cell(row, 'alert on'))
        if enabled is None and cell(row, 'alert on'):
            entry['reason'] = 'Alert On must read YES or NO'
            plan.append(entry)
            continue
        if enabled is None:
            enabled = alert_price is not None
        if enabled and alert_price is None:
            entry['reason'] = 'an alert is switched on with no alert price'
            plan.append(entry)
            continue

        wanted = {
            'trade_nature_id': nature.id if nature is not None else None,
            'target_price': target_price,
            'alert_price': alert_price,
            'alert_direction': direction if alert_price is not None else None,
            'price_alert_enabled': bool(enabled),
        }
        entry['values'] = wanted

        # ---- the note, which belongs to the stock rather than to this row ---
        note_key = _note_key(symbol, exchange)
        note_values = None
        note_changes = []
        note_cleared = False
        if note_columns:
            note_values = {}
            for heading, field in note_columns:
                text = str(cell(row, heading) or '').strip()
                if len(text) > MAX_NOTE_CHARS:
                    note_values = None
                    entry['reason'] = ('the %s is %d characters, and the limit '
                                       'is %d' % (heading, len(text),
                                                  MAX_NOTE_CHARS))
                    break
                note_values[field] = text

            if note_values is None:
                plan.append(entry)
                continue

            claimed = note_wanted.get(note_key)
            if claimed is not None and claimed != note_values:
                # The same stock, on two lists, with two different notes. There
                # is one note per stock, so one of these would silently lose.
                entry['reason'] = (
                    '%s appears more than once in this file with different note '
                    'text, and a stock has only one note' % symbol
                )
                plan.append(entry)
                continue
            note_wanted[note_key] = note_values

            note = existing_notes.get(note_key)
            for heading, field in note_columns:
                before = (getattr(note, field, None) or '') if note is not None else ''
                after = note_values[field]
                if before.strip() == after.strip():
                    continue
                label = 'To Watch' if field == 'to_watch' else field.title()
                if after:
                    note_changes.append(
                        '%s %s' % (label, 'written' if not before else 'changed')
                    )
                else:
                    note_changes.append('%s CLEARED' % label)
                    note_cleared = True
            entry['note_values'] = note_values
            entry['note_changes'] = note_changes
            entry['note_cleared'] = note_cleared

        item = existing.get(key)
        if item is None:
            room_left = MAX_WATCHLIST_ITEMS - used.get(target_list.id, 0)
            if room_left <= 0:
                entry['reason'] = ('%s already holds the most it can (%d stocks)'
                                   % (target_list.name, MAX_WATCHLIST_ITEMS))
                plan.append(entry)
                continue
            used[target_list.id] = used.get(target_list.id, 0) + 1
            entry['action'] = 'ADD'
            entry['arms_alert'] = bool(enabled and alert_price)
            entry['changes'] = (
                _describe_import_values(wanted, natures_by_name) + note_changes
            )
            plan.append(entry)
            continue

        entry['item_id'] = item.id
        changes = []
        for field, value in wanted.items():
            before = getattr(item, field)
            if field == 'price_alert_enabled':
                before = bool(before)
            elif field in ('target_price', 'alert_price'):
                before = None if before is None else round(float(before), 4)
            if before != value:
                changes.append({
                    'field': field,
                    'from': before,
                    'to': value,
                })
        if not changes and not note_changes:
            entry['action'] = 'UNCHANGED'
            plan.append(entry)
            continue

        entry['action'] = 'CHANGE'
        entry['changes'] = (
            _describe_import_changes(changes, natures_by_name) + note_changes
        )
        entry['change_fields'] = [change['field'] for change in changes]
        # Any touch of the alert re-arms it, so an alert that already fired
        # will fire again. Said out loud, because that is a real consequence of
        # a cell nobody looked at twice.
        entry['arms_alert'] = bool(
            wanted['price_alert_enabled'] and wanted['alert_price']
            and any(change['field'] in ('alert_price', 'alert_direction',
                                        'price_alert_enabled')
                    for change in changes)
        )
        plan.append(entry)

    return plan


def _nature_name_for(nature_id, natures_by_name):
    for name, nature in natures_by_name.items():
        if nature.id == nature_id:
            return nature.name
    return None


def _describe_import_values(values, natures_by_name):
    """What an ADD will set, in words."""
    lines = []
    nature = _nature_name_for(values.get('trade_nature_id'), natures_by_name)
    if nature:
        lines.append('trade nature %s' % nature)
    if values.get('target_price'):
        lines.append('target %s' % _money(values['target_price']))
    if values.get('alert_price'):
        lines.append('alert %s %s' % (
            (values.get('alert_direction') or 'at').lower(),
            _money(values['alert_price'])
        ))
        lines.append('alert %s' % ('on' if values['price_alert_enabled'] else 'off'))
    return lines


def _describe_import_changes(changes, natures_by_name):
    """What a CHANGE will alter, in words, old value and new."""
    labels = {
        'trade_nature_id': 'trade nature',
        'target_price': 'target price',
        'alert_price': 'alert price',
        'alert_direction': 'alert direction',
        'price_alert_enabled': 'alert',
    }

    def render(field, value):
        if value is None or value == '':
            return 'nothing'
        if field == 'trade_nature_id':
            return _nature_name_for(value, natures_by_name) or 'nothing'
        if field == 'price_alert_enabled':
            return 'on' if value else 'off'
        if field in ('target_price', 'alert_price'):
            return _money(value)
        return str(value)

    return [
        '%s %s to %s' % (
            labels.get(change['field'], change['field']),
            render(change['field'], change['from']),
            render(change['field'], change['to']),
        )
        for change in changes
    ]


def _import_summary(plan):
    """
    The counts the screen leads with.

    Alerts armed and notes cleared are counted separately from everything else,
    because they are the two things in a file that a person would not want to
    discover afterwards. An armed alert fires at a price; a cleared note is
    prose nobody can retype.
    """
    live = [row for row in plan if row['action'] in ('ADD', 'CHANGE')]
    return {
        'rows': len(plan),
        'add': sum(1 for row in plan if row['action'] == 'ADD'),
        'change': sum(1 for row in plan if row['action'] == 'CHANGE'),
        'unchanged': sum(1 for row in plan if row['action'] == 'UNCHANGED'),
        'skipped': sum(1 for row in plan if row['action'] == 'SKIP'),
        'alerts_armed': sum(1 for row in live if row.get('arms_alert')),
        'notes_changed': sum(1 for row in live if row.get('note_changes')),
        'notes_cleared': sum(1 for row in live if row.get('note_cleared')),
    }


def _import_fingerprint(plan):
    """
    A short hash of exactly what the plan would do.

    The apply re-plans from the same file and compares. If the watch list moved
    underneath in between - another tab, or an alert firing - the two differ and
    the apply refuses rather than doing something that was never shown.
    """
    parts = []
    for row in plan:
        parts.append('%s|%s|%s|%s|%s' % (
            row.get('line'), row.get('action'), row.get('symbol'),
            row.get('exchange'), row.get('watchlist_id')
        ))
        for change in row.get('changes') or []:
            parts.append('  %s' % change)
        for field, value in sorted((row.get('note_values') or {}).items()):
            parts.append('  note %s=%s' % (field, value))
    digest = hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()
    return digest[:16]


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

    target_list = _resolve_watchlist(data)

    # A stock you hold does not belong here. Its stop loss, target, exit mode,
    # trade nature and note are changed on Holdings, one stock at a time, and a
    # row added here would be hidden the moment it was created.
    if (symbol, exchange) in _held_symbol_keys():
        raise _BadRequest(
            f'You hold {symbol}, so it is on the Holdings screen rather than a '
            'watch list. Its stop loss, target and note are changed there. It '
            'comes back to the watch list by itself when the last share is sold.'
        )

    existing = EquityWatchlistItem.query.filter_by(
        watchlist_id=target_list.id, symbol=symbol, exchange=exchange
    ).first()
    if existing is not None:
        raise _BadRequest(f'{symbol} is already on {target_list.name}')

    # The cap is per list, so filling one list does not block every other.
    count = EquityWatchlistItem.query.filter_by(watchlist_id=target_list.id).count()
    if count >= MAX_WATCHLIST_ITEMS:
        raise _BadRequest(
            f'{target_list.name} holds at most {MAX_WATCHLIST_ITEMS} stocks. '
            'Remove one before adding another.'
        )

    item = EquityWatchlistItem(
        user_id=current_user.id,
        watchlist_id=target_list.id,
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
        raise _BadRequest(f'{symbol} is already on {target_list.name}')

    _log_activity('equity_watchlist_added', {
        'symbol': symbol, 'exchange': exchange, 'item_id': item.id,
        'watchlist_id': target_list.id
    })

    payload = _build_watchlist_payload(watchlist_id=target_list.id)
    payload['item_id'] = item.id
    return _ok(payload, f'{symbol} added to {target_list.name}')


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

    # Answer with the list this row actually belongs to, not the default one.
    # Rebuilding the default meant a screen showing any other list redrew itself
    # as the default for one frame after every edit, then snapped back on the
    # next poll.
    payload = _build_watchlist_payload(watchlist_id=item.watchlist_id)
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
    # Read before the delete: the row is gone by the time the payload is built,
    # and the answer has to describe the list the admin is looking at.
    source_list_id = item.watchlist_id

    db.session.delete(item)
    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity watch list delete failed: {exc}')
        return _json_error(f'Failed to remove the watch list row: {exc}', 500)

    _log_activity('equity_watchlist_removed', {'item_id': item_id, 'symbol': symbol})

    payload = _build_watchlist_payload(watchlist_id=source_list_id)
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
# Watch lists
#
# Named lists, maintained from Settings in the same manner as trade natures.
# A stock may sit in several lists at once, each entry with its own target
# price and its own alert, so uniqueness is scoped to the list.
# ---------------------------------------------------------------------------

@equity_bp.route('/api/watchlists')
@login_required
@api_rate_limit()
@_json_route
def api_watchlists():
    """Every watch list this user keeps, with how many stocks each holds."""
    _default_watchlist()
    return _ok(_build_watchlists_payload())


@equity_bp.route('/api/watchlists', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_create_watchlist():
    """
    Create a watch list.

    Request body: {"name": "My Holdings"}
    """
    data = _body()
    name = _read_text(data, 'name', maximum=60)

    existing = EquityWatchlist.query.filter_by(
        user_id=current_user.id, name=name
    ).first()
    if existing is not None:
        raise _BadRequest(f'A watch list named {name} already exists')

    if len(_all_watchlists()) >= MAX_WATCHLISTS:
        raise _BadRequest(
            f'You can keep at most {MAX_WATCHLISTS} watch lists. '
            'Delete one before adding another.'
        )

    highest = db.session.query(
        db.func.max(EquityWatchlist.sort_order)
    ).filter_by(user_id=current_user.id).scalar()

    # The very first list a user creates becomes their default, so there is
    # always somewhere for a stock to go.
    entry = EquityWatchlist(
        user_id=current_user.id,
        name=name,
        sort_order=_to_int(highest) + 1,
        is_default=not _all_watchlists(),
    )
    db.session.add(entry)
    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise _BadRequest(f'A watch list named {name} already exists')

    _log_activity('equity_watchlist_created', {'id': entry.id, 'name': name})

    payload = _build_watchlists_payload()
    payload['watchlist_id'] = entry.id
    return _ok(payload, f'Watch list {name} created')


@equity_bp.route('/api/watchlists/<int:watchlist_id>', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_update_watchlist(watchlist_id):
    """
    Rename a watch list, or make it the default.

    Request body: {"name": "Core"} and/or {"is_default": true}

    Renaming is safe at any time: a list is identified by its id everywhere, so
    the stocks in it, and any past order raised from it, are undisturbed.
    Only one list can be default, so setting it here clears the flag elsewhere.
    """
    entry = _owned_watchlist(watchlist_id)
    data = _body()
    changed = []

    if 'name' in data:
        name = _read_text(data, 'name', maximum=60)
        if name != entry.name:
            clash = EquityWatchlist.query.filter_by(
                user_id=current_user.id, name=name
            ).first()
            if clash is not None and clash.id != entry.id:
                raise _BadRequest(f'A watch list named {name} already exists')
            entry.name = name
            changed.append('name')

    if _read_bool(data, 'is_default', default=False):
        if not entry.is_default:
            for other in _all_watchlists():
                other.is_default = other.id == entry.id
            changed.append('default')

    if not changed:
        return _ok(_build_watchlists_payload(), 'Nothing to change')

    try:
        db.session.commit()
    except IntegrityError:
        db.session.rollback()
        raise _BadRequest('That watch list name is already taken')

    _log_activity('equity_watchlist_updated', {
        'id': entry.id, 'name': entry.name, 'changed': changed
    })
    return _ok(_build_watchlists_payload(), f'Watch list {entry.name} saved')


@equity_bp.route('/api/watchlists/<int:watchlist_id>', methods=['DELETE'])
@login_required
@api_rate_limit()
@_json_route
def api_delete_watchlist(watchlist_id):
    """
    Delete an empty watch list.

    Deliberately refused while the list still holds stocks. Removing a list
    would take its target prices and alerts with it, and a delete button is
    too easy to press by accident for that to be silent. The default list is
    never deletable, so a stock always has somewhere to go.
    """
    entry = _owned_watchlist(watchlist_id)

    if entry.is_default:
        raise _BadRequest(
            f'{entry.name} is your default watch list and cannot be deleted. '
            'Make another list the default first.'
        )

    count = EquityWatchlistItem.query.filter_by(watchlist_id=entry.id).count()
    if count:
        raise _BadRequest(
            f'{entry.name} still holds {count} '
            f'{"stock" if count == 1 else "stocks"}. '
            'Remove them on the Watch List screen before deleting it.'
        )

    name = entry.name
    db.session.delete(entry)
    db.session.commit()

    _log_activity('equity_watchlist_deleted', {'id': watchlist_id, 'name': name})
    return _ok(_build_watchlists_payload(), f'Watch list {name} deleted')


@equity_bp.route('/api/watchlists/reorder', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_reorder_watchlists():
    """
    Set the order the watch lists appear in.

    Request body: {"order": [3, 1, 2]}

    Any list not named keeps its place after the ones that are, so a partial
    list cannot silently drop a watch list out of the ordering.
    """
    data = _body()
    raw = data.get('order')
    if not isinstance(raw, list) or not raw:
        raise _BadRequest('order must be a list of watch list ids')

    wanted = []
    for value in raw:
        try:
            entry_id = int(value)
        except (TypeError, ValueError):
            raise _BadRequest('Invalid watch list id in order')
        if entry_id not in wanted:
            wanted.append(entry_id)

    lists = {entry.id: entry for entry in _all_watchlists()}
    unknown = [str(entry_id) for entry_id in wanted if entry_id not in lists]
    if unknown:
        raise _BadRequest(f'Watch list {", ".join(unknown)} not found')

    position = 1
    for entry_id in wanted:
        lists[entry_id].sort_order = position
        position += 1
    for entry_id, entry in sorted(lists.items()):
        if entry_id in wanted:
            continue
        entry.sort_order = position
        position += 1

    db.session.commit()
    _log_activity('equity_watchlists_reordered', {'order': wanted})
    return _ok(_build_watchlists_payload(), 'Watch list order saved')


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

    Results are ranked before they are capped - exact match, then symbols that
    begin with the search text, then names that do, shares ahead of funds - and
    each carries the kind it was read as, so the screen can badge it and filter
    on it without asking the broker again.

    Response: {"status", "message", "query", "exchange", "results": [
        {"symbol", "exchange", "name", "token", "instrument_type", "kind",
         "kind_label", "lot_size", "tick_size"}], "count", "counts"}
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

    results = _normalise_search_results(response, query)
    counts = {kind: 0 for kind in INSTRUMENT_KIND_LABELS}
    for row in results:
        counts[row['kind']] = counts.get(row['kind'], 0) + 1

    return _ok({
        'query': query,
        'exchange': exchange or 'all',
        'results': results,
        'count': len(results),
        'counts': counts,
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/quote')
@login_required
@heavy_rate_limit()
@_json_route
def api_quote():
    """
    Last traded price for one symbol, so a stock picked out of the search box
    can be confirmed before it is added to a watch list.

    Query string:
        symbol    required
        exchange  defaults to NSE
        account   read the price through this account. Optional.

    Response: {"status", "message", "quote": {"symbol", "exchange", "ltp",
        "prev_close", "change", "change_pct", "open", "high", "low"}}
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
        response = client.quotes(symbol=symbol, exchange=exchange)
    except Exception as exc:
        current_app.logger.warning(f'Equity quote read failed for {symbol}: {exc}')
        return _json_error(f'The live price is unavailable: {exc}', 502)

    if not isinstance(response, dict) or response.get('status') != 'success':
        message = (response or {}).get('message') or 'The live price returned no result'
        return _json_error(message, 502)

    data = response.get('data')
    if not isinstance(data, dict):
        data = {}

    ltp = _to_float(data.get('ltp'))
    prev_close = _to_float(data.get('prev_close'))
    change = ltp - prev_close if ltp and prev_close else 0.0
    change_pct = (change / prev_close * 100.0) if prev_close else 0.0

    return _ok({
        'quote': {
            'symbol': symbol,
            'exchange': exchange,
            'ltp': ltp,
            'prev_close': prev_close,
            'change': change,
            'change_pct': change_pct,
            'open': _to_float(data.get('open')),
            'high': _to_float(data.get('high')),
            'low': _to_float(data.get('low')),
        },
        'account_id': credential.get('account_id'),
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

    Every GTT still working is returned separately under "gtt_orders", in the
    same row shape. A standing instruction is not part of the day's activity
    and counting it there made the day read as busier than it was.

    Response:
        {"status", "message",
         "orders": [ ... one per order placed today, see below ... ],
         "gtt_orders": [ ... the same shape, every GTT still working ... ],
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

    accounts_label is a PLACED over selected count, for example "4/5", where
    placed means the order reached the broker (open or filled). The PRD asks for
    filled over selected, which needs fill reconciliation: until that lands,
    accounts_filled carries the true filled count separately, so the two are not
    conflated. status_reason is the short explanation next to PARTIAL, for
    example "1 failed".
    """
    filters, error = _read_book_filters()
    if error:
        return _json_error(error, 404 if 'not found' in error else 400)

    include_splits = _arg('splits').lower() in ('1', 'true', 'yes')
    payload = _build_order_book(
        filters, carry_open_gtt=True, include_splits=include_splits,
        merge_broker=True, split_gtt=True
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

    # SAVING RE-ARMS, WHETHER OR NOT THE NUMBER MOVED.
    #
    # This used to compare the submitted level with the stored one and re-arm
    # only when they differed. That made the Edit dialog's own promise false.
    # After a level has fired - and above all after a PART exit, where the
    # remainder keeps the breach record on purpose - retyping the same stop
    # loss and pressing Save is the obvious way to put the level back in
    # service, and it did nothing at all. The admin had no way to re-arm a
    # level short of moving it to a number he did not want.
    #
    # Submitting a level is the instruction. A field left out of the request is
    # not touched; a field submitted empty clears the level, and clearing takes
    # the stale breach record with it rather than leaving one behind on a row
    # that no longer has a level to breach.
    sl_submitted = 'stop_loss' in entry
    tp_submitted = 'target' in entry

    holding.stop_loss = stop_loss
    holding.target = target_price

    if 'exit_mode' in entry:
        holding.exit_mode = _read_choice(
            entry, 'exit_mode', VALID_EXIT_MODES, required=False,
            default=holding.exit_mode
        )
    if 'trade_nature_id' in entry:
        holding.trade_nature_id = _read_trade_nature_id(entry)

    if sl_submitted and tp_submitted:
        breach_kind = 'BOTH'
    elif sl_submitted:
        breach_kind = EQUITY_EXIT_REASON_STOP_LOSS
    elif tp_submitted:
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


def _broker_read_timeout():
    """The ordinary broker read timeout, raised to the admin's order timeout."""
    try:
        settings = _equity_settings()
        seconds = _to_int(getattr(settings, 'order_timeout_seconds', 0))
    except Exception:
        return BROKER_TIMEOUT_SECONDS
    if seconds <= 0:
        return BROKER_TIMEOUT_SECONDS
    return min(max(seconds, BROKER_TIMEOUT_SECONDS), MAX_ORDER_TIMEOUT_SECONDS)


def _fetch_positions(creds, deadline=None):
    """
    Today's open positions for each account, read straight from the broker.

    A position is not a holding. A delivery buy made today sits in the position
    book until it settles and only then appears among the holdings, which is why
    a stock bought this morning is invisible on the Holdings screen. This is the
    read that makes it visible.

    Returns {account_id: [rows]}, simply omitting an account the broker did not
    answer for rather than reporting it as holding nothing.
    """
    positions = {}
    if not creds:
        return positions

    app = current_app._get_current_object()
    # The admin's order timeout when it is longer than the ordinary read
    # timeout. A broker slow enough to need two minutes to place an order is
    # slow enough to need more than eight seconds to list a position, and an
    # eight second wait against such a broker returns an empty screen that
    # looks exactly like owning nothing.
    timeout = _call_timeout(deadline) if deadline else _broker_read_timeout()
    if timeout <= 0:
        return positions

    def fetch_one(cred):
        with app.app_context():
            if not cred.get('api_key'):
                return (cred['account_id'], None)
            try:
                client = ExtendedOpenAlgoAPI(
                    api_key=cred['api_key'],
                    host=cred['host_url'],
                    timeout=timeout
                )
                response = client.positionbook()
            except Exception as exc:
                current_app.logger.warning(
                    f'Equity position read failed for account {cred["account_id"]}: {exc}'
                )
                return (cred['account_id'], None)

            if not isinstance(response, dict) or response.get('status') != 'success':
                current_app.logger.warning(
                    f'Equity position read refused for account {cred["account_id"]}: '
                    f'{(response or {}).get("message") or (response or {}).get("status")}'
                )
                return (cred['account_id'], None)

            data = response.get('data')
            if isinstance(data, dict):
                data = data.get('positions')
            if not isinstance(data, list):
                return (cred['account_id'], [])
            return (cred['account_id'], [row for row in data if isinstance(row, dict)])

    executor = ThreadPoolExecutor(max_workers=min(MAX_FETCH_WORKERS, len(creds)))
    try:
        futures = [executor.submit(fetch_one, cred) for cred in creds]
        for future in as_completed(futures):
            try:
                account_id, rows = future.result()
            except Exception:
                continue
            if rows is not None:
                positions[account_id] = rows
    finally:
        try:
            executor.shutdown(wait=False, cancel_futures=True)
        except TypeError:
            executor.shutdown(wait=False)

    return positions


@equity_bp.route('/api/positions')
@login_required
@heavy_rate_limit()
@_json_route
def api_positions():
    """
    Today's open positions across every account, with a live price and P&L.

    Separate from Holdings on purpose. A delivery buy placed today is a position
    until it settles; treating the two as one screen would blur a distinction
    that decides what can be sold and what the stop loss monitor may act on.

    Query string: account, an account id or 'all'. Optional.

    Response: {"status", "message", "positions": [...], "totals": {...},
               "accounts_missing": [ids], "price_feed": {...}}
    """
    account_id, error = _filter_account_id()
    if error:
        return _json_error(error, 404 if 'not found' in error else 400)

    accounts = _active_accounts()
    if account_id is not None:
        accounts = [account for account in accounts if account.id == account_id]
    if not accounts:
        return _ok({
            'positions': [], 'totals': _empty_position_totals(),
            'accounts_missing': [], 'price_feed': _feed_status_block(
                {'requested': 0, 'from_feed': 0, 'from_rest': 0, 'fallback_symbols': 0}
            ),
            'generated_at': _iso(datetime.utcnow()),
        })

    creds = _account_credentials(accounts)
    raw = _fetch_positions(creds)
    directory = _account_directory()

    keys = sorted({
        (str(row.get('symbol') or '').strip().upper(),
         str(row.get('exchange') or 'NSE').strip().upper())
        for rows in raw.values() for row in rows
        if _to_int(row.get('quantity'))
    })
    quotes, price_feed = _resolve_prices(creds, {}, keys, want_prev_close=True)

    # The stop loss and target that govern each position, read from the holding
    # row that carries them - which is also the row the monitor acts on, so a
    # level shown here is a level that is genuinely armed. A position bought
    # today has one of these from the moment the buy fills; before that changed,
    # a stop loss set at order time governed nothing until settlement and this
    # screen had nothing to show at all.
    levels = {}
    try:
        for holding in EquityHolding.query.filter(
            EquityHolding.user_id == current_user.id,
            EquityHolding.account_id.in_([account.id for account in accounts]),
        ).all():
            levels[(
                holding.account_id,
                (holding.symbol or '').strip().upper(),
                (holding.exchange or 'NSE').strip().upper(),
            )] = holding
    except Exception as exc:
        current_app.logger.debug(f'Could not read levels for positions: {exc}')

    # Named once for the whole screen rather than looked up per row.
    nature_names = {nature.id: nature.name for nature in _trade_natures()}

    # The shorts, which are the one thing on this screen that MUST be closed
    # today. Read separately from the position book because the position book
    # cannot tell a short apart from a sale out of a holding: both are a
    # negative line, and only this table knows which is which.
    shorts = {}
    try:
        for short in EquityIntradayShort.query.filter(
            EquityIntradayShort.user_id == current_user.id,
            EquityIntradayShort.account_id.in_([account.id for account in accounts]),
            EquityIntradayShort.status == EQUITY_SHORT_STATUS_OPEN,
            EquityIntradayShort.quantity > 0,
        ).all():
            key = (
                short.account_id,
                (short.symbol or '').strip().upper(),
                (short.exchange or 'NSE').strip().upper(),
            )
            entry = shorts.setdefault(key, {
                'quantity': 0, 'stop_status': None, 'stop_price': None,
                'ids': [],
            })
            entry['quantity'] += _to_int(short.quantity)
            entry['ids'].append(short.id)
            if entry['stop_price'] is None:
                entry['stop_price'] = _to_float(short.stop_trigger_price) or None
            # The WORST status wins. Two shorts in the same stock where one is
            # unprotected is an unprotected position, and saying RESTING because
            # the other one is fine would be the wrong half of the truth.
            if short.stop_status != EQUITY_STOP_STATUS_RESTING:
                entry['stop_status'] = short.stop_status
            elif entry['stop_status'] is None:
                entry['stop_status'] = EQUITY_STOP_STATUS_RESTING
    except Exception as exc:
        current_app.logger.debug(f'Could not read open shorts for positions: {exc}')

    _cutoff_minute, squareoff_minute, _monitor_on = _intraday_rules()
    minutes_left = squareoff_minute - _ist_minute_now()

    rows = []
    for account in accounts:
        for entry in raw.get(account.id) or []:
            quantity = _to_int(entry.get('quantity'))
            if not quantity:
                # A closed position comes back with a quantity of zero. It is
                # history, not a position, so it is not listed.
                continue

            symbol = str(entry.get('symbol') or '').strip().upper()
            exchange = str(entry.get('exchange') or 'NSE').strip().upper()
            avg_cost = _to_float(entry.get('average_price'))
            quote = quotes.get((symbol, exchange)) or {}
            ltp = _to_float(quote.get('ltp'))

            invested = avg_cost * quantity
            market_value = ltp * quantity if ltp > 0 else 0.0
            pnl = market_value - invested if ltp > 0 else 0.0

            product = str(entry.get('product') or '').strip().upper()
            short = shorts.get((account.id, symbol, exchange)) or {}

            # A negative delivery line is a sale out of the portfolio, not a
            # short. Brokers show it exactly this way - sell one of eighty and
            # the position book carries a -1 while the holding reads 79 - and
            # the industry convention is worth following rather than inventing
            # a tidier one. It is LABELLED because "-1 RELIANCE" on its own
            # reads as a short to anyone who has not just placed the sale.
            #
            # A SHORT is also a negative line, and calling it a sale out of a
            # holding would be exactly backwards: one is money already taken
            # off the table, the other is an obligation that has to be bought
            # back this afternoon. They are told apart by our own record of the
            # short, and failing that by the product, because MIS is the only
            # thing this module ever sends intraday.
            is_short = bool(short) or (quantity < 0 and product == EQUITY_PRODUCT_MIS)
            sold_from_holding = quantity < 0 and not is_short

            holding = levels.get((account.id, symbol, exchange))
            nature_id = getattr(holding, 'trade_nature_id', None)
            if nature_id is None and not is_short:
                nature_id = _nature_for_new_holding(
                    account.id, symbol, exchange, abs(quantity)
                )
            nature_name = nature_names.get(nature_id) if nature_id else None

            rows.append({
                'account_id': account.id,
                'account_name': (directory.get(account.id) or {}).get(
                    'account_name', account.account_name
                ),
                'symbol': symbol,
                'exchange': exchange,
                'product': product,
                'quantity': quantity,
                'sold_from_holding': sold_from_holding,
                'is_short': is_short,
                'short_quantity': _to_int(short.get('quantity')),
                # Carried to the screen so Cover can name exactly which
                # obligations it is closing, rather than asking the server to
                # work it out again from a symbol and hope it picks the same
                # ones. A row is merged across accounts; the ids are not.
                'short_ids': list(short.get('ids') or []),
                'short_stop_status': short.get('stop_status'),
                'short_stop_price': (
                    _money(short['stop_price']) if short.get('stop_price') else None
                ),
                'squareoff_at': '%02d:%02d' % (
                    squareoff_minute // 60, squareoff_minute % 60
                ),
                'minutes_to_squareoff': minutes_left if is_short else None,
                'avg_cost': _money(avg_cost),
                'stop_loss': _position_level(
                    levels.get((account.id, symbol, exchange)), 'stop_loss'
                ),
                'target': _position_level(
                    levels.get((account.id, symbol, exchange)), 'target'
                ),
                # Armed means the monitor is actually watching this row on its
                # next tick. A level that is recorded but not armed - the row is
                # awaiting a confirmation, or a sell is already in flight - is
                # shown differently, because a stop loss that looks live and is
                # not is the worst thing this screen could say.
                'levels_armed': bool(
                    getattr(levels.get((account.id, symbol, exchange)),
                            'is_monitorable', False)
                ),
                # The exit mode the level is armed under, so the Edit dialog
                # here can show what is in force rather than guessing at it.
                # CONFIRM is the safe reading of "not set": it raises an alert
                # and waits, where AUTO would sell.
                'exit_mode': (
                    getattr(holding, 'exit_mode', None) or EQUITY_EXIT_MODE_CONFIRM
                ) if holding is not None else None,
                # Whether this row's stop loss and target can be CHANGED here.
                #
                # Holdings stopped listing today's unsettled buys on
                # 6 September, so this screen became the only place their
                # levels can be edited. It needs a holding row to write to,
                # which a delivery buy has from the moment it fills.
                #
                # A short is excluded: its stop is the square-off obligation
                # itself, handled by Cover Now, not a holding level. A sale out
                # of a holding is excluded because it is a negative line
                # describing shares that have LEFT, and there is nothing there
                # to arm.
                'can_set_levels': bool(
                    holding is not None and not is_short and not sold_from_holding
                ),
                # The trade nature this position was bought under.
                #
                # Read from the holding row where one exists, and otherwise
                # from AlgoMirror's own buys - which is the usual case here,
                # because a position bought today has no holding row until it
                # settles. Exactly the rule a new holding inherits by, so a
                # stock reads the same before and after settlement instead of
                # changing tag overnight.
                'trade_nature_id': nature_id,
                'trade_nature': nature_name,
                'ltp': _money(ltp),
                'has_ltp': ltp > 0,
                'invested': _money(invested),
                'market_value': _money(market_value),
                'pnl': _money(pnl),
                # Against the MAGNITUDE of what was put in. A sale out of a
                # holding carries a negative quantity and therefore a negative
                # invested figure, and signed_percent_of refuses a negative
                # denominator - so this column read a flat 0.0% on exactly the
                # rows where the number mattered.
                'pnl_pct': _pct(signed_percent_of(pnl, abs(invested))),
            })

    rows.sort(key=lambda row: (row['symbol'], row['account_id']))

    # One stock, one row, with the accounts behind it on the row itself.
    rows = _merge_same_position(rows)

    totals = _empty_position_totals()
    totals['positions'] = len(rows)
    # How many accounts are actually holding something, counted from the
    # accounts behind each row rather than from the rows themselves - one row
    # can stand for two accounts.
    holding_accounts = set()
    for row in rows:
        for entry in row.get('accounts') or []:
            if entry.get('account_id') is not None:
                holding_accounts.add(entry['account_id'])
    totals['accounts'] = len(holding_accounts)
    # The denominator behind the "2/2" on each row: how many accounts were
    # read at all. A stock in two of your two accounts and a stock in two of
    # your five are different facts.
    totals['accounts_in_view'] = len(accounts)
    totals['quantity'] = sum(row['quantity'] for row in rows)
    totals['invested'] = _money(sum(row['invested'] for row in rows))
    totals['market_value'] = _money(sum(row['market_value'] for row in rows))
    totals['pnl'] = _money(sum(row['pnl'] for row in rows))
    # Capital at risk, not the net of longs against sold-out lines. Summing the
    # signed figures lets a negative line cancel a positive one and can leave a
    # denominator near zero, which turns a real percentage into nonsense.
    totals['pnl_pct'] = _pct(signed_percent_of(
        totals['pnl'], sum(abs(row['invested']) for row in rows)
    ))

    return _ok({
        'positions': rows,
        'totals': totals,
        'accounts_missing': [
            account.id for account in accounts if account.id not in raw
        ],
        'price_feed': price_feed,
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/notes')
@login_required
@api_rate_limit()
@_json_route
def api_read_note():
    """
    The investment note for one stock, with its history.

    Query string: symbol (required), exchange (optional, defaults to NSE).

    Response: {"status", "message", "note": {thesis, risk, has_note,
               updated_at, versions: [{thesis, risk, saved_at}]}}

    A stock with no note answers with empty boxes rather than a 404. There is
    nothing exceptional about not having written about a stock yet, and the
    dialog opens the same way either way.
    """
    symbol, exchange = _note_key(
        request.args.get('symbol'), request.args.get('exchange')
    )
    if not symbol:
        raise _BadRequest('A symbol is required')

    note = EquityStockNote.query.filter_by(
        user_id=current_user.id, symbol=symbol, exchange=exchange
    ).first()

    versions = []
    if note is not None:
        versions = note.versions.order_by(
            EquityStockNoteVersion.saved_at.desc()
        ).limit(MAX_NOTE_VERSIONS).all()

    payload = _note_payload(note, versions)
    payload['symbol'] = symbol
    payload['exchange'] = exchange
    return _ok({'note': payload})


@equity_bp.route('/api/notes', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_save_note():
    """
    Save the investment note for one stock.

    Request body: {"symbol", "exchange", "thesis", "risk", "to_watch"}

    The previous text is copied into the version table BEFORE the current row
    is changed, and only when it actually said something and actually differs.
    A save that changes nothing does not manufacture a version, and neither
    does the first note on a stock - there was nothing there to preserve.

    The history is no longer drawn on screen, at the owner's instruction. It is
    still WRITTEN: a record costs nothing to keep and cannot be recovered once
    it stops being kept, so showing it again is one change to the screen rather
    than a gap in the record.

    Clearing every box is how a note is removed. The row stays, so the history
    stays reachable; the marker on the tables goes, because an empty note is
    not a note.

    Response: the same shape as the read.
    """
    data = _body()
    symbol, exchange = _note_key(data.get('symbol'), data.get('exchange'))
    if not symbol:
        raise _BadRequest('A symbol is required')

    typed = {field: _read_note_text(data, field) for field in NOTE_FIELDS}

    note = EquityStockNote.query.filter_by(
        user_id=current_user.id, symbol=symbol, exchange=exchange
    ).first()

    created = False
    if note is None:
        note = EquityStockNote(
            user_id=current_user.id, symbol=symbol, exchange=exchange, **typed
        )
        db.session.add(note)
        created = True
    else:
        before = {
            field: (getattr(note, field, None) or '') for field in NOTE_FIELDS
        }
        unchanged = before == typed
        had_words = any(value.strip() for value in before.values())
        if had_words and not unchanged:
            # The record of what was believed before, written before the new
            # belief overwrites it. Nothing in that table is ever edited: a
            # version you can change is not a record.
            db.session.add(EquityStockNoteVersion(
                note_id=note.id,
                user_id=current_user.id,
                symbol=symbol,
                exchange=exchange,
                saved_at=datetime.utcnow(),
                **before
            ))
        for field, value in typed.items():
            setattr(note, field, value)

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity note save failed for {symbol}: {exc}')
        return _json_error(f'The note could not be saved: {exc}', 500)

    _log_activity('equity_note_saved', dict(
        {'%s_chars' % field: len(value) for field, value in typed.items()},
        symbol=symbol, exchange=exchange, created=created,
    ))

    versions = note.versions.order_by(
        EquityStockNoteVersion.saved_at.desc()
    ).limit(MAX_NOTE_VERSIONS).all()

    payload = _note_payload(note, versions)
    payload['symbol'] = symbol
    payload['exchange'] = exchange
    message = 'Note saved' if any(typed.values()) else 'Note cleared'
    return _ok({'note': payload}, message)


@equity_bp.route('/api/watchlist/<int:item_id>/symbol', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_fix_watchlist_symbol(item_id):
    """
    Correct the symbol on a watch list row.

    For the row that is carrying something the broker does not recognise - a
    company name typed instead of a ticker, or a symbol the exchange has since
    renamed. WOCKHARDT became WOCKPHARMA; no guard on the way in would ever
    have caught that, because it was right when it was typed.

    Request body: {"symbol": "SBIN", "exchange": "NSE"}

    The new symbol is checked against the broker BEFORE anything is written.
    This is the one place where refusing is right: the whole point of the
    action is to replace a symbol that does not exist, and replacing it with
    another that does not exist would be no correction at all.

    Three things travel with the correction:
      - the target, the alert and the trade nature stay on the row;
      - every OTHER row of yours carrying the same wrong symbol is corrected
        too, because the symbol was wrong, not this row;
      - the note moves, since a note is filed against the stock and the thesis
        was written about the company rather than the typo.

    Response: the watch list, reloaded.
    """
    item = _owned_watchlist_item(item_id)
    if item is None:
        raise _BadRequest('That watch list row was not found')

    data = _body()
    symbol = _read_symbol(data)
    exchange = _read_exchange(data)
    if exchange not in SEARCH_EXCHANGES:
        raise _BadRequest('exchange must be one of %s' % ', '.join(SEARCH_EXCHANGES))

    old_symbol = str(item.symbol or '').upper()
    old_exchange = str(item.exchange or 'NSE').upper()
    if (symbol, exchange) == (old_symbol, old_exchange):
        raise _BadRequest('That is the symbol the row already has')

    creds = _account_credentials(_active_accounts())
    verdict = _broker_knows_symbol(_quote_credential(creds, {}), symbol, exchange)
    if verdict is None:
        return _json_error(
            'Your broker could not be reached to check %s, so nothing was '
            'changed. Try again in a moment.' % symbol, 502
        )
    if verdict is False:
        raise _BadRequest(
            '%s is not a symbol your broker knows on %s. Use the search to '
            'pick one.' % (symbol, exchange)
        )

    # Every row of this user's carrying the wrong symbol, not just the one the
    # button was pressed on. The symbol was wrong; that is not a fact about one
    # watch list.
    siblings = EquityWatchlistItem.query.filter_by(
        user_id=current_user.id, symbol=old_symbol, exchange=old_exchange
    ).all()

    moved_note = False
    clash = False
    try:
        for row in siblings:
            # A row for the CORRECT symbol may already exist on that list, and
            # two rows for one stock on one list is what the screen refuses. In
            # that case the wrong one is left alone rather than colliding, and
            # the admin is told.
            already = EquityWatchlistItem.query.filter_by(
                watchlist_id=row.watchlist_id, symbol=symbol, exchange=exchange
            ).first()
            if already is not None:
                clash = True
                continue
            row.symbol = symbol
            row.exchange = exchange

        note = EquityStockNote.query.filter_by(
            user_id=current_user.id, symbol=old_symbol, exchange=old_exchange
        ).first()
        if note is not None:
            target = EquityStockNote.query.filter_by(
                user_id=current_user.id, symbol=symbol, exchange=exchange
            ).first()
            if target is None:
                # The thesis was written about the company, not about the typo.
                note.symbol = symbol
                note.exchange = exchange
                for version in note.versions.all():
                    version.symbol = symbol
                    version.exchange = exchange
                moved_note = True

        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(
            f'Could not correct {old_symbol} to {symbol}: {exc}'
        )
        return _json_error(f'The symbol could not be corrected: {exc}', 500)

    # The old symbol's verdict is now meaningless and the new one is known.
    _remember_symbol_known((symbol, exchange), True)

    _log_activity('equity_symbol_corrected', {
        'from': old_symbol, 'to': symbol, 'exchange': exchange,
        'rows': len(siblings), 'note_moved': moved_note,
    })

    changed = len(siblings) - (1 if clash else 0)
    parts = ['%s corrected to %s' % (old_symbol, symbol)]
    if changed > 1:
        parts.append('on %d rows' % changed)
    if moved_note:
        parts.append('and your note moved with it')
    if clash:
        parts.append('one row was left alone because %s is already on that list'
                     % symbol)

    payload = _build_watchlist_payload(watchlist_id=_query_watchlist_id())
    return _ok(payload, ', '.join(parts))


@equity_bp.route('/api/watchlist/export')
@login_required
@heavy_rate_limit()
def api_watchlist_export():
    """
    The watch list as a CSV, ready to edit in Excel and upload back.

    A pure read: it runs the same builder the screen runs and serialises it.

    ?watchlist_id= exports one list; 'all' exports every list, with the Watch
    List column saying which row belongs where.
    """
    try:
        payload, rows = _watchlist_export_rows(watchlist_id=_query_watchlist_id())
    except Exception as exc:
        current_app.logger.error(f'Equity watch list export failed: {exc}')
        return _json_error(f'Failed to export the watch list: {exc}', 500)

    buffer = io.StringIO()
    writer = csv.writer(buffer)
    writer.writerow(WATCHLIST_EXPORT_HEADERS)
    for row in rows:
        writer.writerow(row)

    name = str(payload.get('watchlist_name') or 'watchlist')
    slug = ''.join(char if char.isalnum() else '-' for char in name).strip('-')
    filename = 'watchlist-%s-%s.csv' % (
        slug.lower() or 'all', datetime.utcnow().strftime('%Y%m%d')
    )
    # utf-8-sig, so Excel opens it with the right characters instead of
    # mangling anything that is not plain ASCII in a note.
    return Response(
        buffer.getvalue().encode('utf-8-sig'),
        mimetype='text/csv; charset=utf-8',
        headers={'Content-Disposition': f'attachment; filename={filename}'}
    )


@equity_bp.route('/api/watchlist/import/preview', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_watchlist_import_preview():
    """
    What an uploaded file WOULD do. Writes nothing.

    Request body: {"csv": "<the file's text>", "watchlist_id": 3}

    watchlist_id is only a fallback for rows whose Watch List cell is blank.
    A row that names its own list goes there.

    Response: {"status", "message", "summary", "plan", "fingerprint"}

    The fingerprint is what the apply must send back. It is a hash of the plan,
    so if the watch list moves underneath between reading this and pressing
    Apply - another tab, an alert firing - the apply refuses rather than doing
    something that was never shown.
    """
    data = _body()
    plan = _plan_watchlist_import(
        data.get('csv'), default_watchlist_id=data.get('watchlist_id')
    )
    return _ok({
        'summary': _import_summary(plan),
        'plan': plan,
        'fingerprint': _import_fingerprint(plan),
        'generated_at': _iso(datetime.utcnow()),
    })


@equity_bp.route('/api/watchlist/import/apply', methods=['POST'])
@login_required
@api_rate_limit()
@_json_route
def api_watchlist_import_apply():
    """
    Apply an upload that has been previewed.

    Request body: {"csv": "<the same text>", "watchlist_id": 3,
                   "fingerprint": "<from the preview>"}

    The plan is worked out AGAIN here, from the file, and compared against the
    fingerprint the preview produced. Two reasons for that. The browser cannot
    hand back an edited plan and have it applied, because nothing the browser
    says about the plan is used. And if the watch list changed in between, the
    two plans differ, and the apply refuses rather than doing something the
    admin never saw.

    An upload can ADD and CHANGE. It can never REMOVE: a stock missing from the
    file is left exactly as it is.
    """
    data = _body()
    fingerprint = str(data.get('fingerprint') or '').strip()
    if not fingerprint:
        raise _BadRequest('Preview this file before applying it')

    plan = _plan_watchlist_import(
        data.get('csv'), default_watchlist_id=data.get('watchlist_id')
    )
    if _import_fingerprint(plan) != fingerprint:
        raise _BadRequest(
            'The watch list has changed since you previewed this file, so '
            'nothing was applied. Preview it again and check what it would do.'
        )

    applied = {'added': 0, 'changed': 0, 'notes': 0, 'failed': 0}
    problems = []

    for entry in plan:
        if entry['action'] not in ('ADD', 'CHANGE'):
            continue
        try:
            if entry['action'] == 'ADD':
                _apply_import_add(entry)
                applied['added'] += 1
            else:
                _apply_import_change(entry)
                applied['changed'] += 1
            if entry.get('note_values') is not None and entry.get('note_changes'):
                _apply_import_note(entry)
                applied['notes'] += 1
        except Exception as exc:
            db.session.rollback()
            applied['failed'] += 1
            problems.append('%s on line %d: %s'
                            % (entry.get('symbol'), entry.get('line'), exc))
            current_app.logger.error(
                f'Watch list import failed on {entry.get("symbol")}: {exc}'
            )

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Watch list import commit failed: {exc}')
        return _json_error(f'The upload could not be saved: {exc}', 500)

    _log_activity('equity_watchlist_imported', dict(applied, problems=len(problems)))

    parts = []
    if applied['added']:
        parts.append('%d added' % applied['added'])
    if applied['changed']:
        parts.append('%d changed' % applied['changed'])
    if applied['notes']:
        parts.append('%d note(s) written' % applied['notes'])
    if applied['failed']:
        parts.append('%d failed' % applied['failed'])
    if not parts:
        parts.append('nothing needed changing')

    payload = _build_watchlist_payload(watchlist_id=_query_watchlist_id())
    payload['applied'] = applied
    payload['problems'] = problems
    return _ok(payload, ', '.join(parts))


def _apply_import_add(entry):
    """One new watch list row, from a planned ADD."""
    values = entry['values']
    item = EquityWatchlistItem(
        user_id=current_user.id,
        watchlist_id=entry['watchlist_id'],
        symbol=entry['symbol'],
        exchange=entry['exchange'],
        trade_nature_id=values['trade_nature_id'],
        target_price=values['target_price'],
        alert_price=values['alert_price'],
        alert_direction=values['alert_direction'],
        price_alert_enabled=values['price_alert_enabled'],
    )
    db.session.add(item)
    db.session.flush()
    entry['item_id'] = item.id


def _apply_import_change(entry):
    """
    One existing watch list row, from a planned CHANGE.

    Scoped through the user as well as the id, so a row id from anywhere else
    cannot be written through this path.
    """
    item = EquityWatchlistItem.query.filter_by(
        id=entry.get('item_id'), user_id=current_user.id
    ).first()
    if item is None:
        raise ValueError('that row is no longer there')

    values = entry['values']
    fields = entry.get('change_fields') or list(values)
    for field in fields:
        setattr(item, field, values[field])

    # Any touch of the alert re-arms it. Without this an alert that already
    # fired would stay silent for good - the same rule the screen follows.
    if any(field in ('alert_price', 'alert_direction', 'price_alert_enabled')
           for field in fields):
        _clear_watchlist_alert(item)


def _apply_import_note(entry):
    """
    The note for one stock, from a planned change.

    Goes through the same keep-the-old-version rule the dialog uses, so a note
    changed by upload leaves the same record behind as one changed by hand.
    """
    symbol, exchange = _note_key(entry['symbol'], entry['exchange'])
    values = entry['note_values']

    note = EquityStockNote.query.filter_by(
        user_id=current_user.id, symbol=symbol, exchange=exchange
    ).first()
    if note is None:
        db.session.add(EquityStockNote(
            user_id=current_user.id, symbol=symbol, exchange=exchange, **values
        ))
        return

    before = {field: (getattr(note, field, None) or '') for field in NOTE_FIELDS}
    if any(value.strip() for value in before.values()):
        db.session.add(EquityStockNoteVersion(
            note_id=note.id,
            user_id=current_user.id,
            symbol=symbol,
            exchange=exchange,
            saved_at=datetime.utcnow(),
            **before
        ))
    for field, value in values.items():
        setattr(note, field, value)


@equity_bp.route('/api/shorts/cover', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_cover_shorts():
    """
    Buy one or more open shorts back NOW, because the admin asked.

    Between the square-off at its minute and the stop resting at the broker
    there was no way out at all: a short that looked wrong at two in the
    afternoon had to be closed at the broker's own terminal, where nothing here
    would know about it.

    It does not get its own sequence. Each short goes through the SAME cover
    the square-off uses - verify the position book, claim, cancel the resting
    stop, then place - so this and the monitor cannot both buy the same shares
    back, and every refusal that protects the square-off protects this too.

    Request body: {"short_ids": [12, 13]}

    Response: {"status", "message", "results": [ ... ], "covered", "skipped",
               "failed"}

    A partial answer is normal and is reported as such: one account's broker
    refusing has nothing to do with another's.
    """
    data = _body()
    raw = data.get('short_ids')
    if raw is None:
        raise _BadRequest('Nothing was selected to buy back')
    if isinstance(raw, (str, int)):
        raw = [raw]
    if not isinstance(raw, (list, tuple)):
        raise _BadRequest('short_ids must be a list')

    short_ids = []
    for value in raw:
        try:
            short_id = int(value)
        except (TypeError, ValueError):
            raise _BadRequest('Invalid short id')
        if short_id not in short_ids:
            short_ids.append(short_id)
    if not short_ids:
        raise _BadRequest('Nothing was selected to buy back')
    if len(short_ids) > 20:
        raise _BadRequest('Buy back at most 20 shorts at a time')

    # Ownership decided here, before the monitor is asked to do anything. The
    # monitor scopes by user as well, but a refusal belongs at the door.
    owned = {
        row.id for row in EquityIntradayShort.query.filter(
            EquityIntradayShort.user_id == current_user.id,
            EquityIntradayShort.id.in_(short_ids),
        ).all()
    }
    unknown = [str(short_id) for short_id in short_ids if short_id not in owned]
    if unknown:
        raise _BadRequest('Short %s not found' % ', '.join(unknown))

    try:
        from app.utils.equity_intraday_monitor import equity_intraday_monitor
    except Exception as exc:
        current_app.logger.error(f'Equity square-off module unavailable: {exc}')
        return _json_error(
            'The square-off module could not be loaded, so nothing was bought '
            'back. Restart the application and close this at your broker in '
            'the meantime.', 500
        )

    results = [
        equity_intraday_monitor.cover_now(short_id, current_user.id)
        for short_id in short_ids
    ]

    covered = sum(1 for row in results if row.get('status') == 'covered')
    skipped = sum(1 for row in results if row.get('status') == 'skipped')
    failed = sum(1 for row in results if row.get('status') == 'failed')

    if failed and not covered:
        status = 'error'
    elif failed or skipped:
        status = 'partial'
    else:
        status = 'success'

    parts = ['%d of %d bought back' % (covered, len(results))]
    if skipped:
        parts.append('%d not sent' % skipped)
    if failed:
        parts.append('%d failed' % failed)

    _log_activity('equity_shorts_covered', {
        'short_ids': short_ids, 'covered': covered,
        'skipped': skipped, 'failed': failed,
    })

    payload = {
        'results': results,
        'covered': covered,
        'skipped': skipped,
        'failed': failed,
        'generated_at': _iso(datetime.utcnow()),
    }
    if status == 'error':
        return _json_error(', '.join(parts), 502, extra=payload)
    return _ok(payload, ', '.join(parts))


def _position_level(holding, field):
    """
    One level off a holding row, or None when there is no row to read.

    A position with no holding row behind it has no levels - not zero. Zero on
    a stop loss column would read as "sell at any price", which is the opposite
    of nothing being set.
    """
    if holding is None:
        return None
    value = getattr(holding, field, None)
    return _money(value) if value is not None else None


def _merge_same_position(rows):
    """
    One stock, one row. The accounts behind it travel with the row.

    A ten share buy split across two accounts is one decision and reads as one
    line, the way the Order Book and Trade Book now read. The per-account
    breakdown is on the row, so View Split needs no second request.

    A sale out of a holding is kept apart from a buy in the same stock. The
    broker reports it as a negative delivery line and netting the two would
    hide both: a plus six and a minus four would read as a plus two that nobody
    traded.
    """
    merged = []
    groups = {}

    for row in rows:
        key = (
            row.get('symbol'), row.get('exchange'), row.get('product'),
            bool(row.get('sold_from_holding')),
            # A short never folds in with anything else. It is the one line
            # here that carries an obligation rather than a position, and
            # merging it into a delivery row of the same stock would hide the
            # only thing about it that matters.
            bool(row.get('is_short')), row.get('short_stop_status'),
            # Levels are per account, and two accounts holding the same stock
            # under different stop losses are two different situations. In the
            # key rather than merely on the row, so one of the two can never be
            # shown for both.
            row.get('stop_loss'), row.get('target'), bool(row.get('levels_armed')),
            # In the key for the same reason as the levels above. One account
            # with a holding behind it and one without are two different
            # situations, and merging them would put an Edit button on a row
            # where half the accounts have nothing to write to.
            bool(row.get('can_set_levels')), row.get('exit_mode'),
        )
        entry = {
            'account_id': row.get('account_id'),
            'account_name': row.get('account_name'),
            'quantity': row.get('quantity'),
            'avg_cost': row.get('avg_cost'),
            'invested': row.get('invested'),
            'market_value': row.get('market_value'),
            'pnl': row.get('pnl'),
            'pnl_pct': row.get('pnl_pct'),
        }

        # An account may appear in a row only once. The Order Book and Trade
        # Book mergers both enforce this and this one did not: a broker that
        # reports no product string leaves the key identical for a delivery and
        # an intraday line on the SAME account, and they would fold into one
        # row of 150 shares listing that account twice - a position that can be
        # sold and one that must be squared off by close, shown as one line.
        account_id = row.get('account_id')
        target = groups.get(key)
        if target is not None and any(
            entry_seen.get('account_id') == account_id
            for entry_seen in target['accounts']
        ):
            target = None
            key = None

        if target is None:
            row['accounts'] = [entry]
            row['accounts_count'] = 1
            if key is not None:
                groups[key] = row
            merged.append(row)
            continue

        target['accounts'].append(entry)
        target['accounts_count'] = len(target['accounts'])
        target['quantity'] = (target.get('quantity') or 0) + (row.get('quantity') or 0)
        target['invested'] = _money((target.get('invested') or 0) + (row.get('invested') or 0))
        target['market_value'] = _money(
            (target.get('market_value') or 0) + (row.get('market_value') or 0)
        )
        target['pnl'] = _money((target.get('pnl') or 0) + (row.get('pnl') or 0))
        target['short_quantity'] = (
            _to_int(target.get('short_quantity')) + _to_int(row.get('short_quantity'))
        )
        target['short_ids'] = (
            list(target.get('short_ids') or []) + list(row.get('short_ids') or [])
        )
        target['pnl_pct'] = _pct(
            signed_percent_of(target['pnl'], abs(target['invested']))
        )
        target['has_ltp'] = bool(target.get('has_ltp')) and bool(row.get('has_ltp'))

        # The row speaks for more than one account now, so naming one of them
        # would be a half truth.
        target['account_id'] = None
        target['account_name'] = None

        # Same for the trade nature. Two accounts holding the same stock under
        # two different purposes have no single honest answer, so the row says
        # nothing rather than picking whichever was read first.
        if target.get('trade_nature_id') != row.get('trade_nature_id'):
            target['trade_nature_id'] = None
            target['trade_nature'] = None

        # Weighted by shares, not by account, and on the size of each line
        # rather than its sign - a sale out of a holding carries a negative
        # quantity and would otherwise pull the average the wrong way.
        weight = sum(abs(item.get('quantity') or 0) for item in target['accounts'])
        if weight:
            target['avg_cost'] = _money(
                sum(
                    (item.get('avg_cost') or 0) * abs(item.get('quantity') or 0)
                    for item in target['accounts']
                ) / weight
            )

    return merged


def _empty_position_totals():
    return {
        'positions': 0, 'accounts': 0, 'accounts_in_view': 0, 'quantity': 0,
        'invested': 0.0, 'market_value': 0.0, 'pnl': 0.0, 'pnl_pct': 0.0,
    }


@equity_bp.route('/api/orders/reconcile', methods=['POST'])
@login_required
@heavy_rate_limit()
@_json_route
def api_reconcile_orders():
    """
    Ask the broker what happened to every order still outstanding.

    The same work the background pass does, run immediately because somebody
    pressed Refresh. It reads the broker's order book and trade book, records
    the fills, updates each account's split and rolls the parent order up.

    An order that timed out and never received a broker reference is matched
    against the broker's own order book and adopted, but only when exactly one
    unclaimed order matches on stock, side, quantity and time. Two candidates
    are indistinguishable, so neither is taken.

    Body (optional): {"account_ids": [1, 2]}
    Response: {"status", "message", "summary": {...}}
    """
    data = _body()
    account_ids = None
    if data.get('account_ids'):
        account_ids = _read_account_ids(data)

    try:
        from app.utils.equity_fill_reconciler import equity_fill_reconciler
        summary = equity_fill_reconciler.reconcile_user(
            current_user.id, account_ids=account_ids
        )
    except Exception as exc:
        current_app.logger.error(f'Equity reconciliation failed: {exc}', exc_info=True)
        return _json_error(f'The orders could not be reconciled: {exc}', 502)

    parts = []
    if summary.get('gtts_fired'):
        parts.append(
            f'{summary["gtts_fired"]} GTT(s) had fired and are now matched to '
            'the order each one released'
        )
    if summary.get('splits_adopted'):
        parts.append(f'{summary["splits_adopted"]} unconfirmed order(s) matched at the broker')
    if summary.get('fills_recorded'):
        parts.append(f'{summary["fills_recorded"]} fill(s) recorded')
    if summary.get('splits_updated'):
        parts.append(f'{summary["splits_updated"]} account order(s) updated')
    if summary.get('adoptions_ambiguous'):
        parts.append(
            f'{summary["adoptions_ambiguous"]} could not be matched safely, '
            'more than one broker order looked the same'
        )
    if summary.get('accounts_unreadable'):
        parts.append(f'{len(summary["accounts_unreadable"])} account(s) did not answer')
    if not parts:
        parts.append(
            'Nothing to reconcile'
            if not summary.get('splits_examined')
            else 'No change, the broker reports the same state'
        )

    _log_activity('equity_orders_reconciled', summary)
    return _ok({'summary': summary}, ', '.join(parts))


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


def _write_holding_levels(entries):
    """
    Write stop loss, target, exit mode and trade nature on a set of holdings.

    Extracted from the endpoint below on 6 September so the CSV upload arms a
    level through EXACTLY the same code the Edit dialog does. Two paths that
    each did their own re-arming would have drifted, and the one that drifted
    would have been the bulk one - the path where a mistake is repeated a
    hundred times rather than once.

    entries: the same shape the endpoint accepts, one dict per holding with
        account_id, symbol, exchange and whichever of stop_loss, target,
        exit_mode and trade_nature_id are being set. Only the keys present are
        changed; a key sent as null clears that field.

    Returns {results, updated, skipped, rearm_failed, error}. error is set only
    when the commit itself failed, in which case nothing was written.
    """
    account_ids, targets = _resolve_level_targets(entries)

    context = _account_context(fetch_holdings=True, fetch_account_ids=account_ids)
    wanted = set(account_ids)
    accounts = [account for account in context['accounts'] if account.id in wanted]
    tracked = _sync_holding_rows(accounts, context['snapshots'])

    results = []
    breaches = []
    for target in targets:
        holding, breach_kind, result = _apply_levels(target, tracked)
        results.append(result)
        if holding is None:
            continue
        if breach_kind is not None:
            breaches.append((holding.id, breach_kind))

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity holding level save failed: {exc}')
        return {
            'results': [], 'updated': 0, 'skipped': 0, 'rearm_failed': [],
            'error': f'Failed to save the levels: {exc}',
        }

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
    return {
        'results': results,
        'updated': updated,
        'skipped': len(results) - updated,
        'rearm_failed': rearm_failed,
        'error': None,
    }


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

    written = _write_holding_levels(entries)
    if written.get('error'):
        return _json_error(written['error'], 500)

    results = written['results']
    rearm_failed = written['rearm_failed']
    updated = written['updated']
    skipped = written['skipped']
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


# ---------------------------------------------------------------------------
# The Holdings upload - BUILT, THEN REMOVED (6 September)
#
# NOT REACHABLE. The two routes that used this were deleted at the owner's
# instruction on the day it was built, after he used it once and read the
# concerns back. Kept, with the reasoning, because reversing the decision is
# two route functions rather than four hundred lines - and because the
# concerns below are worth having written down whatever is built next.
#
# WHY IT WENT
#   1. Blank means clear, and Excel is very good at making blanks. Deleting a
#      column's CONTENTS to mean "leave this alone" clears every stop loss in
#      the file. The rule is the opposite - delete the whole COLUMN - and it
#      depends on the preview being read.
#   2. A stale file silently reverts recent work. A file downloaded on Monday
#      and uploaded on Friday carries Monday's levels and undoes everything
#      changed on screen in between. The fingerprint guards preview against
#      apply; it knows nothing about how old the FILE is. This was never
#      guarded.
#   3. One press changes everything at once, and there is no undo on the
#      server.
#
#   The same first two edges exist on the WATCH LIST upload, which is still
#   live. They matter less there - a cleared target price or a paused alert is
#   not a sold share - but the cleared NOTE is prose nobody can retype, and
#   that note is shared with this screen.
#
# BY STOCK, never by account. One line per stock, applied to every account that
# holds it. The owner said it twice and it is the whole shape of the feature:
# the file has no account column, and there is nowhere in it to name one.
#
# What may be changed: Stop Loss, Target, Exit Mode, Trade Nature and the
# note (Thesis, Risk, To Watch). Everything else in the download is a read-only
# figure and is ignored on the way back in.
#
# The rules are the watch list's, because a person who has used one upload
# should not have to learn a second set:
#   a column that is NOT in the file leaves that field alone;
#   a column that IS there and blank CLEARS it;
#   an upload may change, never add and never remove;
#   preview first, apply second, and the apply re-plans from the file itself.
# ---------------------------------------------------------------------------

HOLDINGS_KEY_COLUMNS = ('symbol', 'exchange')

HOLDINGS_EDIT_COLUMNS = (
    'stop loss', 'target', 'exit mode', 'trade nature',
    'thesis', 'risk', 'to watch',
)

HOLDINGS_IMPORT_COLUMNS = HOLDINGS_KEY_COLUMNS + HOLDINGS_EDIT_COLUMNS

# What a person actually types for an exit mode, and what it means. The
# download writes AE and CE; a person editing by hand writes the words.
EXIT_MODE_WORDS = {
    'ae': EQUITY_EXIT_MODE_AUTO,
    'auto': EQUITY_EXIT_MODE_AUTO,
    'auto sell': EQUITY_EXIT_MODE_AUTO,
    'autosell': EQUITY_EXIT_MODE_AUTO,
    'ce': EQUITY_EXIT_MODE_CONFIRM,
    'confirm': EQUITY_EXIT_MODE_CONFIRM,
    'to confirm': EQUITY_EXIT_MODE_CONFIRM,
    'toconfirm': EQUITY_EXIT_MODE_CONFIRM,
}


def _held_by_symbol():
    """
    Every holding this user has, grouped by (symbol, exchange).

    Read from AlgoMirror's own holding rows rather than the broker, because
    those are what carries a stop loss and what the upload writes to. A row
    with nothing left in it is not a holding any more and is left out, so a
    file naming a stock sold last week is told so rather than silently arming
    a level on nothing.
    """
    grouped = {}
    rows = EquityHolding.query.filter(
        EquityHolding.user_id == current_user.id
    ).all()
    for row in rows:
        if _to_int(row.quantity) <= 0:
            continue
        key = (
            str(row.symbol or '').strip().upper(),
            str(row.exchange or 'NSE').strip().upper(),
        )
        grouped.setdefault(key, []).append(row)
    return grouped


def _plan_holdings_import(csv_text):
    """
    Work out what an uploaded file would do to the holdings, writing nothing.

    Returns one entry per row: CHANGE, UNCHANGED or SKIP with the reason. The
    same call runs for the preview and again for the apply, so the apply acts
    on what it plans itself rather than on anything the browser hands back.

    A round trip of an unedited download plans nothing but UNCHANGED. That is
    the single best test this feature has and there is a check for it.
    """
    text = str(csv_text or '')
    if not text.strip():
        raise _BadRequest('That file is empty')

    # Excel writes a byte order mark on a CSV saved as UTF-8, and it lands on
    # the first heading. Left there, the Symbol column is never recognised.
    if text.startswith('﻿'):
        text = text[1:]

    try:
        raw_rows = list(csv.reader(io.StringIO(text)))
    except Exception as exc:
        raise _BadRequest('That file could not be read as a CSV: %s' % exc)

    raw_rows = [row for row in raw_rows if any(str(cell).strip() for cell in row)]
    if not raw_rows:
        raise _BadRequest('That file has no rows in it')

    headers = [_import_header_key(cell) for cell in raw_rows[0]]
    index = {}
    for position, name in enumerate(headers):
        if name in HOLDINGS_IMPORT_COLUMNS and name not in index:
            index[name] = position
    if 'symbol' not in index:
        raise _BadRequest(
            'That file has no Symbol column. Download the holdings first and '
            'edit that file, so the headings are the ones this expects.'
        )

    body = raw_rows[1:]
    if len(body) > MAX_IMPORT_ROWS:
        raise _BadRequest(
            'That file has %d rows. The most one upload may carry is %d.'
            % (len(body), MAX_IMPORT_ROWS)
        )

    def cell(row, name):
        position = index.get(name)
        if position is None or position >= len(row):
            return ''
        return str(row[position]).strip()

    held = _held_by_symbol()
    natures_by_name = {
        str(nature.name or '').strip().lower(): nature
        for nature in _trade_natures()
    }
    account_names = _account_directory()

    # Which of the four level columns the file actually carries. Absent means
    # "leave alone"; present and blank means "clear", and that difference is
    # the whole safety of this feature.
    has = {name: (name in index) for name in HOLDINGS_EDIT_COLUMNS}
    note_columns = [
        (heading, field) for heading, field in NOTE_IMPORT_COLUMNS
        if heading in index
    ]
    existing_notes = {}
    if note_columns:
        existing_notes = _notes_by_key([
            (cell(row, 'symbol').upper(), (cell(row, 'exchange') or 'NSE').upper())
            for row in body if cell(row, 'symbol')
        ])

    plan = []
    seen = set()

    for offset, row in enumerate(body):
        line = offset + 2   # the heading is line 1, as Excel numbers it
        entry = {
            'line': line, 'action': 'SKIP', 'reason': '',
            'symbol': '', 'exchange': '', 'accounts': [],
            'changes': [], 'arms_auto': False, 'clears_level': False,
        }

        symbol = cell(row, 'symbol').upper()
        if not symbol:
            entry['reason'] = 'no symbol in this row'
            plan.append(entry)
            continue
        if symbol == 'TOTAL':
            # The download writes a TOTAL line under the rows. Skipped by name
            # and said so, rather than reported as a stock nobody holds.
            entry['symbol'] = symbol
            entry['reason'] = 'this is the TOTAL line of the download'
            plan.append(entry)
            continue
        entry['symbol'] = symbol

        exchange = (cell(row, 'exchange') or 'NSE').upper()
        entry['exchange'] = exchange

        key = (symbol, exchange)
        if key in seen:
            entry['reason'] = 'this stock appears twice in the file'
            plan.append(entry)
            continue
        seen.add(key)

        holdings = held.get(key) or []
        if not holdings:
            entry['reason'] = 'you do not hold this stock, so there is nothing to set'
            plan.append(entry)
            continue
        entry['accounts'] = sorted(
            (account_names.get(holding.account_id) or {}).get(
                'account_name', 'Account %s' % holding.account_id
            )
            for holding in holdings
        )

        # ---- the four level columns ---------------------------------------
        wanted = {}

        if has['stop loss']:
            stop_loss, error = _import_price(cell(row, 'stop loss'))
            if error:
                entry['reason'] = 'the stop loss %s' % error
                plan.append(entry)
                continue
            wanted['stop_loss'] = stop_loss

        if has['target']:
            target, error = _import_price(cell(row, 'target'))
            if error:
                entry['reason'] = 'the target %s' % error
                plan.append(entry)
                continue
            wanted['target'] = target

        # Checked against what will be in force after this row, which is the
        # new value where the file sets one and the stored value where it does
        # not. Checking only the cells present would let a file that sets a
        # stop loss above an untouched target through.
        stored = holdings[0]
        after_sl = wanted.get('stop_loss', _to_float(stored.stop_loss) or None)
        after_tp = wanted.get('target', _to_float(stored.target) or None)
        if after_sl is not None and after_tp is not None and after_sl >= after_tp:
            entry['reason'] = (
                'the stop loss must be below the target, and this row leaves '
                'it at or above'
            )
            plan.append(entry)
            continue

        if has['exit mode']:
            mode_text = cell(row, 'exit mode')
            if mode_text:
                mode = EXIT_MODE_WORDS.get(mode_text.strip().lower())
                if mode is None:
                    entry['reason'] = (
                        'the exit mode must read AE, CE, Auto Sell or To '
                        'Confirm, not "%s"' % mode_text
                    )
                    plan.append(entry)
                    continue
                wanted['exit_mode'] = mode
            else:
                # Blank clears it to the SAFE reading, not to nothing. There is
                # no such thing as a holding with no exit mode: an empty cell
                # means "do not sell without asking me".
                wanted['exit_mode'] = EQUITY_EXIT_MODE_CONFIRM

        if has['trade nature']:
            nature_name = cell(row, 'trade nature')
            # The words this application itself uses for "no trade nature".
            # A stock with none reads "Not set" on the table and "Unassigned"
            # in the picker, so a file that carries either back is saying
            # exactly what the screen said - not naming a nature that does not
            # exist. Refusing those was a fault the owner hit on his first real
            # upload.
            if nature_name.strip().lower() in ('unassigned', 'not set', 'none', '-', '--'):
                nature_name = ''
            if nature_name:
                nature = natures_by_name.get(nature_name.strip().lower())
                if nature is None:
                    entry['reason'] = ('there is no trade nature called "%s"'
                                       % nature_name)
                    plan.append(entry)
                    continue
                wanted['trade_nature_id'] = nature.id
            else:
                wanted['trade_nature_id'] = None

        # ---- the note, which belongs to the stock --------------------------
        note_values = None
        note_changes = []
        if note_columns:
            note_values = {}
            for heading, field in note_columns:
                note_text = str(cell(row, heading) or '').strip()
                if len(note_text) > MAX_NOTE_CHARS:
                    note_values = None
                    entry['reason'] = ('the %s is %d characters, and the limit '
                                       'is %d' % (heading, len(note_text),
                                                  MAX_NOTE_CHARS))
                    break
                note_values[field] = note_text
            if note_values is None:
                plan.append(entry)
                continue

            note = existing_notes.get(_note_key(symbol, exchange))
            for heading, field in note_columns:
                before = (getattr(note, field, None) or '') if note is not None else ''
                after = note_values[field]
                if before.strip() == after.strip():
                    continue
                label = 'To Watch' if field == 'to_watch' else field.title()
                if after:
                    note_changes.append(
                        '%s %s' % (label, 'written' if not before else 'changed')
                    )
                else:
                    note_changes.append('%s CLEARED' % label)
            entry['note_values'] = note_values
            entry['note_changes'] = note_changes

        # ---- what would actually change, account by account ----------------
        changes = []
        for field, value in wanted.items():
            differing = [
                holding for holding in holdings
                if _level_differs(holding, field, value)
            ]
            if differing:
                changes.append({
                    'field': field,
                    'from': _level_now(holdings, field, natures_by_name),
                    'to': value,
                    'accounts': len(differing),
                })

        if not changes and not note_changes:
            entry['action'] = 'UNCHANGED'
            plan.append(entry)
            continue

        entry['action'] = 'CHANGE'
        entry['values'] = wanted
        entry['holding_ids'] = [holding.id for holding in holdings]
        entry['changes'] = (
            _describe_holding_changes(changes, natures_by_name) + note_changes
        )
        # The two things a person would not want to discover afterwards.
        entry['arms_auto'] = bool(
            wanted.get('exit_mode') == EQUITY_EXIT_MODE_AUTO
            and any(change['field'] == 'exit_mode' for change in changes)
        )
        entry['clears_level'] = any(
            change['field'] in ('stop_loss', 'target') and change['to'] is None
            for change in changes
        )
        plan.append(entry)

    return plan


def _level_differs(holding, field, value):
    """Whether one holding would actually change on this field."""
    if field == 'trade_nature_id':
        return (holding.trade_nature_id or None) != (value or None)
    if field == 'exit_mode':
        current = holding.exit_mode or EQUITY_EXIT_MODE_CONFIRM
        return current != value
    current = getattr(holding, field, None)
    current = None if current is None else round(float(current), 4)
    return current != (None if value is None else round(float(value), 4))


def _level_now(holdings, field, natures_by_name):
    """
    What this field reads today, for the preview's "from" side.

    None when the contributing accounts disagree, because there is no honest
    single answer to print and the preview must not invent one.
    """
    values = set()
    for holding in holdings:
        if field == 'trade_nature_id':
            values.add(holding.trade_nature_id or None)
        elif field == 'exit_mode':
            values.add(holding.exit_mode or EQUITY_EXIT_MODE_CONFIRM)
        else:
            current = getattr(holding, field, None)
            values.add(None if current is None else round(float(current), 4))
    if len(values) != 1:
        return '__MIXED__'
    return values.pop()


def _describe_holding_changes(changes, natures_by_name):
    """What a CHANGE will alter, in words, old value and new."""
    labels = {
        'stop_loss': 'stop loss',
        'target': 'target',
        'exit_mode': 'exit mode',
        'trade_nature_id': 'trade nature',
    }

    def render(field, value):
        if value == '__MIXED__':
            return 'differing values'
        if value is None or value == '':
            return 'nothing'
        if field == 'trade_nature_id':
            return _nature_name_for(value, natures_by_name) or 'nothing'
        if field == 'exit_mode':
            return 'Auto Sell' if value == EQUITY_EXIT_MODE_AUTO else 'To Confirm'
        # Rendered here rather than left as a float, because this string is
        # read by a person deciding whether to press Apply.
        return '%s%s' % ('\u20b9', '{:,.2f}'.format(float(value)))

    lines = []
    for change in changes:
        field = change['field']
        lines.append('%s %s to %s on %d account(s)' % (
            labels.get(field, field),
            render(field, change['from']),
            render(field, change['to']),
            change['accounts'],
        ))
    return lines


def _holdings_import_summary(plan):
    """
    The counts the screen leads with.

    Auto Sell and cleared levels are counted apart from everything else,
    because they are the two things in a file a person would not want to
    discover afterwards. Auto Sell sells on a breach with no further
    confirmation; a cleared stop loss stops a sale that would have happened.
    """
    live = [row for row in plan if row['action'] == 'CHANGE']
    return {
        'rows': len(plan),
        'change': len(live),
        'unchanged': sum(1 for row in plan if row['action'] == 'UNCHANGED'),
        'skipped': sum(1 for row in plan if row['action'] == 'SKIP'),
        'arms_auto': sum(1 for row in live if row.get('arms_auto')),
        'levels_cleared': sum(1 for row in live if row.get('clears_level')),
        'notes_changed': sum(1 for row in live if row.get('note_changes')),
    }


def _holdings_import_fingerprint(plan):
    """
    A short hash of exactly what the plan would do.

    The apply re-plans from the same file and compares. If a holding moved
    underneath in between - a fill, a sale, a stop loss firing - the two differ
    and the apply refuses rather than doing something that was never shown.
    """
    parts = []
    for row in plan:
        parts.append('%s|%s|%s|%s|%s' % (
            row.get('line'), row.get('action'), row.get('symbol'),
            row.get('exchange'), ','.join(str(i) for i in row.get('holding_ids') or [])
        ))
        for change in row.get('changes') or []:
            parts.append('  %s' % change)
        for field, value in sorted((row.get('note_values') or {}).items()):
            parts.append('  note %s=%s' % (field, value))
    return hashlib.sha256('\n'.join(parts).encode('utf-8')).hexdigest()[:16]


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
                      "price_alerts_enabled", "order_timeout_seconds",
                      "monitor_last_run_at",
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
         "price_alerts_enabled": true,
         "order_timeout_seconds": 30,
         "intraday_monitor_enabled": true,
         "intraday_cutoff_at": "15:00",
         "intraday_squareoff_at": "15:12"}

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

    if 'order_timeout_seconds' in data:
        value = _read_int(
            data, 'order_timeout_seconds',
            minimum=MIN_ORDER_TIMEOUT_SECONDS,
            maximum=MAX_ORDER_TIMEOUT_SECONDS,
            required=True
        )
        if value != _to_int(settings.order_timeout_seconds):
            changes['order_timeout_seconds'] = {
                'from': _to_int(settings.order_timeout_seconds), 'to': value
            }
            settings.order_timeout_seconds = value

    # The intraday short controls. Read as a group, because the cut-off and the
    # square-off only make sense against each other and validating them one at
    # a time lets a save leave the pair in a state that opens shorts nothing
    # would ever close.
    if ('intraday_cutoff_at' in data) or ('intraday_squareoff_at' in data):
        squareoff = (
            _read_clock(data, 'intraday_squareoff_at',
                        MIN_INTRADAY_MINUTE, MAX_SQUAREOFF_MINUTE)
            if 'intraday_squareoff_at' in data
            else _to_int(getattr(settings, 'intraday_squareoff_minute', 0))
        )
        cutoff = (
            _read_clock(data, 'intraday_cutoff_at',
                        MIN_INTRADAY_MINUTE, MAX_SQUAREOFF_MINUTE)
            if 'intraday_cutoff_at' in data
            else _to_int(getattr(settings, 'intraday_cutoff_minute', 0))
        )
        if cutoff >= squareoff:
            raise _BadRequest(
                'The last time a short may be OPENED (%s) has to be before the '
                'time they are all bought back (%s). Otherwise a short could be '
                'opened that nothing would ever close.' % (
                    _minute_to_clock(cutoff), _minute_to_clock(squareoff)
                )
            )
        if squareoff != _to_int(getattr(settings, 'intraday_squareoff_minute', 0)):
            changes['intraday_squareoff_at'] = {
                'from': _minute_to_clock(
                    getattr(settings, 'intraday_squareoff_minute', 0)),
                'to': _minute_to_clock(squareoff),
            }
            settings.intraday_squareoff_minute = squareoff
        if cutoff != _to_int(getattr(settings, 'intraday_cutoff_minute', 0)):
            changes['intraday_cutoff_at'] = {
                'from': _minute_to_clock(
                    getattr(settings, 'intraday_cutoff_minute', 0)),
                'to': _minute_to_clock(cutoff),
            }
            settings.intraday_cutoff_minute = cutoff

    if 'intraday_monitor_enabled' in data:
        value = bool(_read_bool(data, 'intraday_monitor_enabled', default=True))
        if value != bool(getattr(settings, 'intraday_monitor_enabled', True)):
            changes['intraday_monitor_enabled'] = {
                'from': bool(getattr(settings, 'intraday_monitor_enabled', True)),
                'to': value,
            }
            settings.intraday_monitor_enabled = value

    try:
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        current_app.logger.error(f'Equity preferences save failed: {exc}')
        return _json_error(f'Failed to save the equity preferences: {exc}', 500)

    if changes:
        _log_activity('equity_preferences_saved', {'changes': changes})

    return _ok(_build_preferences_payload(), 'Equity preferences saved')


# ---------------------------------------------------------------------------
# Background account cache warmer
#
# WHY THIS EXISTS. The F&O dashboard renders in well under a second because its
# request path contains no network at all: it SELECTs rows that background
# services keep current, and the page is four queries and a template. The
# equity dashboard was doing the opposite - every thirty second poll WAS the
# refresh job, calling funds() and holdings() at the broker for each account on
# the request path, which is the four to five seconds that was visible.
#
# The freshness gate in _fan_out was already there to prevent exactly this, and
# it was never firing: BROKER_CACHE_TTL_SECONDS is 30 and the dashboard polls
# every 30 seconds, so by the time each poll arrived the cache had just aged
# out. The gate needs somebody OTHER than the request to keep the cache warm.
# That is all this does. No screen code changed; the same gate now hits.
# ---------------------------------------------------------------------------

# Comfortably inside BROKER_CACHE_TTL_SECONDS, so a request arriving at any
# moment finds the cache fresh rather than racing the warmer.
CACHE_WARM_INTERVAL_SECONDS = 20

# How long after somebody last looked at an equity screen the warmer keeps
# running outside market hours.
CACHE_WARM_IDLE_SECONDS = 300


def _market_is_open_now():
    """
    Roughly, is the Indian cash market open. Deliberately generous at both
    ends, because the cost of being wrong is one extra broker read.
    """
    now = datetime.utcnow() + timedelta(hours=5, minutes=30)
    if now.weekday() >= 5:
        return False
    minutes = now.hour * 60 + now.minute
    return (9 * 60) <= minutes <= (15 * 60 + 45)


def warm_account_cache():
    """
    Refresh every active account's funds and holdings, off the request path.

    Called from the scheduler, never from a view. Returns the number of
    accounts refreshed, or 0 when the pass was skipped.

    Scoped to accounts rather than to a user, because the cache columns being
    filled belong to the account row. Outside market hours it only runs while
    somebody is actually using an equity screen, so a machine left on overnight
    makes no broker calls.

    NOTE ON SHARED STATE. This writes TradingAccount.last_funds_data,
    last_holdings_data and last_data_update - the same columns the equity
    request path has always written, and which the F&O funds screen reads as
    its own cache. Nothing about that read changes; those columns simply get
    refreshed on a timer now instead of only when an equity page was open. No
    F&O code is touched.
    """
    if not _market_is_open_now():
        idle_for = time.monotonic() - _LAST_EQUITY_VIEW_AT[0]
        if _LAST_EQUITY_VIEW_AT[0] <= 0 or idle_for > CACHE_WARM_IDLE_SECONDS:
            return 0

    accounts = TradingAccount.query.filter_by(
        is_active=True
    ).order_by(TradingAccount.id).all()
    if not accounts:
        return 0

    creds = _account_credentials(accounts, force_refresh=True)
    snapshots = _fan_out(creds, want_funds=True, want_holdings=True)
    _refresh_account_cache(accounts, snapshots)

    # And the books behind Today's Orders. Once funds and holdings came off the
    # request path these were the whole of the dashboard's remaining load time,
    # for a summary widget. Read here instead, on the same tick.
    #
    # A failure is not allowed to cost the funds refresh that already
    # succeeded: the books simply stay as they were and the next screen that
    # needs them reads live, which is the behaviour before any of this existed.
    try:
        from app.utils.equity_fill_reconciler import read_broker_books
        books, unreadable = read_broker_books(accounts)
        _remember_books(accounts, books, unreadable)
    except Exception as exc:
        current_app.logger.warning(
            f'[EQUITY_CACHE] Could not warm the broker books, screens will '
            f'read them live: {exc}'
        )

    return len(snapshots)
