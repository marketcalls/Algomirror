"""
Fill reconciliation for equity orders.

What this closes
----------------
Placing an order tells you it was accepted. It does not tell you whether it
filled, at what price, or whether the broker rejected it a moment later. Until
something asks, an order sits at PENDING or PARTIAL for ever, the Trade Book
stays empty, and no holding is ever built - which is exactly the state the
module shipped in. The schema for all of this already exists: EquityTrade with
its de-duplicating index, and fill_status, filled_quantity, avg_fill_price,
broker_order_status and last_synced_at on every split. Only the engine was
missing. This is that engine.

How it reads
------------
Two calls per account, not per order: orderbook() gives the state of every
order, tradebook() gives the fills. A hundred orders across two accounts is
four broker calls, not two hundred.

Adoption of orphaned splits
---------------------------
A placement that times out leaves a split with no broker_order_id, so there is
no handle to look it up by - the order is real at the broker and invisible
here. Adoption closes that gap by matching the broker's own order book on
stock, side, quantity, product and a time window around the placement, and it
adopts ONLY when exactly one unclaimed candidate matches. Two candidates means
two identical orders and no way to tell them apart, so it adopts neither and
says so. Guessing here would attach a fill to the wrong order, which is worse
than leaving it unknown.

Idempotency
-----------
Every pass re-reads the same broker rows, so everything here is written to be
safe to repeat. Fills are keyed by a content fingerprint of the fill itself,
so the same fill always produces the same key and the unique index refuses the
duplicate. Nothing is ever incremented; quantities are set from what the broker
currently reports.

This module never places, modifies or cancels anything. It only reads and
records.
"""

import logging
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from typing import Dict, List, Optional

from flask import current_app, has_app_context
from sqlalchemy import or_

from app import db
from app.models import (
    EQUITY_EXIT_MODE_CONFIRM,
    EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT,
    EQUITY_SIDE_BUY,
    EQUITY_SIDE_SELL,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_INDETERMINATE,
    EQUITY_SPLIT_STATUS_PARTIAL,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
    EquityOrder,
    EquityOrderSplit,
    EquityTrade,
    EquityHolding,
    EquitySetting,
    TradingAccount,
)
from app.utils.equity_order_engine import recompute_parent_status
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# One reconciliation pass at most this often, however fast the tick that calls
# us is. Fills do not need second-by-second attention: the screens that show
# them refresh on their own, and a broker read costs a network round trip.
RECONCILE_INTERVAL_SECONDS = 20

# How far back a split is still worth chasing. An order left open for longer
# than this is not going to resolve itself, and re-reading it on every pass for
# ever would be a slow leak of broker calls.
RECONCILE_WINDOW_DAYS = 7

# Floor for a broker read. The admin's order timeout is used when it is longer,
# because it is the number that already expresses how slow this particular
# broker is; a sandbox that takes a minute to place an order takes about as long
# to answer a book.
BROKER_READ_TIMEOUT_SECONDS = 20
MAX_BROKER_READ_TIMEOUT_SECONDS = 180

# How far either side of the recorded placement time an unclaimed broker order
# may sit and still be considered the same order. Wide enough to cover a
# placement that took a minute to answer, narrow enough that yesterday's
# identical order is never a candidate.
ADOPTION_WINDOW_MINUTES = 15

# The GTT book endpoint. The installed SDK wraps no GTT method, so it is posted
# through the client's own request helper, exactly as the order engine does for
# placegttorder.
GTT_BOOK_ENDPOINT = 'gttorderbook'

# Where a trigger id can appear in a GTT book row. The same four spellings the
# order engine accepts from a placegttorder reply.
GTT_ID_KEYS = ('trigger_id', 'triggerid', 'gtt_id', 'gttid')

# How far BEFORE the GTT was placed a released order may still be timestamped
# and be believed. Not a window, a tolerance: the released order must come
# after the instruction that released it, and this only allows for the two
# clocks disagreeing.
GTT_FIRE_SKEW_MINUTES = 5

# How recently a buy must have filled for a missing holding row to be read as
# "not settled yet" rather than "not there". Settlement is T+1, so a day would
# do; this is deliberately looser to cover a long weekend and a market holiday
# on the end of it. Beyond it, a stock the broker does not list is a stock the
# broker does not hold, and arming a stop loss against it would be arming it
# against nothing.
UNSETTLED_WINDOW_DAYS = 5

# Times in this application are stored as UTC. Indian brokers report theirs in
# IST, with no marker to say so. Comparing the two directly is a five and a half
# hour error that rejects a perfect match, which is exactly what happened on
# 2026-08-30: a split placed at 13:10:46 UTC against a broker order stamped
# 18:41:50 IST - the same instant, 19,860 seconds apart on paper.
IST_OFFSET = timedelta(hours=5, minutes=30)

# Broker order status text, lower cased, mapped onto a split's fill status.
# Anything unrecognised leaves the split alone rather than guessing.
BROKER_STATUS_MAP = {
    'complete': EQUITY_SPLIT_STATUS_COMPLETED,
    'completed': EQUITY_SPLIT_STATUS_COMPLETED,
    'filled': EQUITY_SPLIT_STATUS_COMPLETED,
    'executed': EQUITY_SPLIT_STATUS_COMPLETED,
    'open': EQUITY_SPLIT_STATUS_PENDING,
    'pending': EQUITY_SPLIT_STATUS_PENDING,
    'trigger pending': EQUITY_SPLIT_STATUS_PENDING,
    'open pending': EQUITY_SPLIT_STATUS_PENDING,
    'validation pending': EQUITY_SPLIT_STATUS_PENDING,
    'put order req received': EQUITY_SPLIT_STATUS_PENDING,
    'modify pending': EQUITY_SPLIT_STATUS_PENDING,
    'partial': EQUITY_SPLIT_STATUS_PARTIAL,
    'partially filled': EQUITY_SPLIT_STATUS_PARTIAL,
    'cancelled': EQUITY_SPLIT_STATUS_CANCELLED,
    'canceled': EQUITY_SPLIT_STATUS_CANCELLED,
    'rejected': EQUITY_SPLIT_STATUS_REJECTED,
}

# Splits worth looking at: still working, or placed but never confirmed.
CHASEABLE_STATUSES = (
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_PARTIAL,
    EQUITY_SPLIT_STATUS_INDETERMINATE,
)

# Statuses at which the broker is finished with the order. Nothing more can
# fill, so the holding behind it can safely be settled or released.
#
# PARTIAL is deliberately NOT here. A partially filled order is still resting
# at the broker for its remainder, and handing the holding back at that point
# re-arms the stop loss monitor against shares already committed to a sell.
CLOSED_SPLIT_STATUSES = (
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_REJECTED,
)

# What different broker adapters call the quantity that has filled so far, and
# the quantity still working. Adapters report one, the other, or neither.
FILLED_QUANTITY_KEYS = (
    'filled_quantity', 'filledshares', 'filled_qty', 'filledqty',
    'fillsize', 'filled', 'cumulative_quantity', 'cumqty',
)
PENDING_QUANTITY_KEYS = (
    'pending_quantity', 'pendingqty', 'pending_qty',
    'remaining_quantity', 'unfilled_quantity',
)


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default


def _text(value):
    return str(value or '').strip()


def _fill_fingerprint(order_id, entry):
    """
    A stable key for one fill.

    OpenAlgo's tradebook carries no trade id, and the unique index on
    (split_id, broker_trade_id) cannot de-duplicate NULLs. So the key is built
    from the fill itself: the same fill always produces the same string, and a
    genuinely different fill produces a different one. Two fills identical in
    order, time, quantity and price are indistinguishable to any observer, so
    collapsing them is the correct behaviour rather than a limitation.
    """
    parts = [
        _text(order_id),
        _text(entry.get('timestamp')),
        str(_to_int(entry.get('quantity'))),
        '%.4f' % _to_float(entry.get('average_price')),
    ]
    return ':'.join(parts)[:100]


def _parse_broker_time(value):
    """Broker timestamps arrive as text in a handful of shapes. None if unclear."""
    text = _text(value)
    if not text:
        return None
    for shape in ('%d-%b-%Y %H:%M:%S', '%d-%b-%Y %H:%M', '%Y-%m-%d %H:%M:%S',
                  '%Y-%m-%dT%H:%M:%S', '%d/%m/%Y %H:%M:%S'):
        try:
            return datetime.strptime(text, shape)
        except ValueError:
            continue
    return None


# The timezone a broker reports its timestamps in. Indian brokers report IST
# and mark nothing, and this module talks to Indian brokers through OpenAlgo,
# so IST is the assumption - stated here, in one place, rather than inferred
# per timestamp. If a broker is ever added that reports UTC, this becomes a
# per-account setting; it does not become a guess.
BROKER_CLOCK_OFFSET = IST_OFFSET

# How far a converted fill may sit from the order that produced it before the
# conversion is worth complaining about. Generous, because a limit order can
# rest all day.
BROKER_CLOCK_SANITY_HOURS = 12


def broker_time_to_utc(value, anchor=None):
    """
    A broker's unmarked timestamp, converted to the UTC this application stores.

    Indian brokers report IST and say nothing about it. AlgoMirror stores UTC
    and every screen renders UTC back into IST for display. Writing the broker's
    IST digits straight into a UTC column therefore does not merely mislabel the
    value: the screen then adds five and a half hours to it, and a fill at 09:44
    is shown at 15:14.

    The conversion is a plain subtraction of BROKER_CLOCK_OFFSET. It is
    deliberately NOT inferred per timestamp, and the first version of this
    function was wrong for exactly that reason. It picked between the two
    readings by asking whether the raw value would be in the future - but IST
    digits only look like the future while the event is less than five and a
    half hours old. A fill at 09:44 read correctly all morning and then, from
    about 14:45 onward, silently reverted to 15:14. A rule that is right in the
    morning and wrong in the afternoon is worse than no rule, because it passes
    every test run before lunch.

    The general point: the digits "09:44" are the same whether they are IST
    from six hours ago or UTC from half an hour ago. No amount of arithmetic
    separates those. Only knowing the broker's clock does.

    anchor, where the caller has one - the moment AlgoMirror itself placed the
    order, which is genuine UTC - is used as a SANITY CHECK, not as the
    decision. A converted fill that lands more than half a day from its own
    order says the assumption above is wrong for this broker, and that is worth
    a log line rather than a silent correction.

    Returns None when the text cannot be parsed at all, which the caller treats
    as "no timestamp" rather than inventing one.
    """
    stamped = _parse_broker_time(value)
    if stamped is None:
        return None

    converted = stamped - BROKER_CLOCK_OFFSET

    if anchor is not None:
        drift = abs((converted - anchor).total_seconds())
        if drift > BROKER_CLOCK_SANITY_HOURS * 3600:
            logger.warning(
                '[EQUITY_FILL] Broker timestamp %r converts to %s, which is %.1f '
                'hours from the order placed at %s. The broker clock is assumed '
                'to be IST; if this broker reports UTC that assumption is wrong.',
                value, converted, drift / 3600.0, anchor
            )

    return converted


class EquityFillReconciler:
    """
    Singleton fill reconciler.

    Public surface:
        run_checks()     one paced pass over every user, for the scheduler
        reconcile_user() one immediate pass for one user, for the Refresh button
        status()         diagnostics, safe to call at any time
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

        self._lock = threading.RLock()
        self._last_pass_monotonic = 0.0
        self._last_run_at: Optional[datetime] = None
        self._last_error: Optional[str] = None
        self._stats = {
            'passes': 0,
            'users_evaluated': 0,
            'accounts_read': 0,
            'splits_examined': 0,
            'splits_updated': 0,
            'splits_adopted': 0,
            'gtts_fired': 0,
            'adoptions_ambiguous': 0,
            'fills_recorded': 0,
            'orders_recomputed': 0,
        }

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def run_checks(self):
        """
        One paced pass over every user with something outstanding.

        Never raises. A background pass that throws is a background pass that
        eventually stops being scheduled, and fills would quietly stop being
        recorded with nothing on screen saying so.
        """
        if not has_app_context():
            logger.error(
                '[EQUITY_FILL] run_checks called with no Flask app context. '
                'Call it inside "with app.app_context():".'
            )
            return

        with self._lock:
            now = time.monotonic()
            if now - self._last_pass_monotonic < RECONCILE_INTERVAL_SECONDS:
                return
            self._last_pass_monotonic = now

        try:
            db.session.expire_all()

            # Before the chase, not after. A claim whose sell has already
            # filled is not waiting on a broker read, and closing it first
            # means a stranded holding is repaired even on a pass where every
            # account turns out to be unreadable.
            self._settle_exit_claims()

            for user_id in self._pending_user_ids():
                try:
                    self.reconcile_user(user_id)
                except Exception as exc:
                    logger.error(
                        '[EQUITY_FILL] User %s pass failed: %s', user_id, exc,
                        exc_info=True
                    )
                    self._safe_rollback()

            # And again, so a sell whose fill was recorded a moment ago on
            # this same pass does not wait twenty seconds to close.
            self._settle_exit_claims()
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error('[EQUITY_FILL] Pass failed: %s', exc, exc_info=True)
            self._safe_rollback()
        finally:
            self._stats['passes'] += 1
            self._last_run_at = datetime.utcnow()

    # ------------------------------------------------------------------
    # One user
    # ------------------------------------------------------------------

    def reconcile_user(self, user_id, account_ids=None) -> Dict:
        """
        Reconcile one user's outstanding splits and return what changed.

        Called both by the background pass and by the Refresh button, so the
        summary it returns is shaped for a screen as well as a log.
        """
        summary = {
            'splits_examined': 0,
            'splits_updated': 0,
            'splits_adopted': 0,
            'gtts_fired': 0,
            'adoptions_ambiguous': 0,
            'fills_recorded': 0,
            'exits_closed': 0,
            'orders_recomputed': 0,
            'accounts_read': 0,
            'accounts_unreadable': [],
        }

        # Before the early return below. A user can have no split left worth
        # chasing and still have a holding stuck on a sell that completed, and
        # pressing Refresh on that screen has to fix it.
        summary['exits_closed'] = self._settle_exit_claims(user_id)

        splits = self._chaseable_splits(user_id, account_ids)
        if not splits:
            return summary

        summary['splits_examined'] = len(splits)
        self._stats['splits_examined'] += len(splits)
        self._stats['users_evaluated'] += 1

        logger.info(
            '[EQUITY_FILL] User %s has %d split(s) to chase',
            user_id, len(splits)
        )

        by_account: Dict[int, List[EquityOrderSplit]] = {}
        for split in splits:
            by_account.setdefault(split.account_id, []).append(split)

        touched_order_ids = set()

        for account_id, account_splits in by_account.items():
            account = TradingAccount.query.filter_by(
                id=account_id, user_id=user_id
            ).first()
            if account is None or not account.is_active:
                continue

            books = self._read_books(account, timeout=self._read_timeout_for(user_id))
            if books is None:
                summary['accounts_unreadable'].append(account_id)
                continue

            summary['accounts_read'] += 1
            self._stats['accounts_read'] += 1

            orders, trades = books
            self._adopt_orphans(account_splits, orders, summary)

            # The GTT book is read only when this account is actually waiting
            # on a trigger. It is a second broker call and there is no reason
            # to pay for it on an account holding none.
            if self._has_resting_gtt(account_splits):
                self._adopt_fired_gtts(
                    account_splits, orders,
                    self._read_active_triggers(
                        account, timeout=self._read_timeout_for(user_id)
                    ),
                    summary,
                )

            for split in account_splits:
                if not split.broker_order_id:
                    continue
                changed = self._apply_order_state(split, orders, summary)
                recorded = self._record_fills(split, trades, summary)
                if changed or recorded:
                    touched_order_ids.add(split.equity_order_id)

        summary['exits_closed'] = (
            summary.get('exits_closed', 0) + self._settle_exit_claims(user_id)
        )

        if touched_order_ids or summary['splits_adopted']:
            try:
                db.session.commit()
            except Exception as exc:
                logger.error(
                    '[EQUITY_FILL] Could not save reconciliation for user %s: %s',
                    user_id, exc, exc_info=True
                )
                self._safe_rollback()
                return summary

        for order_id in touched_order_ids:
            try:
                recompute_parent_status(order_id, user_id, commit=False)
                summary['orders_recomputed'] += 1
                self._stats['orders_recomputed'] += 1
            except Exception as exc:
                logger.error(
                    '[EQUITY_FILL] Could not roll up order %s: %s', order_id, exc
                )

        if touched_order_ids:
            try:
                db.session.commit()
            except Exception as exc:
                logger.error('[EQUITY_FILL] Could not save order status: %s', exc)
                self._safe_rollback()

        # Last, and deliberately after the fills are committed: a buy that has
        # filled today is a position the monitor cannot see, because a holding
        # row does not exist until settlement. This raises one so the stop loss
        # set on the order is armed from the fill rather than from tomorrow.
        summary['unsettled_holdings'] = self._ensure_unsettled_holdings(user_id)

        return summary

    # ------------------------------------------------------------------
    # Unsettled holdings
    #
    # A delivery buy is a POSITION on the day it is bought and only becomes a
    # HOLDING at settlement. The stop loss and target monitor works on holding
    # rows, so until this existed a stock bought this morning with a stop loss
    # on the order was governed by nothing at all until tomorrow. Seen live on
    # 2026-09-04: INFY filled at 11:12 carrying a stop loss of 1120, with no
    # holding row anywhere for it to act on.
    #
    # The row raised here is an ordinary EquityHolding in every respect except
    # is_settled, which is False. Everything the monitor already does then
    # applies unchanged. Two other places read the flag: the holdings sync must
    # not retire the row while the broker cannot yet see it, and a sell must
    # verify its quantity against the position book rather than the holdings
    # book.
    # ------------------------------------------------------------------

    def _ensure_unsettled_holdings(self, user_id):
        """
        Raise a holding row for a buy that has filled but not yet settled.

        Returns how many rows were created or resized.

        Conservative on purpose:

        - Only ever creates a row where NONE exists for that account and stock.
          A stock already held is already governed by its own levels, and
          adding today's shares to that row would inflate a quantity the broker
          has not yet delivered. The existing row is left exactly alone.
        - The quantity is AlgoMirror's own net filled position - buys minus
          sells - so selling part of it back the same day reduces the row
          rather than leaving a stop loss armed on shares that have gone.
        - Levels and trade nature are inherited only when ONE set of them sits
          behind every filled share, the same rule the settled path already
          uses for the trade nature. Two different stop losses behind one
          position have no honest single answer, so the row starts bare.
        - A row with a sell in flight is never touched.
        """
        try:
            rows = self._own_positions(user_id)
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not total own positions for user %s: %s',
                user_id, exc
            )
            self._safe_rollback()
            return 0

        if not rows:
            return 0

        try:
            tracked = {
                (h.account_id, h.symbol.upper(), (h.exchange or 'NSE').upper()): h
                for h in EquityHolding.query.filter(
                    EquityHolding.user_id == user_id
                ).all()
            }
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not read holdings for user %s: %s', user_id, exc
            )
            self._safe_rollback()
            return 0

        settings = EquitySetting.query.filter_by(user_id=user_id).first()
        default_exit_mode = (
            settings.default_exit_mode if settings else EQUITY_EXIT_MODE_CONFIRM
        )
        settle_cutoff = datetime.utcnow() - timedelta(days=UNSETTLED_WINDOW_DAYS)

        changed = 0
        for key, facts in rows.items():
            net = _to_int(facts['net'])
            holding = tracked.get(key)

            if holding is not None:
                # Only an unsettled row of our own is kept in step. A settled
                # row belongs to the broker's figures and must not be rewritten
                # from our own arithmetic - that is rule D10a.
                if holding.is_settled or holding.is_exit_in_flight:
                    continue
                if _to_int(holding.quantity) == max(net, 0):
                    continue
                holding.quantity = max(net, 0)
                changed += 1
                logger.info(
                    '[EQUITY_FILL] Unsettled holding %s (%s) resized to %s shares',
                    holding.id, holding.symbol, max(net, 0)
                )
                continue

            if net <= 0:
                continue

            # A buy old enough to have settled several times over, with still
            # no holding row anywhere, is not an unsettled position - it is a
            # stock the broker says is not there. Inventing a row for it would
            # arm a stop loss against shares nobody can point to. Only a recent
            # fill raises one.
            last_buy_at = facts.get('last_buy_at')
            if last_buy_at is None or last_buy_at < settle_cutoff:
                continue

            holding = EquityHolding(
                user_id=user_id,
                account_id=key[0],
                symbol=key[1],
                exchange=key[2],
                quantity=net,
                avg_cost=facts['avg_cost'] or None,
                exit_mode=default_exit_mode,
                trade_nature_id=facts['trade_nature_id'],
                stop_loss=facts['stop_loss'],
                target=facts['target'],
                is_settled=False,
                # Measured, not absorbed. The broker has not delivered these
                # shares yet, so its holdings book reports none of them and the
                # baseline for "shares that moved without an order from here"
                # is zero until it does.
                external_quantity=None,
            )
            db.session.add(holding)
            changed += 1
            logger.info(
                '[EQUITY_FILL] Unsettled holding raised for account %s %s@%s: '
                '%s shares, stop loss %s, target %s',
                key[0], key[1], key[2], net, facts['stop_loss'], facts['target']
            )

        if not changed:
            return 0

        try:
            db.session.commit()
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not save unsettled holdings for user %s: %s',
                user_id, exc
            )
            self._safe_rollback()
            return 0

        self._stats['unsettled_holdings'] = (
            self._stats.get('unsettled_holdings', 0) + changed
        )
        return changed

    @staticmethod
    def _own_positions(user_id):
        """
        What AlgoMirror itself put into each account, by stock.

        Returns {(account_id, SYMBOL, EXCHANGE): {net, avg_cost,
        trade_nature_id, stop_loss, target}}.

        Net of sells and with no time window, for the same reason the settled
        path takes that view: a running total of every order this application
        ever placed for a stock is a complete answer at any moment, where "the
        last N days" goes quietly wrong the day N is too small.
        """
        rows = db.session.query(
            EquityOrder.side,
            EquityOrder.symbol,
            EquityOrder.exchange,
            EquityOrder.trade_nature_id,
            EquityOrder.stop_loss,
            EquityOrder.target,
            EquityOrder.placed_at,
            EquityOrderSplit.account_id,
            EquityOrderSplit.filled_quantity,
            EquityOrderSplit.quantity,
            EquityOrderSplit.avg_fill_price,
            EquityOrderSplit.fill_status,
        ).join(
            EquityOrderSplit, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == user_id,
            EquityOrderSplit.fill_status.in_(
                (EQUITY_SPLIT_STATUS_COMPLETED, EQUITY_SPLIT_STATUS_PARTIAL)
            ),
        ).all()

        found = {}
        for (side, symbol, exchange, nature_id, stop_loss, target, placed_at,
             account_id, filled, ordered, fill_price, fill_status) in rows:
            # filled_quantity is the truth. A split marked COMPLETED before the
            # reconciler wrote a fill back is trusted for its ordered quantity,
            # which is what completed means; anything else contributes nothing.
            shares = _to_int(filled)
            if shares <= 0 and fill_status == EQUITY_SPLIT_STATUS_COMPLETED:
                shares = _to_int(ordered)
            if shares <= 0:
                continue

            key = (
                account_id,
                (symbol or '').strip().upper(),
                (exchange or 'NSE').strip().upper(),
            )
            facts = found.setdefault(key, {
                'net': 0, 'bought': 0, 'cost': 0.0,
                'natures': set(), 'levels': set(), 'last_buy_at': None,
            })

            if side == EQUITY_SIDE_SELL:
                facts['net'] -= shares
                continue

            facts['net'] += shares
            if placed_at is not None and (
                facts['last_buy_at'] is None or placed_at > facts['last_buy_at']
            ):
                facts['last_buy_at'] = placed_at
            price = _to_float(fill_price)
            if price > 0:
                facts['bought'] += shares
                facts['cost'] += price * shares
            if nature_id is not None:
                facts['natures'].add(nature_id)
            if stop_loss is not None or target is not None:
                facts['levels'].add((
                    _to_float(stop_loss) or None,
                    _to_float(target) or None,
                ))

        resolved = {}
        for key, facts in found.items():
            # One answer or none. Two different natures, or two different pairs
            # of levels, behind the same position have no single honest answer,
            # so the row starts bare and the admin sets it.
            natures = facts['natures']
            levels = facts['levels']
            stop_loss, target = (None, None)
            if len(levels) == 1:
                stop_loss, target = next(iter(levels))

            resolved[key] = {
                'net': facts['net'],
                'avg_cost': (
                    facts['cost'] / facts['bought'] if facts['bought'] else 0.0
                ),
                'trade_nature_id': next(iter(natures)) if len(natures) == 1 else None,
                'stop_loss': stop_loss,
                'target': target,
                'last_buy_at': facts['last_buy_at'],
            }
        return resolved

    # ------------------------------------------------------------------
    # Broker reads
    # ------------------------------------------------------------------

    @staticmethod
    def _read_timeout_for(user_id):
        """
        How long to wait for a book. The admin's order timeout, when longer.

        Reads and writes go to the same broker over the same connection, so the
        wait that was right for placing an order is the right floor for reading
        one back. Reading with a shorter timeout than the order that created the
        row is how a reconciler ends up silently unable to see its own orders.
        """
        try:
            from app.models import EquitySetting

            settings = EquitySetting.query.filter_by(user_id=user_id).first()
            seconds = int(getattr(settings, 'order_timeout_seconds', 0) or 0)
        except Exception:
            return BROKER_READ_TIMEOUT_SECONDS

        if seconds <= 0:
            return BROKER_READ_TIMEOUT_SECONDS
        return min(max(seconds, BROKER_READ_TIMEOUT_SECONDS),
                   MAX_BROKER_READ_TIMEOUT_SECONDS)

    def _read_books(self, account, timeout=None):
        """
        This account's order book and trade book, or None when unreadable.

        Two calls per account however many orders are outstanding. An account
        that cannot be read is skipped and retried on the next pass; it is
        never treated as "the order is gone".
        """
        try:
            api_key = account.get_api_key()
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not read the API key for account %s: %s',
                account.id, exc
            )
            return None
        if not api_key:
            return None

        try:
            client = ExtendedOpenAlgoAPI(
                api_key=api_key,
                host=account.host_url,
                timeout=timeout or BROKER_READ_TIMEOUT_SECONDS
            )
            order_response = client.orderbook()
            trade_response = client.tradebook()
        except Exception as exc:
            logger.warning(
                '[EQUITY_FILL] Broker read failed for account %s: %s',
                account.id, exc
            )
            return None

        orders = self._extract_orders(order_response)
        trades = self._extract_trades(trade_response)

        # EITHER book refusing makes this account unreadable, not half read.
        # A tradebook that refuses while the orderbook answers is the dangerous
        # shape: the order would be marked COMPLETED from the order book, its
        # fills would be looked for in an empty trade book and not found, and
        # because COMPLETED is not chaseable the split is never revisited - so
        # the fill is lost permanently while the screen reports the account as
        # verified.
        if orders is None or trades is None:
            # Said out loud rather than counted quietly: an account that cannot
            # be read looks exactly like an account with nothing outstanding,
            # and the difference matters.
            logger.warning(
                '[EQUITY_FILL] Account %s answered neither book. '
                'orderbook said %r, tradebook said %r',
                account.id,
                self._refusal(order_response),
                self._refusal(trade_response),
            )
            return None

        logger.info(
            '[EQUITY_FILL] Account %s: %d order(s), %d trade(s) read',
            account.id, len(orders or []), len(trades or [])
        )
        return (orders or [], trades or [])

    def _read_active_triggers(self, account, timeout=None):
        """
        The trigger ids still RESTING at this account's broker, or None when
        the GTT book could not be read.

        This is the only question the GTT book can answer for us, and it
        answers it in the negative. OpenAlgo's book lists ACTIVE triggers only
        and carries no released-order id, so it cannot say which order a
        trigger produced - but a trigger that is no longer in it has stopped
        resting. It fired, or was cancelled, or expired.

        None means "could not ask", which is not the same as "not there" and
        must never be read as one. An unreadable book adopts nothing.
        """
        try:
            api_key = account.get_api_key()
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not read the API key for account %s: %s',
                account.id, exc
            )
            return None
        if not api_key:
            return None

        try:
            client = ExtendedOpenAlgoAPI(
                api_key=api_key,
                host=account.host_url,
                timeout=timeout or BROKER_READ_TIMEOUT_SECONDS
            )
            request = getattr(client, '_make_request', None)
            if not callable(request):
                logger.warning(
                    '[EQUITY_FILL] Account %s: client cannot post %s',
                    account.id, GTT_BOOK_ENDPOINT
                )
                return None
            # The api key goes in the BODY. ExtendedOpenAlgoAPI._make_request
            # posts the payload verbatim and adds nothing to it, so an
            # endpoint called without the key is refused with HTTP 400 and
            # every GTT here quietly stays unmatched. Every other GTT call in
            # this application passes it the same way.
            response = request(GTT_BOOK_ENDPOINT, {'apikey': api_key})
        except Exception as exc:
            logger.warning(
                '[EQUITY_FILL] GTT book read failed for account %s: %s',
                account.id, exc
            )
            return None

        rows = self._extract_gtt_rows(response)
        if rows is None:
            # A broker with no GTT support answers 501 and a refusal envelope.
            # Said out loud, because silence here would look identical to a
            # broker holding no triggers, and the two mean opposite things.
            logger.info(
                '[EQUITY_FILL] Account %s would not serve the GTT book: %s',
                account.id, self._refusal(response)
            )
            return None

        triggers = set()
        for row in rows:
            for key in GTT_ID_KEYS:
                value = _text(row.get(key))
                if value:
                    triggers.add(value)
                    break

        logger.info(
            '[EQUITY_FILL] Account %s has %d trigger(s) still resting',
            account.id, len(triggers)
        )
        return triggers

    @staticmethod
    def _extract_gtt_rows(response):
        if not isinstance(response, dict) or response.get('status') != 'success':
            return None
        data = response.get('data')
        if isinstance(data, dict):
            rows = data.get('orders') or data.get('gtt') or data.get('triggers')
        else:
            rows = data
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None

    @staticmethod
    def _refusal(response):
        """The broker's own words when a book could not be read."""
        if not isinstance(response, dict):
            return 'no reply'
        return str(response.get('message') or response.get('status') or 'no reply')[:120]

    @staticmethod
    def _extract_orders(response):
        if not isinstance(response, dict) or response.get('status') != 'success':
            return None
        data = response.get('data')
        if isinstance(data, dict):
            rows = data.get('orders')
        else:
            rows = data
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None

    @staticmethod
    def _extract_trades(response):
        if not isinstance(response, dict) or response.get('status') != 'success':
            return None
        data = response.get('data')
        if isinstance(data, dict):
            rows = data.get('trades')
        else:
            rows = data
        return [row for row in rows if isinstance(row, dict)] if isinstance(rows, list) else None

    # ------------------------------------------------------------------
    # Adoption
    # ------------------------------------------------------------------

    def _adopt_orphans(self, splits, orders, summary):
        """
        Attach a broker order id to a split that never received one.

        Only splits that are INDETERMINATE qualify: a split that failed
        outright has no order at the broker to find, and adopting one would
        invent a trade that never happened.
        """
        orphans = [
            split for split in splits
            if not split.broker_order_id
            and split.fill_status == EQUITY_SPLIT_STATUS_INDETERMINATE
        ]
        if not orphans or not orders:
            return

        claimed = self._claimed_order_ids(splits)

        for split in orphans:
            parent = getattr(split, 'equity_order', None)
            if parent is None:
                parent = db.session.get(EquityOrder, split.equity_order_id)
            if parent is None:
                continue

            candidates = [
                entry for entry in orders
                if _text(entry.get('orderid')) not in claimed
                and _text(entry.get('symbol')).upper() == _text(parent.symbol).upper()
                and _text(entry.get('exchange')).upper() == _text(parent.exchange).upper()
                and _text(entry.get('action')).upper() == _text(parent.side).upper()
                and _to_int(entry.get('quantity')) == _to_int(split.quantity)
                and self._within_window(entry, split)
            ]

            if len(candidates) != 1:
                if candidates:
                    # Two orders identical in stock, side, quantity and time are
                    # indistinguishable. Attaching a fill to the wrong one is
                    # worse than leaving it unknown, so neither is adopted.
                    summary['adoptions_ambiguous'] += 1
                    self._stats['adoptions_ambiguous'] += 1
                    logger.warning(
                        '[EQUITY_FILL] Split %s matched %d broker orders, not adopting',
                        split.id, len(candidates)
                    )
                else:
                    # Nothing matched. Said out loud with the criteria, because
                    # a silent no-match is indistinguishable from not looking,
                    # and one wrong criterion here hides a real order for ever.
                    logger.warning(
                        '[EQUITY_FILL] Split %s found no broker order among %d: '
                        'wanted %s %s %s qty %s placed %s',
                        split.id, len(orders), parent.symbol, parent.exchange,
                        parent.side, split.quantity,
                        (split.placed_at or split.created_at)
                    )
                continue

            broker_order_id = _text(candidates[0].get('orderid'))
            split.broker_order_id = broker_order_id
            split.last_synced_at = datetime.utcnow()
            claimed.add(broker_order_id)

            summary['splits_adopted'] += 1
            self._stats['splits_adopted'] += 1
            logger.info(
                '[EQUITY_FILL] Split %s adopted broker order %s',
                split.id, broker_order_id
            )

    @staticmethod
    def _has_resting_gtt(splits):
        """Whether any of these legs is a GTT still waiting on its order."""
        return any(
            _text(split.broker_gtt_id)
            and not split.broker_order_id
            and split.fill_status in CHASEABLE_STATUSES
            for split in splits
        )

    def _adopt_fired_gtts(self, splits, orders, active_triggers, summary):
        """
        Attach the released order to a GTT that has stopped resting.

        A GTT is placed and AlgoMirror is given a TRIGGER id. When the price
        arrives the broker releases an ordinary order with a brand new ORDER
        id that AlgoMirror is never told about. Nothing in OpenAlgo's GTT book
        names it. Left alone, the split sits at PENDING for ever while the
        order it created lives a life of its own - and the Order Book shows
        the same instruction twice: once as an order placed "outside
        AlgoMirror", and once, below, as still waiting.

        Three things must all hold before anything is adopted:

        1. The GTT is NO LONGER RESTING. While a trigger is still in the
           broker's active book nothing here can fire, so an order placed by
           hand at the terminal during that time cannot be captured. An
           unreadable book adopts nothing at all.
        2. Stock, exchange, side and quantity match exactly, and the order is
           unclaimed - the same four tests the ordinary adoption uses.
        3. The order is timestamped AT OR AFTER the GTT was placed. A released
           order cannot predate the instruction that released it.

        And then, as everywhere else here, exactly one candidate must survive.
        Two orders indistinguishable in all of the above are indistinguishable
        full stop, and attaching a fill to the wrong one is worse than leaving
        it unknown.
        """
        if active_triggers is None or not orders:
            return

        orphans = [
            split for split in splits
            if _text(split.broker_gtt_id)
            and not split.broker_order_id
            and split.fill_status in CHASEABLE_STATUSES
            and _text(split.broker_gtt_id) not in active_triggers
        ]
        if not orphans:
            return

        claimed = self._claimed_order_ids(splits)

        for split in orphans:
            parent = getattr(split, 'equity_order', None)
            if parent is None:
                parent = db.session.get(EquityOrder, split.equity_order_id)
            if parent is None:
                continue

            candidates = [
                entry for entry in orders
                if _text(entry.get('orderid')) not in claimed
                and _text(entry.get('symbol')).upper() == _text(parent.symbol).upper()
                and _text(entry.get('exchange')).upper() == _text(parent.exchange).upper()
                and _text(entry.get('action')).upper() == _text(parent.side).upper()
                and _to_int(entry.get('quantity')) == _to_int(split.quantity)
                and self._released_after(entry, split)
            ]

            if len(candidates) != 1:
                if candidates:
                    summary['adoptions_ambiguous'] += 1
                    self._stats['adoptions_ambiguous'] += 1
                    logger.warning(
                        '[EQUITY_FILL] GTT %s on split %s matched %d broker '
                        'orders, not adopting',
                        split.broker_gtt_id, split.id, len(candidates)
                    )
                else:
                    # A GTT that left the book with no order behind it was
                    # cancelled or expired, not fired. Nothing to adopt, and
                    # nothing wrong - but said out loud, because a silent
                    # no-match is indistinguishable from not having looked.
                    logger.info(
                        '[EQUITY_FILL] GTT %s on split %s is no longer resting '
                        'and released no order among %d: wanted %s %s %s qty %s',
                        split.broker_gtt_id, split.id, len(orders),
                        parent.symbol, parent.exchange, parent.side, split.quantity
                    )
                continue

            broker_order_id = _text(candidates[0].get('orderid'))
            split.broker_order_id = broker_order_id
            split.last_synced_at = datetime.utcnow()
            claimed.add(broker_order_id)

            summary['splits_adopted'] += 1
            summary['gtts_fired'] = summary.get('gtts_fired', 0) + 1
            self._stats['splits_adopted'] += 1
            logger.info(
                '[EQUITY_FILL] GTT %s fired: split %s adopted broker order %s',
                split.broker_gtt_id, split.id, broker_order_id
            )

    @staticmethod
    def _released_after(entry, split):
        """
        True when a broker order is timestamped at or after the GTT was placed.

        One sided on purpose. The ordinary adoption uses a window either side
        of the placement because an order and its placement happen seconds
        apart. A GTT is the opposite: it can rest for weeks before it fires, so
        there is no upper bound to impose - but there IS a lower one, because a
        released order cannot predate the instruction that released it.

        The broker's timestamp carries no timezone. An Indian broker reports
        IST and this application stores UTC, so both readings are tried and the
        one that sits closer to the placement is the one believed - the same
        hedge the ordinary adoption makes, for the same reason.
        """
        placed = split.placed_at or split.created_at
        if placed is None:
            return True

        stamped = _parse_broker_time(entry.get('timestamp'))
        if stamped is None:
            # No usable timestamp is not evidence against a match. Every other
            # test still has to agree and exactly one candidate must survive.
            return True

        as_utc = stamped
        as_ist = stamped - IST_OFFSET
        believed = min(
            (as_utc, as_ist),
            key=lambda reading: abs((reading - placed).total_seconds())
        )
        return (believed - placed).total_seconds() >= -(GTT_FIRE_SKEW_MINUTES * 60)

    @staticmethod
    def _claimed_order_ids(splits):
        """
        Broker order ids already spoken for, so one order is never adopted twice.

        Read across the whole account, not just this pass's splits: an id
        attached to some other order weeks ago is still not available.
        """
        account_ids = {split.account_id for split in splits}
        rows = db.session.query(EquityOrderSplit.broker_order_id).filter(
            EquityOrderSplit.account_id.in_(account_ids),
            EquityOrderSplit.broker_order_id.isnot(None),
        ).all()
        return {_text(row[0]) for row in rows if _text(row[0])}

    @staticmethod
    def _within_window(entry, split):
        """
        True when a broker order sits close enough in time to the placement.

        The broker's timestamp carries no timezone. An Indian broker reports
        IST; this application stores UTC. Rather than assume one and be wrong by
        five and a half hours, both readings are tried and the closer one wins.
        Being tolerant here is safe: time is the tie-breaker, not the test. The
        stock, exchange, side and quantity must already agree, the id must be
        unclaimed, and exactly one candidate must survive - so a generous window
        cannot on its own adopt the wrong order, while a strict one silently
        rejects the right one.
        """
        placed = split.placed_at or split.created_at
        if placed is None:
            return True

        stamped = _parse_broker_time(entry.get('timestamp'))
        if stamped is None:
            # No usable timestamp is not evidence against a match. The other
            # four checks still have to agree, and the id must be unclaimed.
            return True

        limit = ADOPTION_WINDOW_MINUTES * 60
        as_utc = abs((stamped - placed).total_seconds())
        as_ist = abs(((stamped - IST_OFFSET) - placed).total_seconds())
        return min(as_utc, as_ist) <= limit

    # ------------------------------------------------------------------
    # Applying what the broker says
    # ------------------------------------------------------------------

    @staticmethod
    def _filled_from_trades(split):
        """
        How much of this split is on file as actually executed.

        Fill rows are written for every split whatever its status, so this is
        the one source that does not depend on the broker labelling an order
        COMPLETED. A recorded fill is proof that shares moved.

        Returns 0 when nothing is recorded, which is NOT proof that nothing
        filled - only that nothing has been read yet. Callers must treat those
        two cases differently.
        """
        total = db.session.query(
            db.func.sum(EquityTrade.executed_quantity)
        ).filter(EquityTrade.split_id == split.id).scalar()
        return _to_int(total)

    @classmethod
    def _resolve_filled_quantity(cls, entry, split, mapped):
        """
        Work out how many shares of this split have filled.

        Until today this was only ever computed for a COMPLETED order, and the
        order book's own `quantity` was used as the answer. That is the ORDERED
        quantity, which happens to equal the filled quantity when everything
        filled and overstates it the rest of the time - so the field could not
        be populated for any other status without being wrong.

        Two guards depend on this number, and both were dead while it stayed at
        zero: the release refuses to hand back a holding whose sell partly
        filled, and the settle shrinks a holding by what actually went. A sell
        of 100 that filled 40 and was then cancelled returned the holding to
        ACTIVE at 100, with 60 shares in existence.

        Sources, in order of authority:
          1. an explicit filled quantity from the broker,
          2. ordered minus still-pending, when the broker reports pending,
          3. everything, when the broker says the order completed,
          4. nothing, when the broker says it was rejected outright.
        Whatever that yields is then raised to the recorded fills if those are
        larger, because an execution on file cannot be un-executed by a book
        that has not caught up.

        Returns None for "cannot tell", which is different from zero and must
        stay different: zero releases a holding, None must not.
        """
        ordered = _to_int(split.quantity)
        book_ordered = _to_int(entry.get('quantity'), ordered) or ordered
        resolved = None

        for key in FILLED_QUANTITY_KEYS:
            if key in entry:
                value = _to_int(entry.get(key), -1)
                if value >= 0:
                    resolved = value
                    break

        if resolved is None:
            for key in PENDING_QUANTITY_KEYS:
                if key in entry:
                    pending = _to_int(entry.get(key), -1)
                    if pending >= 0:
                        resolved = max(book_ordered - pending, 0)
                        break

        if resolved is None:
            if mapped == EQUITY_SPLIT_STATUS_COMPLETED:
                resolved = book_ordered
            elif mapped == EQUITY_SPLIT_STATUS_REJECTED:
                # A rejected order never reached the exchange.
                resolved = 0

        from_trades = cls._filled_from_trades(split)
        if from_trades > 0:
            resolved = from_trades if resolved is None else max(resolved, from_trades)

        if resolved is None:
            return None
        # An adapter that reports more filled than was ordered is confused, and
        # the ordered quantity is the only figure that can be trusted to bound
        # it. Shrinking a holding by more than was sold is the worst outcome
        # available here.
        return min(resolved, ordered) if ordered > 0 else resolved

    def _apply_order_state(self, split, orders, summary):
        """Update one split from its entry in the broker's order book."""
        wanted = _text(split.broker_order_id)
        entry = None
        for row in orders:
            if _text(row.get('orderid')) == wanted:
                entry = row
                break
        if entry is None:
            return False

        changed = False
        raw_status = _text(entry.get('order_status')).lower()

        if raw_status and raw_status != _text(split.broker_order_status).lower():
            split.broker_order_status = raw_status[:50]
            changed = True

        mapped = BROKER_STATUS_MAP.get(raw_status)
        if mapped and mapped != split.fill_status:
            split.fill_status = mapped
            changed = True

        # Quantities are SET, never incremented: the broker's number is the
        # truth, and a pass that runs twice must not double anything.
        #
        # Written for EVERY status now, not only COMPLETED. None means the
        # broker gave nothing to go on, and the field is then left exactly as
        # it was rather than being stamped with a zero that would read as
        # "nothing filled".
        filled = self._resolve_filled_quantity(entry, split, mapped)
        if filled is not None and filled != _to_int(split.filled_quantity):
            split.filled_quantity = filled
            changed = True

        price = _to_float(entry.get('average_price')) or _to_float(entry.get('price'))
        if price > 0 and price != _to_float(split.avg_fill_price):
            split.avg_fill_price = price
            changed = True

        if changed:
            split.last_synced_at = datetime.utcnow()
            summary['splits_updated'] += 1
            self._stats['splits_updated'] += 1

        return changed

    def _settle_exit_claims(self, user_id=None):
        """
        Close every exit claim whose sell has finished filling.

        Driven from the HOLDING, not from the split, and that is the whole
        point. The reconciler chases splits that are PENDING, PARTIAL or
        INDETERMINATE, because a COMPLETED split has nothing left to chase -
        which is true of the split and false of the holding behind it. A sell
        that filled leaves a COMPLETED split and a holding still claimed, and
        a pass that only ever looks at chaseable splits can never see it. That
        is exactly how a holding sat at its pre-sale quantity with a sale that
        had visibly completed.

        Starting from the holding also means this needs no broker call and no
        readable account: it is one indexed query over a small table, and it
        repairs rows left stranded by earlier passes as readily as ones that
        filled a moment ago.

        user_id scopes it when a person pressed Refresh. The background pass
        leaves it open, the same way it discovers users to reconcile.
        """
        query = EquityHolding.query.filter(
            EquityHolding.exit_split_id.isnot(None),
            EquityHolding.exit_status.in_(EQUITY_HOLDING_STATUSES_EXIT_IN_FLIGHT),
        )
        if user_id is not None:
            query = query.filter(EquityHolding.user_id == user_id)

        closed = 0
        for holding in query.populate_existing().all():
            split = EquityOrderSplit.query.filter_by(
                id=holding.exit_split_id
            ).first()
            if split is None:
                continue
            try:
                if self._settle_exit_claim(holding, split):
                    closed += 1
                elif self._release_rejected_exit(holding, split):
                    closed += 1
            except Exception as exc:
                logger.error(
                    '[EQUITY_FILL] Could not close the exit on holding %s: %s',
                    holding.id, exc, exc_info=True
                )
                self._safe_rollback()
        return closed

    def _release_rejected_exit(self, holding, split):
        """
        Hand a holding back when the broker refused its sell and nothing filled.

        The settle above closes a claim whose sell happened. This closes the
        other end: a sell the broker took an id for and then rejected or
        cancelled, filling none of it. Left alone the row stays EXIT_SUBMITTED
        for ever - invisible to the monitor, its levels unreachable - on the
        strength of an order that will never happen.

        Only ever reached when the settle declined the row, which is what keeps
        a cancellation after a partial fill out of here: that one has shares to
        account for and belongs to the settle.

        Nothing is inferred about why. The broker's own message, where there is
        one, is put on the row so the admin reads a reason rather than a status.
        """
        if split.fill_status not in (
            EQUITY_SPLIT_STATUS_CANCELLED, EQUITY_SPLIT_STATUS_REJECTED
        ):
            return False
        if not holding.is_exit_in_flight:
            return False
        if _to_int(split.filled_quantity) > 0:
            # Shares moved. That is a settlement and the caller has already
            # tried it; releasing here would restore a quantity that was sold.
            return False

        # A release restores shares, so it needs positive evidence that none
        # went - not merely the absence of evidence that some did.
        #
        # filled_quantity is zero both when the broker said nothing filled and
        # when nobody has managed to ask, because the column defaults to zero.
        # Those two look identical here and must not be treated alike, so two
        # further things have to agree: the split has actually been read back
        # from the broker at least once, and no execution is on file for it.
        if split.last_synced_at is None:
            logger.warning(
                '[EQUITY_FILL] Holding %s stays claimed: split %s is %s but '
                'has never been read back from the broker, so a clean miss '
                'cannot be confirmed.',
                holding.id, split.id, split.fill_status
            )
            return False

        if self._filled_from_trades(split) > 0:
            # Executions on file that filled_quantity does not know about. The
            # settle owns this row; releasing it would restore sold shares.
            logger.warning(
                '[EQUITY_FILL] Holding %s not released: split %s is %s and '
                'reports nothing filled, but executions are on file for it.',
                holding.id, split.id, split.fill_status
            )
            return False

        holding_id = holding.id
        user_id = holding.user_id
        symbol = holding.symbol
        split_id = split.id
        reason = _text(split.error_message) or (
            'The broker %s this sell without filling any of it.'
            % ('cancelled' if split.fill_status == EQUITY_SPLIT_STATUS_CANCELLED
               else 'rejected')
        )

        try:
            db.session.commit()
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not save before releasing the exit on '
                'holding %s: %s', holding_id, exc
            )
            self._safe_rollback()
            return False

        released = EquityHolding.release_rejected_exit(
            holding_id, user_id, split_id, message=reason
        )

        if released:
            self._stats['exits_released'] = self._stats.get('exits_released', 0) + 1
            logger.warning(
                '[EQUITY_FILL] Exit released on holding %s (%s): split %s is %s '
                'with nothing filled. Back to ACTIVE. Reason: %s',
                holding_id, symbol, split_id, split.fill_status, reason
            )
        return bool(released)

    def _settle_exit_claim(self, holding, split):
        """
        Close one exit claim once the sell it carries has filled.

        Without this the exit path has no way out of its second-to-last state.
        A holding is claimed, the sell is submitted, the fill is booked into the
        Trade Book - and the holding stays EXIT_SUBMITTED for ever. It is then
        out of the monitor's set permanently, its quantity is never reduced, and
        the holdings sync will not correct it either, because that sync refuses
        to touch a row with an exit in flight. The row is stuck at its pre-sale
        quantity with a sale that visibly completed.

        Only ever reached through the holding's own exit_split_id, so a plain
        sell placed from Place Order cannot close somebody else's claim.

        The remaining quantity is what the holding had minus what filled, which
        is how a PARTIAL fill returns the row to ACTIVE with the smaller number
        rather than closing it out.
        """
        # CLOSED statuses only, and that is a change: PARTIAL used to be
        # accepted here. A split marked PARTIAL is a LIVE order - its remainder
        # is still resting at the broker - and settling it hands the holding
        # back to the monitor at the reduced quantity while those shares are
        # still committed to a sell. The monitor could then breach a level and
        # sell them a second time. A partial fill is settled when the order
        # that carried it finishes, as CANCELLED with shares gone, and until
        # then the holding stays claimed, which is the safe place for it.
        #
        # CANCELLED and REJECTED remain here on purpose. A sell the broker
        # refused or the admin pulled AFTER part of it filled is a settlement,
        # not a release: those shares are gone and the row has to shrink. With
        # nothing filled they fall through the quantity check below and the
        # release handles them instead.
        if split.fill_status not in CLOSED_SPLIT_STATUSES:
            return False
        if not holding.is_exit_in_flight:
            return False

        filled = _to_int(split.filled_quantity)
        if filled <= 0 and split.fill_status == EQUITY_SPLIT_STATUS_COMPLETED:
            filled = _to_int(split.quantity)
        if filled <= 0:
            return False

        holding_id = holding.id
        user_id = holding.user_id
        symbol = holding.symbol
        remaining = max(_to_int(holding.quantity) - filled, 0)

        # The claim is committed state, so this commits too. It has to happen
        # before mark_exit_completed takes its own lock, or a rollback there
        # would leave the holding claimed against a sale that is on file as
        # complete.
        try:
            db.session.commit()
        except Exception as exc:
            logger.error(
                '[EQUITY_FILL] Could not save before closing the exit on '
                'holding %s: %s', holding_id, exc
            )
            self._safe_rollback()
            return False

        closed = EquityHolding.mark_exit_completed(
            holding_id, user_id, remaining_quantity=remaining
        )

        if closed:
            self._stats['exits_closed'] = self._stats.get('exits_closed', 0) + 1
            logger.info(
                '[EQUITY_FILL] Exit closed on holding %s (%s): %s filled, %s '
                'remaining, now %s',
                holding_id, symbol, filled, remaining,
                'EXITED' if remaining <= 0 else 'ACTIVE'
            )
        return bool(closed)

    def _record_fills(self, split, trades, summary):
        """
        Write this split's fills, skipping any already recorded.

        The fingerprint is what makes a repeated pass harmless. Nothing here
        adds to a running total; each fill is either already on file or it is
        inserted once.
        """
        wanted = _text(split.broker_order_id)
        if not wanted or not trades:
            return False

        existing = {
            _text(row[0]) for row in db.session.query(EquityTrade.broker_trade_id)
            .filter(EquityTrade.split_id == split.id).all()
        }

        recorded = 0
        for entry in trades:
            if _text(entry.get('orderid')) != wanted:
                continue

            fingerprint = _fill_fingerprint(wanted, entry)
            if fingerprint in existing:
                continue

            quantity = _to_int(entry.get('quantity'))
            if quantity <= 0:
                continue

            db.session.add(EquityTrade(
                split_id=split.id,
                execution_price=_to_float(entry.get('average_price')),
                executed_quantity=quantity,
                exchange=_text(entry.get('exchange')) or None,
                executed_at=broker_time_to_utc(
                    entry.get('timestamp'), split.placed_at or split.created_at
                ) or datetime.utcnow(),
                broker_trade_id=fingerprint,
            ))
            existing.add(fingerprint)
            recorded += 1

        if recorded:
            summary['fills_recorded'] += recorded
            self._stats['fills_recorded'] += recorded
            split.last_synced_at = datetime.utcnow()

        return recorded > 0

    # ------------------------------------------------------------------
    # Selecting work
    # ------------------------------------------------------------------

    @staticmethod
    def _pending_user_ids() -> List[int]:
        """
        Owners with at least one split still worth chasing.

        The one query not filtered on a user: a background pass has no
        current_user and has to discover whose rows to read. It selects nothing
        but the owner column, and every query after it is scoped on it.
        """
        cutoff = datetime.utcnow() - timedelta(days=RECONCILE_WINDOW_DAYS)
        rows = db.session.query(EquityOrder.user_id).join(
            EquityOrderSplit, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrderSplit.fill_status.in_(CHASEABLE_STATUSES),
            EquityOrder.created_at >= cutoff,
        ).distinct().all()
        return [row[0] for row in rows if row[0] is not None]

    @staticmethod
    def _chaseable_splits(user_id, account_ids=None) -> List[EquityOrderSplit]:
        cutoff = datetime.utcnow() - timedelta(days=RECONCILE_WINDOW_DAYS)
        query = db.session.query(EquityOrderSplit).join(
            EquityOrder, EquityOrderSplit.equity_order_id == EquityOrder.id
        ).filter(
            EquityOrder.user_id == user_id,
            EquityOrderSplit.fill_status.in_(CHASEABLE_STATUSES),
            # The age cutoff exists so an order left open for ever is not
            # re-read on every pass for the rest of time. A GTT is exempt from
            # it, and has to be: resting for weeks is what a GTT is FOR, and a
            # trigger that fires on day twenty would otherwise fall outside
            # the window and never be noticed at all.
            or_(
                EquityOrder.created_at >= cutoff,
                EquityOrderSplit.broker_gtt_id.isnot(None),
            ),
        )
        if account_ids:
            query = query.filter(EquityOrderSplit.account_id.in_(list(account_ids)))
        # populate_existing() so a status another thread has just written is
        # not read back from the identity map as it was when first loaded.
        return query.populate_existing().all()

    @staticmethod
    def _safe_rollback():
        try:
            db.session.rollback()
        except Exception:
            logger.debug('[EQUITY_FILL] Rollback failed', exc_info=True)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict:
        return {
            'interval_seconds': RECONCILE_INTERVAL_SECONDS,
            'window_days': RECONCILE_WINDOW_DAYS,
            'last_run_at': self._last_run_at.isoformat() if self._last_run_at else None,
            'last_error': self._last_error,
            'stats': dict(self._stats),
        }


# Module level singleton, in the same shape as equity_exit_monitor,
# equity_alert_monitor and equity_price_feed.
equity_fill_reconciler = EquityFillReconciler()


def run_equity_fill_reconciliation():
    """One reconciliation pass. Called from the equity exit monitor's tick."""
    equity_fill_reconciler.run_checks()


# ---------------------------------------------------------------------------
# Reading the broker's books for a screen
#
# The reconciler reads these books to correct what AlgoMirror already knows.
# The Order Book and Trade Book screens need the same two calls for a different
# reason: to show what the broker has that AlgoMirror has never heard of. One
# reader serves both rather than two implementations drifting apart.
# ---------------------------------------------------------------------------

BOOK_READ_MAX_WORKERS = 4


def read_broker_books(accounts, timeout=None, max_workers=None):
    """
    Read every account's order book and trade book, in parallel.

    Returns (books, unreadable):
        books      {account_id: {'orders': [...], 'trades': [...]}}
        unreadable [account_id, ...] for accounts that did not answer

    An account that could not be read is reported, never silently treated as
    an account with nothing in it. Those two look identical in a dict and mean
    opposite things, and a screen that confuses them tells the admin their
    orders have vanished.

    API keys are decrypted on the calling thread and only plain values cross
    into the workers, which is the same rule the order engine follows.
    """
    accounts = list(accounts or [])
    if not accounts:
        return {}, []

    credentials = []
    unreadable = []
    for account in accounts:
        try:
            api_key = account.get_api_key()
        except Exception as exc:
            logger.error(
                '[EQUITY_BOOKS] Could not read the API key for account %s: %s',
                account.id, exc
            )
            api_key = None
        if not api_key:
            unreadable.append(account.id)
            continue
        credentials.append({
            'account_id': account.id,
            'api_key': api_key,
            'host_url': account.host_url,
        })

    if not credentials:
        return {}, unreadable

    app = current_app._get_current_object() if has_app_context() else None
    read_timeout = timeout or BROKER_READ_TIMEOUT_SECONDS

    def worker(credential):
        account_id = credential['account_id']

        def read_one(book_name):
            # Its own client per call. The two run at the same time, and a
            # client is cheaper than the round trip it saves.
            client = ExtendedOpenAlgoAPI(
                api_key=credential['api_key'],
                host=credential['host_url'],
                timeout=read_timeout,
            )
            return getattr(client, book_name)()

        def run():
            try:
                # The order book and the trade book are independent reads, and
                # they used to be issued one after the other - so an account
                # cost two round trips end to end, and the dashboard waited for
                # all of them. Issued together an account costs one.
                #
                # Bounded: at most two threads per account, inside a pool
                # already capped at BOOK_READ_MAX_WORKERS accounts.
                with ThreadPoolExecutor(max_workers=2) as inner:
                    order_future = inner.submit(read_one, 'orderbook')
                    trade_future = inner.submit(read_one, 'tradebook')
                    order_response = order_future.result()
                    trade_response = trade_future.result()
            except Exception as exc:
                logger.warning(
                    '[EQUITY_BOOKS] Broker read failed for account %s: %s',
                    account_id, exc
                )
                return account_id, None

            orders = EquityFillReconciler._extract_orders(order_response)
            trades = EquityFillReconciler._extract_trades(trade_response)
            # EITHER refusing is enough. Half a book is not a book: an
            # orderbook that answers while the tradebook refuses would put the
            # account's orders on screen and silently drop its fills, with the
            # screen still reporting the account as verified.
            if orders is None or trades is None:
                logger.warning(
                    '[EQUITY_BOOKS] Account %s did not answer both books. '
                    'orderbook %s, tradebook %s.',
                    account_id,
                    'refused' if orders is None else 'answered',
                    'refused' if trades is None else 'answered',
                )
                return account_id, None
            return account_id, {'orders': orders or [], 'trades': trades or []}

        if app is None:
            return run()
        with app.app_context():
            return run()

    books = {}
    workers = max_workers or min(BOOK_READ_MAX_WORKERS, len(credentials))
    try:
        with ThreadPoolExecutor(max_workers=workers) as pool:
            for account_id, result in pool.map(worker, credentials):
                if result is None:
                    unreadable.append(account_id)
                else:
                    books[account_id] = result
    except Exception as exc:
        # A pool that will not start is not a reason to fail a page. Every
        # account is reported unreadable and the screen falls back to the
        # stored record, clearly marked.
        logger.error('[EQUITY_BOOKS] Could not read the books: %s', exc)
        return {}, [credential['account_id'] for credential in credentials] + unreadable

    return books, unreadable
