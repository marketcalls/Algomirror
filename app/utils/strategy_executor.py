"""
Strategy Executor Service
Handles multi-account strategy execution with OpenAlgo integration

Uses standard threading for background tasks.
"""

import logging
import threading
from datetime import datetime, time
from typing import Dict, List, Any, Optional
import json

# Cross-platform compatibility
from app.utils.compat import sleep, create_lock

from app import db
from app.models import Strategy, StrategyLeg, StrategyExecution, TradingAccount
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.background_service import option_chain_service
from app.utils.order_status_poller import order_status_poller

logger = logging.getLogger(__name__)


class StrategyExecutor:
    """Execute trading strategies across multiple accounts"""

    def __init__(self, strategy: Strategy, use_margin_calculator: bool = True, trade_quality: str = 'B'):
        self.strategy = strategy
        self.accounts = self._get_active_accounts()
        self.execution_results = []
        self.lock = create_lock()
        self.websocket_manager = None
        self.price_subscriptions = {}  # Map symbol to WebSocket subscription
        self.latest_prices = {}  # Cache latest prices from WebSocket
        self.expiry_cache = {}  # Cache expiry dates to reduce API calls
        self.use_margin_calculator = use_margin_calculator
        self.trade_quality = trade_quality
        self.margin_calculator = None
        self.account_margins = {}  # Track available margin per account
        self.pre_calculated_quantities = {}  # Store pre-calculated quantities for straddles/strangles

        # Map strategy risk_profile to quality grade for database lookup
        # Aggressive -> Grade A, Balanced -> Grade B, Conservative -> Grade C
        self.risk_profile_to_grade = {
            'aggressive': 'A',
            'balanced': 'B',
            'conservative': 'C'
        }

        # Get margin percentage and margin source from TradeQuality table in database
        # This ensures consistency with the Margin Calculator page
        self.margin_percentage, self.margin_source = self._get_margin_percentage_from_db(strategy)

        # Determine is_expiry based on market_condition setting
        # 'expiry' -> True (use expiry margins)
        # 'non_expiry' -> False (use non-expiry margins)
        # 'any' or None -> None (auto-detect based on today's date)
        market_condition = strategy.market_condition
        if market_condition == 'expiry':
            self.is_expiry_override = True
        elif market_condition == 'non_expiry':
            self.is_expiry_override = False
        else:  # 'any' or None
            self.is_expiry_override = None

        logger.debug(f"Strategy {strategy.id}: market_condition='{market_condition}', is_expiry_override={self.is_expiry_override}")

        # Store app reference for thread context
        from flask import current_app
        self.app = current_app._get_current_object()

        if use_margin_calculator:
            from app.utils.margin_calculator import MarginCalculator
            self.margin_calculator = MarginCalculator(strategy.user_id)
            margin_type = "cash" if self.margin_source == 'cash' else "available"
            logger.debug(f"Strategy {strategy.id} ({strategy.name}): Using {self.margin_percentage*100}% {margin_type} margin based on risk_profile '{strategy.risk_profile}'")

    def _get_margin_percentage_from_db(self, strategy: Strategy) -> tuple:
        """
        Fetch margin percentage and margin source from TradeQuality table in database.
        This ensures consistency with the Margin Calculator page.

        Supports two formats:
        - New format: 'grade_A', 'grade_B', 'grade_C', 'grade_D', etc.
        - Legacy format: 'aggressive', 'balanced', 'conservative' (mapped to A, B, C)

        Returns:
            tuple: (margin_percentage as decimal, margin_source as string)
                   margin_source: 'available' for option sellers, 'cash' for option buyers
        """
        from app.models import TradeQuality

        risk_profile = strategy.risk_profile

        # If fixed_lots or not set, return default
        if not risk_profile or risk_profile == 'fixed_lots':
            return 0.65, 'available'  # Default 65% of available margin

        # Determine quality grade from risk profile
        quality_grade = None

        # New format: grade_X (e.g., grade_A, grade_B, grade_D)
        if risk_profile.startswith('grade_'):
            quality_grade = risk_profile.replace('grade_', '')
        # Legacy format: map old names to grades
        elif risk_profile in self.risk_profile_to_grade:
            quality_grade = self.risk_profile_to_grade.get(risk_profile)

        if not quality_grade:
            logger.warning(f"Unknown risk_profile '{risk_profile}', using default 65%")
            return 0.65, 'available'

        try:
            # Fetch from TradeQuality table
            trade_quality = TradeQuality.query.filter_by(
                user_id=strategy.user_id,
                quality_grade=quality_grade,
                is_active=True
            ).first()

            if trade_quality and trade_quality.margin_percentage:
                margin_pct = trade_quality.margin_percentage / 100  # Convert from 50 to 0.50
                # Get margin_source: 'available' for option sellers, 'cash' for option buyers
                margin_source = getattr(trade_quality, 'margin_source', 'available') or 'available'
                logger.debug(f"Loaded from DB: Grade {quality_grade} = {trade_quality.margin_percentage}% {margin_source} margin")
                return margin_pct, margin_source
            else:
                # Fallback if not found in DB
                logger.warning(f"TradeQuality not found for Grade {quality_grade}, using fallback 65%")
                return 0.65, 'available'

        except Exception as e:
            logger.error(f"Error fetching TradeQuality from DB: {e}")
            return 0.65, 'available'

    def _get_active_accounts(self) -> List[TradingAccount]:
        """Get active trading accounts for strategy"""
        account_ids = self.strategy.selected_accounts or []
        return TradingAccount.query.filter(
            TradingAccount.id.in_(account_ids),
            TradingAccount.is_active == True
        ).all()

    def _get_margin_for_account(self, account: TradingAccount) -> float:
        """
        Get the appropriate margin for an account based on margin_source setting.

        For Option Sellers (margin_source='available'): Uses available margin (cash + collateral)
        For Option Buyers (margin_source='cash'): Uses cash margin only
        """
        if not self.margin_calculator:
            return 0.0

        if self.margin_source == 'cash':
            # Option buyers use cash margin only
            margin = self.margin_calculator.get_cash_margin(account)
            logger.debug(f"[MARGIN] Using CASH margin for {account.account_name}: {margin:,.2f}")
        else:
            # Option sellers use available margin (cash + collateral)
            margin = self.margin_calculator.get_available_margin(account)
            logger.debug(f"[MARGIN] Using AVAILABLE margin for {account.account_name}: {margin:,.2f}")

        return margin

    def execute(self) -> List[Dict[str, Any]]:
        """
        Execute strategy across all selected accounts with BUY-FIRST priority.

        Execution order:
        1. All BUY legs execute in parallel across all accounts
        2. Wait for all BUY orders to be placed
        3. All SELL legs execute in parallel across all accounts

        This ensures covered positions (buys before sells) and reduces margin requirements.
        """
        if not self.accounts:
            raise ValueError("No active accounts selected for strategy")

        # Ensure legs are loaded and filter only non-executed legs
        all_legs = self.strategy.legs.order_by(StrategyLeg.leg_number).all()
        legs = [leg for leg in all_legs if not leg.is_executed]

        print(f"\n[EXECUTE START] Strategy {self.strategy.id} - {self.strategy.name}")
        print(f"[EXECUTE] Total legs: {len(all_legs)}, Unexecuted legs: {len(legs)}")
        print(f"[EXECUTE MODE] BUY-FIRST PRIORITY EXECUTION")
        for leg in legs:
            print(f"  Leg {leg.leg_number}: {leg.instrument} {leg.action} {leg.option_type} {leg.strike_selection} offset={leg.strike_offset}")

        if not legs:
            raise ValueError("No unexecuted legs found for this strategy")

        # Separate legs by action: BUY legs execute first, then SELL legs
        buy_legs = [leg for leg in legs if leg.action == 'BUY']
        sell_legs = [leg for leg in legs if leg.action == 'SELL']

        print(f"[EXECUTE] BUY legs: {len(buy_legs)}, SELL legs: {len(sell_legs)}")
        logger.debug(f"[BUY-FIRST MODE] Executing strategy {self.strategy.id}: {len(buy_legs)} BUY legs, "
                   f"{len(sell_legs)} SELL legs across {len(self.accounts)} accounts")

        # PRE-CALCULATION PHASE: Calculate quantities for straddles/strangles/spreads
        # This ensures all related legs get the same quantity
        if self.use_margin_calculator:
            self._pre_calculate_multi_leg_quantities(legs)

        results = []
        results_lock = create_lock()

        # PHASE 1: Execute all BUY legs in PARALLEL
        if buy_legs:
            print(f"\n[PHASE 1] Executing {len(buy_legs)} BUY leg(s) across {len(self.accounts)} accounts...")
            logger.debug(f"[PHASE 1] Starting BUY legs execution")

            buy_threads = []
            for i, leg in enumerate(buy_legs, 1):
                logger.debug(f"[BUY LEG {i}] Starting parallel thread: "
                           f"{leg.instrument} {leg.action} {leg.option_type if leg.product_type == 'options' else ''}")

                thread = threading.Thread(
                    target=self._execute_leg_parallel,
                    args=(leg, results, results_lock),
                    name=f"BUY-Leg-{leg.leg_number}-{leg.instrument}",
                    daemon=False
                )
                thread.start()
                buy_threads.append(thread)

            # Wait for all BUY legs to complete before proceeding to SELL
            logger.debug(f"[PHASE 1] Waiting for {len(buy_threads)} BUY legs to complete...")
            for thread in buy_threads:
                thread.join()

            print(f"[PHASE 1] All BUY orders placed. Orders so far: {len(results)}")
            logger.debug(f"[PHASE 1 COMPLETE] All BUY legs completed. Orders: {len(results)}")

        # PHASE 2: Execute all SELL legs in PARALLEL (after BUY legs complete)
        if sell_legs:
            print(f"\n[PHASE 2] Executing {len(sell_legs)} SELL leg(s) across {len(self.accounts)} accounts...")
            logger.debug(f"[PHASE 2] Starting SELL legs execution")

            sell_threads = []
            for i, leg in enumerate(sell_legs, 1):
                logger.debug(f"[SELL LEG {i}] Starting parallel thread: "
                           f"{leg.instrument} {leg.action} {leg.option_type if leg.product_type == 'options' else ''}")

                thread = threading.Thread(
                    target=self._execute_leg_parallel,
                    args=(leg, results, results_lock),
                    name=f"SELL-Leg-{leg.leg_number}-{leg.instrument}",
                    daemon=False
                )
                thread.start()
                sell_threads.append(thread)

            # Wait for all SELL legs to complete
            logger.debug(f"[PHASE 2] Waiting for {len(sell_threads)} SELL legs to complete...")
            for thread in sell_threads:
                thread.join()

            print(f"[PHASE 2] All SELL orders placed. Total orders: {len(results)}")
            logger.debug(f"[PHASE 2 COMPLETE] All SELL legs completed. Total orders: {len(results)}")

        logger.debug(f"[COMPLETED] All {len(legs)} legs completed. Total orders: {len(results)}")
        print(f"[EXECUTE END] Total orders placed: {len(results)}")

        # CRITICAL: Mark legs as executed in the MAIN session after all threads complete
        # This ensures the commit happens in the correct session context
        print(f"\n[MAIN SESSION] ========== STARTING ==========")
        print(f"[MAIN SESSION] Processing {len(legs)} legs, {len(results)} results")
        print(f"[MAIN SESSION] Results dump: {results}")
        logger.debug(f"[MAIN SESSION] Processing {len(legs)} legs, {len(results)} results")

        for leg in legs:
            # Check if this leg had any results at all (order was attempted)
            leg_results = [r for r in results if r.get('leg') == leg.leg_number]
            successful = [r for r in leg_results if r.get('status') in ['success', 'pending']]
            print(f"[MAIN SESSION] Leg {leg.leg_number} (id={leg.id}): {len(leg_results)} results, {len(successful)} successful")
            logger.debug(f"[MAIN SESSION] Leg {leg.leg_number} (id={leg.id}): {len(leg_results)} results, {len(successful)} successful")

            # Mark as executed if we have any results (successful or not) - order was attempted
            # This prevents leg from being deleted on next save
            if leg_results:
                try:
                    # Refresh leg object from database to ensure we're in the right session
                    fresh_leg = StrategyLeg.query.get(leg.id)
                    print(f"[MAIN SESSION] Fresh leg query: {fresh_leg}, is_executed={fresh_leg.is_executed if fresh_leg else 'N/A'}")
                    logger.debug(f"[MAIN SESSION] Fresh leg query result: {fresh_leg}, is_executed={fresh_leg.is_executed if fresh_leg else 'N/A'}")
                    if fresh_leg and not fresh_leg.is_executed:
                        fresh_leg.is_executed = True
                        db.session.commit()
                        print(f"[MAIN SESSION] Leg {leg.leg_number} marked as is_executed=True - COMMITTED")
                        logger.debug(f"[MAIN SESSION] Leg {leg.leg_number} marked as is_executed=True")
                    elif fresh_leg and fresh_leg.is_executed:
                        print(f"[MAIN SESSION] Leg {leg.leg_number} already is_executed=True, skipping")
                        logger.debug(f"[MAIN SESSION] Leg {leg.leg_number} already is_executed=True, skipping")
                    else:
                        print(f"[MAIN SESSION] WARNING: Leg {leg.leg_number} not found in database!")
                        logger.warning(f"[MAIN SESSION] Leg {leg.leg_number} not found in database!")
                except Exception as e:
                    print(f"[MAIN SESSION] ERROR: Failed to mark leg {leg.leg_number} as executed: {e}")
                    logger.error(f"[MAIN SESSION] Failed to mark leg {leg.leg_number} as executed: {e}")
                    db.session.rollback()
            else:
                print(f"[MAIN SESSION] Leg {leg.leg_number} had no results at all - order not attempted?")
                logger.warning(f"[MAIN SESSION] Leg {leg.leg_number} had no results - order may not have been attempted")

        # INITIALIZE TSL VALUES immediately after execution
        # This ensures Initial Stop and Current Stop are available without refresh
        if self.strategy.trailing_sl and self.strategy.trailing_sl > 0:
            try:
                self._initialize_tsl_values(results)
            except Exception as e:
                logger.error(f"[TSL INIT] Failed to initialize TSL values: {e}")
                print(f"[TSL INIT] Error initializing TSL: {e}")

        print(f"[MAIN SESSION] ========== COMPLETED ==========")

        return results

    def _initialize_tsl_values(self, results: List[Dict]):
        """
        Initialize TSL (Trailing Stop Loss) values immediately after order execution.
        This calculates the initial_stop based on entry prices so it's available without refresh.
        """
        try:
            # Refresh strategy from DB
            strategy = Strategy.query.get(self.strategy.id)
            if not strategy or not strategy.trailing_sl:
                return

            # Get all entered executions for this strategy
            open_executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id,
                status='entered'
            ).all()

            if not open_executions:
                logger.debug(f"[TSL INIT] No open executions found for strategy {strategy.name}")
                return

            # Calculate net premium from entry prices
            # SELL legs contribute positive premium (we receive)
            # BUY legs contribute negative premium (we pay)
            net_premium = 0.0
            entry_value = 0.0

            for execution in open_executions:
                if execution.entry_price and execution.quantity:
                    position_value = execution.entry_price * execution.quantity
                    entry_value += position_value

                    leg_action = execution.leg.action.upper() if execution.leg else 'BUY'
                    if leg_action == 'SELL':
                        net_premium += position_value  # Credit received
                    else:
                        net_premium -= position_value  # Debit paid

            logger.debug(f"[TSL INIT] Strategy {strategy.name}: Net Premium={net_premium:.2f}, Entry Value={entry_value:.2f}")

            # Calculate initial stop based on trailing SL type and value
            trailing_type = strategy.trailing_sl_type or 'percentage'
            trailing_value = strategy.trailing_sl

            if trailing_type == 'percentage':
                # Initial stop = -X% of entry value
                initial_stop_pnl = -entry_value * (trailing_value / 100)
            elif trailing_type == 'points':
                initial_stop_pnl = -trailing_value
            else:  # 'amount'
                initial_stop_pnl = -trailing_value

            # Set TSL values
            strategy.trailing_sl_initial_stop = initial_stop_pnl
            strategy.trailing_sl_active = True
            strategy.trailing_sl_peak_pnl = 0.0  # Start at 0, will be updated by risk manager
            strategy.trailing_sl_trigger_pnl = initial_stop_pnl  # Current stop = initial stop at start

            db.session.commit()

            logger.info(f"[TSL INIT] Strategy {strategy.name}: Initial Stop={initial_stop_pnl:.2f}, "
                       f"Entry Value={entry_value:.2f}, Type={trailing_type}, Value={trailing_value}")
            print(f"[TSL INIT] {strategy.name}: Initial Stop=Rs.{initial_stop_pnl:.2f}")

        except Exception as e:
            logger.error(f"[TSL INIT] Error initializing TSL values: {e}", exc_info=True)
            db.session.rollback()

    def _execute_leg_parallel(self, leg: StrategyLeg, results: List, results_lock):
        """
        Execute a single leg across all accounts (called in parallel with other legs)
        Greenlet-safe version that appends to shared results list
        """
        try:
            # Create fresh app context for this thread (similar to _monitor_exit_conditions)
            from app import create_app
            app = create_app()

            with app.app_context():
                logger.debug(f"[LEG {leg.leg_number}] [STARTING] Starting parallel execution")

                # Reuse existing _execute_leg logic
                leg_results = self._execute_leg(leg)

                # Thread-safe append to results
                with results_lock:
                    results.extend(leg_results)

                logger.debug(f"[LEG {leg.leg_number}] [COMPLETED] Completed: {len(leg_results)} orders")

        except Exception as e:
            logger.error(f"[LEG {leg.leg_number}] [ERROR] Error: {e}", exc_info=True)
            with results_lock:
                results.append({
                    'leg': leg.leg_number,
                    'status': 'error',
                    'error': str(e)
                })

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

        logger.debug(f"Built symbol: {symbol} on exchange: {exchange}")

        # Calculate quantity per account (will be calculated per account if margin calculator is enabled)
        base_quantity = self._calculate_quantity(leg, len(self.accounts))

        if base_quantity <= 0 and not self.use_margin_calculator:
            logger.error(f"Invalid quantity calculated for leg {leg.leg_number}: {base_quantity}")
            return [{
                'leg': leg.leg_number,
                'status': 'error',
                'error': f'Invalid quantity: {base_quantity}'
            }]

        # Execute on each account (using parallel threads with BATCHING for scalability)
        # BATCH_SIZE limits concurrent threads to prevent resource exhaustion
        BATCH_SIZE = 5  # Max 5 concurrent threads at a time
        all_threads = []
        accounts_to_process = []

        logger.debug(f"Executing leg {leg.leg_number} on {len(self.accounts)} accounts: {[a.account_name for a in self.accounts]}")
        print(f"[EXECUTE] Leg {leg.leg_number}: Processing {len(self.accounts)} accounts in batches of {BATCH_SIZE}")

        # First, prepare all accounts with their quantities
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

            accounts_to_process.append((account, quantity))

        # Process accounts in batches
        total_accounts = len(accounts_to_process)
        batch_num = 0

        for batch_start in range(0, total_accounts, BATCH_SIZE):
            batch_end = min(batch_start + BATCH_SIZE, total_accounts)
            batch_accounts = accounts_to_process[batch_start:batch_end]
            batch_num += 1

            logger.debug(f"[BATCH {batch_num}] Processing accounts {batch_start + 1}-{batch_end} of {total_accounts}")
            print(f"[BATCH {batch_num}] Processing {len(batch_accounts)} accounts...")

            threads = []
            thread_index = 0

            for account, quantity in batch_accounts:
                logger.debug(f"Starting thread for account {account.account_name}, leg {leg.leg_number}, qty {quantity}")
                thread = threading.Thread(
                    target=self._execute_on_account,
                    args=(account, leg, symbol, exchange, quantity, results, thread_index),
                    daemon=True
                )
                thread.start()
                threads.append(thread)
                all_threads.append(thread)
                thread_index += 1

            # Wait for this batch to complete before starting next batch
            THREAD_TIMEOUT = 60
            for thread in threads:
                thread.join(timeout=THREAD_TIMEOUT)
                if thread.is_alive():
                    logger.error(f"[THREAD TIMEOUT] Thread still running after {THREAD_TIMEOUT}s in batch {batch_num}")

            logger.debug(f"[BATCH {batch_num}] Completed. Total results so far: {len(results)}")

            # Small delay between batches to let DB settle
            if batch_end < total_accounts:
                sleep(0.2)

        logger.debug(f"All {batch_num} batches completed for leg {leg.leg_number}. Total results: {len(results)}")

        # Count expected vs actual orders
        expected_orders = len(self.accounts)
        actual_results = len(results)

        # Mark leg as executed in the main session after all accounts complete
        # Check if at least one order succeeded or is pending (pending = order placed but not filled yet)
        successful_orders = [r for r in results if r.get('status') in ['success', 'pending']]
        failed_orders = [r for r in results if r.get('status') in ['failed', 'error']]
        skipped_orders = [r for r in results if r.get('status') == 'skipped']

        logger.warning(f"[LEG {leg.leg_number} SUMMARY] Expected: {expected_orders} accounts | "
                      f"Success: {len(successful_orders)} | Failed: {len(failed_orders)} | Skipped: {len(skipped_orders)} | "
                      f"Total results: {actual_results}")
        print(f"[LEG {leg.leg_number} SUMMARY] Expected: {expected_orders} | Success: {len(successful_orders)} | "
              f"Failed: {len(failed_orders)} | Skipped: {len(skipped_orders)}")

        # CRITICAL: Check for missing orders (results < expected)
        if actual_results < expected_orders:
            # Find which accounts are missing from results
            result_accounts = set(r.get('account') for r in results)
            all_accounts = set(a.account_name for a in self.accounts)
            missing_accounts = all_accounts - result_accounts

            for missing_account in missing_accounts:
                logger.error(f"[MISSING ORDER] Account {missing_account} has NO result for leg {leg.leg_number} - thread may have failed silently!")
                print(f"[MISSING ORDER] Account {missing_account} - NO RESULT!")
                # Add a failed result for tracking
                results.append({
                    'account': missing_account,
                    'symbol': symbol,
                    'status': 'error',
                    'error': 'Thread failed silently - no response received',
                    'leg': leg.leg_number
                })

            # Recalculate after adding missing
            failed_orders = [r for r in results if r.get('status') in ['failed', 'error']]

        # RETRY MECHANISM: Retry failed accounts (up to 2 additional attempts)
        if failed_orders and len(successful_orders) > 0:
            # Only retry if some succeeded (avoid infinite retry on systemic issues)
            failed_account_names = [r.get('account') for r in failed_orders]
            logger.warning(f"[RETRY] {len(failed_orders)} accounts failed, attempting retry: {failed_account_names}")
            print(f"[RETRY] Retrying {len(failed_orders)} failed accounts: {failed_account_names}")

            for retry_attempt in range(2):  # Up to 2 retries
                if not failed_orders:
                    break

                retry_threads = []
                retry_results = []

                for failed_result in failed_orders[:]:  # Copy list to iterate
                    failed_account_name = failed_result.get('account')
                    # Find the account object
                    account = next((a for a in self.accounts if a.account_name == failed_account_name), None)
                    if not account:
                        continue

                    # Determine quantity for retry
                    if self.use_margin_calculator:
                        retry_quantity = self._calculate_quantity(leg, 1, account)
                        if retry_quantity <= 0:
                            continue
                    else:
                        retry_quantity = base_quantity

                    logger.debug(f"[RETRY {retry_attempt + 1}] Retrying {failed_account_name} for leg {leg.leg_number}")

                    thread = threading.Thread(
                        target=self._execute_on_account,
                        args=(account, leg, symbol, exchange, retry_quantity, retry_results, retry_attempt),
                        daemon=True
                    )
                    thread.start()
                    retry_threads.append(thread)
                    sleep(0.5)  # Stagger retries

                # Wait for retry threads
                for thread in retry_threads:
                    thread.join(timeout=THREAD_TIMEOUT)

                # Check retry results
                for retry_result in retry_results:
                    if retry_result.get('status') in ['success', 'pending']:
                        # Remove from failed, add to successful
                        account_name = retry_result.get('account')
                        failed_orders = [f for f in failed_orders if f.get('account') != account_name]
                        successful_orders.append(retry_result)
                        # Update the original results list
                        results[:] = [r for r in results if r.get('account') != account_name or r.get('status') in ['success', 'pending']]
                        results.append(retry_result)
                        logger.warning(f"[RETRY SUCCESS] Account {account_name} succeeded on retry {retry_attempt + 1}")
                        print(f"[RETRY SUCCESS] {account_name} succeeded on retry!")

            if failed_orders:
                logger.error(f"[RETRY EXHAUSTED] {len(failed_orders)} accounts still failed after retries: "
                           f"{[f.get('account') for f in failed_orders]}")

        # Log details of any remaining failures
        for failed in failed_orders:
            logger.error(f"[FINAL FAILURE] Order failed on {failed.get('account', 'unknown')}: {failed.get('error', 'unknown error')}")

        # Note: is_executed=True is now set in the MAIN session after all threads complete
        # This thread-level attempt is kept as a backup but may fail due to session conflicts
        if successful_orders:
            try:
                # Try to mark leg as executed in thread session (backup, main session handles this)
                leg.is_executed = True
                db.session.commit()
                logger.debug(f"Leg {leg.leg_number} marked as executed (thread session - backup)")
            except Exception as e:
                # This is expected to fail sometimes due to session conflicts
                # The main session commit after threads complete will handle it
                logger.debug(f"Thread session commit for leg {leg.leg_number} failed (expected): {e}")
                db.session.rollback()

        return results

    def _execute_on_account(self, account: TradingAccount, leg: StrategyLeg,
                           symbol: str, exchange: str, quantity: int, results: List, thread_index: int):
        """Execute order on a specific account"""
        # Add staggered delay based on thread index to prevent OpenAlgo race condition
        # Each thread waits: index * 300ms (0ms, 300ms, 600ms, 900ms, ...)
        # This GUARANTEES threads never hit OpenAlgo at the same time
        delay = thread_index * 0.3
        if delay > 0:
            sleep(delay)
            logger.debug(f"[TASK {thread_index}] Waited {delay:.2f}s to prevent race condition")

        account_name = account.account_name
        logger.debug(f"[THREAD START] Executing leg {leg.leg_number} on account {account_name}: {symbol} {leg.action} qty={quantity}")

        # Create fresh app context for this thread to avoid session conflicts
        from app import create_app
        app = create_app()

        with app.app_context():
            try:
                # Get API key before creating client
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
                    'strategy': self.strategy.name,
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
                    # Simple LIMIT order
                    order_params['price_type'] = 'LIMIT'
                    if leg.limit_price:
                        order_params['price'] = leg.limit_price

                print(f"[ORDER PARAMS] Placing order for {account_name}: {order_params}")
                logger.debug(f"Order params: {order_params}")

                # Place order with freeze quantity check and retry logic for reliability
                from app.utils.freeze_quantity_handler import place_order_with_freeze_check

                max_retries = 3
                retry_delay = 1  # Start with 1 second
                response = None
                last_error = None

                for attempt in range(max_retries):
                    try:
                        # Use freeze-aware order placement
                        response = place_order_with_freeze_check(
                            client=client,
                            user_id=self.strategy.user_id,
                            **order_params
                        )
                        print(f"[ORDER RESPONSE] Attempt {attempt + 1}: {response}")

                        # If we got a response, break the retry loop
                        if response and isinstance(response, dict):
                            break

                    except Exception as api_error:
                        last_error = str(api_error)
                        logger.warning(f"[RETRY] Order placement attempt {attempt + 1}/{max_retries} failed: {last_error}")

                        if attempt < max_retries - 1:
                            import time as time_sleep
                            time_sleep.sleep(retry_delay)
                            retry_delay *= 2  # Exponential backoff
                        else:
                            logger.error(f"[RETRY EXHAUSTED] All {max_retries} attempts failed for {account_name}")
                            response = {'status': 'error', 'message': f'API error after {max_retries} retries: {last_error}'}

                # Handle case where response is None
                if not response:
                    response = {'status': 'error', 'message': f'No response from OpenAlgo API: {last_error}'}

                if response.get('status') == 'success':
                    order_id = response.get('orderid')

                    # PHASE 2: Save as 'pending' immediately, no blocking wait!
                    # Background poller will update status asynchronously
                    logger.debug(f"[ORDER PLACED] Order ID: {order_id} for {symbol} on {account_name} (will poll status)")

                    # IMPORTANT: Do NOT pre-set entry_price to limit_price
                    # The actual execution price may differ (e.g., LIMIT converts to MARKET)
                    # Always let the poller fetch the real average_price from broker
                    initial_entry_price = None

                    # Create execution record (already in app context from _execute_leg_parallel)
                    execution = StrategyExecution(
                        strategy_id=self.strategy.id,
                        account_id=account_id,
                        leg_id=leg.id,
                        order_id=order_id,
                        symbol=symbol,
                        exchange=exchange,
                        quantity=quantity,
                        product=self.strategy.product_order_type or 'MIS',  # MIS, NRML, CNC
                        status='pending',  # Will be updated by background poller
                        broker_order_status='open',  # Assume open until poller updates
                        entry_time=datetime.utcnow(),
                        entry_price=initial_entry_price  # Set to limit price for LIMIT orders, None for MARKET
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

                        # PHASE 2: Add order to background poller for status tracking
                        order_status_poller.add_order(
                            execution_id=execution.id,
                            account=account,
                            order_id=order_id,
                            strategy_name=self.strategy.name
                        )

                        # Report as pending - background poller will update status
                        results.append({
                            'account': account_name,
                            'symbol': symbol,
                            'order_id': order_id,
                            'status': 'pending',
                            'message': 'Order placed, checking status in background',
                            'order_status': 'open',
                            'leg': leg.leg_number
                        })

                        logger.debug(f"[THREAD SUCCESS] Leg {leg.leg_number} order placed on {account_name}, order_id: {order_id} (polling in background)")

                else:
                    # Order failed - create execution record for visibility and tracking
                    error_msg = response.get('message', 'Order placement failed')
                    logger.error(f"[THREAD FAILED] Leg {leg.leg_number} failed on {account_name}: {error_msg}")

                    # Create execution record for failed order (for tracking and visibility)
                    try:
                        failed_execution = StrategyExecution(
                            strategy_id=self.strategy.id,
                            account_id=account_id,
                            leg_id=leg.id,
                            order_id=None,  # No order ID since it failed
                            symbol=symbol,
                            exchange=exchange,
                            quantity=quantity,
                            status='failed',
                            broker_order_status='rejected',  # Mark as rejected since order didn't go through
                            entry_time=datetime.utcnow(),
                            entry_price=None,
                            error_message=error_msg[:500]  # Store error (truncate if too long)
                        )

                        with self.lock:
                            db.session.add(failed_execution)
                            # Try to commit, but don't fail if it doesn't work
                            try:
                                db.session.commit()
                                logger.debug(f"[FAILED ORDER TRACKED] Created execution record for failed order on {account_name}")
                            except Exception as commit_error:
                                logger.warning(f"Could not commit failed execution record: {commit_error}")
                                db.session.rollback()

                    except Exception as tracking_error:
                        logger.warning(f"Could not create tracking record for failed order: {tracking_error}")

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

        logger.debug(f"[THREAD END] Completed execution for leg {leg.leg_number} on account {account_name}")

    def _get_order_status(self, client: ExtendedOpenAlgoAPI, order_id: str, strategy_name: str) -> Dict:
        """Fetch order status from broker using OpenAlgo API"""
        try:
            response = client.orderstatus(
                order_id=order_id,
                strategy=strategy_name
            )

            if response.get('status') == 'success':
                data = response.get('data', {})
                logger.debug(f"Order {order_id} status: {data.get('order_status')} at price {data.get('price')}")
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
        logger.debug(f"Exit monitoring would start for execution {execution_id}")

    def _build_symbol(self, leg: StrategyLeg) -> str:
        """Build OpenAlgo symbol format based on leg configuration"""
        try:
            logger.debug(f"Building symbol for leg {leg.leg_number}: {leg.instrument} {leg.product_type} "
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
                logger.debug(f"Got strike price: {strike} for {leg.strike_selection} offset={leg.strike_offset}")

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
                logger.debug(f"Built option symbol: {symbol}")

            elif leg.product_type == 'futures':
                # Get expiry date
                logger.debug(f"[FUTURES] Getting expiry for {leg.instrument}, expiry_type={leg.expiry}")
                expiry = self._get_expiry_string(leg)

                if not expiry:
                    logger.error(f"[FUTURES] Failed to get expiry for futures leg {leg.leg_number}, instrument={leg.instrument}, expiry_type={leg.expiry}")
                    return ""

                # Build futures symbol: NIFTY30DEC25FUT
                symbol = f"{base_symbol}{expiry}FUT"
                logger.debug(f"[FUTURES] Built symbol: {symbol} (base={base_symbol}, expiry={expiry})")

            else:
                # Equity symbol
                symbol = base_symbol
                logger.debug(f"Using equity symbol: {symbol}")

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
                        """Parse expiry string like '10-JUL-25' or '10JUL25' to date"""
                        if not exp_str:
                            return dt.max
                        # Normalize to uppercase for consistent parsing
                        exp_upper = exp_str.upper().strip()
                        formats = ['%d-%b-%y', '%d%b%y', '%d-%B-%y', '%d%B%y', '%d-%b-%Y', '%d%b%Y']
                        for fmt in formats:
                            try:
                                return dt.strptime(exp_upper, fmt)
                            except ValueError:
                                continue
                        logger.warning(f"Could not parse expiry date: {exp_str}")
                        return dt.max

                    sorted_expiries = sorted(expiries, key=parse_expiry)
                    logger.debug(f"[EXPIRY] {leg.instrument} {leg.product_type} on {exchange}: Raw expiries from API: {expiries}")
                    logger.debug(f"[EXPIRY] {leg.instrument} {leg.product_type} on {exchange}: Sorted expiries ({len(sorted_expiries)} total): {sorted_expiries}")

                    # Select appropriate expiry based on leg configuration
                    selected_expiry = None
                    logger.debug(f"[EXPIRY] Selecting expiry for {leg.instrument} {leg.product_type}, expiry_type={leg.expiry}")

                    if leg.expiry == 'current_week':
                        # For OPTIONS: First expiry (nearest weekly)
                        # For FUTURES: No weekly expiries exist - use current month (index 0)
                        selected_expiry = sorted_expiries[0] if sorted_expiries else None
                        if leg.product_type == 'futures':
                            logger.debug(f"[EXPIRY] FUTURES current_week -> current_month (no weekly futures): {selected_expiry}")
                        else:
                            logger.debug(f"[EXPIRY] OPTIONS current_week: {selected_expiry}")

                    elif leg.expiry == 'next_week':
                        # For OPTIONS: Second expiry (next weekly)
                        # For FUTURES: No weekly expiries exist - use next month (index 1)
                        if leg.product_type == 'futures':
                            # Futures: next_week maps to next_month (index 1)
                            if len(sorted_expiries) > 1:
                                selected_expiry = sorted_expiries[1]
                            else:
                                selected_expiry = sorted_expiries[0] if sorted_expiries else None
                            logger.debug(f"[EXPIRY] FUTURES next_week -> next_month (no weekly futures): {selected_expiry}")
                        else:
                            # Options: second expiry
                            if len(sorted_expiries) > 1:
                                selected_expiry = sorted_expiries[1]
                            else:
                                selected_expiry = sorted_expiries[0] if sorted_expiries else None
                            logger.debug(f"[EXPIRY] OPTIONS next_week: {selected_expiry}")

                    elif leg.expiry == 'current_month':
                        # For FUTURES: Use index-based selection (first contract = current month)
                        # For OPTIONS: Find the MONTHLY expiry (last expiry of the month, not weekly)
                        if leg.product_type == 'futures':
                            # Futures typically have 3 contracts: current, next, far
                            # current_month = first expiry (index 0)
                            selected_expiry = sorted_expiries[0] if sorted_expiries else None
                            logger.debug(f"[EXPIRY] FUTURES current_month: using first expiry = {selected_expiry}")
                        else:
                            # OPTIONS: Find MONTHLY expiry (last expiry of the month)
                            # Monthly expiry is typically last Thu (NSE) or last Fri (BSE/SENSEX)
                            current_month = dt.now().month
                            current_year = dt.now().year
                            today = dt.now().date()
                            logger.debug(f"[EXPIRY] OPTIONS Looking for current_month MONTHLY expiry: month={current_month}, year={current_year}")

                            # Group expiries by month to find monthly (last of each month)
                            expiries_by_month = {}
                            for exp_str in sorted_expiries:
                                exp_date = parse_expiry(exp_str)
                                if exp_date == dt.max:
                                    continue
                                key = (exp_date.year, exp_date.month)
                                if key not in expiries_by_month:
                                    expiries_by_month[key] = []
                                expiries_by_month[key].append((exp_date, exp_str))

                            # Sort expiries within each month and get the LAST one (monthly)
                            monthly_expiries = {}
                            for key, month_exps in expiries_by_month.items():
                                sorted_month = sorted(month_exps, key=lambda x: x[0])
                                monthly_expiries[key] = sorted_month[-1]  # Last expiry of month

                            logger.debug(f"[EXPIRY] Monthly expiries: {monthly_expiries}")

                            # Look for current month's MONTHLY expiry
                            current_key = (current_year, current_month)
                            if current_key in monthly_expiries:
                                monthly_date, monthly_str = monthly_expiries[current_key]
                                # Only use if it's in the future (or today)
                                if monthly_date.date() >= today:
                                    selected_expiry = monthly_str
                                    logger.debug(f"[EXPIRY] Found current_month MONTHLY expiry: {selected_expiry}")

                            # If no current month monthly, or it's in the past, use next month's MONTHLY
                            if not selected_expiry:
                                next_month = (current_month % 12) + 1
                                next_year = current_year + 1 if next_month == 1 else current_year
                                next_key = (next_year, next_month)
                                if next_key in monthly_expiries:
                                    _, monthly_str = monthly_expiries[next_key]
                                    selected_expiry = monthly_str
                                    logger.debug(f"[EXPIRY] Current month passed, using next_month MONTHLY: {selected_expiry}")

                            # Final fallback: use first future monthly expiry
                            if not selected_expiry:
                                for key in sorted(monthly_expiries.keys()):
                                    exp_date, exp_str = monthly_expiries[key]
                                    if exp_date.date() >= today:
                                        selected_expiry = exp_str
                                        logger.debug(f"[EXPIRY] Fallback to first future MONTHLY: {selected_expiry}")
                                        break

                    elif leg.expiry == 'next_month':
                        # For FUTURES: Use index-based selection (second contract = next month)
                        # For OPTIONS: Find the MONTHLY expiry of next month (last expiry of next month)
                        if leg.product_type == 'futures':
                            # Futures typically have 3 contracts: current, next, far
                            # next_month = second expiry (index 1)
                            logger.debug(f"[EXPIRY] FUTURES next_month: {len(sorted_expiries)} expiries available: {sorted_expiries}")
                            if len(sorted_expiries) > 1:
                                selected_expiry = sorted_expiries[1]
                                logger.debug(f"[EXPIRY] FUTURES next_month: using index[1] = {selected_expiry}")
                            else:
                                selected_expiry = sorted_expiries[0] if sorted_expiries else None
                                logger.warning(f"[EXPIRY] FUTURES next_month: only {len(sorted_expiries)} expiry available, using index[0] = {selected_expiry}")
                        else:
                            # OPTIONS: Find MONTHLY expiry of next month (last expiry of month)
                            current_month = dt.now().month
                            current_year = dt.now().year
                            next_month = (current_month % 12) + 1
                            next_year = current_year + 1 if next_month == 1 else current_year
                            logger.debug(f"[EXPIRY] OPTIONS Looking for next_month MONTHLY expiry: month={next_month}, year={next_year}")

                            # Group expiries by month to find monthly (last of each month)
                            expiries_by_month = {}
                            for exp_str in sorted_expiries:
                                exp_date = parse_expiry(exp_str)
                                if exp_date == dt.max:
                                    continue
                                key = (exp_date.year, exp_date.month)
                                if key not in expiries_by_month:
                                    expiries_by_month[key] = []
                                expiries_by_month[key].append((exp_date, exp_str))

                            # Sort expiries within each month and get the LAST one (monthly)
                            monthly_expiries = {}
                            for key, month_exps in expiries_by_month.items():
                                sorted_month = sorted(month_exps, key=lambda x: x[0])
                                monthly_expiries[key] = sorted_month[-1]  # Last expiry of month

                            # Look for next month's MONTHLY expiry
                            next_key = (next_year, next_month)
                            if next_key in monthly_expiries:
                                _, monthly_str = monthly_expiries[next_key]
                                selected_expiry = monthly_str
                                logger.debug(f"[EXPIRY] Found next_month MONTHLY expiry: {selected_expiry}")

                            # If no next month monthly found, look for the month after
                            if not selected_expiry:
                                month_after = (next_month % 12) + 1
                                year_after = next_year + 1 if month_after == 1 else next_year
                                after_key = (year_after, month_after)
                                if after_key in monthly_expiries:
                                    _, monthly_str = monthly_expiries[after_key]
                                    selected_expiry = monthly_str
                                    logger.debug(f"[EXPIRY] Using month after next MONTHLY: {selected_expiry}")

                            # Final fallback: use first available MONTHLY after current month
                            if not selected_expiry:
                                logger.warning(f"[EXPIRY] No exact next_month MONTHLY, looking for next available")
                                for key in sorted(monthly_expiries.keys()):
                                    exp_date, exp_str = monthly_expiries[key]
                                    if key > (current_year, current_month):
                                        selected_expiry = exp_str
                                        logger.debug(f"[EXPIRY] Using next available MONTHLY: {selected_expiry}")
                                        break

                    if selected_expiry:
                        # Convert to OpenAlgo format (e.g., '10-JUL-25' to '10JUL25')
                        # Ensure uppercase for consistency (e.g., '10JUL25' not '10jul25')
                        formatted_expiry = selected_expiry.replace('-', '').upper()

                        # Cache the result
                        self.expiry_cache[cache_key] = {
                            'expiry': formatted_expiry,
                            'timestamp': dt.utcnow()
                        }

                        logger.debug(f"[EXPIRY] {leg.instrument} {leg.expiry} -> {selected_expiry} -> {formatted_expiry}")
                        return formatted_expiry
                    else:
                        logger.error(f"[EXPIRY] Could not determine expiry for {leg.instrument} {leg.expiry}. Available: {sorted_expiries}")
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
                logger.debug(f"Strike selection: {leg.strike_selection}, Offset from DB: {leg.strike_offset}, Using offset: {offset}")

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
                logger.debug(f"{leg.instrument} {leg.option_type}: Spot={spot_price}, ATM={atm_strike}, "
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
                    logger.debug(f"Using WebSocket price for {instrument}: {price_info['ltp']}")
                    return price_info['ltp']

            # Check option chain service for cached underlying price
            underlying_key = f"{instrument}_spot"
            if underlying_key in option_chain_service.active_managers:
                manager = option_chain_service.active_managers[underlying_key]
                if manager and manager.underlying_ltp > 0:
                    logger.debug(f"Using option chain price for {instrument}: {manager.underlying_ltp}")
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
                    logger.debug(f"Using API price for {instrument}: {ltp}")
                    return ltp
                else:
                    logger.error(f"API quote failed for {instrument}: {response.get('message')}")

        except Exception as e:
            logger.error(f"Error getting spot price for {instrument}: {e}")

        return 0

    def _find_strike_by_premium(self, leg: StrategyLeg, atm_strike: int, strike_step: int) -> str:
        """Find strike with premium closest to target value"""
        try:
            from datetime import datetime, time as dt_time

            # INFO: Check if market is open
            # Indian market hours: 9:15 AM - 3:30 PM IST
            current_time = datetime.now().time()
            market_open = dt_time(9, 15)
            market_close = dt_time(15, 30)

            if not (market_open <= current_time <= market_close):
                logger.debug(f"[PREMIUM INFO] Executing after market hours ({current_time.strftime('%H:%M')})")
                logger.debug(f"[PREMIUM INFO] Using closing prices - illiquid OTM strikes may have stale LTP")
                logger.debug(f"[PREMIUM INFO] For best results, execute premium-based strategies during market hours")

            target_premium = leg.premium_value if leg.premium_value else 50

            # IMPROVED: Collect ALL premiums first, then find best match
            # This ensures we don't miss better options due to search order
            strikes_checked = 0
            strikes_with_data = 0
            strikes_no_data = []  # Track strikes with no data
            all_premiums = []  # Collect ALL valid premiums

            logger.debug(f"[PREMIUM SEARCH] Target premium: {target_premium}, ATM Strike: {atm_strike}, Strike step: {strike_step}")

            # PHASE 1: Collect all premium data (don't select yet)
            # Range: ±20 strikes (NIFTY: ATM ± 1000 points | BANKNIFTY: ATM ± 2000 points)
            for i in range(-20, 21):  # ±20 strikes = 41 total strikes to check
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
                    strikes_checked += 1

                    try:
                        # Retry failed API calls up to 2 times
                        max_retries = 2
                        response = None

                        for retry in range(max_retries):
                            try:
                                response = client.quotes(symbol=symbol, exchange=exchange)
                                if response and response.get('status') == 'success':
                                    break  # Success, exit retry loop
                                elif retry < max_retries - 1:
                                    sleep(0.1)  # Brief pause before retry
                            except Exception as retry_error:
                                if retry < max_retries - 1:
                                    sleep(0.1)
                                else:
                                    raise  # Re-raise on final attempt

                        if response and response.get('status') == 'success':
                            premium = response.get('data', {}).get('ltp', 0)

                            # Only consider strikes with premium > 0 (valid trading data)
                            if premium > 0:
                                strikes_with_data += 1
                                diff = abs(premium - target_premium)

                                # Store all valid premiums for analysis
                                all_premiums.append({
                                    'strike': strike,
                                    'premium': premium,
                                    'diff': diff,
                                    'direction': 'OVER' if premium > target_premium else 'UNDER' if premium < target_premium else 'EXACT'
                                })

                                logger.debug(f"[PREMIUM] Strike {strike}: Premium={premium:.2f}, Diff={diff:.2f}")
                            else:
                                strikes_no_data.append(strike)
                                logger.debug(f"[PREMIUM] Strike {strike}: No premium data (LTP=0)")
                        else:
                            strikes_no_data.append(strike)
                            logger.debug(f"[PREMIUM] API call failed for strike {strike}: {response.get('message', 'Unknown error') if response else 'No response'}")

                    except Exception as api_error:
                        strikes_no_data.append(strike)
                        logger.debug(f"[PREMIUM] Exception fetching premium for strike {strike}: {api_error}")
                        continue

            # PHASE 2: Find the best match from collected data
            if not all_premiums:
                logger.error(f"[PREMIUM ERROR] No valid premium data found!")
                return str(atm_strike)

            # Sort by difference (ascending), then prefer UNDER target for ties
            all_premiums.sort(key=lambda x: (x['diff'], 1 if x['direction'] == 'OVER' else 0))

            # Select the best match
            best_match = all_premiums[0]
            best_strike = best_match['strike']
            best_premium = best_match['premium']
            best_diff = best_match['diff']

            # VALIDATION: Check if the found premium is acceptable
            percent_diff = abs(best_premium - target_premium) / target_premium * 100

            logger.debug(f"[PREMIUM SEARCH RESULT] Checked {strikes_checked} strikes, found {strikes_with_data} with valid data")

            # Log strikes with no data if significant number missing
            if strikes_no_data:
                logger.debug(f"[PREMIUM SEARCH RESULT] {len(strikes_no_data)} strikes had no data: {strikes_no_data[:10]}" +
                           (f"... and {len(strikes_no_data)-10} more" if len(strikes_no_data) > 10 else ""))

            logger.debug(f"[PREMIUM SEARCH RESULT] ⭐ SELECTED ⭐")
            logger.debug(f"[PREMIUM SEARCH RESULT] Target: {target_premium} → Found: {best_premium} at strike {best_strike}")
            logger.debug(f"[PREMIUM SEARCH RESULT] Direction: {best_match['direction']} target by {best_diff:.2f} ({percent_diff:.1f}%)")

            # Create a visual premium distribution map
            if all_premiums and len(all_premiums) > 1:
                logger.debug(f"[PREMIUM MAP] Distribution of {len(all_premiums)} valid premiums:")

                # Group premiums by ranges for visual clarity
                ranges = [
                    (0, target_premium * 0.5, "FAR BELOW"),
                    (target_premium * 0.5, target_premium * 0.9, "BELOW"),
                    (target_premium * 0.9, target_premium * 1.1, "NEAR TARGET"),
                    (target_premium * 1.1, target_premium * 1.5, "ABOVE"),
                    (target_premium * 1.5, float('inf'), "FAR ABOVE")
                ]

                for low, high, label in ranges:
                    in_range = [p for p in all_premiums if low <= p['premium'] < high]
                    if in_range:
                        count = len(in_range)
                        strikes_str = ', '.join([str(p['strike']) for p in in_range[:5]])
                        if len(in_range) > 5:
                            strikes_str += f" ... (+{len(in_range)-5} more)"
                        logger.debug(f"  {label}: {count} strikes - {strikes_str}")

                # Show top 15 closest matches
                logger.debug(f"[PREMIUM] Top 15 closest matches to target {target_premium}:")
                for i, match in enumerate(all_premiums[:15], 1):
                    marker = " ← SELECTED" if match['strike'] == best_strike else ""
                    logger.debug(f"  {i:2d}. Strike {match['strike']:5d}: Premium {match['premium']:7.2f}, Diff {match['diff']:6.2f} ({match['direction']:5s}){marker}")

            # WARNING: If difference is too large, log warning with threshold based on target
            # For small premiums (<50): warn if >20% away
            # For medium premiums (50-100): warn if >15% away
            # For large premiums (>100): warn if >10% away
            if target_premium < 50:
                warning_threshold = 20
            elif target_premium < 100:
                warning_threshold = 15
            else:
                warning_threshold = 10

            if percent_diff > warning_threshold:
                logger.warning(f"[PREMIUM WARNING] Best match is {percent_diff:.1f}% away from target (threshold: {warning_threshold}%)")
                logger.warning(f"[PREMIUM WARNING] Found premium {best_premium} vs target {target_premium}")
                logger.warning(f"[PREMIUM WARNING] Consider expanding search range or adjusting target premium")

                # Suggest possible reasons
                if len(strikes_no_data) > 10:
                    logger.warning(f"[PREMIUM WARNING] Many strikes ({len(strikes_no_data)}) had no data - try during market hours")
                if strikes_with_data < 10:
                    logger.warning(f"[PREMIUM WARNING] Only {strikes_with_data} strikes with valid data - limited options available")

            return str(best_strike)

        except Exception as e:
            logger.error(f"Error finding strike by premium: {e}", exc_info=True)
            return str(atm_strike)

    def _calculate_quantity(self, leg: StrategyLeg, num_accounts: int, account: TradingAccount = None) -> int:
        """Calculate quantity per account based on allocation type and available margin"""
        logger.debug(f"[QTY CALC DEBUG] Starting quantity calculation for leg {leg.leg_number}, account: {account.account_name if account else 'None'}")
        logger.debug(f"[QTY CALC DEBUG] use_margin_calculator: {self.use_margin_calculator}, num_accounts: {num_accounts}")

        # Get lot size for the instrument
        lot_size = self._get_lot_size(leg)
        logger.debug(f"[QTY CALC DEBUG] Lot size for {leg.instrument}: {lot_size}")

        # Check for pre-calculated quantity (for straddles, strangles, spreads)
        if account and self.use_margin_calculator:
            key = f"{leg.id}_{account.id}"
            if key in self.pre_calculated_quantities:
                pre_calc_qty = self.pre_calculated_quantities[key]
                logger.debug(f"[QTY CALC DEBUG] Using pre-calculated quantity for leg {leg.leg_number}: {pre_calc_qty}")
                return pre_calc_qty

        # SEQUENTIAL EXECUTION FIX: For BUY legs, match quantity with executed SELL leg
        # This ensures BUY CE matches SELL CE qty, and BUY PE matches SELL PE qty
        if account and self.use_margin_calculator and leg.action == 'BUY' and leg.product_type == 'options':
            executed_sell_qty = self._get_executed_sell_leg_quantity(leg, account)
            if executed_sell_qty:
                logger.debug(f"[SEQ EXEC] BUY {leg.option_type} using executed SELL quantity: {executed_sell_qty}")
                return executed_sell_qty

        # If margin calculator is enabled and account provided, calculate based on margin
        if self.use_margin_calculator and self.margin_calculator and account:
            logger.debug(f"[QTY CALC DEBUG] Using margin calculator for {account.account_name}")

            # Determine trade type for margin calculation
            trade_type = self._get_trade_type_for_margin(leg)
            logger.debug(f"[QTY CALC DEBUG] Trade type: {trade_type}")

            # Special case for option buying
            if trade_type == 'buy':
                # Check if this BUY leg is part of a spread (has corresponding SELL leg)
                is_part_of_spread = self._is_buy_part_of_spread(leg)

                if is_part_of_spread:
                    # For spreads, use margin calculation based on SELL leg's margin
                    # This ensures both BUY and SELL legs have the same quantity
                    logger.debug(f"[QTY CALC DEBUG] Option buying is part of spread - using SELL leg margin calculation")
                    # Use 'sell_c_p' margin to calculate quantity (same as corresponding SELL leg)
                    trade_type = 'sell_c_p'
                elif self.margin_source == 'cash':
                    # Option Buyer grade selected: Calculate lots based on cash margin and premium per lot
                    logger.debug(f"[QTY CALC DEBUG] Option Buyer grade - calculating lots from cash margin and premium")

                    # Get cash margin for the account
                    if account.id not in self.account_margins:
                        self.account_margins[account.id] = self._get_margin_for_account(account)
                    cash_margin = self.account_margins[account.id]

                    # Get option buying premium per lot from margin calculator
                    premium_per_lot = self.margin_calculator.get_option_buying_premium(leg.instrument)

                    # Calculate: effective_margin / premium_per_lot
                    effective_margin = cash_margin * self.margin_percentage
                    raw_lots = effective_margin / premium_per_lot if premium_per_lot > 0 else 0
                    optimal_lots = int(raw_lots)

                    logger.debug(f"[QTY CALC DEBUG] Cash margin: {cash_margin:,.2f}, Margin %: {self.margin_percentage*100}%")
                    logger.debug(f"[QTY CALC DEBUG] Effective margin: {effective_margin:,.2f}, Premium/lot: {premium_per_lot:,.2f}")
                    logger.debug(f"[QTY CALC DEBUG] Raw lots: {raw_lots:.3f}, Optimal lots: {optimal_lots}")

                    if optimal_lots > 0:
                        # Update remaining margin for next trades
                        margin_used = optimal_lots * premium_per_lot
                        self.account_margins[account.id] -= margin_used
                        total_quantity = optimal_lots * lot_size
                        logger.debug(f"[QTY CALC DEBUG] Option buying: {optimal_lots} lots × {lot_size} = {total_quantity} qty")
                        return total_quantity
                    else:
                        logger.warning(f"[QTY CALC DEBUG] Insufficient cash margin for option buying")
                        return 0
                else:
                    # Standalone option buying with Option Seller grade - no margin blocked, use leg's configured lots
                    logger.debug(f"[QTY CALC DEBUG] Standalone option buying (seller grade) - using leg's configured lots")
                    if leg.lots and leg.lots > 0:
                        total_quantity = leg.lots * lot_size
                    elif leg.quantity and leg.quantity > 0:
                        total_quantity = leg.quantity
                    else:
                        total_quantity = lot_size  # Default to 1 lot

                    logger.debug(f"[QTY CALC DEBUG] Option buying quantity for {account.account_name}: {total_quantity} (lots={leg.lots}, lot_size={lot_size})")
                    return total_quantity

            # Get margin for the account (cash or available based on margin_source)
            if account.id not in self.account_margins:
                logger.debug(f"[QTY CALC DEBUG] Fetching fresh margin for account {account.id}")
                self.account_margins[account.id] = self._get_margin_for_account(account)
            else:
                logger.debug(f"[QTY CALC DEBUG] Using cached margin for account {account.id}")

            available_margin = self.account_margins[account.id]
            margin_type = "cash" if self.margin_source == 'cash' else "available"
            logger.debug(f"[QTY CALC DEBUG] {margin_type.title()} margin for {account.account_name}: {available_margin:,.2f}")
            logger.debug(f"[QTY CALC DEBUG] Risk profile: {self.strategy.risk_profile}, Margin %: {self.margin_percentage*100}%")

            # Calculate optimal lot size based on margin with custom percentage
            # Pass is_expiry based on strategy's market_condition setting
            # Pass margin_source to determine option buyer vs seller calculation
            optimal_lots, details = self.margin_calculator.calculate_lot_size_custom(
                account=account,
                instrument=leg.instrument,
                trade_type=trade_type,
                margin_percentage=self.margin_percentage,
                available_margin=available_margin,
                is_expiry=self.is_expiry_override,
                margin_source=self.margin_source
            )

            logger.debug(f"[QTY CALC DEBUG] Calculated optimal lots: {optimal_lots}")
            logger.debug(f"[QTY CALC DEBUG] Calculation details: {details}")

            if optimal_lots > 0:
                # Update remaining margin for next trades
                margin_used = optimal_lots * details.get('margin_per_lot', 0)
                self.account_margins[account.id] -= margin_used

                # Convert lots to quantity
                total_quantity = optimal_lots * lot_size

                logger.debug(f"[QTY CALC DEBUG] Margin used: ₹{margin_used:,.2f}, Remaining margin: ₹{self.account_margins[account.id]:,.2f}")
                logger.debug(f"[QTY CALC DEBUG] Final quantity: {optimal_lots} lots × {lot_size} = {total_quantity}")
                logger.debug(f"Margin-based calculation for {account.account_name}: "
                           f"{optimal_lots} lots = {total_quantity} qty "
                           f"(Margin: {available_margin:.2f} -> {self.account_margins[account.id]:.2f})")

                return total_quantity
            else:
                logger.warning(f"[QTY CALC DEBUG] Insufficient margin! optimal_lots={optimal_lots}")
                logger.warning(f"Insufficient margin for {leg.instrument} on {account.account_name}")
                return 0

        # Fallback to original calculation if margin calculator not used
        # IMPORTANT: Always recalculate from lots to use current lot size
        # (lot sizes may differ for current vs next month contracts)
        if leg.lots and leg.lots > 0:
            # Calculate from lots using current lot size
            total_quantity = leg.lots * lot_size
        elif leg.quantity and leg.quantity > 0:
            # Fallback to stored quantity only if no lots defined
            total_quantity = leg.quantity
        else:
            # Default to 1 lot
            total_quantity = lot_size

        logger.debug(f"Leg {leg.leg_number}: Lots={leg.lots}, Quantity={leg.quantity}, "
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

        logger.debug(f"Quantity per account: {quantity_per_account} for {num_accounts} accounts")
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
        """Check if current leg is part of a spread strategy (CE+PE SELL pairs - straddle/strangle)"""
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

    def _is_buy_part_of_spread(self, current_leg: StrategyLeg) -> bool:
        """
        Check if a BUY leg is part of a spread (has corresponding SELL leg).
        This includes:
        - Bull Call Spread (BUY CE + SELL CE)
        - Bear Put Spread (BUY PE + SELL PE)
        - Any spread where there's a matching SELL leg for the same instrument
        """
        if current_leg.action != 'BUY':
            return False

        all_legs = self.strategy.legs.all()
        for other_leg in all_legs:
            if (other_leg.instrument == current_leg.instrument and
                other_leg.product_type == 'options' and
                other_leg.action == 'SELL' and
                other_leg.id != current_leg.id):
                # Found a SELL leg for the same instrument - this is a spread
                logger.debug(f"[SPREAD DETECT] BUY leg {current_leg.option_type} has matching SELL leg {other_leg.option_type}")
                return True
        return False

    def _get_executed_sell_leg_quantity(self, buy_leg: StrategyLeg, account: TradingAccount) -> Optional[int]:
        """
        Look up the quantity of an already-executed SELL leg that corresponds to this BUY leg.
        Used for sequential execution to ensure BUY legs match SELL leg quantities.

        For Iron Condor/Butterfly strategies:
        - BUY CE should match the executed SELL CE quantity
        - BUY PE should match the executed SELL PE quantity

        Returns:
            The quantity of the matching executed SELL leg, or None if not found
        """
        if buy_leg.action != 'BUY':
            return None

        # Find corresponding SELL leg in strategy (same instrument and option type)
        all_legs = self.strategy.legs.all()
        matching_sell_leg = None

        for other_leg in all_legs:
            if (other_leg.instrument == buy_leg.instrument and
                other_leg.product_type == 'options' and
                other_leg.action == 'SELL' and
                other_leg.option_type == buy_leg.option_type and  # Same option type (CE matches CE, PE matches PE)
                other_leg.id != buy_leg.id):
                matching_sell_leg = other_leg
                break

        if not matching_sell_leg:
            logger.debug(f"[SEQ EXEC] No matching SELL {buy_leg.option_type} leg found for BUY {buy_leg.option_type}")
            return None

        # Look up the executed quantity for this SELL leg from StrategyExecution
        executed = StrategyExecution.query.filter_by(
            strategy_id=self.strategy.id,
            account_id=account.id,
            leg_id=matching_sell_leg.id
        ).filter(
            StrategyExecution.status.in_(['pending', 'entered', 'success'])
        ).first()

        if executed and executed.quantity:
            logger.debug(f"[SEQ EXEC] Found executed SELL {matching_sell_leg.option_type} leg with qty={executed.quantity}, "
                       f"BUY {buy_leg.option_type} will use same quantity")
            return executed.quantity

        logger.debug(f"[SEQ EXEC] SELL {matching_sell_leg.option_type} leg not yet executed for account {account.account_name}")
        return None

    def _pre_calculate_multi_leg_quantities(self, legs: List[StrategyLeg]):
        """
        Pre-calculate quantities for multi-leg strategies (straddles, strangles, spreads).
        This ensures all related legs get the same quantity.

        Strategy types handled:
        - Straddle/Strangle: SELL CE + SELL PE (same instrument) -> use 'sell_c_and_p' margin once
        - Spread: BUY CE + SELL CE or BUY PE + SELL PE -> use 'sell_c_p' margin once
        """
        logger.debug(f"[PRE-CALC] Starting pre-calculation for {len(legs)} legs across {len(self.accounts)} accounts")

        # Group legs by instrument and identify patterns
        instrument_legs = {}
        for leg in legs:
            if leg.instrument not in instrument_legs:
                instrument_legs[leg.instrument] = []
            instrument_legs[leg.instrument].append(leg)

        # Process each instrument group
        for instrument, inst_legs in instrument_legs.items():
            # Get option legs only
            option_legs = [l for l in inst_legs if l.product_type == 'options']

            if len(option_legs) < 2:
                continue

            # Check for straddle/strangle pattern (SELL CE + SELL PE)
            sell_legs = [l for l in option_legs if l.action == 'SELL']
            sell_ce_legs = [l for l in sell_legs if l.option_type == 'CE']
            sell_pe_legs = [l for l in sell_legs if l.option_type == 'PE']

            if sell_ce_legs and sell_pe_legs:
                # This is a straddle/strangle
                logger.debug(f"[PRE-CALC] Detected straddle/strangle for {instrument}: "
                           f"{len(sell_ce_legs)} SELL CE + {len(sell_pe_legs)} SELL PE")

                # Calculate quantity once using 'sell_c_and_p' margin for all accounts
                for account in self.accounts:
                    self._pre_calculate_straddle_quantity(
                        instrument,
                        sell_ce_legs + sell_pe_legs,
                        account
                    )

                # Also handle any BUY legs that are part of spreads with these SELL legs
                buy_legs = [l for l in option_legs if l.action == 'BUY']
                for buy_leg in buy_legs:
                    # Find matching SELL leg (same option type)
                    matching_sells = [s for s in sell_legs if s.option_type == buy_leg.option_type]
                    if matching_sells:
                        # BUY leg is part of spread, should use same quantity as SELL leg
                        for account in self.accounts:
                            key = f"{matching_sells[0].id}_{account.id}"
                            if key in self.pre_calculated_quantities:
                                buy_key = f"{buy_leg.id}_{account.id}"
                                self.pre_calculated_quantities[buy_key] = self.pre_calculated_quantities[key]
                                logger.debug(f"[PRE-CALC] Spread BUY leg {buy_leg.id} assigned quantity "
                                           f"{self.pre_calculated_quantities[buy_key]} from SELL leg {matching_sells[0].id}")
            else:
                # Check for simple spreads (BUY + SELL of same type)
                for option_type in ['CE', 'PE']:
                    type_legs = [l for l in option_legs if l.option_type == option_type]
                    buy_legs = [l for l in type_legs if l.action == 'BUY']
                    sell_legs = [l for l in type_legs if l.action == 'SELL']

                    if buy_legs and sell_legs:
                        # This is a spread (e.g., Bull Call Spread, Bear Put Spread)
                        logger.debug(f"[PRE-CALC] Detected {option_type} spread for {instrument}: "
                                   f"{len(buy_legs)} BUY + {len(sell_legs)} SELL")

                        # Calculate quantity using 'sell_c_p' margin for the SELL leg
                        for account in self.accounts:
                            self._pre_calculate_spread_quantity(
                                instrument,
                                buy_legs + sell_legs,
                                account
                            )

        logger.debug(f"[PRE-CALC] Pre-calculation complete. {len(self.pre_calculated_quantities)} quantities stored")

    def _pre_calculate_straddle_quantity(self, instrument: str, sell_legs: List[StrategyLeg],
                                          account: TradingAccount):
        """
        Calculate quantity once for a straddle/strangle and assign to all SELL legs.
        Uses 'sell_c_and_p' margin which covers BOTH legs together.
        """
        if not self.margin_calculator:
            return

        lot_size = self._get_lot_size(sell_legs[0])

        # Get margin for the account (cash or available based on margin_source)
        if account.id not in self.account_margins:
            self.account_margins[account.id] = self._get_margin_for_account(account)

        available_margin = self.account_margins[account.id]

        # Calculate using 'sell_c_and_p' margin (covers both legs)
        # Pass is_expiry based on strategy's market_condition setting for consistency
        # Pass margin_source to determine option buyer vs seller calculation
        optimal_lots, details = self.margin_calculator.calculate_lot_size_custom(
            account=account,
            instrument=instrument,
            trade_type='sell_c_and_p',  # Combined margin for CE+PE
            margin_percentage=self.margin_percentage,
            available_margin=available_margin,
            is_expiry=self.is_expiry_override,
            margin_source=self.margin_source
        )

        if optimal_lots > 0:
            # Convert lots to quantity
            total_quantity = optimal_lots * lot_size

            # Update margin used (only once for the entire straddle)
            margin_used = optimal_lots * details.get('margin_per_lot', 0)
            self.account_margins[account.id] -= margin_used

            # Store quantity for ALL legs in the straddle
            for leg in sell_legs:
                key = f"{leg.id}_{account.id}"
                self.pre_calculated_quantities[key] = total_quantity
                logger.debug(f"[PRE-CALC] Straddle leg {leg.id} ({leg.option_type}) for {account.account_name}: "
                           f"{optimal_lots} lots = {total_quantity} qty")

            logger.debug(f"[PRE-CALC] Straddle margin calculation for {account.account_name}: "
                       f"Margin used: {margin_used:,.2f}, Remaining: {self.account_margins[account.id]:,.2f}")
        else:
            # Store 0 quantity for insufficient margin
            for leg in sell_legs:
                key = f"{leg.id}_{account.id}"
                self.pre_calculated_quantities[key] = 0
            logger.warning(f"[PRE-CALC] Insufficient margin for straddle on {account.account_name}")

    def _pre_calculate_spread_quantity(self, instrument: str, spread_legs: List[StrategyLeg],
                                        account: TradingAccount):
        """
        Calculate quantity once for a spread (BUY + SELL) and assign to all legs.
        Uses 'sell_c_p' margin for the SELL leg.
        """
        if not self.margin_calculator:
            return

        lot_size = self._get_lot_size(spread_legs[0])

        # Get margin for the account (cash or available based on margin_source)
        if account.id not in self.account_margins:
            self.account_margins[account.id] = self._get_margin_for_account(account)

        available_margin = self.account_margins[account.id]

        # Calculate using 'sell_c_p' margin (single option selling)
        # Pass is_expiry based on strategy's market_condition setting for consistency
        # Pass margin_source to determine option buyer vs seller calculation
        optimal_lots, details = self.margin_calculator.calculate_lot_size_custom(
            account=account,
            instrument=instrument,
            trade_type='sell_c_p',
            margin_percentage=self.margin_percentage,
            available_margin=available_margin,
            is_expiry=self.is_expiry_override,
            margin_source=self.margin_source
        )

        if optimal_lots > 0:
            # Convert lots to quantity
            total_quantity = optimal_lots * lot_size

            # Update margin used
            margin_used = optimal_lots * details.get('margin_per_lot', 0)
            self.account_margins[account.id] -= margin_used

            # Store quantity for ALL legs in the spread
            for leg in spread_legs:
                key = f"{leg.id}_{account.id}"
                self.pre_calculated_quantities[key] = total_quantity
                logger.debug(f"[PRE-CALC] Spread leg {leg.id} ({leg.action} {leg.option_type}) for {account.account_name}: "
                           f"{optimal_lots} lots = {total_quantity} qty")

            logger.debug(f"[PRE-CALC] Spread margin calculation for {account.account_name}: "
                       f"Margin used: {margin_used:,.2f}, Remaining: {self.account_margins[account.id]:,.2f}")
        else:
            # Store 0 quantity for insufficient margin
            for leg in spread_legs:
                key = f"{leg.id}_{account.id}"
                self.pre_calculated_quantities[key] = 0
            logger.warning(f"[PRE-CALC] Insufficient margin for spread on {account.account_name}")

    def _get_lot_size(self, leg: StrategyLeg) -> int:
        """Get lot size for instrument from database based on expiry type"""
        from app.models import TradingSettings

        # Determine if this is a next month contract
        # Only 'next_month' uses the new lot size, 'next_week' still uses current lot size
        is_next_month = leg.expiry == 'next_month' if leg.expiry else False

        # Try to get lot size from user's trading settings
        if self.strategy.user_id:
            setting = TradingSettings.query.filter_by(
                user_id=self.strategy.user_id,
                symbol=leg.instrument,
                is_active=True
            ).first()

            if setting:
                # Use next_month_lot_size if available and expiry is next month
                if is_next_month and setting.next_month_lot_size:
                    logger.debug(f"Using next_month_lot_size {setting.next_month_lot_size} for {leg.instrument} (expiry={leg.expiry})")
                    return setting.next_month_lot_size
                else:
                    logger.debug(f"Using lot_size {setting.lot_size} for {leg.instrument} (expiry={leg.expiry})")
                    return setting.lot_size

        # Fallback to defaults if not found (shouldn't happen if settings are initialized)
        default_lot_sizes = {
            'NIFTY': 75,
            'BANKNIFTY': 30,
            'FINNIFTY': 25,
            'MIDCPNIFTY': 50,
            'SENSEX': 10,
            'BANKEX': 15
        }
        lot_size = default_lot_sizes.get(leg.instrument, 75)
        logger.warning(f"Using default lot size {lot_size} for {leg.instrument}")
        return lot_size

    def _start_exit_monitoring(self, execution: StrategyExecution):
        """Start monitoring position for exit conditions using WebSocket data"""
        # Subscribe to WebSocket for real-time price updates
        self._subscribe_to_websocket(execution.symbol, execution.exchange)

        # Start monitoring thread - pass execution ID to avoid session issues
        thread = threading.Thread(target=self._monitor_exit_conditions, args=(execution.id,), daemon=True)
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
                    logger.debug(f"Using existing WebSocket manager for {underlying}")
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

                logger.debug(f"Subscribed to WebSocket for {symbol}")
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

            logger.debug(f"[MONITOR_START] Starting monitoring for execution ID={execution.id}, symbol={execution.symbol}")

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
                        strategy=self.strategy.name
                    )
                    if order_response.get('status') == 'success':
                        avg_price = order_response.get('data', {}).get('average_price')

                        # If average_price is missing/zero, wait 3 seconds and re-fetch
                        # Some brokers return complete status before average_price is populated
                        if not avg_price or avg_price == 0:
                            logger.warning(f"[MONITOR] Entry price missing for {symbol}, waiting 3s to re-fetch...")
                            time_module.sleep(3)

                            retry_response = client.orderstatus(
                                order_id=execution.order_id,
                                strategy=self.strategy.name
                            )
                            if retry_response.get('status') == 'success':
                                retry_avg_price = retry_response.get('data', {}).get('average_price')
                                if retry_avg_price and retry_avg_price > 0:
                                    avg_price = retry_avg_price
                                    logger.debug(f"[MONITOR] Entry price after retry: {avg_price} for {symbol}")

                        # Only update if we have a valid price
                        if avg_price and avg_price > 0:
                            execution.entry_price = avg_price
                            db.session.commit()
                            logger.debug(f"[MONITOR] Fetched entry price: {execution.entry_price} for {symbol}")
                        else:
                            logger.warning(f"[MONITOR] Could not fetch valid entry price for {symbol}")

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
                            logger.debug(f"Square off time reached for {symbol}")
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
                    logger.debug(f"[EXIT_TRIGGER] Stop loss hit! P&L={pnl:.2f} >= SL={leg.stop_loss_value}")
                    return True
            elif leg.stop_loss_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                sl_threshold = entry_value * leg.stop_loss_value / 100
                logger.debug(f"[EXIT_CHECK] SL Type=percentage, SL%={leg.stop_loss_value}, Threshold={sl_threshold:.2f}, abs(P&L)={abs(pnl):.2f}")
                if abs(pnl) >= sl_threshold:
                    logger.debug(f"[EXIT_TRIGGER] Stop loss hit! abs(P&L)={abs(pnl):.2f} >= Threshold={sl_threshold:.2f}")
                    return True

        # Check take profit
        if leg.take_profit_value:
            if leg.take_profit_type == 'points':
                logger.debug(f"[EXIT_CHECK] TP Type=points, TP Value={leg.take_profit_value}, P&L={pnl:.2f}")
                if pnl >= leg.take_profit_value:
                    logger.debug(f"[EXIT_TRIGGER] Take profit hit! P&L={pnl:.2f} >= TP={leg.take_profit_value}")
                    return True
            elif leg.take_profit_type == 'percentage':
                entry_value = execution.entry_price * execution.quantity
                tp_threshold = entry_value * leg.take_profit_value / 100
                logger.debug(f"[EXIT_CHECK] TP Type=percentage, TP%={leg.take_profit_value}, Threshold={tp_threshold:.2f}, P&L={pnl:.2f}")
                if pnl >= tp_threshold:
                    logger.debug(f"[EXIT_TRIGGER] Take profit hit! P&L={pnl:.2f} >= Threshold={tp_threshold:.2f}")
                    return True

        # Check max loss/profit at strategy level
        if self.strategy.max_loss:
            total_pnl = self._get_strategy_pnl()
            logger.debug(f"[EXIT_CHECK] Strategy Max Loss={self.strategy.max_loss}, Total P&L={total_pnl:.2f}")
            if abs(total_pnl) >= self.strategy.max_loss:
                logger.debug(f"[EXIT_TRIGGER] Strategy max loss hit! Total P&L={total_pnl:.2f}")
                return True

        if self.strategy.max_profit:
            total_pnl = self._get_strategy_pnl()
            logger.debug(f"[EXIT_CHECK] Strategy Max Profit={self.strategy.max_profit}, Total P&L={total_pnl:.2f}")
            if total_pnl >= self.strategy.max_profit:
                logger.debug(f"[EXIT_TRIGGER] Strategy max profit hit! Total P&L={total_pnl:.2f}")
                return True

        logger.debug(f"[EXIT_CHECK] No exit conditions met")
        return False

    def _exit_position(self, execution: StrategyExecution, client: ExtendedOpenAlgoAPI,
                      reason: str = 'exit_condition'):
        """Exit a position"""
        try:
            # Reverse the action
            exit_action = 'SELL' if execution.leg.action == 'BUY' else 'BUY'

            # Use freeze-aware order placement for exit orders
            from app.utils.freeze_quantity_handler import place_order_with_freeze_check

            response = place_order_with_freeze_check(
                client=client,
                user_id=self.strategy.user_id,
                strategy=self.strategy.name,
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

                # Fetch exit order details to get executed price
                order_status_response = client.orderstatus(
                    order_id=exit_order_id,
                    strategy=self.strategy.name
                )

                exit_avg_price = None
                if order_status_response.get('status') == 'success':
                    order_data = order_status_response.get('data', {})
                    exit_avg_price = order_data.get('average_price')
                    execution.broker_order_status = order_data.get('order_status')  # OpenAlgo API returns 'order_status' not 'status'

                    # If exit price is missing/zero, wait 3 seconds and re-fetch
                    if not exit_avg_price or exit_avg_price == 0:
                        logger.warning(f"[EXIT] Exit price missing for {execution.symbol}, waiting 3s to re-fetch...")
                        import time as time_sleep
                        time_sleep.sleep(3)

                        retry_response = client.orderstatus(
                            order_id=exit_order_id,
                            strategy=self.strategy.name
                        )
                        if retry_response.get('status') == 'success':
                            retry_data = retry_response.get('data', {})
                            retry_exit_price = retry_data.get('average_price')
                            if retry_exit_price and retry_exit_price > 0:
                                exit_avg_price = retry_exit_price
                                logger.debug(f"[EXIT] Exit price after retry: Rs.{exit_avg_price} for {execution.symbol}")

                # Calculate realized P&L using actual prices
                if exit_avg_price and exit_avg_price > 0:
                    execution.exit_price = exit_avg_price
                    # Calculate realized P&L based on action (BUY/SELL)
                    if execution.leg.action == 'BUY':
                        execution.realized_pnl = (exit_avg_price - execution.entry_price) * execution.quantity
                    else:
                        execution.realized_pnl = (execution.entry_price - exit_avg_price) * execution.quantity
                else:
                    # Fallback: use unrealized_pnl if exit price unavailable
                    logger.warning(f"[EXIT] Could not fetch valid exit price for {execution.symbol}, using unrealized_pnl as fallback")
                    execution.realized_pnl = execution.unrealized_pnl if execution.unrealized_pnl else 0

                db.session.commit()

                logger.debug(f"Exited position for {execution.symbol}: {reason}, Exit Order ID: {exit_order_id}, Exit Price: {execution.exit_price}")

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
        """
        Exit all active positions with BUY-FIRST priority and robust order placement.

        Features:
        - Batch execution (5 positions at a time) for scalability
        - Thread timeout (60 seconds) to prevent hanging
        - Retry mechanism (up to 2 retries) for failed orders
        - Verification and missing order detection

        Exit order:
        1. Close SELL positions first (places BUY orders to close)
        2. Close BUY positions second (places SELL orders to close)

        This maintains covered positions during exit and reduces margin spikes.
        """
        results = []
        results_lock = create_lock()
        BATCH_SIZE = 5
        THREAD_TIMEOUT = 60

        # Separate positions by original action
        # SELL positions close with BUY orders (execute first)
        # BUY positions close with SELL orders (execute second)
        sell_positions = [e for e in executions if e.leg and e.leg.action == 'SELL']
        buy_positions = [e for e in executions if e.leg and e.leg.action == 'BUY']

        # Handle positions without leg reference (fallback to sequential)
        unknown_positions = [e for e in executions if not e.leg]

        print(f"[EXIT] BUY-FIRST priority: {len(sell_positions)} SELL positions (close first), "
              f"{len(buy_positions)} BUY positions (close second)")
        logger.debug(f"[EXIT] Separating {len(executions)} positions: "
                    f"{len(sell_positions)} SELL, {len(buy_positions)} BUY, {len(unknown_positions)} unknown")

        def exit_position_worker(execution, thread_index, retry_attempt=0):
            """Worker to exit a single position with retry mechanism"""
            try:
                # Staggered delay to prevent race conditions
                if thread_index > 0 and retry_attempt == 0:
                    sleep(thread_index * 0.3)

                account = execution.account
                if not account:
                    raise Exception("No account associated with execution")

                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                # Call exit with retry logic built into _exit_position_with_retry
                success = self._exit_position_with_retry(execution, client, reason='manual_exit')

                with results_lock:
                    if success:
                        results.append({
                            'execution_id': execution.id,
                            'symbol': execution.symbol,
                            'account': account.account_name,
                            'status': 'exited',
                            'original_action': execution.leg.action if execution.leg else 'unknown'
                        })
                    else:
                        results.append({
                            'execution_id': execution.id,
                            'symbol': execution.symbol,
                            'account': account.account_name,
                            'status': 'error',
                            'error': 'Exit order failed after retries'
                        })

            except Exception as e:
                logger.error(f"Error exiting position {execution.id}: {e}")
                with results_lock:
                    results.append({
                        'execution_id': execution.id,
                        'symbol': execution.symbol,
                        'account': execution.account.account_name if execution.account else 'Unknown',
                        'status': 'error',
                        'error': str(e)
                    })

        def process_positions_in_batches(positions, phase_name):
            """Process positions in batches with timeout"""
            total = len(positions)
            batch_num = 0

            for batch_start in range(0, total, BATCH_SIZE):
                batch_end = min(batch_start + BATCH_SIZE, total)
                batch = positions[batch_start:batch_end]
                batch_num += 1

                logger.debug(f"[EXIT {phase_name} BATCH {batch_num}] Processing {len(batch)} positions...")
                print(f"[EXIT {phase_name} BATCH {batch_num}] Processing {len(batch)} positions...")

                threads = []
                for i, execution in enumerate(batch):
                    thread = threading.Thread(
                        target=exit_position_worker,
                        args=(execution, i),
                        name=f"Exit{phase_name}-{execution.symbol}",
                        daemon=True
                    )
                    thread.start()
                    threads.append(thread)

                # Wait for batch to complete with timeout
                for thread in threads:
                    thread.join(timeout=THREAD_TIMEOUT)
                    if thread.is_alive():
                        logger.error(f"[EXIT TIMEOUT] Thread {thread.name} still running after {THREAD_TIMEOUT}s")

                # Small delay between batches
                if batch_end < total:
                    sleep(0.2)

        # PHASE 1: Close SELL positions first (BUY close orders)
        if sell_positions:
            print(f"[EXIT PHASE 1] Closing {len(sell_positions)} SELL position(s) with BUY orders...")
            logger.debug(f"[EXIT PHASE 1] Starting SELL position exits")
            process_positions_in_batches(sell_positions, "SELL")
            print(f"[EXIT PHASE 1] All SELL positions processed. Results: {len(results)}")
            logger.debug(f"[EXIT PHASE 1 COMPLETE] SELL positions processed: {len(results)}")

        # PHASE 2: Close BUY positions (SELL close orders)
        if buy_positions:
            print(f"[EXIT PHASE 2] Closing {len(buy_positions)} BUY position(s) with SELL orders...")
            logger.debug(f"[EXIT PHASE 2] Starting BUY position exits")
            process_positions_in_batches(buy_positions, "BUY")
            print(f"[EXIT PHASE 2] All BUY positions processed. Total results: {len(results)}")
            logger.debug(f"[EXIT PHASE 2 COMPLETE] BUY positions processed. Total: {len(results)}")

        # Handle unknown positions sequentially (fallback)
        for execution in unknown_positions:
            exit_position_worker(execution, 0)

        # VERIFICATION: Check for missing/failed orders
        expected_count = len(executions)
        actual_count = len(results)
        success_count = len([r for r in results if r.get('status') == 'exited'])
        error_count = len([r for r in results if r.get('status') == 'error'])

        logger.warning(f"[EXIT SUMMARY] Expected: {expected_count} | Success: {success_count} | Failed: {error_count}")
        print(f"[EXIT SUMMARY] Expected: {expected_count} | Success: {success_count} | Failed: {error_count}")

        # Check for missing results
        if actual_count < expected_count:
            result_exec_ids = set(r.get('execution_id') for r in results if r.get('execution_id'))
            all_exec_ids = set(e.id for e in executions)
            missing_exec_ids = all_exec_ids - result_exec_ids

            for missing_id in missing_exec_ids:
                missing_exec = next((e for e in executions if e.id == missing_id), None)
                if missing_exec:
                    logger.error(f"[EXIT MISSING] Execution {missing_id} ({missing_exec.symbol}) has no result - thread may have failed!")
                    print(f"[EXIT MISSING] {missing_exec.symbol} - NO RESULT!")
                    results.append({
                        'execution_id': missing_id,
                        'symbol': missing_exec.symbol,
                        'account': missing_exec.account.account_name if missing_exec.account else 'Unknown',
                        'status': 'error',
                        'error': 'Thread failed silently - no response received'
                    })

        # RETRY: Retry failed orders (up to 2 times)
        failed_results = [r for r in results if r.get('status') == 'error']
        if failed_results and success_count > 0:
            logger.warning(f"[EXIT RETRY] {len(failed_results)} orders failed, attempting retry...")
            print(f"[EXIT RETRY] Retrying {len(failed_results)} failed orders...")

            for retry_attempt in range(2):
                if not failed_results:
                    break

                retry_threads = []
                for failed in failed_results[:]:
                    exec_id = failed.get('execution_id')
                    if not exec_id:
                        continue

                    execution = next((e for e in executions if e.id == exec_id), None)
                    if not execution or not execution.account:
                        continue

                    # Check if already exited (another retry may have succeeded)
                    db.session.refresh(execution)
                    if execution.status == 'exited' or execution.exit_order_id:
                        failed_results.remove(failed)
                        continue

                    logger.debug(f"[EXIT RETRY {retry_attempt + 1}] Retrying {execution.symbol} on {execution.account.account_name}")

                    thread = threading.Thread(
                        target=exit_position_worker,
                        args=(execution, 0, retry_attempt + 1),
                        daemon=True
                    )
                    thread.start()
                    retry_threads.append((thread, failed))

                # Wait for retry threads
                for thread, failed in retry_threads:
                    thread.join(timeout=THREAD_TIMEOUT)

                # Check which ones succeeded now
                failed_results = [r for r in results if r.get('status') == 'error']
                sleep(0.5)

        return results

    def _exit_position_with_retry(self, execution: StrategyExecution, client: ExtendedOpenAlgoAPI,
                                   reason: str = 'exit_condition') -> bool:
        """Exit a position with retry mechanism. Returns True on success.

        RELIABILITY FIXES (v2.0):
        - Uses row-level locking to prevent concurrent exit orders
        - Marks status as exit_pending BEFORE placing order
        - Validates position state before processing
        """
        max_retries = 3
        retry_delay = 1
        exec_id = execution.id

        # RELIABILITY FIX: Use row-level locking
        execution = StrategyExecution.query.with_for_update(nowait=False).get(exec_id)
        if not execution:
            logger.warning(f"[EXIT] Execution {exec_id} no longer exists")
            return False

        # CRITICAL: Skip if exit order already placed
        if execution.exit_order_id:
            logger.warning(f"[EXIT] SKIPPING execution {exec_id}: exit_order_id={execution.exit_order_id} already exists")
            db.session.rollback()
            return True  # Consider already exited as success

        # CRITICAL: Skip if status is not 'entered'
        if execution.status != 'entered':
            logger.warning(f"[EXIT] SKIPPING execution {exec_id}: status={execution.status} (not entered)")
            db.session.rollback()
            return execution.status in ['exit_pending', 'exited']

        # CRITICAL: Skip if quantity is 0 or None
        if not execution.quantity or execution.quantity <= 0:
            logger.warning(f"[EXIT] SKIPPING execution {exec_id}: quantity={execution.quantity}")
            execution.status = 'exited'
            execution.exit_reason = f"{reason}_no_quantity"
            execution.exit_time = datetime.utcnow()
            db.session.commit()
            return True

        # RELIABILITY FIX: Mark as exit_pending BEFORE placing order
        execution.status = 'exit_pending'
        execution.exit_reason = reason
        execution.exit_time = datetime.utcnow()
        db.session.commit()  # Commit immediately to claim this execution

        # Store values we need for order placement
        exec_symbol = execution.symbol
        exec_exchange = execution.exchange
        exec_quantity = execution.quantity
        exec_product = execution.product
        exec_entry_price = execution.entry_price
        leg_action = execution.leg.action if execution.leg else 'BUY'

        # Reverse the action
        exit_action = 'SELL' if leg_action == 'BUY' else 'BUY'

        for attempt in range(max_retries):
            try:
                # Use freeze-aware order placement for exit orders
                from app.utils.freeze_quantity_handler import place_order_with_freeze_check

                response = place_order_with_freeze_check(
                    client=client,
                    user_id=self.strategy.user_id,
                    strategy=self.strategy.name,
                    symbol=exec_symbol,
                    action=exit_action,
                    exchange=exec_exchange,
                    price_type='MARKET',
                    product=exec_product or self.strategy.product_order_type or 'MIS',
                    quantity=exec_quantity
                )

                if response and response.get('status') == 'success':
                    # Get the exit order ID
                    exit_order_id = response.get('orderid')

                    # Re-fetch execution with lock to update
                    execution = StrategyExecution.query.with_for_update(nowait=False).get(exec_id)

                    # Update with actual exit order ID
                    execution.exit_order_id = exit_order_id
                    execution.status = 'exited'

                    # Fetch exit order details to get executed price
                    order_status_response = client.orderstatus(
                        order_id=exit_order_id,
                        strategy=self.strategy.name
                    )

                    exit_avg_price = None
                    if order_status_response.get('status') == 'success':
                        order_data = order_status_response.get('data', {})
                        exit_avg_price = order_data.get('average_price')
                        execution.broker_order_status = order_data.get('order_status')

                        # If exit price is missing/zero, wait and re-fetch
                        if not exit_avg_price or exit_avg_price == 0:
                            logger.warning(f"[EXIT] Exit price missing for {exec_symbol}, waiting 3s to re-fetch...")
                            sleep(3)

                            retry_response = client.orderstatus(
                                order_id=exit_order_id,
                                strategy=self.strategy.name
                            )
                            if retry_response.get('status') == 'success':
                                retry_data = retry_response.get('data', {})
                                retry_exit_price = retry_data.get('average_price')
                                if retry_exit_price and retry_exit_price > 0:
                                    exit_avg_price = retry_exit_price

                    # Calculate realized P&L
                    if exit_avg_price and exit_avg_price > 0:
                        execution.exit_price = exit_avg_price
                        if leg_action == 'BUY':
                            execution.realized_pnl = (exit_avg_price - exec_entry_price) * exec_quantity
                        else:
                            execution.realized_pnl = (exec_entry_price - exit_avg_price) * exec_quantity
                    else:
                        execution.realized_pnl = execution.unrealized_pnl if execution.unrealized_pnl else 0

                    db.session.commit()
                    logger.debug(f"[EXIT SUCCESS] {exec_symbol} exited, Order ID: {exit_order_id}")
                    return True
                else:
                    error_msg = response.get('message', 'Unknown') if response else 'No response'
                    logger.warning(f"[EXIT] Attempt {attempt + 1}/{max_retries} failed for {exec_symbol}: {error_msg}")

            except Exception as e:
                logger.warning(f"[EXIT] Attempt {attempt + 1}/{max_retries} exception for {exec_symbol}: {e}")

            if attempt < max_retries - 1:
                sleep(retry_delay)
                retry_delay *= 2

        # RELIABILITY FIX: Revert status to 'entered' so retry can pick it up
        try:
            execution = StrategyExecution.query.with_for_update(nowait=False).get(exec_id)
            if execution and execution.status == 'exit_pending' and not execution.exit_order_id:
                execution.status = 'entered'
                execution.exit_reason = f"failed: after {max_retries} attempts"
                execution.exit_time = None
                db.session.commit()
        except Exception as e:
            logger.error(f"[EXIT] Failed to revert status for {exec_id}: {e}")
            db.session.rollback()

        logger.error(f"[EXIT FAILED] {exec_symbol} failed after {max_retries} attempts")
        return False