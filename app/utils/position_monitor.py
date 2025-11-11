"""
Position Monitor Service
Monitors open positions via WebSocket for real-time P&L and risk management.

Key Features:
- Subscribes ONLY to symbols with open positions (5-50 vs 1000)
- Primary account connection required
- Trading hours from TradingHoursTemplate (no hardcoding)
- Integrates with OrderStatusPoller for order updates
"""
import logging
from datetime import datetime
from typing import Dict, List, Set, Optional
import pytz
from sqlalchemy import and_

from app import db
from app.models import (
    TradingAccount, StrategyExecution, Strategy,
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

        logger.info("PositionMonitor initialized")

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

            logger.info(f"Primary account {primary_account.account_name} is connected")

        except Exception as e:
            logger.error(f"Failed to ping primary account: {e}")
            return False

        # 3. Check trading hours from template
        if not self.is_trading_hours():
            logger.info("Outside trading hours - monitoring disabled")
            return False

        logger.info("All conditions met - monitoring can start")
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
            logger.info(f"Market holiday: {is_holiday.holiday_name}")
            return False

        # Get trading sessions for today
        sessions = TradingSession.query.join(TradingHoursTemplate).filter(
            TradingSession.day_of_week == day_of_week,
            TradingSession.is_active == True,
            TradingHoursTemplate.is_active == True
        ).all()

        if not sessions:
            logger.info(f"No trading sessions configured for day {day_of_week}")
            return False

        # Check if current time is within any session
        for session in sessions:
            if session.start_time <= current_time <= session.end_time:
                logger.info(
                    f"Within trading hours: {session.session_name} "
                    f"({session.start_time} - {session.end_time})"
                )
                return True

        logger.info(f"Outside all trading sessions for day {day_of_week}")
        return False

    def get_open_positions(self) -> List[StrategyExecution]:
        """
        Get all open positions from database.

        Filters:
        - status='entered'
        - broker_order_status NOT IN ['rejected', 'cancelled']

        Returns:
            List[StrategyExecution]: List of open position executions
        """
        try:
            # Query for entered positions
            open_executions = StrategyExecution.query.filter(
                StrategyExecution.status == 'entered'
            ).all()

            # Filter out rejected/cancelled orders
            filtered_executions = []
            for execution in open_executions:
                # Check if broker_order_status exists and is not rejected/cancelled
                if hasattr(execution, 'broker_order_status') and execution.broker_order_status:
                    if execution.broker_order_status.lower() in ['rejected', 'cancelled']:
                        continue

                filtered_executions.append(execution)

            logger.info(f"Found {len(filtered_executions)} open positions to monitor")
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
            logger.info("No open positions to monitor")
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

        logger.info(f"Subscribing to {len(symbols_to_subscribe)} unique symbols")

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

                logger.info(
                    f"Subscribed to {data['symbol']} "
                    f"({len(data['executions'])} positions)"
                )

            except Exception as e:
                logger.error(f"Failed to subscribe to {data['symbol']}: {e}")

        logger.info(
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

            logger.info(f"Unsubscribed from {symbol}")

        except Exception as e:
            logger.error(f"Error unsubscribing from {symbol}: {e}")

    def on_order_filled(self, execution: StrategyExecution):
        """
        Called by OrderStatusPoller when an order fills.
        Adds the position to monitoring.

        Args:
            execution: The filled strategy execution
        """
        if not self.is_running:
            logger.debug("Monitor not running - ignoring order fill")
            return

        key = f"{execution.symbol}_{execution.exchange}"

        # Check if we're already monitoring this symbol
        if key in self.subscribed_symbols:
            # Add to existing position tracking
            if key in self.position_map:
                self.position_map[key].append(execution)
            logger.info(f"Added {execution.symbol} to existing monitoring")
            return

        # Subscribe to new symbol
        try:
            if self.websocket_manager:
                self.websocket_manager.subscribe({
                    'symbol': execution.symbol,
                    'exchange': execution.exchange,
                    'mode': 'quote'
                })

                self.subscribed_symbols.add(key)
                self.position_map[key] = [execution]

                logger.info(f"New position filled - subscribed to {execution.symbol}")

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
        key = f"{execution.symbol}_{execution.exchange}"

        if key not in self.position_map:
            return

        # Remove from position map
        positions = self.position_map[key]
        if execution in positions:
            positions.remove(execution)

        # If no more positions for this symbol, unsubscribe
        if not positions:
            self.unsubscribe_from_symbol(execution.symbol, execution.exchange)
            logger.info(
                f"No more positions for {execution.symbol} - unsubscribed"
            )
        else:
            logger.info(
                f"Position closed for {execution.symbol} - "
                f"{len(positions)} positions remaining"
            )

    def update_last_price(self, symbol: str, exchange: str, ltp: float):
        """
        Update last traded price for all positions of a symbol.
        Called by WebSocket message handler.

        Args:
            symbol: Trading symbol
            exchange: Exchange name
            ltp: Last traded price
        """
        key = f"{symbol}_{exchange}"

        if key not in self.position_map:
            return

        try:
            now = datetime.utcnow()

            # Update all positions for this symbol
            for execution in self.position_map[key]:
                execution.last_price = ltp
                execution.last_price_updated = now
                execution.websocket_subscribed = True

            db.session.commit()

            logger.debug(
                f"Updated LTP for {symbol}: {ltp} "
                f"({len(self.position_map[key])} positions)"
            )

        except Exception as e:
            logger.error(f"Error updating last price for {symbol}: {e}")
            db.session.rollback()

    def _handle_websocket_data(self, data: Dict):
        """
        WebSocket data handler - updates last prices for monitored positions.

        Note: This is called from WebSocket callbacks which may not have app context.
        We need to provide app context for database operations.

        Args:
            data: WebSocket market data
        """
        try:
            symbol = data.get('symbol')
            exchange = data.get('exchange', 'NFO')
            ltp = data.get('ltp')

            if not symbol or not ltp:
                return

            # WebSocket callbacks run in separate thread - need app context
            from flask import current_app, has_app_context
            if has_app_context():
                # Already in app context
                self.update_last_price(symbol, exchange, float(ltp))
            else:
                # Need to create app context
                with current_app.app_context():
                    self.update_last_price(symbol, exchange, float(ltp))

        except Exception as e:
            logger.error(f"Error handling WebSocket data: {e}")

    def start(self, websocket_manager):
        """
        Start position monitoring.

        Args:
            websocket_manager: WebSocket manager instance
        """
        if self.is_running:
            logger.warning("Position monitor already running")
            return

        # Store websocket manager reference
        self.websocket_manager = websocket_manager

        # Check if monitoring should start (app context provided by caller)
        if not self.should_start_monitoring():
            logger.info("Position monitoring not started - conditions not met")
            return

        # Register WebSocket data handler for quote updates
        if hasattr(websocket_manager, 'data_processor'):
            websocket_manager.data_processor.register_quote_handler(self._handle_websocket_data)
            logger.info("Position monitor registered for WebSocket quote updates")

        # Subscribe to open positions
        self.subscribe_to_positions()

        self.is_running = True
        logger.info("Position monitoring started")

    def stop(self):
        """Stop position monitoring and unsubscribe from all symbols"""
        if not self.is_running:
            return

        logger.info("Stopping position monitoring...")

        # Unsubscribe from all symbols
        for key in list(self.subscribed_symbols):
            symbol, exchange = key.split('_', 1)
            self.unsubscribe_from_symbol(symbol, exchange)

        self.is_running = False
        self.subscribed_symbols.clear()
        self.position_map.clear()

        logger.info("Position monitoring stopped")

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
