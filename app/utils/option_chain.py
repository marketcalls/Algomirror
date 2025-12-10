"""
Option Chain Manager Module
Real-time option chain management for NIFTY and BANKNIFTY with market depth
"""

import json
import threading
import time
from datetime import datetime, timedelta
from collections import deque
from typing import Dict, List, Optional, Any
import logging
from cachetools import TTLCache
import pytz

from openalgo import api

logger = logging.getLogger(__name__)


class OptionChainCache:
    """Zero-config cache for option chain data"""
    
    def __init__(self, maxsize=100, ttl=30):
        self.cache = TTLCache(maxsize=maxsize, ttl=ttl)
        self.lock = threading.Lock()
    
    def get(self, key):
        with self.lock:
            return self.cache.get(key)
    
    def set(self, key, value):
        with self.lock:
            self.cache[key] = value


class OptionChainManager:
    """
    Manager class for option chain with market depth
    Handles both LTP and bid/ask data for order management
    Note: Not a singleton anymore to support multiple underlying/expiry combinations
    """
    
    def __init__(self, underlying, expiry, websocket_manager=None):
        self.underlying = underlying
        self.expiry = expiry
        self.strike_step = 50 if underlying == 'NIFTY' else 100
        self.option_data = {}
        self.subscription_map = {}
        self.underlying_ltp = 0
        self.underlying_bid = 0
        self.underlying_ask = 0
        self.atm_strike = 0
        self.websocket_manager = websocket_manager
        self.cache = OptionChainCache()
        self.monitoring_active = False
        self.initialized = False
        self.manager_id = f"{underlying}_{expiry}"
    
    def initialize(self, api_client):
        """Setup option chain with depth subscriptions"""
        if self.initialized:
            logger.debug(f"Option chain already initialized for {self.underlying}")
            return True
            
        self.api_client = api_client
        self.calculate_atm()
        self.generate_strikes()
        self.setup_depth_subscriptions()
        self.initialized = True
        return True
    
    def calculate_atm(self):
        """Determine ATM strike from underlying LTP"""
        try:
            # If we already have underlying_ltp from WebSocket, use it
            if self.underlying_ltp and self.underlying_ltp > 0:
                # Calculate ATM strike from existing LTP
                self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                logger.debug(f"{self.underlying} LTP: {self.underlying_ltp}, ATM: {self.atm_strike} (from cached)")
                return self.atm_strike
            
            # Otherwise fetch underlying quote from API
            exchange = 'BSE_INDEX' if self.underlying == 'SENSEX' else 'NSE_INDEX'
            response = self.api_client.quotes(symbol=self.underlying, exchange=exchange)
            
            if response.get('status') == 'success':
                data = response.get('data', {})
                self.underlying_ltp = data.get('ltp', 0)
                self.underlying_bid = data.get('bid', self.underlying_ltp)
                self.underlying_ask = data.get('ask', self.underlying_ltp)
                
                # Calculate ATM strike
                if self.underlying_ltp > 0:
                    self.atm_strike = round(self.underlying_ltp / self.strike_step) * self.strike_step
                    logger.debug(f"{self.underlying} LTP: {self.underlying_ltp}, ATM: {self.atm_strike} (from API)")
                    return self.atm_strike
                else:
                    logger.warning(f"Invalid LTP received for {self.underlying}: {self.underlying_ltp}")
                    return 0
            else:
                logger.warning(f"Failed to fetch quote for {self.underlying}: {response.get('message', 'Unknown error')}")
                return 0
        except Exception as e:
            logger.error(f"Error calculating ATM: {e}")
            return 0
    
    def generate_strikes(self):
        """Create strike list with proper tagging"""
        if not self.atm_strike:
            return
        
        strikes = []
        
        # Generate ITM strikes (20 strikes below ATM for CE, above for PE)
        for i in range(20, 0, -1):
            strike = self.atm_strike - (i * self.strike_step)
            strikes.append({
                'strike': strike,
                'tag': f'ITM{i}',
                'position': -i
            })
        
        # Add ATM strike
        strikes.append({
            'strike': self.atm_strike,
            'tag': 'ATM',
            'position': 0
        })
        
        # Generate OTM strikes (20 strikes above ATM for CE, below for PE)
        for i in range(1, 21):
            strike = self.atm_strike + (i * self.strike_step)
            strikes.append({
                'strike': strike,
                'tag': f'OTM{i}',
                'position': i
            })
        
        # Initialize option data structure
        for strike_info in strikes:
            strike = strike_info['strike']
            self.option_data[strike] = {
                'strike': strike,
                'tag': strike_info['tag'],
                'position': strike_info['position'],
                'ce_symbol': self.construct_option_symbol(strike, 'CE'),
                'pe_symbol': self.construct_option_symbol(strike, 'PE'),
                'ce_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0
                },
                'pe_data': {
                    'ltp': 0, 'bid': 0, 'ask': 0, 'bid_qty': 0,
                    'ask_qty': 0, 'spread': 0, 'volume': 0, 'oi': 0
                }
            }
            
            # Map symbols to strikes for quick lookup
            self.subscription_map[self.option_data[strike]['ce_symbol']] = {
                'strike': strike, 'type': 'CE'
            }
            self.subscription_map[self.option_data[strike]['pe_symbol']] = {
                'strike': strike, 'type': 'PE'
            }
        
        logger.debug(f"Generated {len(strikes)} strikes for {self.underlying}")
    
    def construct_option_symbol(self, strike, option_type):
        """Construct OpenAlgo option symbol"""
        # Format: [Base Symbol][Expiration Date][Strike Price][Option Type]
        # Date format: DDMMMYY (e.g., 28AUG25 for August 28, 2025)
        # Example: NIFTY28AUG2524800CE
        
        # Parse expiry date to proper format
        expiry_formatted = None
        
        if isinstance(self.expiry, str):
            # For OpenAlgo, format should be DDMMMYY (e.g., 28AUG25)
            # But we only need DDMMM for the actual symbol
            try:
                # Handle format like "28-AUG-25" -> "28AUG"
                parts = self.expiry.split('-')
                if len(parts) >= 2:
                    day = parts[0].zfill(2)
                    month = parts[1].upper()[:3]
                    # NO YEAR in the expiry part of symbol
                    expiry_formatted = f"{day}{month}"
                else:
                    # Extract day and month
                    expiry_clean = self.expiry.replace('-', '').upper()
                    # Find month in string
                    for mon in ['JAN', 'FEB', 'MAR', 'APR', 'MAY', 'JUN', 'JUL', 'AUG', 'SEP', 'OCT', 'NOV', 'DEC']:
                        if mon in expiry_clean:
                            idx = expiry_clean.index(mon)
                            day = expiry_clean[max(0, idx-2):idx]
                            if not day or not day.isdigit():
                                day = '01'
                            expiry_formatted = f"{day.zfill(2)}{mon}"
                            break
                    else:
                        expiry_formatted = '28AUG'  # Default
            except Exception as e:
                logger.error(f"Error parsing expiry: {e}")
                expiry_formatted = '28AUG'
        elif isinstance(self.expiry, datetime):
            # Format datetime as DDMMM (no year in symbol)
            expiry_formatted = self.expiry.strftime('%d%b').upper()
        else:
            # Default handling
            expiry_formatted = '28AUG'
        
        # Remove decimal if whole number
        if strike == int(strike):
            strike_str = str(int(strike))
        else:
            strike_str = str(strike)
        
        # Construct symbol: BASE + EXPIRY + 25 + STRIKE + CE/PE
        # The "25" is the year 2025, hardcoded for now
        # Format: NIFTY28AUG2524800CE
        symbol = f"{self.underlying}{expiry_formatted}25{strike_str}{option_type}"
        
        logger.debug(f"Generated symbol: {symbol} from expiry={self.expiry}, strike={strike}, type={option_type}")
        
        return symbol
    
    def setup_depth_subscriptions(self):
        """
        Configure WebSocket subscriptions with appropriate modes
        - Quote mode for underlying (NIFTY/BANKNIFTY spot)
        - Depth mode for all option strikes (CE & PE)
        """
        if not self.websocket_manager:
            logger.warning("WebSocket manager not available for subscriptions")
            return
        
        # IMPORTANT: Register handlers BEFORE subscribing
        logger.debug(f"[REGISTER] Registering handlers for {self.underlying} option chain")
        self.websocket_manager.register_handler('depth', self.handle_depth_update)
        self.websocket_manager.register_handler('quote', self.handle_quote_update)
        logger.debug(f"[REGISTER] Handlers registered successfully")
        
        # Ensure WebSocket is authenticated before subscribing
        if not self.websocket_manager.authenticated:
            logger.warning(f"WebSocket not authenticated, skipping subscriptions for {self.underlying}")
            return
        
        # Small delay to ensure WebSocket is ready
        time.sleep(0.5)
        
        # Subscribe to underlying in quote mode
        self.subscribe_underlying_quote()
        
        # Batch subscribe to all options in depth mode for efficiency
        self.batch_subscribe_options()
        
        logger.debug(f"Setup depth subscriptions for {self.underlying} option chain")
    
    def subscribe_underlying_quote(self):
        """Subscribe to underlying index in quote mode"""
        if self.websocket_manager:
            # Determine exchange based on underlying
            exchange = 'BSE_INDEX' if self.underlying == 'SENSEX' else 'NSE_INDEX'
            
            subscription = {
                'exchange': exchange,
                'symbol': self.underlying,
                'mode': 'quote'
            }
            self.websocket_manager.subscribe(subscription)
    
    def subscribe_option_depth(self, symbol):
        """Subscribe to option symbol in depth mode"""
        if self.websocket_manager:
            # Determine exchange based on underlying
            exchange = 'BFO' if self.underlying == 'SENSEX' else 'NFO'
            
            subscription = {
                'symbol': symbol,
                'exchange': exchange,
                'mode': 'depth'
            }
            self.websocket_manager.subscribe(subscription)
    
    def batch_subscribe_options(self):
        """Batch subscribe to all option strikes for both quote and depth"""
        if not self.websocket_manager:
            return
        
        # Determine exchange based on underlying
        exchange = 'BFO' if self.underlying == 'SENSEX' else 'NFO'
        
        # Build instruments list for batch subscription
        instruments = []
        for strike_data in self.option_data.values():
            # Add CE strike
            instruments.append({
                'symbol': strike_data['ce_symbol'],
                'exchange': exchange
            })
            # Add PE strike
            instruments.append({
                'symbol': strike_data['pe_symbol'],
                'exchange': exchange
            })
        
        # Subscribe in batches of 20 to avoid overwhelming the server
        batch_size = 20
        for i in range(0, len(instruments), batch_size):
            batch = instruments[i:i + batch_size]
            logger.debug(f"Subscribing to batch of {len(batch)} option instruments in depth mode")
            
            # Subscribe to depth mode (includes quote data)
            # Depth mode provides: LTP, Volume, Bid/Ask with quantities
            self.websocket_manager.subscribe_batch(batch, mode='depth')
            
            # No delay to prevent blocking Flask startup
    
    def handle_quote_update(self, data):
        """
        Handle quote updates for underlying index (NIFTY/BANKNIFTY)
        """
        symbol = data.get('symbol', '')
        
        # Check if this is our underlying
        if symbol == self.underlying:
            ltp = data.get('ltp', 0)
            if ltp:
                self.underlying_ltp = float(ltp)
                
                # Update ATM strike based on new spot price
                old_atm = self.atm_strike
                self.atm_strike = self.calculate_atm()
                
                if old_atm != self.atm_strike:
                    logger.debug(f"[ATM_UPDATE] ATM strike changed from {old_atm} to {self.atm_strike} (spot: {self.underlying_ltp})")
                    
                    # If strikes haven't been generated yet (option_data is empty), generate them now
                    if not self.option_data:
                        logger.debug(f"[STRIKE_GEN] Generating strikes for {self.underlying} with ATM {self.atm_strike}")
                        self.generate_strikes()
                        # Also setup subscriptions if not done yet
                        if self.websocket_manager and self.websocket_manager.authenticated:
                            self.batch_subscribe_options()
                    else:
                        # Update tags if strikes already exist
                        self.update_option_tags()
                
                logger.debug(f"[QUOTE_UPDATE] {self.underlying} spot updated to {self.underlying_ltp}")
                
                # Also extract bid/ask if available
                self.underlying_bid = float(data.get('bid', 0) or 0)
                self.underlying_ask = float(data.get('ask', 0) or 0)
    
    def handle_depth_update(self, data):
        """
        Process incoming depth data for options
        Extract top-level bid/ask for order management
        """
        # Enhanced debugging
        logger.debug(f"[DEPTH_UPDATE] Called with data type: {type(data)}")
        logger.debug(f"[DEPTH_UPDATE] Data keys: {list(data.keys()) if isinstance(data, dict) else 'Not a dict'}")
        
        # Try multiple symbol fields
        symbol = data.get('symbol') or data.get('Symbol') or data.get('trading_symbol') or data.get('tradingSymbol') or ''
        
        logger.debug(f"[DEPTH_UPDATE] Extracted symbol: '{symbol}'")
        logger.debug(f"[DEPTH_UPDATE] Subscription map has {len(self.subscription_map)} symbols")
        if len(self.subscription_map) > 0:
            logger.debug(f"[DEPTH_UPDATE] Sample symbols in map: {list(self.subscription_map.keys())[:3]}")
        
        # Log raw data structure for debugging
        logger.debug(f"[OPTION_CHAIN_RAW] LTP={data.get('ltp')}, bids count={len(data.get('bids', []))}, asks count={len(data.get('asks', []))}")
        
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            option_type = strike_info['type']  # 'CE' or 'PE'
            strike = strike_info['strike']
            
            logger.debug(f"[OPTION_CHAIN] Updating {self.underlying} {strike} {option_type} with depth data")
            
            # Update with depth data - handle various formats
            # Check for nested depth structure first
            depth_data_raw = data.get('depth', {})
            if depth_data_raw:
                bids = depth_data_raw.get('buy', depth_data_raw.get('bids', []))
                asks = depth_data_raw.get('sell', depth_data_raw.get('asks', []))
            else:
                bids = data.get('bids', [])
                asks = data.get('asks', [])
            
            logger.debug(f"[DEPTH_EXTRACT] depth field: {depth_data_raw}, bids: {bids[:1] if bids else 'empty'}, asks: {asks[:1] if asks else 'empty'}")
            
            # Extract LTP - try multiple possible fields
            ltp = data.get('ltp') or data.get('last_price') or data.get('lastPrice') or 0
            
            # Handle bid/ask format variations
            best_bid = 0
            best_ask = 0
            bid_qty = 0
            ask_qty = 0
            
            if bids and len(bids) > 0:
                if isinstance(bids[0], dict):
                    best_bid = bids[0].get('price', bids[0].get('Price', 0))
                    bid_qty = bids[0].get('quantity', bids[0].get('Quantity', bids[0].get('qty', 0)))
                elif isinstance(bids[0], (list, tuple)) and len(bids[0]) >= 2:
                    best_bid = bids[0][0]  # Price at index 0
                    bid_qty = bids[0][1]   # Quantity at index 1
            
            if asks and len(asks) > 0:
                if isinstance(asks[0], dict):
                    best_ask = asks[0].get('price', asks[0].get('Price', 0))
                    ask_qty = asks[0].get('quantity', asks[0].get('Quantity', asks[0].get('qty', 0)))
                elif isinstance(asks[0], (list, tuple)) and len(asks[0]) >= 2:
                    best_ask = asks[0][0]  # Price at index 0
                    ask_qty = asks[0][1]   # Quantity at index 1
            
            # If no bid/ask data but we have LTP, use LTP as approximation
            if not best_bid and not best_ask and ltp:
                # Use a small spread around LTP as fallback
                best_bid = float(ltp) * 0.995  # 0.5% below LTP
                best_ask = float(ltp) * 1.005  # 0.5% above LTP
                bid_qty = 100  # Default quantity
                ask_qty = 100
                logger.debug(f"[FALLBACK] No bid/ask, using LTP-based approximation: bid={best_bid:.2f}, ask={best_ask:.2f}")
            
            depth_data = {
                'ltp': float(ltp) if ltp else 0,
                'bid': float(best_bid) if best_bid else 0,
                'ask': float(best_ask) if best_ask else 0,
                'bid_qty': int(bid_qty) if bid_qty else 0,
                'ask_qty': int(ask_qty) if ask_qty else 0,
                'spread': 0,
                'volume': int(data.get('volume', data.get('Volume', 0)) or 0),
                'oi': int(data.get('oi', data.get('openInterest', data.get('OI', data.get('open_interest', 0)))) or 0)
            }
            
            # Calculate spread
            if depth_data['bid'] > 0 and depth_data['ask'] > 0:
                depth_data['spread'] = depth_data['ask'] - depth_data['bid']
            
            # Update option chain data
            self.update_option_depth(strike, option_type, depth_data)
    
    def update_option_depth(self, strike, option_type, depth_data):
        """Update option chain with depth data"""
        if strike in self.option_data:
            if option_type == 'CE':
                self.option_data[strike]['ce_data'] = depth_data
            else:
                self.option_data[strike]['pe_data'] = depth_data
            
            # Update cache
            cache_key = f"{self.underlying}_{strike}_{option_type}"
            self.cache.set(cache_key, depth_data)
    
    def get_option_chain(self):
        """Return formatted option chain data"""
        return {
            'underlying': self.underlying,
            'underlying_ltp': self.underlying_ltp,
            'underlying_bid': self.underlying_bid,
            'underlying_ask': self.underlying_ask,
            'atm_strike': self.atm_strike,
            'expiry': self.expiry,
            'timestamp': datetime.now(pytz.timezone('Asia/Kolkata')).isoformat(),
            'options': list(self.option_data.values()),
            'market_metrics': self.calculate_market_metrics()
        }
    
    def update_option_tags(self):
        """Update option tags when ATM changes"""
        for strike_data in self.option_data.values():
            strike = strike_data['strike']
            position = self.get_strike_position(strike)
            strike_data['position'] = position
            strike_data['tag'] = self.get_position_tag(position)
            
            # Update PE tag (reversed)
            if position == 0:
                strike_data['pe_tag'] = 'ATM'
            elif position > 0:
                strike_data['pe_tag'] = f'OTM{abs(position)}'
            else:
                strike_data['pe_tag'] = f'ITM{abs(position)}'
    
    def calculate_market_metrics(self):
        """Calculate PCR and other metrics"""
        total_ce_volume = sum(opt['ce_data'].get('volume', 0) for opt in self.option_data.values())
        total_pe_volume = sum(opt['pe_data'].get('volume', 0) for opt in self.option_data.values())
        total_ce_oi = sum(opt['ce_data'].get('oi', 0) for opt in self.option_data.values())
        total_pe_oi = sum(opt['pe_data'].get('oi', 0) for opt in self.option_data.values())
        
        # Calculate PCR based on OI
        pcr = total_pe_oi / total_ce_oi if total_ce_oi > 0 else 0
        
        # Calculate volume-based PCR as well
        pcr_volume = total_pe_volume / total_ce_volume if total_ce_volume > 0 else 0
        
        return {
            'total_ce_volume': total_ce_volume,
            'total_pe_volume': total_pe_volume,
            'total_volume': total_ce_volume + total_pe_volume,
            'total_ce_oi': total_ce_oi,
            'total_pe_oi': total_pe_oi,
            'pcr': round(pcr, 2),
            'pcr_volume': round(pcr_volume, 2),
            'max_pain': self.calculate_max_pain()
        }
    
    def calculate_max_pain(self):
        """Calculate max pain strike"""
        # Simplified max pain calculation
        # In production, use proper max pain algorithm
        return self.atm_strike

    def get_strike_position(self, strike):
        """Get strike position relative to ATM"""
        if not self.atm_strike:
            return 0
        return (strike - self.atm_strike) // self.strike_step

    def get_position_tag(self, position):
        """Get position tag based on strike position"""
        if position == 0:
            return 'ATM'
        elif position > 0:
            return f'OTM{abs(position)}'
        else:
            return f'ITM{abs(position)}'
    
    def get_execution_price(self, symbol, action, quantity=None):
        """
        Calculate expected execution price based on market depth
        Used for order management and slippage calculation
        """
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            strike = strike_info['strike']
            option_type = strike_info['type']
            
            if option_type == 'CE':
                depth_data = self.option_data[strike]['ce_data']
            else:
                depth_data = self.option_data[strike]['pe_data']
            
            if action == 'BUY':
                return depth_data['ask']
            else:  # SELL
                return depth_data['bid']
        return 0
    
    def get_option_spread(self, symbol):
        """Get bid-ask spread for a symbol"""
        if symbol in self.subscription_map:
            strike_info = self.subscription_map[symbol]
            strike = strike_info['strike']
            option_type = strike_info['type']
            
            if option_type == 'CE':
                return self.option_data[strike]['ce_data']['spread']
            else:
                return self.option_data[strike]['pe_data']['spread']
        return 0
    
    def get_option_by_tag(self, tag):
        """Get option data by tag (ATM, ITM1, OTM1, etc.)"""
        for strike_data in self.option_data.values():
            if strike_data['tag'] == tag:
                return strike_data
        return None
    
    def start_monitoring(self):
        """Start background monitoring"""
        self.monitoring_active = True
        logger.debug(f"Started monitoring option chain for {self.underlying}")
    
    def stop_monitoring(self):
        """Stop background monitoring"""
        self.monitoring_active = False
        logger.debug(f"Stopped monitoring option chain for {self.underlying}")
    
    def is_active(self):
        """Check if option chain monitoring is active"""
        return self.monitoring_active