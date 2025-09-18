"""
Strategy Executor Service
Handles multi-account strategy execution with OpenAlgo integration
"""

import logging
from datetime import datetime, time
from typing import Dict, List, Any, Optional
import threading
import json
from app import db
from app.models import Strategy, StrategyLeg, StrategyExecution, TradingAccount
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.background_service import option_chain_service

logger = logging.getLogger(__name__)


class StrategyExecutor:
    """Execute trading strategies across multiple accounts"""

    def __init__(self, strategy: Strategy):
        self.strategy = strategy
        self.accounts = self._get_active_accounts()
        self.execution_results = []
        self.lock = threading.Lock()
        self.websocket_manager = None
        self.price_subscriptions = {}  # Map symbol to WebSocket subscription
        self.latest_prices = {}  # Cache latest prices from WebSocket
        self.expiry_cache = {}  # Cache expiry dates to reduce API calls

    def _get_active_accounts(self) -> List[TradingAccount]:
        """Get active trading accounts for strategy"""
        account_ids = self.strategy.selected_accounts or []
        return TradingAccount.query.filter(
            TradingAccount.id.in_(account_ids),
            TradingAccount.is_active == True
        ).all()

    def execute(self) -> List[Dict[str, Any]]:
        """Execute strategy across all selected accounts"""
        if not self.accounts:
            raise ValueError("No active accounts selected for strategy")

        results = []
        for leg in self.strategy.legs:
            leg_results = self._execute_leg(leg)
            results.extend(leg_results)

        return results

    def _execute_leg(self, leg: StrategyLeg) -> List[Dict[str, Any]]:
        """Execute a strategy leg across all accounts"""
        results = []

        # Build symbol based on leg configuration
        symbol = self._build_symbol(leg)
        exchange = self._get_exchange(leg)

        # Calculate quantity per account
        quantity_per_account = self._calculate_quantity(leg, len(self.accounts))

        # Execute on each account
        threads = []
        for account in self.accounts:
            thread = threading.Thread(
                target=self._execute_on_account,
                args=(account, leg, symbol, exchange, quantity_per_account, results)
            )
            thread.start()
            threads.append(thread)

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        return results

    def _execute_on_account(self, account: TradingAccount, leg: StrategyLeg,
                           symbol: str, exchange: str, quantity: int, results: List):
        """Execute order on a specific account"""
        try:
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            # Prepare order parameters
            order_params = {
                'strategy': f"Strategy_{self.strategy.id}",
                'symbol': symbol,
                'action': leg.action,
                'exchange': exchange,
                'price_type': leg.order_type,
                'product': 'MIS',  # Default to MIS for intraday
                'quantity': quantity
            }

            # Add price for limit orders
            if leg.order_type == 'LIMIT' and leg.strike_price:
                order_params['price'] = leg.strike_price

            # Place order
            response = client.placeorder(**order_params)

            if response.get('status') == 'success':
                # Create execution record
                execution = StrategyExecution(
                    strategy_id=self.strategy.id,
                    account_id=account.id,
                    leg_id=leg.id,
                    order_id=response.get('orderid'),
                    symbol=symbol,
                    exchange=exchange,
                    quantity=quantity,
                    status='entered',
                    entry_time=datetime.utcnow()
                )

                with self.lock:
                    db.session.add(execution)
                    db.session.commit()

                    results.append({
                        'account': account.account_name,
                        'symbol': symbol,
                        'order_id': response.get('orderid'),
                        'status': 'success'
                    })

                # Start monitoring for exits
                self._start_exit_monitoring(execution)

            else:
                with self.lock:
                    results.append({
                        'account': account.account_name,
                        'symbol': symbol,
                        'status': 'failed',
                        'error': response.get('message', 'Order placement failed')
                    })

        except Exception as e:
            logger.error(f"Error executing on account {account.account_name}: {e}")
            with self.lock:
                results.append({
                    'account': account.account_name,
                    'symbol': symbol,
                    'status': 'error',
                    'error': str(e)
                })

    def _build_symbol(self, leg: StrategyLeg) -> str:
        """Build OpenAlgo symbol format based on leg configuration"""
        base_symbol = leg.instrument

        if leg.product_type == 'options':
            # Get expiry date
            expiry = self._get_expiry_string(leg)

            # Get strike price
            strike = self._get_strike_price(leg)

            # Build option symbol: NIFTY28MAR2420800CE
            symbol = f"{base_symbol}{expiry}{strike}{leg.option_type}"

        elif leg.product_type == 'futures':
            # Get expiry date
            expiry = self._get_expiry_string(leg)

            # Build futures symbol: NIFTY28MAR24FUT
            symbol = f"{base_symbol}{expiry}FUT"

        else:
            # Equity symbol
            symbol = base_symbol

        return symbol

    def _get_exchange(self, leg: StrategyLeg) -> str:
        """Get exchange based on instrument"""
        if leg.instrument == 'SENSEX':
            return 'BFO' if leg.product_type in ['options', 'futures'] else 'BSE'
        elif leg.instrument in ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY']:
            return 'NFO' if leg.product_type in ['options', 'futures'] else 'NSE'
        else:
            return 'NSE'  # Default to NSE

    def _get_expiry_string(self, leg: StrategyLeg) -> str:
        """Get actual expiry date from OpenAlgo API"""
        try:
            # Check cache first
            cache_key = f"{leg.instrument}_{leg.product_type}_{leg.expiry}"
            if cache_key in self.expiry_cache:
                cached_data = self.expiry_cache[cache_key]
                # Check if cache is still valid (less than 1 hour old)
                if (datetime.utcnow() - cached_data['timestamp']).seconds < 3600:
                    return cached_data['expiry']

            # Determine exchange
            exchange = 'BFO' if leg.instrument == 'SENSEX' else 'NFO'

            # Get expiry dates from API
            if self.accounts:
                client = ExtendedOpenAlgoAPI(
                    api_key=self.accounts[0].get_api_key(),
                    host=self.accounts[0].host_url
                )

                # Fetch all expiries for the instrument
                expiry_response = client.expiry(
                    symbol=leg.instrument,
                    exchange=exchange,
                    instrumenttype='options' if leg.product_type == 'options' else 'futures'
                )

                if expiry_response.get('status') == 'success':
                    expiries = expiry_response.get('data', [])

                    if not expiries:
                        logger.error(f"No expiries available for {leg.instrument}")
                        return ""

                    # Sort expiries to ensure they're in chronological order
                    # Convert expiry strings to dates for sorting
                    from datetime import datetime

                    def parse_expiry(exp_str):
                        """Parse expiry string like '10-JUL-25' to date"""
                        try:
                            return datetime.strptime(exp_str, '%d-%b-%y')
                        except:
                            try:
                                return datetime.strptime(exp_str, '%d%b%y')
                            except:
                                return datetime.max

                    sorted_expiries = sorted(expiries, key=parse_expiry)

                    # Select appropriate expiry based on leg configuration
                    selected_expiry = None

                    if leg.expiry == 'current_week':
                        # First expiry in the list (nearest)
                        selected_expiry = sorted_expiries[0] if sorted_expiries else None

                    elif leg.expiry == 'next_week':
                        # Second expiry if available, else first
                        if len(sorted_expiries) > 1:
                            selected_expiry = sorted_expiries[1]
                        else:
                            selected_expiry = sorted_expiries[0] if sorted_expiries else None

                    elif leg.expiry == 'current_month':
                        # Find the monthly expiry (usually last Thursday of month)
                        # For indices, monthly expiry is typically the last expiry of the month
                        current_month = datetime.now().month
                        current_year = datetime.now().year

                        for exp_str in sorted_expiries:
                            exp_date = parse_expiry(exp_str)
                            if exp_date.month == current_month and exp_date.year == current_year:
                                # Keep updating to get the last expiry of current month
                                selected_expiry = exp_str

                        # If no current month expiry found, use the first available
                        if not selected_expiry and sorted_expiries:
                            selected_expiry = sorted_expiries[0]

                    elif leg.expiry == 'next_month':
                        # Find next month's expiry
                        next_month = (datetime.now().month % 12) + 1
                        next_year = datetime.now().year if next_month > datetime.now().month else datetime.now().year + 1

                        for exp_str in sorted_expiries:
                            exp_date = parse_expiry(exp_str)
                            if exp_date.month == next_month and exp_date.year == next_year:
                                selected_expiry = exp_str
                                break

                        # If no next month expiry found, use the first expiry after current month
                        if not selected_expiry:
                            current_date = datetime.now()
                            for exp_str in sorted_expiries:
                                exp_date = parse_expiry(exp_str)
                                if exp_date > current_date:
                                    selected_expiry = exp_str
                                    break

                    if selected_expiry:
                        # Convert to OpenAlgo format (e.g., '10-JUL-25' to '10JUL25')
                        formatted_expiry = selected_expiry.replace('-', '')

                        # Cache the result
                        self.expiry_cache[cache_key] = {
                            'expiry': formatted_expiry,
                            'timestamp': datetime.utcnow()
                        }

                        logger.info(f"{leg.instrument} {leg.expiry} mapped to expiry: {formatted_expiry}")
                        return formatted_expiry
                    else:
                        logger.error(f"Could not determine expiry for {leg.instrument} {leg.expiry}")
                        return ""

                else:
                    logger.error(f"Failed to fetch expiries: {expiry_response.get('message')}")
                    return ""

        except Exception as e:
            logger.error(f"Error getting expiry string: {e}")
            return ""

        # Fallback - should not reach here
        return ""

    def _get_strike_price(self, leg: StrategyLeg) -> str:
        """Get strike price based on selection method with support for ITM/OTM 1-20"""
        if leg.strike_selection == 'strike_price':
            return str(int(leg.strike_price))

        # Get current spot price for ATM/ITM/OTM calculation
        try:
            # Determine exchange for underlying
            if leg.instrument == 'SENSEX':
                exchange = 'BSE_INDEX'
            else:
                exchange = 'NSE_INDEX'

            # Get spot price from API or WebSocket
            spot_price = self._get_spot_price(leg.instrument, exchange)

            if not spot_price:
                logger.error(f"Could not get spot price for {leg.instrument}")
                return "0"

            # Determine strike step based on instrument
            strike_steps = {
                'NIFTY': 50,
                'BANKNIFTY': 100,
                'FINNIFTY': 50,
                'MIDCPNIFTY': 50,
                'SENSEX': 100
            }
            strike_step = strike_steps.get(leg.instrument, 50)

            # Calculate ATM strike (round to nearest strike)
            atm_strike = round(spot_price / strike_step) * strike_step

            # Handle different selection methods
            if leg.strike_selection == 'ATM':
                return str(atm_strike)

            elif leg.strike_selection in ['ITM', 'OTM']:
                # Get offset (1-20) from strike_offset field
                offset = leg.strike_offset if leg.strike_offset else 0

                # Validate offset range (1-20)
                offset = max(0, min(20, offset))

                if leg.option_type == 'CE':
                    # For Call options
                    if leg.strike_selection == 'ITM':
                        # ITM calls are below spot
                        strike = atm_strike - (offset * strike_step)
                    else:  # OTM
                        # OTM calls are above spot
                        strike = atm_strike + (offset * strike_step)
                else:  # PE
                    # For Put options
                    if leg.strike_selection == 'ITM':
                        # ITM puts are above spot
                        strike = atm_strike + (offset * strike_step)
                    else:  # OTM
                        # OTM puts are below spot
                        strike = atm_strike - (offset * strike_step)

                logger.info(f"{leg.instrument} {leg.option_type}: Spot={spot_price}, ATM={atm_strike}, "
                          f"{leg.strike_selection}{offset}={strike}")
                return str(strike)

            elif leg.strike_selection == 'premium_near':
                # Find strike with premium closest to target
                return self._find_strike_by_premium(leg, atm_strike, strike_step)

        except Exception as e:
            logger.error(f"Error calculating strike price: {e}")
            return "0"

    def _get_spot_price(self, instrument: str, exchange: str) -> float:
        """Get current spot price from WebSocket or API"""
        try:
            # First check if we have WebSocket data
            if instrument in self.latest_prices:
                price_info = self.latest_prices[instrument]
                if price_info['timestamp'] and (datetime.utcnow() - price_info['timestamp']).seconds < 5:
                    return price_info['ltp']

            # Check option chain service for cached underlying price
            underlying_key = f"{instrument}_spot"
            if underlying_key in option_chain_service.active_managers:
                manager = option_chain_service.active_managers[underlying_key]
                if manager and manager.underlying_ltp > 0:
                    return manager.underlying_ltp

            # Fallback to API call
            if self.accounts:
                client = ExtendedOpenAlgoAPI(
                    api_key=self.accounts[0].get_api_key(),
                    host=self.accounts[0].host_url
                )

                response = client.quotes(symbol=instrument, exchange=exchange)
                if response.get('status') == 'success':
                    return response.get('data', {}).get('ltp', 0)

        except Exception as e:
            logger.error(f"Error getting spot price for {instrument}: {e}")

        return 0

    def _find_strike_by_premium(self, leg: StrategyLeg, atm_strike: int, strike_step: int) -> str:
        """Find strike with premium closest to target value"""
        try:
            target_premium = leg.premium_value if leg.premium_value else 50

            # Check strikes around ATM to find closest premium
            best_strike = atm_strike
            best_diff = float('inf')

            # Check +/- 10 strikes from ATM
            for i in range(-10, 11):
                strike = atm_strike + (i * strike_step)

                # Build option symbol for this strike
                expiry = self._get_expiry_string(leg)
                symbol = f"{leg.instrument}{expiry}{strike}{leg.option_type}"

                # Get premium for this strike
                if self.accounts:
                    client = ExtendedOpenAlgoAPI(
                        api_key=self.accounts[0].get_api_key(),
                        host=self.accounts[0].host_url
                    )

                    exchange = 'BFO' if leg.instrument == 'SENSEX' else 'NFO'
                    response = client.quotes(symbol=symbol, exchange=exchange)

                    if response.get('status') == 'success':
                        premium = response.get('data', {}).get('ltp', 0)
                        diff = abs(premium - target_premium)

                        if diff < best_diff:
                            best_diff = diff
                            best_strike = strike

                            # If we found exact match or very close, stop searching
                            if diff < 5:
                                break

            logger.info(f"Found strike {best_strike} with premium closest to {target_premium}")
            return str(best_strike)

        except Exception as e:
            logger.error(f"Error finding strike by premium: {e}")
            return str(atm_strike)

    def _calculate_quantity(self, leg: StrategyLeg, num_accounts: int) -> int:
        """Calculate quantity per account based on allocation type"""
        total_quantity = leg.quantity or (leg.lots * self._get_lot_size(leg))

        if self.strategy.allocation_type == 'equal':
            return total_quantity // num_accounts
        elif self.strategy.allocation_type == 'proportional':
            # Would implement proportional allocation based on account value
            return total_quantity // num_accounts
        else:
            # Custom allocation
            return total_quantity // num_accounts

    def _get_lot_size(self, leg: StrategyLeg) -> int:
        """Get lot size for instrument"""
        lot_sizes = {
            'NIFTY': 75,
            'BANKNIFTY': 25,
            'FINNIFTY': 65,
            'MIDCPNIFTY': 75,
            'SENSEX': 10
        }
        return lot_sizes.get(leg.instrument, 1)

    def _start_exit_monitoring(self, execution: StrategyExecution):
        """Start monitoring position for exit conditions using WebSocket data"""
        # Subscribe to WebSocket for real-time price updates
        self._subscribe_to_websocket(execution.symbol, execution.exchange)

        # Start monitoring thread
        thread = threading.Thread(
            target=self._monitor_exit_conditions,
            args=(execution,),
            daemon=True
        )
        thread.start()

    def _subscribe_to_websocket(self, symbol: str, exchange: str):
        """Subscribe to WebSocket for real-time price updates"""
        try:
            # Check if we have an active WebSocket manager from background service
            if not self.websocket_manager:
                # Try to get existing WebSocket manager from option chain service
                underlying = self._get_underlying_from_symbol(symbol)

                if underlying and underlying in option_chain_service.websocket_managers:
                    self.websocket_manager = option_chain_service.websocket_managers[underlying]
                    logger.info(f"Using existing WebSocket manager for {underlying}")
                else:
                    # Create new WebSocket manager if needed
                    self.websocket_manager = ProfessionalWebSocketManager()
                    if self.accounts:
                        self.websocket_manager.create_connection_pool(
                            primary_account=self.accounts[0],
                            backup_accounts=self.accounts[1:] if len(self.accounts) > 1 else []
                        )

                        # Connect WebSocket
                        if hasattr(self.accounts[0], 'websocket_url'):
                            self.websocket_manager.connect(
                                ws_url=self.accounts[0].websocket_url,
                                api_key=self.accounts[0].get_api_key()
                            )

            # Register price handler for this symbol
            if self.websocket_manager and self.websocket_manager.authenticated:
                # Subscribe to depth data for better fill information
                instruments = [{'exchange': exchange, 'symbol': symbol}]

                # Register handler to update latest prices
                def on_depth_update(data):
                    if data.get('symbol') == symbol:
                        self.latest_prices[symbol] = {
                            'ltp': data.get('ltp'),
                            'bid': data.get('bid'),
                            'ask': data.get('ask'),
                            'timestamp': datetime.utcnow()
                        }
                        logger.debug(f"Price update for {symbol}: {data.get('ltp')}")

                self.websocket_manager.data_processor.register_depth_handler(on_depth_update)
                self.websocket_manager.subscribe_depth(instruments)

                logger.info(f"Subscribed to WebSocket for {symbol}")
                self.price_subscriptions[symbol] = True
            else:
                logger.warning(f"WebSocket not available for {symbol}, falling back to REST API")

        except Exception as e:
            logger.error(f"Error subscribing to WebSocket for {symbol}: {e}")

    def _get_underlying_from_symbol(self, symbol: str) -> Optional[str]:
        """Extract underlying from option/future symbol"""
        for underlying in ['NIFTY', 'BANKNIFTY', 'FINNIFTY', 'MIDCPNIFTY', 'SENSEX']:
            if symbol.startswith(underlying):
                return underlying
        return None

    def _monitor_exit_conditions(self, execution: StrategyExecution):
        """Monitor position for exit conditions using real-time WebSocket data"""
        import time as time_module

        leg = execution.leg
        account = execution.account
        symbol = execution.symbol

        try:
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            # Track entry price if not set
            if not execution.entry_price:
                # Get order details to fetch entry price
                order_response = client.orderstatus(
                    order_id=execution.order_id,
                    strategy=f"Strategy_{self.strategy.id}"
                )
                if order_response.get('status') == 'success':
                    execution.entry_price = order_response.get('data', {}).get('price')
                    db.session.commit()

            while execution.status == 'entered':
                # First check position status
                position_response = client.openposition(
                    strategy=f"Strategy_{self.strategy.id}",
                    symbol=execution.symbol,
                    exchange=execution.exchange,
                    product='MIS'
                )

                if position_response.get('status') == 'success':
                    current_qty = int(position_response.get('quantity', 0))

                    if current_qty == 0:
                        # Position already exited
                        execution.status = 'exited'
                        execution.exit_time = datetime.utcnow()
                        db.session.commit()
                        break

                # Get current price - prefer WebSocket data, fallback to REST
                ltp = None
                price_data = None

                # Check if we have recent WebSocket data (within last 2 seconds)
                if symbol in self.latest_prices:
                    price_info = self.latest_prices[symbol]
                    if price_info['timestamp'] and (datetime.utcnow() - price_info['timestamp']).seconds < 2:
                        # Use WebSocket data
                        ltp = price_info['ltp']
                        price_data = price_info
                        logger.debug(f"Using WebSocket price for {symbol}: {ltp}")

                # Fallback to REST API if no recent WebSocket data
                if not ltp:
                    quote_response = client.quotes(
                        symbol=execution.symbol,
                        exchange=execution.exchange
                    )

                    if quote_response.get('status') == 'success':
                        data = quote_response.get('data', {})
                        ltp = data.get('ltp')
                        price_data = {
                            'ltp': ltp,
                            'bid': data.get('bid'),
                            'ask': data.get('ask')
                        }
                        logger.debug(f"Using REST API price for {symbol}: {ltp}")

                if ltp and execution.entry_price:
                    # Calculate P&L based on action
                    if execution.leg.action == 'BUY':
                        pnl = (ltp - execution.entry_price) * execution.quantity
                    else:  # SELL
                        pnl = (execution.entry_price - ltp) * execution.quantity

                    execution.unrealized_pnl = pnl
                    db.session.commit()

                    # Log P&L periodically for monitoring
                    if int(time_module.time()) % 30 == 0:  # Every 30 seconds
                        logger.info(f"Position {symbol}: Entry={execution.entry_price}, LTP={ltp}, P&L={pnl:.2f}")

                    # Check exit conditions with live price
                    if self._should_exit(execution, ltp, pnl):
                        self._exit_position(execution, client)
                        break

                    # Check for trailing stop loss
                    if leg.enable_trailing and leg.trailing_value:
                        self._update_trailing_stop(execution, ltp, pnl)

                # Check square off time
                if self.strategy.square_off_time:
                    current_time = datetime.now().time()
                    if current_time >= self.strategy.square_off_time:
                        logger.info(f"Square off time reached for {symbol}")
                        self._exit_position(execution, client, reason='square_off')
                        break

                # Sleep briefly - WebSocket provides continuous updates
                time_module.sleep(1)  # Reduced from 5 to 1 second since we have live data

        except Exception as e:
            logger.error(f"Error monitoring exit conditions for {symbol}: {e}")
            execution.status = 'error'
            execution.error_message = str(e)
            db.session.commit()

    def _update_trailing_stop(self, execution: StrategyExecution, ltp: float, current_pnl: float):
        """Update trailing stop loss based on favorable price movement"""
        leg = execution.leg

        # Only trail if position is profitable
        if current_pnl > 0:
            if leg.trailing_type == 'percentage':
                # Calculate new stop based on current price
                if leg.action == 'BUY':
                    new_stop = ltp * (1 - leg.trailing_value / 100)
                else:  # SELL
                    new_stop = ltp * (1 + leg.trailing_value / 100)

                # Update stop loss if more favorable
                if not hasattr(execution, 'trailing_stop') or \
                   (leg.action == 'BUY' and new_stop > execution.trailing_stop) or \
                   (leg.action == 'SELL' and new_stop < execution.trailing_stop):
                    execution.trailing_stop = new_stop
                    logger.debug(f"Updated trailing stop for {execution.symbol}: {new_stop}")

    def _should_exit(self, execution: StrategyExecution, ltp: float, pnl: float) -> bool:
        """Check if position should be exited"""
        leg = execution.leg

        # Check stop loss
        if leg.stop_loss_value:
            if leg.stop_loss_type == 'points':
                if abs(pnl) >= leg.stop_loss_value:
                    return True
            elif leg.stop_loss_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                if abs(pnl) >= (entry_value * leg.stop_loss_value / 100):
                    return True

        # Check take profit
        if leg.take_profit_value:
            if leg.take_profit_type == 'points':
                if pnl >= leg.take_profit_value:
                    return True
            elif leg.take_profit_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                if pnl >= (entry_value * leg.take_profit_value / 100):
                    return True

        # Check max loss/profit at strategy level
        if self.strategy.max_loss:
            total_pnl = self._get_strategy_pnl()
            if abs(total_pnl) >= self.strategy.max_loss:
                return True

        if self.strategy.max_profit:
            total_pnl = self._get_strategy_pnl()
            if total_pnl >= self.strategy.max_profit:
                return True

        return False

    def _exit_position(self, execution: StrategyExecution, client: ExtendedOpenAlgoAPI,
                      reason: str = 'exit_condition'):
        """Exit a position"""
        try:
            # Reverse the action
            exit_action = 'SELL' if execution.leg.action == 'BUY' else 'BUY'

            response = client.placeorder(
                strategy=f"Strategy_{self.strategy.id}",
                symbol=execution.symbol,
                action=exit_action,
                exchange=execution.exchange,
                price_type='MARKET',
                product='MIS',
                quantity=execution.quantity
            )

            if response.get('status') == 'success':
                execution.status = 'exited'
                execution.exit_time = datetime.utcnow()
                execution.exit_reason = reason
                execution.realized_pnl = execution.unrealized_pnl
                db.session.commit()

                logger.info(f"Exited position for {execution.symbol}: {reason}")

        except Exception as e:
            logger.error(f"Error exiting position: {e}")

    def _get_strategy_pnl(self) -> float:
        """Get total P&L for the strategy"""
        executions = StrategyExecution.query.filter_by(
            strategy_id=self.strategy.id
        ).all()

        total_pnl = 0
        for execution in executions:
            if execution.realized_pnl:
                total_pnl += execution.realized_pnl
            elif execution.unrealized_pnl:
                total_pnl += execution.unrealized_pnl

        return total_pnl

    def exit_all_positions(self, executions: List[StrategyExecution]) -> List[Dict]:
        """Exit all active positions"""
        results = []

        for execution in executions:
            try:
                account = execution.account
                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                self._exit_position(execution, client, reason='manual_exit')

                results.append({
                    'symbol': execution.symbol,
                    'account': account.account_name,
                    'status': 'exited'
                })

            except Exception as e:
                logger.error(f"Error exiting position {execution.id}: {e}")
                results.append({
                    'symbol': execution.symbol,
                    'account': execution.account.account_name if execution.account else 'Unknown',
                    'status': 'error',
                    'error': str(e)
                })

        return results