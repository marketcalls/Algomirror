"""
Equity Price Feed

Event driven last traded prices for the Equity module, backed by the single
shared OpenAlgo WebSocket manager that already serves the F&O screens.

Why this exists: the Equity screens used to pay for a broker REST round trip
(multiquotes) on every poll, behind the account fan-out barrier. OpenAlgo pushes
LTP over the WebSocket, so prices can be read from a local cache with zero REST
calls. Funds and holdings are not pushed by OpenAlgo and stay on REST, they are
simply not this module's problem.

Design rules, in the same spirit as app/utils/position_monitor.py:
- Module level singleton, equity_price_feed.
- Defensive everywhere. The shared manager may be missing, may not be
  authenticated yet, and may be replaced after a reconnect. A caller must never
  block or see an exception because the feed is not up.
- No database write and no broker REST call on any path, least of all on the
  tick callback path.
- The tick callback runs on the WebSocket reader thread while requests read from
  Flask worker threads, so shared state is guarded by a lock and every critical
  section is kept short.

This module deliberately imports nothing from app.equity, to avoid a circular
import. The shared manager is imported lazily inside a helper for the same
reason.
"""

import logging
import threading
import weakref
from datetime import datetime, timedelta, timezone
from typing import Dict, List, Optional, Set, Tuple

logger = logging.getLogger(__name__)

# A tracked instrument, always upper case: ('RELIANCE', 'NSE').
SymbolKey = Tuple[str, str]

# Exchange assumed when a caller or a tick omits it. Equity holdings are NSE or
# BSE, and NSE is the common case.
DEFAULT_EXCHANGE = 'NSE'

# Subscription mode. Quote rather than LTP.
#
# LTP is the cheaper push mode, but it carries only the traded price, so
# previous close had to be fetched over REST once per symbol per day and
# retried on a zero answer. Quote pushes open, high, low, close, volume and the
# timestamp alongside the LTP, and its `close` IS the previous close. That
# removes a recurring REST call from the hot path and means the day change on
# every equity screen comes from the same tick as the price it is compared
# against, rather than from a separate call that could be a day stale.
#
# The extra bandwidth is per tick, not per symbol subscribed, and the equity
# feed is capped at MAX_TRACKED_SYMBOLS well below the 1000 the proxy allows.
SUBSCRIPTION_MODE = 'quote'

# Hard ceiling on how many symbols this feed will ever hold subscribed, so the
# set cannot grow without bound as holdings change across accounts.
MAX_TRACKED_SYMBOLS = 500

# Upper bound on one subscribe call. Anything above this stays pending and goes
# out on the next ensure_subscribed.
MAX_SUBSCRIBE_BATCH = 100

# How long a pushed price stays trustworthy. Past this the symbol is
# reported as having no price, so the caller's bounded REST backstop
# refreshes it. Set above the 30 second screen poll so a healthy feed is
# never second-guessed, and low enough that a silently dead subscription
# cannot leave a frozen number looking live.
MAX_PRICE_AGE_SECONDS = 90.0


def _to_price(value) -> float:
    """Coerce a pushed or cached price to a float. Returns 0.0 when unusable."""
    if value is None or value == '':
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if number != number:  # NaN
        return 0.0
    if number in (float('inf'), float('-inf')):
        return 0.0
    return number


def _normalise_key(item) -> Optional[SymbolKey]:
    """
    Coerce one caller supplied instrument to ('SYMBOL', 'EXCHANGE').

    Accepts a (symbol, exchange) tuple or list, a bare symbol string, or a dict
    carrying 'symbol' and 'exchange'. Both parts are upper cased, because a
    lookup miss caused by casing would silently send the caller back to a slow
    REST fallback. Returns None when there is no usable symbol.
    """
    symbol = ''
    exchange = ''

    if isinstance(item, dict):
        symbol = item.get('symbol') or ''
        exchange = item.get('exchange') or ''
    elif isinstance(item, (tuple, list)):
        if len(item) >= 1:
            symbol = item[0] or ''
        if len(item) >= 2:
            exchange = item[1] or ''
    elif isinstance(item, str):
        symbol = item

    symbol = str(symbol).strip().upper()
    if not symbol:
        return None

    exchange = str(exchange).strip().upper() or DEFAULT_EXCHANGE
    return (symbol, exchange)


def _normalise_keys(symbol_keys) -> List[SymbolKey]:
    """
    Coerce an iterable of instruments to a de-duplicated list of SymbolKey,
    preserving the caller's order. Unusable entries are dropped, never raised.
    """
    keys: List[SymbolKey] = []
    if not symbol_keys:
        return keys

    seen: Set[SymbolKey] = set()
    try:
        for item in symbol_keys:
            key = _normalise_key(item)
            if key is None or key in seen:
                continue
            seen.add(key)
            keys.append(key)
    except TypeError:
        # Not iterable. Treat it as a single instrument if it looks like one.
        key = _normalise_key(symbol_keys)
        if key is not None:
            keys.append(key)

    return keys


def _get_manager():
    """
    Return the shared ProfessionalWebSocketManager, or None.

    Imported lazily: app.utils.background_service pulls in a large part of the
    service layer, and importing it at module load would risk an import cycle.
    """
    try:
        from app.utils.background_service import option_chain_service
        return option_chain_service.shared_websocket_manager
    except Exception as exc:
        logger.debug(f"[EQUITY_FEED] Shared WebSocket manager unavailable: {exc}")
        return None


def _is_ready(manager) -> bool:
    """True when the manager exists and has authenticated."""
    if manager is None:
        return False
    try:
        return bool(getattr(manager, 'authenticated', False))
    except Exception:
        return False


class EquityPriceFeed:
    """
    Singleton WebSocket backed price cache for the Equity module.

    Lifecycle: symbols enter through ensure_subscribed, are pushed as LTP ticks
    by the shared manager, are read back by get_prices, and leave through
    release or release_all when they are no longer held.
    """

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

        # Guards every field below except the handler registration flag.
        self._lock = threading.Lock()

        # Symbols confirmed subscribed on the current manager.
        self._subscribed: Set[SymbolKey] = set()
        # Symbols waiting for the feed to come up, or for a failed subscribe to
        # be retried on the next ensure_subscribed call.
        self._pending: Set[SymbolKey] = set()
        # Symbols currently inside a subscribe call, so a concurrent request
        # thread does not subscribe them a second time.
        self._inflight: Set[SymbolKey] = set()

        # symbol -> set of subscribed exchanges. Lets the tick callback reject a
        # symbol we do not track in O(1), which matters because every LTP tick
        # in the process passes through the handler.
        self._symbol_index: Dict[str, Set[str]] = {}

        # Local price cache for tracked symbols, filled by pushed ticks.
        self._prices: Dict[SymbolKey, float] = {}
        # Tick time per symbol, so an aged price can be told from a live one.
        self._price_times: Dict[SymbolKey, datetime] = {}
        # Previous close per symbol, pushed on every Quote tick. Kept apart
        # from _prices so a previous close can never be served as a live price.
        self._prev_closes: Dict[SymbolKey, float] = {}

        # Diagnostics.
        self._tick_count = 0
        self._last_tick_at: Optional[datetime] = None
        self._last_feed_tick_at: Optional[datetime] = None

        # Identity of the manager we last synchronised with, so a replaced
        # manager can be detected and every symbol re-subscribed on it.
        self._manager_ref = None

        # Handler registration is guarded separately and is never held together
        # with self._lock.
        self._handler_lock = threading.Lock()
        self._handler_registered = False

        logger.debug("[EQUITY_FEED] Initialised")

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def ensure_subscribed(self, symbol_keys) -> Dict[str, object]:
        """
        Subscribe every instrument that is not already subscribed, in one batch,
        in LTP mode. Idempotent and cheap to call on every request.

        If the shared manager is missing or not authenticated, the symbols are
        recorded as pending and the call returns quietly. Pending symbols are
        retried on the next call, once the manager is ready.

        Args:
            symbol_keys: iterable of (symbol, exchange) tuples. Dicts with
                'symbol' and 'exchange', and bare symbol strings, are accepted.

        Returns:
            dict: {'ready': bool, 'added': int, 'subscribed': int,
                   'pending': int, 'dropped': int}
            'added' counts the symbols this call subscribed, 'dropped' counts
            the symbols refused because MAX_TRACKED_SYMBOLS was reached.
        """
        keys = _normalise_keys(symbol_keys)
        manager = _get_manager()
        self._sync_manager(manager)

        if not _is_ready(manager):
            with self._lock:
                dropped = self._absorb_locked(keys)
                result = self._counts_locked()
            result['ready'] = False
            result['added'] = 0
            result['dropped'] = dropped
            return result

        self._ensure_handler(manager)

        with self._lock:
            dropped = self._absorb_locked(keys)
            batch = self._take_pending_locked()

        if not batch:
            with self._lock:
                result = self._counts_locked()
            result['ready'] = True
            result['added'] = 0
            result['dropped'] = dropped
            return result

        instruments = [{'symbol': symbol, 'exchange': exchange} for symbol, exchange in batch]
        subscribed_ok = False
        try:
            subscribed_ok = bool(manager.subscribe_batch(instruments, mode=SUBSCRIPTION_MODE))
        except Exception as exc:
            logger.error(f"[EQUITY_FEED] Subscribe failed for {len(batch)} symbols: {exc}")
            subscribed_ok = False

        with self._lock:
            for key in batch:
                self._inflight.discard(key)
            if subscribed_ok:
                for key in batch:
                    self._add_subscribed_locked(key)
            else:
                self._pending.update(batch)
            result = self._counts_locked()

        result['ready'] = True
        result['added'] = len(batch) if subscribed_ok else 0
        result['dropped'] = dropped

        if subscribed_ok:
            logger.debug(f"[EQUITY_FEED] Subscribed {len(batch)} symbols in {SUBSCRIPTION_MODE} mode")

        return result

    def get_prices(self, symbol_keys) -> Dict[SymbolKey, float]:
        """
        Return the prices the feed currently knows, read from the shared
        manager's LTP cache with the locally pushed cache as a backstop.

        Never calls the broker. A symbol with no known price is simply absent
        from the mapping, so the caller decides how to fall back.

        Args:
            symbol_keys: iterable of (symbol, exchange) tuples.

        Returns:
            dict: {(SYMBOL, EXCHANGE): price} with price a float greater than
            zero. Keys are upper cased, and only requested symbols appear.
        """
        keys = _normalise_keys(symbol_keys)
        if not keys:
            return {}

        manager = _get_manager()
        cached = self._manager_ltp(manager)
        upper_index: Optional[Dict[str, object]] = None

        # Timezone aware to match how _price_times is written in _on_tick.
        # Comparing a naive datetime against an aware one raises TypeError.
        cutoff = datetime.now(timezone.utc) - timedelta(seconds=MAX_PRICE_AGE_SECONDS)
        with self._lock:
            local = {}
            for key in keys:
                seen = self._price_times.get(key)
                if seen is not None and seen < cutoff:
                    # Too old to trust. Leave it out so the caller's REST
                    # backstop picks the symbol up as missing.
                    local[key] = 0.0
                    continue
                local[key] = self._prices.get(key, 0.0)

        prices: Dict[SymbolKey, float] = {}
        for key in keys:
            symbol, exchange = key

            # The age gate applies to EVERY source, not only the local cache.
            # ProfessionalWebSocketManager.get_ltp serves its last valid value
            # indefinitely by design (zero-value protection), so consulting it
            # without an age check would reintroduce the exact failure the tick
            # times exist to prevent: a dead subscription showing a frozen price
            # as a live one. A symbol is served only when THIS feed has seen a
            # recent tick for it; anything else is reported absent so the
            # caller's bounded REST backstop refreshes it.
            if local.get(key, 0.0) <= 0:
                continue

            price = _to_price(cached.get(f"{exchange}:{symbol}"))

            if price <= 0 and cached:
                # Defensive casing pass. The manager keys its cache with what
                # the feed sent back, which is not guaranteed to be upper case.
                if upper_index is None:
                    upper_index = {}
                    for cache_key, value in cached.items():
                        try:
                            upper_index[str(cache_key).upper()] = value
                        except Exception:
                            continue
                price = _to_price(upper_index.get(f"{exchange}:{symbol}"))

            if price <= 0:
                price = _to_price(local.get(key))

            if price > 0:
                prices[key] = price

        return prices

    def get_previous_closes(self, symbol_keys) -> Dict[SymbolKey, float]:
        """
        Previous closes pushed on Quote ticks, for the day change on screen.

        Deliberately not age gated, unlike get_prices. A previous close belongs
        to the last completed session, so it does not go stale while the market
        is open; a traded price does, and serving a frozen one as live is the
        failure get_prices exists to prevent.

        Never calls the broker. A symbol with no pushed close is simply absent,
        so the caller keeps its own REST fallback for the first poll after a
        restart and for anything the feed has not yet seen a tick for.

        Args:
            symbol_keys: iterable of (symbol, exchange) tuples.

        Returns:
            dict: {(SYMBOL, EXCHANGE): previous_close} with values above zero.
        """
        keys = _normalise_keys(symbol_keys)
        if not keys:
            return {}

        with self._lock:
            return {
                key: self._prev_closes[key]
                for key in keys
                if _to_price(self._prev_closes.get(key)) > 0
            }

    def prime(self, symbol_keys) -> Dict[SymbolKey, float]:
        """
        ensure_subscribed followed by get_prices, for a caller that wants both.

        On the first call for a symbol the price is usually absent, because the
        subscription has only just gone out. The caller falls back for that one
        poll and the price is there on the next.

        Returns:
            dict: same shape as get_prices, {(SYMBOL, EXCHANGE): price}.
        """
        keys = _normalise_keys(symbol_keys)
        if not keys:
            return {}
        self.ensure_subscribed(keys)
        return self.get_prices(keys)

    def release(self, symbol_keys) -> int:
        """
        Unsubscribe instruments that are no longer held, so the subscription set
        cannot grow without bound as holdings change.

        Symbols that were never subscribed are ignored, including ones still
        waiting in the pending set, which are simply forgotten.

        Args:
            symbol_keys: iterable of (symbol, exchange) tuples.

        Returns:
            int: how many symbols were dropped from the subscribed set.
        """
        keys = _normalise_keys(symbol_keys)
        if not keys:
            return 0

        with self._lock:
            released = [key for key in keys if key in self._subscribed]
            for key in released:
                self._remove_subscribed_locked(key)
            for key in keys:
                self._pending.discard(key)
                self._prices.pop(key, None)
                self._price_times.pop(key, None)
                self._prev_closes.pop(key, None)

        if released:
            self._unsubscribe(released)
        return len(released)

    def release_all(self) -> int:
        """
        Unsubscribe everything this feed holds and forget every pending symbol.

        Returns:
            int: how many symbols were dropped from the subscribed set.
        """
        with self._lock:
            released = sorted(self._subscribed)
            self._subscribed.clear()
            self._symbol_index.clear()
            self._pending.clear()
            self._prices.clear()
            self._price_times.clear()
            self._prev_closes.clear()

        if released:
            self._unsubscribe(released)
        return len(released)

    def status(self) -> Dict[str, object]:
        """
        Diagnostic snapshot. Safe to call at any time, never raises.

        Returns:
            dict: {
              'available': bool,          shared manager object exists
              'authenticated': bool,      manager has authenticated
              'handler_registered': bool, LTP handler is attached
              'subscribed': int,          symbols subscribed right now
              'pending': int,             symbols waiting for the feed
              'inflight': int,            symbols inside a subscribe call
              'priced': int,              subscribed symbols with a price
              'ticks': int,               ticks seen for tracked symbols
              'last_tick_at': str|None,   ISO 8601 UTC, tracked symbols
              'last_tick_age_seconds': float|None,
              'last_feed_tick_at': str|None,  ISO 8601 UTC, any LTP tick
            }
        """
        manager = _get_manager()

        with self._lock:
            counts = self._counts_locked()
            subscribed = sorted(self._subscribed)
            ticks = self._tick_count
            last_tick_at = self._last_tick_at
            last_feed_tick_at = self._last_feed_tick_at

        with self._handler_lock:
            handler_registered = self._handler_registered

        priced = len(self.get_prices(subscribed)) if subscribed else 0

        age = None
        if last_tick_at is not None:
            try:
                age = round((datetime.now(timezone.utc) - last_tick_at).total_seconds(), 3)
            except Exception:
                age = None

        return {
            'available': manager is not None,
            'authenticated': _is_ready(manager),
            'handler_registered': handler_registered,
            'subscribed': counts['subscribed'],
            'pending': counts['pending'],
            'inflight': counts['inflight'],
            'priced': priced,
            'ticks': ticks,
            'last_tick_at': last_tick_at.isoformat() if last_tick_at else None,
            'last_tick_age_seconds': age,
            'last_feed_tick_at': last_feed_tick_at.isoformat() if last_feed_tick_at else None,
        }

    # ------------------------------------------------------------------
    # Tick handling
    # ------------------------------------------------------------------

    def _on_tick(self, payload):
        """
        Tick handler, called on the WebSocket reader thread.

        Records the tick timestamp and, for a symbol this feed tracks, the
        pushed price. In Quote mode the tick also carries `close`, which is the
        PREVIOUS close rather than a current one, so it is stored separately and
        never confused with the traded price.

        Registered for both LTP and Quote ticks: the manager routes by the mode
        on each tick, not by what was subscribed, so a stray LTP tick during a
        mode change still lands somewhere rather than being dropped.

        No database write and no broker call happens here. Every failure is
        swallowed: an exception must never propagate back into the reader thread.
        """
        try:
            if not isinstance(payload, dict):
                return

            nested = payload.get('data')
            data = nested if isinstance(nested, dict) else payload

            symbol = str(payload.get('symbol') or data.get('symbol') or '').strip().upper()
            exchange = str(payload.get('exchange') or data.get('exchange') or '').strip().upper()
            price = _to_price(data.get('ltp') if data.get('ltp') is not None else payload.get('ltp'))
            # Quote mode only. Absent from an LTP tick, and 0 before the first
            # session of a newly listed symbol, so it is only stored when real.
            prev_close = _to_price(data.get('close'))

            now = datetime.now(timezone.utc)

            with self._lock:
                self._last_feed_tick_at = now
                if not symbol:
                    return
                key = self._resolve_key_locked(symbol, exchange)
                if key is None:
                    return
                self._tick_count += 1
                self._last_tick_at = now
                if price > 0:
                    # Store the tick time with the price. A price with no age
                    # cannot be told apart from a live one when the feed goes
                    # quiet, and a frozen number on a trading screen reads as
                    # current. get_prices treats an aged entry as absent so the
                    # bounded REST backstop refreshes it.
                    self._prices[key] = price
                    self._price_times[key] = now
                if prev_close > 0:
                    # No age gate. A previous close is a property of the last
                    # completed session, so unlike a traded price it does not
                    # go stale during the day.
                    self._prev_closes[key] = prev_close

        except Exception as exc:
            try:
                logger.debug(f"[EQUITY_FEED] Ignored bad tick: {exc}")
            except Exception:
                pass

    def _ensure_handler(self, manager):
        """
        Register the LTP handler on the manager's data processor, exactly once
        per manager instance. Guarded by a flag and a lock because the shared
        manager may not exist at import time.
        """
        if manager is None:
            return

        with self._handler_lock:
            if self._handler_registered:
                return
            try:
                processor = getattr(manager, 'data_processor', None)
                if processor is None or not hasattr(processor, 'register_ltp_handler'):
                    return
                # Both, because the manager routes each tick by the mode on the
                # tick itself. Subscribing in Quote mode does not guarantee that
                # nothing ever arrives as LTP, and a tick routed to a handler
                # that was never registered is simply lost.
                processor.register_ltp_handler(self._on_tick)
                if hasattr(processor, 'register_quote_handler'):
                    processor.register_quote_handler(self._on_tick)
                self._handler_registered = True
                logger.debug(
                    "[EQUITY_FEED] Tick handlers registered on shared WebSocket manager "
                    f"(subscribing in {SUBSCRIPTION_MODE} mode)"
                )
            except Exception as exc:
                logger.error(f"[EQUITY_FEED] Could not register tick handlers: {exc}")

    # ------------------------------------------------------------------
    # Internals
    # ------------------------------------------------------------------

    def _sync_manager(self, manager):
        """
        Detect a replaced shared manager.

        The background service drops and recreates the manager on a hard
        failure. Subscriptions and the handler registration do not survive that,
        so everything tracked moves back to pending and the handler flag is
        cleared, which makes the next ensure_subscribed rebuild the feed.
        """
        if manager is None:
            return

        with self._lock:
            previous = self._manager_ref() if self._manager_ref is not None else None
            if previous is manager:
                return
            try:
                self._manager_ref = weakref.ref(manager)
            except TypeError:
                self._manager_ref = None
            carried = len(self._subscribed) + len(self._inflight)
            self._pending.update(self._subscribed)
            self._pending.update(self._inflight)
            self._subscribed.clear()
            self._inflight.clear()
            self._symbol_index.clear()
            self._prices.clear()
            self._price_times.clear()
            self._prev_closes.clear()
            replaced = previous is not None

        with self._handler_lock:
            self._handler_registered = False

        if replaced:
            logger.debug(
                f"[EQUITY_FEED] Shared WebSocket manager replaced, "
                f"{carried} symbols queued for re-subscription"
            )

    def _manager_ltp(self, manager) -> Dict[str, object]:
        """
        Read the manager's cached LTP mapping, keyed 'EXCHANGE:SYMBOL'.

        The manager already applies zero-value protection. Any failure returns
        an empty mapping so the caller falls back to the local cache.
        """
        if manager is None:
            return {}
        try:
            data = manager.get_ltp()
        except Exception as exc:
            logger.debug(f"[EQUITY_FEED] LTP cache read failed: {exc}")
            return {}
        if not isinstance(data, dict):
            return {}
        cached = data.get('ltp')
        return cached if isinstance(cached, dict) else {}

    def _unsubscribe(self, keys: List[SymbolKey]):
        """Send one unsubscribe batch. Local state is already updated."""
        manager = _get_manager()
        if manager is None:
            return
        instruments = [{'symbol': symbol, 'exchange': exchange} for symbol, exchange in keys]
        try:
            manager.unsubscribe_batch(instruments, mode=SUBSCRIPTION_MODE)
            logger.debug(f"[EQUITY_FEED] Unsubscribed {len(instruments)} symbols")
        except Exception as exc:
            logger.error(f"[EQUITY_FEED] Unsubscribe failed for {len(instruments)} symbols: {exc}")

    def _absorb_locked(self, keys: List[SymbolKey]) -> int:
        """
        Queue unknown symbols as pending. Caller holds self._lock.

        Returns the number of symbols refused because MAX_TRACKED_SYMBOLS was
        reached, so the feed cannot grow without bound.
        """
        dropped = 0
        for key in keys:
            if key in self._subscribed or key in self._inflight or key in self._pending:
                continue
            tracked = len(self._subscribed) + len(self._inflight) + len(self._pending)
            if tracked >= MAX_TRACKED_SYMBOLS:
                dropped += 1
                continue
            self._pending.add(key)

        if dropped:
            logger.warning(
                f"[EQUITY_FEED] Symbol cap of {MAX_TRACKED_SYMBOLS} reached, "
                f"{dropped} symbols not subscribed"
            )
        return dropped

    def _take_pending_locked(self) -> List[SymbolKey]:
        """
        Move up to MAX_SUBSCRIBE_BATCH pending symbols into the in-flight set
        and return them. Caller holds self._lock.
        """
        if not self._pending:
            return []
        batch = sorted(self._pending)[:MAX_SUBSCRIBE_BATCH]
        for key in batch:
            self._pending.discard(key)
            self._inflight.add(key)
        return batch

    def _add_subscribed_locked(self, key: SymbolKey):
        """Record a confirmed subscription. Caller holds self._lock."""
        self._subscribed.add(key)
        symbol, exchange = key
        exchanges = self._symbol_index.get(symbol)
        if exchanges is None:
            self._symbol_index[symbol] = {exchange}
        else:
            exchanges.add(exchange)

    def _remove_subscribed_locked(self, key: SymbolKey):
        """Forget a subscription. Caller holds self._lock."""
        self._subscribed.discard(key)
        symbol, exchange = key
        exchanges = self._symbol_index.get(symbol)
        if exchanges is not None:
            exchanges.discard(exchange)
            if not exchanges:
                self._symbol_index.pop(symbol, None)

    def _resolve_key_locked(self, symbol: str, exchange: str) -> Optional[SymbolKey]:
        """
        Map a pushed tick to a tracked SymbolKey, or None. Caller holds
        self._lock.

        The data processor substitutes a default exchange when the feed omits
        one, so a tick whose exchange does not match is still accepted when the
        symbol is tracked on exactly one exchange.
        """
        exchanges = self._symbol_index.get(symbol)
        if not exchanges:
            return None
        if exchange and exchange in exchanges:
            return (symbol, exchange)
        if len(exchanges) == 1:
            return (symbol, next(iter(exchanges)))
        return None

    def _counts_locked(self) -> Dict[str, object]:
        """Current set sizes. Caller holds self._lock."""
        return {
            'subscribed': len(self._subscribed),
            'pending': len(self._pending),
            'inflight': len(self._inflight),
        }


# Global instance
equity_price_feed = EquityPriceFeed()
