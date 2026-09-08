"""
Close the GTT lifecycle: ask the broker what became of every resting trigger.

The gap this fills is the whole reason GTT "does not work". Placement already
works and already degrades correctly per broker. What never existed is the back
half: nothing called gttorderbook, and nothing bridged a fired trigger to the
order it released. A GTT was placed, the split kept broker_gtt_id, and it sat
at PENDING for ever.

Two facts make this tractable without guessing.

First, gttorderbook defaults to active triggers only, but it accepts a status
field and status="all" returns the terminal states too, normalised by the
per-broker mappers to active / transit / triggered / cancelled / expired /
rejected. So a trigger that stopped resting can be read rather than inferred
from its absence, which would not have distinguished "fired" from "cancelled".

Second, a fired GTT still gives us no order id: the broker issues a fresh one
and exposes no link back to the trigger. That last hop stays a search over the
account's order book, bounded by the trigger's own symbol, side, quantity and
fire time. It is the only heuristic here, it is clearly marked, and a failure to
match leaves the split open rather than inventing a fill.

Not every broker cooperates equally. Upstox's mapper reports every row as
active, so a terminal state may simply never appear there; a trigger that stops
being listed at all is recorded as unknown rather than assumed dead.
"""

import logging
import time
from datetime import datetime, timedelta

from flask import current_app, has_app_context

from app import db
from app.models import (
    EquityOrder,
    EquityOrderSplit,
    TradingAccount,
    EQUITY_GTT_STATUS_TRIGGERED,
    EQUITY_GTT_STATUS_UNKNOWN,
    EQUITY_GTT_STATUSES_RESTING,
    EQUITY_GTT_STATUSES_TERMINAL,
    EQUITY_GTT_TERMINAL_TO_SPLIT_STATUS,
    EQUITY_SPLIT_STATUSES_OPEN,
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# Reading a GTT book is a cheap list call, not an order write, so it sits on
# OpenAlgo's 50/sec bucket rather than the 10/sec order one. Keep the timeout
# short: a slow GTT book must never delay the next sweep.
GTT_BOOK_TIMEOUT_SECONDS = 10

# How far either side of the trigger time to look for the order a fired GTT
# released. Brokers stamp the child order with their own clock, so allow for
# skew rather than requiring the order to be strictly later.
CHILD_ORDER_WINDOW = timedelta(hours=12)

# Order-book statuses that mean the row is a real order rather than a leftover
# trigger row. "trigger pending" is deliberately included: a fired GTT can land
# there, and matching bare "pending" (which OpenAlgo never emits) was one of the
# ways this used to silently find nothing.
CHILD_ORDER_STATUSES = (
    'open', 'complete', 'completed', 'rejected', 'cancelled', 'trigger pending',
)


def default_client_factory(credential):
    """The only place this module builds a broker client. Tests replace it."""
    return ExtendedOpenAlgoAPI(
        api_key=credential.get('api_key'),
        host=credential.get('host_url'),
        timeout=credential.get('timeout') or GTT_BOOK_TIMEOUT_SECONDS,
    )


def _normalise(value):
    return str(value).strip().lower() if value else ''


def _as_text(value):
    return str(value).strip() if value not in (None, '') else ''


def _rows_from(response):
    """The GTT entries out of a gttorderbook response, or None when it failed."""
    if not isinstance(response, dict):
        return None
    if _normalise(response.get('status')) == 'error':
        return None
    data = response.get('data')
    if isinstance(data, dict):
        data = data.get('gtt') or data.get('orders') or data.get('data')
    return data if isinstance(data, list) else None


def _parse_broker_time(value):
    """Broker timestamps arrive in several shapes. None when unparseable."""
    if not value:
        return None
    text = str(value).strip()
    for fmt in (
        '%Y-%m-%d %H:%M:%S', '%Y-%m-%dT%H:%M:%S', '%Y-%m-%d %H:%M',
        '%d-%b-%Y %H:%M:%S', '%Y-%m-%dT%H:%M:%S.%f',
    ):
        try:
            return datetime.strptime(text[:len(fmt) + 6], fmt)
        except ValueError:
            continue
    return None


class EquityGttReconciler:
    """Sweeps resting GTTs and settles the ones the broker has finished with."""

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
        self._stats = {'runs': 0, 'settled': 0, 'triggered': 0, 'unresolved': 0}

    def start(self):
        self.is_running = True

    def stop(self):
        self.is_running = False

    def run_checks(self):
        """
        One sweep of every open split that carries a GTT id.

        The callable the scheduler drives, wrapped by the caller in a Flask app
        context the same way the equity exit monitor is. Never raises: a
        scheduler job that throws is one that eventually stops being scheduled.
        """
        if not self.is_running:
            return

        if not has_app_context():
            logger.error(
                '[EQUITY_GTT] run_checks called with no Flask app context. '
                'Schedule it inside "with app.app_context():".'
            )
            return

        started = time.monotonic()
        tick = {
            'accounts_read': 0,
            'books_unavailable': 0,
            'splits_examined': 0,
            'splits_settled': 0,
            'splits_triggered': 0,
            'child_orders_matched': 0,
            'child_orders_unresolved': 0,
        }

        try:
            db.session.expire_all()
            for account_id, splits in self._pending_by_account().items():
                try:
                    self._reconcile_account(account_id, splits, tick)
                except Exception as exc:
                    logger.error(
                        '[EQUITY_GTT] Account %s pass failed: %s',
                        account_id, exc, exc_info=True
                    )
                    self._safe_rollback()
            self._last_error = None
        except Exception as exc:
            self._last_error = str(exc)
            logger.error('[EQUITY_GTT] Sweep failed: %s', exc, exc_info=True)
            self._safe_rollback()
        finally:
            self._stats['runs'] += 1
            self._last_run_at = datetime.utcnow()
            self._last_run_duration_ms = round((time.monotonic() - started) * 1000.0, 2)
            self._last_tick = tick

    # ------------------------------------------------------------------ query

    @staticmethod
    def _pending_by_account():
        """
        Open splits that carry a GTT id, grouped by account.

        A split whose gtt_status is already terminal is excluded: TRIGGERED
        stays in the open set only until its child order is found, and the
        other terminal states close the split outright.
        """
        rows = (
            EquityOrderSplit.query
            .filter(
                EquityOrderSplit.broker_gtt_id.isnot(None),
                EquityOrderSplit.broker_gtt_id != '',
                EquityOrderSplit.fill_status.in_(EQUITY_SPLIT_STATUSES_OPEN),
            )
            .all()
        )
        grouped = {}
        for split in rows:
            grouped.setdefault(split.account_id, []).append(split)
        return grouped

    # ------------------------------------------------------------ reconcile

    def _reconcile_account(self, account_id, splits, tick):
        account = TradingAccount.query.get(account_id)
        if account is None or not account.is_active:
            return

        client = self.client_factory({
            'api_key': account.get_api_key(),
            'host_url': account.host_url,
        })

        response = client.gttorderbook(status='all')
        rows = _rows_from(response)
        if rows is None:
            # A broker with no gtt_api answers 501 here. That is a permanent
            # property of the broker, not of the trigger, so leave the splits
            # alone rather than settling them on a read failure.
            tick['books_unavailable'] += 1
            logger.warning(
                '[EQUITY_GTT] GTT book unavailable for account %s: %s',
                account_id,
                (response or {}).get('message') if isinstance(response, dict) else response,
            )
            return

        tick['accounts_read'] += 1
        by_trigger = {}
        for row in rows:
            trigger_id = _as_text(row.get('trigger_id'))
            if trigger_id:
                by_trigger[trigger_id] = row

        now = datetime.utcnow()
        for split in splits:
            tick['splits_examined'] += 1
            row = by_trigger.get(_as_text(split.broker_gtt_id))
            status = _normalise(row.get('status')) if row else EQUITY_GTT_STATUS_UNKNOWN
            self._apply(split, status, row, client, account, now, tick)

        db.session.commit()

    def _apply(self, split, status, row, client, account, now, tick):
        """Move one split forward on what the broker reported."""
        split.gtt_status = status
        split.gtt_synced_at = now

        if status in EQUITY_GTT_STATUSES_RESTING:
            return

        if status == EQUITY_GTT_STATUS_TRIGGERED:
            if split.gtt_triggered_at is None:
                split.gtt_triggered_at = (
                    _parse_broker_time(row.get('updated_at') if row else None) or now
                )
            tick['splits_triggered'] += 1
            self._stats['triggered'] += 1
            if not split.broker_order_id:
                matched = self._resolve_child_order(split, client, account)
                if matched:
                    split.broker_order_id = matched
                    tick['child_orders_matched'] += 1
                    logger.info(
                        '[EQUITY_GTT] Trigger %s fired, matched order %s on account %s',
                        split.broker_gtt_id, matched, account.id
                    )
                else:
                    tick['child_orders_unresolved'] += 1
                    self._stats['unresolved'] += 1
                    # Deliberately leaves the split open. A fired trigger with
                    # no matched order is a real order somewhere, so inventing a
                    # terminal state here would hide it.
                    logger.warning(
                        '[EQUITY_GTT] Trigger %s fired on account %s but no child '
                        'order matched. Split %s left open for manual review.',
                        split.broker_gtt_id, account.id, split.id
                    )
            return

        settled = EQUITY_GTT_TERMINAL_TO_SPLIT_STATUS.get(status)
        if settled:
            split.fill_status = settled
            split.error_message = f'GTT {status} at broker'
            tick['splits_settled'] += 1
            self._stats['settled'] += 1
            logger.info(
                '[EQUITY_GTT] Trigger %s %s on account %s, split %s -> %s',
                split.broker_gtt_id, status, account.id, split.id, settled
            )
            return

        # status == unknown: the trigger is not in the book at all. On a broker
        # whose mapper only ever reports active rows this is expected and says
        # nothing, so record it and keep watching rather than closing the split.
        logger.debug(
            '[EQUITY_GTT] Trigger %s not present in the book for account %s',
            split.broker_gtt_id, account.id
        )

    # --------------------------------------------------------------- matching

    def _resolve_child_order(self, split, client, account):
        """
        Find the order a fired trigger released.

        The one heuristic in this module, because no broker exposes a link from
        a trigger to the order it created. Matched on symbol, side, quantity and
        a time window around the fire time. Returns None rather than a guess
        when more than one row fits, since picking the wrong order would attach
        a fill to the wrong instruction.
        """
        parent = EquityOrder.query.get(split.equity_order_id)
        if parent is None:
            return None

        try:
            response = client.orderbook()
        except Exception as exc:
            logger.warning(
                '[EQUITY_GTT] Order book read failed for account %s: %s',
                account.id, exc
            )
            return None

        rows = response.get('data') if isinstance(response, dict) else None
        if isinstance(rows, dict):
            rows = rows.get('orders')
        if not isinstance(rows, list):
            return None

        fired_at = split.gtt_triggered_at or datetime.utcnow()
        window_start = fired_at - CHILD_ORDER_WINDOW
        window_end = fired_at + CHILD_ORDER_WINDOW

        candidates = []
        for row in rows:
            if _normalise(row.get('symbol')) != _normalise(parent.symbol):
                continue
            if _normalise(row.get('action')) != _normalise(parent.side):
                continue
            if _normalise(row.get('status')) not in CHILD_ORDER_STATUSES:
                continue
            try:
                quantity = int(float(row.get('quantity') or 0))
            except (TypeError, ValueError):
                continue
            if quantity != int(split.quantity or 0):
                continue

            stamped = _parse_broker_time(row.get('timestamp'))
            if stamped is not None and not (window_start <= stamped <= window_end):
                continue

            order_id = _as_text(row.get('orderid') or row.get('order_id'))
            if order_id:
                candidates.append(order_id)

        # Exactly one match is an answer. Several is an ambiguity, and this
        # module refuses to resolve it rather than attach the wrong fill.
        if len(candidates) == 1:
            return candidates[0]
        if len(candidates) > 1:
            logger.warning(
                '[EQUITY_GTT] %d order-book rows match trigger %s on account %s. '
                'Refusing to guess which one it released.',
                len(candidates), split.broker_gtt_id, account.id
            )
        return None

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


equity_gtt_reconciler = EquityGttReconciler()


def reconcile_account_gtts(account_id):
    """
    Read one account's GTT book once, on demand.

    No longer driven by a clock. A GTT that fires now announces itself: the
    broker releases a real order and that order arrives on the push stream,
    where equity_order_stream matches it back to the resting trigger.

    What a stream cannot tell us is a trigger that was CANCELLED or EXPIRED at
    the broker, because no order is ever created and so no event is ever sent.
    That is what this pass is for, and the stream runs it on connect and on
    every reconnect.

    Runs inside the caller's app context.
    """
    reconciler = equity_gtt_reconciler
    tick = {
        'accounts_read': 0, 'books_unavailable': 0, 'splits_examined': 0,
        'splits_settled': 0, 'splits_triggered': 0,
        'child_orders_matched': 0, 'child_orders_unresolved': 0,
    }
    splits = reconciler._pending_by_account().get(account_id) or []
    if not splits:
        return tick

    reconciler._reconcile_account(account_id, splits, tick)
    logger.info(
        '[EQUITY_GTT] Catch-up for account %s: %d trigger(s) examined, %d settled',
        account_id, tick['splits_examined'], tick['splits_settled']
    )
    return tick


def run_equity_gtt_reconciliation():
    """
    Kept for a manual sweep across every account.

    No scheduler drives this any more.
    """
    equity_gtt_reconciler.run_checks()
