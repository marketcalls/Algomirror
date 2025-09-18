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

    return render_template('strategy/dashboard.html',
                         strategies=strategies,
                         accounts=accounts,
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

        # Initialize strategy executor
        executor = StrategyExecutor(strategy)

        # Execute strategy
        results = executor.execute()

        # Count successful and failed executions
        successful = sum(1 for r in results if r.get('status') == 'success')
        failed = sum(1 for r in results if r.get('status') in ['failed', 'error'])

        return jsonify({
            'status': 'success',
            'message': f'Strategy executed: {successful} successful, {failed} failed',
            'results': results,
            'summary': {
                'total_legs': leg_count,
                'accounts': len(strategy.selected_accounts),
                'successful_orders': successful,
                'failed_orders': failed
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
    """Delete a strategy"""
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Check for active positions
        active_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy_id,
            status='entered'
        ).count()

        if active_executions > 0:
            return jsonify({
                'status': 'error',
                'message': 'Cannot delete strategy with active positions'
            }), 400

        db.session.delete(strategy)
        db.session.commit()

        return jsonify({
            'status': 'success',
            'message': 'Strategy deleted successfully'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting strategy {strategy_id}: {e}")
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