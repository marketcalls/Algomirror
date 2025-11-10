from flask import render_template, redirect, url_for, current_app, jsonify, request
from flask_login import login_required, current_user
from app.main import main_bp
from app.models import TradingAccount, ActivityLog, User
from openalgo import api
from datetime import datetime
from sqlalchemy import desc
from app import db

@main_bp.route('/')
def index():
    if current_user.is_authenticated:
        return redirect(url_for('main.dashboard'))

    # Check if registration is available (single-user app - only if no users exist)
    registration_available = (User.query.count() == 0)

    return render_template('main/index.html', registration_available=registration_available)

@main_bp.route('/dashboard')
@login_required
def dashboard():
    """Strategy dashboard showing active strategies and account status (migrated from /strategy)"""
    from app.models import Strategy, StrategyExecution
    from datetime import datetime, timedelta

    # Get user's strategies
    strategies = Strategy.query.filter_by(user_id=current_user.id).order_by(Strategy.created_at.desc()).all()

    # Get user's active accounts
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    # If no accounts, redirect to add account page
    if not accounts:
        return redirect(url_for('accounts.add'))

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

    # Create mapping of account_id -> list of active strategy names (only with open positions)
    account_strategies = {}
    for account in accounts:
        account_strategies[account.id] = []
        for strategy in active_strategies:
            # Check if this account is in the strategy's selected_accounts
            if strategy.selected_accounts and account.id in strategy.selected_accounts:
                # Only include strategies with open positions for this account
                open_positions_count = StrategyExecution.query.filter_by(
                    strategy_id=strategy.id,
                    account_id=account.id,
                    status='entered'
                ).count()

                if open_positions_count > 0:
                    account_strategies[account.id].append({
                        'id': strategy.id,
                        'name': strategy.name,
                        'positions': open_positions_count
                    })

    current_app.logger.info(
        f'Dashboard accessed by user {current_user.username}',
        extra={
            'event': 'dashboard_access',
            'user_id': current_user.id,
            'accounts_count': len(accounts),
            'strategies_count': len(strategies)
        }
    )

    return render_template('main/dashboard.html',
                         strategies=strategies,
                         strategies_json=strategies_data,
                         accounts=accounts,
                         accounts_json=accounts_data,
                         today_pnl=today_pnl,
                         active_strategies=len(active_strategies),
                         account_strategies=account_strategies)

@main_bp.route('/account-positions')
@login_required
def account_positions():
    """Account-wise positions view with open strategies and close functionality"""
    from app.models import Strategy, StrategyExecution

    # Get account filter from query parameter
    selected_account_id = request.args.get('account', type=int)

    # Get user's active accounts
    all_accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    if not all_accounts:
        return redirect(url_for('accounts.add'))

    # Filter accounts based on selection
    if selected_account_id:
        accounts = [acc for acc in all_accounts if acc.id == selected_account_id]
        single_account = True
    else:
        accounts = all_accounts
        single_account = False

    # Build detailed account positions data
    accounts_with_positions = []

    for account in accounts:
        # Get all strategies with open positions for this account
        open_executions = StrategyExecution.query.filter_by(
            account_id=account.id,
            status='entered'
        ).join(Strategy).filter(
            Strategy.user_id == current_user.id
        ).all()

        # Group executions by strategy
        strategy_positions = {}
        total_unrealized_pnl = 0
        total_positions = 0

        for execution in open_executions:
            strategy_id = execution.strategy_id
            strategy = execution.strategy

            if strategy_id not in strategy_positions:
                strategy_positions[strategy_id] = {
                    'id': strategy.id,
                    'name': strategy.name,
                    'executions': [],
                    'total_pnl': 0,
                    'position_count': 0
                }

            # Add execution details
            strategy_positions[strategy_id]['executions'].append({
                'id': execution.id,
                'symbol': execution.symbol,
                'exchange': execution.exchange,
                'quantity': execution.quantity,
                'entry_price': execution.entry_price,
                'unrealized_pnl': execution.unrealized_pnl or 0,
                'entry_time': execution.entry_time
            })

            strategy_positions[strategy_id]['total_pnl'] += execution.unrealized_pnl or 0
            strategy_positions[strategy_id]['position_count'] += 1
            total_unrealized_pnl += execution.unrealized_pnl or 0
            total_positions += 1

        accounts_with_positions.append({
            'id': account.id,
            'account_name': account.account_name,
            'broker_name': account.broker_name,
            'connection_status': account.connection_status,
            'strategies': list(strategy_positions.values()),
            'total_unrealized_pnl': total_unrealized_pnl,
            'total_positions': total_positions
        })

    current_app.logger.info(
        f'Account positions page accessed by user {current_user.username}',
        extra={
            'event': 'account_positions_access',
            'user_id': current_user.id,
            'accounts_count': len(accounts)
        }
    )

    return render_template('main/account_positions.html',
                         accounts=accounts_with_positions,
                         all_accounts=all_accounts,
                         single_account=single_account,
                         selected_account_id=selected_account_id)

@main_bp.route('/account/<int:account_id>/close-all-positions', methods=['POST'])
@login_required
def close_account_positions(account_id):
    """Close all open positions for a specific account"""
    from app.models import Strategy, StrategyExecution, StrategyLeg
    from app.utils.openalgo_client import ExtendedOpenAlgoAPI

    # Verify account ownership
    account = TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id
    ).first()

    if not account:
        return jsonify({
            'status': 'error',
            'message': 'Account not found'
        }), 404

    # Get all open executions for this account
    open_executions = StrategyExecution.query.filter_by(
        account_id=account_id,
        status='entered'
    ).all()

    if not open_executions:
        return jsonify({
            'status': 'info',
            'message': 'No open positions to close'
        })

    # Initialize OpenAlgo client
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to connect to account: {str(e)}'
        }), 500

    # Close all positions
    closed_count = 0
    failed_count = 0
    total_pnl = 0
    errors = []

    for execution in open_executions:
        try:
            leg = StrategyLeg.query.get(execution.leg_id)
            if not leg:
                continue

            # Determine exit action (opposite of entry)
            exit_action = 'SELL' if leg.action == 'BUY' else 'BUY'

            # Place exit order
            order_response = client.placesmartorder(
                symbol=execution.symbol,
                action=exit_action,
                exchange=execution.exchange,
                price_type='MARKET',
                product='MIS',
                quantity=str(execution.quantity),
                position_size='0'
            )

            if order_response.get('status') == 'success':
                # Update execution record
                execution.status = 'exited'
                execution.exit_order_id = order_response.get('orderid')
                execution.exit_time = datetime.utcnow()
                execution.exit_reason = 'account_close_all'

                # Calculate P&L if available
                if execution.unrealized_pnl:
                    execution.realized_pnl = execution.unrealized_pnl
                    total_pnl += execution.unrealized_pnl

                closed_count += 1
            else:
                failed_count += 1
                errors.append(f"{execution.symbol}: {order_response.get('message', 'Unknown error')}")

        except Exception as e:
            failed_count += 1
            errors.append(f"{execution.symbol}: {str(e)}")
            current_app.logger.error(f'Error closing position {execution.id}: {str(e)}')

    # Commit all changes
    db.session.commit()

    # Log activity
    ActivityLog.query.filter_by().delete()  # Cleanup
    log = ActivityLog(
        user_id=current_user.id,
        account_id=account_id,
        action='close_all_account_positions',
        details={
            'closed': closed_count,
            'failed': failed_count,
            'total_pnl': total_pnl
        },
        status='success' if failed_count == 0 else 'partial'
    )
    db.session.add(log)
    db.session.commit()

    # Build response message
    if closed_count > 0 and failed_count == 0:
        return jsonify({
            'status': 'success',
            'message': f'Successfully closed {closed_count} position(s)',
            'total_pnl': total_pnl,
            'closed_count': closed_count
        })
    elif closed_count > 0 and failed_count > 0:
        return jsonify({
            'status': 'warning',
            'message': f'Closed {closed_count} position(s), but {failed_count} failed',
            'total_pnl': total_pnl,
            'closed_count': closed_count,
            'errors': errors[:5]  # Return first 5 errors
        })
    else:
        return jsonify({
            'status': 'error',
            'message': f'Failed to close any positions. {failed_count} error(s)',
            'errors': errors[:5]
        }), 500