"""
A process-wide change signal for the Equity module.

The equity screens polled: every one ran a setInterval and re-fetched its whole
payload on a timer whether anything had changed or not. That is the browser half
of the polling this module set out to remove, and it is wasteful in the specific
way that matters here, because the server already knows the instant something
changes. Prices arrive on the shared WebSocket and order state arrives on the
order stream.

This is the smallest thing that turns "the server knows" into "the browser
knows": a revision counter per topic and a threading.Event to wake anybody
waiting on it.

    bump(TOPIC_ORDERS)             called by whatever changed something
    wait_for_change(seen, timeout)  called by an SSE generator

A waiter passes the revisions it has already sent. If the current revisions are
higher it returns immediately, otherwise it BLOCKS until a bump or the timeout.
The timeout is a heartbeat, not a poll: it exists so an SSE connection sends
something occasionally and so a client that has gone away is noticed, and no
work happens when it expires.

Revisions are per topic so a price tick does not force every screen to rebuild
its order book, and they are plain integers guarded by one lock because every
critical section is a single increment or a read.

Deliberately in-process. The equity module runs on one worker (see
app/utils/service_lock.py), so a shared counter is enough and a Redis dependency
would buy nothing. If that ever changes, this is the seam to replace.
"""

import logging
import threading

logger = logging.getLogger(__name__)

# Topics. A screen subscribes to the ones it renders, so a watch list price tick
# does not wake the Trade Book.
TOPIC_PRICES = 'prices'
TOPIC_ORDERS = 'orders'
TOPIC_HOLDINGS = 'holdings'
TOPIC_ALERTS = 'alerts'
TOPIC_EXTERNAL = 'external'

ALL_TOPICS = (
    TOPIC_PRICES, TOPIC_ORDERS, TOPIC_HOLDINGS, TOPIC_ALERTS, TOPIC_EXTERNAL,
)

_lock = threading.Lock()
_revisions = {topic: 0 for topic in ALL_TOPICS}

# One Condition rather than an Event per topic: waiters are few (one per open
# SSE connection) and a single notify_all is cheaper than tracking which topic
# each waiter cares about at signal time. The waiter does that filtering itself.
_changed = threading.Condition(_lock)


def bump(topic):
    """
    Record that something on this topic changed, and wake every waiter.

    Called from the WebSocket reader thread and from the order stream worker, so
    it must be cheap and must never raise: an exception here would propagate
    into a reader thread and take a feed down.
    """
    if topic not in _revisions:
        return
    try:
        with _changed:
            _revisions[topic] += 1
            _changed.notify_all()
    except Exception as exc:
        try:
            logger.debug('[EQUITY_EVENTS] bump(%s) failed: %s', topic, exc)
        except Exception:
            pass


def revisions(topics=None):
    """Current revision per topic, for a caller about to wait on them."""
    wanted = topics or ALL_TOPICS
    with _lock:
        return {topic: _revisions.get(topic, 0) for topic in wanted if topic in _revisions}


def wait_for_change(seen, timeout=25.0, topics=None):
    """
    Block until one of these topics moves past what the caller has seen.

    Args:
        seen: {topic: revision} the caller has already acted on.
        timeout: seconds to wait before returning anyway. This is a heartbeat
            for the SSE connection, not a poll: nothing happens on expiry, the
            caller simply gets a chance to send a keep-alive and notice a
            client that has disconnected.
        topics: which topics to watch. Defaults to all of them.

    Returns:
        ({topic: revision}, changed) where changed is False on a timeout.
    """
    wanted = [t for t in (topics or ALL_TOPICS) if t in _revisions]
    seen = seen or {}

    with _changed:
        def _moved():
            return any(_revisions[t] > seen.get(t, -1) for t in wanted)

        if _moved():
            return {t: _revisions[t] for t in wanted}, True

        # wait() releases the lock and blocks. It returns False on timeout.
        signalled = _changed.wait(timeout=timeout)
        current = {t: _revisions[t] for t in wanted}
        return current, bool(signalled and _moved())


def reset_for_tests():
    """Zero every revision. Only for tests."""
    with _changed:
        for topic in _revisions:
            _revisions[topic] = 0
        _changed.notify_all()
