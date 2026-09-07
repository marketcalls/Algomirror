"""
Background watch list price alert monitor.

Why this exists
---------------
Watch list price alerts used to be evaluated inside the request that refreshed
the Watch List screen. That made an alert conditional on somebody looking at it:
close the tab and nothing was checked, which is the opposite of what an alert is
for. This module moves the evaluation into the background service, so an alert
fires for as long as AlgoMirror itself is running.

What it does NOT do
-------------------
It never places an order and never touches a holding. A watch list alert is a
notification and nothing else. Selling is the stop loss and target monitor's job
(app.utils.equity_exit_monitor), which is a separate mechanism working on
different rows, and the two deliberately share nothing but the tick that drives
them.

How it is driven
----------------
run_equity_exit_checks() in equity_exit_monitor calls run_watchlist_alert_checks()
at the end of its tick, so this needs no scheduler registration of its own. The
exit monitor's tick is every few seconds; the pacing gate below thins that down
to one alert pass every ALERT_INTERVAL_SECONDS, which is all a price alert needs
and keeps the feed lookups cheap.

Two guards, both borrowed from the screen this replaces:

  alert_triggered_at   on the watch list row, the de-duplication guard. An alert
                       fires only while it is NULL. Setting it is what marks the
                       alert delivered, and re-arming is an explicit write.
                       Firing also switches price_alert_enabled off, so the row
                       reads as spent rather than as still live.
  alert_direction      which way the price has to cross. Resolved from the first
                       live price seen when the admin did not choose one, and
                       never fired on the pass that resolves it, because a level
                       that defines itself was never actually crossed.

Every fired alert is written to equity_alert_events, which is what lets a
browser that was not open at the time still be told, and what a later delivery
channel will read.
"""

import logging
import threading
import time
from datetime import datetime
from typing import Dict, List

from flask import current_app, has_app_context

from app import db
from app.models import (
    EQUITY_ALERT_DIRECTION_ABOVE,
    EQUITY_ALERT_DIRECTION_BELOW,
    EquityAlertEvent,
    EquityHolding,
    EquitySetting,
    EquityWatchlistItem,
)
from app.utils.equity_price_feed import equity_price_feed

logger = logging.getLogger(__name__)

# One alert pass at most this often, however fast the tick that calls us is.
ALERT_INTERVAL_SECONDS = 10

# A single pass will not write more than this many events. A price feed that
# starts answering after a long silence could otherwise cross a great many
# levels at once, and a thousand popups is not a useful alert.
MAX_EVENTS_PER_PASS = 50


def _to_float(value, default=0.0):
    try:
        result = float(value)
    except (TypeError, ValueError):
        return default
    return result if result == result else default  # NaN is never equal to itself


def _money(value):
    return round(_to_float(value), 2)


class EquityAlertMonitor:
    """
    Singleton watch list alert monitor.

    Public surface:
        run_checks()  one pass, called from the exit monitor's tick
        status()      diagnostics, safe to call at any time
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
        self._last_run_at = None
        self._last_error = None

        self._stats = {
            'passes': 0,
            'users_evaluated': 0,
            'items_evaluated': 0,
            'items_without_price': 0,
            'directions_resolved': 0,
            'alerts_fired': 0,
            'alerts_suspended': 0,
        }

    # ------------------------------------------------------------------
    # Tick
    # ------------------------------------------------------------------

    def run_checks(self):
        """
        Evaluate every armed watch list alert once.

        Never raises. A background pass that throws is a background pass that
        eventually stops being scheduled, and the alerts would go quiet without
        anything on screen saying so.
        """
        if not has_app_context():
            logger.error(
                '[EQUITY_ALERT] run_checks called with no Flask app context. '
                'Call it inside "with app.app_context():".'
            )
            return

        with self._lock:
            now = time.monotonic()
            if now - self._last_pass_monotonic < ALERT_INTERVAL_SECONDS:
                return
            self._last_pass_monotonic = now

        try:
            # Another request may have re-armed an alert or changed a price
            # since this session last looked.
            db.session.expire_all()

            user_ids = self._alertable_user_ids()
            if not user_ids:
                return

            for user_id in user_ids:
                try:
                    self._check_user(user_id)
                except Exception as exc:
                    logger.error(
                        '[EQUITY_ALERT] User %s pass failed: %s', user_id, exc,
                        exc_info=True
                    )
                    self._safe_rollback()

            self._last_error = None

        except Exception as exc:
            self._last_error = str(exc)
            logger.error('[EQUITY_ALERT] Alert pass failed: %s', exc, exc_info=True)
            self._safe_rollback()
        finally:
            self._stats['passes'] += 1
            self._last_run_at = datetime.utcnow()

    # ------------------------------------------------------------------
    # One user
    # ------------------------------------------------------------------

    def _check_user(self, user_id):
        settings = EquitySetting.get_or_create(user_id)
        if settings is not None and not settings.price_alerts_enabled:
            return

        items = self._armed_items(user_id)
        if not items:
            return

        # A stock this owner HOLDS is not watched from here.
        #
        # From 6 September a held stock leaves the watch list screen, and its
        # levels live on Holdings, where a stop loss and a target do the
        # watching. An alert left armed on a hidden row would fire from a
        # screen the owner cannot see, which is the worst thing an alert can
        # do - so the alert is CLEARED rather than merely skipped.
        #
        # Cleared here, at the moment the stock is held, rather than when it is
        # sold: it closes the window in which the alert could still fire, and
        # it is why the row comes back with an empty alert price when the last
        # share goes. That is the owner's instruction - a new level must be set
        # deliberately, not inherited from whatever was there before the buy.
        held = self._held_symbol_keys(user_id)
        if held:
            suspended = [
                item for item in items
                if (str(item.symbol or '').strip().upper(),
                    str(item.exchange or 'NSE').strip().upper()) in held
            ]
            if suspended:
                for item in suspended:
                    logger.info(
                        f'[EQUITY_ALERT] {item.symbol} {item.exchange} is held '
                        f'by user {user_id}, so its alert at {item.alert_price} '
                        'is cleared. Set a new one when it is sold.'
                    )
                    item.alert_price = None
                    item.alert_direction = None
                    item.price_alert_enabled = False
                    item.alert_triggered_at = None
                    item.alert_triggered_price = None
                self._stats['alerts_suspended'] += len(suspended)
                try:
                    db.session.commit()
                except Exception as exc:
                    logger.error(
                        f'[EQUITY_ALERT] Could not suspend held alerts for '
                        f'user {user_id}: {exc}', exc_info=True
                    )
                    self._safe_rollback()
                items = [item for item in items if item not in suspended]
        if not items:
            return

        prices = self._prices_for(items)
        self._stats['users_evaluated'] += 1

        fired = 0
        changed = False

        for item in items:
            self._stats['items_evaluated'] += 1

            key = (
                str(item.symbol or '').strip().upper(),
                str(item.exchange or 'NSE').strip().upper(),
            )
            ltp = _to_float(prices.get(key))
            if ltp <= 0:
                self._stats['items_without_price'] += 1
                continue

            alert_price = _to_float(item.alert_price)
            if alert_price <= 0:
                continue

            if not item.alert_direction:
                # The first price seen decides which side we started on. No
                # alert is raised on this pass: an alert on the tick that
                # defines the direction would report a level never crossed.
                item.alert_direction = (
                    EQUITY_ALERT_DIRECTION_ABOVE if alert_price > ltp
                    else EQUITY_ALERT_DIRECTION_BELOW
                )
                self._stats['directions_resolved'] += 1
                changed = True
                continue

            crossed = (
                ltp >= alert_price
                if item.alert_direction == EQUITY_ALERT_DIRECTION_ABOVE
                else ltp <= alert_price
            )
            if not crossed:
                continue

            item.alert_triggered_at = datetime.utcnow()
            item.alert_triggered_price = _money(ltp)
            # A fired alert is spent, so it is switched OFF as well as stamped.
            #
            # It could not fire again either way - alert_triggered_at is the
            # de-duplication guard - but leaving the switch reading ON said
            # "this alert is live" about an alert that would never speak again.
            # Off is the truth, and turning it back on is already the re-arm.
            item.price_alert_enabled = False
            db.session.add(self._build_event(item, alert_price, ltp))

            # The exit monitor logs every breach; this logged nothing at all,
            # so a fired alert could only ever be confirmed from a screen the
            # admin happened to be looking at. Both prices are recorded because
            # the gap between them is the slippage on a fast move.
            logger.info(
                '[EQUITY_ALERT] %s %s fired for user %s: level %s %s, price at '
                'firing %s',
                item.symbol, item.exchange, user_id,
                item.alert_direction, alert_price, _money(ltp)
            )

            changed = True
            fired += 1
            self._stats['alerts_fired'] += 1

            if fired >= MAX_EVENTS_PER_PASS:
                logger.warning(
                    '[EQUITY_ALERT] User %s hit the %s alert ceiling in one pass',
                    user_id, MAX_EVENTS_PER_PASS
                )
                break

        if not changed:
            return

        try:
            db.session.commit()
        except Exception as exc:
            logger.error(
                '[EQUITY_ALERT] Could not record alerts for user %s: %s',
                user_id, exc, exc_info=True
            )
            self._safe_rollback()

    @staticmethod
    def _build_event(item, alert_price, ltp):
        side = (
            'at or above'
            if item.alert_direction == EQUITY_ALERT_DIRECTION_ABOVE
            else 'at or below'
        )
        return EquityAlertEvent(
            user_id=item.user_id,
            watchlist_item_id=item.id,
            symbol=item.symbol,
            exchange=item.exchange,
            alert_price=_money(alert_price),
            alert_direction=item.alert_direction,
            ltp=_money(ltp),
            # "last" read as "the price now", which it is not: this is the
            # price at the instant the level was crossed, and the stock has
            # usually moved on by the time anyone reads the alert.
            message=(
                f'{item.symbol} traded {side} \u20b9{alert_price:,.2f}. '
                f'Price when it fired: \u20b9{ltp:,.2f}'
            ),
        )

    # ------------------------------------------------------------------
    # Rows and prices
    # ------------------------------------------------------------------

    @staticmethod
    def _alertable_user_ids() -> List[int]:
        """
        Owners with at least one alert still waiting to fire.

        This is the one query not filtered on a user: a background pass has no
        current_user and has to discover whose rows to read. It selects nothing
        but the owner column, and every query after it is scoped on the id it
        returns.
        """
        rows = db.session.query(EquityWatchlistItem.user_id).filter(
            EquityWatchlistItem.price_alert_enabled.is_(True),
            EquityWatchlistItem.alert_price.isnot(None),
            EquityWatchlistItem.alert_triggered_at.is_(None),
        ).distinct().all()

        return [row[0] for row in rows if row[0] is not None]

    @staticmethod
    def _held_symbol_keys(user_id) -> set:
        """
        Every (SYMBOL, EXCHANGE) this owner currently holds.

        Derived from the holding rows rather than recorded anywhere: shares in
        the row means held, zero means sold. Nothing to keep in step.

        A failure returns the EMPTY set, which leaves every alert armed. An
        alert that fires when it should not is a message; an alert silently
        disarmed by a failed read is a stop nobody is watching, and between the
        two the message is the safe error.
        """
        try:
            rows = db.session.query(
                EquityHolding.symbol, EquityHolding.exchange
            ).filter(
                EquityHolding.user_id == user_id,
                EquityHolding.quantity > 0,
            ).all()
        except Exception as exc:
            logger.error(
                f'[EQUITY_ALERT] Could not read holdings for user {user_id}: '
                f'{exc}', exc_info=True
            )
            return set()
        return {
            (str(symbol or '').strip().upper(),
             str(exchange or 'NSE').strip().upper())
            for symbol, exchange in rows
        }

    @staticmethod
    def _armed_items(user_id) -> List[EquityWatchlistItem]:
        # populate_existing() is not decoration: without it a row this session
        # already holds comes back from the identity map with the values it had
        # when first loaded, and an alert another thread re-armed a moment ago
        # would be invisible.
        return EquityWatchlistItem.query.filter(
            EquityWatchlistItem.user_id == user_id,
            EquityWatchlistItem.price_alert_enabled.is_(True),
            EquityWatchlistItem.alert_price.isnot(None),
            EquityWatchlistItem.alert_triggered_at.is_(None),
        ).populate_existing().all()

    @staticmethod
    def _prices_for(items) -> Dict[tuple, float]:
        """
        Live prices from the shared WebSocket feed, the same source the watch
        list screen reads. No REST poll and no broker call: a background pass
        must not add broker traffic that scales with the number of alerts.

        A symbol the feed has no fresh price for is simply absent, and that
        alert is skipped this pass. The feed drops a price past its own age
        ceiling, so absent means "not trustworthy" as well as "not known".
        """
        keys = sorted({
            (str(item.symbol or '').strip().upper(),
             str(item.exchange or 'NSE').strip().upper())
            for item in items
            if item.symbol
        })
        if not keys:
            return {}

        try:
            return equity_price_feed.prime(keys) or {}
        except Exception as exc:
            # No prices means no alerts, which is the correct failure direction:
            # a missed alert is recoverable, a false one is not.
            logger.error('[EQUITY_ALERT] Price feed unavailable: %s', exc)
            return {}

    @staticmethod
    def _safe_rollback():
        try:
            db.session.rollback()
        except Exception:
            logger.debug('[EQUITY_ALERT] Rollback failed', exc_info=True)

    # ------------------------------------------------------------------
    # Diagnostics
    # ------------------------------------------------------------------

    def status(self) -> Dict:
        return {
            'interval_seconds': ALERT_INTERVAL_SECONDS,
            'last_run_at': self._last_run_at.isoformat() if self._last_run_at else None,
            'last_error': self._last_error,
            'stats': dict(self._stats),
        }


# Module level singleton, in the same shape as equity_exit_monitor and
# equity_price_feed.
equity_alert_monitor = EquityAlertMonitor()


def run_watchlist_alert_checks():
    """One alert pass. Called from the equity exit monitor's scheduled tick."""
    equity_alert_monitor.run_checks()
