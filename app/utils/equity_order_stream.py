"""
Event driven order updates for the Equity module.

AlgoMirror's architecture is push based wherever a push exists. Prices arrive on
the shared WebSocket rather than being fetched, and the equity screens read a
local cache. Order state was the exception: it was reconciled on a ten second
timer, which is the shape the client's module used throughout and the shape this
module exists to remove.

OpenAlgo pushes order updates. `subscribe_orders` is an account level stream
carrying every asynchronous status change the broker reports after an order is
placed: fills, partial fills, rejections, cancellations. Seventeen brokers ship
an order adapter, including all five this deployment uses, so there is no reason
to ask repeatedly for something the broker will tell us.

What is per account and what is shared
--------------------------------------
Market data is broker agnostic, so one shared connection serves every symbol for
every account. Order updates are not: the stream is authenticated with one
account's API key and carries only that account's orders. So this module holds
one connection per active account, which is the only place in the equity module
that does.

Threading
---------
The SDK invokes the callback on its WebSocket reader thread. Doing database work
there would block the reader and, worse, an exception would propagate into it.
So the callback does one thing: put the event on a queue. A single worker thread
BLOCKS on that queue and does the work inside a Flask app context.

Blocking on a queue is the point. There is no interval anywhere in this module:
the worker sleeps until an event arrives, and a day with no orders costs nothing.

Catch up without polling
------------------------
A stream can only report what happens while it is connected. Anything that
changed during a restart or a dropped connection would be missed for ever, so
one reconciliation pass runs when a stream connects and again whenever it
reconnects. That is still event driven: the trigger is the connect event, not a
clock. The SDK reconnects on its own with backoff and replays subscriptions, so
this module does not implement retry logic of its own.

What an unknown order id means
------------------------------
An update for an order id we have no split for is one of exactly two things, and
they are checked in that order:

  a GTT that just fired. The broker issues a fresh order id for the released
  order and exposes no link back to the trigger, so a resting GTT for the same
  symbol, side and quantity is the match.

  activity that did not originate here: the broker's own terminal or app. That
  is recorded as a notice, never acted on.
"""

import logging
import queue
import threading
from datetime import datetime

from app import db
from app.models import (
    EquityExternalTrade,
    EquityOrderSplit,
    EquityTrade,
    TradingAccount,
    EQUITY_GTT_STATUS_TRIGGERED,
    EQUITY_GTT_STATUSES_RESTING,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_PARTIAL,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUSES_OPEN,
)
from app.utils.equity_events import (
    bump, TOPIC_EXTERNAL, TOPIC_HOLDINGS, TOPIC_ORDERS,
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

# How long the worker waits on an empty queue before looping to re-check the
# stop flag. Not a poll interval: no work happens on a timeout, it exists purely
# so stop() is responsive rather than leaving a thread parked for ever.
QUEUE_WAIT_SECONDS = 1.0

# A hard ceiling so a broker that floods the stream cannot exhaust memory. The
# oldest event is dropped with a loud log rather than the newest, because the
# newest carries the most recent state of an order.
MAX_QUEUE_DEPTH = 5000

# Marks a trade row the stream reconstructed rather than one the broker
# reported. The stream never sees a broker trade id, only a running total, so
# its rows are provisional and the reconciler replaces them with the real ones.
# Shared with equity_fill_poller, which does the replacing.
PROVISIONAL_TRADE_PREFIX = 'stream:'

# Broker order statuses, as OpenAlgo normalises them. 'pending' is deliberately
# absent: it appears in the published table but is never emitted, and the
# two-word 'trigger pending' is what a resting or just-fired trigger reports.
STATUS_COMPLETE = 'complete'
STATUS_OPEN = 'open'
STATUS_TRIGGER_PENDING = 'trigger pending'
STATUS_REJECTED = 'rejected'
STATUS_CANCELLED = 'cancelled'
STATUS_EXPIRED = 'expired'

STATUSES_WORKING = (STATUS_OPEN, STATUS_TRIGGER_PENDING)

STATUS_TO_SPLIT_STATUS = {
    STATUS_COMPLETE: EQUITY_SPLIT_STATUS_COMPLETED,
    STATUS_REJECTED: EQUITY_SPLIT_STATUS_REJECTED,
    STATUS_CANCELLED: EQUITY_SPLIT_STATUS_CANCELLED,
    STATUS_EXPIRED: EQUITY_SPLIT_STATUS_CANCELLED,
}


def _normalise(value):
    return str(value).strip().lower() if value not in (None, '') else ''


def _as_text(value):
    return str(value).strip() if value not in (None, '') else ''


def _to_int(value, default=0):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def default_client_factory(credential):
    """The only place this module builds a broker client. Tests replace it."""
    return ExtendedOpenAlgoAPI(
        api_key=credential.get('api_key'),
        host=credential.get('host_url'),
        ws_url=credential.get('websocket_url'),
    )


class EquityOrderStream:
    """One order-update connection per active account, drained by one worker."""

    _instance = None
    _singleton_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._singleton_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return
        self._initialized = True

        self._app = None
        self._lock = threading.Lock()
        self._clients = {}            # account_id -> client
        self._queue = queue.Queue(maxsize=MAX_QUEUE_DEPTH)
        self._worker = None
        self._running = False

        self._stats = {
            'events': 0,
            'splits_updated': 0,
            'fills_booked': 0,
            'gtt_resolved': 0,
            'external_recorded': 0,
            'unmatched': 0,
            'dropped': 0,
        }
        self._last_event_at = None
        self._last_error = None
        self.client_factory = default_client_factory
        # Set by the app factory so a catch-up can run without a request.
        self.reconciler = None

    # ------------------------------------------------------------- lifecycle

    def start(self, app):
        """Open a stream per active account and start the drain worker."""
        self._app = app
        with self._lock:
            if self._running:
                return
            self._running = True
            self._worker = threading.Thread(
                target=self._drain, name='equity-order-stream', daemon=True
            )
            self._worker.start()

        with app.app_context():
            accounts = TradingAccount.query.filter_by(is_active=True).all()
            for account in accounts:
                self._open(account)

    def stop(self):
        """Close every stream. Safe to call twice."""
        with self._lock:
            self._running = False
            clients = list(self._clients.items())
            self._clients.clear()

        for account_id, client in clients:
            try:
                client.unsubscribe_orders()
            except Exception:
                pass
            try:
                client.disconnect()
            except Exception:
                pass
            logger.debug('[EQUITY_STREAM] Closed order stream for account %s', account_id)

    def _open(self, account):
        """
        Connect and subscribe for one account.

        Never raises. An account whose stream cannot be opened is logged and
        skipped: it costs that account live order updates, not the module.
        """
        try:
            client = self.client_factory({
                'api_key': account.get_api_key(),
                'host_url': account.host_url,
                'websocket_url': account.websocket_url,
            })

            if not client.connect():
                logger.warning(
                    '[EQUITY_STREAM] Could not authenticate the order stream for '
                    'account %s. Orders on it will not update until it connects.',
                    account.id
                )
                return False

            account_id = account.id
            ok = client.subscribe_orders(
                on_order_update=lambda message, _id=account_id: self._on_event(_id, message)
            )
            if not ok:
                logger.warning(
                    '[EQUITY_STREAM] Order subscription refused for account %s',
                    account_id
                )
                try:
                    client.disconnect()
                except Exception:
                    pass
                return False

            with self._lock:
                self._clients[account_id] = client

            logger.info('[EQUITY_STREAM] Order stream live for account %s', account_id)

            # A stream reports only what happens while it is connected, so
            # anything that changed during a restart would be missed. One
            # reconciliation pass closes that gap. Triggered by connecting, not
            # by a clock.
            self._catch_up(account_id)
            return True

        except Exception as exc:
            logger.error(
                '[EQUITY_STREAM] Failed to open the order stream for account %s: %s',
                account.id, exc, exc_info=True
            )
            return False

    def _catch_up(self, account_id):
        """One reconciliation pass for whatever the stream could not have seen."""
        if self.reconciler is None:
            return
        try:
            self.reconciler(account_id)
        except Exception as exc:
            logger.error(
                '[EQUITY_STREAM] Catch-up failed for account %s: %s',
                account_id, exc, exc_info=True
            )

    # ----------------------------------------------------------------- intake

    def _on_event(self, account_id, message):
        """
        The SDK callback. Runs on the WebSocket reader thread.

        Does exactly one thing: hand the event to the worker. Database work here
        would block the reader, and an exception would propagate into it and take
        the stream down.
        """
        try:
            self._queue.put_nowait((account_id, message))
        except queue.Full:
            # Drop the oldest rather than the newest: the newest carries the
            # most recent state of an order, which is the one that matters.
            self._stats['dropped'] += 1
            try:
                self._queue.get_nowait()
                self._queue.put_nowait((account_id, message))
            except Exception:
                pass
            logger.error(
                '[EQUITY_STREAM] Order event queue is full. Dropped an event on '
                'account %s. Reconcile before trusting order state.', account_id
            )
        except Exception as exc:
            try:
                logger.debug('[EQUITY_STREAM] Ignored bad order event: %s', exc)
            except Exception:
                pass

    def _drain(self):
        """
        The worker. Blocks on the queue: there is no interval here.

        The timeout exists only so a stop() is noticed; nothing happens when it
        expires.
        """
        while True:
            with self._lock:
                if not self._running:
                    return
            try:
                item = self._queue.get(timeout=QUEUE_WAIT_SECONDS)
            except queue.Empty:
                continue

            account_id, message = item
            try:
                with self._app.app_context():
                    topics = self._apply(account_id, message)
                    db.session.commit()
                # Signalled after the commit, so a woken SSE reader sees the
                # state the event describes rather than the state before it.
                for topic in (topics or ()):
                    bump(topic)
            except Exception as exc:
                self._last_error = str(exc)
                logger.error(
                    '[EQUITY_STREAM] Failed to apply an order event on account %s: %s',
                    account_id, exc, exc_info=True
                )
                try:
                    with self._app.app_context():
                        db.session.rollback()
                except Exception:
                    pass
            finally:
                self._queue.task_done()

    # ----------------------------------------------------------------- apply

    def _apply(self, account_id, message):
        """
        Move one order event into the database.

        Returns the change topics it touched, so the caller can wake the SSE
        readers that care about them after the commit.
        """
        if not isinstance(message, dict):
            return ()

        self._stats['events'] += 1
        self._last_event_at = datetime.utcnow()

        order_id = _as_text(message.get('orderid') or message.get('order_id'))
        if not order_id:
            return ()

        split = EquityOrderSplit.query.filter_by(
            account_id=account_id, broker_order_id=order_id
        ).first()

        if split is None:
            # Either a GTT we placed has just fired under a new order id, or
            # this did not originate here at all.
            split = self._match_resting_gtt(account_id, order_id, message)

        if split is None:
            self._record_external(account_id, order_id, message)
            return (TOPIC_EXTERNAL,)

        self._update_split(split, message)
        # A settled order changes what Holdings shows, not just the books.
        return (TOPIC_ORDERS, TOPIC_HOLDINGS)

    def _match_resting_gtt(self, account_id, order_id, message):
        """
        Claim this order for a resting GTT, if one fits.

        A fired trigger releases a real order under a fresh id and no broker
        links the two, so symbol, side and quantity are the match. Two equally
        good candidates produce no answer: attaching an order to the wrong
        trigger is worse than leaving it unattached.
        """
        symbol = _normalise(message.get('symbol'))
        action = _normalise(message.get('action'))
        quantity = _to_int(message.get('quantity'))
        if not symbol or not action:
            return None

        candidates = [
            split for split in EquityOrderSplit.query.filter(
                EquityOrderSplit.account_id == account_id,
                EquityOrderSplit.broker_gtt_id.isnot(None),
                EquityOrderSplit.broker_order_id.is_(None),
                EquityOrderSplit.fill_status.in_(EQUITY_SPLIT_STATUSES_OPEN),
            ).all()
            if (split.gtt_status is None or split.gtt_status in EQUITY_GTT_STATUSES_RESTING)
            and _normalise(split.equity_order.symbol) == symbol
            and _normalise(split.equity_order.side) == action
            and (quantity <= 0 or _to_int(split.quantity) == quantity)
        ]

        if len(candidates) != 1:
            if len(candidates) > 1:
                logger.warning(
                    '[EQUITY_STREAM] Order %s on account %s matches %d resting '
                    'GTTs. Refusing to guess which one fired.',
                    order_id, account_id, len(candidates)
                )
            return None

        split = candidates[0]
        split.broker_order_id = order_id
        split.gtt_status = EQUITY_GTT_STATUS_TRIGGERED
        split.gtt_triggered_at = datetime.utcnow()
        split.gtt_synced_at = datetime.utcnow()
        self._stats['gtt_resolved'] += 1
        logger.info(
            '[EQUITY_STREAM] GTT %s fired on account %s and released order %s',
            split.broker_gtt_id, account_id, order_id
        )
        return split

    def _update_split(self, split, message):
        """Apply a pushed status to one split, booking any fill it reports."""
        status = _normalise(message.get('order_status') or message.get('status'))
        split.broker_order_status = status or None
        split.last_synced_at = datetime.utcnow()

        filled = _to_int(message.get('filled_quantity'))
        price = _to_float(message.get('average_price'))
        if filled > 0:
            self._book_fill(split, filled, price, message)

        if status in STATUSES_WORKING:
            if filled > 0 and filled < _to_int(split.quantity):
                split.fill_status = EQUITY_SPLIT_STATUS_PARTIAL
            self._stats['splits_updated'] += 1
            return

        settled = STATUS_TO_SPLIT_STATUS.get(status)
        if settled is None:
            # A status this broker invented, or 'unknown'. Leave the split open
            # rather than settle it on something we cannot interpret.
            logger.info(
                '[EQUITY_STREAM] Order %s reported uninterpretable status %r, left open',
                split.broker_order_id, status
            )
            return

        if settled == EQUITY_SPLIT_STATUS_COMPLETED:
            if not split.filled_quantity:
                split.filled_quantity = filled or _to_int(split.quantity)
            if split.avg_fill_price is None and price is not None:
                split.avg_fill_price = price
        elif settled == EQUITY_SPLIT_STATUS_REJECTED:
            split.error_message = _as_text(
                message.get('rejection_reason') or message.get('message')
            ) or 'Rejected at broker'

        split.fill_status = settled
        self._stats['splits_updated'] += 1
        self._recompute_parent(split)

    def _book_fill(self, split, filled, price, message):
        """
        Record the fill this event reports, without double counting.

        The stream sends a cumulative filled_quantity on every update, not a
        delta, so the same fill arrives again on the next event. Only the
        increase is booked.
        """
        already = _to_int(split.filled_quantity)
        if filled <= already:
            split.filled_quantity = max(already, filled)
            if price is not None:
                split.avg_fill_price = price
            return

        delta = filled - already
        # PROVISIONAL. The stream reports a cumulative quantity and never a
        # broker trade id, so this row is our own reconstruction of the fill,
        # not the broker's record of it.
        #
        # The id is synthetic but deterministic, which does two things. The
        # unique index on (split_id, broker_trade_id) de-duplicates a repeated
        # event, where a NULL would not: both databases allow repeated NULLs in
        # a unique index, so NULL rows accumulate silently. And the prefix makes
        # these rows identifiable, so the reconciler can replace them with the
        # broker's authoritative rows instead of booking the same execution
        # twice, once with a trade id and once without.
        db.session.add(EquityTrade(
            split_id=split.id,
            broker_trade_id='%s%s:%d' % (PROVISIONAL_TRADE_PREFIX,
                                         _as_text(split.broker_order_id) or split.id,
                                         filled),
            executed_quantity=delta,
            execution_price=price,
            exchange=_as_text(message.get('exchange')) or None,
            executed_at=datetime.utcnow(),
        ))
        split.filled_quantity = filled
        if price is not None:
            split.avg_fill_price = price
        self._stats['fills_booked'] += 1

    def _record_external(self, account_id, order_id, message):
        """Note an order in this account that did not originate here."""
        self._stats['unmatched'] += 1

        existing = EquityExternalTrade.query.filter_by(
            account_id=account_id, broker_trade_id=order_id
        ).first()
        if existing is not None:
            return

        account = db.session.get(TradingAccount, account_id)
        if account is None:
            return

        db.session.add(EquityExternalTrade(
            user_id=account.user_id,
            account_id=account_id,
            broker_trade_id=order_id,
            broker_order_id=order_id,
            symbol=_as_text(message.get('symbol')).upper() or None,
            exchange=_as_text(message.get('exchange')).upper() or None,
            side=_as_text(message.get('action')).upper() or None,
            quantity=_to_int(message.get('quantity')),
            price=_to_float(message.get('average_price')),
            executed_at=datetime.utcnow(),
            first_seen_at=datetime.utcnow(),
        ))
        self._stats['external_recorded'] += 1
        logger.warning(
            '[EQUITY_STREAM] Order %s on account %s did not originate here. '
            'Recorded as external activity for review.', order_id, account_id
        )

    @staticmethod
    def _recompute_parent(split):
        from app.utils.equity_order_engine import recompute_parent_status
        order = split.equity_order
        if order is not None:
            recompute_parent_status(order.id, order.user_id, commit=False)

    # ---------------------------------------------------------------- status

    def status(self):
        with self._lock:
            accounts = sorted(self._clients)
        return {
            'running': self._running,
            'accounts_streaming': accounts,
            'account_count': len(accounts),
            'queue_depth': self._queue.qsize(),
            'last_event_at': self._last_event_at.isoformat() if self._last_event_at else None,
            'last_error': self._last_error,
            'stats': dict(self._stats),
        }


equity_order_stream = EquityOrderStream()
