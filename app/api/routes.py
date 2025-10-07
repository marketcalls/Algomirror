from flask import jsonify, request
from flask_login import login_required, current_user
from app.api import api_bp
from app.models import TradingAccount
from app.utils.rate_limiter import api_rate_limit
from app.utils.ping_monitor import ping_monitor

@api_bp.route('/accounts')
@login_required
@api_rate_limit()
def get_accounts():
    """Get user's trading accounts"""
    accounts = current_user.get_active_accounts()
    
    accounts_data = []
    for account in accounts:
        accounts_data.append({
            'id': account.id,
            'name': account.account_name,
            'broker': account.broker_name,
            'status': account.connection_status,
            'is_primary': account.is_primary,
            'last_connected': account.last_connected.isoformat() if account.last_connected else None
        })
    
    return jsonify({
        'status': 'success',
        'data': accounts_data
    })

@api_bp.route('/ping-status')
@login_required
@api_rate_limit()
def get_ping_status():
    """Get ping status summary for user's accounts"""
    try:
        status_summary = ping_monitor.get_account_status_summary(current_user.id)
        return jsonify({
            'status': 'success',
            **status_summary  # Spread the summary directly into the response
        })
    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to get ping status: {str(e)}'
        }), 500

@api_bp.route('/accounts/<int:account_id>/ping', methods=['POST'])
@login_required
@api_rate_limit()
def force_ping_check(account_id):
    """Force immediate ping check for specific account"""
    try:
        # Verify account belongs to current user
        account = TradingAccount.query.filter_by(
            id=account_id,
            user_id=current_user.id
        ).first()

        if not account:
            return jsonify({
                'status': 'error',
                'message': 'Account not found'
            }), 404

        result = ping_monitor.force_check_account(account_id)
        return jsonify(result)

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to check account: {str(e)}'
        }), 500

@api_bp.route('/accounts/<int:account_id>/funds')
@login_required
@api_rate_limit()
def get_account_funds(account_id):
    """Get real-time funds data for specific account"""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from datetime import datetime
        from app import db

        # Verify account belongs to current user
        account = TradingAccount.query.filter_by(
            id=account_id,
            user_id=current_user.id,
            is_active=True
        ).first()

        if not account:
            return jsonify({
                'status': 'error',
                'message': 'Account not found'
            }), 404

        # Create API client
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )

        # Fetch real-time funds data
        response = client.funds()

        if response.get('status') == 'success':
            funds_data = response.get('data', {})

            # Cache the data
            account.last_funds_data = funds_data
            account.last_data_update = datetime.utcnow()
            db.session.commit()

            return jsonify({
                'status': 'success',
                'data': {
                    'account_id': account.id,
                    'account_name': account.account_name,
                    'broker_name': account.broker_name,
                    'availablecash': funds_data.get('availablecash', 0),
                    'collateral': funds_data.get('collateral', 0),
                    'utiliseddebits': funds_data.get('utiliseddebits', 0),
                    'used_margin': funds_data.get('utiliseddebits', 0),  # Alias for compatibility
                    'net': funds_data.get('net', 0),
                    'm2mrealized': funds_data.get('m2mrealized', 0),
                    'm2munrealized': funds_data.get('m2munrealized', 0)
                }
            })
        elif account.last_funds_data:
            # Return cached data if API fails
            cached_data = account.last_funds_data
            return jsonify({
                'status': 'success',
                'data': {
                    'account_id': account.id,
                    'account_name': account.account_name,
                    'broker_name': account.broker_name,
                    'availablecash': cached_data.get('availablecash', 0),
                    'collateral': cached_data.get('collateral', 0),
                    'utiliseddebits': cached_data.get('utiliseddebits', 0),
                    'used_margin': cached_data.get('utiliseddebits', 0),  # Alias for compatibility
                    'net': cached_data.get('net', 0),
                    'm2mrealized': cached_data.get('m2mrealized', 0),
                    'm2munrealized': cached_data.get('m2munrealized', 0),
                    'cached': True
                }
            })
        else:
            return jsonify({
                'status': 'error',
                'message': response.get('message', 'Failed to fetch funds data')
            }), 500

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to get funds: {str(e)}'
        }), 500

@api_bp.route('/accounts/<int:account_id>/pnl')
@login_required
@api_rate_limit()
def get_account_pnl(account_id):
    """Get account-specific P&L (realized + unrealized) for today"""
    try:
        from app.models import Strategy, StrategyExecution
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from datetime import datetime, timezone
        from app import db

        # Verify account belongs to current user
        account = TradingAccount.query.filter_by(
            id=account_id,
            user_id=current_user.id,
            is_active=True
        ).first()

        if not account:
            return jsonify({
                'status': 'error',
                'message': 'Account not found'
            }), 404

        # Calculate today's P&L for this specific account
        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

        # Get all executions for this account today
        today_executions = StrategyExecution.query.join(Strategy).filter(
            Strategy.user_id == current_user.id,
            StrategyExecution.account_id == account_id,
            StrategyExecution.created_at >= today_start
        ).all()

        # Calculate realized P&L (closed positions)
        realized_pnl = sum(e.realized_pnl or 0 for e in today_executions if e.realized_pnl)

        # Calculate unrealized P&L (open positions) with current LTP
        unrealized_pnl = 0
        open_positions = 0

        try:
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            for execution in today_executions:
                if execution.status == 'entered' and execution.entry_price:
                    open_positions += 1

                    # Get current LTP for this position
                    try:
                        quote = client.quotes(symbol=execution.symbol, exchange=execution.exchange)
                        if quote.get('status') == 'success':
                            ltp = float(quote.get('data', {}).get('ltp', execution.entry_price))

                            # Calculate P&L based on action
                            if execution.leg.action == 'BUY':
                                pnl = (ltp - execution.entry_price) * execution.quantity
                            else:  # SELL
                                pnl = (execution.entry_price - ltp) * execution.quantity

                            # Update unrealized P&L in database
                            execution.unrealized_pnl = pnl
                            unrealized_pnl += pnl
                        else:
                            # Use cached unrealized P&L if quote fails
                            unrealized_pnl += execution.unrealized_pnl or 0
                    except Exception as e_quote:
                        # Use cached unrealized P&L on error
                        unrealized_pnl += execution.unrealized_pnl or 0

            # Commit updated unrealized P&L to database
            db.session.commit()

        except Exception as e_client:
            # Fallback to cached unrealized P&L if client fails
            unrealized_pnl = sum(e.unrealized_pnl or 0 for e in today_executions
                                if e.status == 'entered' and e.unrealized_pnl)
            open_positions = sum(1 for e in today_executions if e.status == 'entered')

        # Total P&L
        total_pnl = realized_pnl + unrealized_pnl

        # Count closed positions
        closed_positions = sum(1 for e in today_executions if e.status == 'exited')

        return jsonify({
            'status': 'success',
            'data': {
                'account_id': account_id,
                'account_name': account.account_name,
                'realized_pnl': round(realized_pnl, 2),
                'unrealized_pnl': round(unrealized_pnl, 2),
                'total_pnl': round(total_pnl, 2),
                'open_positions': open_positions,
                'closed_positions': closed_positions
            }
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to get P&L: {str(e)}'
        }), 500

@api_bp.route('/trading-hours/status')
@login_required
def get_trading_hours_status():
    """Get current trading hours status"""
    try:
        from app.utils.background_service import option_chain_service
        from datetime import datetime
        import pytz

        # Get current time in IST
        ist = pytz.timezone('Asia/Kolkata')
        now = datetime.now(ist)

        # Check if within trading hours
        is_trading_hours = option_chain_service.is_trading_hours()
        is_holiday = option_chain_service.is_holiday()

        # Get next session info
        sessions = option_chain_service.get_trading_sessions()
        next_session = None

        if not is_trading_hours and sessions:
            # Find next session
            current_day = now.weekday()
            current_time = now.time()

            for session in sessions:
                if session['is_active']:
                    if session['day_of_week'] == current_day and session['start_time'] > current_time:
                        next_session = {
                            'day': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][session['day_of_week']],
                            'start_time': session['start_time'].strftime('%H:%M'),
                            'end_time': session['end_time'].strftime('%H:%M')
                        }
                        break
                    elif session['day_of_week'] > current_day:
                        next_session = {
                            'day': ['Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday', 'Sunday'][session['day_of_week']],
                            'start_time': session['start_time'].strftime('%H:%M'),
                            'end_time': session['end_time'].strftime('%H:%M')
                        }
                        break

        return jsonify({
            'status': 'success',
            'data': {
                'is_trading_hours': is_trading_hours,
                'is_holiday': is_holiday,
                'current_time': now.strftime('%Y-%m-%d %H:%M:%S'),
                'timezone': 'Asia/Kolkata',
                'next_session': next_session
            }
        })

    except Exception as e:
        return jsonify({
            'status': 'error',
            'message': f'Failed to get trading hours status: {str(e)}'
        }), 500