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
            self.scheduler.add_job(
                func=self.monitor_strategies,
                trigger='cron',
                second=0,  # Run at :00 of every minute
                id='supertrend_monitor',
                replace_existing=True
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
        try:
            from app import create_app

            # Create Flask app context
            app = create_app()
            with app.app_context():
                from app import db

                # Get all strategies with Supertrend exit enabled and not yet triggered
                strategies = Strategy.query.filter_by(
                    supertrend_exit_enabled=True,
                    supertrend_exit_triggered=False,
                    is_active=True
                ).all()

                if not strategies:
                    return

                logger.debug(f"Monitoring {len(strategies)} strategies with Supertrend exit enabled")

                for strategy in strategies:
                    try:
                        # Check if we should monitor this strategy based on timeframe
                        if self.should_check_strategy(strategy):
                            logger.debug(f"Checking Supertrend for strategy {strategy.id} ({strategy.name})")
                            self.check_supertrend_exit(strategy, app)
                    except Exception as e:
                        logger.error(f"Error monitoring strategy {strategy.id}: {e}", exc_info=True)

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
                # Update last check time
                self.monitoring_strategies[strategy.id] = datetime.now(pytz.timezone('Asia/Kolkata'))

                # Check if strategy has open positions
                open_positions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).all()

                # Filter out rejected/cancelled
                open_positions = [
                    pos for pos in open_positions
                    if not (hasattr(pos, 'broker_order_status') and
                           pos.broker_order_status in ['rejected', 'cancelled'])
                ]

                if not open_positions:
                    logger.debug(f"Strategy {strategy.id} has no open positions, skipping")
                    return

                # Get set of leg IDs that have open positions
                # A leg is considered "open" if ANY account has an open position for it
                open_leg_ids = set(pos.leg_id for pos in open_positions if pos.leg_id)
                logger.debug(f"Strategy {strategy.id}: {len(open_positions)} open positions across {len(open_leg_ids)} legs (leg_ids: {open_leg_ids})")

                # Fetch combined spread data ONLY for legs with open positions
                # This ensures closed legs don't affect the Supertrend calculation
                spread_data = self.fetch_combined_spread_data(strategy, open_leg_ids=open_leg_ids)

                if spread_data is None or len(spread_data) < strategy.supertrend_period + 5:
                    logger.warning(f"Insufficient data for Supertrend calculation for strategy {strategy.id}")
                    return

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
                        exit_reason = f'supertrend_breakout (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'
                        logger.debug(f"Strategy {strategy.id}: Supertrend BREAKOUT - Close crossed above ST on candle close")

                elif strategy.supertrend_exit_type == 'breakdown':
                    # Breakdown: CLOSE crossed BELOW Supertrend (direction = 1 in Pine Script)
                    # Checked on candle close only, not intrabar
                    if latest_direction == 1:  # Bearish - price below supertrend
                        should_exit = True
                        exit_reason = f'supertrend_breakdown (Close: {latest_close:.2f}, ST: {latest_supertrend:.2f})'
                        logger.debug(f"Strategy {strategy.id}: Supertrend BREAKDOWN - Close crossed below ST on candle close")

                if should_exit:
                    logger.debug(f"Triggering parallel exit for strategy {strategy.id} - Reason: {exit_reason}")
                    self.trigger_parallel_exit(strategy, exit_reason, app)
                else:
                    logger.debug(f"Strategy {strategy.id}: No exit signal (Direction: {latest_direction}, Type: {strategy.supertrend_exit_type})")

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

    def trigger_parallel_exit(self, strategy: Strategy, exit_reason: str, app):
        """
        Trigger parallel exit for all open positions in the strategy
        Reuses the close_all_positions logic from strategy routes
        """
        with app.app_context():
            from app import db
            import threading
            from app.utils.openalgo_client import ExtendedOpenAlgoAPI
            from datetime import datetime

            try:
                logger.debug(f"[SUPERTREND EXIT] Initiating parallel exit for strategy {strategy.id}")

                # Mark strategy as triggered and store the reason
                strategy.supertrend_exit_triggered = True
                strategy.supertrend_exit_reason = exit_reason
                strategy.supertrend_exit_triggered_at = datetime.utcnow()
                db.session.commit()

                # Get all open positions
                open_positions = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    status='entered'
                ).all()

                # Filter out rejected/cancelled
                open_positions = [
                    pos for pos in open_positions
                    if not (hasattr(pos, 'broker_order_status') and
                           pos.broker_order_status in ['rejected', 'cancelled'])
                ]

                if not open_positions:
                    logger.warning(f"No open positions found for strategy {strategy.id}")
                    return

                logger.debug(f"[SUPERTREND EXIT] Closing {len(open_positions)} positions in parallel")

                # Thread-safe results collection
                results = []
                results_lock = threading.Lock()

                def close_position_worker(position, strategy_name, product_type, thread_index, user_id):
                    """Worker to close a single position"""
                    import time

                    # Staggered delay to prevent race conditions
                    delay = thread_index * 0.3
                    if delay > 0:
                        time.sleep(delay)

                    # Create Flask app context for this thread
                    app_ctx = create_app()
                    with app_ctx.app_context():
                        try:
                            # Reverse action for closing
                            close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

                            client = ExtendedOpenAlgoAPI(
                                api_key=position.account.get_api_key(),
                                host=position.account.host_url
                            )

                            # Place close order with freeze awareness and retry logic
                            from app.utils.freeze_quantity_handler import place_order_with_freeze_check
                            import time as time_module

                            max_retries = 3
                            retry_delay = 1
                            response = None

                            for attempt in range(max_retries):
                                try:
                                    response = place_order_with_freeze_check(
                                        client=client,
                                        user_id=user_id,
                                        strategy=strategy_name,
                                        symbol=position.symbol,
                                        exchange=position.exchange,
                                        action=close_action,
                                        quantity=position.quantity,
                                        price_type='MARKET',
                                        product=product_type or 'MIS'
                                    )
                                    if response and isinstance(response, dict):
                                        break
                                except Exception as api_error:
                                    logger.warning(f"[RETRY] Supertrend exit attempt {attempt + 1}/{max_retries} failed: {api_error}")
                                    if attempt < max_retries - 1:
                                        time_module.sleep(retry_delay)
                                        retry_delay *= 2
                                    else:
                                        response = {'status': 'error', 'message': f'API error after {max_retries} retries'}

                            if response and response.get('status') == 'success':
                                # Update position - set to exit_pending, poller will update with actual fill price
                                position_to_update = StrategyExecution.query.get(position.id)
                                if position_to_update:
                                    exit_order_id = response.get('orderid')
                                    position_to_update.status = 'exit_pending'
                                    position_to_update.exit_order_id = exit_order_id
                                    position_to_update.exit_time = datetime.utcnow()
                                    position_to_update.exit_reason = exit_reason
                                    position_to_update.broker_order_status = 'open'

                                    db.session.commit()

                                    # Add exit order to poller to get actual fill price (same as entry orders)
                                    from app.utils.order_status_poller import order_status_poller
                                    order_status_poller.add_order(
                                        execution_id=position_to_update.id,
                                        account=position_to_update.account,
                                        order_id=exit_order_id,
                                        strategy_name=strategy_name
                                    )

                                    with results_lock:
                                        results.append({
                                            'symbol': position.symbol,
                                            'account': position.account.account_name,
                                            'status': 'success',
                                            'pnl': 0  # P&L will be calculated by poller when fill price is received
                                        })
                            else:
                                error_msg = response.get('message', 'Unknown error') if response else 'No response'
                                with results_lock:
                                    results.append({
                                        'symbol': position.symbol,
                                        'account': position.account.account_name,
                                        'status': 'failed',
                                        'error': error_msg
                                    })

                        except Exception as e:
                            logger.error(f"Error closing position {position.symbol}: {e}")
                            with results_lock:
                                results.append({
                                    'symbol': position.symbol,
                                    'account': getattr(position.account, 'account_name', 'unknown'),
                                    'status': 'error',
                                    'error': str(e)
                                })

                # Import create_app here to avoid circular import
                from app import create_app

                # Create and start threads
                threads = []
                for idx, position in enumerate(open_positions):
                    thread = threading.Thread(
                        target=close_position_worker,
                        args=(position, strategy.name, strategy.product_order_type, idx, strategy.user_id),
                        name=f"SupertrendExit_{position.symbol}"
                    )
                    threads.append(thread)
                    thread.start()

                # Wait for all threads
                for thread in threads:
                    thread.join(timeout=30)

                # Log results
                successful = len([r for r in results if r.get('status') == 'success'])
                failed = len([r for r in results if r.get('status') in ['failed', 'error']])
                total_pnl = sum(r.get('pnl', 0) for r in results if r.get('status') == 'success')

                logger.debug(f"[SUPERTREND EXIT] Completed: {successful}/{len(open_positions)} positions closed, Total P&L: {total_pnl:.2f}")

            except Exception as e:
                logger.error(f"Error triggering parallel exit: {e}", exc_info=True)


# Global service instance
supertrend_exit_service = SupertrendExitService()
