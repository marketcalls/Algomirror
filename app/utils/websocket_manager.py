"""
Professional WebSocket Manager with Account Failover
Handles real-time data streaming with enterprise-grade reliability
"""

import json
import threading
import time
import logging
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Any
import websocket
import pytz

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
        Process incoming WebSocket data based on subscription mode
        """
        try:
            mode = data.get('mode', 'ltp')
            symbol = data.get('symbol', 'UNKNOWN')
            
            logger.debug(f"[DATA_PROCESSOR] Routing data for {symbol}, mode={mode}, handlers: quote={len(self.quote_handlers)}, depth={len(self.depth_handlers)}, ltp={len(self.ltp_handlers)}")
            
            if mode == 'quote':
                self.handle_quote_update(data)
            elif mode == 'depth':
                self.handle_depth_update(data)
            else:  # ltp
                self.handle_ltp_update(data)
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
    Enterprise-Grade WebSocket Connection Management with Account Failover
    """
    
    def __init__(self):
        self.connection_pool = {}
        self.max_connections = 10
        self.heartbeat_interval = 30
        self.reconnect_attempts = 3  # Reduced for faster failover
        self.backoff_strategy = ExponentialBackoff(base=2, max_delay=60)
        self.account_failover_enabled = True
        self.data_processor = WebSocketDataProcessor()
        self.subscriptions = set()
        self.active = False
        self.ws = None
        self.ws_thread = None
        self.reconnect_thread = None
        self._lock = threading.Lock()
    
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
            'ws_primary': None,
            'ws_backup': None,
            'status': 'active',
            'failure_count': 0
        }
        
        # Pre-configure backup accounts (standby mode)
        for idx, backup_account in enumerate((backup_accounts or [])[:3]):
            pool['connections'][f'backup_{idx}'] = {
                'account': backup_account,
                'ws_primary': None,
                'ws_backup': None,
                'status': 'standby',
                'failure_count': 0
            }
        
        self.connection_pool = pool
        logger.info(f"Connection pool created with {len(backup_accounts or [])} backup accounts")
        return pool
    
    def connect(self, ws_url, api_key):
        """Establish WebSocket connection"""
        try:
            self.ws_url = ws_url
            self.api_key = api_key
            self.authenticated = False
            self.connection_failed = False
            
            # Create WebSocket connection (no header auth - uses message auth)
            self.ws = websocket.WebSocketApp(
                ws_url,
                on_open=self.on_open,
                on_message=self.on_message,
                on_error=self.on_error,
                on_close=self.on_close
            )
            
            # Start WebSocket in separate thread
            self.ws_thread = threading.Thread(target=self.ws.run_forever)
            self.ws_thread.daemon = True
            self.ws_thread.start()
            
            # Wait for connection to establish
            time.sleep(2)
            
            # Check if connection was immediately refused
            if self.connection_failed:
                logger.warning("Connection immediately failed")
                return False
            
            self.active = True
            logger.info("WebSocket connection established")
            return True
            
        except Exception as e:
            logger.error(f"Failed to connect WebSocket: {e}")
            self.handle_connection_failure()
            return False
    
    def on_open(self, ws):
        """WebSocket opened callback"""
        logger.info("WebSocket connection opened")
        self.backoff_strategy.reset()
        
        # Send authentication message (OpenAlgo style)
        self.authenticate()
        
        # Resubscribe to all symbols after auth
        if self.subscriptions and self.authenticated:
            self.resubscribe_all()
    
    def authenticate(self):
        """Authenticate with WebSocket server using OpenAlgo protocol"""
        if self.ws:
            auth_msg = {
                "action": "authenticate",
                "api_key": self.api_key
            }
            logger.info(f"Authenticating with API key: {self.api_key[:8]}...{self.api_key[-8:]}")
            self.ws.send(json.dumps(auth_msg))
    
    def on_message(self, ws, message):
        """Handle incoming WebSocket messages"""
        try:
            data = json.loads(message)
            
            # Log ALL incoming messages for debugging - include full data structure
            # Reduced logging - only log non-market data or use debug level
            msg_type = data.get('type')
            if msg_type != 'market_data':
                logger.info(f"[WS_MSG] Received: type={msg_type}, symbol={data.get('symbol')}, exchange={data.get('exchange')}")
            else:
                logger.debug(f"[WS_DATA] Market data for {data.get('symbol')}")
            
            # Handle authentication response
            if data.get("type") == "auth":
                if data.get("status") == "success":
                    self.authenticated = True
                    logger.info("Authentication successful!")
                    # Resubscribe after successful auth
                    if self.subscriptions:
                        self.resubscribe_all()
                else:
                    logger.error(f"Authentication failed: {data}")
                    self.authenticated = False
                return
            
            # Handle subscription response
            if data.get("type") == "subscribe":
                logger.info(f"[WS_SUB] Subscription response: status={data.get('status')}, message={data.get('message')}")
                return
            
            # Update metrics
            if self.connection_pool:
                self.connection_pool['metrics']['messages_received'] += 1
                self.connection_pool['metrics']['last_message_time'] = datetime.now()
            
            # Process market data - handle various message types
            # OpenAlgo WebSocket format includes nested 'data' field
            if data.get("type") == "market_data":
                # Extract actual market data from nested 'data' field
                market_data = data.get('data', {})
                if not market_data:
                    market_data = data  # Fallback to root level
                
                # Merge symbol info from root level if needed
                if not market_data.get('symbol') and data.get('symbol'):
                    market_data['symbol'] = data['symbol']
                    market_data['exchange'] = data.get('exchange', 'NFO')
                
                # Force mode to 'depth' for option data (mode 3)
                if data.get('mode') == 3 or market_data.get('mode') == 3:
                    market_data['mode'] = 'depth'
                elif data.get('mode') == 2 or market_data.get('mode') == 2:
                    market_data['mode'] = 'quote'
                else:
                    market_data['mode'] = 'ltp'
                
                logger.debug(f"[WS_DATA] Processing market data for {market_data.get('symbol')}: LTP={market_data.get('ltp')}")
                self.data_processor.on_data_received(market_data)
            elif data.get("ltp") is not None or data.get("symbol"):
                # Direct data format
                # Ensure mode is set based on data structure
                if 'bids' in data or 'asks' in data or 'depth' in data:
                    data['mode'] = 'depth'
                elif 'open' in data or 'high' in data or 'low' in data:
                    data['mode'] = 'quote'
                else:
                    data['mode'] = 'ltp'
                
                logger.debug(f"[WS_DATA] Processing price update for {data.get('symbol')}: mode={data['mode']}, LTP={data.get('ltp')}")
                self.data_processor.on_data_received(data)
            else:
                logger.debug(f"[WS_UNKNOWN] Unhandled message type: {data}")
            
        except json.JSONDecodeError:
            logger.error(f"Invalid JSON message: {message[:100]}...")
        except Exception as e:
            logger.error(f"Error processing message: {e}, Data: {message[:100]}...")
    
    def on_error(self, ws, error):
        """WebSocket error callback"""
        logger.error(f"WebSocket error: {error}")
        
        # Check if this is a connection refused error
        error_str = str(error)
        if "10061" in error_str or "Connection refused" in error_str or "actively refused" in error_str:
            self.connection_failed = True
            logger.warning("Connection refused by server, marking for failover")
        
        if self.connection_pool:
            self.connection_pool['metrics']['total_failures'] += 1
    
    def on_close(self, ws, close_status_code=None, close_msg=None):
        """WebSocket closed callback"""
        logger.warning(f"WebSocket connection closed - Code: {close_status_code}, Message: {close_msg}")
        
        if self.active:
            # Attempt reconnection
            self.schedule_reconnection()
    
    def schedule_reconnection(self):
        """Schedule reconnection with exponential backoff"""
        if not self.reconnect_thread or not self.reconnect_thread.is_alive():
            self.reconnect_thread = threading.Thread(target=self.reconnect_with_backoff)
            self.reconnect_thread.daemon = True
            self.reconnect_thread.start()
    
    def reconnect_with_backoff(self):
        """Reconnect with exponential backoff"""
        connection_refused_count = 0
        
        for attempt in range(self.reconnect_attempts):
            delay = self.backoff_strategy.get_next_delay()
            logger.info(f"Reconnection attempt {attempt + 1}/{self.reconnect_attempts} in {delay} seconds")
            time.sleep(delay)
            
            try:
                # Reset the connection_failed flag before each attempt
                self.connection_failed = False
                
                # First try to reconnect to the current account
                current_account = self.connection_pool.get('current_account') if self.connection_pool else None
                
                # Use current account credentials if available, otherwise use stored ones
                if current_account and hasattr(current_account, 'websocket_url'):
                    ws_url = current_account.websocket_url
                    api_key = current_account.get_api_key()
                else:
                    ws_url = self.ws_url
                    api_key = self.api_key
                
                # Try to connect
                connected = self.connect(ws_url, api_key)
                
                # Check if connection succeeded or was refused
                if connected:
                    logger.info("Reconnection successful")
                    
                    # Resubscribe to all previous subscriptions
                    if self.subscriptions:
                        logger.info(f"Resubscribing to {len(self.subscriptions)} symbols after reconnection")
                        time.sleep(1)  # Wait for authentication
                        for sub_str in list(self.subscriptions):
                            subscription = json.loads(sub_str)
                            self.subscribe(subscription)
                    return
                else:
                    # Check if connection was refused
                    if self.connection_failed:
                        connection_refused_count += 1
                        logger.warning(f"Reconnection attempt {attempt + 1} failed - connection refused (count: {connection_refused_count})")
                        
                        # If connection is refused twice, immediately trigger failover
                        if connection_refused_count >= 2:
                            logger.error("Connection refused multiple times, immediately triggering failover")
                            break
                    else:
                        logger.warning(f"Reconnection attempt {attempt + 1} failed - general failure")
                    # Continue to next attempt
                    
            except Exception as e:
                logger.error(f"Reconnection attempt {attempt + 1} failed with exception: {e}")
        
        # All reconnection attempts failed, initiate failover
        logger.error(f"Max reconnection attempts ({self.reconnect_attempts}) reached, initiating failover")
        self.handle_connection_failure()
    
    def handle_connection_failure(self):
        """Handle complete connection failure"""
        if self.account_failover_enabled and self.connection_pool:
            self.attempt_account_failover()
    
    def attempt_account_failover(self):
        """Attempt to switch to backup account"""
        backup_accounts = self.connection_pool.get('backup_accounts', [])
        
        if backup_accounts:
            # Store the previous account name before updating
            previous_account = self.connection_pool.get('current_account')
            from_account_name = previous_account.account_name if previous_account and hasattr(previous_account, 'account_name') else 'Unknown'
            
            next_account = backup_accounts[0]
            logger.info(f"Switching from {from_account_name} to backup account: {next_account.account_name}")
            
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
                logger.info(f"Attempting to connect to backup WebSocket: {next_account.websocket_url}")
                
                # Update stored credentials for future reconnection attempts
                self.ws_url = next_account.websocket_url
                self.api_key = next_account.get_api_key()
                
                # Reset connection_failed flag before attempting backup connection
                self.connection_failed = False
                
                # Try to connect with the backup account
                if self.connect(next_account.websocket_url, next_account.get_api_key()):
                    logger.info(f"Successfully connected to backup account: {next_account.account_name}")
                    
                    # Resubscribe to all previous subscriptions
                    if self.subscriptions:
                        logger.info(f"Resubscribing to {len(self.subscriptions)} symbols after failover")
                        time.sleep(1)  # Wait for authentication
                        for sub_str in list(self.subscriptions):
                            subscription = json.loads(sub_str)
                            self.subscribe(subscription)
                else:
                    logger.error(f"Failed to connect to backup account: {next_account.account_name}")
                    # Try next backup if available
                    if self.connection_pool.get('backup_accounts'):
                        self.attempt_account_failover()
            else:
                logger.error(f"Backup account missing required attributes: websocket_url or get_api_key")
        else:
            logger.critical("No backup accounts available for failover")
    
    def subscribe_batch(self, instruments, mode='ltp'):
        """
        Subscribe to multiple instruments using OpenAlgo format
        instruments: list of dicts with 'symbol' and 'exchange' keys
        mode: subscription mode ('ltp', 'quote', 'depth')
        """
        try:
            if not instruments:
                logger.warning("[WS_BATCH] No instruments provided")
                return False

            # Check if authenticated
            if not self.authenticated:
                logger.warning("[WS_BATCH] Not authenticated, queuing batch subscription")
                # Queue individual subscriptions for later
                for inst in instruments:
                    subscription = {
                        'symbol': inst.get('symbol'),
                        'exchange': inst.get('exchange'),
                        'mode': mode
                    }
                    self.subscriptions.add(json.dumps(subscription))
                return False

            if self.ws and self.ws.sock and self.ws.sock.connected:
                # Map mode names to numbers
                mode_map = {
                    'ltp': 1,      # Mode 1 for LTP
                    'quote': 2,    # Mode 2 for Quote
                    'depth': 3     # Mode 3 for Market Depth
                }

                mode_num = mode_map.get(mode, 1)  # Default to LTP

                logger.info(f"[WS_BATCH] Subscribing to {len(instruments)} instruments in {mode} mode")

                # Send individual subscription messages for each instrument
                for inst in instruments:
                    symbol = inst.get('symbol')
                    exchange = inst.get('exchange')

                    if not symbol or not exchange:
                        logger.warning(f"[WS_BATCH] Skipping invalid instrument: {inst}")
                        continue

                    message = {
                        'action': 'subscribe',
                        'symbol': symbol,
                        'exchange': exchange,
                        'mode': mode_num,
                        'depth': 5  # Default depth level
                    }

                    self.ws.send(json.dumps(message))

                    # Add to subscriptions for tracking
                    subscription = {
                        'symbol': symbol,
                        'exchange': exchange,
                        'mode': mode
                    }
                    self.subscriptions.add(json.dumps(subscription))

                    # Minimal delay between subscriptions
                    # Removed to prevent blocking Flask startup

                return True
            else:
                logger.warning("[WS_BATCH] WebSocket not connected")
                return False

        except Exception as e:
            logger.error(f"[WS_BATCH] Error: {e}")
            return False
    
    def subscribe(self, subscription):
        """Subscribe to symbol with specified mode"""
        try:
            symbol = subscription.get('symbol')
            exchange = subscription.get('exchange')
            mode = subscription.get('mode', 'ltp')

            # Validate required fields
            if not symbol or not exchange:
                logger.error(f"[WS_SUBSCRIBE] Missing symbol or exchange: symbol={symbol}, exchange={exchange}")
                return False

            logger.info(f"[WS_SUBSCRIBE] Request: {symbol} on {exchange} in {mode} mode")

            # Check if authenticated
            if not self.authenticated:
                logger.warning(f"[WS_SUBSCRIBE] Not authenticated, queuing {symbol}")
                self.subscriptions.add(json.dumps(subscription))
                return False

            if self.ws and self.ws.sock and self.ws.sock.connected:
                # Use exact OpenAlgo subscription format - individual messages per symbol
                # Map mode names to numbers
                mode_map = {
                    'ltp': 1,      # Mode 1 for LTP
                    'quote': 2,    # Mode 2 for Quote
                    'depth': 3     # Mode 3 for Market Depth
                }

                mode_num = mode_map.get(mode, 1)  # Default to LTP

                message = {
                    'action': 'subscribe',
                    'symbol': symbol,
                    'exchange': exchange,
                    'mode': mode_num,
                    'depth': 5  # Default depth level
                }

                logger.info(f"[WS_SUBSCRIBE] Sending subscription for {symbol}")
                self.ws.send(json.dumps(message))
                self.subscriptions.add(json.dumps(subscription))

                # Small delay between subscriptions to avoid overwhelming server
                time.sleep(0.05)  # 50ms delay
                return True
            else:
                logger.warning(f"[WS_SUBSCRIBE] WebSocket not connected, queuing {symbol}")
                self.subscriptions.add(json.dumps(subscription))
                return False

        except Exception as e:
            logger.error(f"[WS_SUBSCRIBE] Error: {e}")
            return False
    
    def unsubscribe(self, subscription):
        """Unsubscribe from symbol"""
        try:
            if self.ws and self.ws.sock and self.ws.sock.connected:
                message = {
                    'action': 'unsubscribe',
                    'exchange': subscription.get('exchange'),
                    'symbol': subscription.get('symbol')
                }
                
                self.ws.send(json.dumps(message))
                
                # Remove from subscriptions set
                sub_str = json.dumps(subscription)
                if sub_str in self.subscriptions:
                    self.subscriptions.remove(sub_str)
                
                logger.debug(f"Unsubscribed from {subscription}")
                return True
                
        except Exception as e:
            logger.error(f"Unsubscription error: {e}")
            return False
    
    def resubscribe_all(self):
        """Resubscribe to all symbols after reconnection"""
        logger.info(f"Resubscribing to {len(self.subscriptions)} symbols")
        
        # Map mode names to numbers
        mode_map = {
            'ltp': 1,      # Mode 1 for LTP
            'quote': 2,    # Mode 2 for Quote
            'depth': 3     # Mode 3 for Market Depth
        }
        
        # Send individual subscription messages (OpenAlgo format)
        for sub_str in self.subscriptions:
            try:
                subscription = json.loads(sub_str)
                symbol = subscription.get('symbol')
                exchange = subscription.get('exchange')
                mode = subscription.get('mode', 'ltp')
                
                if not symbol or not exchange:
                    logger.warning(f"[WS_RESUB] Skipping invalid subscription: {subscription}")
                    continue
                
                mode_num = mode_map.get(mode, 1)  # Default to LTP
                
                message = {
                    'action': 'subscribe',
                    'symbol': symbol,
                    'exchange': exchange,
                    'mode': mode_num,
                    'depth': 5  # Default depth level
                }
                
                self.ws.send(json.dumps(message))
                time.sleep(0.05)  # Small delay between subscriptions
                
            except json.JSONDecodeError:
                logger.error(f"Failed to parse subscription: {sub_str}")
                continue
            except Exception as e:
                logger.error(f"Failed to resubscribe: {e}")
    
    def disconnect(self):
        """Disconnect WebSocket"""
        try:
            self.active = False
            if self.ws:
                self.ws.close()
            logger.info("WebSocket disconnected")
        except Exception as e:
            logger.error(f"Error disconnecting WebSocket: {e}")
    
    def get_status(self):
        """Get WebSocket connection status"""
        if not self.connection_pool:
            return {'status': 'not_initialized'}
        
        return {
            'status': 'active' if self.active else 'inactive',
            'current_account': getattr(self.connection_pool.get('current_account'), 'name', 'Unknown'),
            'backup_accounts': len(self.connection_pool.get('backup_accounts', [])),
            'metrics': self.connection_pool.get('metrics', {}),
            'subscriptions': len(self.subscriptions),
            'connected': self.ws.sock.connected if self.ws and self.ws.sock else False
        }
    
    def register_handler(self, mode, handler):
        """Register data handler for specific mode"""
        if mode == 'quote':
            self.data_processor.register_quote_handler(handler)
        elif mode == 'depth':
            self.data_processor.register_depth_handler(handler)
        elif mode == 'ltp':
            self.data_processor.register_ltp_handler(handler)