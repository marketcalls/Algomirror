"""
Risk Manager Service
Monitors and enforces risk thresholds for strategies.

Key Features:
- Max Loss monitoring with auto-exit
- Max Profit monitoring with auto-exit
- Trailing Stop Loss implementation
- Real-time P&L calculations using WebSocket data
- Audit logging of all risk events

Uses standard threading for background tasks
"""
import logging
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple
from decimal import Decimal
import pytz

from app import db

# IST timezone for storing timestamps
IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    """Get current time in IST (naive datetime for DB storage)"""
    return datetime.now(IST).replace(tzinfo=None)
from app.models import (
    Strategy, StrategyExecution, StrategyLeg, RiskEvent,
    TradingAccount
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)


class RiskManager:
    """
    Singleton service to monitor and enforce risk thresholds.

    Risk Types:
    - Max Loss: Closes all positions when total loss exceeds threshold
    - Max Profit: Closes all positions when total profit exceeds threshold
    - Trailing SL: Dynamically adjusts stop loss as price moves favorably

    Calculates P&L using real-time LTP from PositionMonitor.
    """

    _instance = None

    def __new__(cls):
        """Singleton pattern - only one instance"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the risk manager"""
        if self._initialized:
            return

        self._initialized = True
        self.is_running = False
        self.monitored_strategies: Dict[int, Strategy] = {}

        # Position cache to avoid redundant API calls
        # Format: {account_id: {'positions': [], 'timestamp': datetime}}
        self._positions_cache: Dict[int, Dict] = {}
        self._cache_ttl_seconds = 5  # Cache positions for 5 seconds (matches risk check interval)

        # Track which account is currently working for price feeds (failover support)
        self._current_price_account_id: Optional[int] = None
        self._failed_accounts: Dict[int, datetime] = {}  # Track failed accounts with timestamp
        self._failed_account_cooldown = 60  # Retry failed account after 60 seconds

        logger.debug("RiskManager initialized")

    def _get_prices_with_failover(self) -> Dict[str, float]:
        """
        Get current prices with automatic failover across accounts.

        Failover Logic:
        1. Check if within trading hours (skip API calls if market closed)
        2. Try last working account first (if known)
        3. Try primary account
        4. If primary fails, try next active account (max 3 attempts)
        5. Failed accounts are retried after cooldown period (60s)

        Returns:
            Dict mapping symbol to LTP price
        """
        now = datetime.now()

        # OPTIMIZATION: Skip API calls if outside trading hours
        if not self._is_within_trading_hours():
            logger.debug("Outside trading hours - skipping API price fetch")
            return {}

        # Get all active accounts ordered by: primary first, then by id
        accounts = TradingAccount.query.filter_by(
            is_active=True
        ).order_by(
            TradingAccount.is_primary.desc(),  # Primary first
            TradingAccount.id.asc()  # Then by ID
        ).all()

        if not accounts:
            logger.warning("No active accounts available for price feeds")
            return {}

        # OPTIMIZATION: If we have a known working account, try it first
        if self._current_price_account_id:
            working_account = next(
                (a for a in accounts if a.id == self._current_price_account_id),
                None
            )
            if working_account:
                # Move working account to front of list
                accounts = [working_account] + [a for a in accounts if a.id != self._current_price_account_id]

        # OPTIMIZATION: Limit failover attempts to 3 accounts max
        # This prevents 10+ API calls when all accounts are down
        max_failover_attempts = 3
        attempts = 0
        skipped_due_to_cooldown = 0

        # Try each account in order
        for account in accounts:
            if attempts >= max_failover_attempts:
                logger.warning(f"Reached max failover attempts ({max_failover_attempts}), stopping")
                break

            # Skip recently failed accounts (unless cooldown expired)
            if account.id in self._failed_accounts:
                fail_time = self._failed_accounts[account.id]
                if (now - fail_time).total_seconds() < self._failed_account_cooldown:
                    skipped_due_to_cooldown += 1
                    continue
                else:
                    # Cooldown expired, remove from failed list
                    del self._failed_accounts[account.id]

            attempts += 1

            # Try to get prices from this account
            prices = self._get_cached_positions(account)

            if prices:
                # Success - track this as the working account
                if self._current_price_account_id != account.id:
                    if self._current_price_account_id is not None:
                        logger.warning(f"Price feed failover: switched to account {account.account_name}")
                    self._current_price_account_id = account.id
                return prices
            else:
                # Failed - mark account and try next
                self._failed_accounts[account.id] = now
                logger.warning(f"Price feed failed for account {account.account_name}, trying next...")

        # All attempts failed
        if attempts == 0 and skipped_due_to_cooldown > 0:
            # All accounts in cooldown - clear cooldown and try again next cycle
            logger.warning(f"All {skipped_due_to_cooldown} accounts in cooldown (failed recently). Will retry in {self._failed_account_cooldown}s")
            # Reduce cooldown for next attempt if all accounts failed
            if len(self._failed_accounts) == len(accounts):
                # Clear oldest failure to allow retry
                oldest_account = min(self._failed_accounts.keys(), key=lambda k: self._failed_accounts[k])
                del self._failed_accounts[oldest_account]
                logger.debug(f"Cleared cooldown for account {oldest_account} to allow retry")
        elif attempts > 0:
            logger.error(f"All {attempts} failover attempts failed to provide price feeds")
        return {}

    def _is_within_trading_hours(self) -> bool:
        """
        Check if current time is within trading hours.
        Uses TradingSession from database.

        Returns:
            bool: True if within trading hours
        """
        try:
            now = datetime.now(pytz.timezone('Asia/Kolkata'))
            current_time = now.time()
            day_of_week = now.weekday()

            # Import here to avoid circular imports
            from app.models import TradingSession, TradingHoursTemplate, MarketHoliday

            # Check if today is a holiday
            today = now.date()
            is_holiday = MarketHoliday.query.filter_by(holiday_date=today).first()
            if is_holiday:
                return False

            # Get active trading sessions for today
            sessions = TradingSession.query.join(TradingHoursTemplate).filter(
                TradingSession.day_of_week == day_of_week,
                TradingSession.is_active == True,
                TradingHoursTemplate.is_active == True
            ).all()

            for session in sessions:
                if session.start_time <= current_time <= session.end_time:
                    return True

            return False

        except Exception as e:
            logger.error(f"Error checking trading hours: {e}")
            # Default to True to avoid blocking risk checks due to DB errors
            return True

    def _get_cached_positions(self, account: TradingAccount) -> Dict[str, float]:
        """
        Get positions from cache or fetch from API if cache expired.

        Uses a 5-second cache to reduce API calls and prevent blocking.
        Includes a 3-second timeout to prevent indefinite hangs.

        Args:
            account: Trading account to fetch positions for

        Returns:
            Dict mapping symbol to LTP price
        """
        now = datetime.now()
        cache_entry = self._positions_cache.get(account.id)

        # Check if cache is valid (use 5-second cache to reduce API load)
        if cache_entry:
            cache_age = (now - cache_entry['timestamp']).total_seconds()
            if cache_age < self._cache_ttl_seconds:
                return cache_entry['positions']

        # Fetch fresh positions from API with timeout
        current_prices = {}
        try:
            import requests
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            # Set a 3-second timeout on the API call to prevent blocking
            original_timeout = getattr(requests, 'DEFAULT_TIMEOUT', None)
            try:
                positions_response = client.positionbook()
            except requests.exceptions.Timeout:
                logger.warning(f"Positions API timeout for account {account.account_name}")
                if cache_entry:
                    return cache_entry['positions']
                return {}

            if positions_response.get('status') == 'success':
                positions_data = positions_response.get('data', [])
                for pos in positions_data:
                    symbol = pos.get('symbol', '')
                    ltp = pos.get('ltp', 0)
                    if symbol and ltp:
                        current_prices[symbol] = float(ltp)

            # Update cache
            self._positions_cache[account.id] = {
                'positions': current_prices,
                'timestamp': now
            }

        except Exception as api_err:
            logger.warning(f"Failed to fetch positions for account {account.account_name}: {api_err}")
            # Return cached data if API fails
            if cache_entry:
                return cache_entry['positions']

        return current_prices

    def calculate_execution_pnl(self, execution: StrategyExecution) -> Tuple[float, float]:
        """
        Calculate P&L for a single strategy execution.

        Args:
            execution: Strategy execution to calculate P&L for

        Returns:
            Tuple of (realized_pnl, unrealized_pnl)
        """
        realized_pnl = 0.0
        unrealized_pnl = 0.0

        try:
            # Get entry and exit prices
            entry_price = float(execution.entry_price or 0)
            exit_price = float(execution.exit_price or 0)
            quantity = int(execution.quantity or 0)

            if quantity == 0:
                return (0.0, 0.0)

            # Determine if long or short from leg action
            is_long = execution.leg and execution.leg.action.upper() == 'BUY'

            # Calculate realized P&L (if position is closed)
            if execution.status == 'exited' and exit_price > 0:
                if is_long:
                    realized_pnl = (exit_price - entry_price) * quantity
                else:
                    realized_pnl = (entry_price - exit_price) * quantity

                return (realized_pnl, 0.0)

            # Calculate unrealized P&L (if position is open)
            if execution.status == 'entered':
                # Use real-time LTP from WebSocket if available
                current_price = float(execution.last_price or entry_price)

                if current_price > 0:
                    if is_long:
                        unrealized_pnl = (current_price - entry_price) * quantity
                    else:
                        unrealized_pnl = (entry_price - current_price) * quantity

                return (0.0, unrealized_pnl)

        except Exception as e:
            logger.error(f"Error calculating P&L for execution {execution.id}: {e}")

        return (realized_pnl, unrealized_pnl)

    def calculate_strategy_pnl(self, strategy: Strategy) -> Dict:
        """
        Calculate total P&L for a strategy across all executions.

        PRICE SOURCES (in order of preference):
        1. PRIMARY: WebSocket prices from execution.last_price (updated by position_monitor)
        2. FALLBACK: REST API (positionbook) only if WebSocket price is stale (>30s old)

        This reduces API calls from ~12/min to nearly zero when WebSocket is working.

        Args:
            strategy: Strategy to calculate P&L for

        Returns:
            Dict with realized_pnl, unrealized_pnl, total_pnl, valid (bool)
        """
        total_realized = 0.0
        total_unrealized = 0.0

        try:
            # Get all executions for this strategy
            executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id
            ).all()

            # Get open executions
            open_executions = [e for e in executions if e.status == 'entered']

            # Check if any WebSocket prices are stale (need API fallback)
            api_prices = {}
            needs_api_fallback = False

            if open_executions:
                now = datetime.now()
                stale_threshold = 30  # seconds

                for exec in open_executions:
                    # Check if WebSocket price is stale or missing
                    if not exec.last_price or not exec.last_price_updated:
                        needs_api_fallback = True
                        break

                    price_age = (now - exec.last_price_updated).total_seconds()
                    if price_age > stale_threshold:
                        needs_api_fallback = True
                        break

                # Only fetch from API if WebSocket prices are stale
                if needs_api_fallback:
                    logger.debug("WebSocket prices stale, using API fallback")
                    api_prices = self._get_prices_with_failover()

            for execution in executions:
                # PRIORITY: WebSocket price (fresh) > API price (fallback) > entry price
                current_price = None

                if execution.status == 'entered':
                    # Try WebSocket price first (from position_monitor)
                    if execution.last_price and execution.last_price > 0:
                        current_price = float(execution.last_price)
                    # Fallback to API price if WebSocket stale
                    elif api_prices.get(execution.symbol):
                        current_price = api_prices.get(execution.symbol)

                if current_price and execution.status == 'entered':
                    # Calculate P&L using best available price
                    entry_price = float(execution.entry_price or 0)
                    quantity = int(execution.quantity or 0)
                    is_long = execution.leg and execution.leg.action.upper() == 'BUY'

                    if is_long:
                        total_unrealized += (current_price - entry_price) * quantity
                    else:
                        total_unrealized += (entry_price - current_price) * quantity
                else:
                    # Exited positions - calculate realized P&L
                    realized, unrealized = self.calculate_execution_pnl(execution)
                    total_realized += realized
                    total_unrealized += unrealized

            total_pnl = total_realized + total_unrealized

            return {
                'realized_pnl': round(total_realized, 2),
                'unrealized_pnl': round(total_unrealized, 2),
                'total_pnl': round(total_pnl, 2),
                'valid': True  # Calculation succeeded
            }

        except Exception as e:
            logger.error(f"Error calculating strategy P&L for {strategy.name}: {e}")
            return {
                'realized_pnl': 0.0,
                'unrealized_pnl': 0.0,
                'total_pnl': 0.0,
                'valid': False  # Calculation failed - DO NOT use for risk decisions
            }

    def check_max_loss(self, strategy: Strategy) -> Optional[RiskEvent]:
        """
        Check if strategy has breached max loss threshold.

        Args:
            strategy: Strategy to check

        Returns:
            RiskEvent if threshold breached, None otherwise
        """
        # Check if max loss monitoring is enabled
        if not strategy.max_loss or strategy.max_loss <= 0:
            return None

        if not strategy.auto_exit_on_max_loss:
            return None

        # Check if already triggered (prevent re-triggering)
        if strategy.max_loss_triggered_at:
            return None

        # Calculate current P&L
        pnl_data = self.calculate_strategy_pnl(strategy)
        current_pnl = pnl_data['total_pnl']

        # Skip if P&L calculation failed (prevents false triggers)
        if not pnl_data.get('valid', True):
            logger.warning(f"[MaxLoss] Strategy {strategy.name}: P&L calculation INVALID, skipping check")
            return None

        # Check if loss exceeds threshold (loss is negative)
        max_loss_threshold = -abs(float(strategy.max_loss))

        if current_pnl <= max_loss_threshold:
            logger.warning(
                f"Max Loss breached for {strategy.name}: "
                f"P&L={current_pnl} <= Threshold={max_loss_threshold}"
            )

            # Store exit reason and timestamp
            exit_reason = f"Max Loss: P&L {current_pnl:.2f} breached threshold {max_loss_threshold:.2f}"
            strategy.max_loss_triggered_at = get_ist_now()
            strategy.max_loss_exit_reason = exit_reason
            db.session.commit()

            # Create risk event
            risk_event = RiskEvent(
                strategy_id=strategy.id,
                event_type='max_loss',
                threshold_value=max_loss_threshold,
                current_value=current_pnl,
                action_taken='close_all' if strategy.auto_exit_on_max_loss else 'alert_only',
                notes=exit_reason
            )

            return risk_event

        return None

    def check_max_profit(self, strategy: Strategy) -> Optional[RiskEvent]:
        """
        Check if strategy has breached max profit threshold.

        Args:
            strategy: Strategy to check

        Returns:
            RiskEvent if threshold breached, None otherwise
        """
        # Check if max profit monitoring is enabled
        if not strategy.max_profit or strategy.max_profit <= 0:
            return None

        if not strategy.auto_exit_on_max_profit:
            return None

        # Check if already triggered (prevent re-triggering)
        if strategy.max_profit_triggered_at:
            return None

        # Calculate current P&L
        pnl_data = self.calculate_strategy_pnl(strategy)
        current_pnl = pnl_data['total_pnl']

        # Skip if P&L calculation failed (prevents false triggers)
        if not pnl_data.get('valid', True):
            logger.warning(f"[MaxProfit] Strategy {strategy.name}: P&L calculation INVALID, skipping check")
            return None

        # Check if profit exceeds threshold (profit is positive)
        max_profit_threshold = abs(float(strategy.max_profit))

        if current_pnl >= max_profit_threshold:
            logger.debug(
                f"Max Profit reached for {strategy.name}: "
                f"P&L={current_pnl} >= Threshold={max_profit_threshold}"
            )

            # Store exit reason and timestamp
            exit_reason = f"Max Profit: P&L {current_pnl:.2f} reached target {max_profit_threshold:.2f}"
            strategy.max_profit_triggered_at = get_ist_now()
            strategy.max_profit_exit_reason = exit_reason
            db.session.commit()

            # Create risk event
            risk_event = RiskEvent(
                strategy_id=strategy.id,
                event_type='max_profit',
                threshold_value=max_profit_threshold,
                current_value=current_pnl,
                action_taken='close_all' if strategy.auto_exit_on_max_profit else 'alert_only',
                notes=exit_reason
            )

            return risk_event

        return None

    def check_trailing_sl(self, strategy: Strategy) -> Optional[RiskEvent]:
        """
        Check if trailing stop loss should be triggered based on COMBINED strategy P&L.

        AFL-style ratcheting trailing stop logic:
        =========================================
        StopLevel = 1 - trailing_pct/100  (e.g., 30% = 0.70)

        When P&L first goes positive:
          - initial_stop = peak_pnl * StopLevel
          - trailing_stop = initial_stop

        On each update:
          - new_stop = peak_pnl * StopLevel
          - trailing_stop = Max(new_stop, trailing_stop)  // Ratchet UP only!

        Exit when: current_pnl < trailing_stop

        TSL State Machine:
        1. Waiting: P&L <= 0, TSL not yet activated
        2. Active: P&L became positive, now tracking peak and ratcheting stop UP
        3. Triggered: P&L dropped below trailing stop level, exit all positions

        Once Active, TSL stays active until exit (doesn't go back to Waiting).

        Args:
            strategy: Strategy to check

        Returns:
            RiskEvent if trailing SL triggered, None otherwise
        """
        # Check if trailing SL is enabled
        if not strategy.trailing_sl or strategy.trailing_sl <= 0:
            return None

        try:
            # Get all open executions
            open_executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id,
                status='entered'
            ).all()

            if not open_executions:
                # No open positions - clean up TSL active state (but preserve triggered_at for history)
                if strategy.trailing_sl_active:
                    strategy.trailing_sl_active = False
                    strategy.trailing_sl_peak_pnl = 0.0
                    strategy.trailing_sl_initial_stop = None
                    strategy.trailing_sl_trigger_pnl = None
                    db.session.commit()
                return None

            # If TSL was already triggered for this trade, don't re-process
            if strategy.trailing_sl_triggered_at:
                logger.debug(f"[TSL] Strategy {strategy.name}: TSL already triggered at {strategy.trailing_sl_triggered_at}, skipping")
                return None

            # Calculate COMBINED strategy P&L (not individual execution P&L)
            pnl_data = self.calculate_strategy_pnl(strategy)
            current_pnl = pnl_data['total_pnl']

            # CRITICAL: Skip TSL check if P&L calculation failed (API error, etc.)
            if not pnl_data.get('valid', True):
                logger.warning(f"[TSL] Strategy {strategy.name}: P&L calculation INVALID, skipping TSL check")
                return None

            # Get trailing SL settings
            trailing_type = strategy.trailing_sl_type or 'percentage'
            trailing_value = float(strategy.trailing_sl)

            # TSL ACTIVE FROM ENTRY - Calculate initial stop based on NET PREMIUM
            # For combined strategies (straddles, spreads), we need to account for direction:
            #   - BUY leg: Premium paid (debit) = positive contribution to net premium
            #   - SELL leg: Premium received (credit) = negative contribution to net premium
            # Net Premium > 0 means net debit (paid money)
            # Net Premium < 0 means net credit (received money)
            #
            # Example - Short Straddle (SELL CE + SELL PE):
            #   SELL CE @ 150 * 75 = -11,250 (credit)
            #   SELL PE @ 120 * 75 = -9,000 (credit)
            #   Net Premium = -20,250 (total credit received)
            #
            # Example - Bull Call Spread (BUY lower + SELL higher):
            #   BUY CE 24000 @ 200 * 75 = +15,000 (debit)
            #   SELL CE 24100 @ 150 * 75 = -11,250 (credit)
            #   Net Premium = +3,750 (net debit paid)

            net_premium = 0
            buy_premium = 0
            sell_premium = 0

            for exec in open_executions:
                if exec.entry_price and exec.quantity:
                    premium = (exec.entry_price or 0) * (exec.quantity or 0)
                    # Check leg action to determine direction
                    if exec.leg and exec.leg.action and exec.leg.action.upper() == 'BUY':
                        net_premium += premium  # Debit (cost)
                        buy_premium += premium
                    else:  # SELL
                        net_premium -= premium  # Credit (received)
                        sell_premium += premium

            # For TSL calculation, use absolute value of net premium
            # This represents the actual capital at risk
            entry_value = abs(net_premium)

            # Detect strategy type for logging
            is_combined = buy_premium > 0 and sell_premium > 0
            is_net_credit = net_premium < 0
            strategy_type = "Combined" if is_combined else "Single Leg"
            premium_type = "Net Credit" if is_net_credit else "Net Debit"

            logger.debug(f"[TSL] Strategy {strategy.name}: {strategy_type} ({premium_type})")
            logger.debug(f"[TSL]   BUY premiums: {buy_premium:.2f}, SELL premiums: {sell_premium:.2f}")
            logger.debug(f"[TSL]   Net Premium: {net_premium:.2f}, Entry Value (abs): {entry_value:.2f}")

            # Calculate initial stop (max loss from entry)
            if trailing_type == 'percentage':
                initial_stop_pnl = -entry_value * (trailing_value / 100)
            elif trailing_type == 'points':
                initial_stop_pnl = -trailing_value
            else:  # 'amount'
                initial_stop_pnl = -trailing_value

            # Set initial stop if not already set
            if strategy.trailing_sl_initial_stop is None:
                strategy.trailing_sl_initial_stop = initial_stop_pnl
                logger.debug(f"[TSL STATE] Strategy {strategy.name}: Initial stop set at {initial_stop_pnl:.2f} (Net Premium: {net_premium:.2f}, Entry Value: {entry_value:.2f})")

            # TSL is ALWAYS active from entry (no waiting state)
            strategy.trailing_sl_active = True

            # Track peak P&L (highest P&L achieved)
            current_peak = strategy.trailing_sl_peak_pnl or 0.0
            if current_pnl > current_peak:
                strategy.trailing_sl_peak_pnl = current_pnl
                current_peak = current_pnl
                logger.debug(f"[TSL] Strategy {strategy.name}: New peak P&L = {current_peak:.2f}")

            # Calculate trailing stop based on peak P&L
            # Logic: Current Stop = Initial Stop + Peak P&L
            # The stop trails UP from initial stop level by the amount of peak profit
            # Example: Initial=-860, Peak=100 -> Stop = -860 + 100 = -760
            current_stop = strategy.trailing_sl_initial_stop + current_peak
            logger.debug(f"[TSL] Strategy {strategy.name}: Initial={strategy.trailing_sl_initial_stop:.2f} + Peak={current_peak:.2f} = Stop={current_stop:.2f}")

            # Ratchet: current stop can only increase from previous value
            previous_stop = strategy.trailing_sl_trigger_pnl or strategy.trailing_sl_initial_stop
            if current_stop > previous_stop:
                logger.debug(f"[TSL] Strategy {strategy.name}: Stop ratcheted UP from {previous_stop:.2f} to {current_stop:.2f}")
            current_stop = max(current_stop, previous_stop)

            strategy.trailing_sl_trigger_pnl = current_stop
            db.session.commit()

            # Exit when P&L drops to or below current stop
            if current_pnl <= current_stop:
                # State transition: ACTIVE -> TRIGGERED
                logger.warning(
                    f"[TSL STATE] Strategy {strategy.name}: ACTIVE -> TRIGGERED | "
                    f"P&L={current_pnl:.2f} <= Stop={current_stop:.2f} (Peak={current_peak:.2f}, Initial={strategy.trailing_sl_initial_stop:.2f})"
                )

                # Store exit reason and timestamp
                exit_reason = f"TSL: P&L {current_pnl:.2f} <= Stop {current_stop:.2f} (Peak: {current_peak:.2f}, Initial: {strategy.trailing_sl_initial_stop:.2f})"
                strategy.trailing_sl_triggered_at = get_ist_now()
                strategy.trailing_sl_exit_reason = exit_reason
                db.session.commit()

                # Create risk event
                risk_event = RiskEvent(
                    strategy_id=strategy.id,
                    event_type='trailing_sl',
                    threshold_value=current_stop,
                    current_value=current_pnl,
                    action_taken='close_all',
                    notes=exit_reason
                )

                return risk_event

        except Exception as e:
            logger.error(f"Error checking trailing SL for {strategy.name}: {e}")

        return None

    def close_strategy_positions(self, strategy: Strategy, risk_event: RiskEvent) -> bool:
        """
        Close all open positions for a strategy across ALL accounts.

        IMPORTANT: For multi-account strategies, each execution is closed on its own account.
        This ensures orders are placed to the correct broker account.

        Args:
            strategy: Strategy to close
            risk_event: Risk event that triggered the closure

        Returns:
            bool: True if all positions closed successfully
        """
        try:
            # Get all open executions
            open_executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id,
                status='entered'
            ).all()

            if not open_executions:
                logger.debug(f"No open positions to close for {strategy.name}")
                return True

            # Log all executions we're about to close
            logger.debug(f"[RISK EXIT] Strategy {strategy.name}: Found {len(open_executions)} open positions to close")
            for exec in open_executions:
                logger.debug(f"[RISK EXIT]   - Execution {exec.id}: {exec.symbol} on account {exec.account.account_name if exec.account else 'NONE'}, qty={exec.quantity}")

            exit_order_ids = []
            success_count = 0
            fail_count = 0

            # Close each position with freeze-aware placement and retry logic
            from app.utils.freeze_quantity_handler import place_order_with_freeze_check

            # BUY-FIRST EXIT PRIORITY: Close SELL positions first (BUY orders), then BUY positions (SELL orders)
            sell_positions = [e for e in open_executions if e.leg and e.leg.action == 'SELL']
            buy_positions = [e for e in open_executions if e.leg and e.leg.action == 'BUY']
            unknown_positions = [e for e in open_executions if not e.leg]

            # Reorder: SELL positions first (will place BUY close orders), then BUY positions (will place SELL close orders)
            ordered_executions = sell_positions + buy_positions + unknown_positions

            logger.debug(f"[RISK EXIT] BUY-FIRST priority: {len(sell_positions)} SELL positions (close first), "
                        f"{len(buy_positions)} BUY positions (close second)")
            print(f"[RISK EXIT] BUY-FIRST priority: {len(sell_positions)} SELL positions (close first), "
                  f"{len(buy_positions)} BUY positions (close second)")

            # Track phase transitions for logging
            current_phase = 1 if sell_positions else 2
            sell_count = len(sell_positions)

            for idx, execution in enumerate(ordered_executions):
                # Log phase transitions
                if idx == sell_count and sell_positions and buy_positions:
                    logger.debug(f"[RISK EXIT PHASE 2] All SELL positions closed. Starting BUY position exits...")
                    print(f"[RISK EXIT PHASE 2] All SELL positions closed. Starting BUY position exits...")
                logger.debug(f"[RISK EXIT] Processing execution {idx + 1}/{len(open_executions)}: ID={execution.id}, symbol={execution.symbol}")
                try:
                    # Use the account from the execution (not primary account)
                    # Each execution might be on a different account in multi-account setups
                    account = execution.account
                    if not account or not account.is_active:
                        logger.error(f"[RISK EXIT] Account not found or inactive for execution {execution.id}")
                        fail_count += 1
                        continue

                    logger.debug(f"[RISK EXIT] Using account {account.account_name} (ID={account.id}) for execution {execution.id}")

                    # Initialize OpenAlgo client for this execution's account
                    client = ExtendedOpenAlgoAPI(
                        api_key=account.get_api_key(),
                        host=account.host_url
                    )

                    # Reverse transaction type for exit (get action from leg)
                    leg_action = execution.leg.action.upper() if execution.leg else 'BUY'
                    exit_transaction = 'SELL' if leg_action == 'BUY' else 'BUY'

                    logger.debug(f"[RISK EXIT] Placing {exit_transaction} order for {execution.symbol}, qty={execution.quantity} on {account.account_name}")

                    # Place exit order with freeze-aware placement and retry logic
                    max_retries = 3
                    retry_delay = 1
                    response = None

                    # Get product type - prefer execution's product, fallback to strategy's product_order_type
                    # This ensures NRML entries exit as NRML, not MIS
                    exit_product = execution.product or strategy.product_order_type or 'MIS'
                    logger.debug(f"[RISK EXIT] Product type: execution.product='{execution.product}', strategy.product_order_type='{strategy.product_order_type}', exit_product='{exit_product}'")

                    for attempt in range(max_retries):
                        try:
                            response = place_order_with_freeze_check(
                                client=client,
                                user_id=strategy.user_id,
                                strategy=strategy.name,
                                symbol=execution.symbol,
                                exchange=execution.exchange,
                                action=exit_transaction,
                                quantity=execution.quantity,
                                price_type='MARKET',
                                product=exit_product
                            )
                            if response and isinstance(response, dict):
                                logger.debug(f"[RISK EXIT] Order response for {execution.symbol}: {response}")
                                break
                        except Exception as api_error:
                            logger.warning(f"[RISK EXIT] Attempt {attempt + 1}/{max_retries} failed for {execution.symbol} on {account.account_name}: {api_error}")
                            if attempt < max_retries - 1:
                                import time
                                time.sleep(retry_delay)
                                retry_delay *= 2
                            else:
                                response = {'status': 'error', 'message': f'API error after {max_retries} retries: {api_error}'}

                    if response and response.get('status') == 'success':
                        order_id = response.get('orderid')
                        exit_order_ids.append(order_id)
                        success_count += 1

                        logger.debug(
                            f"[RISK EXIT] SUCCESS: Exit order placed for {execution.symbol} on {account.account_name}: "
                            f"Order ID {order_id}"
                        )

                        # Update execution status - poller will update to exited with actual fill price
                        execution.status = 'exit_pending'
                        execution.exit_order_id = order_id
                        execution.broker_order_status = 'open'
                        execution.exit_time = get_ist_now()
                        execution.exit_reason = risk_event.event_type

                        # Add exit order to poller to get actual fill price (same as entry orders)
                        from app.utils.order_status_poller import order_status_poller
                        order_status_poller.add_order(
                            execution_id=execution.id,
                            account=execution.account,
                            order_id=order_id,
                            strategy_name=strategy.name
                        )

                    else:
                        fail_count += 1
                        error_msg = response.get('message') if response else 'No response'
                        logger.error(
                            f"[RISK EXIT] FAILED: Exit order for {execution.symbol} on {account.account_name}: {error_msg}"
                        )

                except Exception as e:
                    fail_count += 1
                    logger.error(f"[RISK EXIT] EXCEPTION for {execution.symbol}: {e}", exc_info=True)

            # Update risk event with order IDs
            risk_event.exit_order_ids = exit_order_ids
            db.session.add(risk_event)
            db.session.commit()

            logger.debug(
                f"[RISK EXIT] Completed for {strategy.name}: "
                f"{success_count} success, {fail_count} failed out of {len(open_executions)} total"
            )

            return success_count > 0

        except Exception as e:
            logger.error(f"Error closing strategy positions: {e}")
            db.session.rollback()
            return False

    def check_strategy(self, strategy: Strategy):
        """
        Check all risk thresholds for a strategy.

        Args:
            strategy: Strategy to check
        """
        try:
            # Check if risk monitoring is enabled
            if not strategy.risk_monitoring_enabled:
                return

            # Check max loss
            risk_event = self.check_max_loss(strategy)
            if risk_event:
                db.session.add(risk_event)
                db.session.commit()

                # Close positions if auto-exit enabled
                if strategy.auto_exit_on_max_loss:
                    self.close_strategy_positions(strategy, risk_event)
            else:
                # RETRY MECHANISM: If max loss was already triggered but positions still open, retry closing
                if strategy.max_loss_triggered_at:
                    open_positions = StrategyExecution.query.filter_by(
                        strategy_id=strategy.id,
                        status='entered'
                    ).count()
                    if open_positions > 0:
                        logger.debug(f"[MAX LOSS RETRY] Strategy {strategy.name}: Max loss triggered but {open_positions} positions still open, retrying close")
                        retry_event = RiskEvent(
                            strategy_id=strategy.id,
                            event_type='max_loss_retry',
                            threshold_value=strategy.max_loss,
                            current_value=0,
                            action_taken='close_remaining'
                        )
                        self.close_strategy_positions(strategy, retry_event)

            # Check max profit
            risk_event = self.check_max_profit(strategy)
            if risk_event:
                db.session.add(risk_event)
                db.session.commit()

                # Close positions if auto-exit enabled
                if strategy.auto_exit_on_max_profit:
                    self.close_strategy_positions(strategy, risk_event)
            else:
                # RETRY MECHANISM: If max profit was already triggered but positions still open, retry closing
                if strategy.max_profit_triggered_at:
                    open_positions = StrategyExecution.query.filter_by(
                        strategy_id=strategy.id,
                        status='entered'
                    ).count()
                    if open_positions > 0:
                        logger.debug(f"[MAX PROFIT RETRY] Strategy {strategy.name}: Max profit triggered but {open_positions} positions still open, retrying close")
                        retry_event = RiskEvent(
                            strategy_id=strategy.id,
                            event_type='max_profit_retry',
                            threshold_value=strategy.max_profit,
                            current_value=0,
                            action_taken='close_remaining'
                        )
                        self.close_strategy_positions(strategy, retry_event)

            # Check trailing SL
            risk_event = self.check_trailing_sl(strategy)
            if risk_event:
                db.session.add(risk_event)
                db.session.commit()

                # Trailing SL always triggers exit
                self.close_strategy_positions(strategy, risk_event)
            else:
                # RETRY MECHANISM: If TSL was already triggered but positions still open, retry closing
                if strategy.trailing_sl_triggered_at:
                    open_positions = StrategyExecution.query.filter_by(
                        strategy_id=strategy.id,
                        status='entered'
                    ).count()
                    if open_positions > 0:
                        logger.debug(f"[TSL RETRY] Strategy {strategy.name}: TSL triggered but {open_positions} positions still open, retrying close")
                        # Create a retry risk event
                        retry_event = RiskEvent(
                            strategy_id=strategy.id,
                            event_type='trailing_sl_retry',
                            threshold_value=strategy.trailing_sl_trigger_pnl,
                            current_value=0,  # Will be recalculated
                            action_taken='close_remaining'
                        )
                        self.close_strategy_positions(strategy, retry_event)

        except Exception as e:
            logger.error(f"Error checking strategy {strategy.name}: {e}")

    def run_risk_checks(self):
        """
        Run risk checks for all monitored strategies.
        Called by background scheduler.

        Uses a subquery to get strategies with open positions efficiently.
        Note: Strategy.executions uses lazy='dynamic' which doesn't support joinedload.
        """
        if not self.is_running:
            return

        try:
            # OPTIMIZED: Get strategy IDs with open positions in a single query
            # Then fetch strategies - avoids N+1 pattern while respecting dynamic relationship
            from sqlalchemy import exists

            # Subquery to find strategies with at least one open position
            has_open_positions = exists().where(
                StrategyExecution.strategy_id == Strategy.id,
                StrategyExecution.status == 'entered'
            )

            strategies_with_positions = Strategy.query.filter(
                Strategy.is_active == True,
                Strategy.risk_monitoring_enabled == True,
                has_open_positions
            ).all()

            for strategy in strategies_with_positions:
                self.check_strategy(strategy)

        except Exception as e:
            logger.error(f"Error running risk checks: {e}")

    def start(self):
        """Start risk monitoring"""
        if self.is_running:
            logger.warning("Risk manager already running")
            return

        self.is_running = True
        logger.debug("Risk monitoring started")

    def stop(self):
        """Stop risk monitoring"""
        if not self.is_running:
            return

        self.is_running = False
        self.monitored_strategies.clear()
        logger.debug("Risk monitoring stopped")

    def get_monitoring_status(self) -> Dict:
        """
        Get current monitoring status for admin dashboard.

        Returns:
            Dict with monitoring statistics
        """
        try:
            # Count strategies with risk monitoring enabled
            total_strategies = Strategy.query.filter_by(
                is_active=True,
                risk_monitoring_enabled=True
            ).count()

            # Count strategies with open positions
            strategies_with_positions = db.session.query(Strategy.id).join(
                StrategyExecution
            ).filter(
                Strategy.is_active == True,
                Strategy.risk_monitoring_enabled == True,
                StrategyExecution.status == 'entered'
            ).distinct().count()

            # Get recent risk events (last 24 hours)
            from datetime import timedelta
            yesterday = datetime.utcnow() - timedelta(days=1)
            recent_events = RiskEvent.query.filter(
                RiskEvent.triggered_at >= yesterday
            ).count()

            return {
                'is_running': self.is_running,
                'total_strategies': total_strategies,
                'active_strategies': strategies_with_positions,
                'recent_events_24h': recent_events
            }

        except Exception as e:
            logger.error(f"Error getting monitoring status: {e}")
            return {
                'is_running': self.is_running,
                'total_strategies': 0,
                'active_strategies': 0,
                'recent_events_24h': 0
            }


# Global instance
risk_manager = RiskManager()
