"""
Position Monitor Service
Monitors open positions via WebSocket for real-time P&L and risk management.

Key Features:
- Subscribes ONLY to symbols with open positions (5-50 vs 1000)
- Primary account connection required
- Trading hours from TradingHoursTemplate (no hardcoding)
- Integrates with OrderStatusPoller for order updates

Uses standard threading for background tasks
"""
import logging
from datetime import datetime
from typing import Dict, List, Set, Optional
import pytz
from sqlalchemy import and_
from sqlalchemy.orm import joinedload

from app import db
from app.models import (
    TradingAccount, StrategyExecution, Strategy, StrategyLeg,
    TradingHoursTemplate, TradingSession, MarketHoliday
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)


class PositionMonitor:
    """
    Singleton service to monitor open positions via WebSocket.

    Architecture:
    - Subscribes ONLY to symbols with status='entered'
    - Checks primary account connection before starting
    - Respects trading hours from database
    - Provides real-time LTP updates for risk management
    """

    _instance = None

    def __new__(cls):
        """Singleton pattern - only one instance"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the position monitor"""
        if self._initialized:
            return

        self._initialized = True
        self.is_running = False
        self.websocket_manager = None
        self.subscribed_symbols: Set[str] = set()
        self.position_map: Dict[str, List[StrategyExecution]] = {}
        self.app = None  # Store Flask app instance for creating app context

        # Batch update mechanism for WebSocket price updates
        # Stores {symbol_exchange: {'ltp': float, 'updated': datetime}}
        self._pending_price_updates: Dict[str, Dict] = {}
        self._price_update_lock = None  # Will be initialized with threading.Lock()
        self._batch_flush_interval = 2.0  # Flush every 2 seconds
        self._last_flush_time = None
        self._flush_thread = None

        logger.debug("PositionMonitor initialized")

    def should_start_monitoring(self) -> bool:
        """
        Check if monitoring should start.

        Conditions:
        1. Primary account exists and is connected
        2. Current time is within trading hours (from TradingHoursTemplate)
        3. Today is not a market holiday

        Returns:
            bool: True if all conditions met, False otherwise
        """
        # 1. Check primary account exists
        primary_account = TradingAccount.query.filter_by(
            is_primary=True,
            is_active=True
        ).first()

        if not primary_account:
            logger.warning("No primary account found - monitoring disabled")
            return False

        # 2. Check primary account connection
        try:
            client = ExtendedOpenAlgoAPI(
                api_key=primary_account.get_api_key(),
                host=primary_account.host_url
            )
            ping_response = client.ping()

            if ping_response.get('status') != 'success':
                logger.warning(
                    f"Primary account {primary_account.account_name} not connected - "
                    f"monitoring disabled"
                )
                return False

            logger.debug(f"Primary account {primary_account.account_name} is connected")

        except Exception as e:
            logger.error(f"Failed to ping primary account: {e}")
            return False

        # 3. Check trading hours from template
        if not self.is_trading_hours():
            logger.debug("Outside trading hours - monitoring disabled")
            return False

        logger.debug("All conditions met - monitoring can start")
        return True

    def is_trading_hours(self) -> bool:
        """
        Check if current time is within trading hours.
        Uses TradingHoursTemplate from database (NO HARDCODING).

        Returns:
            bool: True if within trading hours, False otherwise
        """
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        today = now.date()
        current_time = now.time()
        day_of_week = now.weekday()  # 0=Monday, 6=Sunday

        # Check if today is a market holiday
        is_holiday = MarketHoliday.query.filter(
            MarketHoliday.holiday_date == today
        ).first()

        if is_holiday:
            logger.debug(f"Market holiday: {is_holiday.holiday_name}")
            return False

        # Get trading sessions for today
        sessions = TradingSession.query.join(TradingHoursTemplate).filter(
            TradingSession.day_of_week == day_of_week,
            TradingSession.is_active == True,
            TradingHoursTemplate.is_active == True
        ).all()

        if not sessions:
            logger.debug(f"No trading sessions configured for day {day_of_week}")
            return False

        # Check if current time is within any session
        for session in sessions:
            if session.start_time <= current_time <= session.end_time:
                logger.debug(
                    f"Within trading hours: {session.session_name} "
                    f"({session.start_time} - {session.end_time})"
                )
                return True

        logger.debug(f"Outside all trading sessions for day {day_of_week}")
        return False

    def get_open_positions(self) -> List[StrategyExecution]:
        """
        Get open positions that need WebSocket monitoring.

        Only returns positions where risk management is configured:
        - Leg-level: stop_loss_value or take_profit_value on StrategyLeg
        - Strategy-level: max_loss or max_profit on Strategy

        Filters:
        - status='entered'
        - broker_order_status NOT IN ['rejected', 'cancelled']
        - Has SL/TP configured at leg or strategy level

        Returns:
            List[StrategyExecution]: List of open position executions with risk management
        """
        try:
            # Query for entered positions with eager loading to avoid N+1 queries
            # This loads leg and strategy in a single query instead of separate queries per execution
            open_executions = StrategyExecution.query.options(
                joinedload(StrategyExecution.leg),
                joinedload(StrategyExecution.strategy)
            ).filter(
                StrategyExecution.status == 'entered'
            ).all()

            # Filter out rejected/cancelled orders and positions without risk management
            filtered_executions = []
            for execution in open_executions:
                # Check if broker_order_status exists and is not rejected/cancelled
                if hasattr(execution, 'broker_order_status') and execution.broker_order_status:
                    if execution.broker_order_status.lower() in ['rejected', 'cancelled']:
                        continue

                # Check leg-level risk management (StrategyLeg)
                leg = execution.leg
                has_leg_sl = False
                has_leg_tp = False
                if leg:
                    has_leg_sl = leg.stop_loss_value is not None and leg.stop_loss_value > 0
                    has_leg_tp = leg.take_profit_value is not None and leg.take_profit_value > 0

                # Check strategy-level risk management (Strategy)
                strategy = execution.strategy
                has_strategy_sl = False
                has_strategy_tp = False
                if strategy:
                    has_strategy_sl = strategy.max_loss is not None and strategy.max_loss > 0
                    has_strategy_tp = strategy.max_profit is not None and strategy.max_profit > 0

                # Include if ANY risk management is configured
                if has_leg_sl or has_leg_tp or has_strategy_sl or has_strategy_tp:
                    filtered_executions.append(execution)
                    logger.debug(f"Position {execution.symbol} has risk management "
                               f"(Leg SL={leg.stop_loss_value if leg else None}, "
                               f"Leg TP={leg.take_profit_value if leg else None}, "
                               f"Strategy SL={strategy.max_loss if strategy else None}, "
                               f"Strategy TP={strategy.max_profit if strategy else None})")
                else:
                    logger.debug(f"Skipping {execution.symbol} - no risk management configured")

            logger.debug(f"Found {len(filtered_executions)} positions with risk management to monitor")
            return filtered_executions

        except Exception as e:
            logger.error(f"Error getting open positions: {e}")
            return []

    def subscribe_to_positions(self):
        """
        Subscribe to WebSocket for all open positions.

        Logic:
        1. Get open positions from database
        2. Group by symbol to avoid duplicate subscriptions
        3. Subscribe to each unique symbol
        4. Update internal tracking
        """
        if not self.websocket_manager:
            logger.error("WebSocket manager not initialized - cannot subscribe")
            return

        # Get open positions
        open_executions = self.get_open_positions()

        if not open_executions:
            logger.debug("No open positions to monitor")
            return

        # Group by symbol to avoid duplicate subscriptions
        symbols_to_subscribe = {}
        for execution in open_executions:
            # Create unique key from symbol and exchange
            key = f"{execution.symbol}_{execution.exchange}"

            if key not in symbols_to_subscribe:
                symbols_to_subscribe[key] = {
                    'symbol': execution.symbol,
                    'exchange': execution.exchange,
                    'executions': []
                }

            symbols_to_subscribe[key]['executions'].append(execution)

        logger.debug(f"Subscribing to {len(symbols_to_subscribe)} unique symbols")

        # Subscribe to each unique symbol
        for key, data in symbols_to_subscribe.items():
            try:
                # Subscribe to WebSocket
                self.websocket_manager.subscribe({
                    'symbol': data['symbol'],
                    'exchange': data['exchange'],
                    'mode': 'quote'  # Need LTP for P&L calculation
                })

                # Update internal tracking
                self.subscribed_symbols.add(key)
                self.position_map[key] = data['executions']

                logger.debug(
                    f"Subscribed to {data['symbol']} "
                    f"({len(data['executions'])} positions)"
                )

            except Exception as e:
                logger.error(f"Failed to subscribe to {data['symbol']}: {e}")

        logger.debug(
            f"Position monitoring active: {len(self.subscribed_symbols)} symbols, "
            f"{len(open_executions)} positions"
        )

    def unsubscribe_from_symbol(self, symbol: str, exchange: str):
        """
        Unsubscribe from a symbol when no more positions exist.

        Args:
            symbol: Trading symbol
            exchange: Exchange name
        """
        key = f"{symbol}_{exchange}"

        if key not in self.subscribed_symbols:
            return

        try:
            if self.websocket_manager:
                self.websocket_manager.unsubscribe({
                    'symbol': symbol,
                    'exchange': exchange
                })

            self.subscribed_symbols.discard(key)
            self.position_map.pop(key, None)

            logger.debug(f"Unsubscribed from {symbol}")

        except Exception as e:
            logger.error(f"Error unsubscribing from {symbol}: {e}")

    def on_order_filled(self, execution: StrategyExecution):
        """
        Called by OrderStatusPoller when an order fills.
        Only subscribes to WebSocket if risk management (SL/TP) is configured.

        Args:
            execution: The filled strategy execution
        """
        if not self.is_running:
            logger.debug("Monitor not running - ignoring order fill")
            return

        # Check leg-level risk management (StrategyLeg)
        leg = execution.leg
        has_leg_sl = False
        has_leg_tp = False
        if leg:
            has_leg_sl = leg.stop_loss_value is not None and leg.stop_loss_value > 0
            has_leg_tp = leg.take_profit_value is not None and leg.take_profit_value > 0

        # Check strategy-level risk management (Strategy)
        strategy = execution.strategy
        has_strategy_sl = False
        has_strategy_tp = False
        if strategy:
            has_strategy_sl = strategy.max_loss is not None and strategy.max_loss > 0
            has_strategy_tp = strategy.max_profit is not None and strategy.max_profit > 0

        # Only subscribe if ANY risk management is configured
        if not (has_leg_sl or has_leg_tp or has_strategy_sl or has_strategy_tp):
            logger.debug(f"Order filled for {execution.symbol} but no risk management configured - skipping WebSocket subscription")
            return

        key = f"{execution.symbol}_{execution.exchange}"

        # Check if we're already monitoring this symbol
        if key in self.subscribed_symbols:
            # Add to existing position tracking
            if key in self.position_map:
                self.position_map[key].append(execution)
            logger.debug(f"Added {execution.symbol} to existing monitoring")
            return

        # Subscribe to new symbol (only if risk management is configured)
        try:
            if self.websocket_manager:
                self.websocket_manager.subscribe({
                    'symbol': execution.symbol,
                    'exchange': execution.exchange,
                    'mode': 'quote'
                })

                self.subscribed_symbols.add(key)
                self.position_map[key] = [execution]

                logger.debug(f"New position filled with risk management - subscribed to {execution.symbol}")

        except Exception as e:
            logger.error(f"Failed to subscribe to new position {execution.symbol}: {e}")

    def on_order_cancelled(self, execution: StrategyExecution):
        """
        Called by OrderStatusPoller when an order is cancelled.
        Does NOT unsubscribe since this only affects pending orders.

        Args:
            execution: The cancelled strategy execution
        """
        logger.debug(f"Order cancelled: {execution.symbol} - no action needed")

    def on_position_closed(self, execution: StrategyExecution):
        """
        Called when a position is closed (exit order filled).
        Removes from monitoring and unsubscribes if no more positions.

        Args:
            execution: The closed strategy execution
        """
        try:
            key = f"{execution.symbol}_{execution.exchange}"

            logger.info(f"[POSITION_CLOSE] Received notification for {execution.symbol} (execution_id={execution.id})")
            logger.info(f"[POSITION_CLOSE] Currently subscribed symbols: {list(self.subscribed_symbols)}")

            # Remove from position map by ID (not object identity)
            if key in self.position_map:
                positions = self.position_map[key]
                # Remove by ID since object instances may differ
                self.position_map[key] = [p for p in positions if p.id != execution.id]
                remaining = len(self.position_map[key])
                logger.info(f"[POSITION_CLOSE] Removed execution {execution.id} from position map, {remaining} remaining")

                # If no more positions in our map, remove the key
                if remaining == 0:
                    self.position_map.pop(key, None)

            # Query database to check if ANY open positions remain for this symbol
            # This is the source of truth, not our in-memory map
            remaining_positions = StrategyExecution.query.filter(
                StrategyExecution.symbol == execution.symbol,
                StrategyExecution.exchange == execution.exchange,
                StrategyExecution.status == 'entered'
            ).count()

            logger.info(f"[POSITION_CLOSE] Database shows {remaining_positions} remaining open positions for {execution.symbol}")

            if remaining_positions == 0:
                # No more open positions - unsubscribe from WebSocket
                logger.info(f"[POSITION_CLOSE] No more open positions - unsubscribing from {execution.symbol}")

                # Force unsubscribe regardless of internal tracking
                if key in self.subscribed_symbols:
                    self.unsubscribe_from_symbol(execution.symbol, execution.exchange)
                    logger.info(f"[POSITION_CLOSE] Unsubscribed {execution.symbol} from WebSocket")
                else:
                    # Still try to unsubscribe even if not in our tracking (defensive)
                    logger.warning(f"[POSITION_CLOSE] {execution.symbol} not in subscribed_symbols but trying to unsubscribe anyway")
                    if self.websocket_manager:
                        try:
                            self.websocket_manager.unsubscribe({
                                'symbol': execution.symbol,
                                'exchange': execution.exchange
                            })
                            logger.info(f"[POSITION_CLOSE] Force-unsubscribed {execution.symbol}")
                        except Exception as unsub_error:
                            logger.error(f"[POSITION_CLOSE] Force-unsubscribe failed: {unsub_error}")
            else:
                logger.info(f"[POSITION_CLOSE] Position closed for {execution.symbol} - {remaining_positions} positions still open")

        except Exception as e:
            logger.error(f"[POSITION_CLOSE] Error processing position close: {e}", exc_info=True)

    def update_last_price(self, symbol: str, exchange: str, ltp: float):
        """
        Queue price update for batch processing.
        Called by WebSocket message handler.

        Instead of writing to DB immediately, queues the update for batch flush.
        This reduces DB writes from potentially 100s per second to 1 per 2 seconds.

        Args:
            symbol: Trading symbol
            exchange: Exchange name
            ltp: Last traded price
        """
        key = f"{symbol}_{exchange}"

        if key not in self.position_map:
            return

        # Queue for batch update instead of immediate DB write
        if self._price_update_lock:
            with self._price_update_lock:
                self._pending_price_updates[key] = {
                    'symbol': symbol,
                    'exchange': exchange,
                    'ltp': ltp,
                    'updated': datetime.utcnow()
                }

    def _flush_pending_updates(self):
        """
        Flush all pending price updates to database in a single transaction.
        Called periodically by the flush thread.
        """
        if not self._pending_price_updates:
            return

        if not self.app:
            return

        updates_to_process = {}

        # Get pending updates with lock
        if self._price_update_lock:
            with self._price_update_lock:
                updates_to_process = self._pending_price_updates.copy()
                self._pending_price_updates.clear()

        if not updates_to_process:
            return

        try:
            with self.app.app_context():
                # Get all execution IDs that need updating
                execution_ids = []
                for key, data in updates_to_process.items():
                    if key in self.position_map:
                        execution_ids.extend([e.id for e in self.position_map[key]])

                if not execution_ids:
                    return

                # Batch update all positions in a single query
                now = datetime.utcnow()
                for key, data in updates_to_process.items():
                    if key in self.position_map:
                        position_ids = [e.id for e in self.position_map[key]]
                        if position_ids:
                            StrategyExecution.query.filter(
                                StrategyExecution.id.in_(position_ids)
                            ).update({
                                'last_price': data['ltp'],
                                'last_price_updated': now,
                                'websocket_subscribed': True
                            }, synchronize_session=False)

                db.session.commit()
                logger.debug(f"Batch flushed {len(updates_to_process)} price updates")

        except Exception as e:
            logger.error(f"Error flushing price updates: {e}")
            try:
                with self.app.app_context():
                    db.session.rollback()
            except:
                pass

    def _flush_thread_runner(self):
        """Background thread that periodically flushes price updates."""
        import time
        while self.is_running:
            time.sleep(self._batch_flush_interval)
            try:
                self._flush_pending_updates()
            except Exception as e:
                logger.error(f"Flush thread error: {e}")

    def _handle_websocket_data(self, data: Dict):
        """
        WebSocket data handler - queues price updates for batch processing.

        Note: This is called from WebSocket callbacks at high frequency.
        Uses batching to avoid creating app context and DB writes for each update.

        Args:
            data: WebSocket market data
        """
        try:
            symbol = data.get('symbol')
            exchange = data.get('exchange', 'NFO')
            ltp = data.get('ltp')

            if not symbol or not ltp:
                return

            # Queue for batch update - no app context needed here
            self.update_last_price(symbol, exchange, float(ltp))

        except Exception as e:
            logger.error(f"Error handling WebSocket data: {e}")

    def start(self, websocket_manager, app=None):
        """
        Start position monitoring.

        Args:
            websocket_manager: WebSocket manager instance
            app: Flask app instance (for creating app context in WebSocket callbacks)
        """
        import threading

        if self.is_running:
            logger.warning("Position monitor already running")
            return

        # Store websocket manager reference
        self.websocket_manager = websocket_manager

        # Store Flask app reference for batch flush operations
        if app is not None:
            self.app = app
            logger.debug("Flask app reference stored for batch flush operations")

        # Initialize thread lock for batch updates
        self._price_update_lock = threading.Lock()

        # Check if monitoring should start (app context provided by caller)
        if not self.should_start_monitoring():
            logger.debug("Position monitoring not started - conditions not met")
            return

        # Register WebSocket data handler for quote updates
        if hasattr(websocket_manager, 'data_processor'):
            websocket_manager.data_processor.register_quote_handler(self._handle_websocket_data)
            logger.debug("Position monitor registered for WebSocket quote updates")

        # Subscribe to open positions
        self.subscribe_to_positions()

        self.is_running = True

        # Start batch flush thread
        self._flush_thread = threading.Thread(target=self._flush_thread_runner, daemon=True)
        self._flush_thread.start()
        logger.debug("Position monitoring started with batch flush thread")

    def stop(self):
        """Stop position monitoring and unsubscribe from all symbols"""
        if not self.is_running:
            return

        logger.debug("Stopping position monitoring...")

        # Mark as not running first to stop flush thread
        self.is_running = False

        # Flush any pending updates before stopping
        try:
            self._flush_pending_updates()
        except Exception as e:
            logger.error(f"Error flushing updates during stop: {e}")

        # Wait for flush thread to finish
        if self._flush_thread and self._flush_thread.is_alive():
            self._flush_thread.join(timeout=5.0)

        # Unsubscribe from all symbols
        for key in list(self.subscribed_symbols):
            symbol, exchange = key.split('_', 1)
            self.unsubscribe_from_symbol(symbol, exchange)

        self.subscribed_symbols.clear()
        self.position_map.clear()
        self._pending_price_updates.clear()

    def refresh_positions(self):
        """
        Refresh position subscriptions by re-scanning database.
        Called periodically to catch any positions that might have been missed.
        """
        if not self.is_running:
            logger.debug("Position monitor not running - skipping refresh")
            return

        if not self.websocket_manager:
            logger.debug("No WebSocket manager - skipping refresh")
            return

        try:
            # Get current open positions from database
            open_executions = self.get_open_positions()

            if not open_executions:
                return

            # Subscribe to any new positions
            for execution in open_executions:
                key = f"{execution.symbol}_{execution.exchange}"

                # Skip if already subscribed
                if key in self.subscribed_symbols:
                    # Make sure it's in the position map
                    if key in self.position_map:
                        if execution not in self.position_map[key]:
                            self.position_map[key].append(execution)
                    continue

                # Subscribe to new symbol
                self.websocket_manager.subscribe({
                    'symbol': execution.symbol,
                    'exchange': execution.exchange,
                    'mode': 'quote'
                })

                self.subscribed_symbols.add(key)
                self.position_map[key] = [execution]
                logger.debug(f"Refresh: Subscribed to {execution.symbol}")

        except Exception as e:
            logger.error(f"Error refreshing positions: {e}")

    def get_monitoring_status(self) -> Dict:
        """
        Get current monitoring status for admin dashboard.

        Returns:
            Dict with monitoring statistics
        """
        return {
            'is_running': self.is_running,
            'subscribed_symbols': len(self.subscribed_symbols),
            'total_positions': sum(len(positions) for positions in self.position_map.values()),
            'symbols': list(self.subscribed_symbols),
            'can_start': self.should_start_monitoring()
        }


# Global instance
position_monitor = PositionMonitor()
