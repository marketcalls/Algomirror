from flask import render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.strategy import strategy_bp
from app.models import Strategy, StrategyLeg, StrategyExecution, TradingAccount, TradeQuality
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit
from app.utils.strategy_executor import StrategyExecutor
from datetime import datetime, timedelta
import json
import logging

logger = logging.getLogger(__name__)

def utc_to_ist(utc_time):
    """Convert UTC datetime to IST (UTC+5:30)"""
    if not utc_time:
        return None
    ist_offset = timedelta(hours=5, minutes=30)
    return utc_time + ist_offset

@strategy_bp.route('/')
@login_required
def dashboard():
    """Strategy dashboard showing active strategies and account status"""
    # Get user's strategies
    strategies = Strategy.query.filter_by(user_id=current_user.id).order_by(Strategy.created_at.desc()).all()

    # Get user's active accounts
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    # Calculate today's P&L across all strategies
    today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)
    today_executions = StrategyExecution.query.join(Strategy).filter(
        Strategy.user_id == current_user.id,
        StrategyExecution.created_at >= today_start
    ).all()

    # Calculate P&L only from successful executions (exclude rejected/failed)
    today_pnl = sum(
        e.realized_pnl or 0
        for e in today_executions
        if e.realized_pnl and e.status != 'failed'
        and not (hasattr(e, 'broker_order_status') and e.broker_order_status in ['rejected', 'cancelled'])
    )

    # Get active strategy count
    active_strategies = [s for s in strategies if s.is_active]

    # Convert strategies to dictionaries for JSON serialization
    strategies_data = []
    for strategy in strategies:
        strategies_data.append({
            'id': strategy.id,
            'name': strategy.name,
            'description': strategy.description,
            'market_condition': strategy.market_condition,
            'risk_profile': strategy.risk_profile,
            'is_active': strategy.is_active,
            'created_at': strategy.created_at.isoformat() if strategy.created_at else None,
            'updated_at': strategy.updated_at.isoformat() if strategy.updated_at else None,
            'selected_accounts': strategy.selected_accounts or [],
            'allocation_type': strategy.allocation_type,
            'max_loss': strategy.max_loss,
            'max_profit': strategy.max_profit,
            'trailing_sl': strategy.trailing_sl,
            # Per-strategy P&L calculation using new properties
            'total_pnl': strategy.total_pnl,
            'realized_pnl': strategy.realized_pnl,
            'unrealized_pnl': strategy.unrealized_pnl
        })

    # Convert accounts to dictionaries for JSON serialization
    accounts_data = []
    for account in accounts:
        accounts_data.append({
            'id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'is_primary': account.is_primary,
            'connection_status': account.connection_status
        })

    return render_template('strategy/dashboard.html',
                         strategies=strategies,
                         strategies_json=strategies_data,
                         accounts=accounts,
                         accounts_json=accounts_data,
                         today_pnl=today_pnl,
                         active_strategies=len(active_strategies))

@strategy_bp.route('/create-new', methods=['GET'])
@login_required
def create_new_strategy():
    """Create a blank strategy and redirect to its builder page"""
    try:
        # Create a new blank strategy
        strategy = Strategy(
            user_id=current_user.id,
            name=f"New Strategy {datetime.utcnow().strftime('%Y%m%d_%H%M%S')}",
            description="",
            is_active=True
        )
        db.session.add(strategy)
        db.session.commit()

        logger.debug(f"Created new blank strategy ID {strategy.id} for user {current_user.id}")

        # Redirect to the builder page with the new strategy ID
        return redirect(url_for('strategy.builder', strategy_id=strategy.id))

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error creating new strategy: {e}")
        flash('Error creating new strategy', 'error')
        return redirect(url_for('strategy.dashboard'))

@strategy_bp.route('/builder', methods=['GET', 'POST'])
@strategy_bp.route('/builder/<int:strategy_id>', methods=['GET', 'POST'])
@login_required
def builder(strategy_id=None):
    """Strategy builder for creating/editing strategies"""
    strategy = None
    if strategy_id:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()
        # Explicitly load legs for the strategy
        strategy.legs_list = StrategyLeg.query.filter_by(
            strategy_id=strategy.id
        ).order_by(StrategyLeg.leg_number).all()
        logger.debug(f"Loading strategy {strategy_id} with {len(strategy.legs_list)} legs")

        # Log details for debugging
        for leg in strategy.legs_list:
            logger.debug(f"Leg {leg.leg_number}: {leg.instrument} {leg.action} is_executed={leg.is_executed}")

    # Get user's accounts
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    if request.method == 'POST':
        try:
            data = request.get_json()

            # Create or update strategy
            if not strategy:
                # Check if a strategy with this name already exists
                existing_strategy = Strategy.query.filter_by(
                    user_id=current_user.id,
                    name=data.get('name')
                ).first()

                if existing_strategy:
                    return jsonify({
                        'status': 'error',
                        'message': 'A strategy with this name already exists. Please choose a different name.'
                    }), 400

                strategy = Strategy(user_id=current_user.id)
                db.session.add(strategy)

            # Update strategy fields
            strategy.name = data.get('name')
            strategy.description = data.get('description')
            strategy.market_condition = data.get('market_condition')
            strategy.risk_profile = data.get('risk_profile')
            strategy.product_order_type = data.get('product_order_type', 'NRML')
            strategy.selected_accounts = data.get('selected_accounts', [])
            strategy.allocation_type = data.get('allocation_type', 'equal')

            # Risk management fields (mutually exclusive)
            # If Supertrend exit is enabled, clear traditional risk management
            if data.get('supertrend_exit_enabled'):
                strategy.max_loss = None
                strategy.max_profit = None
                strategy.trailing_sl = None
                strategy.supertrend_exit_enabled = True
                strategy.supertrend_exit_type = data.get('supertrend_exit_type')
                strategy.supertrend_period = data.get('supertrend_period', 7)
                strategy.supertrend_multiplier = data.get('supertrend_multiplier', 3.0)
                strategy.supertrend_timeframe = data.get('supertrend_timeframe', '5m')
                # Don't reset triggered flag - it should persist for this strategy
            else:
                # Traditional risk management
                strategy.max_loss = data.get('max_loss')
                strategy.max_profit = data.get('max_profit')
                strategy.trailing_sl = data.get('trailing_sl')
                strategy.supertrend_exit_enabled = False
                strategy.supertrend_exit_type = None
                strategy.supertrend_period = None
                strategy.supertrend_multiplier = None
                strategy.supertrend_timeframe = None

            # Save strategy first to get ID
            db.session.flush()

            # When updating, preserve legs that have been executed OR have pending orders
            if strategy_id:
                # Get all existing legs
                all_existing_legs = StrategyLeg.query.filter_by(strategy_id=strategy.id).all()

                # Determine which legs to preserve:
                # 1. is_executed=True (already executed)
                # 2. Legs with StrategyExecution records (orders placed, even if is_executed=False)
                legs_to_delete = []
                legs_to_preserve = []

                for leg in all_existing_legs:
                    # Check if leg has any execution records
                    has_executions = StrategyExecution.query.filter_by(leg_id=leg.id).first() is not None

                    if leg.is_executed or has_executions:
                        legs_to_preserve.append(leg)
                        # Fix: If leg has executions but is_executed=False, update it
                        if has_executions and not leg.is_executed:
                            logger.warning(f"Leg {leg.leg_number} has executions but is_executed=False, fixing...")
                            leg.is_executed = True
                    else:
                        legs_to_delete.append(leg)

                # Delete only truly non-executed legs (no orders placed)
                for leg in legs_to_delete:
                    db.session.delete(leg)

                logger.debug(f"Preserved {len(legs_to_preserve)} legs (executed or with orders), deleted {len(legs_to_delete)} unused legs")

            # Calculate starting leg number (count of preserved legs)
            # Since we just fixed is_executed for legs with executions, we can simply count is_executed=True
            existing_leg_count = len(legs_to_preserve) if strategy_id else 0

            # Add NEW strategy legs (only the ones from the form)
            for i, leg_data in enumerate(data.get('legs', [])):
                leg_number = existing_leg_count + i + 1  # Start after executed legs

                # SAFETY CHECK: Validate lots for Fixed Lot Size mode
                lots = leg_data.get('lots', 1)
                if strategy.risk_profile == 'fixed_lots':
                    MAX_LOTS = 100  # Hard limit for fixed lot size mode
                    if lots and lots > MAX_LOTS:
                        return jsonify({
                            'status': 'error',
                            'message': f'Safety limit exceeded: Maximum {MAX_LOTS} lots allowed per leg in Fixed Lot Size mode. Leg {leg_number} has {lots} lots.'
                        }), 400

                # Log the received data for debugging
                logger.debug(f"Saving leg {leg_number}: instrument={leg_data.get('instrument')}, "
                           f"lots={lots}, quantity={leg_data.get('quantity')}")

                leg = StrategyLeg(
                    strategy_id=strategy.id,
                    leg_number=leg_number,
                    instrument=leg_data.get('instrument'),
                    product_type=leg_data.get('product_type'),
                    expiry=leg_data.get('expiry'),
                    action=leg_data.get('action'),
                    option_type=leg_data.get('option_type'),
                    strike_selection=leg_data.get('strike_selection'),
                    strike_offset=leg_data.get('strike_offset', 0),
                    strike_price=leg_data.get('strike_price'),
                    premium_value=leg_data.get('premium_value'),
                    order_type=leg_data.get('order_type', 'MARKET'),
                    price_condition=leg_data.get('price_condition'),
                    limit_price=leg_data.get('limit_price'),
                    trigger_price=leg_data.get('trigger_price'),
                    quantity=leg_data.get('quantity'),
                    lots=lots,
                    stop_loss_type=leg_data.get('stop_loss_type'),
                    stop_loss_value=leg_data.get('stop_loss_value'),
                    take_profit_type=leg_data.get('take_profit_type'),
                    take_profit_value=leg_data.get('take_profit_value'),
                    enable_trailing=leg_data.get('enable_trailing', False),
                    trailing_type=leg_data.get('trailing_type'),
                    trailing_value=leg_data.get('trailing_value')
                )
                db.session.add(leg)

            db.session.commit()

            return jsonify({
                'status': 'success',
                'message': 'Strategy saved successfully',
                'strategy_id': strategy.id
            })

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error saving strategy: {e}")
            return jsonify({'status': 'error', 'message': str(e)}), 400

    # Pass legs as a separate variable for easier access in template
    legs_data = []
    if strategy and hasattr(strategy, 'legs_list'):
        legs_data = strategy.legs_list

    # Get trade quality settings for dynamic risk profile percentages
    trade_qualities = TradeQuality.query.filter_by(user_id=current_user.id).order_by(TradeQuality.quality_grade).all()

    # Create quality map and list for template
    quality_map = {q.quality_grade: q.margin_percentage for q in trade_qualities}

    # Default values if not set
    quality_percentages = {
        'A': quality_map.get('A', 80),  # Aggressive
        'B': quality_map.get('B', 65),  # Balanced
        'C': quality_map.get('C', 40)   # Conservative
    }

    # Add any custom grades to percentages
    for q in trade_qualities:
        if q.quality_grade not in quality_percentages:
            quality_percentages[q.quality_grade] = q.margin_percentage

    return render_template('strategy/builder.html',
                         strategy=strategy,
                         strategy_legs=legs_data,
                         accounts=accounts,
                         quality_percentages=quality_percentages,
                         trade_qualities=trade_qualities)

@strategy_bp.route('/execute/<int:strategy_id>', methods=['POST'])
@login_required
@api_rate_limit()
def execute_strategy(strategy_id):
    """Execute a strategy across selected accounts"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        if not strategy.is_active:
            return jsonify({
                'status': 'error',
                'message': 'Strategy is not active'
            }), 400

        # Check if strategy has legs
        leg_count = strategy.legs.count()
        if leg_count == 0:
            return jsonify({
                'status': 'error',
                'message': 'Strategy has no legs defined'
            }), 400

        # Filter only non-executed legs
        unexecuted_legs = [leg for leg in strategy.legs if not leg.is_executed]

        if len(unexecuted_legs) == 0:
            return jsonify({
                'status': 'error',
                'message': 'All legs have already been executed. Please add new legs to execute.'
            }), 400

        # Check if accounts are selected
        if not strategy.selected_accounts:
            return jsonify({
                'status': 'error',
                'message': 'No accounts selected for strategy'
            }), 400

        logger.debug(f"Executing strategy {strategy_id} ({strategy.name}): {len(unexecuted_legs)} unexecuted legs out of {leg_count} total")

        # Check if risk profile is set to fixed lot size
        is_fixed_lots = strategy.risk_profile == 'fixed_lots'

        logger.debug(f"[EXEC DEBUG] Strategy {strategy_id} execution started")
        logger.debug(f"[EXEC DEBUG] Risk profile: {strategy.risk_profile}")
        logger.debug(f"[EXEC DEBUG] Selected accounts: {strategy.selected_accounts}")
        logger.debug(f"[EXEC DEBUG] Total legs: {leg_count}, Unexecuted legs: {len(unexecuted_legs)}")

        # For margin-based profiles (balanced, conservative, aggressive):
        # Use MarginCalculator to calculate lots dynamically at execution time
        # For fixed_lots profile: Use explicit lot sizes from legs
        if is_fixed_lots:
            # Verify that all legs have explicit lots defined
            missing_lots = [leg for leg in unexecuted_legs if not leg.lots or leg.lots <= 0]
            if missing_lots:
                logger.error(f"[EXEC DEBUG] Fixed lots profile but {len(missing_lots)} legs missing lot values")
                return jsonify({
                    'status': 'error',
                    'message': f'Fixed Lot Size profile requires all legs to have lots specified. {len(missing_lots)} leg(s) missing lot values.'
                }), 400
            use_margin_calc = False
            logger.debug(f"[EXEC DEBUG] Strategy {strategy_id}: Risk profile is 'Fixed Lot Size', using explicit lot sizes")
            for leg in unexecuted_legs:
                logger.debug(f"[EXEC DEBUG] Leg {leg.leg_number}: {leg.instrument} {leg.action} - {leg.lots} lots")
        else:
            # Margin-based profiles: use MarginCalculator
            use_margin_calc = True
            logger.debug(f"[EXEC DEBUG] Strategy {strategy_id}: Using MarginCalculator with risk profile '{strategy.risk_profile}'")
            for leg in unexecuted_legs:
                logger.debug(f"[EXEC DEBUG] Leg {leg.leg_number}: {leg.instrument} {leg.action} - lots will be calculated dynamically")

        # Initialize strategy executor
        logger.debug(f"[EXEC DEBUG] Initializing StrategyExecutor...")
        executor = StrategyExecutor(strategy, use_margin_calculator=use_margin_calc)

        # CRITICAL: Reset TSL tracking BEFORE execution starts
        # This prevents stale TSL data from previous trades from triggering exits
        # TSL State: RESET -> WAITING -> ACTIVE -> TRIGGERED -> EXIT
        if strategy.trailing_sl and strategy.trailing_sl > 0:
            logger.debug(f"[TSL STATE] Strategy {strategy.id} ({strategy.name}): RESET - Clearing TSL for new entry")
            strategy.trailing_sl_active = False
            strategy.trailing_sl_peak_pnl = 0.0
            strategy.trailing_sl_initial_stop = None
            strategy.trailing_sl_trigger_pnl = None
            strategy.trailing_sl_triggered_at = None
            strategy.trailing_sl_exit_reason = None
            db.session.commit()
            logger.debug(f"[TSL STATE] Strategy {strategy.id} ({strategy.name}): State = WAITING (monitoring for P&L > 0)")

        # Execute strategy
        logger.debug(f"[EXEC DEBUG] Executing strategy...")
        results = executor.execute()
        logger.debug(f"[EXEC DEBUG] Execution complete. Results count: {len(results)}")

        # Count successful, failed, and skipped executions
        # With Phase 2: 'pending' means order placed successfully, being tracked in background
        successful = sum(1 for r in results if r.get('status') in ['success', 'pending'])
        failed = sum(1 for r in results if r.get('status') in ['failed', 'error'])
        skipped = sum(1 for r in results if r.get('status') == 'skipped')

        # Determine overall status and message
        if successful == 0 and skipped > 0:
            # All orders were skipped
            overall_status = 'warning'
            message = f'Strategy execution skipped: Insufficient margin for all {skipped} orders'
        elif successful == 0 and failed > 0:
            # All orders failed
            overall_status = 'error'
            message = f'Strategy execution failed: All {failed} orders failed'
        elif successful > 0 and (failed > 0 or skipped > 0):
            # Mixed results
            overall_status = 'partial'
            message = f'Strategy partially executed: {successful} successful, {failed} failed, {skipped} skipped'
        elif successful > 0:
            # All successful
            overall_status = 'success'
            message = f'Strategy executed successfully: {successful} order(s) placed and being tracked'
        else:
            # No orders processed
            overall_status = 'error'
            message = 'No orders were processed'

        return jsonify({
            'status': overall_status,
            'message': message,
            'results': results,
            'summary': {
                'total_legs': leg_count,
                'accounts': len(strategy.selected_accounts),
                'successful_orders': successful,
                'failed_orders': failed,
                'skipped_orders': skipped,
                'total_attempts': len(results)
            }
        })

    except Exception as e:
        logger.error(f"Error executing strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/exit/<int:strategy_id>', methods=['POST'])
@login_required
@api_rate_limit()
def exit_strategy(strategy_id):
    """Exit all positions for a strategy"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Get active executions (exclude rejected/cancelled)
        active_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

        # Filter out rejected/cancelled orders
        active_executions = [
            exec for exec in active_executions
            if not (hasattr(exec, 'broker_order_status') and exec.broker_order_status in ['rejected', 'cancelled'])
        ]

        if not active_executions:
            return jsonify({
                'status': 'error',
                'message': 'No active positions to exit'
            }), 400

        executor = StrategyExecutor(strategy)
        results = executor.exit_all_positions(active_executions)

        return jsonify({
            'status': 'success',
            'message': f'Exited {len(results)} positions',
            'results': results
        })

    except Exception as e:
        logger.error(f"Error exiting strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/leg/<int:leg_id>/cancel', methods=['POST'])
@login_required
@api_rate_limit()
def cancel_leg_orders(strategy_id, leg_id):
    """Cancel all open orders for a specific leg"""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        leg = StrategyLeg.query.filter_by(
            id=leg_id,
            strategy_id=strategy_id
        ).first_or_404()

        # Get all executions for this leg with status 'pending' (open orders)
        pending_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='pending'
        ).all()

        # Also check for 'entered' status with broker_order_status='open'
        entered_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='entered'
        ).filter(StrategyExecution.broker_order_status == 'open').all()

        all_pending = pending_executions + entered_executions

        if not all_pending:
            return jsonify({
                'status': 'error',
                'message': 'No pending orders found for this leg'
            }), 400

        cancelled_count = 0
        failed_count = 0
        errors = []

        for execution in all_pending:
            try:
                account = execution.account
                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                # Cancel the order using OpenAlgo API
                response = client.cancelorder(
                    order_id=execution.order_id,
                    strategy=f"Strategy_{strategy.id}"
                )

                if response.get('status') == 'success':
                    # Update execution status
                    execution.status = 'failed'
                    execution.broker_order_status = 'cancelled'
                    execution.exit_reason = 'manual_cancel'
                    execution.exit_time = datetime.utcnow()
                    cancelled_count += 1
                    logger.debug(f"Cancelled order {execution.order_id} for leg {leg_id}")
                else:
                    failed_count += 1
                    error_msg = response.get('message', 'Unknown error')
                    errors.append(f"Order {execution.order_id}: {error_msg}")
                    logger.error(f"Failed to cancel order {execution.order_id}: {error_msg}")

            except Exception as e:
                failed_count += 1
                errors.append(f"Order {execution.order_id}: {str(e)}")
                logger.error(f"Exception cancelling order {execution.order_id}: {e}")

        # Check if leg has any remaining active executions (entered with complete status)
        remaining_active = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='entered'
        ).filter(
            StrategyExecution.broker_order_status.in_(['complete', 'traded', 'filled'])
        ).count()

        # If all orders were cancelled and no active positions remain, reset leg execution status
        if cancelled_count > 0 and remaining_active == 0:
            leg.is_executed = False
            logger.debug(f"Reset leg {leg.leg_number} is_executed to False (all orders cancelled, no active positions)")

        db.session.commit()

        if cancelled_count > 0 and failed_count == 0:
            return jsonify({
                'status': 'success',
                'message': f'Cancelled {cancelled_count} order(s) for leg {leg.leg_number}',
                'cancelled': cancelled_count,
                'leg_reset': remaining_active == 0  # Inform frontend if leg was reset
            })
        elif cancelled_count > 0:
            return jsonify({
                'status': 'partial',
                'message': f'Cancelled {cancelled_count} order(s), {failed_count} failed',
                'cancelled': cancelled_count,
                'failed': failed_count,
                'errors': errors,
                'leg_reset': remaining_active == 0
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'Failed to cancel all orders',
                'cancelled': 0,
                'failed': failed_count,
                'errors': errors
            }), 500

    except Exception as e:
        logger.error(f"Error cancelling leg orders: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/leg/<int:leg_id>/modify', methods=['POST'])
@login_required
@api_rate_limit()
def modify_leg_orders(strategy_id, leg_id):
    """Modify the price of all open orders for a specific leg"""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

        data = request.get_json()
        new_price = data.get('price')

        if not new_price:
            return jsonify({
                'status': 'error',
                'message': 'New price is required'
            }), 400

        try:
            new_price = float(new_price)
            if new_price <= 0:
                return jsonify({
                    'status': 'error',
                    'message': 'Price must be greater than 0'
                }), 400
        except ValueError:
            return jsonify({
                'status': 'error',
                'message': 'Invalid price format'
            }), 400

        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        leg = StrategyLeg.query.filter_by(
            id=leg_id,
            strategy_id=strategy_id
        ).first_or_404()

        # Get all executions for this leg with status 'pending' (open orders)
        pending_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='pending'
        ).all()

        # Also check for 'entered' status with broker_order_status='open'
        entered_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='entered'
        ).filter(StrategyExecution.broker_order_status == 'open').all()

        all_pending = pending_executions + entered_executions

        if not all_pending:
            return jsonify({
                'status': 'error',
                'message': 'No pending orders found for this leg'
            }), 400

        modified_count = 0
        failed_count = 0
        errors = []

        for execution in all_pending:
            try:
                account = execution.account
                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                # Modify the order using OpenAlgo API
                # Use execution fields (actual placed order details) not leg fields
                response = client.modifyorder(
                    order_id=execution.order_id,
                    strategy=f"Strategy_{strategy.id}",
                    symbol=execution.symbol,
                    action=leg.action,
                    exchange=execution.exchange,
                    price_type='LIMIT',
                    product=strategy.product_order_type,  # MIS/CNC from strategy
                    quantity=execution.quantity,
                    price=new_price
                )

                if response.get('status') == 'success':
                    # Update entry_price to new limit price for display in orderbook
                    # This will be updated to actual filled price by the order status poller when order fills
                    execution.entry_price = new_price
                    modified_count += 1
                    logger.debug(f"Modified order {execution.order_id} for leg {leg_id} to price {new_price}")
                else:
                    failed_count += 1
                    error_msg = response.get('message', 'Unknown error')
                    errors.append(f"Order {execution.order_id}: {error_msg}")
                    logger.error(f"Failed to modify order {execution.order_id}: {error_msg}")

            except Exception as e:
                failed_count += 1
                errors.append(f"Order {execution.order_id}: {str(e)}")
                logger.error(f"Exception modifying order {execution.order_id}: {e}")

        # Update leg limit price
        if modified_count > 0:
            leg.limit_price = new_price

        db.session.commit()

        if modified_count > 0 and failed_count == 0:
            return jsonify({
                'status': 'success',
                'message': f'Modified {modified_count} order(s) for leg {leg.leg_number} to Rs.{new_price}',
                'modified': modified_count
            })
        elif modified_count > 0:
            return jsonify({
                'status': 'partial',
                'message': f'Modified {modified_count} order(s), {failed_count} failed',
                'modified': modified_count,
                'failed': failed_count,
                'errors': errors
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'Failed to modify all orders',
                'modified': 0,
                'failed': failed_count,
                'errors': errors
            }), 500

    except Exception as e:
        logger.error(f"Error modifying leg orders: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/leg/<int:leg_id>/convert-to-market', methods=['POST'])
@login_required
@api_rate_limit()
def convert_leg_to_market(strategy_id, leg_id):
    """Convert pending limit orders to market orders (cancel + place market order)"""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app.utils.order_status_poller import order_status_poller

        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        leg = StrategyLeg.query.filter_by(
            id=leg_id,
            strategy_id=strategy_id
        ).first_or_404()

        # Get all executions for this leg with status 'pending' (open orders)
        pending_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='pending'
        ).all()

        # Also check for 'entered' status with broker_order_status='open'
        entered_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg_id,
            status='entered'
        ).filter(StrategyExecution.broker_order_status == 'open').all()

        all_pending = pending_executions + entered_executions

        if not all_pending:
            return jsonify({
                'status': 'error',
                'message': 'No pending orders found for this leg'
            }), 400

        converted_count = 0
        failed_count = 0
        errors = []

        for execution in all_pending:
            try:
                # Step 0: Remove from order status poller to prevent it from marking as failed
                order_status_poller.remove_order(execution.id)
                logger.debug(f"Removed execution {execution.id} (order {execution.order_id}) from poller before converting to market")

                account = execution.account
                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                # Step 1: Cancel the existing limit order
                cancel_response = client.cancelorder(
                    order_id=execution.order_id,
                    strategy=f"Strategy_{strategy.id}"
                )

                if cancel_response.get('status') != 'success':
                    failed_count += 1
                    error_msg = cancel_response.get('message', 'Failed to cancel order')
                    errors.append(f"Order {execution.order_id}: {error_msg}")
                    logger.error(f"Failed to cancel order {execution.order_id}: {error_msg}")
                    continue

                logger.debug(f"Cancelled order {execution.order_id} for leg {leg_id}")

                # Step 2: Place a new market order
                market_response = client.placeorder(
                    strategy=f"Strategy_{strategy.id}",
                    symbol=execution.symbol,
                    action=leg.action,
                    exchange=execution.exchange,
                    price_type='MARKET',
                    product=strategy.product_order_type,
                    quantity=execution.quantity
                )

                if market_response.get('status') == 'success':
                    # Update execution with new market order ID
                    new_order_id = market_response.get('orderid')
                    execution.order_id = new_order_id
                    execution.broker_order_status = 'complete'  # Market orders execute immediately
                    execution.status = 'entered'  # Order is now entered
                    converted_count += 1
                    logger.debug(f"Placed market order {new_order_id} for leg {leg_id} (converted from limit order)")
                else:
                    failed_count += 1
                    error_msg = market_response.get('message', 'Failed to place market order')
                    errors.append(f"Account {account.account_name}: {error_msg}")
                    logger.error(f"Failed to place market order for account {account.account_name}: {error_msg}")

            except Exception as e:
                failed_count += 1
                errors.append(f"Order {execution.order_id}: {str(e)}")
                logger.error(f"Exception converting order {execution.order_id}: {e}")

        db.session.commit()

        if converted_count > 0 and failed_count == 0:
            return jsonify({
                'status': 'success',
                'message': f'Converted {converted_count} order(s) for leg {leg.leg_number} to market orders',
                'converted': converted_count
            })
        elif converted_count > 0:
            return jsonify({
                'status': 'partial',
                'message': f'Converted {converted_count} order(s), {failed_count} failed',
                'converted': converted_count,
                'failed': failed_count,
                'errors': errors
            })
        else:
            return jsonify({
                'status': 'error',
                'message': f'Failed to convert all orders',
                'converted': 0,
                'failed': failed_count,
                'errors': errors
            }), 500

    except Exception as e:
        logger.error(f"Error converting leg to market orders: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/delete/<int:strategy_id>', methods=['DELETE'])
@login_required
def delete_strategy(strategy_id):
    """Delete a strategy with optional force deletion"""
    try:
        from flask import request

        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Check if force deletion is requested
        force_delete = request.args.get('force', 'false').lower() == 'true'

        # Check for active positions (exclude rejected/cancelled)
        active_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

        # Filter out rejected/cancelled orders
        active_executions = [
            exec for exec in active_executions
            if not (hasattr(exec, 'broker_order_status') and exec.broker_order_status in ['rejected', 'cancelled'])
        ]

        # Get all executions (for cleanup on force delete)
        all_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id
        ).all()

        if active_executions and not force_delete:
            # Return warning with details
            execution_details = []
            for exec in active_executions[:5]:  # Show first 5
                execution_details.append({
                    'symbol': exec.symbol,
                    'quantity': exec.quantity,
                    'entry_price': exec.entry_price,
                    'unrealized_pnl': exec.unrealized_pnl or 0,
                    'account': exec.account.account_name if exec.account else 'N/A'
                })

            return jsonify({
                'status': 'warning',
                'message': f'Strategy has {len(active_executions)} open position(s)',
                'active_positions': len(active_executions),
                'total_executions': len(all_executions),
                'execution_details': execution_details,
                'can_force_delete': True,
                'warning_message': 'This will delete the strategy AND all execution records (including open positions). Use force delete if positions are expired or manually closed.'
            }), 409  # Conflict status code

        # Perform deletion
        if force_delete and active_executions:
            logger.warning(f"Force deleting strategy {strategy_id} ({strategy.name}) with {len(active_executions)} open positions")

        # Delete risk events first (foreign key constraint)
        from app.models import RiskEvent
        deleted_risk_events = RiskEvent.query.filter_by(strategy_id=strategy_id).delete()
        logger.debug(f"Deleted {deleted_risk_events} risk events for strategy {strategy_id}")

        # Delete all executions (cascade should handle this, but explicit is better)
        deleted_executions = 0
        for execution in all_executions:
            db.session.delete(execution)
            deleted_executions += 1

        # Delete strategy legs
        deleted_legs = 0
        for leg in strategy.legs:
            db.session.delete(leg)
            deleted_legs += 1

        # Delete strategy itself
        db.session.delete(strategy)
        db.session.commit()

        message = f'Strategy deleted successfully'
        if force_delete and active_executions:
            message = f'Strategy force deleted: Removed {deleted_executions} execution(s) and {deleted_legs} leg(s) (including {len(active_executions)} open position(s))'
        elif deleted_executions > 0:
            message = f'Strategy deleted: Removed {deleted_executions} execution record(s) and {deleted_legs} leg(s)'

        return jsonify({
            'status': 'success',
            'message': message,
            'deleted_executions': deleted_executions,
            'deleted_legs': deleted_legs,
            'force_deleted': force_delete
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/cleanup-expired', methods=['POST'])
@login_required
def cleanup_expired_executions():
    """Cleanup executions for expired contracts (admin utility)"""
    try:
        from datetime import datetime, timedelta

        # Get strategies with executions older than 7 days
        cutoff_date = datetime.utcnow() - timedelta(days=7)

        old_executions = StrategyExecution.query.join(Strategy).filter(
            Strategy.user_id == current_user.id,
            StrategyExecution.created_at < cutoff_date,
            StrategyExecution.status == 'entered'  # Still marked as open
        ).all()

        # Filter out rejected/cancelled orders
        old_executions = [
            exec for exec in old_executions
            if not (hasattr(exec, 'broker_order_status') and exec.broker_order_status in ['rejected', 'cancelled'])
        ]

        if not old_executions:
            return jsonify({
                'status': 'info',
                'message': 'No expired executions found',
                'cleaned': 0
            })

        # Group by strategy
        strategies_affected = {}
        for exec in old_executions:
            if exec.strategy_id not in strategies_affected:
                strategies_affected[exec.strategy_id] = {
                    'name': exec.strategy.name,
                    'count': 0,
                    'executions': []
                }
            strategies_affected[exec.strategy_id]['count'] += 1
            strategies_affected[exec.strategy_id]['executions'].append({
                'symbol': exec.symbol,
                'created_at': exec.created_at.isoformat(),
                'days_old': (datetime.utcnow() - exec.created_at).days
            })

        return jsonify({
            'status': 'info',
            'message': f'Found {len(old_executions)} expired execution(s) across {len(strategies_affected)} strategy(ies)',
            'total_expired': len(old_executions),
            'strategies_affected': strategies_affected,
            'can_cleanup': True,
            'note': 'Use force delete on each strategy to cleanup'
        })

    except Exception as e:
        logger.error(f"Error checking expired executions: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/orderbook', methods=['GET'])
@login_required
def strategy_orderbook(strategy_id):
    """Get strategy-level orderbook (OpenAlgo format)"""
    # Clear session cache to get fresh data from database
    # This is crucial after multi-threaded order placements/closes
    db.session.rollback()  # Abandon any cached state
    db.session.expire_all()  # Force reload of all objects

    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Sync pending orders from broker to ensure latest state
    # This handles LIMIT orders that may have filled since last check
    from app.utils.order_status_poller import order_status_poller
    pending_executions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id,
        status='pending'
    ).all()
    for execution in pending_executions:
        if execution.order_id:
            order_status_poller.sync_order_status(execution.id)

    # Refresh session after sync
    db.session.expire_all()

    # Get all executions for this strategy (INCLUDE rejected/failed for visibility)
    executions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id
    ).join(TradingAccount).join(StrategyLeg).all()

    orders = []
    for execution in executions:
        # Determine order status for display
        # IMPORTANT: Check broker_order_status FIRST (most reliable)
        # DO NOT use entry_price to determine status - it can be set to limit price
        if hasattr(execution, 'broker_order_status') and execution.broker_order_status:
            order_status = execution.broker_order_status
        # Fallback to mapping from execution status
        elif execution.status:
            order_status = execution.status
            if order_status == 'entered':
                order_status = 'complete'
            elif order_status == 'exited':
                order_status = 'complete'
            elif order_status == 'failed':
                order_status = 'rejected'
            elif order_status == 'pending':
                order_status = 'open'
        else:
            # Last resort - unknown status
            order_status = 'open'

        # Get actual product type (MIS, NRML, CNC) from strategy, not leg.product_type (options/futures)
        actual_product = strategy.product_order_type.upper() if strategy.product_order_type else 'MIS'

        # Add entry order
        orders.append({
            'account_name': execution.account.account_name if execution.account else 'N/A',
            'broker_name': execution.account.broker_name if execution.account else 'N/A',
            'action': execution.leg.action,
            'symbol': execution.symbol,
            'exchange': execution.exchange,
            'orderid': execution.order_id or f"STG_{execution.id}",
            'product': actual_product,
            'quantity': str(execution.quantity),
            'price': execution.entry_price or 0.0,
            'pricetype': execution.leg.order_type or 'MARKET',
            'order_status': order_status,
            'trigger_price': 0.0,
            'timestamp': utc_to_ist(execution.entry_time).strftime('%d-%b-%Y %H:%M:%S') if execution.entry_time else "",
            'leg_id': execution.leg_id,
            'leg_number': execution.leg.leg_number if execution.leg else None
        })

        # If position was exited, add the exit order as a separate order
        if execution.status == 'exited' and execution.exit_price:
            exit_action = 'SELL' if execution.leg.action == 'BUY' else 'BUY'
            # Use real exit order ID if available, otherwise fallback to generated ID
            exit_orderid = execution.exit_order_id if execution.exit_order_id else f"STG_{execution.id}_EXIT"
            orders.append({
                'account_name': execution.account.account_name if execution.account else 'N/A',
                'broker_name': execution.account.broker_name if execution.account else 'N/A',
                'leg_number': execution.leg.leg_number if execution.leg else None,
                'action': exit_action,
                'symbol': execution.symbol,
                'exchange': execution.exchange,
                'orderid': exit_orderid,  # Use real order ID from OpenAlgo
                'product': actual_product,
                'quantity': str(execution.quantity),
                'price': execution.exit_price,
                'pricetype': 'MARKET',
                'order_status': 'complete',
                'trigger_price': 0.0,
                'timestamp': utc_to_ist(execution.exit_time).strftime('%d-%b-%Y %H:%M:%S') if execution.exit_time else ""
            })

    # Calculate statistics like OpenAlgo
    statistics = {
        'total_buy_orders': float(len([o for o in orders if o['action'] == 'BUY'])),
        'total_sell_orders': float(len([o for o in orders if o['action'] == 'SELL'])),
        'total_completed_orders': float(len([o for o in orders if o['order_status'] == 'complete'])),
        'total_open_orders': float(len([o for o in orders if o['order_status'] == 'open'])),
        'total_rejected_orders': float(len([o for o in orders if o['order_status'] == 'rejected']))
    }

    return jsonify({
        'status': 'success',
        'data': {
            'orders': orders,
            'statistics': statistics
        }
    })

@strategy_bp.route('/<int:strategy_id>/tradebook', methods=['GET'])
@login_required
def strategy_tradebook(strategy_id):
    """Get strategy-level tradebook (OpenAlgo format)"""
    # Clear session cache to get fresh data from database
    # This is crucial after multi-threaded order placements/closes
    db.session.rollback()  # Abandon any cached state
    db.session.expire_all()  # Force reload of all objects

    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Get executed trades (exclude rejected/cancelled/failed)
    trades = StrategyExecution.query.filter(
        StrategyExecution.strategy_id == strategy_id,
        StrategyExecution.status.in_(['entered', 'exited'])
    ).join(TradingAccount).join(StrategyLeg).all()

    # Filter to show only successfully executed trades
    # Exclude: failed/pending status, or rejected/cancelled/open/pending broker status
    # BUT: If trade has entry_price, it's filled - include it regardless of broker status
    # Note: 'open' broker status means order not yet filled
    # Note: 'pending' is legacy (not valid OpenAlgo status) but kept for backward compatibility
    trades = [
        trade for trade in trades
        if (
            # Include if has entry_price (definitely filled)
            (trade.entry_price and trade.entry_price > 0)
            # OR include if status is good AND broker status is good
            or (
                trade.status not in ['failed', 'pending']
                and not (hasattr(trade, 'broker_order_status') and
                        trade.broker_order_status in ['rejected', 'cancelled', 'open', 'pending'])
            )
        )
    ]

    # Get actual product type (MIS, NRML, CNC) from strategy
    actual_product = strategy.product_order_type.upper() if strategy.product_order_type else 'MIS'

    data = []
    for trade in trades:
        # Add entry trade
        entry_value = (trade.entry_price or 0.0) * trade.quantity
        data.append({
            'account_name': trade.account.account_name if trade.account else 'N/A',
            'broker_name': trade.account.broker_name if trade.account else 'N/A',
            'leg_number': trade.leg.leg_number if trade.leg else None,
            'action': trade.leg.action,
            'symbol': trade.symbol,
            'exchange': trade.exchange,
            'orderid': trade.order_id or f"STG_{trade.id}",
            'product': actual_product,
            'quantity': trade.quantity,
            'average_price': trade.entry_price or 0.0,
            'timestamp': utc_to_ist(trade.entry_time).strftime('%H:%M:%S') if trade.entry_time else "",
            'trade_value': entry_value
        })

        # If position was exited, add the exit trade as a separate entry
        if trade.status == 'exited' and trade.exit_price:
            exit_action = 'SELL' if trade.leg.action == 'BUY' else 'BUY'
            exit_value = trade.exit_price * trade.quantity
            # Use real exit order ID if available, otherwise fallback to generated ID
            exit_orderid = trade.exit_order_id if trade.exit_order_id else f"STG_{trade.id}_EXIT"
            data.append({
                'account_name': trade.account.account_name if trade.account else 'N/A',
                'broker_name': trade.account.broker_name if trade.account else 'N/A',
                'leg_number': trade.leg.leg_number if trade.leg else None,
                'action': exit_action,
                'symbol': trade.symbol,
                'exchange': trade.exchange,
                'orderid': exit_orderid,  # Use real order ID from OpenAlgo
                'product': actual_product,
                'quantity': trade.quantity,
                'average_price': trade.exit_price,
                'timestamp': utc_to_ist(trade.exit_time).strftime('%H:%M:%S') if trade.exit_time else "",
                'trade_value': exit_value
            })

    return jsonify({
        'status': 'success',
        'data': data
    })

@strategy_bp.route('/<int:strategy_id>/positions', methods=['GET'])
@login_required
def strategy_positions(strategy_id):
    """Get strategy-level positions including closed positions with qty=0 (OpenAlgo format)"""
    # Clear session cache to get fresh data from database
    # This is crucial after multi-threaded position closes
    db.session.rollback()  # Abandon any cached state
    db.session.expire_all()  # Force reload of all objects

    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Sync pending orders from broker to ensure latest state
    # This handles LIMIT orders that may have filled since last check
    from app.utils.order_status_poller import order_status_poller
    pending_executions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id,
        status='pending'
    ).all()
    for execution in pending_executions:
        if execution.order_id:
            order_status_poller.sync_order_status(execution.id)

    # Refresh session after sync
    db.session.expire_all()

    # Get both open AND closed positions (status='entered' or 'exited')
    # Closed positions will show with quantity=0
    positions = StrategyExecution.query.filter(
        StrategyExecution.strategy_id == strategy_id,
        StrategyExecution.status.in_(['entered', 'exit_pending', 'exited'])
    ).join(TradingAccount).join(StrategyLeg).all()

    logger.debug(f"[POSITIONS] Found {len(positions)} positions for strategy {strategy_id}")
    for pos in positions:
        logger.debug(f"[POSITIONS] Position ID {pos.id}: symbol={pos.symbol}, status={pos.status}, qty={pos.quantity}, leg={pos.leg.leg_number if pos.leg else None}")

    from app.utils.openalgo_client import ExtendedOpenAlgoAPI

    data = []
    for position in positions:
        # Skip orders that were failed, pending, or have problematic broker status
        # BUT: If position has entry_price, it's filled - include it regardless of status

        # If has entry_price, it's definitely filled - include it
        has_entry_price = position.entry_price and position.entry_price > 0

        if not has_entry_price:
            # Only apply status checks if no entry price
            if position.status in ['failed', 'pending']:
                continue
            # Skip if broker status indicates order is not successfully filled
            # 'open' broker status means order placed but not yet filled
            # 'pending' is legacy (not valid OpenAlgo status) but kept for backward compatibility
            if hasattr(position, 'broker_order_status') and position.broker_order_status in ['rejected', 'cancelled', 'open', 'pending']:
                continue

        # Check if this is a closed position
        is_closed = position.status == 'exited'

        if is_closed:
            # Closed position - show with quantity=0 and realized P&L
            quantity = 0  # Show as 0 for closed positions
            ltp = position.exit_price or position.entry_price or 0
            pnl = position.realized_pnl or 0
        else:
            # Open position - calculate unrealized P&L
            # Get current price for P&L calculation
            try:
                client = ExtendedOpenAlgoAPI(
                    api_key=position.account.get_api_key(),
                    host=position.account.host_url
                )
                quote = client.quotes(symbol=position.symbol, exchange=position.exchange)
                ltp = float(quote.get('data', {}).get('ltp', position.entry_price))
            except:
                ltp = position.entry_price or 0

            # Calculate P&L based on action
            quantity = position.quantity
            if position.leg.action == 'SELL':
                quantity = -quantity  # Negative for sell positions

            # Calculate P&L
            if position.leg.action == 'BUY':
                pnl = (ltp - (position.entry_price or 0)) * position.quantity
            else:  # SELL
                pnl = ((position.entry_price or 0) - ltp) * position.quantity

            # Update unrealized P&L in database
            position.unrealized_pnl = pnl

        # Get actual product type (MIS, NRML, CNC) from strategy
        actual_product = strategy.product_order_type.upper() if strategy.product_order_type else 'MIS'

        data.append({
            'account_name': position.account.account_name if position.account else 'N/A',
            'account_id': position.account_id,  # Add account_id for close button
            'broker_name': position.account.broker_name if position.account else 'N/A',
            'leg_number': position.leg.leg_number if position.leg else None,
            'symbol': position.symbol,
            'exchange': position.exchange,
            'product': actual_product,
            'quantity': str(quantity),  # String format, 0 for closed, negative for sell if open
            'average_price': str(position.entry_price or 0.0),
            'ltp': str(ltp),
            'pnl': str(round(pnl, 2)),
            'is_closed': is_closed,  # Flag to indicate closed position
            'status': position.status,  # Add status for debugging
            'supertrend_exit_enabled': strategy.supertrend_exit_enabled,  # Supertrend risk management
            'stop_loss': position.leg.stop_loss_value if position.leg else None,  # Leg-level SL
            'take_profit': position.leg.take_profit_value if position.leg else None,  # Leg-level TP
            'max_loss': strategy.max_loss,  # Strategy-level max loss (Traditional)
            'max_profit': strategy.max_profit,  # Strategy-level max profit (Traditional)
            'trailing_sl': strategy.trailing_sl  # Strategy-level trailing SL (Traditional)
        })

    # Calculate total P&L for trailing SL tracking
    total_pnl = sum(float(p['pnl']) for p in data)

    # Update Trailing SL tracking if enabled
    tsl_status = {
        'enabled': bool(strategy.trailing_sl and strategy.trailing_sl > 0),
        'active': False,
        'no_positions': False,
        'peak_pnl': 0.0,
        'trigger_pnl': None,
        'current_pnl': total_pnl,
        'trailing_pct': strategy.trailing_sl or 0
    }

    if tsl_status['enabled']:
        # Get open positions count and calculate entry value
        open_positions = [p for p in data if not p['is_closed'] and int(p['quantity']) != 0]
        open_positions_count = len(open_positions)

        if open_positions_count > 0:
            # If TSL was already triggered for this trade, don't re-calculate
            if strategy.trailing_sl_triggered_at:
                logger.debug(f"[TSL] Strategy {strategy.name}: TSL already triggered, returning triggered state")
                tsl_status['active'] = False
                tsl_status['triggered'] = True
                tsl_status['peak_pnl'] = strategy.trailing_sl_peak_pnl or 0.0
                tsl_status['trigger_pnl'] = strategy.trailing_sl_trigger_pnl
                tsl_status['initial_stop'] = strategy.trailing_sl_initial_stop
                tsl_status['current_stop'] = strategy.trailing_sl_trigger_pnl
                tsl_status['exit_reason'] = strategy.trailing_sl_exit_reason
            else:
                # TSL ACTIVE FROM ENTRY - Calculate initial stop based on NET PREMIUM
                # For combined strategies (straddles, spreads), we need to account for direction:
                #   - BUY leg: Premium paid (debit) = positive contribution to net premium
                #   - SELL leg: Premium received (credit) = negative contribution to net premium
                # Net Premium > 0 means net debit (paid money)
                # Net Premium < 0 means net credit (received money)

                # Get open executions from database for accurate action info
                open_executions = StrategyExecution.query.filter_by(
                    strategy_id=strategy_id,
                    status='entered'
                ).join(StrategyLeg).all()

                net_premium = 0
                buy_premium = 0
                sell_premium = 0

                for exec in open_executions:
                    if exec.entry_price and exec.quantity:
                        premium = float(exec.entry_price) * abs(int(exec.quantity))
                        # Check leg action to determine direction
                        if exec.leg and exec.leg.action and exec.leg.action.upper() == 'BUY':
                            net_premium += premium  # Debit (cost)
                            buy_premium += premium
                        else:  # SELL
                            net_premium -= premium  # Credit (received)
                            sell_premium += premium

                # For TSL calculation, use absolute value of net premium
                # This represents the actual capital at risk
                entry_value = abs(net_premium)

                # Detect strategy type for logging
                is_combined = buy_premium > 0 and sell_premium > 0
                is_net_credit = net_premium < 0
                strategy_type = "Combined" if is_combined else "Single Leg"
                premium_type = "Net Credit" if is_net_credit else "Net Debit"

                logger.debug(f"[TSL] Strategy {strategy.name}: {strategy_type} ({premium_type})")
                logger.debug(f"[TSL]   BUY premiums: {buy_premium:.2f}, SELL premiums: {sell_premium:.2f}")
                logger.debug(f"[TSL]   Net Premium: {net_premium:.2f}, Entry Value (abs): {entry_value:.2f}")

                trailing_value = strategy.trailing_sl
                trailing_type = strategy.trailing_sl_type or 'percentage'

                # Calculate initial stop (max loss from entry)
                # For 20% TSL: initial_stop = -20% of entry value (net premium)
                if trailing_type == 'percentage':
                    initial_stop_pnl = -entry_value * (trailing_value / 100)
                elif trailing_type == 'points':
                    initial_stop_pnl = -trailing_value
                else:  # 'amount'
                    initial_stop_pnl = -trailing_value

                # Set initial stop if not already set
                if strategy.trailing_sl_initial_stop is None:
                    strategy.trailing_sl_initial_stop = initial_stop_pnl
                    logger.debug(f"[TSL STATE] Strategy {strategy.name}: Initial stop set at {initial_stop_pnl:.2f} (Net Premium: {net_premium:.2f}, Entry Value: {entry_value:.2f})")

                # TSL is ALWAYS active from entry (no waiting state)
                tsl_status['active'] = True
                strategy.trailing_sl_active = True

                # Track peak P&L (highest P&L achieved)
                current_peak = strategy.trailing_sl_peak_pnl or 0.0
                if total_pnl > current_peak:
                    strategy.trailing_sl_peak_pnl = total_pnl
                    current_peak = total_pnl
                    logger.debug(f"[TSL] Strategy {strategy.name}: New peak P&L = {current_peak:.2f}")

                tsl_status['peak_pnl'] = current_peak

                # Calculate trailing stop based on peak P&L
                # Logic: Current Stop = Initial Stop + Peak P&L
                # The stop trails UP from initial stop level by the amount of peak profit
                # Example: Initial=-860, Peak=100 -> Stop = -860 + 100 = -760
                current_stop = strategy.trailing_sl_initial_stop + current_peak
                logger.debug(f"[TSL] Strategy {strategy.name}: Initial={strategy.trailing_sl_initial_stop:.2f} + Peak={current_peak:.2f} = Stop={current_stop:.2f}")

                # Ratchet: current stop can only increase from previous value
                previous_stop = strategy.trailing_sl_trigger_pnl or strategy.trailing_sl_initial_stop
                if current_stop > previous_stop:
                    logger.debug(f"[TSL] Strategy {strategy.name}: Stop ratcheted UP from {previous_stop:.2f} to {current_stop:.2f}")

                current_stop = max(current_stop, previous_stop)

                strategy.trailing_sl_trigger_pnl = current_stop
                tsl_status['trigger_pnl'] = current_stop
                tsl_status['initial_stop'] = strategy.trailing_sl_initial_stop
                tsl_status['current_stop'] = current_stop

                # Exit when P&L drops to or below current stop
                if total_pnl <= current_stop:
                    tsl_status['should_exit'] = True
                    logger.warning(f"[TSL STATE] Strategy {strategy.name}: ACTIVE -> TRIGGERED | P&L {total_pnl:.2f} <= Stop {current_stop:.2f} (Peak: {current_peak:.2f}, Initial: {strategy.trailing_sl_initial_stop:.2f})")
        else:
            # No open positions, reset TSL tracking completely
            tsl_status['no_positions'] = True
            strategy.trailing_sl_active = False
            strategy.trailing_sl_peak_pnl = 0.0
            strategy.trailing_sl_initial_stop = None
            strategy.trailing_sl_trigger_pnl = None

    # Save unrealized P&L and TSL state to database with error handling for database locks
    try:
        db.session.commit()
    except Exception as e:
        logger.debug(f"DB commit error in positions (will continue): {e}")
        db.session.rollback()

    return jsonify({
        'status': 'success',
        'data': data,
        'total_pnl': total_pnl,
        'tsl_status': tsl_status
    })

@strategy_bp.route('/<int:strategy_id>/close-all', methods=['POST'])
@login_required
@api_rate_limit()
def close_all_positions(strategy_id):
    """Close all open positions for a strategy - PARALLEL EXECUTION

    Optional: Pass account_id in request body to close positions for specific account only
    """
    import threading

    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Check if account_id is provided in request (for account-specific closing)
        # Use silent=True to avoid errors when no JSON body is sent
        request_data = request.get_json(silent=True) or {}
        account_id = request_data.get('account_id') if request_data else None

        # Build query for open positions
        query = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        )

        # If account_id provided, filter by that account only
        if account_id:
            query = query.filter_by(account_id=account_id)
            logger.debug(f"Closing strategy {strategy_id} positions for account {account_id} only")
        else:
            logger.debug(f"Closing strategy {strategy_id} positions for ALL accounts")

        # Get all open positions (exclude rejected/cancelled)
        open_positions = query.all()

        # Filter out rejected/cancelled orders
        open_positions = [
            pos for pos in open_positions
            if not (hasattr(pos, 'broker_order_status') and pos.broker_order_status in ['rejected', 'cancelled'])
        ]

        if not open_positions:
            return jsonify({
                'status': 'error',
                'message': 'No open positions to close'
            }), 400

        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app import create_app

        # Thread-safe results collection
        results = []
        results_lock = threading.Lock()

        def close_position_worker(position, strategy_name, product_type, thread_index, user_id):
            """Worker function to close a single position in parallel"""
            import time

            # Add staggered delay based on thread index to prevent OpenAlgo race condition
            # Each thread waits: index * 300ms (0ms, 300ms, 600ms, 900ms, ...)
            # This GUARANTEES threads never hit OpenAlgo at the same time
            delay = thread_index * 0.3
            if delay > 0:
                time.sleep(delay)
                logger.debug(f"[THREAD {thread_index}] Waited {delay:.2f}s to prevent race condition")

            # Create Flask app context for this thread
            app = create_app()

            with app.app_context():
                try:
                    logger.debug(f"[THREAD] Closing position: {position.symbol} on account {position.account.account_name}, leg {position.leg.leg_number}")

                    # Reverse the position
                    client = ExtendedOpenAlgoAPI(
                        api_key=position.account.get_api_key(),
                        host=position.account.host_url
                    )

                    # Reverse action for closing
                    close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

                    logger.debug(f"[THREAD] Placing close order: {close_action} {position.quantity} {position.symbol} on {position.exchange}")

                    # Place close order with freeze-aware placement and retry logic
                    from app.utils.freeze_quantity_handler import place_order_with_freeze_check

                    max_retries = 3
                    retry_delay = 1
                    response = None
                    last_error = None

                    for attempt in range(max_retries):
                        try:
                            # Use freeze-aware order placement for close orders
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
                            last_error = str(api_error)
                            logger.warning(f"[RETRY] Close order attempt {attempt + 1}/{max_retries} failed: {last_error}")
                            if attempt < max_retries - 1:
                                import time as time_sleep
                                time_sleep.sleep(retry_delay)
                                retry_delay *= 2
                            else:
                                response = {'status': 'error', 'message': f'API error after {max_retries} retries: {last_error}'}

                    logger.debug(f"[THREAD] Close order response for {position.symbol}: {response}")

                    if response and response.get('status') == 'success':
                        # Get exit order ID from response
                        exit_order_id = response.get('orderid')

                        # Get fresh position from database to avoid stale data
                        position_to_update = StrategyExecution.query.get(position.id)

                        if position_to_update:
                            # Update position status to exit_pending (poller will update to 'exited' with actual fill price)
                            position_to_update.status = 'exit_pending'
                            position_to_update.exit_order_id = exit_order_id  # Store the real exit order ID
                            position_to_update.exit_time = datetime.utcnow()
                            position_to_update.exit_reason = 'manual_close'
                            position_to_update.broker_order_status = 'open'  # Will be updated by poller

                            # Set preliminary exit price from LTP (will be updated by poller with actual fill price)
                            try:
                                quote = client.quotes(symbol=position.symbol, exchange=position.exchange)
                                position_to_update.exit_price = float(quote.get('data', {}).get('ltp', 0))
                            except Exception as quote_error:
                                logger.warning(f"[THREAD] Failed to fetch exit price for {position.symbol}: {quote_error}")
                                position_to_update.exit_price = position_to_update.entry_price  # Fallback to entry price

                            # Calculate preliminary realized P&L (will be updated by poller with actual fill price)
                            if position.leg.action == 'BUY':
                                position_to_update.realized_pnl = (position_to_update.exit_price - position_to_update.entry_price) * position_to_update.quantity
                            else:
                                position_to_update.realized_pnl = (position_to_update.entry_price - position_to_update.exit_price) * position_to_update.quantity

                            # Commit position update
                            db.session.commit()

                            # Add exit order to poller to get actual fill price (same as entry orders)
                            from app.utils.order_status_poller import order_status_poller
                            order_status_poller.add_order(
                                execution_id=position_to_update.id,
                                account=position.account,
                                order_id=exit_order_id,
                                strategy_name=position.strategy.name if position.strategy else 'Unknown'
                            )

                            logger.debug(f"[THREAD SUCCESS] Exit order placed for {position.symbol}, order_id: {exit_order_id} (polling for fill price)")

                            with results_lock:
                                results.append({
                                    'symbol': position.symbol,
                                    'account': position.account.account_name,
                                    'status': 'success',
                                    'pnl': position_to_update.realized_pnl
                                })
                        else:
                            logger.error(f"[THREAD] Position {position.id} not found in database")
                            with results_lock:
                                results.append({
                                    'symbol': position.symbol,
                                    'account': position.account.account_name,
                                    'status': 'error',
                                    'error': 'Position not found in database'
                                })
                    else:
                        error_msg = response.get('message', 'Unknown error') if response else 'No response from API'
                        logger.error(f"[THREAD FAILED] Failed to close {position.symbol}: {error_msg}")

                        with results_lock:
                            results.append({
                                'symbol': position.symbol,
                                'account': position.account.account_name,
                                'status': 'failed',
                                'error': error_msg
                            })

                except Exception as e:
                    logger.error(f"[THREAD ERROR] Exception closing position {position.symbol}: {str(e)}", exc_info=True)

                    with results_lock:
                        results.append({
                            'symbol': position.symbol,
                            'account': getattr(position.account, 'account_name', 'unknown'),
                            'status': 'error',
                            'error': str(e)
                        })

        # Create and start threads for parallel execution with staggered delays
        threads = []
        logger.debug(f"[PARALLEL CLOSE] Starting parallel close for {len(open_positions)} positions")

        for idx, position in enumerate(open_positions):
            thread = threading.Thread(
                target=close_position_worker,
                args=(position, strategy.name, strategy.product_order_type, idx, strategy.user_id),
                name=f"ClosePosition_{position.symbol}_{position.account.account_name}"
            )
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join(timeout=30)  # 30 second timeout per thread

        logger.debug(f"[PARALLEL CLOSE] All threads completed. Results: {len(results)}")

        # Calculate summary
        total_pnl = sum(r.get('pnl', 0) for r in results if r.get('status') == 'success')
        successful = len([r for r in results if r.get('status') == 'success'])
        failed = len([r for r in results if r.get('status') in ['failed', 'error']])

        return jsonify({
            'status': 'success',
            'message': f'Closed {successful}/{len(open_positions)} positions (parallel execution)',
            'total_pnl': total_pnl,
            'successful': successful,
            'failed': failed,
            'results': results
        })

    except Exception as e:
        logger.error(f"Error closing positions for strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/close-position', methods=['POST'])
@login_required
@api_rate_limit()
def close_individual_position(strategy_id):
    """Close a specific position for a strategy"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        data = request.get_json()
        symbol = data.get('symbol')
        exchange = data.get('exchange')
        product = data.get('product')
        account_id = data.get('account_id')

        if not all([symbol, exchange, product, account_id]):
            return jsonify({
                'status': 'error',
                'message': 'Missing required parameters'
            }), 400

        # Find the open position in database
        # Note: product is stored in the leg, not in StrategyExecution
        position = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            symbol=symbol,
            exchange=exchange,
            account_id=account_id,
            status='entered'
        ).first()

        if not position:
            return jsonify({
                'status': 'error',
                'message': 'Position not found or already closed in database'
            }), 404

        # Check if order was rejected/cancelled
        if hasattr(position, 'broker_order_status') and position.broker_order_status in ['rejected', 'cancelled']:
            return jsonify({
                'status': 'error',
                'message': 'Cannot close rejected/cancelled order'
            }), 400

        # Get account details first for API access
        account = TradingAccount.query.get(account_id)
        if not account or account.user_id != current_user.id:
            return jsonify({
                'status': 'error',
                'message': 'Account not found'
            }), 404

        # Verify position exists at broker level before attempting to close
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )

        # Get the actual product type - use strategy's product_order_type or default to MIS
        # Note: leg.product_type might be 'options'/'futures', not the actual order product type
        valid_products = ['MIS', 'NRML', 'CNC']
        position_product = strategy.product_order_type.upper() if strategy.product_order_type else 'MIS'
        if position_product not in valid_products:
            position_product = 'MIS'  # Default to MIS for intraday

        # Try to verify position at broker level (optional - don't block close if verification fails)
        try:
            positionbook_response = client.positionbook()

            if positionbook_response.get('status') == 'success':
                broker_positions = positionbook_response.get('data', [])
                position_found = False
                actual_quantity = 0

                for broker_pos in broker_positions:
                    broker_symbol = broker_pos.get('symbol', '')
                    broker_exchange = broker_pos.get('exchange', '')

                    if broker_symbol == symbol and broker_exchange == exchange:
                        qty = int(broker_pos.get('quantity', 0))
                        if qty != 0:
                            position_found = True
                            actual_quantity = qty
                            logger.debug(f"Found position at broker: {broker_symbol}, qty={qty}")
                            break

                if position_found:
                    # Update quantity if different
                    if abs(actual_quantity) != position.quantity:
                        logger.warning(f"Quantity mismatch: DB={position.quantity}, Broker={abs(actual_quantity)}")
                        position.quantity = abs(actual_quantity)
                        db.session.commit()
                else:
                    # Position not found at broker - log but still attempt close
                    logger.warning(f"Position {symbol} not found in broker positionbook. Will still attempt close order.")

        except Exception as e:
            logger.warning(f"Error verifying position at broker: {e}. Continuing with close attempt.")

        from app.utils.freeze_quantity_handler import place_order_with_freeze_check
        import time

        # Reverse action for closing
        close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

        logger.debug(f"Closing individual position: {close_action} {position.quantity} {symbol} on {exchange}")

        # Place close order with freeze-aware placement and retry logic
        max_retries = 3
        retry_delay = 1
        response = None
        last_error = None

        for attempt in range(max_retries):
            try:
                response = place_order_with_freeze_check(
                    client=client,
                    user_id=current_user.id,
                    strategy=strategy.name,
                    symbol=symbol,
                    exchange=exchange,
                    action=close_action,
                    quantity=position.quantity,
                    price_type='MARKET',
                    product=position_product
                )
                if response and isinstance(response, dict):
                    break
            except Exception as api_error:
                last_error = str(api_error)
                logger.warning(f"[RETRY] Close position attempt {attempt + 1}/{max_retries} failed: {last_error}")
                if attempt < max_retries - 1:
                    time.sleep(retry_delay)
                    retry_delay *= 2
                else:
                    response = {'status': 'error', 'message': f'API error after {max_retries} retries: {last_error}'}

        logger.debug(f"Close position response for {symbol}: {response}")

        if response and response.get('status') == 'success':
            # Get exit order ID from response
            exit_order_id = response.get('orderid')

            # Update position status to exit_pending (poller will update to 'exited' with actual fill price)
            position.status = 'exit_pending'
            position.exit_order_id = exit_order_id
            position.exit_time = datetime.utcnow()
            position.exit_reason = 'manual_close'
            position.broker_order_status = 'open'  # Will be updated by poller

            # Set preliminary exit price from LTP (will be updated by poller with actual fill price)
            try:
                quote = client.quotes(symbol=symbol, exchange=exchange)
                position.exit_price = float(quote.get('data', {}).get('ltp', 0))
            except Exception as quote_error:
                logger.warning(f"Failed to fetch exit price for {symbol}: {quote_error}")
                position.exit_price = position.entry_price

            # Calculate preliminary P&L (will be updated by poller with actual fill price)
            if position.leg.action == 'BUY':
                pnl = (position.exit_price - position.entry_price) * position.quantity
            else:
                pnl = (position.entry_price - position.exit_price) * position.quantity

            position.realized_pnl = pnl

            db.session.commit()

            # Add exit order to poller to get actual fill price (same as entry orders)
            from app.utils.order_status_poller import order_status_poller
            order_status_poller.add_order(
                execution_id=position.id,
                account=position.account,
                order_id=exit_order_id,
                strategy_name=position.strategy.name if position.strategy else 'Unknown'
            )

            logger.debug(f"Exit order placed for {symbol}, order_id: {exit_order_id} (polling for fill price)")

            return jsonify({
                'status': 'success',
                'message': f'Position close order placed for {symbol}',
                'pnl': pnl,
                'orderid': exit_order_id
            })
        else:
            error_msg = response.get('message', 'Failed to place close order') if response else 'No response from broker'
            logger.error(f"Failed to close position {symbol}: {error_msg}")
            return jsonify({
                'status': 'error',
                'message': error_msg
            }), 400

    except Exception as e:
        logger.error(f"Error closing individual position: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/<int:strategy_id>/close-leg', methods=['POST'])
@login_required
@api_rate_limit()
def close_leg_all_accounts(strategy_id):
    """Close a specific leg across all accounts"""
    import threading

    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        data = request.get_json()
        leg_number = data.get('leg_number')

        if not leg_number:
            return jsonify({
                'status': 'error',
                'message': 'Leg number is required'
            }), 400

        # Find the leg
        leg = StrategyLeg.query.filter_by(
            strategy_id=strategy_id,
            leg_number=leg_number
        ).first()

        if not leg:
            return jsonify({
                'status': 'error',
                'message': f'Leg {leg_number} not found'
            }), 404

        # Get all open positions for this leg
        open_positions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            leg_id=leg.id,
            status='entered'
        ).all()

        # Filter out rejected/cancelled orders
        open_positions = [
            pos for pos in open_positions
            if not (hasattr(pos, 'broker_order_status') and pos.broker_order_status in ['rejected', 'cancelled'])
        ]

        if not open_positions:
            return jsonify({
                'status': 'error',
                'message': f'No open positions found for Leg {leg_number}'
            }), 400

        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app import create_app

        # Thread-safe results collection
        results = []
        results_lock = threading.Lock()

        def close_leg_position_worker(position, strategy_name, product_type, thread_index, user_id):
            """Worker function to close a leg position in parallel"""
            import time

            # Store position ID for reloading in this thread's session
            position_id = position.id

            # Add staggered delay to prevent race condition
            delay = thread_index * 0.3
            if delay > 0:
                time.sleep(delay)
                logger.debug(f"[THREAD {thread_index}] Waited {delay:.2f}s to prevent race condition")

            # Create Flask app context for this thread
            app = create_app()

            with app.app_context():
                try:
                    # CRITICAL FIX: Reload position in this thread's session
                    # The position object passed from main thread is attached to a different session
                    # We must reload it here to ensure changes persist when committed
                    position = StrategyExecution.query.get(position_id)
                    if not position:
                        logger.error(f"[THREAD] Position {position_id} not found in database")
                        return

                    logger.debug(f"[THREAD] Closing leg position: {position.symbol} on account {position.account.account_name}, leg {position.leg.leg_number}")

                    # Create API client
                    client = ExtendedOpenAlgoAPI(
                        api_key=position.account.get_api_key(),
                        host=position.account.host_url
                    )

                    # Use strategy's product_order_type (MIS/NRML), not leg's product_type (options/futures)
                    # This matches the close_all_positions behavior
                    position_product = product_type or 'MIS'

                    # Try to verify position at broker level (optional - don't block close if verification fails)
                    try:
                        positionbook_response = client.positionbook()

                        if positionbook_response.get('status') == 'success':
                            broker_positions = positionbook_response.get('data', [])
                            position_found = False
                            actual_quantity = 0

                            for broker_pos in broker_positions:
                                broker_symbol = broker_pos.get('symbol', '')
                                broker_exchange = broker_pos.get('exchange', '')
                                broker_product = broker_pos.get('product', '').upper()  # Case-insensitive comparison

                                # Match symbol and exchange (product can vary)
                                if (broker_symbol == position.symbol and
                                    broker_exchange == position.exchange):
                                    qty = int(broker_pos.get('quantity', 0))
                                    if qty != 0:
                                        position_found = True
                                        actual_quantity = qty
                                        logger.debug(f"[THREAD] Found position at broker: {broker_symbol}, qty={qty}, product={broker_product}")
                                        break

                            if position_found:
                                # Update quantity if different
                                if abs(actual_quantity) != position.quantity:
                                    logger.warning(f"[THREAD] Quantity mismatch: DB={position.quantity}, Broker={abs(actual_quantity)}")
                                    position.quantity = abs(actual_quantity)
                                    db.session.commit()
                            else:
                                # Position not found at broker - log but still attempt close
                                # The position might have a different format or already be closed
                                logger.warning(f"[THREAD] Position {position.symbol} not found in broker positionbook. Will still attempt close order.")

                    except Exception as e:
                        logger.warning(f"[THREAD] Error verifying position at broker: {e}. Continuing with close attempt.")

                    # Reverse action for closing
                    close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

                    logger.debug(f"[THREAD] Placing close order: {close_action} {position.quantity} {position.symbol} on {position.exchange}")

                    # Place close order with freeze-aware placement and retry logic
                    from app.utils.freeze_quantity_handler import place_order_with_freeze_check

                    max_retries = 3
                    retry_delay = 1
                    response = None
                    last_error = None

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
                                product=position_product
                            )
                            if response and isinstance(response, dict):
                                break
                        except Exception as api_error:
                            last_error = str(api_error)
                            logger.warning(f"[RETRY] Close leg order attempt {attempt + 1}/{max_retries} failed: {last_error}")
                            if attempt < max_retries - 1:
                                time.sleep(retry_delay)
                                retry_delay *= 2
                            else:
                                response = {'status': 'error', 'message': f'API error after {max_retries} retries: {last_error}'}

                    logger.debug(f"[THREAD] Close leg order response for {position.symbol}: {response}")

                    if response and response.get('status') == 'success':
                        # Get exit order ID from response
                        exit_order_id = response.get('orderid')

                        # Get fresh position from database to avoid stale data
                        position_to_update = StrategyExecution.query.get(position.id)

                        if position_to_update:
                            # Update position status - set to exit_pending, poller will update to exited with actual fill price
                            position_to_update.status = 'exit_pending'
                            position_to_update.exit_order_id = exit_order_id
                            position_to_update.exit_time = datetime.utcnow()
                            position_to_update.exit_reason = 'manual_leg_close'
                            position_to_update.broker_order_status = 'open'

                            # Commit the pending status
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
                                    'status': 'success',
                                    'symbol': position.symbol,
                                    'account': position.account.account_name,
                                    'pnl': 0,  # P&L will be calculated by poller when fill price is received
                                    'orderid': exit_order_id
                                })

                            logger.debug(f"[THREAD] Exit order placed for leg position: {position.symbol}, Exit Order: {exit_order_id} (awaiting fill price from poller)")
                        else:
                            logger.error(f"[THREAD] Position {position.id} not found in database after close")
                            with results_lock:
                                results.append({
                                    'status': 'error',
                                    'symbol': position.symbol,
                                    'account': position.account.account_name,
                                    'error': 'Position not found in database'
                                })
                    else:
                        error_msg = response.get('message', 'Failed to place close order') if response else 'No response from broker'
                        with results_lock:
                            results.append({
                                'status': 'failed',
                                'symbol': position.symbol,
                                'account': position.account.account_name,
                                'error': error_msg
                            })
                        logger.error(f"[THREAD] Failed to close leg position: {position.symbol}, Error: {error_msg}")

                except Exception as e:
                    with results_lock:
                        results.append({
                            'status': 'error',
                            'symbol': position.symbol,
                            'account': position.account.account_name,
                            'error': str(e)
                        })
                    logger.error(f"[THREAD] Error closing leg position: {e}", exc_info=True)

        # Create and start threads for parallel execution
        threads = []
        logger.debug(f"[PARALLEL LEG CLOSE] Starting parallel close for leg {leg_number} ({len(open_positions)} positions)")

        for idx, position in enumerate(open_positions):
            thread = threading.Thread(
                target=close_leg_position_worker,
                args=(position, strategy.name, strategy.product_order_type, idx, strategy.user_id),
                name=f"CloseLeg{leg_number}_{position.symbol}_{position.account.account_name}"
            )
            threads.append(thread)
            thread.start()

        # Wait for all threads to complete
        for thread in threads:
            thread.join(timeout=30)

        logger.debug(f"[PARALLEL LEG CLOSE] All threads completed. Results: {len(results)}")

        # Calculate summary
        total_pnl = sum(r.get('pnl', 0) for r in results if r.get('status') == 'success')
        successful = len([r for r in results if r.get('status') == 'success'])
        failed = len([r for r in results if r.get('status') in ['failed', 'error']])

        return jsonify({
            'status': 'success',
            'message': f'Closed Leg {leg_number}: {successful}/{len(open_positions)} positions closed successfully',
            'total_pnl': total_pnl,
            'successful': successful,
            'failed': failed,
            'results': results
        })

    except Exception as e:
        logger.error(f"Error closing leg {leg_number} for strategy {strategy_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/toggle/<int:strategy_id>', methods=['POST'])
@login_required
def toggle_strategy(strategy_id):
    """Toggle strategy active status"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        strategy.is_active = not strategy.is_active
        db.session.commit()

        return jsonify({
            'status': 'success',
            'is_active': strategy.is_active,
            'message': f'Strategy {"activated" if strategy.is_active else "deactivated"}'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error toggling strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/api/lot-sizes')
@login_required
def get_lot_sizes():
    """Get lot sizes for all instruments from trading settings"""
    from app.models import TradingSettings

    # Get user's trading settings
    settings = TradingSettings.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    # Create a dictionary of lot sizes
    lot_sizes = {}
    for setting in settings:
        lot_sizes[setting.symbol] = setting.lot_size

    # Add defaults for any missing instruments
    defaults = {
        'NIFTY': 75,
        'BANKNIFTY': 35,
        'FINNIFTY': 65,
        'MIDCPNIFTY': 75,
        'SENSEX': 20
    }

    for symbol, default_size in defaults.items():
        if symbol not in lot_sizes:
            lot_sizes[symbol] = default_size

    return jsonify(lot_sizes)

@strategy_bp.route('/templates')
@login_required
def templates():
    """View strategy templates"""
    # Get public templates
    public_templates = Strategy.query.filter_by(is_template=True).all()

    # Get user's templates
    user_templates = Strategy.query.filter_by(
        user_id=current_user.id,
        is_template=True
    ).all()

    return render_template('strategy/templates.html',
                         public_templates=public_templates,
                         user_templates=user_templates)

@strategy_bp.route('/save_template/<int:strategy_id>', methods=['POST'])
@login_required
def save_as_template(strategy_id):
    """Save strategy as template"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        strategy.is_template = True
        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Strategy saved as template'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error saving template {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500

@strategy_bp.route('/positions/<int:strategy_id>')
@login_required
def get_positions(strategy_id):
    """Get current positions for a strategy"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

        # Filter out rejected/cancelled orders
        executions = [
            exec for exec in executions
            if not (hasattr(exec, 'broker_order_status') and exec.broker_order_status in ['rejected', 'cancelled'])
        ]

        positions = []
        for execution in executions:
            positions.append({
                'id': execution.id,
                'symbol': execution.symbol,
                'exchange': execution.exchange,
                'quantity': execution.quantity,
                'entry_price': execution.entry_price,
                'current_pnl': execution.unrealized_pnl,
                'account': execution.account.account_name if execution.account else 'Unknown'
            })

        return jsonify({
            'status': 'success',
            'positions': positions
        })

    except Exception as e:
        logger.error(f"Error getting positions for strategy {strategy_id}: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500