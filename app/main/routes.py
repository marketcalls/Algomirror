from flask import render_template, redirect, url_for, current_app, jsonify, request
from flask_login import login_required, current_user
from app.main import main_bp
from app.models import TradingAccount, ActivityLog, User
from openalgo import api
from datetime import datetime
from sqlalchemy import desc
from app import db
import json

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

    # Calculate overall summary statistics
    total_active_accounts = len(accounts)

    # Count strategies with non-zero open positions
    strategies_with_positions = 0
    for strategy in strategies:
        open_positions_count = StrategyExecution.query.filter_by(
            strategy_id=strategy.id,
            status='entered'
        ).count()
        if open_positions_count > 0:
            strategies_with_positions += 1

    # Calculate total available cash across all accounts (will be updated via API in frontend)
    # These are placeholders for the frontend to populate
    overall_stats = {
        'total_active_accounts': total_active_accounts,
        'total_strategies': strategies_with_positions,  # Only count strategies with open positions
        'total_available_cash': 0,  # Will be calculated client-side
        'total_m2m_pnl': 0  # Will be calculated client-side
    }

    current_app.logger.debug(
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
                         account_strategies=account_strategies,
                         overall_stats=overall_stats)

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

    current_app.logger.debug(
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
    """Close all open positions for a specific account - PARALLEL EXECUTION

    Uses the same robust logic as strategy close-all:
    - Parallel execution with staggered delays
    - Freeze quantity handling
    - Retry logic for failed API calls
    - Proper P&L calculation from exit prices
    """
    import threading
    from app.models import Strategy, StrategyExecution, StrategyLeg
    from app.utils.openalgo_client import ExtendedOpenAlgoAPI
    from app.utils.freeze_quantity_handler import place_order_with_freeze_check
    from app import create_app

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

    # Filter out rejected/cancelled orders
    open_executions = [
        pos for pos in open_executions
        if not (hasattr(pos, 'broker_order_status') and pos.broker_order_status in ['rejected', 'cancelled'])
    ]

    if not open_executions:
        return jsonify({
            'status': 'info',
            'message': 'No open positions to close'
        })

    current_app.logger.debug(f"[ACCOUNT CLOSE ALL] Closing {len(open_executions)} positions for account {account_id}")

    # Thread-safe results collection
    results = []
    results_lock = threading.Lock()
    user_id = current_user.id

    def close_position_worker(execution, thread_index):
        """Worker function to close a single position in parallel"""
        import time as time_module

        # Add staggered delay based on thread index to prevent race conditions
        delay = thread_index * 0.3
        if delay > 0:
            time_module.sleep(delay)

        # Create Flask app context for this thread
        app = create_app()

        with app.app_context():
            try:
                # Get fresh execution from database
                exec_to_close = StrategyExecution.query.get(execution.id)
                if not exec_to_close:
                    with results_lock:
                        results.append({
                            'symbol': execution.symbol,
                            'status': 'error',
                            'error': 'Position not found'
                        })
                    return

                leg = StrategyLeg.query.get(exec_to_close.leg_id)
                if not leg:
                    with results_lock:
                        results.append({
                            'symbol': execution.symbol,
                            'status': 'error',
                            'error': 'Leg not found'
                        })
                    return

                # Get strategy for product type
                strategy = Strategy.query.get(exec_to_close.strategy_id)
                product_type = strategy.product_order_type if strategy else (exec_to_close.product or 'MIS')

                # Initialize client
                acct = TradingAccount.query.get(exec_to_close.account_id)
                client = ExtendedOpenAlgoAPI(
                    api_key=acct.get_api_key(),
                    host=acct.host_url
                )

                # Determine exit action (opposite of entry)
                exit_action = 'SELL' if leg.action == 'BUY' else 'BUY'

                current_app.logger.debug(f"[THREAD {thread_index}] Closing: {exit_action} {exec_to_close.quantity} {exec_to_close.symbol}")

                # Place close order with freeze-aware placement and retry logic
                max_retries = 3
                retry_delay = 1
                response = None

                for attempt in range(max_retries):
                    try:
                        response = place_order_with_freeze_check(
                            client=client,
                            user_id=user_id,
                            strategy=strategy.name if strategy else 'AccountClose',
                            symbol=exec_to_close.symbol,
                            exchange=exec_to_close.exchange,
                            action=exit_action,
                            quantity=exec_to_close.quantity,
                            price_type='MARKET',
                            product=product_type
                        )
                        if response and isinstance(response, dict):
                            break
                    except Exception as api_error:
                        current_app.logger.warning(f"[RETRY] Attempt {attempt + 1}/{max_retries} failed: {api_error}")
                        if attempt < max_retries - 1:
                            time_module.sleep(retry_delay)
                            retry_delay *= 2
                        else:
                            response = {'status': 'error', 'message': f'API error after {max_retries} retries'}

                if response and response.get('status') == 'success':
                    # Update execution record
                    exec_to_close.status = 'exited'
                    exec_to_close.exit_order_id = response.get('orderid')
                    exec_to_close.exit_time = datetime.utcnow()
                    exec_to_close.exit_reason = 'account_close_all'
                    exec_to_close.broker_order_status = 'complete'

                    # Fetch exit price
                    try:
                        quote = client.quotes(symbol=exec_to_close.symbol, exchange=exec_to_close.exchange)
                        exec_to_close.exit_price = float(quote.get('data', {}).get('ltp', 0))
                    except Exception:
                        exec_to_close.exit_price = exec_to_close.entry_price

                    # Calculate realized P&L
                    if leg.action == 'BUY':
                        exec_to_close.realized_pnl = (exec_to_close.exit_price - exec_to_close.entry_price) * exec_to_close.quantity
                    else:
                        exec_to_close.realized_pnl = (exec_to_close.entry_price - exec_to_close.exit_price) * exec_to_close.quantity

                    db.session.commit()

                    current_app.logger.debug(f"[THREAD {thread_index}] SUCCESS: {exec_to_close.symbol}, P&L: {exec_to_close.realized_pnl:.2f}")

                    with results_lock:
                        results.append({
                            'symbol': exec_to_close.symbol,
                            'status': 'success',
                            'pnl': exec_to_close.realized_pnl
                        })
                else:
                    error_msg = response.get('message', 'Unknown error') if response else 'No response'
                    current_app.logger.error(f"[THREAD {thread_index}] FAILED: {exec_to_close.symbol}: {error_msg}")

                    with results_lock:
                        results.append({
                            'symbol': exec_to_close.symbol,
                            'status': 'failed',
                            'error': error_msg
                        })

            except Exception as e:
                current_app.logger.error(f"[THREAD {thread_index}] ERROR: {execution.symbol}: {str(e)}", exc_info=True)
                with results_lock:
                    results.append({
                        'symbol': execution.symbol,
                        'status': 'error',
                        'error': str(e)
                    })

    # Create and start threads for parallel execution
    threads = []
    for idx, execution in enumerate(open_executions):
        thread = threading.Thread(
            target=close_position_worker,
            args=(execution, idx),
            name=f"AccountClose_{execution.symbol}"
        )
        threads.append(thread)
        thread.start()

    # Wait for all threads to complete
    for thread in threads:
        thread.join(timeout=30)

    # Calculate results
    successful = [r for r in results if r.get('status') == 'success']
    failed = [r for r in results if r.get('status') in ['failed', 'error']]
    total_pnl = sum(r.get('pnl', 0) for r in successful)
    closed_count = len(successful)
    failed_count = len(failed)

    current_app.logger.debug(f"[ACCOUNT CLOSE ALL] Completed: {closed_count}/{len(open_executions)} closed, P&L: {total_pnl:.2f}")

    # Log activity
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

    # Build response
    errors = [r.get('error') for r in failed if r.get('error')]

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
            'errors': errors[:5]
        })
    else:
        return jsonify({
            'status': 'error',
            'message': f'Failed to close any positions. {failed_count} error(s)',
            'errors': errors[:5]
        }), 500

@main_bp.route('/websocket-monitor')
@login_required
def websocket_monitor():
    """WebSocket monitor page showing active connections and subscriptions"""
    current_app.logger.debug(
        f'WebSocket monitor accessed by user {current_user.username}',
        extra={
            'event': 'websocket_monitor_access',
            'user_id': current_user.id
        }
    )

    return render_template('main/websocket_monitor.html')

@main_bp.route('/api/websocket-status')
@login_required
def websocket_status():
    """API endpoint to get current WebSocket status"""
    from app.utils.background_service import option_chain_service

    try:
        websocket_manager = option_chain_service.shared_websocket_manager

        if not websocket_manager:
            return jsonify({
                'status': 'not_initialized',
                'message': 'WebSocket manager not initialized'
            })

        # Get basic status
        ws_status = websocket_manager.get_status()

        # Get detailed subscription information
        subscriptions_list = []
        for sub_str in websocket_manager.subscriptions:
            try:
                subscription = json.loads(sub_str)
                subscriptions_list.append({
                    'symbol': subscription.get('symbol', 'N/A'),
                    'exchange': subscription.get('exchange', 'N/A'),
                    'mode': subscription.get('mode', 'ltp')
                })
            except:
                continue

        # Get connection pool details
        connection_details = None
        if websocket_manager.connection_pool:
            current_account = websocket_manager.connection_pool.get('current_account')
            backup_accounts = websocket_manager.connection_pool.get('backup_accounts', [])

            connection_details = {
                'current_account': {
                    'name': getattr(current_account, 'account_name', 'Unknown'),
                    'broker': getattr(current_account, 'broker_name', 'Unknown'),
                    'websocket_url': getattr(current_account, 'websocket_url', 'N/A')
                },
                'backup_accounts': [
                    {
                        'name': getattr(acc, 'account_name', 'Unknown'),
                        'broker': getattr(acc, 'broker_name', 'Unknown'),
                        'websocket_url': getattr(acc, 'websocket_url', 'N/A')
                    }
                    for acc in backup_accounts
                ],
                'failover_history': websocket_manager.connection_pool.get('failover_history', [])
            }

        # Build comprehensive response
        response = {
            'status': ws_status.get('status', 'unknown'),
            'connected': ws_status.get('connected', False),
            'authenticated': websocket_manager.authenticated if hasattr(websocket_manager, 'authenticated') else False,
            'active': websocket_manager.active if hasattr(websocket_manager, 'active') else False,
            'subscriptions': {
                'count': len(subscriptions_list),
                'list': subscriptions_list
            },
            'connection_details': connection_details,
            'metrics': ws_status.get('metrics', {}),
            'timestamp': datetime.utcnow().isoformat()
        }

        return jsonify(response)

    except Exception as e:
        current_app.logger.error(f'Error getting WebSocket status: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500