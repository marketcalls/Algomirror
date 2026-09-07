"""
Keep the equity account cache warm, off the request path.

The F&O dashboard is fast because nothing on its request path talks to a
broker: background services keep the database current and the request is a
handful of SELECTs. The equity dashboard was doing the refresh itself, on every
poll, which is where its four to five seconds went.

This module supplies the missing half. It owns no logic of its own - the fan
out, the freshness gate and the cache write all already existed in
app/equity/routes.py and are unchanged. All this does is call them on a timer
so that by the time a screen asks, the answer is already in the database.

Follows the same contract as equity_exit_monitor: it does not schedule itself,
it exposes a plain callable plus a job id and interval for the app factory to
drive, and it must be armed with start() before the first tick.

Nothing here places, modifies or cancels an order.
"""

import logging

logger = logging.getLogger(__name__)

SCHEDULER_JOB_ID = 'equity_account_cache_warm'
SCHEDULER_INTERVAL_SECONDS = 20


class EquityAccountCacheWarmer(object):
    """Armed by the app factory, ticked by the scheduler."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super(EquityAccountCacheWarmer, cls).__new__(cls)
            cls._instance._armed = False
            cls._instance._stats = {
                'ticks': 0,
                'skipped': 0,
                'accounts_refreshed': 0,
                'failures': 0,
            }
        return cls._instance

    def start(self):
        self._armed = True

    def stop(self):
        self._armed = False

    @property
    def is_armed(self):
        return bool(self._armed)

    @property
    def stats(self):
        return dict(self._stats)

    def run_once(self):
        """
        One warm pass. Never raises: a broker that is down must not stop the
        scheduler, and the screens fall back to the request path refresh on
        their own when the cache is stale.
        """
        if not self._armed:
            return 0

        # Imported here, not at module scope. app.equity.routes imports from
        # app.utils, so a top level import in the other direction would be
        # circular at start up.
        from app.equity.routes import warm_account_cache

        self._stats['ticks'] += 1
        try:
            refreshed = warm_account_cache()
        except Exception as exc:                       # noqa: BLE001
            self._stats['failures'] += 1
            logger.warning(
                '[EQUITY_CACHE] Warm pass failed, screens will refresh on '
                'demand instead: %s' % exc
            )
            return 0

        if not refreshed:
            self._stats['skipped'] += 1
        else:
            self._stats['accounts_refreshed'] += refreshed
        return refreshed


equity_account_cache_warmer = EquityAccountCacheWarmer()


def run_equity_account_cache_warm():
    """Scheduler entry point. Must be called inside a Flask app context."""
    return equity_account_cache_warmer.run_once()
