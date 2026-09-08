"""
Equity (CNC delivery) order placement engine.

This module owns every broker WRITE the equity module makes. Nothing else in
the equity module may call placeorder, modifyorder, cancelorder or any of the
GTT endpoints. Keeping the writes in one file is what makes the safety rules
below auditable: there is exactly one place to check them.

WHAT THIS MODULE GUARANTEES

1. Claim before order. An exit (a sell against an existing holding) is never
   sent to a broker until the holding row has been locked, re-checked and
   committed as claimed. exit_holding is the ONE helper that does this, and
   both the manual sell and the background stop loss / target monitor call it.
   The claim itself lives on EquityHolding (claim_for_exit, mark_exit_submitted,
   mark_exit_indeterminate, release_exit_claim), which already encodes the lock
   and re-check. This module orchestrates, it does not reimplement.

2. A timeout is not a rejection. ExtendedOpenAlgoAPI tags a request that never
   got an answer as 'timeout_error' or 'connection_error'. The order may be
   live at the broker, so an indeterminate outcome is NEVER retried: it lands
   in EQUITY_SPLIT_STATUS_INDETERMINATE (and EXIT_INDETERMINATE on the holding)
   and waits for a human. A definite rejection is safe to re-send, because
   nothing reached the broker or the broker refused it outright.
   equity_is_indeterminate_response is the single place that decision is made.
   A success with no order id is treated as indeterminate too: an order we
   cannot name is one we can neither track nor cancel.

3. Partial failure is normal. If three accounts succeed and two fail, the three
   stand. No placed order is ever rolled back, and one account's failure never
   stops another account's order. The parent order becomes PARTIAL, which is an
   outcome, not an error.

4. Product is always CNC. There is no product argument anywhere in this module.

5. No freeze quantity splitting. Freeze quantity is an F&O exchange regime.
   Equity delivery orders go straight to placeorder, never through
   app.utils.freeze_quantity_handler and never through splitorder.

6. No buy before sell phasing. One equity instruction is one symbol on one
   side, so there is nothing to phase.

7. Every query is ownership scoped on user_id. An id on its own is never
   enough to reach a row.

8. Threading. The Flask app object is captured before any thread starts, every
   worker body runs inside "with app.app_context():", credentials are extracted
   into plain dicts before the pool is created so no ORM lazy load crosses a
   thread boundary, every worker is wrapped in try/except, and the pool is
   bounded by MAX_ORDER_WORKERS.

9. API keys are read through account.get_api_key() only, are held in plain
   credential dicts that never leave this module, and are never returned to a
   caller or written to a log.

10. Point in time. qty_ratio_at_order, ratio_quantity and cash_balance_at_order
    are written once, when the order is created, and are never recalculated.
    A later allocation change or a later modify does not rewrite them.

STOP LOSS AND TARGET ARE NOT SENT TO THE BROKER. AlgoMirror watches them in its
own monitor (PRD 8.3) and exits through exit_holding. Sending them as broker
side attached orders as well would leave two protective orders against one
holding, which is exactly the duplicate sell the claim exists to prevent.

TESTABILITY. Every broker interaction goes through one seam: the client_factory
argument. It is called as client_factory(credential) where credential is a
plain dict, and it must return an object exposing the ExtendedOpenAlgoAPI
surface this module uses (placeorder, modifyorder, cancelorder, funds and
_make_request). Tests pass a fake. Nothing in this module constructs a broker
client except default_client_factory.

GTT. The installed OpenAlgo SDK has no GTT methods, so GTT goes through the
raw endpoints placegttorder, modifygttorder and cancelgttorder using the
client's _make_request. Only Dhan and Zerodha ship a gtt_api module in
OpenAlgo. Every other broker answers HTTP 501, which fails THAT account with
EQUITY_SPLIT_STATUS_UNSUPPORTED and a specific message while the remaining
accounts proceed normally.
"""

import logging
import time
from collections import OrderedDict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from flask import current_app

from app import db
from app.models import (
    EquityAccountAllocation,
    EquityHolding,
    EquityOrder,
    EquityOrderSplit,
    EquitySetting,
    TradingAccount,
    EQUITY_EXIT_REASON_MANUAL,
    EQUITY_EXIT_REASON_STOP_LOSS,
    EQUITY_EXIT_REASON_TARGET,
    EQUITY_FUNDS_ACTION_ABORT,
    EQUITY_FUNDS_ACTION_SKIP,
    EQUITY_ORDER_SOURCE_MANUAL,
    EQUITY_ORDER_SOURCE_STOP_LOSS,
    EQUITY_ORDER_SOURCE_TARGET,
    EQUITY_ORDER_STATUS_CANCELLED,
    EQUITY_ORDER_STATUS_COMPLETED,
    EQUITY_ORDER_STATUS_PARTIAL,
    EQUITY_ORDER_STATUS_PENDING,
    EQUITY_ORDER_TYPE_GTT,
    EQUITY_ORDER_TYPE_LIMIT,
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_PRODUCT_CNC,
    EQUITY_SIDE_BUY,
    EQUITY_SIDE_SELL,
    EQUITY_SPLIT_STATUS_CANCELLED,
    EQUITY_SPLIT_STATUS_COMPLETED,
    EQUITY_SPLIT_STATUS_FAILED,
    EQUITY_SPLIT_STATUS_INDETERMINATE,
    EQUITY_SPLIT_STATUS_PENDING,
    EQUITY_SPLIT_STATUS_REJECTED,
    EQUITY_SPLIT_STATUS_SKIPPED,
    EQUITY_SPLIT_STATUS_UNSUPPORTED,
    EQUITY_SPLIT_STATUSES_OPEN,
    equity_is_indeterminate_response,
)
from app.utils.equity_ratio import compute_order_qty_ratios, split_quantity_by_ratio
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)

__all__ = [
    'EquityOrderError',
    'DEFAULT_STRATEGY_NAME',
    'BROKER_ORDER_TIMEOUT_SECONDS',
    'BROKER_FUNDS_TIMEOUT_SECONDS',
    'DEFAULT_MAX_ATTEMPTS',
    'DEFAULT_RETRY_DELAY_SECONDS',
    'MAX_ORDER_WORKERS',
    'GTT_LEG_STOP_LOSS',
    'GTT_LEG_TARGET',
    'default_client_factory',
    'preview_order_split',
    'place_multi_account_order',
    'modify_order',
    'cancel_order',
    'exit_holding',
    'exit_holdings',
    'recompute_parent_status',
    'summarise_splits',
]


# ---------------------------------------------------------------------------
# Tunables
# ---------------------------------------------------------------------------

# Strategy tag sent to OpenAlgo on every equity write, so an order raised here
# is identifiable in the broker's own logs.
DEFAULT_STRATEGY_NAME = 'AlgoMirror Equity'

# A write is not a screen refresh. It is given a generous timeout on purpose:
# every second shaved off here converts a slow but successful order into an
# INDETERMINATE one that a human then has to reconcile by hand.
BROKER_ORDER_TIMEOUT_SECONDS = 30

# The pre-trade funds read sits in front of the order, so it fails fast. An
# account whose cash cannot be read is not blocked, it is placed with the funds
# check recorded as not performed.
BROKER_FUNDS_TIMEOUT_SECONDS = 8

# Placement attempts per account. Only a DEFINITE failure is re-sent, so this
# can never duplicate an order. Kept low because every extra attempt widens the
# window in which an indeterminate outcome can happen.
DEFAULT_MAX_ATTEMPTS = 2
DEFAULT_RETRY_DELAY_SECONDS = 0.5

# Upper bound on the fan-out pool, one worker per account.
MAX_ORDER_WORKERS = 10

# OpenAlgo answers a GTT request with HTTP 501 when the broker ships no
# gtt_api module. Five brokers currently do: angel, dhan, fyers, upstox and
# zerodha. The other nineteen supported brokers refuse every GTT call, which is
# the first thing to check when a user reports "GTT is not working".
GTT_UNSUPPORTED_HTTP_STATUS = 501

# OpenAlgo GTT endpoints, posted through the client's _make_request. openalgo
# 2.0.4 added native GTT methods, but they are not used here: the raw post
# keeps GTT on AlgoMirror's own error envelope, which is what every retry and
# indeterminacy rule in this module reads.
GTT_PLACE_ENDPOINT = 'placegttorder'
GTT_MODIFY_ENDPOINT = 'modifygttorder'
GTT_CANCEL_ENDPOINT = 'cancelgttorder'

# A SINGLE GTT carries its trigger in exactly one of two slots. The slots are
# labels for what the trigger means, not different behaviours: OpenAlgo
# resolves whichever one is positive into the trigger price it sends on.
#
# Two broker traps are avoided by construction rather than by a guard, so they
# are recorded here instead:
#
#   OCO is never placed. On Upstox an OCO does not bracket an existing holding,
#   it opens a position at market on the inverse of `action` before arming the
#   legs, and discards the per-leg limit prices. Every GTT here is SINGLE.
#
#   MIS is never sent. GTT accepts CNC and NRML only and rejects MIS outright,
#   because a trigger can rest for days. _gtt_payload hardcodes CNC.
GTT_TRIGGER_TYPE_SINGLE = 'SINGLE'
GTT_LEG_STOP_LOSS = 'SL'
GTT_LEG_TARGET = 'TG'

# Broker payload aliases for the cash figure. The equity screens read
# availablecash, the aliases cover adapters that spell it differently.
_CASH_KEYS = ('availablecash', 'available_cash', 'cash', 'availablebalance')

# Where an order id can appear in a placeorder response.
_ORDER_ID_KEYS = ('orderid', 'order_id', 'orderId')

# Where a GTT trigger id can appear in a placegttorder response.
_TRIGGER_ID_KEYS = ('trigger_id', 'triggerid', 'gtt_id', 'gttid')

_VALID_SIDES = (EQUITY_SIDE_BUY, EQUITY_SIDE_SELL)
_VALID_ORDER_TYPES = (
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_ORDER_TYPE_LIMIT,
    EQUITY_ORDER_TYPE_GTT,
)

# Exit reason to the source recorded on the order that carries the exit.
_EXIT_REASON_TO_SOURCE = {
    EQUITY_EXIT_REASON_STOP_LOSS: EQUITY_ORDER_SOURCE_STOP_LOSS,
    EQUITY_EXIT_REASON_TARGET: EQUITY_ORDER_SOURCE_TARGET,
    EQUITY_EXIT_REASON_MANUAL: EQUITY_ORDER_SOURCE_MANUAL,
}


class EquityOrderError(ValueError):
    """
    The instruction itself is unusable, so nothing was sent anywhere.

    Raised internally and converted into an error result by the public
    functions, which never raise for bad input.
    """


# ---------------------------------------------------------------------------
# Small helpers
# ---------------------------------------------------------------------------

def _to_int(value, default=0):
    """Coerce to int, falling back to default on anything unusable."""
    try:
        if value is None or value == '':
            return default
        return int(float(value))
    except (TypeError, ValueError):
        return default


def _to_float(value, default=None):
    """Coerce to float, falling back to default on anything unusable."""
    try:
        if value is None or value == '':
            return default
        result = float(value)
    except (TypeError, ValueError):
        return default
    if result != result or result in (float('inf'), float('-inf')):
        return default
    return result


def _first_number(mapping, keys, default=None):
    """First key in keys that carries a usable number."""
    if not isinstance(mapping, dict):
        return default
    for key in keys:
        value = _to_float(mapping.get(key))
        if value is not None:
            return value
    return default


def _first_text(mapping, keys):
    """First key in keys that carries a non-empty value, as text."""
    if not isinstance(mapping, dict):
        return None
    for key in keys:
        value = mapping.get(key)
        if value not in (None, ''):
            return str(value)
    return None


def _clip(text, limit=1000):
    """Trim free text before it goes into a Text column or a log line."""
    if text is None:
        return None
    return str(text)[:limit]


def _now():
    """UTC now. The equity schema stores naive UTC throughout."""
    return datetime.utcnow()


def default_client_factory(credential):
    """
    Build the real broker client for one account.

    This is the only place in the equity module that constructs a broker
    client for a write. Tests replace it wholesale by passing their own
    client_factory, which is why no other function here builds a client.

    Args:
        credential: plain dict with api_key, host_url and an optional timeout.
            Never an ORM object, so it is safe to hand to a worker thread.

    Returns:
        ExtendedOpenAlgoAPI. Keyword arguments only: the installed SDK moved
        timeout into the fourth positional slot, so a positional call would
        silently pass the timeout as ws_port.
    """
    return ExtendedOpenAlgoAPI(
        api_key=credential.get('api_key'),
        host=credential.get('host_url'),
        timeout=credential.get('timeout') or BROKER_ORDER_TIMEOUT_SECONDS,
    )


def _resolve_factory(client_factory):
    """The caller's seam, or the real client when none was supplied."""
    return client_factory or default_client_factory


def _sleep_with(sleeper, seconds):
    """Sleep between retries through the injected sleeper, if there is one."""
    if seconds and seconds > 0:
        (sleeper or time.sleep)(seconds)


# ---------------------------------------------------------------------------
# Broker response reading
#
# Every judgement about what a broker answer MEANS is made here, once.
# ---------------------------------------------------------------------------

def _is_success(response):
    """True only for an explicit success envelope."""
    return isinstance(response, dict) and response.get('status') == 'success'


def _response_message(response, fallback='Broker did not return a result'):
    """Human readable failure text from a broker response."""
    if not isinstance(response, dict):
        return fallback
    return str(response.get('message') or fallback)


def _response_error_type(response):
    """Raw error_type from ExtendedOpenAlgoAPI, kept verbatim for reconciliation."""
    if not isinstance(response, dict):
        return None
    error_type = response.get('error_type')
    return str(error_type) if error_type else None


def _is_gtt_unsupported(response):
    """
    True when the broker has no GTT capability at all.

    OpenAlgo's GTT services return HTTP 501 when the broker ships no gtt_api
    module, and ExtendedOpenAlgoAPI turns a non 200 into an http_error envelope
    carrying the status code. This is a permanent property of that broker, not
    a transient failure, so the split is marked UNSUPPORTED and never retried.
    """
    if not isinstance(response, dict):
        return False
    if _to_int(response.get('code'), 0) == GTT_UNSUPPORTED_HTTP_STATUS:
        return True
    message = str(response.get('message') or '')
    return 'HTTP %d' % GTT_UNSUPPORTED_HTTP_STATUS in message


def _extract_order_id(response):
    """Broker order id from a placeorder response, wherever the adapter put it."""
    if not isinstance(response, dict):
        return None
    found = _first_text(response, _ORDER_ID_KEYS)
    if found:
        return found
    data = response.get('data')
    return _first_text(data, _ORDER_ID_KEYS) if isinstance(data, dict) else None


def _extract_trigger_id(response):
    """GTT trigger id from a placegttorder response."""
    if not isinstance(response, dict):
        return None
    found = _first_text(response, _TRIGGER_ID_KEYS)
    if found:
        return found
    data = response.get('data')
    return _first_text(data, _TRIGGER_ID_KEYS) if isinstance(data, dict) else None


def _cash_from_funds(response):
    """
    Available cash out of a funds() response, or None when it cannot be read.

    None is deliberately different from 0.0: a cash figure that could not be
    read must not be treated as an empty account and skip the order.
    """
    if not _is_success(response):
        return None
    data = response.get('data')
    if not isinstance(data, dict):
        return None
    return _first_number(data, _CASH_KEYS)


# ---------------------------------------------------------------------------
# The broker calls themselves. Nothing else in the equity module may do this.
# ---------------------------------------------------------------------------

def _call_endpoint(client, endpoint, payload):
    """
    Post one raw OpenAlgo endpoint through the client's request helper.

    Used for the GTT endpoints, which the installed SDK does not wrap.
    ExtendedOpenAlgoAPI._make_request is AlgoMirror's own override, so its
    error envelope (including error_type) is the same one the wrapped methods
    return and every rule in this module still applies.
    """
    request = getattr(client, '_make_request', None)
    if not callable(request):
        return {
            'status': 'error',
            'message': 'Broker client cannot post the %s endpoint' % endpoint,
            'error_type': 'unsupported_client',
        }
    return request(endpoint, payload)


def _place_regular_order(client, job):
    """
    One MARKET or LIMIT placement attempt. Product is CNC, always.

    placeorder is called directly. Equity delivery has no freeze quantity
    regime, so splitorder and the freeze quantity handler are not involved.
    Stop loss and target are NOT sent: AlgoMirror monitors them itself.
    """
    params = {
        'strategy': job['strategy_name'],
        'symbol': job['symbol'],
        'action': job['side'],
        'exchange': job['exchange'],
        'price_type': job['order_type'],
        'product': EQUITY_PRODUCT_CNC,
        'quantity': job['quantity'],
    }
    if job['order_type'] == EQUITY_ORDER_TYPE_LIMIT:
        params['price'] = job['price']
    return client.placeorder(**params)


def _gtt_leg_for(side, gtt_trigger_leg=None):
    """
    Pick which SINGLE GTT slot carries the trigger.

    A SELL GTT is normally protective, so it goes in the stop loss slot. A BUY
    GTT is normally an entry, so it goes in the target slot. OpenAlgo resolves
    whichever slot is positive into one trigger price either way, so this is a
    label, not a behaviour change. Callers can override it explicitly.
    """
    if gtt_trigger_leg in (GTT_LEG_STOP_LOSS, GTT_LEG_TARGET):
        return gtt_trigger_leg
    return GTT_LEG_STOP_LOSS if side == EQUITY_SIDE_SELL else GTT_LEG_TARGET


def _gtt_payload(job, api_key, trigger_id=None):
    """Flat GTT payload in the shape OpenAlgo's GTT schema expects."""
    leg = _gtt_leg_for(job['side'], job.get('gtt_trigger_leg'))
    trigger = _to_float(job.get('trigger_price'), 0.0) or 0.0
    payload = {
        'apikey': api_key,
        'strategy': job['strategy_name'],
        'trigger_type': GTT_TRIGGER_TYPE_SINGLE,
        'exchange': job['exchange'],
        'symbol': job['symbol'],
        'action': job['side'],
        'product': EQUITY_PRODUCT_CNC,
        'quantity': job['quantity'],
        'pricetype': EQUITY_ORDER_TYPE_LIMIT,
        'price': job['price'],
        'triggerprice_sl': trigger if leg == GTT_LEG_STOP_LOSS else 0.0,
        'triggerprice_tg': trigger if leg == GTT_LEG_TARGET else 0.0,
    }
    if trigger_id:
        payload['trigger_id'] = str(trigger_id)
    return payload


def _place_gtt_order(client, job):
    """One GTT placement attempt against the placegttorder endpoint."""
    payload = _gtt_payload(job, job['credential'].get('api_key'))
    return _call_endpoint(client, GTT_PLACE_ENDPOINT, payload)


def _blank_outcome():
    """The result shape every placement attempt resolves to."""
    return {
        'ok': False,
        'fill_status': EQUITY_SPLIT_STATUS_FAILED,
        'broker_order_id': None,
        'broker_gtt_id': None,
        'error_message': None,
        'error_type': None,
        'indeterminate': False,
        'unsupported': False,
        'attempts': 0,
        'placed_at': None,
        'broker_order_status': None,
    }


def _classify_placement(response, is_gtt):
    """
    Turn one broker answer into a split outcome.

    The three failure shapes are kept apart because they carry different
    safety rules:
        indeterminate  the request never got an answer, so the order may be
                       live. Terminal, never retried.
        unsupported    the broker cannot serve a GTT at all. Terminal for this
                       account, retrying would fail identically.
        definite       the broker refused. Nothing is live, so it is safe to
                       re-send.
    """
    outcome = _blank_outcome()

    if equity_is_indeterminate_response(response):
        outcome['fill_status'] = EQUITY_SPLIT_STATUS_INDETERMINATE
        outcome['indeterminate'] = True
        outcome['error_type'] = _response_error_type(response)
        outcome['error_message'] = _response_message(
            response, 'No answer from the broker, the order may still be live'
        )
        return outcome

    if is_gtt and _is_gtt_unsupported(response):
        outcome['fill_status'] = EQUITY_SPLIT_STATUS_UNSUPPORTED
        outcome['unsupported'] = True
        outcome['error_type'] = _response_error_type(response)
        outcome['error_message'] = (
            'This broker does not support GTT orders. Place a MARKET or LIMIT '
            'order for this account instead. Broker said: %s'
            % _response_message(response, 'GTT is not available')
        )
        return outcome

    if _is_success(response):
        identifier = _extract_trigger_id(response) if is_gtt else _extract_order_id(response)
        if not identifier:
            # The broker says it worked but will not name the order. It cannot
            # be tracked, modified or cancelled, so treat it exactly like a
            # lost answer: terminal, never retried, reconciled by a human.
            outcome['fill_status'] = EQUITY_SPLIT_STATUS_INDETERMINATE
            outcome['indeterminate'] = True
            outcome['error_type'] = 'missing_order_id'
            outcome['error_message'] = (
                'Broker reported success but returned no %s. Verify at the '
                'broker before re-sending.' % ('trigger id' if is_gtt else 'order id')
            )
            return outcome

        outcome['ok'] = True
        outcome['fill_status'] = EQUITY_SPLIT_STATUS_PENDING
        if is_gtt:
            outcome['broker_gtt_id'] = identifier
        else:
            outcome['broker_order_id'] = identifier
        outcome['broker_order_status'] = _first_text(response, ('order_status', 'orderstatus'))
        return outcome

    outcome['fill_status'] = EQUITY_SPLIT_STATUS_REJECTED
    outcome['error_type'] = _response_error_type(response)
    outcome['error_message'] = _response_message(response, 'Broker rejected the order')
    return outcome


def _send_placement(client, job):
    """
    Place one account's order, retrying ONLY a definite refusal.

    An indeterminate outcome and an unsupported order type both stop the loop
    at once. attempt_count is the true number of requests sent, so a split that
    ended INDETERMINATE on its second attempt still shows two: the count is
    evidence for the person reconciling it, and the guard against re-sending is
    the status itself (INDETERMINATE is absent from
    EQUITY_SPLIT_STATUSES_SAFE_TO_RETRY), never the count.
    """
    is_gtt = job['order_type'] == EQUITY_ORDER_TYPE_GTT
    max_attempts = max(1, _to_int(job.get('max_attempts'), DEFAULT_MAX_ATTEMPTS))
    outcome = _blank_outcome()

    for attempt in range(1, max_attempts + 1):
        sent_at = _now()
        try:
            response = (
                _place_gtt_order(client, job) if is_gtt
                else _place_regular_order(client, job)
            )
        except Exception as exc:
            # The call blew up before it returned, so we cannot know whether
            # the request reached the broker. equity_is_indeterminate_response
            # treats a missing response as unknown, which is the safe side.
            logger.error(
                'Equity placement raised for account %s on %s: %s',
                job['credential'].get('account_id'), job.get('symbol'), exc
            )
            response = None

        outcome = _classify_placement(response, is_gtt)
        outcome['attempts'] = attempt
        outcome['placed_at'] = sent_at

        if outcome['ok'] or outcome['indeterminate'] or outcome['unsupported']:
            return outcome

        if attempt < max_attempts:
            logger.warning(
                'Equity placement attempt %d of %d refused for account %s on %s: %s',
                attempt, max_attempts, job['credential'].get('account_id'),
                job.get('symbol'), outcome['error_message']
            )
            _sleep_with(job.get('sleeper'), job.get('retry_delay_seconds'))

    return outcome


def _placement_worker(app, job):
    """
    One account's placement, start to finish, on its own thread.

    Makes NO database call. Threads return plain data and the calling thread
    writes it, which keeps every split write in one transaction and avoids
    write lock contention between workers on SQLite.
    """
    account_id = job['credential'].get('account_id')
    with app.app_context():
        try:
            client = _resolve_factory(job.get('client_factory'))(job['credential'])
        except Exception as exc:
            outcome = _blank_outcome()
            outcome['fill_status'] = EQUITY_SPLIT_STATUS_FAILED
            outcome['error_message'] = 'Could not open a broker connection: %s' % exc
            outcome['error_type'] = 'client_error'
            outcome['account_id'] = account_id
            return outcome

        try:
            outcome = _send_placement(client, job)
        except Exception as exc:
            # Anything unexpected outside the call itself. Nothing is known to
            # have been sent, but nothing is known NOT to have been sent
            # either, so this is indeterminate.
            logger.error('Equity placement worker failed for account %s: %s', account_id, exc)
            outcome = _blank_outcome()
            outcome['fill_status'] = EQUITY_SPLIT_STATUS_INDETERMINATE
            outcome['indeterminate'] = True
            outcome['error_type'] = 'worker_error'
            outcome['error_message'] = 'Placement failed unexpectedly: %s' % exc
            outcome['attempts'] = 1

        outcome['account_id'] = account_id
        return outcome


def _run_jobs(app, jobs, worker, max_workers=None):
    """
    Run one worker per account concurrently and collect every result.

    A single job runs inline, so the common one account case costs no thread.
    The pool is bounded, each worker is already wrapped in try/except, and a
    worker that still manages to raise is recorded as a failed account rather
    than being allowed to break the other accounts.
    """
    if not jobs:
        return []
    if len(jobs) == 1:
        return [worker(app, jobs[0])]

    bound = min(max_workers or MAX_ORDER_WORKERS, MAX_ORDER_WORKERS, len(jobs))
    results = []
    with ThreadPoolExecutor(max_workers=bound) as executor:
        futures = [(job, executor.submit(worker, app, job)) for job in jobs]
        for job, future in futures:
            try:
                results.append(future.result())
            except Exception as exc:
                account_id = job['credential'].get('account_id')
                logger.error('Equity worker crashed for account %s: %s', account_id, exc)
                outcome = _blank_outcome()
                outcome['fill_status'] = EQUITY_SPLIT_STATUS_INDETERMINATE
                outcome['indeterminate'] = True
                outcome['error_type'] = 'worker_crash'
                outcome['error_message'] = 'Worker crashed: %s' % exc
                outcome['account_id'] = account_id
                results.append(outcome)
    return results


# ---------------------------------------------------------------------------
# Accounts, allocations and the split plan
# ---------------------------------------------------------------------------

def _load_accounts(user_id, account_ids):
    """
    Load the selected accounts, ownership scoped, in a stable order.

    An id the user does not own, or an inactive account, aborts the whole
    instruction before anything is created. A selection we cannot honour
    exactly is a confused caller, and guessing at what was meant is how an
    unintended order gets placed.
    """
    wanted = []
    for raw in account_ids or []:
        account_id = _to_int(raw, 0)
        if account_id > 0 and account_id not in wanted:
            wanted.append(account_id)
    # Sorted so the split is decided by the accounts themselves and not by the
    # order the checkboxes happened to arrive in. split_quantity_by_ratio
    # serves in insertion order when the quantity runs short, and that has to
    # be reproducible.
    wanted.sort()

    if not wanted:
        raise EquityOrderError('Select at least one account')

    rows = TradingAccount.query.filter(
        TradingAccount.user_id == user_id,
        TradingAccount.id.in_(wanted)
    ).all()
    by_id = {row.id: row for row in rows}

    missing = [account_id for account_id in wanted if account_id not in by_id]
    if missing:
        raise EquityOrderError(
            'Account %s is not available for this user'
            % ', '.join(str(account_id) for account_id in missing)
        )

    inactive = [by_id[account_id] for account_id in wanted if not by_id[account_id].is_active]
    if inactive:
        raise EquityOrderError(
            'Account %s is inactive and cannot be traded'
            % ', '.join(account.account_name for account in inactive)
        )

    return [by_id[account_id] for account_id in wanted]


def _allocation_amounts(user_id, accounts):
    """
    Rupee allocation per selected account, in selection order.

    A deactivated allocation row counts as zero, which gives that account no
    quantity from the ratio and lands it as SKIPPED with a reason, rather than
    silently trading an account the admin took out of the equity module.
    """
    account_ids = [account.id for account in accounts]
    rows = EquityAccountAllocation.query.filter(
        EquityAccountAllocation.user_id == user_id,
        EquityAccountAllocation.account_id.in_(account_ids)
    ).all()
    by_account = {row.account_id: row for row in rows}

    amounts = OrderedDict()
    for account in accounts:
        row = by_account.get(account.id)
        if row is None or row.is_active is False:
            amounts[account.id] = 0.0
        else:
            amounts[account.id] = _to_float(row.equity_fund_allocation, 0.0) or 0.0
    return amounts


def _normalise_price_fields(order_type, price, trigger_price):
    """
    Validate the order type and the prices that go with it.

    Shared by placement and by exit_holding, so a MARKET exit and a MARKET
    entry cannot disagree about what a valid instruction looks like.

    Returns (order_type, price, trigger_price) with the fields that do not
    apply to this order type cleared to None.
    """
    order_type = str(order_type or '').strip().upper()
    if order_type not in _VALID_ORDER_TYPES:
        raise EquityOrderError('Order type must be one of %s' % ', '.join(_VALID_ORDER_TYPES))

    limit_price = _to_float(price)
    trigger = _to_float(trigger_price)

    if order_type == EQUITY_ORDER_TYPE_MARKET:
        return order_type, None, None

    if order_type == EQUITY_ORDER_TYPE_LIMIT:
        if limit_price is None or limit_price <= 0:
            raise EquityOrderError('A LIMIT order needs a price')
        return order_type, limit_price, None

    if trigger is None or trigger <= 0:
        raise EquityOrderError('A GTT order needs a trigger price')
    if limit_price is None or limit_price <= 0:
        raise EquityOrderError('A GTT order needs a limit price')
    return order_type, limit_price, trigger


def _normalise_instruction(symbol, exchange, side, order_type, price, trigger_price,
                           total_quantity):
    """
    Validate and normalise one equity instruction.

    Raises EquityOrderError for anything that cannot be traded, before any row
    is written and long before any broker is called.
    """
    symbol = str(symbol or '').strip().upper()
    if not symbol:
        raise EquityOrderError('Symbol is required')

    exchange = str(exchange or '').strip().upper()
    if not exchange:
        raise EquityOrderError('Exchange is required')

    side = str(side or '').strip().upper()
    if side not in _VALID_SIDES:
        raise EquityOrderError('Side must be %s or %s' % _VALID_SIDES)

    quantity = _to_int(total_quantity, 0)
    if quantity <= 0:
        raise EquityOrderError('Total quantity must be a positive whole number')

    order_type, limit_price, trigger = _normalise_price_fields(
        order_type, price, trigger_price
    )
    return symbol, exchange, side, order_type, limit_price, trigger, quantity


def _reference_price_for(order_type, price, reference_price):
    """
    The price used for Est. Value and for the cash check.

    A LIMIT or GTT order has its own price. A MARKET order has none, so the
    caller supplies the live LTP. Without either, the value of the order is
    unknown and the funds check cannot run, which is recorded rather than
    guessed.
    """
    if order_type in (EQUITY_ORDER_TYPE_LIMIT, EQUITY_ORDER_TYPE_GTT):
        return price
    value = _to_float(reference_price)
    return value if value and value > 0 else None


def _credential_for(account, timeout=None):
    """
    Plain credential dict for one account, built on the calling thread.

    Rule 8: no ORM object and no lazy load ever crosses a thread boundary, so
    the API key is decrypted here and the dict is what the worker receives.
    The dict never leaves this module and is never logged.
    """
    try:
        api_key = account.get_api_key()
    except Exception as exc:
        logger.error('Could not read the API key for account %s: %s', account.id, exc)
        api_key = None

    return {
        'account_id': account.id,
        'account_name': account.account_name,
        'api_key': api_key,
        'host_url': account.host_url,
        'timeout': timeout or BROKER_ORDER_TIMEOUT_SECONDS,
    }


def _funds_worker(app, job):
    """Read one account's cash. A read only call, never a write."""
    account_id = job['credential'].get('account_id')
    with app.app_context():
        try:
            client = _resolve_factory(job.get('client_factory'))(job['credential'])
            response = client.funds()
        except Exception as exc:
            logger.warning('Equity funds read failed for account %s: %s', account_id, exc)
            return {'account_id': account_id, 'cash': None}
        return {'account_id': account_id, 'cash': _cash_from_funds(response)}


def _fetch_cash_balances(credentials, client_factory=None, max_workers=None):
    """
    Read available cash for every account concurrently.

    An account whose funds cannot be read comes back as None, which means the
    cash check did not run for it. It is NOT treated as an empty account.
    """
    app = current_app._get_current_object()
    jobs = [
        {
            'credential': dict(credential, timeout=BROKER_FUNDS_TIMEOUT_SECONDS),
            'client_factory': client_factory,
        }
        for credential in credentials
    ]
    results = _run_jobs(app, jobs, _funds_worker, max_workers=max_workers)
    return {result['account_id']: result.get('cash') for result in results}


def _build_split_plan(user_id, accounts, total_quantity, quantity_overrides,
                      order_type, price, side, reference_price, cash_balances,
                      client_factory, max_workers):
    """
    Work out what each account would be sent, and whether it can afford it.

    Ratios are computed across the accounts PARTICIPATING IN THIS ORDER, not
    across every active account. With every account ticked the two are the same
    number. With a subset ticked, using the whole set would leave most of the
    quantity unallocated, so the participating set is what actually decides the
    split and therefore what is recorded as qty_ratio_at_order.

    Returns (rows, credentials, meta). Credentials carry the API keys and stay
    inside this module. Rows are safe to hand to a caller or a template.
    """
    allocations = _allocation_amounts(user_id, accounts)
    ratios = compute_order_qty_ratios(allocations)
    split = split_quantity_by_ratio(total_quantity, ratios)

    overrides = {}
    for raw_account_id, raw_quantity in (quantity_overrides or {}).items():
        account_id = _to_int(raw_account_id, 0)
        quantity = _to_int(raw_quantity, -1)
        if quantity < 0:
            raise EquityOrderError('A quantity override cannot be negative')
        overrides[account_id] = quantity

    unknown = set(overrides) - {account.id for account in accounts}
    if unknown:
        raise EquityOrderError(
            'Quantity override given for account %s, which is not part of this order'
            % ', '.join(str(account_id) for account_id in sorted(unknown))
        )

    credentials = [_credential_for(account) for account in accounts]

    quantities = OrderedDict()
    for account in accounts:
        ratio_quantity = _to_int(split.quantities.get(account.id), 0)
        quantities[account.id] = overrides.get(account.id, ratio_quantity)

    allocated = sum(quantities.values())
    if allocated > total_quantity:
        raise EquityOrderError(
            'The per-account quantities add up to %d, which is more than the '
            'total quantity of %d. Lower an override or raise the total.'
            % (allocated, total_quantity)
        )

    unit_price = _reference_price_for(order_type, price, reference_price)

    # Cash only matters for a BUY. A SELL releases cash rather than using it.
    needs_cash_check = side == EQUITY_SIDE_BUY and unit_price is not None
    if needs_cash_check and cash_balances is None:
        cash_balances = _fetch_cash_balances(
            credentials, client_factory=client_factory, max_workers=max_workers
        )
    cash_balances = cash_balances or {}

    rows = []
    for account, credential in zip(accounts, credentials):
        quantity = quantities[account.id]
        ratio_quantity = _to_int(split.quantities.get(account.id), 0)
        cash = _to_float(cash_balances.get(account.id))
        required = round(quantity * unit_price, 2) if unit_price is not None else None
        funds_checked = bool(needs_cash_check and quantity > 0 and cash is not None)

        ok = True
        reason = None

        if quantity <= 0:
            ok = False
            if allocations.get(account.id, 0.0) <= 0:
                reason = 'No equity allocation on this account, so the ratio gives it no quantity'
            else:
                reason = 'The ratio gives this account no whole share of the total quantity'
        elif credential.get('api_key') is None:
            ok = False
            reason = 'The API key for this account could not be read'
        elif funds_checked and required is not None and required > cash:
            ok = False
            reason = (
                'Cash %.2f is short of the %.2f this account needs for %d shares'
                % (cash, required, quantity)
            )

        rows.append({
            'account_id': account.id,
            'account_name': account.account_name,
            'qty_ratio': ratios.get(account.id, 0.0),
            'ratio_quantity': ratio_quantity,
            'quantity': quantity,
            'qty_overridden': account.id in overrides and overrides[account.id] != ratio_quantity,
            'est_value': required,
            'cash_balance': cash,
            'funds_checked': funds_checked,
            'required_cash': required,
            'check_ok': ok,
            'check_reason': reason,
        })

    meta = {
        'ratio_leftover': _to_int(split.leftover, 0),
        'leftover_quantity': max(total_quantity - allocated, 0),
        'allocated_quantity': allocated,
        'reference_price': unit_price,
        'total_est_value': round(
            sum(row['est_value'] or 0.0 for row in rows), 2
        ) if unit_price is not None else None,
    }
    return rows, {credential['account_id']: credential for credential in credentials}, meta


def _resolve_funds_action(user_id, insufficient_funds_action):
    """
    The insufficient funds policy in force for this order.

    Snapshotted onto the order so that changing the setting later cannot
    rewrite the history of what this order actually did.
    """
    if insufficient_funds_action:
        action = str(insufficient_funds_action).strip().upper()
        if action in (EQUITY_FUNDS_ACTION_SKIP, EQUITY_FUNDS_ACTION_ABORT):
            return action
        raise EquityOrderError(
            'Insufficient funds action must be %s or %s'
            % (EQUITY_FUNDS_ACTION_SKIP, EQUITY_FUNDS_ACTION_ABORT)
        )

    setting = EquitySetting.get_or_create(user_id)
    return setting.insufficient_funds_action if setting else EQUITY_FUNDS_ACTION_SKIP


# ---------------------------------------------------------------------------
# Parent status roll-up
# ---------------------------------------------------------------------------

def summarise_splits(splits):
    """
    Count the splits of one order and describe the outcome in words.

    Args:
        splits: iterable of EquityOrderSplit

    Returns:
        dict with total, open, filled, cancelled, failed, skipped,
        unsupported, indeterminate and a short reason string such as
        "1 failed", which is what the Order Status screen shows next to
        PARTIAL.
    """
    rows = list(splits or [])
    counts = {
        'total': len(rows),
        'open': 0,
        'filled': 0,
        'cancelled': 0,
        'failed': 0,
        'skipped': 0,
        'unsupported': 0,
        'indeterminate': 0,
    }

    for split in rows:
        status = split.fill_status
        if status in EQUITY_SPLIT_STATUSES_OPEN:
            counts['open'] += 1
        elif status == EQUITY_SPLIT_STATUS_COMPLETED:
            counts['filled'] += 1
        elif status == EQUITY_SPLIT_STATUS_CANCELLED:
            counts['cancelled'] += 1
        elif status == EQUITY_SPLIT_STATUS_SKIPPED:
            counts['skipped'] += 1
        elif status == EQUITY_SPLIT_STATUS_UNSUPPORTED:
            counts['unsupported'] += 1
        elif status == EQUITY_SPLIT_STATUS_INDETERMINATE:
            counts['indeterminate'] += 1
        else:
            counts['failed'] += 1

    parts = []
    if counts['failed']:
        parts.append('%d failed' % counts['failed'])
    if counts['indeterminate']:
        parts.append('%d unconfirmed' % counts['indeterminate'])
    if counts['unsupported']:
        parts.append('%d not supported' % counts['unsupported'])
    if counts['skipped']:
        parts.append('%d skipped' % counts['skipped'])
    if counts['cancelled']:
        parts.append('%d cancelled' % counts['cancelled'])
    counts['reason'] = ', '.join(parts)
    return counts


def _rollup_status(counts, current_status):
    """
    The parent status implied by the split counts.

        every split filled                          COMPLETED
        nothing filled and every split cancelled
        or never sent                               CANCELLED
        at least one still open, and no account
        has diverged from the others                PENDING
        anything else                               PARTIAL

    PENDING means "all of this order is still working". The moment one account
    fails while another is live or filled, the order is PARTIAL, even though
    the others have not finished: PRD M4b wants a failed account to surface on
    the Order Status screen straight away ("PARTIAL, 1 failed"), not once the
    remaining accounts happen to fill.

    PARTIAL is also the honest answer when every account failed. The order was
    not cancelled, it simply did not work, and CANCELLED would claim somebody
    pulled it.
    """
    if counts['total'] == 0:
        return current_status
    if counts['filled'] == counts['total']:
        return EQUITY_ORDER_STATUS_COMPLETED
    if counts['filled'] == 0 and (counts['cancelled'] + counts['skipped']) == counts['total']:
        return EQUITY_ORDER_STATUS_CANCELLED

    diverged = (
        counts['failed'] + counts['indeterminate'] + counts['unsupported']
        + counts['skipped'] + counts['cancelled']
    )
    if counts['open'] > 0 and diverged == 0:
        return EQUITY_ORDER_STATUS_PENDING
    return EQUITY_ORDER_STATUS_PARTIAL


def recompute_parent_status(order_id, user_id, commit=True):
    """
    Recompute one parent order's status from its splits and store it.

    Safe to call from anywhere that changes a split, including an order status
    poller written later. Ownership scoped, so an order id alone cannot reach
    another user's order.

    Returns:
        The stored status string, or None when the order does not exist for
        this user.
    """
    order = EquityOrder.query.filter_by(id=order_id, user_id=user_id).first()
    if order is None:
        return None

    counts = summarise_splits(order.splits.all())
    status = _rollup_status(counts, order.status)

    if status != order.status:
        order.status = status
        if status == EQUITY_ORDER_STATUS_CANCELLED and order.cancelled_at is None:
            order.cancelled_at = _now()
    if commit:
        db.session.commit()
    return status


# ---------------------------------------------------------------------------
# Public: preview
# ---------------------------------------------------------------------------

def preview_order_split(user_id, symbol, exchange, side, total_quantity,
                        order_type=EQUITY_ORDER_TYPE_MARKET, price=None,
                        trigger_price=None, account_ids=None,
                        quantity_overrides=None, reference_price=None,
                        cash_balances=None, insufficient_funds_action=None,
                        client_factory=None, max_workers=None):
    """
    Work out the account-wise order split WITHOUT touching a broker or writing
    a row. This is what fills the Place Order split table, including the Check
    column that flags an account whose cash cannot cover its share.

    It shares _build_split_plan with place_multi_account_order, so what the
    admin is shown before submitting and what is actually sent cannot drift
    apart.

    Returns:
        dict with status ('success' or 'error'), message, rows (one per
        account, described in _build_split_plan), leftover_quantity,
        ratio_leftover, allocated_quantity, total_est_value, accounts_ok,
        accounts_flagged and the insufficient funds action that would apply.
    """
    try:
        (symbol, exchange, side, order_type, price, trigger_price,
         total_quantity) = _normalise_instruction(
            symbol, exchange, side, order_type, price, trigger_price, total_quantity
        )
        accounts = _load_accounts(user_id, account_ids)
        funds_action = _resolve_funds_action(user_id, insufficient_funds_action)
        rows, _credentials, meta = _build_split_plan(
            user_id=user_id,
            accounts=accounts,
            total_quantity=total_quantity,
            quantity_overrides=quantity_overrides,
            order_type=order_type,
            price=price,
            side=side,
            reference_price=reference_price,
            cash_balances=cash_balances,
            client_factory=client_factory,
            max_workers=max_workers,
        )
    except EquityOrderError as exc:
        return {'status': 'error', 'message': str(exc), 'rows': []}

    flagged = [row for row in rows if not row['check_ok']]
    return {
        'status': 'success',
        'message': '',
        'symbol': symbol,
        'exchange': exchange,
        'side': side,
        'order_type': order_type,
        'product': EQUITY_PRODUCT_CNC,
        'price': price,
        'trigger_price': trigger_price,
        'total_quantity': total_quantity,
        'rows': rows,
        'leftover_quantity': meta['leftover_quantity'],
        'ratio_leftover': meta['ratio_leftover'],
        'allocated_quantity': meta['allocated_quantity'],
        'reference_price': meta['reference_price'],
        'total_est_value': meta['total_est_value'],
        'accounts_selected': len(rows),
        'accounts_ok': len(rows) - len(flagged),
        'accounts_flagged': len(flagged),
        'insufficient_funds_action': funds_action,
    }


# ---------------------------------------------------------------------------
# Public: place
# ---------------------------------------------------------------------------

def _create_parent_order(user_id, symbol, exchange, side, order_type, price,
                         trigger_price, total_quantity, stop_loss, target,
                         trade_nature_id, source, leftover_quantity, funds_action):
    """Create and COMMIT the parent order before any broker is called."""
    order = EquityOrder(
        user_id=user_id,
        symbol=symbol,
        exchange=exchange,
        side=side,
        order_type=order_type,
        product=EQUITY_PRODUCT_CNC,
        total_quantity=total_quantity,
        price=price,
        trigger_price=trigger_price,
        stop_loss=_to_float(stop_loss),
        target=_to_float(target),
        trade_nature_id=trade_nature_id,
        source=source,
        leftover_quantity=leftover_quantity,
        insufficient_funds_action=funds_action,
        status=EQUITY_ORDER_STATUS_PENDING,
        placed_at=_now(),
    )
    db.session.add(order)
    db.session.commit()
    return order


def _create_split(order, row, fill_status=EQUITY_SPLIT_STATUS_PENDING,
                  error_message=None, sent=True):
    """
    Create one account's split with its point in time snapshot.

    qty_ratio_at_order, ratio_quantity and cash_balance_at_order are written
    here and are never touched again, which is PRD section 9.1.

    quantity is what was actually SENT, so an account that never reached the
    broker records zero. What it would have been is still visible: the ratio
    kept it in ratio_quantity and the reason is on error_message.
    """
    split = EquityOrderSplit(
        equity_order_id=order.id,
        account_id=row['account_id'],
        qty_ratio_at_order=row['qty_ratio'],
        quantity=row['quantity'] if sent else 0,
        ratio_quantity=row['ratio_quantity'],
        qty_overridden=bool(row['qty_overridden']),
        est_value=row['est_value'] if sent else None,
        cash_balance_at_order=row['cash_balance'],
        fill_status=fill_status,
        filled_quantity=0,
        error_message=_clip(error_message),
        attempt_count=0,
    )
    db.session.add(split)
    return split


def _apply_outcome(split, outcome):
    """Write one broker outcome onto its split. Called on the main thread only."""
    split.fill_status = outcome['fill_status']
    split.attempt_count = _to_int(outcome.get('attempts'), 0)
    split.error_message = _clip(outcome.get('error_message'))
    split.error_type = outcome.get('error_type')
    split.broker_order_status = outcome.get('broker_order_status')
    if outcome.get('broker_order_id'):
        split.broker_order_id = outcome['broker_order_id']
    if outcome.get('broker_gtt_id'):
        split.broker_gtt_id = outcome['broker_gtt_id']
    if outcome.get('placed_at'):
        split.placed_at = outcome['placed_at']
    split.last_synced_at = _now()


def _persist_outcomes(order, splits_by_account, outcomes):
    """
    Write the fan-out results onto their splits.

    The orders are already at the broker by the time this runs, so losing this
    write means AlgoMirror does not know about real orders. One bulk commit is
    tried first; if it fails, each split is retried on its own so that one bad
    row cannot take the others down with it, and anything still unwritten is
    logged at CRITICAL with its broker order id so it can be recovered from the
    log alone.
    """
    pending = []
    for outcome in outcomes:
        split = splits_by_account.get(outcome.get('account_id'))
        if split is None:
            logger.error(
                'Equity order %s got a result for account %s that has no split',
                order.id, outcome.get('account_id')
            )
            continue
        _apply_outcome(split, outcome)
        pending.append((split, outcome))

    try:
        db.session.commit()
        return
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'Equity order %s could not record its results in one write: %s', order.id, exc
        )

    for split, outcome in pending:
        try:
            fresh = EquityOrderSplit.query.get(split.id)
            if fresh is None:
                raise LookupError('split %s disappeared' % split.id)
            _apply_outcome(fresh, outcome)
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.critical(
                'EQUITY ORDER NOT RECORDED. Order %s account %s broker order %s '
                'gtt %s status %s could not be written: %s',
                order.id, outcome.get('account_id'), outcome.get('broker_order_id'),
                outcome.get('broker_gtt_id'), outcome.get('fill_status'), exc
            )


def _split_view(split, account_name=None):
    """Serialisable view of one split. Never carries a credential."""
    return {
        'split_id': split.id,
        'account_id': split.account_id,
        'account_name': account_name,
        'quantity': _to_int(split.quantity, 0),
        'ratio_quantity': _to_int(split.ratio_quantity, 0),
        'qty_ratio': _to_float(split.qty_ratio_at_order, 0.0),
        'qty_overridden': bool(split.qty_overridden),
        'est_value': _to_float(split.est_value),
        'cash_balance': _to_float(split.cash_balance_at_order),
        'fill_status': split.fill_status,
        'broker_order_id': split.broker_order_id,
        'broker_gtt_id': split.broker_gtt_id,
        'error_message': split.error_message,
        'error_type': split.error_type,
        'attempt_count': _to_int(split.attempt_count, 0),
        'placed_at': split.placed_at.isoformat() if split.placed_at else None,
    }


def place_multi_account_order(user_id, symbol, exchange, side, total_quantity,
                              order_type=EQUITY_ORDER_TYPE_MARKET, price=None,
                              trigger_price=None, stop_loss=None, target=None,
                              account_ids=None, quantity_overrides=None,
                              trade_nature_id=None,
                              source=EQUITY_ORDER_SOURCE_MANUAL,
                              reference_price=None, cash_balances=None,
                              insufficient_funds_action=None,
                              client_factory=None,
                              strategy_name=DEFAULT_STRATEGY_NAME,
                              gtt_trigger_leg=None,
                              max_attempts=DEFAULT_MAX_ATTEMPTS,
                              retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS,
                              sleeper=None, max_workers=None):
    """
    Place one equity instruction across several accounts at once.

    The sequence is deliberate. The parent order and every split are created
    and COMMITTED before a single broker is called, so a crash mid fan-out
    leaves a record of what was being attempted instead of silence. Broker
    calls then run concurrently, one worker per account, and the results are
    written back on this thread in one transaction.

    Product is always CNC. Stop loss and target are recorded on the order for
    AlgoMirror's own monitor and are NOT sent to the broker.

    Args:
        user_id: owner. Every query in here is scoped to it.
        symbol, exchange: the instrument, upper cased.
        side: EQUITY_SIDE_BUY or EQUITY_SIDE_SELL.
        total_quantity: total shares across all selected accounts.
        order_type: EQUITY_ORDER_TYPE_MARKET, _LIMIT or _GTT.
        price: limit price. Required for LIMIT and for GTT.
        trigger_price: GTT trigger. Required for GTT, ignored otherwise.
        stop_loss, target: AlgoMirror side levels stored on the order.
        account_ids: the ticked accounts. Required, never defaulted to all.
        quantity_overrides: {account_id: quantity} to override the ratio for
            those accounts. The overridden totals may not exceed total_quantity.
        trade_nature_id: nature carried from the Watch List, optional.
        source: EQUITY_ORDER_SOURCE_MANUAL by default.
        reference_price: live LTP, used for Est. Value and the cash check on a
            MARKET order. Without it a MARKET order's funds check cannot run
            and is recorded as not performed.
        cash_balances: {account_id: cash} when the caller already has funds in
            hand, which skips the funds read entirely. None fetches them.
        insufficient_funds_action: EQUITY_FUNDS_ACTION_SKIP (default from
            settings) or _ABORT.
        client_factory: the broker seam. See the module docstring.
        strategy_name: strategy tag sent to OpenAlgo.
        gtt_trigger_leg: GTT_LEG_STOP_LOSS or GTT_LEG_TARGET to override which
            SINGLE GTT slot carries the trigger.
        max_attempts: placement attempts per account. Only a DEFINITE refusal
            is ever re-sent.
        retry_delay_seconds, sleeper: retry pacing, injectable for tests.
        max_workers: pool bound, capped at MAX_ORDER_WORKERS.

    Returns:
        dict with:
            status              'success' when every selected account got its
                                order, 'partial' when some did and some did
                                not, 'error' when none did or the instruction
                                was refused before anything was created.
            message             one line describing the outcome.
            order_id            EquityOrder.id, or None when nothing was created.
            parent_status       PENDING, PARTIAL, COMPLETED or CANCELLED.
            symbol, exchange, side, order_type, product, price, trigger_price
            total_quantity, placed_quantity, leftover_quantity, ratio_leftover
            accounts_selected, accounts_placed, accounts_failed,
            accounts_skipped, accounts_indeterminate, accounts_unsupported
            splits              one _split_view per account.
    """
    try:
        (symbol, exchange, side, order_type, price, trigger_price,
         total_quantity) = _normalise_instruction(
            symbol, exchange, side, order_type, price, trigger_price, total_quantity
        )
        accounts = _load_accounts(user_id, account_ids)
        funds_action = _resolve_funds_action(user_id, insufficient_funds_action)
        rows, credentials, meta = _build_split_plan(
            user_id=user_id,
            accounts=accounts,
            total_quantity=total_quantity,
            quantity_overrides=quantity_overrides,
            order_type=order_type,
            price=price,
            side=side,
            reference_price=reference_price,
            cash_balances=cash_balances,
            client_factory=client_factory,
            max_workers=max_workers,
        )
    except EquityOrderError as exc:
        return {
            'status': 'error',
            'message': str(exc),
            'order_id': None,
            'parent_status': None,
            'splits': [],
        }

    account_names = {account.id: account.account_name for account in accounts}
    blocked = [row for row in rows if not row['check_ok']]
    abort = bool(blocked) and funds_action == EQUITY_FUNDS_ACTION_ABORT

    order = _create_parent_order(
        user_id=user_id, symbol=symbol, exchange=exchange, side=side,
        order_type=order_type, price=price, trigger_price=trigger_price,
        total_quantity=total_quantity, stop_loss=stop_loss, target=target,
        trade_nature_id=trade_nature_id, source=source,
        leftover_quantity=meta['leftover_quantity'], funds_action=funds_action,
    )

    # ABORT: every account is recorded as skipped and nothing is sent anywhere.
    if abort:
        first_reason = blocked[0]['check_reason']
        message = (
            'Aborted before any order was placed: %d of %d accounts failed the '
            'pre-trade check (%s)' % (len(blocked), len(rows), first_reason)
        )
        for row in rows:
            _create_split(
                order, row,
                fill_status=EQUITY_SPLIT_STATUS_SKIPPED,
                error_message=row['check_reason'] or 'Aborted with the rest of the order',
                sent=False,
            )
        order.status = EQUITY_ORDER_STATUS_CANCELLED
        order.cancelled_at = _now()
        order.error_message = _clip(message)
        db.session.commit()
        logger.warning('Equity order %s aborted on the funds policy: %s', order.id, message)
        return _place_result(order, account_names, message, meta)

    # SKIP: the blocked accounts are recorded and every other account proceeds.
    splits_by_account = {}
    jobs = []
    for row in rows:
        if not row['check_ok']:
            splits_by_account[row['account_id']] = _create_split(
                order, row,
                fill_status=EQUITY_SPLIT_STATUS_SKIPPED,
                error_message=row['check_reason'],
                sent=False,
            )
            continue
        splits_by_account[row['account_id']] = _create_split(order, row)
        jobs.append({
            'credential': credentials[row['account_id']],
            'client_factory': client_factory,
            'symbol': symbol,
            'exchange': exchange,
            'side': side,
            'order_type': order_type,
            'price': price,
            'trigger_price': trigger_price,
            'quantity': row['quantity'],
            'strategy_name': strategy_name,
            'gtt_trigger_leg': gtt_trigger_leg,
            'max_attempts': max_attempts,
            'retry_delay_seconds': retry_delay_seconds,
            'sleeper': sleeper,
        })

    # The whole plan is on disk before the first broker call.
    db.session.commit()

    app = current_app._get_current_object()
    outcomes = _run_jobs(app, jobs, _placement_worker, max_workers=max_workers)

    _persist_outcomes(order, splits_by_account, outcomes)
    recompute_parent_status(order.id, user_id)
    message = _place_message(order, rows, outcomes)
    logger.info(
        'Equity order %s %s %s x%d across %d accounts: %s',
        order.id, side, symbol, total_quantity, len(rows), message
    )
    return _place_result(order, account_names, message, meta)


def _place_message(order, rows, outcomes):
    """One line describing what the fan-out achieved."""
    placed = sum(1 for outcome in outcomes if outcome.get('ok'))
    skipped = sum(1 for row in rows if not row['check_ok'])
    failed = len(outcomes) - placed
    parts = ['%d of %d accounts placed' % (placed, len(rows))]
    if failed:
        parts.append('%d failed' % failed)
    if skipped:
        parts.append('%d skipped on the pre-trade check' % skipped)
    if order.leftover_quantity:
        parts.append('%d shares left over after rounding down' % order.leftover_quantity)
    return ', '.join(parts)


def _place_result(order, account_names, message, meta):
    """Assemble the public result from the committed rows."""
    splits = order.splits.order_by(EquityOrderSplit.account_id).all()
    counts = summarise_splits(splits)
    placed = sum(
        1 for split in splits
        if split.broker_order_id or split.broker_gtt_id
    )
    placed_quantity = sum(
        _to_int(split.quantity, 0) for split in splits
        if split.broker_order_id or split.broker_gtt_id
    )

    if placed == 0:
        status = 'error'
    elif placed < len(splits):
        status = 'partial'
    else:
        status = 'success'

    return {
        'status': status,
        'message': message,
        'order_id': order.id,
        'parent_status': order.status,
        'symbol': order.symbol,
        'exchange': order.exchange,
        'side': order.side,
        'order_type': order.order_type,
        'product': order.product,
        'price': order.price,
        'trigger_price': order.trigger_price,
        'source': order.source,
        'total_quantity': _to_int(order.total_quantity, 0),
        'placed_quantity': placed_quantity,
        'leftover_quantity': _to_int(order.leftover_quantity, 0),
        'ratio_leftover': meta.get('ratio_leftover', 0) if meta else 0,
        'insufficient_funds_action': order.insufficient_funds_action,
        'error_message': order.error_message,
        'accounts_selected': len(splits),
        'accounts_placed': placed,
        'accounts_failed': counts['failed'] + counts['indeterminate'] + counts['unsupported'],
        'accounts_skipped': counts['skipped'],
        'accounts_indeterminate': counts['indeterminate'],
        'accounts_unsupported': counts['unsupported'],
        'counts': counts,
        'splits': [_split_view(split, account_names.get(split.account_id)) for split in splits],
    }


# ---------------------------------------------------------------------------
# Public: modify and cancel
# ---------------------------------------------------------------------------

def _open_splits(order):
    """
    The splits of an order that are still working at the broker AND that we can
    name. A split with no broker id has nothing to modify or cancel.
    """
    return [
        split for split in order.splits.order_by(EquityOrderSplit.account_id).all()
        if split.is_open and (split.broker_order_id or split.broker_gtt_id)
    ]


def _amend_worker(app, job):
    """
    One modify or cancel against one account.

    An indeterminate answer here is NOT written onto the split's status. A
    cancel that timed out may or may not have cancelled, and marking the split
    CANCELLED would lose a live order while marking it INDETERMINATE would lose
    a working one. The split keeps its current status and the reason is
    recorded for reconciliation.
    """
    account_id = job['credential'].get('account_id')
    result = {
        'account_id': account_id,
        'ok': False,
        'indeterminate': False,
        'unsupported': False,
        'error_message': None,
        'error_type': None,
        'action': job['action'],
    }

    with app.app_context():
        try:
            client = _resolve_factory(job.get('client_factory'))(job['credential'])
        except Exception as exc:
            result['error_message'] = 'Could not open a broker connection: %s' % exc
            result['error_type'] = 'client_error'
            return result

        try:
            response = job['call'](client, job)
        except Exception as exc:
            logger.error(
                'Equity %s raised for account %s: %s', job['action'], account_id, exc
            )
            response = None

        if equity_is_indeterminate_response(response):
            result['indeterminate'] = True
            result['error_type'] = _response_error_type(response)
            result['error_message'] = _response_message(
                response,
                'No answer from the broker. The order state at the broker is unknown.'
            )
            return result

        if job.get('is_gtt') and _is_gtt_unsupported(response):
            result['unsupported'] = True
            result['error_type'] = _response_error_type(response)
            result['error_message'] = 'This broker does not support GTT orders'
            return result

        if _is_success(response):
            result['ok'] = True
            return result

        result['error_type'] = _response_error_type(response)
        result['error_message'] = _response_message(response, 'Broker refused the request')
        return result


def _call_modify(client, job):
    """The modify call for one split, GTT aware."""
    if job.get('is_gtt'):
        payload = _gtt_payload(job, job['credential'].get('api_key'), trigger_id=job['trigger_id'])
        return _call_endpoint(client, GTT_MODIFY_ENDPOINT, payload)
    return client.modifyorder(
        order_id=job['broker_order_id'],
        strategy=job['strategy_name'],
        symbol=job['symbol'],
        action=job['side'],
        exchange=job['exchange'],
        price_type=job['order_type'],
        product=EQUITY_PRODUCT_CNC,
        quantity=job['quantity'],
        price=job['price'] if job['price'] is not None else 0,
    )


def _call_cancel(client, job):
    """The cancel call for one split, GTT aware."""
    if job.get('is_gtt'):
        payload = {
            'apikey': job['credential'].get('api_key'),
            'strategy': job['strategy_name'],
            'trigger_id': str(job['trigger_id']),
        }
        return _call_endpoint(client, GTT_CANCEL_ENDPOINT, payload)
    return client.cancelorder(
        order_id=job['broker_order_id'],
        strategy=job['strategy_name'],
    )


def _load_open_order(user_id, order_id):
    """An order that is still modifiable, ownership scoped."""
    order = EquityOrder.query.filter_by(id=order_id, user_id=user_id).first()
    if order is None:
        raise EquityOrderError('Order %s is not available for this user' % order_id)
    if not order.is_open:
        raise EquityOrderError(
            'Order %s is %s. Only a PENDING or PARTIAL order can be changed.'
            % (order_id, order.status)
        )
    return order


def _amend_jobs(order, splits, action, call, strategy_name, client_factory,
                new_quantities=None, price=None, order_type=None, trigger_price=None):
    """Build one amend job per open split, credentials extracted up front."""
    accounts = {
        account.id: account for account in TradingAccount.query.filter(
            TradingAccount.user_id == order.user_id,
            TradingAccount.id.in_([split.account_id for split in splits])
        ).all()
    }

    jobs = []
    for split in splits:
        account = accounts.get(split.account_id)
        if account is None:
            continue
        is_gtt = bool(split.broker_gtt_id) and not split.broker_order_id
        jobs.append({
            'credential': _credential_for(account),
            'client_factory': client_factory,
            'action': action,
            'call': call,
            'is_gtt': is_gtt,
            'split_id': split.id,
            'broker_order_id': split.broker_order_id,
            'trigger_id': split.broker_gtt_id,
            'symbol': order.symbol,
            'exchange': order.exchange,
            'side': order.side,
            'order_type': order_type or order.order_type,
            'price': price if price is not None else order.price,
            'trigger_price': trigger_price if trigger_price is not None else order.trigger_price,
            'quantity': (new_quantities or {}).get(split.account_id, _to_int(split.quantity, 0)),
            'strategy_name': strategy_name,
            'gtt_trigger_leg': None,
        })
    return jobs


def modify_order(user_id, order_id, price=None, trigger_price=None,
                 total_quantity=None, quantity_overrides=None, account_ids=None,
                 client_factory=None, strategy_name=DEFAULT_STRATEGY_NAME,
                 max_workers=None):
    """
    Modify an order that is still PENDING or PARTIAL, account by account,
    concurrently, with the same failure isolation as placement.

    Quantity changes are re-split using the ratio ALREADY RECORDED on each
    split (qty_ratio_at_order), never by recomputing today's allocations: the
    snapshot is point in time and a modify does not rewrite history. The
    snapshot columns themselves are left untouched. Only split.quantity, which
    is what is actually at the broker, moves.

    Args:
        user_id: owner.
        order_id: parent EquityOrder id.
        price: new limit price, applied to every open split.
        trigger_price: new GTT trigger, applied to every open GTT split.
        total_quantity: new total, re-split by the recorded ratios.
        quantity_overrides: {account_id: quantity} applied after the re-split.
        account_ids: limit the modify to these accounts, default every open one.
        client_factory: the broker seam.
        strategy_name, max_workers: as for place_multi_account_order.

    Returns:
        dict with status ('success', 'partial' or 'error'), message, order_id,
        parent_status, accounts_total, accounts_ok, accounts_failed,
        accounts_indeterminate and results (one per account with account_id,
        ok, indeterminate, error_message, error_type).
    """
    try:
        order = _load_open_order(user_id, order_id)
        splits = _open_splits(order)
        if account_ids:
            wanted = {_to_int(value, 0) for value in account_ids}
            splits = [split for split in splits if split.account_id in wanted]
        if not splits:
            raise EquityOrderError('This order has no account order left that can be changed')

        new_price = _to_float(price)
        new_trigger = _to_float(trigger_price)
        if order.order_type == EQUITY_ORDER_TYPE_MARKET and new_price is not None:
            raise EquityOrderError('A MARKET order has no price to modify')
        if new_price is not None and new_price <= 0:
            raise EquityOrderError('A modified price must be positive')
        if new_trigger is not None and new_trigger <= 0:
            raise EquityOrderError('A modified trigger price must be positive')

        new_quantities = _requantify(order, splits, total_quantity, quantity_overrides)
    except EquityOrderError as exc:
        return {
            'status': 'error',
            'message': str(exc),
            'order_id': order_id,
            'results': [],
        }

    jobs = _amend_jobs(
        order, splits, 'modify', _call_modify, strategy_name, client_factory,
        new_quantities=new_quantities,
        price=new_price if new_price is not None else order.price,
        trigger_price=new_trigger if new_trigger is not None else order.trigger_price,
    )

    app = current_app._get_current_object()
    results = _run_jobs(app, jobs, _amend_worker, max_workers=max_workers)
    by_account = {result['account_id']: result for result in results}

    for split in splits:
        result = by_account.get(split.account_id)
        if result is None:
            continue
        if result['ok']:
            if new_quantities and split.account_id in new_quantities:
                split.quantity = new_quantities[split.account_id]
            split.error_message = None
            split.error_type = None
        else:
            split.error_message = _clip(result['error_message'])
            split.error_type = result['error_type']
        split.last_synced_at = _now()

    if new_price is not None:
        order.price = new_price
    if new_trigger is not None:
        order.trigger_price = new_trigger
    if new_quantities:
        # The parent total follows the per-account quantities that are really
        # at the broker. A split that was never sent carries zero, so it does
        # not inflate the total.
        order.total_quantity = sum(
            _to_int(split.quantity, 0) for split in order.splits.all()
        )
    db.session.commit()

    return _amend_result(order, results, 'modify')


def _requantify(order, splits, total_quantity, quantity_overrides):
    """
    Re-split a new total across the open splits using the ratios recorded at
    order time. Returns None when the quantity is not being changed.
    """
    overrides = {}
    for raw_account_id, raw_quantity in (quantity_overrides or {}).items():
        account_id = _to_int(raw_account_id, 0)
        quantity = _to_int(raw_quantity, -1)
        if quantity <= 0:
            raise EquityOrderError('A modified quantity must be a positive whole number')
        overrides[account_id] = quantity

    open_accounts = {split.account_id for split in splits}
    unknown = set(overrides) - open_accounts
    if unknown:
        raise EquityOrderError(
            'Quantity given for account %s, which has no open order on this instruction'
            % ', '.join(str(account_id) for account_id in sorted(unknown))
        )

    if total_quantity is None and not overrides:
        return None

    if total_quantity is None:
        quantities = {
            split.account_id: _to_int(split.quantity, 0) for split in splits
        }
    else:
        total = _to_int(total_quantity, 0)
        if total <= 0:
            raise EquityOrderError('A modified total quantity must be positive')
        ratios = OrderedDict(
            (split.account_id, _to_float(split.qty_ratio_at_order, 0.0) or 0.0)
            for split in splits
        )
        quantities = dict(split_quantity_by_ratio(total, ratios).quantities)

    quantities.update(overrides)

    if any(quantity <= 0 for quantity in quantities.values()):
        raise EquityOrderError(
            'The new quantity leaves an account with nothing to trade. Cancel '
            'that account instead of modifying it to zero.'
        )
    return quantities


def cancel_order(user_id, order_id, account_ids=None, client_factory=None,
                 strategy_name=DEFAULT_STRATEGY_NAME, max_workers=None):
    """
    Cancel an order that is still PENDING or PARTIAL, account by account,
    concurrently, with the same failure isolation as placement.

    A split is marked CANCELLED only on an explicit broker confirmation. A
    cancel whose answer never arrived leaves the split exactly as it was, with
    the reason recorded: we do not know whether that order is still live, and
    pretending either way loses it.

    Returns:
        dict with status ('success', 'partial' or 'error'), message, order_id,
        parent_status, accounts_total, accounts_ok, accounts_failed,
        accounts_indeterminate and results.
    """
    try:
        order = _load_open_order(user_id, order_id)
        splits = _open_splits(order)
        if account_ids:
            wanted = {_to_int(value, 0) for value in account_ids}
            splits = [split for split in splits if split.account_id in wanted]
        if not splits:
            raise EquityOrderError('This order has no account order left that can be cancelled')
    except EquityOrderError as exc:
        return {
            'status': 'error',
            'message': str(exc),
            'order_id': order_id,
            'results': [],
        }

    jobs = _amend_jobs(order, splits, 'cancel', _call_cancel, strategy_name, client_factory)

    app = current_app._get_current_object()
    results = _run_jobs(app, jobs, _amend_worker, max_workers=max_workers)
    by_account = {result['account_id']: result for result in results}

    for split in splits:
        result = by_account.get(split.account_id)
        if result is None:
            continue
        if result['ok']:
            split.fill_status = EQUITY_SPLIT_STATUS_CANCELLED
            split.error_message = None
            split.error_type = None
        else:
            split.error_message = _clip(result['error_message'])
            split.error_type = result['error_type']
        split.last_synced_at = _now()
    db.session.commit()

    recompute_parent_status(order.id, user_id)
    return _amend_result(order, results, 'cancel')


def _amend_result(order, results, action):
    """Assemble the public result of a modify or a cancel."""
    ok = sum(1 for result in results if result['ok'])
    indeterminate = sum(1 for result in results if result['indeterminate'])
    failed = len(results) - ok

    if ok == 0:
        status = 'error'
    elif ok < len(results):
        status = 'partial'
    else:
        status = 'success'

    parts = ['%d of %d accounts %sled' % (ok, len(results), action)]
    if failed:
        parts.append('%d failed' % failed)
    if indeterminate:
        parts.append('%d unconfirmed, verify at the broker' % indeterminate)

    return {
        'status': status,
        'message': ', '.join(parts),
        'order_id': order.id,
        'parent_status': order.status,
        'accounts_total': len(results),
        'accounts_ok': ok,
        'accounts_failed': failed,
        'accounts_indeterminate': indeterminate,
        'results': results,
    }


# ---------------------------------------------------------------------------
# Public: exit. THE claim-and-place helper.
# ---------------------------------------------------------------------------

def _exit_result(status, holding_id, message, **extra):
    """The shape every exit attempt returns."""
    result = {
        'status': status,
        'holding_id': holding_id,
        'message': message,
        'claimed': False,
        'indeterminate': False,
        'broker_order_id': None,
        'order_id': None,
        'split_id': None,
        'account_id': None,
        'quantity': 0,
        'attempts': 0,
    }
    result.update(extra)
    return result


def exit_holding(user_id, holding_id, reason=EQUITY_EXIT_REASON_MANUAL,
                 quantity=None, order_type=EQUITY_ORDER_TYPE_MARKET, price=None,
                 trigger_price=None, allow_from=None, client_factory=None,
                 strategy_name=DEFAULT_STRATEGY_NAME,
                 max_attempts=DEFAULT_MAX_ATTEMPTS,
                 retry_delay_seconds=DEFAULT_RETRY_DELAY_SECONDS, sleeper=None,
                 gtt_trigger_leg=None):
    """
    Sell against one holding. This is the ONLY way an equity exit is placed.

    The manual Sell action and the background stop loss / target monitor both
    call this function, which is what stops them selling the same shares twice.
    The order is fixed:

        1. EquityHolding.claim_for_exit locks the row, re-checks that it is
           still claimable and still carries no broker order id, writes
           EXIT_PENDING and COMMITS. Losing that race returns a skipped result,
           it is not an error.
        2. The parent order and its single split are created and committed.
        3. Only then is the broker called, retrying a DEFINITE refusal only.
        4. On success the broker order id goes onto the holding first, because
           losing an order id is the worst failure available here, and then
           onto the split.
        5. On a definite refusal the claim is released, and only while the row
           is still EXIT_PENDING with no order id.
        6. On an indeterminate outcome the claim is NEVER released. The holding
           becomes EXIT_INDETERMINATE and waits for a human.

    Args:
        user_id: owner.
        holding_id: EquityHolding to sell.
        reason: EQUITY_EXIT_REASON_MANUAL, _STOP_LOSS or _TARGET.
        quantity: shares to sell, capped at the holding's sellable quantity.
            None sells everything sellable.
        order_type: MARKET (the default, and what the monitor uses), LIMIT or
            GTT.
        price: limit price, required for LIMIT and GTT.
        trigger_price: required for GTT.
        allow_from: statuses the claim may be taken from. Pass
            (EQUITY_HOLDING_STATUS_AWAITING_CONFIRM,) when an admin approves a
            CONFIRM mode alert, so the approval cannot fire on a holding that
            was never alerted.
        client_factory: the broker seam.
        strategy_name, max_attempts, retry_delay_seconds, sleeper,
        gtt_trigger_leg: as for place_multi_account_order.

    Returns:
        dict with status:
            'success'        the sell is at the broker, broker_order_id is set
            'skipped'        the claim was not taken (another exit is already
                             in flight, nothing sellable, wrong status). No
                             broker call was made.
            'indeterminate'  the outcome is unknown. The holding is
                             EXIT_INDETERMINATE and must be reconciled by hand.
            'error'          a definite failure. The claim was released and the
                             holding can be exited again.
        plus holding_id, account_id, quantity, order_id, split_id,
        broker_order_id, attempts, claimed and message.
    """
    try:
        order_type, price, trigger_price = _normalise_price_fields(
            order_type, price, trigger_price
        )
    except EquityOrderError as exc:
        return _exit_result('error', holding_id, str(exc))

    if reason not in _EXIT_REASON_TO_SOURCE:
        return _exit_result(
            'error', holding_id,
            'Exit reason must be one of %s' % ', '.join(sorted(_EXIT_REASON_TO_SOURCE))
        )

    # 1. THE CLAIM. Locked, re-checked and committed before anything else.
    holding, refusal = EquityHolding.claim_for_exit(
        holding_id, user_id, reason, quantity=quantity, allow_from=allow_from
    )
    if holding is None:
        logger.info('Equity exit not claimed for holding %s: %s', holding_id, refusal)
        return _exit_result('skipped', holding_id, refusal)

    # Plain values read once, after the claim committed.
    account_id = holding.account_id
    symbol = holding.symbol
    exchange = holding.exchange
    exit_quantity = _to_int(holding.exit_quantity, 0)
    trade_nature_id = holding.trade_nature_id

    def release(message):
        """Give the claim back after a DEFINITE failure, nothing was sent."""
        EquityHolding.release_exit_claim(holding_id, user_id, message=message)

    if exit_quantity <= 0:
        release('Nothing sellable when the exit was prepared')
        return _exit_result('error', holding_id, 'Nothing sellable on this holding',
                            account_id=account_id)

    account = TradingAccount.query.filter_by(id=account_id, user_id=user_id).first()
    if account is None or not account.is_active:
        message = 'The account for this holding is not available for trading'
        release(message)
        return _exit_result('error', holding_id, message, account_id=account_id)

    credential = _credential_for(account)
    if credential.get('api_key') is None:
        message = 'The API key for this account could not be read'
        release(message)
        return _exit_result('error', holding_id, message, account_id=account_id)

    # 2. The record of what is about to be sent, committed BEFORE the send.
    # If this fails, nothing has been sent anywhere, so the claim is given back
    # rather than leaving the holding stuck as EXIT_PENDING for ever.
    try:
        order = _create_parent_order(
            user_id=user_id, symbol=symbol, exchange=exchange,
            side=EQUITY_SIDE_SELL, order_type=order_type, price=price,
            trigger_price=trigger_price, total_quantity=exit_quantity,
            stop_loss=None, target=None, trade_nature_id=trade_nature_id,
            source=_EXIT_REASON_TO_SOURCE[reason], leftover_quantity=0,
            funds_action=EQUITY_FUNDS_ACTION_SKIP,
        )
        split = _create_split(order, {
            'account_id': account_id,
            'qty_ratio': 100.0,
            'quantity': exit_quantity,
            'ratio_quantity': exit_quantity,
            'qty_overridden': False,
            # An exit is priced by the market, and no cash is spent on a sell,
            # so there is no funds read on this path. The stop loss monitor
            # must not wait on one.
            'est_value': round(exit_quantity * price, 2) if price else None,
            'cash_balance': None,
        })
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        message = 'Could not record the exit before sending it: %s' % exc
        logger.error('Equity exit aborted for holding %s: %s', holding_id, message)
        release(message)
        return _exit_result('error', holding_id, message, account_id=account_id)

    job = {
        'credential': credential,
        'client_factory': client_factory,
        'symbol': symbol,
        'exchange': exchange,
        'side': EQUITY_SIDE_SELL,
        'order_type': order_type,
        'price': price,
        'trigger_price': trigger_price,
        'quantity': exit_quantity,
        'strategy_name': strategy_name,
        'gtt_trigger_leg': gtt_trigger_leg,
        'max_attempts': max_attempts,
        'retry_delay_seconds': retry_delay_seconds,
        'sleeper': sleeper,
    }

    # 3. The broker call, with the claim already committed.
    try:
        client = _resolve_factory(client_factory)(credential)
        outcome = _send_placement(client, job)
    except Exception as exc:
        logger.error('Equity exit failed to reach the broker for holding %s: %s',
                     holding_id, exc)
        outcome = _blank_outcome()
        outcome['fill_status'] = EQUITY_SPLIT_STATUS_INDETERMINATE
        outcome['indeterminate'] = True
        outcome['error_type'] = 'client_error'
        outcome['error_message'] = 'Exit failed unexpectedly: %s' % exc
        outcome['attempts'] = 1

    broker_order_id = outcome.get('broker_order_id') or outcome.get('broker_gtt_id')

    # 4. Success: the order id lands on the holding FIRST. Losing an order id
    # is the worst failure available here, so it is written before anything
    # else and its own failure is never allowed to mask the sale.
    if outcome['ok']:
        EquityHolding.mark_exit_submitted(
            holding_id, user_id, broker_order_id, split_id=split.id
        )
        try:
            _apply_outcome(split, outcome)
            db.session.commit()
            recompute_parent_status(order.id, user_id)
        except Exception as exc:
            db.session.rollback()
            logger.critical(
                'EQUITY EXIT NOT FULLY RECORDED. Holding %s sold under broker '
                'order %s but split %s could not be updated: %s',
                holding_id, broker_order_id, split.id, exc
            )
        logger.info(
            'Equity exit placed for holding %s (%s %s x%d), broker order %s',
            holding_id, symbol, reason, exit_quantity, broker_order_id
        )
        return _exit_result(
            'success', holding_id,
            'Exit placed for %d shares of %s' % (exit_quantity, symbol),
            claimed=True, account_id=account_id, quantity=exit_quantity,
            order_id=order.id, split_id=split.id,
            broker_order_id=broker_order_id, attempts=outcome['attempts'],
        )

    # Record the failure on the split. If even this cannot be written, the
    # holding state below still has to be set, so the session is cleaned up
    # and the claim decision goes ahead regardless.
    try:
        _apply_outcome(split, outcome)
        db.session.commit()
        recompute_parent_status(order.id, user_id)
    except Exception as exc:
        db.session.rollback()
        logger.error(
            'Equity exit for holding %s could not record its failure on split %s: %s',
            holding_id, split.id, exc
        )

    # 6. Indeterminate: the claim STAYS. The order may be live at the broker.
    if outcome['indeterminate']:
        EquityHolding.mark_exit_indeterminate(
            holding_id, user_id,
            'Exit outcome unknown (%s). Check the broker order book before '
            'selling again. %s' % (outcome.get('error_type') or 'no answer',
                                   outcome.get('error_message') or '')
        )
        logger.error(
            'Equity exit INDETERMINATE for holding %s (%s): %s',
            holding_id, symbol, outcome.get('error_message')
        )
        return _exit_result(
            'indeterminate', holding_id,
            'The exit for %s could not be confirmed. Verify at the broker '
            'before trying again.' % symbol,
            claimed=True, indeterminate=True, account_id=account_id,
            quantity=exit_quantity, order_id=order.id, split_id=split.id,
            attempts=outcome['attempts'],
        )

    # 5. Definite failure: release the claim so the holding can be exited again.
    release(outcome.get('error_message'))
    logger.warning(
        'Equity exit refused for holding %s (%s): %s',
        holding_id, symbol, outcome.get('error_message')
    )
    return _exit_result(
        'error', holding_id, outcome.get('error_message') or 'The broker refused the exit',
        claimed=True, account_id=account_id, quantity=exit_quantity,
        order_id=order.id, split_id=split.id, attempts=outcome['attempts'],
    )


def _exit_holding_worker(app, holding_id, kwargs):
    """
    One holding's exit on its own thread, with its own session.

    Unlike placement, the claim has to happen inside the worker: the whole
    point of the claim is that two workers racing for the same holding are
    resolved by the database, not by the caller. The session is released at the
    end so the connection goes back to the pool.
    """
    with app.app_context():
        try:
            return exit_holding(holding_id=holding_id, **kwargs)
        except Exception as exc:
            logger.error('Equity exit worker failed for holding %s: %s', holding_id, exc)
            return _exit_result('error', holding_id, 'Exit failed unexpectedly: %s' % exc)
        finally:
            db.session.remove()


def exit_holdings(user_id, holding_ids, max_workers=None, **kwargs):
    """
    Exit several holdings at once, for example one symbol across five accounts.

    Every holding goes through exit_holding, so every one of them is claimed
    before its broker call. Failures are isolated: one holding that cannot be
    exited never stops another.

    Args:
        user_id: owner.
        holding_ids: EquityHolding ids to exit.
        max_workers: pool bound, capped at MAX_ORDER_WORKERS.
        **kwargs: passed straight to exit_holding (reason, order_type, price,
            client_factory and the rest).

    Returns:
        dict with status ('success', 'partial' or 'error'), message, counts for
        placed, skipped, failed and indeterminate, and results, one exit_holding
        result per holding.
    """
    ids = []
    for raw in holding_ids or []:
        holding_id = _to_int(raw, 0)
        if holding_id > 0 and holding_id not in ids:
            ids.append(holding_id)

    if not ids:
        return {
            'status': 'error',
            'message': 'No holding was selected to exit',
            'results': [],
            'placed': 0, 'skipped': 0, 'failed': 0, 'indeterminate': 0,
        }

    call_kwargs = dict(kwargs)
    call_kwargs['user_id'] = user_id

    if len(ids) == 1:
        results = [exit_holding(holding_id=ids[0], **call_kwargs)]
    else:
        app = current_app._get_current_object()
        bound = min(max_workers or MAX_ORDER_WORKERS, MAX_ORDER_WORKERS, len(ids))
        results = []
        with ThreadPoolExecutor(max_workers=bound) as executor:
            futures = [
                (holding_id, executor.submit(_exit_holding_worker, app, holding_id, call_kwargs))
                for holding_id in ids
            ]
            for holding_id, future in futures:
                try:
                    results.append(future.result())
                except Exception as exc:
                    logger.error('Equity exit crashed for holding %s: %s', holding_id, exc)
                    results.append(
                        _exit_result('error', holding_id, 'Exit crashed: %s' % exc)
                    )

    placed = sum(1 for result in results if result['status'] == 'success')
    skipped = sum(1 for result in results if result['status'] == 'skipped')
    indeterminate = sum(1 for result in results if result['status'] == 'indeterminate')
    failed = sum(1 for result in results if result['status'] == 'error')

    if placed == 0:
        status = 'error'
    elif placed < len(results):
        status = 'partial'
    else:
        status = 'success'

    parts = ['%d of %d holdings exited' % (placed, len(results))]
    if failed:
        parts.append('%d failed' % failed)
    if skipped:
        parts.append('%d skipped' % skipped)
    if indeterminate:
        parts.append('%d unconfirmed, verify at the broker' % indeterminate)

    return {
        'status': status,
        'message': ', '.join(parts),
        'placed': placed,
        'skipped': skipped,
        'failed': failed,
        'indeterminate': indeterminate,
        'results': results,
    }
