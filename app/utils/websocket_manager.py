"""
Professional WebSocket Manager with Account Failover
Handles real-time data streaming using OpenAlgo Python SDK

Uses OpenAlgo SDK for WebSocket connections with enterprise-grade reliability.
"""

import json
import logging
import threading
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Any, Callable
import pytz

# OpenAlgo SDK
from openalgo import api

# Cross-platform compatibility
from app.utils.compat import sleep, spawn, create_lock

logger = logging.getLogger(__name__)


class ExponentialBackoff:
    """Exponential backoff strategy for reconnection"""

    def __init__(self, base=2, max_delay=60):
        self.base = base
        self.max_delay = max_delay
        self.attempt = 0

    def get_next_delay(self):
        delay = min(self.base ** self.attempt, self.max_delay)
        self.attempt += 1
        return delay

    def reset(self):
        self.attempt = 0


class WebSocketDataProcessor:
    """Process incoming WebSocket data based on subscription mode"""

    def __init__(self):
        self.quote_handlers = []
        self.depth_handlers = []
        self.ltp_handlers = []

    def register_quote_handler(self, handler):
        self.quote_handlers.append(handler)

    def register_depth_handler(self, handler):
        self.depth_handlers.append(handler)

    def register_ltp_handler(self, handler):
        self.ltp_handlers.append(handler)

    def on_data_received(self, data):
        """
        Process incoming WebSocket data based on subscription mode.
        OpenAlgo SDK format:
        {'type': 'market_data', 'symbol': 'INFY', 'exchange': 'NSE', 'mode': 2,
         'data': {'open': 1585.0, 'high': 1606.8, 'low': 1585.0, 'close': 1598.2,
                  'ltp': 1605.8, 'volume': 1930758, 'timestamp': 1765781412568}}
        """
        try:
            # Get mode from data (1=LTP, 2=Quote, 3=Depth)
            mode = data.get('mode', 1)
            symbol = data.get('symbol', 'UNKNOWN')

            # Extract market data from nested 'data' field if present
            market_data = data.get('data', data)

            # Merge symbol info into market_data for handlers
            if not market_data.get('symbol'):
                market_data['symbol'] = symbol
                market_data['exchange'] = data.get('exchange', 'NFO')

            logger.debug(f"[DATA_PROCESSOR] Routing data for {symbol}, mode={mode}")

            if mode == 3 or mode == 'depth':
                market_data['mode'] = 'depth'
                self.handle_depth_update(market_data)
            elif mode == 2 or mode == 'quote':
                market_data['mode'] = 'quote'
                self.handle_quote_update(market_data)
            else:  # mode == 1 or 'ltp'
                market_data['mode'] = 'ltp'
                self.handle_ltp_update(market_data)

        except Exception as e:
            logger.error(f"Error processing WebSocket data: {e}, Data: {data}")

    def handle_quote_update(self, data):
        """Process quote mode data"""
        for handler in self.quote_handlers:
            try:
                handler(data)
            except Exception as e:
                logger.error(f"Error in quote handler: {e}")

    def handle_depth_update(self, data):
        """Process depth mode data (option strikes)"""
        for handler in self.depth_handlers:
            try:
                handler(data)
            except Exception as e:
                logger.error(f"Error in depth handler: {e}")

    def handle_ltp_update(self, data):
        """Process LTP mode data"""
        for handler in self.ltp_handlers:
            try:
                handler(data)
            except Exception as e:
                logger.error(f"Error in LTP handler: {e}")


class ProfessionalWebSocketManager:
    """
    Enterprise-Grade WebSocket Connection Management using OpenAlgo SDK
    with Account Failover support.
    """

    def __init__(self):
        self.connection_pool = {}
        self.max_connections = 10
        self.heartbeat_interval = 30
        self.reconnect_attempts = 3
        self.backoff_strategy = ExponentialBackoff(base=2, max_delay=60)
        self.account_failover_enabled = True
        self.data_processor = WebSocketDataProcessor()
        self.subscriptions = {}  # {mode: [instruments]}
        self.active = False
        self.client = None
        self.authenticated = False
        self._lock = create_lock()

        # Connection parameters
        self.host_url = None
        self.ws_url = None
        self.api_key = None

        # Cache for valid (non-zero) values to prevent zero-value issues
        # WebSocket sometimes returns 0 which can trigger risk management incorrectly
        self._valid_ltp_cache = {}  # {symbol_key: last_valid_ltp}
        self._valid_quote_cache = {}  # {symbol_key: last_valid_quote}

    def create_connection_pool(self, primary_account, backup_accounts=None):
        """
        Create managed connection pool with multi-account failover capability
        """
        pool = {
            'current_account': primary_account,
            'backup_accounts': backup_accounts or [],
            'connections': {},
            'status': 'initializing',
            'failover_history': [],
            'metrics': {
                'account_switches': 0,
                'total_failures': 0,
                'current_health': 100,
                'messages_received': 0,
                'messages_dropped': 0,
                'reconnect_count': 0,
                'uptime_seconds': 0,
                'last_message_time': None
            }
        }

        # Initialize primary account connection
        pool['connections']['primary'] = {
            'account': primary_account,
            'status': 'active',
            'failure_count': 0
        }

        # Pre-configure backup accounts (standby mode)
        for idx, backup_account in enumerate((backup_accounts or [])[:3]):
            pool['connections'][f'backup_{idx}'] = {
                'account': backup_account,
                'status': 'standby',
                'failure_count': 0
            }

        self.connection_pool = pool
        logger.debug(f"Connection pool created with {len(backup_accounts or [])} backup accounts")
        return pool

    def connect(self, ws_url: str, api_key: str, host_url: str = None):
        """Establish WebSocket connection using OpenAlgo SDK"""
        try:
            self.ws_url = ws_url
            self.api_key = api_key
            self.host_url = host_url or ws_url.replace('ws://', 'http://').replace(':8765', ':5000')
            self.authenticated = False

            logger.debug(f"Connecting to OpenAlgo WebSocket: {ws_url}")

            # Initialize OpenAlgo client with WebSocket support
            self.client = api(
                api_key=api_key,
                host=self.host_url,
                ws_url=ws_url
            )

            # Connect to WebSocket
            self.client.connect()
            self.active = True
            self.authenticated = True

            # Wait for connection to stabilize
            sleep(2)

            # Resubscribe to all previous subscriptions
            if any(self.subscriptions.values()):
                self.resubscribe_all()

            self.backoff_strategy.reset()
            logger.debug("OpenAlgo WebSocket connection established")
            return True

        except Exception as e:
            logger.error(f"Failed to connect WebSocket: {e}")
            self.handle_connection_failure()
            return False

    def _on_ltp_data(self, data):
        """Internal handler for LTP data from OpenAlgo SDK"""
        try:
            if self.connection_pool:
                self.connection_pool['metrics']['messages_received'] += 1
                self.connection_pool['metrics']['last_message_time'] = datetime.now()
            self.data_processor.on_data_received(data)
        except Exception as e:
            logger.error(f"Error in LTP data handler: {e}")

    def _on_quote_data(self, data):
        """Internal handler for Quote data from OpenAlgo SDK"""
        try:
            if self.connection_pool:
                self.connection_pool['metrics']['messages_received'] += 1
                self.connection_pool['metrics']['last_message_time'] = datetime.now()
            self.data_processor.on_data_received(data)
        except Exception as e:
            logger.error(f"Error in Quote data handler: {e}")

    def _on_depth_data(self, data):
        """Internal handler for Depth data from OpenAlgo SDK"""
        try:
            if self.connection_pool:
                self.connection_pool['metrics']['messages_received'] += 1
                self.connection_pool['metrics']['last_message_time'] = datetime.now()
            self.data_processor.on_data_received(data)
        except Exception as e:
            logger.error(f"Error in Depth data handler: {e}")

    def subscribe_batch(self, instruments: List[Dict], mode: str = 'ltp'):
        """
        Subscribe to multiple instruments using OpenAlgo SDK
        instruments: list of dicts with 'symbol' and 'exchange' keys
        mode: subscription mode ('ltp', 'quote', 'depth')
        """
        try:
            if not instruments:
                logger.warning("[WS_BATCH] No instruments provided")
                return False

            if not self.client or not self.active:
                logger.warning("[WS_BATCH] Not connected, queuing batch subscription")
                if mode not in self.subscriptions:
                    self.subscriptions[mode] = []
                self.subscriptions[mode].extend(instruments)
                return False

            logger.debug(f"[WS_BATCH] Subscribing to {len(instruments)} instruments in {mode} mode")

            # Store subscriptions for reconnection
            if mode not in self.subscriptions:
                self.subscriptions[mode] = []
            self.subscriptions[mode].extend(instruments)

            # Subscribe using OpenAlgo SDK based on mode
            if mode == 'ltp':
                self.client.subscribe_ltp(instruments, on_data_received=self._on_ltp_data)
            elif mode == 'quote':
                self.client.subscribe_quote(instruments, on_data_received=self._on_quote_data)
            elif mode == 'depth':
                self.client.subscribe_depth(instruments, on_data_received=self._on_depth_data)
            else:
                logger.error(f"[WS_BATCH] Unknown mode: {mode}")
                return False

            logger.debug(f"[WS_BATCH] Successfully subscribed to {len(instruments)} instruments")
            return True

        except Exception as e:
            logger.error(f"[WS_BATCH] Error: {e}")
            return False

    def subscribe(self, subscription: Dict):
        """Subscribe to single symbol with specified mode"""
        try:
            symbol = subscription.get('symbol')
            exchange = subscription.get('exchange')
            mode = subscription.get('mode', 'ltp')

            if not symbol or not exchange:
                logger.error(f"[WS_SUBSCRIBE] Missing symbol or exchange")
                return False

            instruments = [{'symbol': symbol, 'exchange': exchange}]
            return self.subscribe_batch(instruments, mode)

        except Exception as e:
            logger.error(f"[WS_SUBSCRIBE] Error: {e}")
            return False

    def unsubscribe_batch(self, instruments: List[Dict], mode: str = 'ltp'):
        """Unsubscribe from multiple instruments"""
        try:
            if not self.client or not self.active:
                logger.warning("[WS_UNSUB] Not connected")
                return False

            if mode == 'ltp':
                self.client.unsubscribe_ltp(instruments)
            elif mode == 'quote':
                self.client.unsubscribe_quote(instruments)
            elif mode == 'depth':
                self.client.unsubscribe_depth(instruments)

            # Remove from subscriptions
            if mode in self.subscriptions:
                for inst in instruments:
                    key = f"{inst['exchange']}:{inst['symbol']}"
                    self.subscriptions[mode] = [
                        s for s in self.subscriptions[mode]
                        if f"{s['exchange']}:{s['symbol']}" != key
                    ]

            logger.debug(f"[WS_UNSUB] Unsubscribed from {len(instruments)} instruments")
            return True

        except Exception as e:
            logger.error(f"[WS_UNSUB] Error: {e}")
            return False

    def unsubscribe(self, subscription: Dict):
        """Unsubscribe from single symbol"""
        instruments = [{'symbol': subscription.get('symbol'), 'exchange': subscription.get('exchange')}]
        mode = subscription.get('mode', 'ltp')
        return self.unsubscribe_batch(instruments, mode)

    def resubscribe_all(self):
        """Resubscribe to all symbols after reconnection"""
        total_count = sum(len(v) for v in self.subscriptions.values())
        logger.debug(f"Resubscribing to {total_count} total instruments")

        for mode, instruments in self.subscriptions.items():
            if instruments:
                try:
                    # Clear the list before resubscribing to avoid duplicates
                    instruments_copy = instruments.copy()
                    self.subscriptions[mode] = []
                    self.subscribe_batch(instruments_copy, mode)
                except Exception as e:
                    logger.error(f"Failed to resubscribe {mode} instruments: {e}")

    def handle_connection_failure(self):
        """Handle complete connection failure"""
        if self.account_failover_enabled and self.connection_pool:
            self.attempt_account_failover()

    def attempt_account_failover(self):
        """Attempt to switch to backup account"""
        backup_accounts = self.connection_pool.get('backup_accounts', [])

        if backup_accounts:
            previous_account = self.connection_pool.get('current_account')
            from_account_name = previous_account.account_name if previous_account and hasattr(previous_account, 'account_name') else 'Unknown'

            next_account = backup_accounts[0]
            logger.debug(f"Switching from {from_account_name} to backup account: {next_account.account_name}")

            # Update connection pool
            self.connection_pool['current_account'] = next_account
            self.connection_pool['backup_accounts'] = backup_accounts[1:]
            self.connection_pool['metrics']['account_switches'] += 1

            # Add failover event to history
            self.connection_pool['failover_history'].append({
                'timestamp': datetime.now().isoformat(),
                'from_account': from_account_name,
                'to_account': next_account.account_name,
                'reason': 'Connection failure after max reconnection attempts'
            })

            # Connect with new account
            if hasattr(next_account, 'websocket_url') and hasattr(next_account, 'get_api_key'):
                host_url = next_account.host_url if hasattr(next_account, 'host_url') else None

                if self.connect(next_account.websocket_url, next_account.get_api_key(), host_url):
                    logger.debug(f"Successfully connected to backup account: {next_account.account_name}")
                else:
                    logger.error(f"Failed to connect to backup account: {next_account.account_name}")
                    if self.connection_pool.get('backup_accounts'):
                        self.attempt_account_failover()
            else:
                logger.error("Backup account missing required attributes")
        else:
            logger.critical("No backup accounts available for failover")

    def disconnect(self):
        """Disconnect WebSocket"""
        try:
            self.active = False
            self.authenticated = False

            if self.client:
                try:
                    self.client.disconnect()
                except Exception as e:
                    logger.warning(f"Error during disconnect: {e}")
                self.client = None

            logger.debug("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting WebSocket: {e}")

    def get_status(self):
        """Get WebSocket connection status"""
        if not self.connection_pool:
            return {'status': 'not_initialized'}

        total_subscriptions = sum(len(v) for v in self.subscriptions.values())

        return {
            'status': 'active' if self.active else 'inactive',
            'authenticated': self.authenticated,
            'current_account': getattr(self.connection_pool.get('current_account'), 'account_name', 'Unknown'),
            'backup_accounts': len(self.connection_pool.get('backup_accounts', [])),
            'metrics': self.connection_pool.get('metrics', {}),
            'subscriptions': total_subscriptions,
            'subscriptions_by_mode': {k: len(v) for k, v in self.subscriptions.items()},
            'connected': self.active and self.client is not None
        }

    def register_handler(self, mode: str, handler: Callable):
        """Register data handler for specific mode"""
        if mode == 'quote':
            self.data_processor.register_quote_handler(handler)
        elif mode == 'depth':
            self.data_processor.register_depth_handler(handler)
        elif mode == 'ltp':
            self.data_processor.register_ltp_handler(handler)

    def get_ltp(self):
        """
        Get cached LTP data from OpenAlgo SDK with zero-value protection.

        If WebSocket returns 0 for a symbol, return the last valid cached value instead.
        This prevents risk management from triggering incorrectly due to zero values.
        """
        if self.client:
            try:
                raw_data = self.client.get_ltp()
                raw_ltp = raw_data.get('ltp', {})

                # Validate and cache non-zero values
                validated_ltp = {}
                for symbol_key, ltp_value in raw_ltp.items():
                    try:
                        ltp_float = float(ltp_value) if ltp_value is not None else 0
                    except (ValueError, TypeError):
                        ltp_float = 0

                    if ltp_float > 0:
                        # Valid non-zero value - cache it and use it
                        self._valid_ltp_cache[symbol_key] = ltp_float
                        validated_ltp[symbol_key] = ltp_float
                    elif symbol_key in self._valid_ltp_cache:
                        # Zero value - use cached valid value instead
                        logger.warning(f"[WS_LTP] Zero value received for {symbol_key}, using cached value: {self._valid_ltp_cache[symbol_key]}")
                        validated_ltp[symbol_key] = self._valid_ltp_cache[symbol_key]
                    else:
                        # Zero value and no cache - skip this symbol
                        logger.warning(f"[WS_LTP] Zero value received for {symbol_key}, no cached value available")

                return {'ltp': validated_ltp}
            except Exception as e:
                logger.error(f"Error getting LTP: {e}")

        # If client not available, return cached values if any
        if self._valid_ltp_cache:
            return {'ltp': self._valid_ltp_cache.copy()}
        return {'ltp': {}}

    def get_quotes(self):
        """
        Get cached Quote data from OpenAlgo SDK with zero-value protection.

        If WebSocket returns 0 for LTP in a quote, return the last valid cached quote instead.
        This prevents risk management from triggering incorrectly due to zero values.
        """
        if self.client:
            try:
                raw_data = self.client.get_quotes()
                raw_quotes = raw_data.get('quote', {})

                # Validate and cache quotes with non-zero LTP
                validated_quotes = {}
                for symbol_key, quote_data in raw_quotes.items():
                    if isinstance(quote_data, dict):
                        try:
                            ltp = float(quote_data.get('ltp', 0)) if quote_data.get('ltp') is not None else 0
                        except (ValueError, TypeError):
                            ltp = 0

                        if ltp > 0:
                            # Valid quote with non-zero LTP - cache it and use it
                            self._valid_quote_cache[symbol_key] = quote_data.copy()
                            validated_quotes[symbol_key] = quote_data
                        elif symbol_key in self._valid_quote_cache:
                            # Zero LTP - use cached valid quote instead
                            logger.warning(f"[WS_QUOTE] Zero LTP in quote for {symbol_key}, using cached quote")
                            validated_quotes[symbol_key] = self._valid_quote_cache[symbol_key]
                        else:
                            # Zero value and no cache - skip this symbol
                            logger.warning(f"[WS_QUOTE] Zero LTP in quote for {symbol_key}, no cached quote available")

                return {'quote': validated_quotes}
            except Exception as e:
                logger.error(f"Error getting quotes: {e}")

        # If client not available, return cached values if any
        if self._valid_quote_cache:
            return {'quote': self._valid_quote_cache.copy()}
        return {'quote': {}}

    def get_depth(self):
        """Get cached Depth data from OpenAlgo SDK"""
        if self.client:
            try:
                return self.client.get_depth()
            except Exception as e:
                logger.error(f"Error getting depth: {e}")
        return {'depth': {}}
