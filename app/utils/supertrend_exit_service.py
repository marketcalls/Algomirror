"""
Background Service for Supertrend-based Exit Monitoring
Monitors strategies with Supertrend exit enabled and triggers exits on signal
"""

import threading
import logging
from datetime import datetime, time, timedelta
import time as time_module
from typing import Dict, Any, List
from apscheduler.schedulers.background import BackgroundScheduler
import pytz

# IST timezone for storing timestamps
IST = pytz.timezone('Asia/Kolkata')

def get_ist_now():
    """Get current time in IST (naive datetime for DB storage)"""
    return datetime.now(IST).replace(tzinfo=None)

from app.models import Strategy, StrategyExecution, TradingAccount
from app.utils.supertrend import calculate_supertrend
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
import pandas as pd
import numpy as np

logger = logging.getLogger(__name__)


class SupertrendExitService:
    """
    Background service that monitors strategies with Supertrend exit enabled
    and triggers parallel exits when breakout/breakdown occurs
    """

    _instance = None
    _lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if self._initialized:
            return

        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
        self.is_running = False
        self.monitoring_strategies = {}  # strategy_id -> last_check_time
        self._initialized = True

        logger.debug("Supertrend Exit Service initialized")

    def start_service(self):
        """Start the background service"""
        if not self.is_running:
            # Check if scheduler is actually already running
            if self.scheduler.running:
                self.is_running = True
                logger.debug("Supertrend Exit Service already running")
                return

            self.scheduler.start()
            self.is_running = True

            # Schedule monitoring at the start of every minute (second=0) for precise candle close detection
            # This ensures checks happen exactly at 9:27:00, 9:30:00, etc.
            # The should_check_strategy function filters based on strategy timeframe
            # max_instances=1 prevents overlapping executions if previous run takes longer than 1 minute
            self.scheduler.add_job(
                func=self.monitor_strategies,
                trigger='cron',
                second=0,  # Run at :00 of every minute
                id='supertrend_monitor',
                replace_existing=True,
                max_instances=1,
                coalesce=True  # Skip missed runs if system was busy
            )

            logger.debug("Supertrend Exit Service started - monitoring at start of each minute")

    def stop_service(self):
        """Stop the background service"""
        if self.is_running:
            self.scheduler.shutdown(wait=False)
            self.is_running = False
            logger.debug("Supertrend Exit Service stopped")

    def monitor_strategies(self):
        """
        Monitor all strategies with Supertrend exit enabled
        Check each strategy based on its configured timeframe
        """
        import uuid
        cycle_id = str(uuid.uuid4())[:8]  # Unique ID for this execution cycle

        try:
            from app import create_app

            # Create Flask app context
            app = create_app()
            with app.app_context():
                from app import db

                logger.info(f"[SUPERTREND CYCLE {cycle_id}] === Starting monitor_strategies ===")

                # Track strategies processed in THIS execution cycle to prevent double-processing
                # This prevents the RETRY loop from re-processing strategies that were just triggered
                strategies_processed_this_cycle = set()

                # Get all strategies with Supertrend exit enabled and not yet triggered
                strategies = Strategy.query.filter_by(
                    supertrend_exit_enabled=True,
                    supertrend_exit_triggered=False,
                    is_active=True
                ).all()

                logger.info(f"[SUPERTREND CYCLE {cycle_id}] Found {len(strategies)} non-triggered strategies")

                if strategies:
                    for strategy in strategies:
                        try:
                            # Check if we should monitor this strategy based on timeframe
                            should_check = self.should_check_strategy(strategy)
                            logger.info(f"[SUPERTREND CYCLE {cycle_id}] Strategy {strategy.id} ({strategy.name}): should_check={should_check}")

                            if should_check:
                                # Track that we're processing this strategy in this cycle
                                strategies_processed_this_cycle.add(strategy.id)
                                logger.info(f"[SUPERTREND CYCLE {cycle_id}] Strategy {strategy.id}: Added to processed set, calling check_supertrend_exit")
                                self.check_supertrend_exit(strategy, app)
                                logger.info(f"[SUPERTREND CYCLE {cycle_id}] Strategy {strategy.id}: check_supertrend_exit completed")
                        except Exception as e:
                            logger.error(f"Error monitoring strategy {strategy.id}: {e}", exc_info=True)

                logger.info(f"[SUPERTREND CYCLE {cycle_id}] Processed set after first loop: {strategies_processed_this_cycle}")

                # RETRY MECHANISM: Check strategies where Supertrend was triggered but positions still open
                # Only retry if enough time has passed since the trigger (to avoid race conditions)
                triggered_strategies = Strategy.query.filter_by(
                    supertrend_exit_enabled=True,
                    supertrend_exit_triggered=True,
                    is_active=True
                ).all()

                logger.info(f"[SUPERTREND CYCLE {cycle_id}] Found {len(triggered_strategies)} triggered strategies for RETRY check")

                for strategy in triggered_strategies:
                    try:
                        # CRITICAL: Skip if this strategy was just processed in the first loop
                        # This prevents double-trigger within the same execution cycle
                        if strategy.id in strategies_processed_this_cycle:
                            logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: SKIPPING - in processed set")
                            continue

                        # Skip retry if trigger was too recent (within 30 seconds) to avoid race conditions
                        if strategy.supertrend_exit_triggered_at:
                            seconds_since_trigger = (get_ist_now() - strategy.supertrend_exit_triggered_at).total_seconds()
                            logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: triggered_at={strategy.supertrend_exit_triggered_at}, seconds_since={seconds_since_trigger:.2f}")
                            if seconds_since_trigger < 30:
                                logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: SKIPPING - only {seconds_since_trigger:.0f}s since trigger")
                                continue
                        else:
                            # If triggered_at is None but triggered=True, skip to avoid issues
                            logger.warning(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: triggered=True but triggered_at is None, SKIPPING")
                            continue

                        # Check if there are still open positions that don't have exit orders
                        open_positions = StrategyExecution.query.filter_by(
                            strategy_id=strategy.id,
                            status='entered'
                        ).filter(
                            StrategyExecution.exit_order_id.is_(None)  # Only positions without exit orders
                        ).all()

                        # Filter out rejected/cancelled
                        open_positions = [
                            pos for pos in open_positions
                            if not (hasattr(pos, 'broker_order_status') and
                                   pos.broker_order_status in ['rejected', 'cancelled'])
                        ]

                        logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: {len(open_positions)} open positions without exit orders")

                        if open_positions:
                            for pos in open_positions:
                                logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: Position {pos.id} - {pos.symbol}, status={pos.status}, exit_order_id={pos.exit_order_id}")
                            logger.info(f"[SUPERTREND CYCLE {cycle_id}] RETRY Strategy {strategy.id}: Calling trigger_sequential_exit")
                            self.trigger_sequential_exit(strategy, f"Supertrend RETRY - {len(open_positions)} positions remaining", app)
                    except Exception as e:
                        logger.error(f"Error retrying Supertrend exit for strategy {strategy.id}: {e}", exc_info=True)

                logger.info(f"[SUPERTREND CYCLE {cycle_id}] === Completed monitor_strategies ===")

        except Exception as e:
            logger.error(f"Error in monitor_strategies: {e}", exc_info=True)

    def should_check_strategy(self, strategy: Strategy) -> bool:
        """
        Determine if this is a candle close time for the strategy's timeframe.
        Called at :00 seconds of each minute via cron trigger.

        For 3m: checks at :00, :03, :06, :09, :12, :15, :18, :21, :24, :27, :30, etc.
        For 5m: checks at :00, :05, :10, :15, :20, :25, :30, :35, :40, :45, :50, :55
        For 10m: checks at :00, :10, :20, :30, :40, :50
        For 15m: checks at :00, :15, :30, :45
        """
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        current_minute = now.minute

        # Map timeframe to minutes
        timeframe_minutes = {
            '3m': 3,
            '5m': 5,
            '10m': 10,
            '15m': 15
        }

        interval_minutes = timeframe_minutes.get(strategy.supertrend_timeframe, 5)

        # Check if current minute aligns with the timeframe (candle close)
        return (current_minute % interval_minutes) == 0

    def check_supertrend_exit(self, strategy: Strategy, app):
        """
        Check if Supertrend exit condition is met for a strategy
        """
        with app.app_context():
            from app import db

            try:
                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Starting check_supertrend_exit")
                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Config - timeframe={strategy.supertrend_timeframe}, period={strategy.supertrend_period}, multiplier={strategy.supertrend_multiplier}, exit_type={strategy.supertrend_exit_type}")

                # Update last check time
                self.monitoring_strategies[strategy.id] = datetime.now(pytz.timezone('Asia/Kolkata'))

                # Check if strategy has open positions
                open_positions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).all()

                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Found {len(open_positions)} positions with status='entered'")

                # Filter out rejected/cancelled
                open_positions = [
                    pos for pos in open_positions
                    if not (hasattr(pos, 'broker_order_status') and
                           pos.broker_order_status in ['rejected', 'cancelled'])
                ]

                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: After filtering rejected/cancelled: {len(open_positions)} positions")

                if not open_positions:
                    logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: NO OPEN POSITIONS - skipping exit check")
                    return

                for pos in open_positions:
                    logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Position {pos.id} - {pos.symbol}, leg_id={pos.leg_id}, qty={pos.quantity}, broker_status={getattr(pos, 'broker_order_status', 'N/A')}")

                # Get set of leg IDs that have open positions
                # A leg is considered "open" if ANY account has an open position for it
                open_leg_ids = set(pos.leg_id for pos in open_positions if pos.leg_id)
                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: {len(open_positions)} open positions across {len(open_leg_ids)} legs (leg_ids: {open_leg_ids})")

                # Fetch combined spread data ONLY for legs with open positions
                # This ensures closed legs don't affect the Supertrend calculation
                spread_data = self.fetch_combined_spread_data(strategy, open_leg_ids=open_leg_ids)

                if spread_data is None:
                    logger.warning(f"[CHECK_EXIT] Strategy {strategy.id}: spread_data is None - cannot calculate Supertrend")
                    return

                if len(spread_data) < strategy.supertrend_period + 5:
                    logger.warning(f"[CHECK_EXIT] Strategy {strategy.id}: Insufficient data - got {len(spread_data)} bars, need {strategy.supertrend_period + 5}")
                    return

                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Got {len(spread_data)} bars of spread data")

                # Calculate Supertrend on spread OHLC
                # NOTE: Direction is calculated based on CLOSE price only (not high/low)
                high = spread_data['high'].values
                low = spread_data['low'].values
                close = spread_data['close'].values

                trend, direction, long, short = calculate_supertrend(
                    high, low, close,
                    period=strategy.supertrend_period,
                    multiplier=strategy.supertrend_multiplier
                )

                # Get latest values from COMPLETED candle (checked on candle close only)
                latest_close = close[-1]
                latest_supertrend = trend[-1]
                latest_direction = direction[-1]  # Direction based on CLOSE crossing Supertrend

                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Supertrend values - close={latest_close:.2f}, ST={latest_supertrend:.2f}, direction={latest_direction} ({('BULLISH/UP' if latest_direction == -1 else 'BEARISH/DOWN')})")
                logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: Exit type={strategy.supertrend_exit_type}, Looking for direction={-1 if strategy.supertrend_exit_type == 'breakout' else 1}")

                # Check for exit signal based ONLY on close price vs Supertrend
                # Exit triggers on candle close, executes immediately
                #
                # Pine Script direction convention:
                #   direction = -1: Bullish (Up direction, green) - close crossed ABOVE supertrend
                #   direction =  1: Bearish (Down direction, red) - close crossed BELOW supertrend
                should_exit = False
                exit_reason = None

                if strategy.supertrend_exit_type == 'breakout':
                    # Breakout: CLOSE crossed ABOVE Supertrend (direction = -1 in Pine Script)
                    # Checked on candle close only, not intrabar
                    if latest_direction == -1:  # Bullish - price above supertrend
                        should_exit = True
                        logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: BREAKOUT condition MET - direction=-1 (bullish)")
                        exit_reason = f'supertrend_breakout (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'
                        logger.debug(f"Strategy {strategy.id}: Supertrend BREAKOUT - Close crossed above ST on candle close")

                elif strategy.supertrend_exit_type == 'breakdown':
                    # Breakdown: CLOSE crossed BELOW Supertrend (direction = 1 in Pine Script)
                    # Checked on candle close only, not intrabar
                    if latest_direction == 1:  # Bearish - price below supertrend
                        should_exit = True
                        logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: BREAKDOWN condition MET - direction=1 (bearish)")
                        exit_reason = f'supertrend_breakdown (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'

                if should_exit:
                    logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: EXIT SIGNAL DETECTED - calling trigger_sequential_exit")
                    self.trigger_sequential_exit(strategy, exit_reason, app)
                else:
                    logger.info(f"[CHECK_EXIT] Strategy {strategy.id}: NO EXIT SIGNAL - direction={latest_direction}, exit_type={strategy.supertrend_exit_type}, needed={-1 if strategy.supertrend_exit_type == 'breakout' else 1}")

            except Exception as e:
                logger.error(f"Error checking Supertrend exit for strategy {strategy.id}: {e}", exc_info=True)

    def fetch_combined_spread_data(self, strategy: Strategy, open_leg_ids: set = None) -> pd.DataFrame:
        """
        Fetch real-time combined spread data for strategy legs
        Similar to tradingview routes but optimized for exit monitoring

        Args:
            strategy: Strategy object
            open_leg_ids: Optional set of leg IDs that have open positions.
                         If provided, only these legs will be included in the spread calculation.
                         This ensures closed legs don't affect the Supertrend calculation.
        """
        try:
            from app.models import StrategyLeg, StrategyExecution

            # Get strategy legs
            legs = StrategyLeg.query.filter_by(strategy_id=strategy.id).all()

            if not legs:
                logger.error(f"Strategy {strategy.id} has no legs")
                return None

            # Filter to only include legs with open positions
            # This is crucial for correct Supertrend calculation when some legs are closed
            if open_leg_ids is not None:
                original_leg_count = len(legs)
                legs = [leg for leg in legs if leg.id in open_leg_ids]

                if len(legs) < original_leg_count:
                    closed_count = original_leg_count - len(legs)
                    logger.debug(f"Strategy {strategy.id}: Filtered from {original_leg_count} to {len(legs)} legs "
                               f"({closed_count} leg(s) closed, excluded from spread calculation)")

                if not legs:
                    logger.warning(f"Strategy {strategy.id}: No legs with open positions after filtering")
                    return None

            # Get a trading account
            account_ids = strategy.selected_accounts or []
            account = None

            if account_ids:
                account = TradingAccount.query.filter_by(
                    id=account_ids[0],
                    is_active=True
                ).first()

            if not account:
                account = TradingAccount.query.filter_by(
                    user_id=strategy.user_id,
                    is_active=True
                ).first()

            if not account:
                logger.error(f"No active trading account for strategy {strategy.id}")
                return None

            # Initialize OpenAlgo client
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            # Get actual placed symbols from OPEN executions only
            # This ensures we use symbols from positions that are still active
            if open_leg_ids is not None:
                # Filter to only open positions for the relevant legs
                executions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).filter(
                    StrategyExecution.symbol.isnot(None),
                    StrategyExecution.leg_id.in_(open_leg_ids)
                ).all()
                logger.debug(f"Strategy {strategy.id}: Found {len(executions)} open executions for symbol mapping")
            else:
                # Fallback to existing behavior (get all executions)
                executions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id
                ).filter(
                    StrategyExecution.symbol.isnot(None)
                ).all()

            # Map leg_id to actual symbol (lot size comes from leg.lots)
            leg_symbols = {}
            for execution in executions:
                if execution.leg_id not in leg_symbols:
                    leg_symbols[execution.leg_id] = {
                        'symbol': execution.symbol,
                        'exchange': execution.exchange or 'NSE'
                    }

            # Fetch historical data for each leg
            end_date = datetime.now()
            start_date = end_date - timedelta(days=2)  # 2 days should be enough for intraday

            start_date_str = start_date.strftime('%Y-%m-%d')
            end_date_str = end_date.strftime('%Y-%m-%d')

            leg_data_dict = {}

            for leg in legs:
                try:
                    # Use actual placed symbol if available
                    if leg.id in leg_symbols:
                        symbol = leg_symbols[leg.id]['symbol']
                        exchange = leg_symbols[leg.id]['exchange']
                    else:
                        symbol = leg.instrument
                        exchange = 'NSE'

                    # Fetch historical data
                    response = client.history(
                        symbol=symbol,
                        exchange=exchange,
                        interval=strategy.supertrend_timeframe,
                        start_date=start_date_str,
                        end_date=end_date_str
                    )

                    if isinstance(response, pd.DataFrame) and not response.empty:
                        required_cols = ['open', 'high', 'low', 'close']
                        if all(col in response.columns for col in required_cols):
                            # Store data with action and lot size for proper spread calculation
                            lots = leg.lots or 1
                            leg_data_dict[f"{leg.leg_number}_{symbol}"] = {
                                'data': response,
                                'action': leg.action,
                                'lots': lots,
                                'leg_number': leg.leg_number
                            }
                            logger.debug(f"Fetched {len(response)} bars for leg {leg.leg_number} ({symbol}) - {leg.action} x{lots} lots")
                    elif isinstance(response, dict):
                        # Try fallback to base instrument
                        logger.warning(f"Option symbol {symbol} failed, trying {leg.instrument}")
                        fallback_response = client.history(
                            symbol=leg.instrument,
                            exchange='NSE',
                            interval=strategy.supertrend_timeframe,
                            start_date=start_date_str,
                            end_date=end_date_str
                        )
                        if isinstance(fallback_response, pd.DataFrame) and not fallback_response.empty:
                            lots = leg.lots or 1
                            leg_data_dict[f"{leg.leg_number}_{leg.instrument}"] = {
                                'data': fallback_response,
                                'action': leg.action,
                                'lots': lots,
                                'leg_number': leg.leg_number
                            }

                except Exception as e:
                    logger.error(f"Error fetching data for leg {leg.leg_number}: {e}")
                    continue

            if not leg_data_dict:
                logger.error(f"No data fetched for strategy {strategy.id}")
                return None

            # Combine OHLC data into spread using CORRECT formula: SELL - BUY with lot sizes
            sell_dfs = []
            buy_dfs = []

            for leg_name, leg_info in leg_data_dict.items():
                df = leg_info['data']
                action = leg_info['action']
                lots = leg_info['lots']

                # Multiply OHLC by lot size
                weighted_df = df[['open', 'high', 'low', 'close']].copy()
                weighted_df['open'] = weighted_df['open'] * lots
                weighted_df['high'] = weighted_df['high'] * lots
                weighted_df['low'] = weighted_df['low'] * lots
                weighted_df['close'] = weighted_df['close'] * lots

                if action == 'SELL':
                    sell_dfs.append(weighted_df)
                    logger.debug(f"  SELL leg {leg_info['leg_number']} x {lots} lots")
                else:  # BUY
                    buy_dfs.append(weighted_df)
                    logger.debug(f"  BUY leg {leg_info['leg_number']} x {lots} lots")

            # Sum SELL legs
            sell_total = None
            if sell_dfs:
                sell_total = sell_dfs[0].copy()
                for df in sell_dfs[1:]:
                    sell_total['open'] = sell_total['open'].add(df['open'], fill_value=0)
                    sell_total['high'] = sell_total['high'].add(df['high'], fill_value=0)
                    sell_total['low'] = sell_total['low'].add(df['low'], fill_value=0)
                    sell_total['close'] = sell_total['close'].add(df['close'], fill_value=0)

            # Sum BUY legs
            buy_total = None
            if buy_dfs:
                buy_total = buy_dfs[0].copy()
                for df in buy_dfs[1:]:
                    buy_total['open'] = buy_total['open'].add(df['open'], fill_value=0)
                    buy_total['high'] = buy_total['high'].add(df['high'], fill_value=0)
                    buy_total['low'] = buy_total['low'].add(df['low'], fill_value=0)
                    buy_total['close'] = buy_total['close'].add(df['close'], fill_value=0)

            # Calculate spread = SELL - BUY, then take absolute value
            # Spread values should always be positive
            if sell_total is not None and buy_total is not None:
                combined_df = sell_total.copy()
                combined_df['open'] = sell_total['open'] - buy_total['open']
                combined_df['high'] = sell_total['high'] - buy_total['high']
                combined_df['low'] = sell_total['low'] - buy_total['low']
                combined_df['close'] = sell_total['close'] - buy_total['close']

                # Take absolute value of all OHLC - spread cannot be negative
                combined_df['open'] = combined_df['open'].abs()
                combined_df['high'] = combined_df['high'].abs()
                combined_df['low'] = combined_df['low'].abs()
                combined_df['close'] = combined_df['close'].abs()

                # Ensure high >= low (swap if needed after abs())
                high_vals = combined_df['high'].copy()
                low_vals = combined_df['low'].copy()
                combined_df['high'] = pd.concat([high_vals, low_vals], axis=1).max(axis=1)
                combined_df['low'] = pd.concat([high_vals, low_vals], axis=1).min(axis=1)

                logger.debug(f"  Spread calculated with absolute values (always positive)")
            elif sell_total is not None:
                combined_df = sell_total
            elif buy_total is not None:
                combined_df = buy_total
            else:
                logger.error(f"No valid leg data for strategy {strategy.id}")
                return None

            logger.debug(f"Combined spread data: {len(combined_df)} bars for strategy {strategy.id}")
            return combined_df

        except Exception as e:
            logger.error(f"Error fetching combined spread data: {e}", exc_info=True)
            return None

    def trigger_sequential_exit(self, strategy: Strategy, exit_reason: str, app):
        """
        Trigger sequential exit for all open positions in the strategy.

        IMPORTANT: For multi-account strategies, each execution is closed on its own account.
        This ensures orders are placed to the correct broker account.

        Uses sequential processing (like traditional exit) to avoid race conditions
        that can occur with parallel/threaded execution.
        """
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app.utils.order_status_poller import order_status_poller
        import traceback

        strategy_id = strategy.id  # Save ID before context switch
        call_stack = ''.join(traceback.format_stack()[-5:-1])  # Get caller info

        logger.info(f"[SUPERTREND EXIT] >>> trigger_sequential_exit CALLED for strategy {strategy_id}, reason: {exit_reason}")
        logger.info(f"[SUPERTREND EXIT] Call stack:\n{call_stack}")

        try:
            with app.app_context():
                from app import db

                # ATOMIC CHECK-AND-SET: Re-fetch strategy with row lock to prevent race conditions
                # This ensures only ONE call can proceed for a given strategy
                logger.info(f"[SUPERTREND EXIT] Strategy {strategy_id}: Acquiring row lock...")
                strategy = Strategy.query.with_for_update(nowait=False).get(strategy_id)
                if not strategy:
                    logger.error(f"[SUPERTREND EXIT] Strategy {strategy_id} not found")
                    return

                logger.info(f"[SUPERTREND EXIT] Strategy {strategy_id}: Lock acquired. triggered={strategy.supertrend_exit_triggered}, triggered_at={strategy.supertrend_exit_triggered_at}")

                # CHECK if already triggered - if yes, skip (another call already handling it)
                if strategy.supertrend_exit_triggered:
                    logger.info(f"[SUPERTREND EXIT] Strategy {strategy_id}: ALREADY TRIGGERED - skipping duplicate exit")
                    db.session.rollback()  # Release the lock
                    return

                logger.info(f"[SUPERTREND EXIT] Strategy {strategy_id}: NOT triggered yet, proceeding with exit")

                # Mark strategy as triggered IMMEDIATELY and commit to release lock
                # This prevents any other concurrent calls from proceeding
                strategy.supertrend_exit_triggered = True
                strategy.supertrend_exit_reason = exit_reason
                strategy.supertrend_exit_triggered_at = get_ist_now()
                db.session.commit()  # Commit and release lock - other calls will now see triggered=True

                # Get all open positions that don't already have exit orders
                open_executions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).filter(
                    StrategyExecution.exit_order_id.is_(None)
                ).all()

                # Filter out rejected/cancelled
                open_executions = [
                    ex for ex in open_executions
                    if not (hasattr(ex, 'broker_order_status') and
                           ex.broker_order_status in ['rejected', 'cancelled'])
                ]

                if not open_executions:
                    logger.warning(f"[SUPERTREND EXIT] No open positions to close for strategy {strategy.id}")
                    return

                logger.info(f"[SUPERTREND EXIT] Strategy {strategy.id}: Found {len(open_executions)} positions to close")
                for ex in open_executions:
                    logger.info(f"[SUPERTREND EXIT] Strategy {strategy.id}: Execution {ex.id} - {ex.symbol}, qty={ex.quantity}, status={ex.status}, exit_order_id={ex.exit_order_id}")

                # Cache clients per account to avoid creating multiple instances
                account_clients = {}
                success_count = 0

                # BUY-FIRST EXIT PRIORITY: Close SELL positions first (BUY orders), then BUY positions (SELL orders)
                sell_positions = [ex for ex in open_executions if ex.leg and ex.leg.action == 'SELL']
                buy_positions = [ex for ex in open_executions if ex.leg and ex.leg.action == 'BUY']
                unknown_positions = [ex for ex in open_executions if not ex.leg]

                # Reorder: SELL positions first (will place BUY close orders), then BUY positions
                ordered_executions = sell_positions + buy_positions + unknown_positions

                logger.info(f"[SUPERTREND EXIT] BUY-FIRST priority: {len(sell_positions)} SELL positions (close first), "
                           f"{len(buy_positions)} BUY positions (close second)")
                print(f"[SUPERTREND EXIT] BUY-FIRST priority: {len(sell_positions)} SELL positions (close first), "
                      f"{len(buy_positions)} BUY positions (close second)")

                # Get execution IDs to process (we'll re-query each one with lock)
                execution_ids = [ex.id for ex in ordered_executions]
                sell_count = len(sell_positions)
                logger.info(f"[SUPERTREND EXIT] Strategy {strategy.id}: Processing execution IDs (ordered): {execution_ids}")

                for idx, exec_id in enumerate(execution_ids):
                    # Log phase transitions
                    if idx == sell_count and sell_positions and buy_positions:
                        logger.info(f"[SUPERTREND EXIT PHASE 2] All SELL positions closed. Starting BUY position exits...")
                        print(f"[SUPERTREND EXIT PHASE 2] All SELL positions closed. Starting BUY position exits...")
                    try:
                        # ATOMIC: Re-query execution with row lock to prevent race conditions
                        # This ensures only ONE thread can process this execution
                        execution = StrategyExecution.query.with_for_update(nowait=False).get(exec_id)
                        if not execution:
                            logger.warning(f"[SUPERTREND EXIT] Execution {exec_id} not found")
                            continue

                        # Check if already has exit order (another thread may have processed it)
                        if execution.exit_order_id:
                            logger.info(f"[SUPERTREND EXIT] Execution {exec_id}: SKIPPING - already has exit order {execution.exit_order_id}")
                            continue

                        # Check status is still 'entered'
                        if execution.status != 'entered':
                            logger.info(f"[SUPERTREND EXIT] Execution {exec_id}: SKIPPING - status is {execution.status}, not 'entered'")
                            continue

                        logger.info(f"[SUPERTREND EXIT] Execution {exec_id}: PROCEEDING - status={execution.status}, exit_order_id={execution.exit_order_id}")

                        # Use the execution's account (NOT primary account)
                        account = execution.account
                        if not account or not account.is_active:
                            logger.error(f"[SUPERTREND EXIT] Account not found or inactive for execution {execution.id}")
                            continue

                        # Get or create client for this account
                        if account.id not in account_clients:
                            account_clients[account.id] = ExtendedOpenAlgoAPI(
                                api_key=account.get_api_key(),
                                host=account.host_url
                            )
                        client = account_clients[account.id]

                        # Get entry action from leg
                        leg = execution.leg
                        entry_action = leg.action.upper() if leg else 'BUY'
                        exit_action = 'SELL' if entry_action == 'BUY' else 'BUY'

                        # Get product type - prefer execution's product, fallback to strategy's product_order_type
                        exit_product = execution.product or strategy.product_order_type or 'MIS'

                        # RELIABILITY FIX: Mark as exit_pending BEFORE placing order
                        # This prevents other processes from trying to exit this position
                        execution.status = 'exit_pending'
                        execution.exit_reason = exit_reason
                        execution.exit_time = datetime.utcnow()
                        db.session.commit()  # Commit immediately to claim this execution

                        # Store values we need for order placement
                        exec_id = execution.id
                        exec_symbol = execution.symbol
                        exec_exchange = execution.exchange
                        exec_quantity = execution.quantity

                        logger.info(f"[SUPERTREND EXIT] Placing exit for {exec_symbol} on {account.account_name}, action={exit_action}, qty={exec_quantity}, product={exit_product}")

                        # Place order using freeze quantity handler with retry mechanism
                        from app.utils.freeze_quantity_handler import place_order_with_freeze_check
                        import time as time_module

                        max_retries = 3
                        retry_delay = 1
                        response = None

                        for attempt in range(max_retries):
                            try:
                                response = place_order_with_freeze_check(
                                    client=client,
                                    user_id=strategy.user_id,
                                    strategy=strategy.name,
                                    symbol=exec_symbol,
                                    exchange=exec_exchange,
                                    action=exit_action,
                                    quantity=exec_quantity,
                                    price_type='MARKET',
                                    product=exit_product
                                )
                                if response and isinstance(response, dict):
                                    if response.get('status') == 'success':
                                        break  # Success, exit retry loop
                                    else:
                                        # API returned error, retry
                                        logger.warning(f"[SUPERTREND EXIT] Attempt {attempt + 1}/{max_retries} returned error for {exec_symbol}: {response.get('message', 'Unknown')}")
                            except Exception as api_error:
                                logger.warning(f"[SUPERTREND EXIT] Attempt {attempt + 1}/{max_retries} failed for {exec_symbol} on {account.account_name}: {api_error}")

                            if attempt < max_retries - 1:
                                time_module.sleep(retry_delay)
                                retry_delay *= 2  # Exponential backoff
                            else:
                                if not response:
                                    response = {'status': 'error', 'message': f'API error after {max_retries} retries'}

                        # Re-fetch execution with lock to update order ID
                        execution = StrategyExecution.query.with_for_update(nowait=False).get(exec_id)

                        if response and response.get('status') == 'success':
                            order_id = response.get('orderid')

                            # Update with the actual order ID
                            execution.exit_order_id = order_id
                            execution.broker_order_status = 'open'
                            db.session.commit()  # Commit each execution to release row lock
                            success_count += 1

                            # Add exit order to polling queue
                            order_status_poller.add_order(
                                execution_id=execution.id,
                                account=account,
                                order_id=order_id,
                                strategy_name=strategy.name
                            )

                            logger.info(f"[SUPERTREND EXIT] Exit order {order_id} placed for {exec_symbol} on {account.account_name}")
                        else:
                            error_msg = response.get('message', 'Unknown error') if response else 'No response'
                            logger.error(f"[SUPERTREND EXIT] Failed to place exit for {exec_symbol} on {account.account_name}: {error_msg}")

                            # RELIABILITY FIX: Revert status to 'entered' so retry can pick it up
                            execution.status = 'entered'
                            execution.exit_reason = f"failed: {error_msg[:100]}"
                            execution.exit_time = None
                            db.session.commit()  # Commit to release lock

                    except Exception as e:
                        logger.error(f"[SUPERTREND EXIT] Exception closing execution {exec_id}: {str(e)}", exc_info=True)
                        db.session.rollback()  # Release lock on exception

                # VERIFICATION: Check for missing/failed orders
                fail_count = len(execution_ids) - success_count
                if fail_count > 0:
                    logger.error(f"[SUPERTREND EXIT] WARNING: {fail_count} exit orders FAILED out of {len(execution_ids)} total!")
                    print(f"[SUPERTREND EXIT] CRITICAL: {fail_count} orders failed - manual intervention may be required!")

                    # Log which executions still don't have exit orders
                    for exec_id in execution_ids:
                        try:
                            exec_check = StrategyExecution.query.get(exec_id)
                            if exec_check and exec_check.status == 'entered' and not exec_check.exit_order_id:
                                logger.error(f"[SUPERTREND EXIT] MISSING EXIT: Execution {exec_id} ({exec_check.symbol}) still has no exit order!")
                                print(f"[SUPERTREND EXIT] MISSING: {exec_check.symbol} on {exec_check.account.account_name if exec_check.account else 'Unknown'}")
                        except Exception:
                            pass

                logger.info(f"[SUPERTREND EXIT] Completed: {success_count}/{len(execution_ids)} exit orders placed")

        except Exception as e:
            logger.error(f"[SUPERTREND EXIT] Error in trigger_sequential_exit: {e}", exc_info=True)


# Global service instance
supertrend_exit_service = SupertrendExitService()
