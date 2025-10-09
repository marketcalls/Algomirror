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

    def __init__(self, strategy: Strategy, use_margin_calculator: bool = True, trade_quality: str = 'B'):
        self.strategy = strategy
        self.accounts = self._get_active_accounts()
        self.execution_results = []
        self.lock = threading.Lock()
        self.websocket_manager = None
        self.price_subscriptions = {}  # Map symbol to WebSocket subscription
        self.latest_prices = {}  # Cache latest prices from WebSocket
        self.expiry_cache = {}  # Cache expiry dates to reduce API calls
        self.use_margin_calculator = use_margin_calculator
        self.trade_quality = trade_quality
        self.margin_calculator = None
        self.account_margins = {}  # Track available margin per account

        # Store app reference for thread context
        from flask import current_app
        self.app = current_app._get_current_object()

        if use_margin_calculator:
            from app.utils.margin_calculator import MarginCalculator
            self.margin_calculator = MarginCalculator(strategy.user_id)

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

        # Ensure legs are loaded and filter only non-executed legs
        all_legs = self.strategy.legs.order_by(StrategyLeg.leg_number).all()
        legs = [leg for leg in all_legs if not leg.is_executed]

        print(f"\n[EXECUTE START] Strategy {self.strategy.id} - {self.strategy.name}")
        print(f"[EXECUTE] Total legs: {len(all_legs)}, Unexecuted legs: {len(legs)}")
        for leg in legs:
            print(f"  Leg {leg.leg_number}: {leg.instrument} {leg.action} {leg.option_type} {leg.strike_selection} offset={leg.strike_offset}")

        if not legs:
            raise ValueError("No unexecuted legs found for this strategy")

        logger.info(f"Executing strategy {self.strategy.id}: {len(legs)} unexecuted legs across {len(self.accounts)} accounts")

        results = []
        for i, leg in enumerate(legs, 1):
            logger.info(f"Executing leg {i}/{len(legs)}: {leg.instrument} {leg.action} {leg.option_type if leg.product_type == 'options' else ''}")

            try:
                leg_results = self._execute_leg(leg)
                results.extend(leg_results)
                logger.info(f"Leg {i} execution results: {len(leg_results)} orders placed")
            except Exception as e:
                logger.error(f"Error executing leg {i}: {e}")
                results.append({
                    'leg': i,
                    'status': 'error',
                    'error': str(e)
                })

        return results

    def _execute_leg(self, leg: StrategyLeg) -> List[Dict[str, Any]]:
        """Execute a strategy leg across all accounts"""
        print(f"[LEG DEBUG] Executing leg {leg.leg_number}: {leg.instrument} {leg.product_type} "
              f"{leg.option_type} strike_selection={leg.strike_selection} offset={leg.strike_offset}")
        results = []

        # Build symbol based on leg configuration
        symbol = self._build_symbol(leg)

        if not symbol:
            logger.error(f"Failed to build symbol for leg {leg.leg_number}")
            return [{
                'leg': leg.leg_number,
                'status': 'error',
                'error': 'Failed to build symbol'
            }]

        exchange = self._get_exchange(leg)

        logger.info(f"Built symbol: {symbol} on exchange: {exchange}")

        # Calculate quantity per account (will be calculated per account if margin calculator is enabled)
        base_quantity = self._calculate_quantity(leg, len(self.accounts))

        if base_quantity <= 0 and not self.use_margin_calculator:
            logger.error(f"Invalid quantity calculated for leg {leg.leg_number}: {base_quantity}")
            return [{
                'leg': leg.leg_number,
                'status': 'error',
                'error': f'Invalid quantity: {base_quantity}'
            }]

        # Execute on each account (using threads for parallel execution)
        threads = []
        logger.info(f"Executing leg {leg.leg_number} on {len(self.accounts)} accounts: {[a.account_name for a in self.accounts]}")

        for account in self.accounts:
            # Calculate quantity for this specific account if using margin calculator
            if self.use_margin_calculator:
                quantity = self._calculate_quantity(leg, 1, account)
                if quantity <= 0:
                    logger.warning(f"Skipping {account.account_name} - insufficient margin for {leg.instrument}")
                    results.append({
                        'account': account.account_name,
                        'symbol': symbol,
                        'status': 'skipped',
                        'error': 'Insufficient margin',
                        'leg': leg.leg_number
                    })
                    continue
            else:
                quantity = base_quantity

            logger.info(f"Starting thread for account {account.account_name}, leg {leg.leg_number}, qty {quantity}")
            thread = threading.Thread(
                target=self._execute_on_account,
                args=(account, leg, symbol, exchange, quantity, results)
            )
            thread.start()
            threads.append(thread)

        logger.info(f"Waiting for {len(threads)} threads to complete for leg {leg.leg_number}")

        # Wait for all threads to complete
        for thread in threads:
            thread.join()

        logger.info(f"All threads completed for leg {leg.leg_number}. Total results: {len(results)}")

        # Mark leg as executed in the main session after all accounts complete
        # Check if at least one order succeeded
        successful_orders = [r for r in results if r.get('status') == 'success']
        failed_orders = [r for r in results if r.get('status') in ['failed', 'error']]
        skipped_orders = [r for r in results if r.get('status') == 'skipped']

        logger.info(f"Leg {leg.leg_number} execution summary: {len(successful_orders)} success, {len(failed_orders)} failed, {len(skipped_orders)} skipped")

        # Log details of any failures
        for failed in failed_orders:
            logger.error(f"Failed order on {failed.get('account', 'unknown')}: {failed.get('error', 'unknown error')}")

        if successful_orders:
            try:
                # Mark leg as executed in current session
                leg.is_executed = True
                db.session.commit()
                logger.debug(f"Leg {leg.leg_number} marked as executed (main session)")
            except Exception as e:
                logger.error(f"Failed to mark leg as executed: {e}")
                db.session.rollback()

        return results

    def _execute_on_account(self, account: TradingAccount, leg: StrategyLeg,
                           symbol: str, exchange: str, quantity: int, results: List):
        """Execute order on a specific account"""
        account_name = account.account_name
        logger.info(f"[THREAD START] Executing leg {leg.leg_number} on account {account_name}: {symbol} {leg.action} qty={quantity}")

        try:

            # Get API key before entering thread context
            api_key = account.get_api_key()
            account_id = account.id
            account_name = account.account_name
            host_url = account.host_url

            client = ExtendedOpenAlgoAPI(
                api_key=api_key,
                host=host_url
            )

            # Prepare order parameters based on order type and price condition
            order_params = {
                'strategy': f"Strategy_{self.strategy.id}",
                'symbol': symbol,
                'action': leg.action,
                'exchange': exchange,
                'product': self.strategy.product_order_type or 'MIS',  # Use strategy's product order type
                'quantity': quantity
            }

            # Handle different order types
            if leg.order_type == 'MARKET':
                order_params['price_type'] = 'MARKET'

            elif leg.order_type == 'LIMIT':
                # For LIMIT orders with ABOVE/BELOW condition, determine actual order type
                if leg.price_condition == 'ABOVE':
                    # If price crosses ABOVE, use SL-MKT for BUY, LIMIT for SELL
                    if leg.action == 'BUY':
                        order_params['price_type'] = 'SL-MKT'
                        order_params['trigger_price'] = leg.trigger_price if leg.trigger_price else leg.limit_price
                    else:  # SELL
                        order_params['price_type'] = 'LIMIT'
                        order_params['price'] = leg.limit_price
                elif leg.price_condition == 'BELOW':
                    # If price crosses BELOW, use LIMIT for BUY, SL-MKT for SELL
                    if leg.action == 'BUY':
                        order_params['price_type'] = 'LIMIT'
                        order_params['price'] = leg.limit_price
                    else:  # SELL
                        order_params['price_type'] = 'SL-MKT'
                        order_params['trigger_price'] = leg.trigger_price if leg.trigger_price else leg.limit_price
                else:
                    # Default LIMIT order without condition
                    order_params['price_type'] = 'LIMIT'
                    if leg.limit_price:
                        order_params['price'] = leg.limit_price

            elif leg.order_type == 'SL-MKT':
                order_params['price_type'] = 'SL-MKT'
                if leg.trigger_price:
                    order_params['trigger_price'] = leg.trigger_price

            elif leg.order_type == 'SL-LMT':
                order_params['price_type'] = 'SL-LMT'
                if leg.limit_price:
                    order_params['price'] = leg.limit_price
                if leg.trigger_price:
                    order_params['trigger_price'] = leg.trigger_price

            print(f"[ORDER PARAMS] Placing order for {account_name}: {order_params}")
            logger.debug(f"Order params: {order_params}")

            # Place order
            response = client.placeorder(**order_params)
            print(f"[ORDER RESPONSE] {response}")

            if response.get('status') == 'success':
                order_id = response.get('orderid')

                # Wait a moment for order to be processed
                import time
                time.sleep(1)

                # Fetch actual order status
                order_status_data = self._get_order_status(client, order_id, self.strategy.name)

                # Determine execution status based on broker order status
                # OpenAlgo valid statuses: 'complete', 'open', 'rejected', 'cancelled'
                broker_order_status = order_status_data.get('order_status', 'open') if order_status_data else 'open'

                # Map OpenAlgo broker status to our internal execution status
                if broker_order_status in ['rejected', 'cancelled']:
                    # Rejected/cancelled orders are marked as failed
                    execution_status = 'failed'
                    logger.warning(f"Order {order_id} was {broker_order_status} by broker for {symbol} on {account_name}")
                elif broker_order_status == 'complete':
                    # Completed orders are marked as entered (position opened)
                    execution_status = 'entered'
                    logger.info(f"Order {order_id} completed successfully for {symbol} on {account_name}")
                elif broker_order_status == 'open':
                    # Open orders (not yet filled) are marked as pending
                    execution_status = 'pending'
                    logger.info(f"Order {order_id} is open (pending fill) for {symbol} on {account_name}")
                else:
                    # Unknown status - mark as pending with warning
                    execution_status = 'pending'
                    logger.warning(f"Unknown broker order status '{broker_order_status}' for order {order_id}, defaulting to pending")

                # Create execution record with app context for thread safety
                with self.app.app_context():
                    execution = StrategyExecution(
                        strategy_id=self.strategy.id,
                        account_id=account_id,
                        leg_id=leg.id,
                        order_id=order_id,
                        symbol=symbol,
                        exchange=exchange,
                        quantity=quantity,
                        status=execution_status,
                        broker_order_status=broker_order_status,
                        entry_time=datetime.utcnow(),
                        entry_price=order_status_data.get('average_price') if order_status_data else None
                    )

                    with self.lock:
                        db.session.add(execution)

                        # Note: leg.is_executed is set in main session after all threads complete
                        # This avoids session conflicts between threads

                        # Retry commit with exponential backoff for SQLite locks
                        max_retries = 5
                        for attempt in range(max_retries):
                            try:
                                db.session.commit()
                                break
                            except Exception as commit_error:
                                if attempt < max_retries - 1:
                                    import time as time_sleep
                                    db.session.rollback()
                                    wait_time = 0.1 * (2 ** attempt)  # Exponential backoff: 0.1, 0.2, 0.4, 0.8, 1.6 seconds
                                    logger.debug(f"DB locked, retrying in {wait_time}s (attempt {attempt + 1}/{max_retries})")
                                    time_sleep.sleep(wait_time)
                                else:
                                    logger.error(f"Failed to commit after {max_retries} attempts: {commit_error}")
                                    db.session.rollback()
                                    raise

                        # Report status based on execution status
                        if execution_status == 'failed':
                            results.append({
                                'account': account_name,
                                'symbol': symbol,
                                'order_id': order_id,
                                'status': 'failed',
                                'error': f"Order {broker_order_status} by broker",
                                'order_status': broker_order_status,
                                'leg': leg.leg_number
                            })
                        elif execution_status == 'pending':
                            results.append({
                                'account': account_name,
                                'symbol': symbol,
                                'order_id': order_id,
                                'status': 'pending',
                                'message': 'Order placed but not yet filled',
                                'order_status': broker_order_status,
                                'leg': leg.leg_number
                            })
                        else:  # execution_status == 'entered'
                            results.append({
                                'account': account_name,
                                'symbol': symbol,
                                'order_id': order_id,
                                'status': 'success',
                                'order_status': broker_order_status,
                                'executed_price': order_status_data.get('average_price', 0) if order_status_data else 0,
                                'leg': leg.leg_number
                            })

                            # Start monitoring for exits only for successfully filled positions
                            self._start_exit_monitoring(execution)

                # Log appropriate message based on execution status
                if execution_status == 'failed':
                    logger.warning(f"[THREAD REJECTED] Leg {leg.leg_number} was {broker_order_status} by broker on {account_name}, order_id: {order_id}")
                elif execution_status == 'pending':
                    logger.info(f"[THREAD PENDING] Leg {leg.leg_number} order placed but not yet filled on {account_name}, order_id: {order_id}")
                else:
                    logger.info(f"[THREAD SUCCESS] Leg {leg.leg_number} executed successfully on {account_name}, order_id: {order_id}")

            else:
                error_msg = response.get('message', 'Order placement failed')
                logger.error(f"[THREAD FAILED] Leg {leg.leg_number} failed on {account_name}: {error_msg}")
                with self.lock:
                    results.append({
                        'account': account_name,
                        'symbol': symbol,
                        'status': 'failed',
                        'error': error_msg,
                        'leg': leg.leg_number
                    })

        except Exception as e:
            logger.error(f"[THREAD ERROR] Error executing leg {leg.leg_number} on account {account_name}: {e}", exc_info=True)
            with self.lock:
                results.append({
                    'account': account_name if 'account_name' in locals() else 'unknown',
                    'symbol': symbol,
                    'status': 'error',
                    'error': str(e),
                    'leg': leg.leg_number
                })

        logger.info(f"[THREAD END] Completed execution for leg {leg.leg_number} on account {account_name}")

    def _get_order_status(self, client: ExtendedOpenAlgoAPI, order_id: str, strategy_name: str) -> Dict:
        """Fetch order status from broker using OpenAlgo API"""
        try:
            response = client.orderstatus(
                order_id=order_id,
                strategy=strategy_name
            )

            if response.get('status') == 'success':
                data = response.get('data', {})
                logger.info(f"Order {order_id} status: {data.get('order_status')} at price {data.get('price')}")
                return data
            else:
                logger.warning(f"Failed to get order status for {order_id}: {response.get('message')}")
                return {}
        except Exception as e:
            logger.error(f"Error fetching order status for {order_id}: {e}")
            return {}

    def _start_exit_monitoring_async(self, execution_id: int):
        """Start exit monitoring for an execution (async version)"""
        # This will be called after execution is saved
        # For now, just log - actual implementation would start monitoring
        logger.info(f"Exit monitoring would start for execution {execution_id}")

    def _build_symbol(self, leg: StrategyLeg) -> str:
        """Build OpenAlgo symbol format based on leg configuration"""
        try:
            logger.info(f"Building symbol for leg {leg.leg_number}: {leg.instrument} {leg.product_type} "
                       f"strike_selection={leg.strike_selection} strike_offset={leg.strike_offset}")

            base_symbol = leg.instrument

            if not base_symbol:
                logger.error(f"No instrument specified for leg {leg.leg_number}")
                return ""

            if leg.product_type == 'options':
                # Get expiry date
                expiry = self._get_expiry_string(leg)

                if not expiry:
                    logger.error(f"Failed to get expiry for leg {leg.leg_number}")
                    return ""

                # Get strike price
                strike = self._get_strike_price(leg)
                logger.info(f"Got strike price: {strike} for {leg.strike_selection} offset={leg.strike_offset}")

                if not strike or strike == "0":
                    logger.error(f"Failed to get strike price for leg {leg.leg_number}")
                    return ""

                if not leg.option_type:
                    logger.error(f"No option type (CE/PE) specified for leg {leg.leg_number}")
                    return ""

                # Build option symbol: NIFTY28MAR2420800CE
                symbol = f"{base_symbol}{expiry}{strike}{leg.option_type}"
                print(f"[SYMBOL BUILD] Base: {base_symbol}, Expiry: {expiry}, Strike: {strike}, Type: {leg.option_type}")
                print(f"[FINAL SYMBOL] {symbol}")
                logger.info(f"Built option symbol: {symbol}")

            elif leg.product_type == 'futures':
                # Get expiry date
                expiry = self._get_expiry_string(leg)

                if not expiry:
                    logger.error(f"Failed to get expiry for futures leg {leg.leg_number}")
                    return ""

                # Build futures symbol: NIFTY28MAR24FUT
                symbol = f"{base_symbol}{expiry}FUT"
                logger.info(f"Built futures symbol: {symbol}")

            else:
                # Equity symbol
                symbol = base_symbol
                logger.info(f"Using equity symbol: {symbol}")

            return symbol

        except Exception as e:
            logger.error(f"Error building symbol for leg {leg.leg_number}: {e}")
            return ""

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
            # Import datetime at the top of the method to avoid shadowing
            from datetime import datetime as dt

            # Check cache first
            cache_key = f"{leg.instrument}_{leg.product_type}_{leg.expiry}"
            if cache_key in self.expiry_cache:
                cached_data = self.expiry_cache[cache_key]
                # Check if cache is still valid (less than 1 hour old)
                if (dt.utcnow() - cached_data['timestamp']).seconds < 3600:
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
                    def parse_expiry(exp_str):
                        """Parse expiry string like '10-JUL-25' to date"""
                        try:
                            return dt.strptime(exp_str, '%d-%b-%y')
                        except:
                            try:
                                return dt.strptime(exp_str, '%d%b%y')
                            except:
                                return dt.max

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
                        current_month = dt.now().month
                        current_year = dt.now().year

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
                        next_month = (dt.now().month % 12) + 1
                        next_year = dt.now().year if next_month > dt.now().month else dt.now().year + 1

                        for exp_str in sorted_expiries:
                            exp_date = parse_expiry(exp_str)
                            if exp_date.month == next_month and exp_date.year == next_year:
                                selected_expiry = exp_str
                                break

                        # If no next month expiry found, use the first expiry after current month
                        if not selected_expiry:
                            current_date = dt.now()
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
                            'timestamp': dt.utcnow()
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
                offset = leg.strike_offset if leg.strike_offset else 1  # Default to 1 if not set, not 0

                print(f"[STRIKE DEBUG] Strike selection: {leg.strike_selection}, Offset from DB: {leg.strike_offset}, Using offset: {offset}")
                logger.info(f"Strike selection: {leg.strike_selection}, Offset from DB: {leg.strike_offset}, Using offset: {offset}")

                # If offset is 0, it means we're at ATM which is wrong for ITM/OTM
                if offset == 0:
                    logger.warning(f"ITM/OTM selected but offset is 0, defaulting to 1")
                    offset = 1

                # Validate offset range (1-20)
                offset = max(1, min(20, offset))  # Changed min from 0 to 1

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

                print(f"[STRIKE RESULT] {leg.instrument} {leg.option_type}: Spot={spot_price}, ATM={atm_strike}, "
                      f"{leg.strike_selection}{offset}={strike}")
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
                    logger.info(f"Using WebSocket price for {instrument}: {price_info['ltp']}")
                    return price_info['ltp']

            # Check option chain service for cached underlying price
            underlying_key = f"{instrument}_spot"
            if underlying_key in option_chain_service.active_managers:
                manager = option_chain_service.active_managers[underlying_key]
                if manager and manager.underlying_ltp > 0:
                    logger.info(f"Using option chain price for {instrument}: {manager.underlying_ltp}")
                    return manager.underlying_ltp

            # Fallback to API call
            if self.accounts:
                client = ExtendedOpenAlgoAPI(
                    api_key=self.accounts[0].get_api_key(),
                    host=self.accounts[0].host_url
                )

                response = client.quotes(symbol=instrument, exchange=exchange)
                if response.get('status') == 'success':
                    ltp = response.get('data', {}).get('ltp', 0)
                    logger.info(f"Using API price for {instrument}: {ltp}")
                    return ltp
                else:
                    logger.error(f"API quote failed for {instrument}: {response.get('message')}")

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

    def _calculate_quantity(self, leg: StrategyLeg, num_accounts: int, account: TradingAccount = None) -> int:
        """Calculate quantity per account based on allocation type and available margin"""
        # Get lot size for the instrument
        lot_size = self._get_lot_size(leg)

        # If margin calculator is enabled and account provided, calculate based on margin
        if self.use_margin_calculator and self.margin_calculator and account:
            # Determine trade type for margin calculation
            trade_type = self._get_trade_type_for_margin(leg)

            # Get available margin for the account
            if account.id not in self.account_margins:
                self.account_margins[account.id] = self.margin_calculator.get_available_margin(account)

            available_margin = self.account_margins[account.id]

            # Calculate optimal lot size based on margin
            optimal_lots, details = self.margin_calculator.calculate_lot_size(
                account=account,
                instrument=leg.instrument,
                trade_type=trade_type,
                quality_grade=self.trade_quality,
                available_margin=available_margin
            )

            if optimal_lots > 0:
                # Update remaining margin for next trades
                margin_used = optimal_lots * details.get('margin_per_lot', 0)
                self.account_margins[account.id] -= margin_used

                # Convert lots to quantity
                total_quantity = optimal_lots * lot_size

                logger.info(f"Margin-based calculation for {account.account_name}: "
                           f"{optimal_lots} lots = {total_quantity} qty "
                           f"(Margin: {available_margin:.2f} -> {self.account_margins[account.id]:.2f})")

                return total_quantity
            else:
                logger.warning(f"Insufficient margin for {leg.instrument} on {account.account_name}")
                return 0

        # Fallback to original calculation if margin calculator not used
        if leg.quantity and leg.quantity > 0:
            # If quantity is explicitly set, use it
            total_quantity = leg.quantity
        elif leg.lots and leg.lots > 0:
            # Otherwise calculate from lots
            total_quantity = leg.lots * lot_size
        else:
            # Default to 1 lot
            total_quantity = lot_size

        logger.info(f"Leg {leg.leg_number}: Lots={leg.lots}, Quantity={leg.quantity}, "
                   f"Lot Size={lot_size}, Total Quantity={total_quantity}")

        # Distribute across accounts based on allocation type
        if num_accounts <= 0:
            return 0

        if self.strategy.allocation_type == 'equal':
            # Equal distribution across accounts
            quantity_per_account = total_quantity
            # Note: For equal allocation, each account gets the full quantity
            # If you want to split, use: total_quantity // num_accounts
        elif self.strategy.allocation_type == 'proportional':
            # Would implement proportional allocation based on account value
            quantity_per_account = total_quantity
        else:
            # Custom allocation - default to full quantity per account
            quantity_per_account = total_quantity

        logger.info(f"Quantity per account: {quantity_per_account} for {num_accounts} accounts")
        return quantity_per_account

    def _get_trade_type_for_margin(self, leg: StrategyLeg) -> str:
        """Determine trade type for margin calculation"""
        if leg.product_type == 'options':
            if leg.action == 'SELL':
                # Check if it's part of a spread (both CE and PE)
                is_spread = self._is_spread_strategy(leg)
                return 'sell_c_and_p' if is_spread else 'sell_c_p'
            else:
                return 'buy'
        elif leg.product_type == 'futures':
            return 'futures'
        else:
            return 'buy'

    def _is_spread_strategy(self, current_leg: StrategyLeg) -> bool:
        """Check if current leg is part of a spread strategy"""
        all_legs = self.strategy.legs.all()
        for other_leg in all_legs:
            if (other_leg.instrument == current_leg.instrument and
                other_leg.product_type == 'options' and
                other_leg.action == 'SELL' and
                other_leg.id != current_leg.id):
                # Check if one is CE and other is PE
                if ((current_leg.option_type == 'CE' and other_leg.option_type == 'PE') or
                    (current_leg.option_type == 'PE' and other_leg.option_type == 'CE')):
                    return True
        return False

    def _get_lot_size(self, leg: StrategyLeg) -> int:
        """Get lot size for instrument from database"""
        from app.models import TradingSettings

        # Try to get lot size from user's trading settings
        if self.strategy.user_id:
            setting = TradingSettings.query.filter_by(
                user_id=self.strategy.user_id,
                symbol=leg.instrument,
                is_active=True
            ).first()

            if setting:
                logger.info(f"Using lot size {setting.lot_size} for {leg.instrument} from database")
                return setting.lot_size

        # Fallback to defaults if not found (shouldn't happen if settings are initialized)
        default_lot_sizes = {
            'NIFTY': 75,
            'BANKNIFTY': 35,
            'FINNIFTY': 65,
            'MIDCPNIFTY': 75,
            'SENSEX': 20
        }
        lot_size = default_lot_sizes.get(leg.instrument, 75)
        logger.warning(f"Using default lot size {lot_size} for {leg.instrument}")
        return lot_size

    def _start_exit_monitoring(self, execution: StrategyExecution):
        """Start monitoring position for exit conditions using WebSocket data"""
        # Subscribe to WebSocket for real-time price updates
        self._subscribe_to_websocket(execution.symbol, execution.exchange)

        # Start monitoring thread - pass execution ID to avoid session issues
        thread = threading.Thread(
            target=self._monitor_exit_conditions,
            args=(execution.id,),
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
                self.websocket_manager.subscribe_batch(instruments, mode='ltp')

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

    def _monitor_exit_conditions(self, execution_id: int):
        """Monitor position for exit conditions using real-time WebSocket data"""
        import time as time_module
        from app import create_app

        # Create new app context for this thread
        app = create_app()
        with app.app_context():
            # Query execution fresh in this thread's context
            execution = StrategyExecution.query.get(execution_id)
            if not execution:
                logger.error(f"[MONITOR] Execution {execution_id} not found")
                return

            logger.info(f"[MONITOR_START] Starting monitoring for execution ID={execution.id}, symbol={execution.symbol}")

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
                        execution.entry_price = order_response.get('data', {}).get('average_price')
                        db.session.commit()
                        logger.info(f"[MONITOR] Fetched entry price: {execution.entry_price} for {symbol}")

                logger.debug(f"[MONITOR_LOOP] Entry price={execution.entry_price}, Status={execution.status}")

                while execution.status == 'entered':
                    logger.debug(f"[MONITOR_TICK] Monitoring tick for {symbol}")
                    # First check position status
                    position_response = client.openposition(
                        strategy=f"Strategy_{self.strategy.id}",
                        symbol=execution.symbol,
                        exchange=execution.exchange,
                        product=self.strategy.product_order_type or 'MIS'
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
                            logger.debug(f"[MONITOR_WS] Using WebSocket price for {symbol}: {ltp}")
                    else:
                        logger.debug(f"[MONITOR_WS] No WebSocket data for {symbol}, falling back to REST")

                    # Fallback to REST API if no recent WebSocket data
                    if not ltp:
                        logger.debug(f"[MONITOR_REST] Fetching quote for {symbol}")
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
                            logger.debug(f"[MONITOR_REST] Got price for {symbol}: {ltp}")
                        else:
                            logger.debug(f"[MONITOR_REST] Failed to get quote: {quote_response}")

                    if not ltp:
                        logger.debug(f"[MONITOR_NOLTP] No LTP available for {symbol}, skipping P&L check")

                    if ltp and execution.entry_price:
                        # Calculate P&L based on action
                        if execution.leg.action == 'BUY':
                            pnl = (ltp - execution.entry_price) * execution.quantity
                        else:  # SELL
                            pnl = (execution.entry_price - ltp) * execution.quantity

                        execution.unrealized_pnl = pnl

                        # Commit with retry logic for SQLite database lock
                        try:
                            db.session.commit()
                        except Exception as commit_error:
                            logger.debug(f"DB commit error (will retry): {commit_error}")
                            db.session.rollback()
                            # Don't fail - P&L will update on next tick

                        # Log P&L periodically for monitoring (DEBUG level to avoid spam)
                        if int(time_module.time()) % 30 == 0:  # Every 30 seconds
                            logger.debug(f"Position {symbol}: Entry={execution.entry_price}, LTP={ltp}, P&L={pnl:.2f}")

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

        logger.debug(f"[EXIT_CHECK] Symbol={execution.symbol}, Entry={execution.entry_price}, LTP={ltp}, P&L={pnl:.2f}")

        # Check stop loss
        if leg.stop_loss_value:
            if leg.stop_loss_type == 'points':
                logger.debug(f"[EXIT_CHECK] SL Type=points, SL Value={leg.stop_loss_value}, abs(P&L)={abs(pnl):.2f}")
                if abs(pnl) >= leg.stop_loss_value:
                    logger.info(f"[EXIT_TRIGGER] Stop loss hit! P&L={pnl:.2f} >= SL={leg.stop_loss_value}")
                    return True
            elif leg.stop_loss_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                sl_threshold = entry_value * leg.stop_loss_value / 100
                logger.debug(f"[EXIT_CHECK] SL Type=percentage, SL%={leg.stop_loss_value}, Threshold={sl_threshold:.2f}, abs(P&L)={abs(pnl):.2f}")
                if abs(pnl) >= sl_threshold:
                    logger.info(f"[EXIT_TRIGGER] Stop loss hit! abs(P&L)={abs(pnl):.2f} >= Threshold={sl_threshold:.2f}")
                    return True

        # Check take profit
        if leg.take_profit_value:
            if leg.take_profit_type == 'points':
                logger.debug(f"[EXIT_CHECK] TP Type=points, TP Value={leg.take_profit_value}, P&L={pnl:.2f}")
                if pnl >= leg.take_profit_value:
                    logger.info(f"[EXIT_TRIGGER] Take profit hit! P&L={pnl:.2f} >= TP={leg.take_profit_value}")
                    return True
            elif leg.take_profit_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                tp_threshold = entry_value * leg.take_profit_value / 100
                logger.debug(f"[EXIT_CHECK] TP Type=percentage, TP%={leg.take_profit_value}, Threshold={tp_threshold:.2f}, P&L={pnl:.2f}")
                if pnl >= tp_threshold:
                    logger.info(f"[EXIT_TRIGGER] Take profit hit! P&L={pnl:.2f} >= Threshold={tp_threshold:.2f}")
                    return True

        # Check max loss/profit at strategy level
        if self.strategy.max_loss:
            total_pnl = self._get_strategy_pnl()
            logger.debug(f"[EXIT_CHECK] Strategy Max Loss={self.strategy.max_loss}, Total P&L={total_pnl:.2f}")
            if abs(total_pnl) >= self.strategy.max_loss:
                logger.info(f"[EXIT_TRIGGER] Strategy max loss hit! Total P&L={total_pnl:.2f}")
                return True

        if self.strategy.max_profit:
            total_pnl = self._get_strategy_pnl()
            logger.debug(f"[EXIT_CHECK] Strategy Max Profit={self.strategy.max_profit}, Total P&L={total_pnl:.2f}")
            if total_pnl >= self.strategy.max_profit:
                logger.info(f"[EXIT_TRIGGER] Strategy max profit hit! Total P&L={total_pnl:.2f}")
                return True

        logger.debug(f"[EXIT_CHECK] No exit conditions met")
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
                product=self.strategy.product_order_type or 'MIS',
                quantity=execution.quantity
            )

            if response.get('status') == 'success':
                # Get the exit order ID
                exit_order_id = response.get('orderid')

                # Update original execution status
                execution.status = 'exited'
                execution.exit_time = datetime.utcnow()
                execution.exit_reason = reason
                execution.realized_pnl = execution.unrealized_pnl

                # Fetch exit order details to get executed price
                order_status_response = client.orderstatus(
                    order_id=exit_order_id,
                    strategy=f"Strategy_{self.strategy.id}"
                )

                if order_status_response.get('status') == 'success':
                    order_data = order_status_response.get('data', {})
                    execution.exit_price = order_data.get('average_price')
                    execution.broker_order_status = order_data.get('status')

                db.session.commit()

                logger.info(f"Exited position for {execution.symbol}: {reason}, Exit Order ID: {exit_order_id}, Exit Price: {execution.exit_price}")

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