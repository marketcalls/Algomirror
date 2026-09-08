"""
Ask the broker what happened to every open equity order, and book the fills.

This closes the largest hole in the equity module, and it is our hole rather
than anything the client introduced: nothing has ever written an EquityTrade.
Trade Book is therefore structurally empty (its template says so rather than
looking broken), Holdings has no source for Avg Cost because OpenAlgo's
holdings call returns quantity and P&L but no average price, Order Status
cannot tell PRD Partial from Completed, and a split placed successfully sits at
PENDING until someone looks at the broker terminal.

Shape follows the rest of the equity module rather than the F&O poller: a plain
callable driven from the shared scheduler, not a thread of its own. The F&O
poller owns its thread and its own 1-second loop, which is right for options
where a fill decides whether a hedge is on. Equity delivery does not need that,
and a second self-managed thread is a second thing that can outlive a reload.

Two OpenAlgo details drive the code and are easy to get wrong:

  Status strings are 'open', 'complete', 'rejected', 'cancelled', 'trigger
  pending' and 'unknown'. The published table lists 'pending', which is never
  emitted. Matching it silently matches nothing, which is exactly the bug that
  makes a working order look stuck.

  Types are inconsistent across calls: orderbook quantities arrive as strings
  while tradebook and holdings quantities are numbers. Everything is coerced
  here rather than trusted.
"""

import concurrent.futures
import logging
import time
from datetime import datetime

from flask import current_app, has_app_context

from app import db
from sqlalchemy.exc import IntegrityError

from app.models import (
    EquityExternalTrade,
    EquityOrderSplit,
    EquityTrade,
    TradingAccount,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_PARTIAL,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUSES_OPEN,
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# A status read is a cheap call on OpenAlgo's 50/sec bucket, but the timeout
# stays short so one unreachable broker cannot stall the sweep.
BROKER_READ_TIMEOUT_SECONDS = 10

# Accounts are polled in parallel, orders within an account sequentially. Each
# account is a separate OpenAlgo instance in this deployment, so the per-IP
# limiter is per account and cross-account parallelism costs nothing.
MAX_POLL_WORKERS = 10

# OpenAlgo's real order status vocabulary. 'pending' is deliberately absent: it
# appears in the published table but is never emitted, and the two-word
# 'trigger pending' is what a resting or just-fired trigger actually reports.
BROKER_STATUS_COMPLETE = 'complete'
BROKER_STATUS_OPEN = 'open'
BROKER_STATUS_TRIGGER_PENDING = 'trigger pending'
BROKER_STATUS_REJECTED = 'rejected'
BROKER_STATUS_CANCELLED = 'cancelled'
BROKER_STATUS_EXPIRED = 'expired'

# How a broker status settles a split. 'open' and 'trigger pending' are absent
# because they mean "still working", which is the one answer that changes
# nothing.
BROKER_STATUS_TO_SPLIT_STATUS = {
    BROKER_STATUS_COMPLETE: EQUITY_SPLIT_STATUS_COMPLETED,
    BROKER_STATUS_REJECTED: EQUITY_SPLIT_STATUS_REJECTED,
    BROKER_STATUS_CANCELLED: EQUITY_SPLIT_STATUS_CANCELLED,
    BROKER_STATUS_EXPIRED: EQUITY_SPLIT_STATUS_CANCELLED,
}

BROKER_STATUSES_WORKING = (BROKER_STATUS_OPEN, BROKER_STATUS_TRIGGER_PENDING)


def default_client_factory(credential):
    """The only place this module builds a broker client. Tests replace it."""
    return ExtendedOpenAlgoAPI(
        api_key=credential.get('api_key'),
        host=credential.get('host_url'),
        timeout=credential.get('timeout') or BROKER_READ_TIMEOUT_SECONDS,
    )


def _normalise(value):
    return str(value).strip().lower() if value not in (None, '') else ''


def _as_text(value):
    return str(value).strip() if value not in (None, '') else ''


def _to_int(value, default=0):
    """Coerce a broker number that may arrive as a string or a float."""
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _payload(response):
    """The data object out of an OpenAlgo response, or None when it failed."""
    if not isinstance(response, dict):
        return None
    if _normalise(response.get('status')) == 'error':
        return None
    return response.get('data')


def _rows(response, *keys):
    """A list of rows out of a response whose shape varies by endpoint."""
    data = _payload(response)
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        for key in keys:
            value = data.get(key)
            if isinstance(value, list):
                return value
    return None


class EquityFillPoller:
    """Reads order status per open split and books the resulting fills."""

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
        self.client_factory = default_client_factory
        self._last_run_at = None
        self._last_run_duration_ms = None
        self._last_error = None
        self._last_tick = {}
        self._stats = {'runs': 0, 'settled': 0, 'fills_booked': 0}

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False

    def run_checks(self):
        """
        One sweep of every open split that carries a broker order id.

        Never raises: a scheduler job that throws is a scheduler job that
        eventually stops being scheduled.
        """
        if not self.is_running:
            return

        if not has_app_context():
            logger.error(
                '[EQUITY_FILL] run_checks called with no Flask app context. '
                'Schedule it inside "with app.app_context():".'
            )
            return

        app = current_app._get_current_object()
        started = time.monotonic()
        tick = {
            'accounts_polled': 0,
            'splits_examined': 0,
            'splits_settled': 0,
            'fills_booked': 0,
            'reads_failed': 0,
            'external_trades': 0,
        }

        try:
            db.session.expire_all()
            grouped = self._open_by_account()
            if not grouped:
                return

            # One task per account. Orders inside an account stay sequential so
            # a single account cannot burn its own rate limit.
            with concurrent.futures.ThreadPoolExecutor(
                max_workers=min(MAX_POLL_WORKERS, len(grouped))
            ) as executor:
                futures = [
                    executor.submit(self._poll_account, app, account_id, [s.id for s in splits], tick)
                    for account_id, splits in grouped.items()
                ]
                concurrent.futures.wait(futures, timeout=60)

            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error('[EQUITY_FILL] Sweep failed: %s', exc, exc_info=True)
            self._safe_rollback()
        finally:
            self._stats['runs'] += 1
            self._last_run_at = datetime.utcnow()
            self._last_run_duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            self._last_tick = tick

    # ------------------------------------------------------------------ query

    @staticmethod
    def _open_by_account():
        """Open splits that carry a broker order id, grouped by account."""
        rows = (
            EquityOrderSplit.query
            .filter(
                EquityOrderSplit.broker_order_id.isnot(None),
                EquityOrderSplit.broker_order_id != '',
                EquityOrderSplit.fill_status.in_(EQUITY_SPLIT_STATUSES_OPEN),
            )
            .all()
        )
        grouped = {}
        for split in rows:
            grouped.setdefault(split.account_id, []).append(split)
        return grouped

    # ------------------------------------------------------------- per account

    def _poll_account(self, app, account_id, split_ids, tick):
        """
        One account's pass, in its own app context.

        Pool threads do not inherit the parent's Flask context, and a thread
        that writes without one raises rather than silently skipping.
        """
        with app.app_context():
            try:
                account = db.session.get(TradingAccount, account_id)
                if account is None or not account.is_active:
                    return

                client = self.client_factory({
                    'api_key': account.get_api_key(),
                    'host_url': account.host_url,
                })

                # One trade book read per account, not per order: the same call
                # serves every split we are about to settle.
                trades_by_order = self._trades_by_order(client, account_id)

                # The same read also tells us what happened in this account that
                # did NOT come from here. Free, so it happens on every sweep.
                self._record_external_trades(account, trades_by_order, tick)

                changed = False
                for split_id in split_ids:
                    split = db.session.get(EquityOrderSplit, split_id)
                    if split is None or split.fill_status not in EQUITY_SPLIT_STATUSES_OPEN:
                        continue
                    tick['splits_examined'] += 1
                    if self._poll_split(client, split, trades_by_order, tick):
                        changed = True

                if changed:
                    db.session.commit()
                    self._recompute_parents(split_ids)
                tick['accounts_polled'] += 1

            except Exception as exc:
                logger.error(
                    '[EQUITY_FILL] Account %s pass failed: %s', account_id, exc,
                    exc_info=True
                )
                self._safe_rollback()

    def _trades_by_order(self, client, account_id):
        """Trade book rows grouped by the order id they belong to."""
        try:
            response = client.tradebook()
        except Exception as exc:
            logger.warning(
                '[EQUITY_FILL] Trade book read failed for account %s: %s',
                account_id, exc
            )
            return {}

        rows = _rows(response, 'trades', 'tradebook')
        if not rows:
            return {}

        grouped = {}
        for row in rows:
            order_id = _as_text(row.get('orderid') or row.get('order_id'))
            if order_id:
                grouped.setdefault(order_id, []).append(row)
        return grouped

    def _record_external_trades(self, account, trades_by_order, tick):
        """
        Note fills in this account that did not originate in AlgoMirror.

        The admin also has the broker's terminal and app. A trade placed there
        moves the same holding the stop loss monitor sizes exits against, and
        was previously absorbed in silence.

        Identification is by elimination: the trade book was already read to
        book our own fills, so any order id that matches none of our splits for
        this account came from somewhere else. That makes this free rather than
        another broker call.

        Nothing here corrects a holding or places an order. It records a notice
        and lets the admin decide, because a wrong automatic correction on a
        real position is worse than a visible unknown.
        """
        if not trades_by_order:
            return

        known = {
            _as_text(row[0])
            for row in db.session.query(EquityOrderSplit.broker_order_id)
            .filter(
                EquityOrderSplit.account_id == account.id,
                EquityOrderSplit.broker_order_id.isnot(None),
            ).all()
            if row[0]
        }

        foreign = [
            (order_id, rows)
            for order_id, rows in trades_by_order.items()
            if order_id not in known
        ]
        if not foreign:
            return

        # One query rather than one per candidate row.
        seen_ids = {
            row[0] for row in db.session.query(EquityExternalTrade.broker_trade_id)
            .filter(EquityExternalTrade.account_id == account.id).all()
            if row[0]
        }

        recorded = 0
        for order_id, rows in foreign:
            for row in rows:
                trade_id = _as_text(row.get('tradeid') or row.get('trade_id'))
                if not trade_id:
                    # With no trade id there is nothing stable to de-duplicate
                    # on, and a notice repeated every ten seconds is noise that
                    # teaches the admin to ignore all of them.
                    continue
                if trade_id in seen_ids:
                    continue

                quantity = _to_int(row.get('quantity'))
                if quantity <= 0:
                    continue

                db.session.add(EquityExternalTrade(
                    user_id=account.user_id,
                    account_id=account.id,
                    broker_trade_id=trade_id,
                    broker_order_id=order_id,
                    symbol=_as_text(row.get('symbol')).upper() or None,
                    exchange=_as_text(row.get('exchange')).upper() or None,
                    side=_as_text(row.get('action') or row.get('side')).upper() or None,
                    quantity=quantity,
                    price=_to_float(row.get('average_price') or row.get('price')),
                    executed_at=datetime.utcnow(),
                    first_seen_at=datetime.utcnow(),
                ))
                seen_ids.add(trade_id)
                recorded += 1

        if recorded:
            try:
                db.session.commit()
            except IntegrityError:
                # Another sweep won the race on the unique index. Harmless: the
                # row it wrote is the one we were about to write.
                db.session.rollback()
                return
            tick['external_trades'] = tick.get('external_trades', 0) + recorded
            logger.warning(
                '[EQUITY_FILL] %d trade(s) in account %s did not originate here. '
                'Recorded as external activity for review.',
                recorded, account.id
            )

    def _poll_split(self, client, split, trades_by_order, tick):
        """Read one split's status and settle it. True when anything changed."""
        order_id = _as_text(split.broker_order_id)
        try:
            response = client.orderstatus(order_id=order_id)
        except Exception as exc:
            tick['reads_failed'] += 1
            logger.warning(
                '[EQUITY_FILL] Status read failed for order %s: %s', order_id, exc
            )
            return False

        data = _payload(response)
        if not isinstance(data, dict):
            tick['reads_failed'] += 1
            return False

        broker_status = _normalise(data.get('order_status') or data.get('status'))
        split.broker_order_status = broker_status or None
        split.last_synced_at = datetime.utcnow()

        booked = self._book_fills(split, trades_by_order.get(order_id) or [], tick)

        if broker_status in BROKER_STATUSES_WORKING:
            # Still working. A partial fill is worth recording so Order Status
            # can show it, but the split stays open.
            if split.filled_quantity and split.filled_quantity < (split.quantity or 0):
                split.fill_status = EQUITY_SPLIT_STATUS_PARTIAL
            return True

        settled = BROKER_STATUS_TO_SPLIT_STATUS.get(broker_status)
        if settled is None:
            # 'unknown', or a status this broker invented. Leave the split open
            # rather than settle on an answer we cannot interpret.
            logger.info(
                '[EQUITY_FILL] Order %s reported uninterpretable status %r, left open',
                order_id, broker_status
            )
            return True

        if settled == EQUITY_SPLIT_STATUS_COMPLETED:
            # Trust the fills we booked over the header quantity when they
            # disagree, because the trade book is the record of what executed.
            if not split.filled_quantity:
                split.filled_quantity = _to_int(
                    data.get('filled_quantity') or data.get('quantity'), split.quantity or 0
                )
            if split.avg_fill_price is None:
                split.avg_fill_price = _to_float(
                    data.get('average_price') or data.get('averageprice')
                )
        elif settled == EQUITY_SPLIT_STATUS_REJECTED:
            split.error_message = _as_text(
                data.get('rejection_reason') or data.get('message')
            ) or 'Rejected at broker'

        split.fill_status = settled
        tick['splits_settled'] += 1
        self._stats['settled'] += 1
        return True

    def _book_fills(self, split, rows, tick):
        """
        Write an EquityTrade per broker fill, ignoring ones already booked.

        The unique index on (split_id, broker_trade_id) is what stops a repeated
        poll from doubling the recorded quantity. A broker that returns no trade
        id gets no database-level protection, so those are matched on quantity
        and price instead.
        """
        if not rows:
            return 0

        # Drop anything the stream reconstructed for this split first.
        #
        # The stream never sees a broker trade id, only a running total, so it
        # books provisional rows. The broker's rows carry real trade ids, so the
        # unique index cannot tell that the two describe the same execution, and
        # keeping both would count every filled share twice in the Trade Book,
        # its turnover and its costs. The broker's record wins: it is the
        # system of record, and this runs precisely when we have it.
        from app.utils.equity_order_stream import PROVISIONAL_TRADE_PREFIX

        provisional = EquityTrade.query.filter(
            EquityTrade.split_id == split.id,
            EquityTrade.broker_trade_id.like(PROVISIONAL_TRADE_PREFIX + '%'),
        ).all()
        for row in provisional:
            db.session.delete(row)
        if provisional:
            db.session.flush()
            logger.info(
                '[EQUITY_FILL] Replaced %d provisional fill(s) on split %s with '
                'the broker record', len(provisional), split.id
            )

        existing = {
            (t.broker_trade_id, t.executed_quantity, t.execution_price)
            for t in EquityTrade.query.filter_by(split_id=split.id).all()
        }
        existing_ids = {t[0] for t in existing if t[0]}

        booked = 0
        total_quantity = 0
        weighted = 0.0
        for row in rows:
            trade_id = _as_text(row.get('tradeid') or row.get('trade_id')) or None
            quantity = _to_int(row.get('quantity'))
            price = _to_float(row.get('average_price') or row.get('price'))
            if quantity <= 0:
                continue

            duplicate = (
                (trade_id and trade_id in existing_ids)
                or (not trade_id and (None, quantity, price) in existing)
            )
            if not duplicate:
                db.session.add(EquityTrade(
                    split_id=split.id,
                    broker_trade_id=trade_id,
                    executed_quantity=quantity,
                    execution_price=price,
                    exchange=_as_text(row.get('exchange')) or None,
                    executed_at=datetime.utcnow(),
                ))
                if trade_id:
                    existing_ids.add(trade_id)
                else:
                    existing.add((None, quantity, price))
                booked += 1

            total_quantity += quantity
            if price is not None:
                weighted += price * quantity

        if total_quantity:
            split.filled_quantity = total_quantity
            if weighted:
                split.avg_fill_price = round(weighted / total_quantity, 4)

        if booked:
            tick['fills_booked'] += booked
            self._stats['fills_booked'] += booked
        return booked

    @staticmethod
    def _recompute_parents(split_ids):
        """Roll each touched split up into its parent order's status."""
        from app.utils.equity_order_engine import recompute_parent_status

        seen = set()
        for split_id in split_ids:
            split = db.session.get(EquityOrderSplit, split_id)
            if split is None or split.equity_order_id in seen:
                continue
            seen.add(split.equity_order_id)
            order = split.equity_order
            if order is not None:
                recompute_parent_status(order.id, order.user_id, commit=True)

    # ---------------------------------------------------------------- support

    @staticmethod
    def _safe_rollback():
        try:
            db.session.rollback()
        except Exception:
            pass

    def status(self):
        return {
            'running': self.is_running,
            'last_run_at': self._last_run_at.isoformat() if self._last_run_at else None,
            'last_run_duration_ms': self._last_run_duration_ms,
            'last_error': self._last_error,
            'last_tick': dict(self._last_tick),
            'stats': dict(self._stats),
        }


equity_fill_poller = EquityFillPoller()


def reconcile_account(account_id):
    """
    One reconciliation pass for a single account, on demand.

    This is no longer driven by a clock. Order state arrives on the push stream
    (app/utils/equity_order_stream.py), and this exists only to close the gap a
    stream cannot cover: whatever changed while it was disconnected. The stream
    calls it when it connects and again on every reconnect.

    Runs inside the caller's app context.
    """
    poller = equity_fill_poller
    tick = {
        'accounts_polled': 0, 'splits_examined': 0, 'splits_settled': 0,
        'fills_booked': 0, 'reads_failed': 0, 'external_trades': 0,
    }
    from flask import current_app
    app = current_app._get_current_object()

    splits = [
        split for split in poller._open_by_account().get(account_id, [])
    ]
    if not splits:
        return tick

    poller._poll_account(app, account_id, [s.id for s in splits], tick)
    logger.info(
        '[EQUITY_FILL] Catch-up for account %s: %d split(s) examined, %d settled',
        account_id, tick['splits_examined'], tick['splits_settled']
    )
    return tick


def run_equity_fill_poll():
    """
    Kept for a manual sweep across every account.

    No scheduler drives this any more. It is here for an operator or a route
    that wants to force a full reconciliation.
    """
    equity_fill_poller.run_checks()
