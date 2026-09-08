"""
Guard rails for app/utils/equity_price_feed.py.

These exist because a NameError on get_prices() once reached production: the
staleness guard referenced a helper that did not exist, so every call raised and
the whole WebSocket price feed silently degraded to REST. Nothing was calling
these functions in a test, so nothing caught it.

The feed is deliberately import-safe with no WebSocket and no Flask app, so it
can be exercised directly.
"""

import importlib.util
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest

FEED_PATH = Path(__file__).resolve().parents[1] / "app" / "utils" / "equity_price_feed.py"


def _load():
    spec = importlib.util.spec_from_file_location("equity_price_feed_under_test", FEED_PATH)
    module = importlib.util.module_from_spec(spec)
    sys.modules["equity_price_feed_under_test"] = module
    spec.loader.exec_module(module)
    return module


feed_module = _load()
KEYS = [("RELIANCE", "NSE"), ("TCS", "NSE")]


def _fresh_feed():
    return feed_module.EquityPriceFeed() if hasattr(feed_module, "EquityPriceFeed") else feed_module.equity_price_feed


class TestCallableWithoutAWebSocket:
    """Every public entry point must return, not raise, when no feed is up."""

    def test_get_prices_does_not_raise(self):
        assert feed_module.equity_price_feed.get_prices(KEYS) == {}

    def test_get_prices_on_empty_input(self):
        assert feed_module.equity_price_feed.get_prices([]) == {}

    def test_ensure_subscribed_does_not_raise(self):
        result = feed_module.equity_price_feed.ensure_subscribed(KEYS)
        assert isinstance(result, dict)

    def test_prime_does_not_raise(self):
        assert isinstance(feed_module.equity_price_feed.prime(KEYS), dict)

    def test_status_does_not_raise(self):
        status = feed_module.equity_price_feed.status()
        assert isinstance(status, dict)
        assert "subscribed" in status

    def test_release_does_not_raise(self):
        assert isinstance(feed_module.equity_price_feed.release(KEYS), int)


class TestStalenessGuard:
    """
    The guard that caused the outage. A price past MAX_PRICE_AGE_SECONDS must be
    reported as absent so the caller's REST backstop refreshes it, and the
    comparison must be timezone aware in both directions.
    """

    def test_max_price_age_is_defined_and_sane(self):
        age = feed_module.MAX_PRICE_AGE_SECONDS
        assert age > 30, "must exceed the 30 second screen poll or a healthy feed is second-guessed"
        assert age < 600, "must be low enough that a dead subscription cannot look live"

    def test_a_fresh_pushed_price_is_returned(self):
        f = feed_module.equity_price_feed
        key = ("FRESHSYM", "NSE")
        with f._lock:
            f._prices[key] = 101.5
            f._price_times[key] = datetime.now(timezone.utc)
        try:
            assert f.get_prices([key]).get(key) == pytest.approx(101.5)
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)

    def test_an_aged_price_is_treated_as_absent(self):
        f = feed_module.equity_price_feed
        key = ("STALESYM", "NSE")
        old = datetime.now(timezone.utc) - timedelta(seconds=feed_module.MAX_PRICE_AGE_SECONDS + 30)
        with f._lock:
            f._prices[key] = 101.5
            f._price_times[key] = old
        try:
            # Absent, not zero and not an exception, so the caller falls back.
            assert key not in f.get_prices([key])
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)

    def test_stored_tick_times_are_timezone_aware(self):
        # A naive datetime here would make the cutoff comparison raise TypeError.
        f = feed_module.equity_price_feed
        key = ("TZSYM", "NSE")
        with f._lock:
            f._prices[key] = 10.0
            f._price_times[key] = datetime.now(timezone.utc)
            stored = f._price_times[key]
        try:
            assert stored.tzinfo is not None
            f.get_prices([key])
        finally:
            with f._lock:
                f._prices.pop(key, None)
                f._price_times.pop(key, None)


class TestQuoteMode:
    """
    The feed subscribes in Quote rather than LTP mode.

    Quote carries the previous close as `close`, which removes a REST call that
    used to run once per symbol per day. The risk introduced is confusing that
    previous close with a live price, so these tests pin that they are stored
    separately and aged differently.
    """

    def test_the_feed_subscribes_in_quote_mode(self):
        assert feed_module.SUBSCRIPTION_MODE == "quote"

    def test_a_quote_tick_records_the_previous_close(self):
        f = feed_module.equity_price_feed
        key = ("QUOTESYM", "NSE")
        with f._lock:
            f._subscribed.add(key)
            f._symbol_index.setdefault(key[0], set()).add(key[1])
        try:
            f._on_tick({
                "symbol": "QUOTESYM", "exchange": "NSE", "mode": 2,
                "data": {"ltp": 105.0, "close": 100.0, "open": 101.0,
                         "high": 106.0, "low": 100.5, "volume": 1000},
            })
            assert f.get_prices([key]).get(key) == pytest.approx(105.0)
            assert f.get_previous_closes([key]).get(key) == pytest.approx(100.0)
        finally:
            f.release([key])

    def test_a_previous_close_is_never_served_as_a_price(self):
        """The one way this change could put a wrong number on a screen."""
        f = feed_module.equity_price_feed
        key = ("CLOSEONLY", "NSE")
        with f._lock:
            f._subscribed.add(key)
            f._symbol_index.setdefault(key[0], set()).add(key[1])
        try:
            # A tick with a close but no traded price yet.
            f._on_tick({
                "symbol": "CLOSEONLY", "exchange": "NSE", "mode": 2,
                "data": {"ltp": 0, "close": 100.0},
            })
            assert key not in f.get_prices([key])
            assert f.get_previous_closes([key]).get(key) == pytest.approx(100.0)
        finally:
            f.release([key])

    def test_the_previous_close_is_not_age_gated(self):
        """It belongs to a finished session, so it does not go stale intraday."""
        f = feed_module.equity_price_feed
        key = ("OLDCLOSE", "NSE")
        old = datetime.now(timezone.utc) - timedelta(seconds=feed_module.MAX_PRICE_AGE_SECONDS + 300)
        with f._lock:
            f._prev_closes[key] = 100.0
            f._prices[key] = 105.0
            f._price_times[key] = old
        try:
            # The price has aged out; the previous close has not.
            assert key not in f.get_prices([key])
            assert f.get_previous_closes([key]).get(key) == pytest.approx(100.0)
        finally:
            with f._lock:
                f._prev_closes.pop(key, None)
                f._prices.pop(key, None)
                f._price_times.pop(key, None)

    def test_an_ltp_only_tick_still_records_the_price(self):
        """A stray LTP tick during a mode change must not be dropped."""
        f = feed_module.equity_price_feed
        key = ("LTPONLY", "NSE")
        with f._lock:
            f._subscribed.add(key)
            f._symbol_index.setdefault(key[0], set()).add(key[1])
        try:
            f._on_tick({"symbol": "LTPONLY", "exchange": "NSE", "mode": 1,
                        "data": {"ltp": 42.0}})
            assert f.get_prices([key]).get(key) == pytest.approx(42.0)
            assert key not in f.get_previous_closes([key])
        finally:
            f.release([key])

    def test_a_zero_close_is_not_stored(self):
        """Zero is what a newly listed symbol reports before its first session."""
        f = feed_module.equity_price_feed
        key = ("ZEROCLOSE", "NSE")
        with f._lock:
            f._subscribed.add(key)
            f._symbol_index.setdefault(key[0], set()).add(key[1])
        try:
            f._on_tick({"symbol": "ZEROCLOSE", "exchange": "NSE", "mode": 2,
                        "data": {"ltp": 10.0, "close": 0}})
            assert key not in f.get_previous_closes([key])
        finally:
            f.release([key])

    def test_releasing_a_symbol_drops_its_previous_close(self):
        f = feed_module.equity_price_feed
        key = ("DROPME", "NSE")
        with f._lock:
            f._subscribed.add(key)
            f._prev_closes[key] = 100.0
        f.release([key])
        assert key not in f.get_previous_closes([key])

    def test_get_previous_closes_on_empty_input(self):
        assert feed_module.equity_price_feed.get_previous_closes([]) == {}


class TestDepthMode:
    """
    Depth is subscribed separately, for one symbol, and ages out fast.

    A 90 second old last traded price is a number that has not moved. A 90
    second old order book is fiction, and the Place Order screen is exactly
    where somebody acts on it, so depth has its own much shorter ceiling and is
    reported as absent rather than served once past it.
    """

    def test_depth_is_its_own_mode(self):
        assert feed_module.DEPTH_MODE == 'depth'

    def test_depth_ages_out_far_sooner_than_a_price(self):
        assert feed_module.MAX_DEPTH_AGE_SECONDS < feed_module.MAX_PRICE_AGE_SECONDS

    def test_a_depth_tick_for_the_subscribed_symbol_is_kept(self):
        f = feed_module.equity_price_feed
        key = ("DEPTHSYM", "NSE")
        with f._lock:
            f._depth_key = key
        try:
            f._on_depth_tick({
                "symbol": "DEPTHSYM", "exchange": "NSE", "mode": 3,
                "data": {"ltp": 100.0, "close": 99.0,
                         "depth": {"buy": [{"price": 100.0, "quantity": 5, "orders": 2}],
                                   "sell": [{"price": 100.5, "quantity": 4, "orders": 1}]}},
            })
            book = f.get_depth(key)
            assert book is not None
            assert book["buy"][0]["orders"] == 2
        finally:
            f.release_depth()

    def test_a_depth_tick_for_another_symbol_is_ignored(self):
        """An unsubscribe in flight must not put another book under this name."""
        f = feed_module.equity_price_feed
        key = ("MINE", "NSE")
        with f._lock:
            f._depth_key = key
        try:
            f._on_depth_tick({
                "symbol": "SOMEONEELSE", "exchange": "NSE", "mode": 3,
                "data": {"depth": {"buy": [{"price": 1.0, "quantity": 1}], "sell": []}},
            })
            assert f.get_depth(key) is None
        finally:
            f.release_depth()

    def test_a_stale_book_is_reported_as_absent(self):
        f = feed_module.equity_price_feed
        key = ("STALEDEPTH", "NSE")
        old = datetime.now(timezone.utc) - timedelta(
            seconds=feed_module.MAX_DEPTH_AGE_SECONDS + 5
        )
        with f._lock:
            f._depth_key = key
            f._depth = {"symbol": "STALEDEPTH", "exchange": "NSE", "buy": [], "sell": []}
            f._depth_at = old
        try:
            assert f.get_depth(key) is None
        finally:
            f.release_depth()

    def test_asking_for_a_symbol_that_is_not_subscribed_returns_none(self):
        f = feed_module.equity_price_feed
        f.release_depth()
        assert f.get_depth(("NOTSUBSCRIBED", "NSE")) is None

    def test_a_malformed_depth_tick_does_not_raise(self):
        """This runs on the reader thread; an exception takes the feed down."""
        f = feed_module.equity_price_feed
        for bad in (None, "nonsense", {}, {"symbol": "X"},
                    {"symbol": "X", "data": {"depth": "not-a-dict"}}):
            f._on_depth_tick(bad)

    def test_release_depth_is_safe_when_nothing_is_subscribed(self):
        f = feed_module.equity_price_feed
        f.release_depth()
        assert f.release_depth() is False

    def test_ensure_depth_without_a_feed_reports_failure(self):
        """No manager means the caller must fall back to its REST snapshot."""
        f = feed_module.equity_price_feed
        assert f.ensure_depth(("ANY", "NSE")) is False

    def test_ensure_depth_rejects_an_unusable_symbol(self):
        f = feed_module.equity_price_feed
        assert f.ensure_depth(None) is False
        assert f.ensure_depth("") is False
