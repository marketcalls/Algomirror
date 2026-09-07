"""
Intraday Short Square-Off Monitor

Buys back every open intraday short before the close, and shouts if it cannot.

Why this module exists
----------------------
Everything else in the equity module is a position you HAVE. A short is a
position you OWE, and the difference decides how this file is written. A
holding left alone does nothing. A short left open past the close is unlimited
loss above the entry price, a forced buy-back by the broker at whatever price
is there, and a penalty on top.

So this is the only monitor in the application whose job is not to watch for a
condition but to meet a deadline. It does not ask whether to act. Past the
square-off minute it acts on everything still open, and if it cannot, that is
the loudest thing this application has to say.

Order of the day
----------------
    15:00   the placement path stops opening NEW shorts (EquitySetting
            .intraday_cutoff_minute). A short opened at 15:18 has two minutes
            to work and must then be bought back whatever the price.
    15:12   this monitor buys back everything still open
            (EquitySetting.intraday_squareoff_minute)
    15:15   OpenAlgo's sandbox squares off MIS on NSE and BSE, and a real
            broker has its own cut-off somewhere near here
    15:30   close

Being second means the broker does it, at market, at whatever price is there -
and it means this monitor never gets to prove it works. Hence 15:12.

Safety rules this module is built around
----------------------------------------
Verify against the POSITION book, never the holdings book. A short lives in
positions; the holdings book would answer zero, and zero read as "nothing to
buy back" is the one wrong answer that matters here.

Claim before placing. EquityIntradayShort.claim_for_cover settles it with one
conditional UPDATE, so this monitor and a person pressing Cover cannot both buy
the same shares back.

Never retry an indeterminate outcome. If the buy may be live, a second one
would double the position. It becomes COVER_INDETERMINATE and waits for a
human, exactly as a holding exit does.

A broker that cannot be read stops the attempt, and the attempt is repeated on
the next tick. It does not stop the DEADLINE: an unclosed short past the
square-off raises a notice, once, and keeps trying until the session ends.
"""

import logging
import threading
from datetime import datetime
from typing import Dict, List, Optional

import pytz
from flask import has_app_context

from app import db
from app.models import (
    EQUITY_ORDER_TYPE_MARKET,
    EQUITY_PRODUCT_MIS,
    EQUITY_STOP_STATUS_CANCELLED,
    EQUITY_STOP_STATUS_NONE,
    EQUITY_STOP_STATUS_RESTING,
    EQUITY_STOP_STATUS_TRIGGERED,
    EQUITY_SHORT_STATUS_COVERED,
    EQUITY_SHORT_STATUS_COVER_INDETERMINATE,
    EQUITY_SHORT_STATUS_COVER_SUBMITTED,
    EQUITY_SHORT_STATUS_OPEN,
    EQUITY_SIDE_BUY,
    EquityIntradayShort,
    EquitySetting,
    TradingAccount,
)

logger = logging.getLogger(__name__)

IST = pytz.timezone('Asia/Kolkata')

SCHEDULER_JOB_ID = 'equity_intraday_squareoff'

# Every 15 seconds. Fine enough that the square-off happens within a quarter
# minute of its time, coarse enough not to hammer the broker. It costs nothing
# on a normal day: with no open short the tick ends on a single indexed count.
SCHEDULER_INTERVAL_SECONDS = 15

# Why the buy-back was placed. Recorded on the short and on the order.
COVER_REASON_SQUAREOFF = 'SQUAREOFF'
COVER_REASON_MANUAL = 'MANUAL'
COVER_REASON_STOP_LOSS = 'STOP_LOSS'
COVER_REASON_TARGET = 'TARGET'

# How long past the square-off minute a short may remain open before it is
# announced. Two ticks of grace, so a single slow broker answer is not reported
# as a failure.
ALERT_AFTER_SECONDS = 45


class EquityIntradaySquareOff:
    """
    Singleton. Driven by the shared scheduler, exactly like the exit monitor -
    it schedules nothing itself and does nothing until start() arms it, so a
    failed registration in the app factory leaves it idle rather than half
    wired.
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        with cls._lock:
            if cls._instance is None:
                cls._instance = super().__new__(cls)
                cls._instance._ready = False
            return cls._instance

    def __init__(self):
        if getattr(self, '_initialised', False):
            return
        self._initialised = True
        self._ready = False
        self._placer = None
        self._canceller = None
        self._stats = {
            'ticks': 0,
            'covers_placed': 0,
            'covers_skipped': 0,
            'covers_failed': 0,
            'covers_indeterminate': 0,
        }

    # ------------------------------------------------------------------
    # Arming
    # ------------------------------------------------------------------

    def start(self):
        self._ready = True
        logger.info('[EQUITY_SHORT] Square-off monitor armed')

    def stop(self):
        self._ready = False
        logger.info('[EQUITY_SHORT] Square-off monitor stopped')

    def set_canceller(self, canceller):
        """Inject what cancels the resting stop. Same reason as the placer."""
        self._canceller = canceller

    def _resolve_canceller(self):
        if getattr(self, '_canceller', None) is not None:
            return self._canceller
        from app.utils.equity_order_engine import cancel_order
        return cancel_order

    def set_placer(self, placer):
        """
        Inject what actually places the buy-back.

        Kept behind a seam for one reason: it lets the whole deadline path be
        tested without a broker, which for the only mechanism in this module
        that MUST work is worth the indirection.
        """
        self._placer = placer

    def _resolve_placer(self):
        if self._placer is not None:
            return self._placer
        from app.utils.equity_order_engine import place_multi_account_order
        return place_multi_account_order

    # ------------------------------------------------------------------
    # The tick
    # ------------------------------------------------------------------

    def run_checks(self) -> Dict:
        """One pass. Safe to call as often as the scheduler likes."""
        summary = {'users': 0, 'covered': 0, 'skipped': 0, 'failed': 0, 'open': 0}

        if not self._ready:
            return summary
        if not has_app_context():
            logger.error('[EQUITY_SHORT] Tick without an app context, skipped')
            return summary

        self._stats['ticks'] += 1

        try:
            user_ids = self._users_with_open_shorts()
        except Exception as exc:
            logger.error('[EQUITY_SHORT] Could not list open shorts: %s', exc)
            self._safe_rollback()
            return summary

        for user_id in user_ids:
            summary['users'] += 1
            try:
                self._check_user(user_id, summary)
            except Exception as exc:
                logger.error(
                    '[EQUITY_SHORT] Square-off failed for user %s: %s',
                    user_id, exc, exc_info=True
                )
                self._safe_rollback()
                self._record_error(user_id, str(exc))

        return summary

    def _check_user(self, user_id, summary):
        settings = EquitySetting.query.filter_by(user_id=user_id).first()
        if settings is not None and not settings.intraday_monitor_enabled:
            # Off does not mean safe. The placement path refuses to OPEN a
            # short while this is off, so the only shorts that can reach here
            # with it off are ones opened before it was switched off - and
            # those still have to be closed.
            logger.warning(
                '[EQUITY_SHORT] Monitor is switched off for user %s but open '
                'shorts exist. Squaring them off anyway: an open short is not '
                'something a setting gets to leave alone.', user_id
            )

        squareoff_minute = self._squareoff_minute(settings)
        now = datetime.now(IST)
        minute_now = now.hour * 60 + now.minute

        shorts = self._open_shorts(user_id)
        summary['open'] += len(shorts)

        self._write_heartbeat(settings)

        if minute_now < squareoff_minute:
            # Not yet. The shorts are counted so the screens can show the
            # countdown, and nothing else happens.
            return

        for short in shorts:
            self._cover(short, summary, minute_now, squareoff_minute)

    # ------------------------------------------------------------------
    # One buy-back
    # ------------------------------------------------------------------

    @staticmethod
    def _note(summary, text):
        """
        Why this short did what it did, in words.

        The scheduler only ever wanted counters, and for a background job that
        was enough. A person pressing Cover is owed the reason, and the reason
        exists only at the moment of the decision - reconstructing it afterwards
        from the row would be a guess.
        """
        summary.setdefault('notes', []).append(text)

    def _cover(self, short, summary, minute_now, squareoff_minute,
               reason=COVER_REASON_SQUAREOFF):
        """
        Buy back one short: verify, claim, place. In that order, always.

        The same sequence whether the square-off called it or a person pressed
        Cover. Deliberately one function: a second path with its own ordering
        is how two things end up buying the same short back twice.
        """
        account = TradingAccount.query.filter_by(
            id=short.account_id, user_id=short.user_id
        ).first()
        if account is None or not account.is_active:
            self._announce_unclosed(
                short,
                'The account this short belongs to is not available, so it '
                'could not be bought back.'
            )
            self._note(summary, 'The account this short belongs to is not '
                                'available, so nothing could be sent.')
            summary['failed'] += 1
            return

        owed = int(short.quantity or 0)
        if owed <= 0:
            self._note(summary, 'Nothing is owed on this short.')
            return

        # 1. VERIFY, against the position book. A short is not in holdings and
        #    asking there would answer zero - which reads as "nothing owed".
        verified = self._verify_open_quantity(account, short)
        if verified is None:
            # The broker could not be read. Nothing is placed, and the next
            # tick tries again. This is a delay, not a decision.
            logger.warning(
                '[EQUITY_SHORT] Could not read the position book for account '
                '%s while squaring off %s. Nothing placed; will retry.',
                short.account_id, short.symbol
            )
            self._note(summary, 'Your broker could not be read, so nothing '
                                'was sent. It will be tried again.')
            summary['skipped'] += 1
            self._maybe_announce_late(short, minute_now, squareoff_minute)
            return

        if verified <= 0:
            # The broker says nothing is open. Something else closed it - most
            # likely the protective stop resting at the broker, which is
            # exactly what it is there for; or the broker's own square-off, or
            # a manual buy at the terminal. Record it rather than placing a buy
            # for shares that are not owed.
            #
            # This is the check that makes the resting stop safe to have. Two
            # things can close this short, and the first question asked before
            # the second one acts is "is there still anything to close".
            logger.info(
                '[EQUITY_SHORT] %s on account %s is already flat at the '
                'broker. Closing the record without placing anything.',
                short.symbol, short.account_id
            )
            self._mark_closed_externally(short)
            self._note(summary, 'Your broker shows nothing open. Something '
                                'else already closed it - most likely the stop '
                                'resting at the broker. The record is closed '
                                'and nothing was bought.')
            summary['covered'] += 1
            return

        if verified != owed:
            recorded = owed
            logger.warning(
                '[EQUITY_SHORT] %s on account %s: broker shows %s open '
                'against %s recorded. Buying back the broker figure.',
                short.symbol, short.account_id, verified, recorded
            )
            try:
                short.quantity = verified
                db.session.commit()
                owed = verified
            except Exception as exc:
                db.session.rollback()
                logger.error(
                    '[EQUITY_SHORT] Could not store the verified quantity for '
                    'short %s: %s', short.id, exc
                )
                self._note(summary, 'The broker figure could not be stored, '
                                    'so nothing was sent.')
                summary['skipped'] += 1
                return
            self._note(summary, 'Your broker shows %d open against %d '
                                'recorded. Buying back the broker figure.'
                                % (verified, recorded))

        # 2. CLAIM. One conditional UPDATE decides the winner, so this monitor
        #    and a person pressing Cover cannot both buy the same shares back.
        claimed, refusal = EquityIntradayShort.claim_for_cover(
            short.id, short.user_id, reason, quantity=owed
        )
        if claimed is None:
            logger.info(
                '[EQUITY_SHORT] Skipped %s on account %s: %s',
                short.symbol, short.account_id, refusal
            )
            self._note(summary, 'Something else is already buying this back, '
                                'so nothing was sent a second time. %s'
                                % (refusal or ''))
            summary['skipped'] += 1
            return

        # 3. CANCEL the resting stop, and CONFIRM it. Two things can close
        #    this short - the SL-M sitting at the broker and this buy-back -
        #    and both firing would buy it back twice, leaving a LONG position
        #    nobody asked for. Cancelling first is what makes that impossible.
        cancelled, cancel_message = self._cancel_resting_stop(claimed)
        if not cancelled:
            # Nothing is placed. This is the one refusal in the whole module
            # that is safer than acting: the resting stop is still live and
            # will close the position on its own, and the broker's own
            # square-off sits behind that. Placing anyway is the single move
            # that turns a covered short into an unwanted long.
            logger.error(
                '[EQUITY_SHORT] NOT covering %s on account %s: the resting '
                'stop could not be cancelled (%s). The stop is still live and '
                'will close it.',
                claimed.symbol, claimed.account_id, cancel_message
            )
            EquityIntradayShort.release_claim(
                claimed.id, claimed.user_id,
                error='Resting stop could not be cancelled: %s' % cancel_message
            )
            self._note(summary,
                       'NOTHING was bought. The stop-loss order resting at your '
                       'broker could not be taken out of the market (%s), and '
                       'buying while it is still live would close this short '
                       'twice and leave you LONG. The resting stop will close '
                       'it instead.' % cancel_message)
            summary['skipped'] += 1
            return

        # 4. PLACE. Market, because at this point being filled matters more
        #    than the price - the alternative is the broker doing it minutes
        #    later at a price nobody chose.
        self._place_cover(claimed, account, owed, summary)

    def cover_now(self, short_id, user_id):
        """
        Buy one short back NOW, because a person asked for it.

        The square-off runs at its minute and the resting stop fires on a
        breach, and between those two there was no way out at all: a short that
        looked wrong at two in the afternoon had to be closed at the broker's
        own terminal, outside AlgoMirror, where nothing here would know about
        it.

        It does NOT get its own sequence. It calls the same _cover the
        square-off calls, so the verify, the claim, the cancel and the place
        happen in the same order with the same refusals. A second path with its
        own ordering is precisely how a short gets bought back twice.

        Never raises. Returns {status, message, short_id, notes}, where status
        is one of:

            covered        the buy-back was sent, or the short was already flat
            skipped        nothing was sent, and it was safer that way
            failed         something went wrong and the short is still open

        Safe at any hour, including before the square-off and after it.
        """
        result = {
            'short_id': short_id, 'status': 'failed', 'message': '', 'notes': [],
        }

        short = EquityIntradayShort.query.filter_by(
            id=short_id, user_id=user_id
        ).populate_existing().first()
        if short is None:
            result['status'] = 'failed'
            result['message'] = 'That short could not be found.'
            return result

        result['symbol'] = short.symbol
        result['account_id'] = short.account_id
        result['quantity'] = int(short.quantity or 0)

        if short.status != EQUITY_SHORT_STATUS_OPEN:
            result['status'] = 'skipped'
            result['message'] = (
                'Nothing was sent: this short is already %s.'
                % str(short.status or '').replace('_', ' ').lower()
            )
            return result

        summary = {'covered': 0, 'skipped': 0, 'failed': 0, 'open': 0, 'notes': []}
        now = datetime.now(IST)
        minute_now = now.hour * 60 + now.minute
        settings = EquitySetting.query.filter_by(user_id=user_id).first()
        squareoff_minute = self._squareoff_minute(settings)

        try:
            self._cover(short, summary, minute_now, squareoff_minute,
                        reason=COVER_REASON_MANUAL)
        except Exception as exc:
            self._safe_rollback()
            logger.error(
                '[EQUITY_SHORT] Manual cover raised for short %s: %s',
                short_id, exc, exc_info=True
            )
            result['message'] = (
                'The buy-back could not be completed: %s. Check this short at '
                'your broker.' % exc
            )
            return result

        result['notes'] = summary.get('notes') or []
        if summary['covered']:
            result['status'] = 'covered'
        elif summary['failed']:
            result['status'] = 'failed'
        else:
            result['status'] = 'skipped'
        result['message'] = ' '.join(result['notes']) or 'Nothing was sent.'
        return result

    def _cancel_resting_stop(self, short):
        """
        Take the protective SL-M out of the market before buying back.

        Returns (ok, message). ok is True only when there is nothing left
        resting - either because there never was one, or because the broker
        confirmed the cancellation.

        A cancel whose answer never arrived is NOT ok. The order may still be
        live, and "may still be live" is the same as live for this decision.
        """
        status = getattr(short, 'stop_status', EQUITY_STOP_STATUS_NONE)

        if status != EQUITY_STOP_STATUS_RESTING:
            # Nothing resting. Either none was ever placed, or it has already
            # been cancelled or triggered - and a TRIGGERED stop means the
            # position may already be closed, which the verify step above will
            # have caught by reading zero from the position book.
            return True, 'no resting stop'

        order_id = getattr(short, 'stop_order_id', None)
        if not order_id:
            # RESTING with no order id to cancel by. This should be impossible,
            # and if it happens the honest answer is that we cannot take the
            # order out of the market, so we must not place another.
            return False, 'the resting stop has no order id to cancel by'

        canceller = self._resolve_canceller()
        try:
            result = canceller(short.user_id, order_id)
        except Exception as exc:
            logger.error(
                '[EQUITY_SHORT] Cancel raised for the resting stop on short '
                '%s: %s', short.id, exc, exc_info=True
            )
            return False, str(exc)

        status_text = (result or {}).get('status')
        if status_text != 'success':
            return False, (result or {}).get('message') or 'the broker did not confirm the cancel'

        try:
            short.stop_status = EQUITY_STOP_STATUS_CANCELLED
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            # The cancel DID succeed at the broker; only our record of it
            # failed. Saying otherwise would withhold a buy-back that is now
            # safe, so this is reported as ok with the failure logged.
            logger.error(
                '[EQUITY_SHORT] Cancelled the resting stop on short %s but '
                'could not record it: %s', short.id, exc
            )

        return True, 'cancelled'

    def _place_cover(self, short, account, quantity, summary):
        placer = self._resolve_placer()
        try:
            result = placer(
                user_id=short.user_id,
                symbol=short.symbol,
                exchange=short.exchange,
                side=EQUITY_SIDE_BUY,
                total_quantity=quantity,
                order_type=EQUITY_ORDER_TYPE_MARKET,
                account_ids=[short.account_id],
                quantity_overrides={short.account_id: quantity},
                product=EQUITY_PRODUCT_MIS,
            )
        except Exception as exc:
            # The call raised, so whether it reached the broker is unknown.
            # Unknown is treated as live: never released, never retried.
            logger.error(
                '[EQUITY_SHORT] Buy-back raised for %s on account %s: %s',
                short.symbol, short.account_id, exc, exc_info=True
            )
            self._mark_indeterminate(short, str(exc))
            self._note(summary,
                       'The buy-back was sent but your broker never answered, '
                       'so whether it went through is UNKNOWN. Check it at the '
                       'broker. Nothing will be sent again automatically.')
            summary['failed'] += 1
            self._stats['covers_indeterminate'] += 1
            return

        status = (result or {}).get('status')
        order_id = (result or {}).get('order_id')

        if status in ('success', 'partial'):
            self._mark_submitted(short, result)
            self._note(summary, 'Bought back %d %s. Order %s.'
                                % (quantity, short.symbol, order_id))
            summary['covered'] += 1
            self._stats['covers_placed'] += 1
            logger.info(
                '[EQUITY_SHORT] Bought back %s %s on account %s, order %s',
                quantity, short.symbol, short.account_id, order_id
            )
            return

        # A definite refusal. Nothing is live, so the claim goes back and the
        # next tick tries again - there is still time before the close.
        message = (result or {}).get('message') or 'The broker refused the buy-back'
        released = EquityIntradayShort.release_claim(
            short.id, short.user_id, error=message
        )
        summary['failed'] += 1
        self._stats['covers_failed'] += 1
        logger.error(
            '[EQUITY_SHORT] Buy-back REFUSED for %s on account %s: %s%s',
            short.symbol, short.account_id, message,
            '' if released else ' (claim could not be released)'
        )
        self._note(summary, 'Your broker refused the buy-back: %s. This short '
                            'is still open.' % message)
        self._announce_unclosed(
            short,
            'The buy-back was refused: %s. This short is still open.' % message
        )

    # ------------------------------------------------------------------
    # Reading the broker
    # ------------------------------------------------------------------

    @staticmethod
    def _verify_open_quantity(account, short) -> Optional[int]:
        """
        How many shares are still short at the broker, from the POSITION book.

        Returns None when the broker could not be read - unknown, never zero.
        A zero here would mean "nothing to buy back", which is the single most
        expensive wrong answer this module can produce.

        A short is a NEGATIVE position, so its magnitude is what is owed.
        """
        from app.utils.equity_order_engine import default_client_factory

        try:
            api_key = account.get_api_key()
        except Exception as exc:
            logger.warning(
                '[EQUITY_SHORT] Could not read the key for account %s: %s',
                account.id, exc
            )
            return None
        if not api_key:
            return None

        credential = {
            'account_id': account.id,
            'api_key': api_key,
            'host_url': account.host_url,
        }

        # NOT _broker_position_counts. That one floors at zero, which is the
        # right answer to "how many do I hold" and the wrong answer to "how
        # many do I owe" - a short is the negative side of the book, and
        # flooring it would report every short as already closed.
        return EquityIntradaySquareOff._signed_short_quantity(
            credential, short, default_client_factory
        )

    @staticmethod
    def _signed_short_quantity(credential, short, client_factory) -> Optional[int]:
        """The magnitude of a negative delivery-or-intraday position, or None."""
        from app.utils.equity_order_engine import (
            BROKER_HOLDINGS_TIMEOUT_SECONDS, _resolve_factory, _to_int
        )

        try:
            client = _resolve_factory(client_factory)(
                dict(credential, timeout=BROKER_HOLDINGS_TIMEOUT_SECONDS)
            )
            response = client.positionbook()
        except Exception as exc:
            logger.warning(
                '[EQUITY_SHORT] Position book raised for account %s: %s',
                credential.get('account_id'), exc
            )
            return None

        if not isinstance(response, dict) or response.get('status') != 'success':
            return None

        data = response.get('data')
        rows = data.get('positions') if isinstance(data, dict) else data
        if not isinstance(rows, list):
            return None

        want_symbol = (short.symbol or '').strip().upper()
        want_exchange = (short.exchange or '').strip().upper()
        total = 0
        for row in rows:
            if not isinstance(row, dict):
                continue
            if str(row.get('symbol') or '').strip().upper() != want_symbol:
                continue
            row_exchange = str(row.get('exchange') or '').strip().upper()
            if want_exchange and row_exchange and row_exchange != want_exchange:
                continue
            total += _to_int(row.get('quantity'))

        # Negative is short. A positive or zero total means nothing is owed.
        return -total if total < 0 else 0

    # ------------------------------------------------------------------
    # Writing the outcome
    # ------------------------------------------------------------------

    @staticmethod
    def _mark_submitted(short, result):
        try:
            short.status = EQUITY_SHORT_STATUS_COVER_SUBMITTED
            short.cover_submitted_at = datetime.utcnow()
            short.cover_order_id = (result or {}).get('order_id')
            short.cover_error = None
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error(
                '[EQUITY_SHORT] Could not record the buy-back for short %s: %s',
                short.id, exc
            )

    @staticmethod
    def _mark_indeterminate(short, message):
        try:
            short.status = EQUITY_SHORT_STATUS_COVER_INDETERMINATE
            short.cover_error = message
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error(
                '[EQUITY_SHORT] Could not record an indeterminate buy-back on '
                'short %s: %s', short.id, exc
            )

    @staticmethod
    def _mark_closed_externally(short):
        try:
            short.status = EQUITY_SHORT_STATUS_COVERED
            short.quantity = 0
            short.cover_completed_at = datetime.utcnow()
            # A resting stop that is gone from the market and a position that
            # is flat means the stop did its job. Named, because "cancelled"
            # and "triggered" are very different things to read afterwards.
            if getattr(short, 'stop_status', None) == EQUITY_STOP_STATUS_RESTING:
                short.stop_status = EQUITY_STOP_STATUS_TRIGGERED
                short.cover_reason = 'STOP_LOSS'
            else:
                short.cover_reason = 'EXTERNAL'
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            logger.error(
                '[EQUITY_SHORT] Could not close short %s: %s', short.id, exc
            )

    def _maybe_announce_late(self, short, minute_now, squareoff_minute):
        """Say something once when a short is still open past its deadline."""
        if minute_now * 60 < squareoff_minute * 60 + ALERT_AFTER_SECONDS:
            return
        self._announce_unclosed(
            short,
            'This short is past its square-off time and the broker could not '
            'be read to close it.'
        )

    @staticmethod
    def _announce_unclosed(short, message):
        """
        Raise a notice about a short that could not be closed - once.

        An unclosed short is the one thing in this module worth interrupting
        somebody for, and it must not be announced on a loop: a notice every
        fifteen seconds is a notice nobody reads.
        """
        if short.alerted_at is not None:
            return
        try:
            from app.models import (
                EQUITY_NOTICE_SHARES_LEFT, EquityHoldingNotice
            )
            db.session.add(EquityHoldingNotice(
                user_id=short.user_id,
                account_id=short.account_id,
                symbol=short.symbol,
                exchange=short.exchange,
                kind=EQUITY_NOTICE_SHARES_LEFT,
                quantity_before=int(short.quantity or 0),
                quantity_after=int(short.quantity or 0),
                quantity_delta=0,
                had_armed_level=True,
                message='%s: %d shares are still SHORT. %s' % (
                    short.symbol, int(short.quantity or 0), message
                ),
            ))
            short.alerted_at = datetime.utcnow()
            db.session.commit()
            logger.error(
                '[EQUITY_SHORT] UNCLOSED SHORT %s x%s on account %s. %s',
                short.symbol, short.quantity, short.account_id, message
            )
        except Exception as exc:
            db.session.rollback()
            logger.error(
                '[EQUITY_SHORT] Could not announce an unclosed short: %s', exc
            )

    # ------------------------------------------------------------------
    # Queries, settings and heartbeat
    # ------------------------------------------------------------------

    @staticmethod
    def _users_with_open_shorts() -> List[int]:
        rows = db.session.query(EquityIntradayShort.user_id).filter(
            EquityIntradayShort.status == EQUITY_SHORT_STATUS_OPEN,
            EquityIntradayShort.quantity > 0,
        ).distinct().all()
        return [row[0] for row in rows]

    @staticmethod
    def _open_shorts(user_id) -> List[EquityIntradayShort]:
        # populate_existing, for the same reason the exit monitor uses it: a
        # row already in this session's identity map would otherwise come back
        # with the status it had when first loaded, and a decision taken on a
        # stale copy is exactly what the claim exists to prevent.
        return EquityIntradayShort.query.filter(
            EquityIntradayShort.user_id == user_id,
            EquityIntradayShort.status == EQUITY_SHORT_STATUS_OPEN,
            EquityIntradayShort.quantity > 0,
        ).populate_existing().all()

    @staticmethod
    def _squareoff_minute(settings) -> int:
        """
        The configured square-off, clamped to something sane.

        Bounded because this number decides whether a position closes. Anything
        past 15:20 puts it behind a real broker's own cut-off, at which point
        the setting is a comfort rather than a control.
        """
        default = 15 * 60 + 12
        try:
            minute = int(getattr(settings, 'intraday_squareoff_minute', 0) or 0)
        except (TypeError, ValueError):
            minute = 0
        if minute <= 0:
            minute = default
        return max(9 * 60 + 30, min(15 * 60 + 20, minute))

    def _write_heartbeat(self, settings):
        """Prove it is running. A monitor that cannot is not trusted."""
        if settings is None:
            return
        try:
            settings.intraday_last_run_at = datetime.utcnow()
            settings.intraday_last_error = None
            db.session.commit()
        except Exception:
            db.session.rollback()

    @staticmethod
    def _record_error(user_id, message):
        try:
            settings = EquitySetting.query.filter_by(user_id=user_id).first()
            if settings is not None:
                settings.intraday_last_error = message[:2000]
                db.session.commit()
        except Exception:
            db.session.rollback()

    @staticmethod
    def _safe_rollback():
        try:
            db.session.rollback()
        except Exception:
            pass

    def status(self) -> Dict:
        """What the Settings screen shows about this monitor."""
        return {
            'ready': self._ready,
            'interval_seconds': SCHEDULER_INTERVAL_SECONDS,
            'stats': dict(self._stats),
        }


equity_intraday_monitor = EquityIntradaySquareOff()


def run_equity_intraday_squareoff():
    """Scheduler entry point. One tick."""
    return equity_intraday_monitor.run_checks()
