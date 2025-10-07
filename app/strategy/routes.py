from flask import render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.strategy import strategy_bp
from app.models import Strategy, StrategyLeg, StrategyExecution, TradingAccount
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit
from app.utils.strategy_executor import StrategyExecutor
from datetime import datetime
import json
import logging

logger = logging.getLogger(__name__)

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

    today_pnl = sum(e.realized_pnl or 0 for e in today_executions if e.realized_pnl)

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
            'trailing_sl': strategy.trailing_sl
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
        logger.info(f"Loading strategy {strategy_id} with {len(strategy.legs_list)} legs")

        # Log details for debugging
        for leg in strategy.legs_list:
            logger.debug(f"Leg {leg.leg_number}: {leg.instrument} {leg.action} {leg.option_type}")

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
            strategy.selected_accounts = data.get('selected_accounts', [])
            strategy.allocation_type = data.get('allocation_type', 'equal')
            strategy.max_loss = data.get('max_loss')
            strategy.max_profit = data.get('max_profit')
            strategy.trailing_sl = data.get('trailing_sl')

            # Save strategy first to get ID
            db.session.flush()

            # Delete existing legs if updating
            if strategy_id:
                StrategyLeg.query.filter_by(strategy_id=strategy.id).delete()

            # Add strategy legs
            for i, leg_data in enumerate(data.get('legs', [])):
                # Log the received data for debugging
                logger.info(f"Saving leg {i+1}: instrument={leg_data.get('instrument')}, "
                           f"lots={leg_data.get('lots')}, quantity={leg_data.get('quantity')}")

                leg = StrategyLeg(
                    strategy_id=strategy.id,
                    leg_number=i + 1,
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
                    lots=leg_data.get('lots', 1),
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

    return render_template('strategy/builder.html',
                         strategy=strategy,
                         strategy_legs=legs_data,
                         accounts=accounts)

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

        # Check if accounts are selected
        if not strategy.selected_accounts:
            return jsonify({
                'status': 'error',
                'message': 'No accounts selected for strategy'
            }), 400

        logger.info(f"Executing strategy {strategy_id} ({strategy.name}) with {leg_count} legs")

        # Check if any leg has explicit quantity set
        has_explicit_quantities = any(leg.quantity and leg.quantity > 0 for leg in strategy.legs)

        # If explicit quantities are set, disable margin calculator
        use_margin_calc = not has_explicit_quantities

        if has_explicit_quantities:
            logger.info(f"Strategy {strategy_id}: Using explicit quantities, bypassing margin calculator")
        else:
            logger.info(f"Strategy {strategy_id}: Using margin calculator for lot sizing")

        # Initialize strategy executor
        executor = StrategyExecutor(strategy, use_margin_calculator=use_margin_calc)

        # Execute strategy
        results = executor.execute()

        # Count successful, failed, and skipped executions
        successful = sum(1 for r in results if r.get('status') == 'success')
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
            message = f'Strategy executed successfully: {successful} orders placed'
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

        # Get active executions
        active_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

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

        # Check for active positions
        active_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

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

        # Delete all executions first (cascade should handle this, but explicit is better)
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
    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Get all executions for this strategy
    executions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id
    ).join(TradingAccount).join(StrategyLeg).all()

    orders = []
    for execution in executions:
        # Use actual broker order status if available, otherwise map from execution status
        if hasattr(execution, 'broker_order_status') and execution.broker_order_status:
            order_status = execution.broker_order_status
        else:
            # Fallback to mapping from execution status
            order_status = execution.status
            if order_status == 'entered':
                order_status = 'complete'
            elif order_status == 'exited':
                order_status = 'complete'
            elif order_status == 'failed':
                order_status = 'rejected'
            elif order_status == 'pending':
                order_status = 'open'

        orders.append({
            'account_name': execution.account.account_name if execution.account else 'N/A',
            'broker_name': execution.account.broker_name if execution.account else 'N/A',
            'action': execution.leg.action,
            'symbol': execution.symbol,
            'exchange': execution.exchange,
            'orderid': execution.order_id or f"STG_{execution.id}",
            'product': execution.leg.product_type.upper() if execution.leg.product_type else 'MIS',
            'quantity': str(execution.quantity),
            'price': execution.entry_price or 0.0,
            'pricetype': execution.leg.order_type or 'MARKET',
            'order_status': order_status,
            'trigger_price': 0.0,
            'timestamp': execution.entry_time.strftime('%d-%b-%Y %H:%M:%S') if execution.entry_time else ""
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
    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Get executed trades
    trades = StrategyExecution.query.filter(
        StrategyExecution.strategy_id == strategy_id,
        StrategyExecution.status.in_(['entered', 'exited'])
    ).join(TradingAccount).join(StrategyLeg).all()

    data = []
    for trade in trades:
        # Calculate average price and trade value
        avg_price = trade.entry_price or 0.0
        if trade.exit_price and trade.status == 'exited':
            avg_price = (trade.entry_price + trade.exit_price) / 2

        trade_value = avg_price * trade.quantity

        data.append({
            'account_name': trade.account.account_name if trade.account else 'N/A',
            'broker_name': trade.account.broker_name if trade.account else 'N/A',
            'action': trade.leg.action,
            'symbol': trade.symbol,
            'exchange': trade.exchange,
            'orderid': trade.order_id or f"STG_{trade.id}",
            'product': trade.leg.product_type.upper() if trade.leg.product_type else 'MIS',
            'quantity': 0.0,  # OpenAlgo format shows 0.0 for executed trades
            'average_price': avg_price,
            'timestamp': trade.entry_time.strftime('%H:%M:%S') if trade.entry_time else "",
            'trade_value': trade_value
        })

    return jsonify({
        'status': 'success',
        'data': data
    })

@strategy_bp.route('/<int:strategy_id>/positions', methods=['GET'])
@login_required
def strategy_positions(strategy_id):
    """Get strategy-level open positions (OpenAlgo format)"""
    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Get open positions
    positions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id,
        status='entered'
    ).join(TradingAccount).join(StrategyLeg).all()

    from app.utils.openalgo_client import ExtendedOpenAlgoAPI

    data = []
    for position in positions:
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

        data.append({
            'account_name': position.account.account_name if position.account else 'N/A',
            'broker_name': position.account.broker_name if position.account else 'N/A',
            'symbol': position.symbol,
            'exchange': position.exchange,
            'product': position.leg.product_type.upper() if position.leg.product_type else 'MIS',
            'quantity': str(quantity),  # String format, negative for sell
            'average_price': str(position.entry_price or 0.0),
            'ltp': str(ltp),
            'pnl': str(round(pnl, 2))
        })

    # Save unrealized P&L to database
    db.session.commit()

    return jsonify({
        'status': 'success',
        'data': data
    })

@strategy_bp.route('/<int:strategy_id>/close-all', methods=['POST'])
@login_required
@api_rate_limit()
def close_all_positions(strategy_id):
    """Close all open positions for a strategy"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Get all open positions
        open_positions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).all()

        if not open_positions:
            return jsonify({
                'status': 'error',
                'message': 'No open positions to close'
            }), 400

        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

        results = []
        for position in open_positions:
            try:
                # Reverse the position
                client = ExtendedOpenAlgoAPI(
                    api_key=position.account.get_api_key(),
                    host=position.account.host_url
                )

                # Reverse action for closing
                close_action = 'SELL' if position.leg.action == 'BUY' else 'BUY'

                response = client.placeorder(
                    strategy=f"Close_{strategy.name}",
                    symbol=position.symbol,
                    exchange=position.exchange,
                    action=close_action,
                    quantity=position.quantity,
                    price_type='MARKET',
                    product='MIS'
                )

                if response.get('status') == 'success':
                    # Update position status
                    position.status = 'exited'
                    position.exit_time = datetime.utcnow()
                    position.exit_reason = 'manual_close'

                    # Get exit price
                    quote = client.quotes(symbol=position.symbol, exchange=position.exchange)
                    position.exit_price = float(quote.get('data', {}).get('ltp', 0))

                    # Calculate realized P&L
                    if position.leg.action == 'BUY':
                        position.realized_pnl = (position.exit_price - position.entry_price) * position.quantity
                    else:
                        position.realized_pnl = (position.entry_price - position.exit_price) * position.quantity

                    results.append({
                        'symbol': position.symbol,
                        'status': 'success',
                        'pnl': position.realized_pnl
                    })
                else:
                    results.append({
                        'symbol': position.symbol,
                        'status': 'failed',
                        'error': response.get('message')
                    })

            except Exception as e:
                results.append({
                    'symbol': position.symbol,
                    'status': 'error',
                    'error': str(e)
                })

        db.session.commit()

        total_pnl = sum(r.get('pnl', 0) for r in results if r.get('status') == 'success')
        successful = len([r for r in results if r.get('status') == 'success'])

        return jsonify({
            'status': 'success',
            'message': f'Closed {successful}/{len(open_positions)} positions',
            'total_pnl': total_pnl,
            'results': results
        })

    except Exception as e:
        logger.error(f"Error closing positions for strategy {strategy_id}: {e}")
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