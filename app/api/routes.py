from flask import jsonify, request, make_response
from flask_login import login_required, current_user
from datetime import datetime, timezone
from app.api import api_bp
from app.models import TradingAccount
from app.utils.rate_limiter import api_rate_limit
from app.utils.ping_monitor import ping_monitor


def no_cache_response(data, status=200):
    """Create a JSON response with no-cache headers to prevent stale data"""
    response = make_response(jsonify(data), status)
    response.headers['Cache-Control'] = 'no-store, no-cache, must-revalidate, max-age=0'
    response.headers['Pragma'] = 'no-cache'
    response.headers['Expires'] = '0'
    return response


def utcnow():
    # naive UTC (not datetime.utcnow(), deprecated on 3.12+) - stays naive since SQLite drops tzinfo on read
    return datetime.now(timezone.utc).replace(tzinfo=None)

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
    """Get real-time funds data for specific account.
    Returns cached data if fresh (< 30s) to avoid slow broker API calls."""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
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

        client_holder = {}

        def get_client():
            if 'client' not in client_holder:
                client_holder['client'] = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url,
                    timeout=8
                )
            return client_holder['client']

        # Analyzer (live vs simulated) mode rarely changes, so it's refreshed
        # on a much longer cadence (5 min) than funds to avoid an extra broker
        # round-trip on every 15s funds poll.
        analyzer_mode = account.last_analyzer_mode
        analyzer_stale = (
            account.last_analyzer_check is None or
            (utcnow() - account.last_analyzer_check).total_seconds() >= 300
        )
        if analyzer_stale:
            try:
                analyzer_response = get_client().analyzerstatus()
                if analyzer_response and analyzer_response.get('status') == 'success':
                    analyzer_mode = bool(analyzer_response.get('data', {}).get('analyze_mode', False))
                    account.last_analyzer_mode = analyzer_mode
                    account.last_analyzer_check = utcnow()
                    db.session.commit()
            except Exception:
                db.session.rollback()

        # Return cached data if fresh (< 30 seconds old)
        # Avoids broker API call (~500ms-2s network latency) on every page load
        if account.last_funds_data and account.last_data_update:
            cache_age = (utcnow() - account.last_data_update).total_seconds()
            if cache_age < 30:
                cached_data = account.last_funds_data
                return no_cache_response({
                    'status': 'success',
                    'data': {
                        'account_id': account.id,
                        'account_name': account.account_name,
                        'broker_name': account.broker_name,
                        'availablecash': cached_data.get('availablecash', 0),
                        'collateral': cached_data.get('collateral', 0),
                        'utiliseddebits': cached_data.get('utiliseddebits', 0),
                        'used_margin': cached_data.get('utiliseddebits', 0),
                        'net': cached_data.get('net', 0),
                        'm2mrealized': cached_data.get('m2mrealized', 0),
                        'm2munrealized': cached_data.get('m2munrealized', 0),
                        'analyze_mode': analyzer_mode,
                        'cached': True
                    }
                })

        # Single attempt - on failure we serve cached data (and the dashboard
        # polls again in ~15s). The client has a SHORT timeout for this
        # interactive read: the dashboard aborts the request at ~10s, so a
        # 30s broker timeout plus the old 2-attempt retry made funds spin
        # past the abort and never populate.
        response = None
        try:
            response = get_client().funds()
        except Exception:
            response = None

        if response and response.get('status') == 'success':
            funds_data = response.get('data', {})

            # Cache the data (non-blocking - don't hold up reads if DB is busy)
            try:
                account.last_funds_data = funds_data
                account.last_data_update = utcnow()
                db.session.commit()
            except Exception:
                db.session.rollback()

            return no_cache_response({
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
                    'm2munrealized': funds_data.get('m2munrealized', 0),
                    'analyze_mode': analyzer_mode
                }
            })
        elif account.last_funds_data:
            # Return cached data if API fails
            cached_data = account.last_funds_data
            return no_cache_response({
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
                    'analyze_mode': analyzer_mode,
                    'cached': True
                }
            })
        else:
            return no_cache_response({
                'status': 'error',
                'message': (response or {}).get('message', 'Failed to fetch funds data')
            }, 500)

    except Exception as e:
        return no_cache_response({
            'status': 'error',
            'message': f'Failed to get funds: {str(e)}'
        }, 500)

@api_bp.route('/accounts/<int:account_id>/mode')
@login_required
@api_rate_limit()
def get_account_mode(account_id):
    """Get whether an account's OpenAlgo instance is in Live or Analyze (sandbox) mode."""
    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

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

        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url,
            timeout=8
        )

        response = None
        try:
            response = client.analyzerstatus()
        except Exception:
            response = None

        if response and response.get('status') == 'success':
            mode_data = response.get('data', {})
            return no_cache_response({
                'status': 'success',
                'data': {
                    'account_id': account.id,
                    'mode': mode_data.get('mode', 'live'),
                    'analyze_mode': bool(mode_data.get('analyze_mode', False))
                }
            })

        return no_cache_response({
            'status': 'error',
            'message': (response or {}).get('message', 'Failed to fetch analyzer status')
        }, 500)

    except Exception as e:
        return no_cache_response({
            'status': 'error',
            'message': f'Failed to get mode: {str(e)}'
        }, 500)

@api_bp.route('/accounts/<int:account_id>/pnl')
@login_required
@api_rate_limit()
def get_account_pnl(account_id):
    """Get account-specific P&L (realized + unrealized) for today"""
    try:
        from app.models import Strategy, StrategyExecution
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
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
        today_start = utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

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

        # Collect open executions
        open_executions = [e for e in today_executions if e.status == 'entered' and e.entry_price]
        open_positions = len(open_executions)

        if open_executions:
            # Build LTP cache - try WebSocket first (instant), then API fallback
            ltp_cache = {}
            unique_symbols = set((e.symbol, e.exchange) for e in open_executions)

            # Try WebSocket LTP cache first (no API calls needed)
            try:
                from app.utils.background_service import option_chain_service
                if option_chain_service.shared_websocket_manager:
                    ws_data = option_chain_service.shared_websocket_manager.get_ltp()
                    ws_ltp_data = ws_data.get('ltp', {})
                    for symbol, exchange in unique_symbols:
                        ws_key = f"{exchange}:{symbol}"
                        if ws_key in ws_ltp_data:
                            ltp_cache[(symbol, exchange)] = float(ws_ltp_data[ws_key])
            except Exception:
                pass

            # API fallback only for symbols not in WebSocket cache
            # OPTIMIZED: Single multiquotes() call replaces parallel per-symbol fan-out
            symbols_needing_api = [(s, e) for s, e in unique_symbols if (s, e) not in ltp_cache]
            if symbols_needing_api:
                try:
                    # Short timeout for this interactive read (the dashboard aborts
                    # at ~10s) - on a slow broker we fall back to cached unrealized
                    # P&L below rather than hanging.
                    client = ExtendedOpenAlgoAPI(
                        api_key=account.get_api_key(),
                        host=account.host_url,
                        timeout=8
                    )
                    symbols_payload = [{'symbol': s, 'exchange': e} for s, e in symbols_needing_api]
                    response = client.multiquotes(symbols=symbols_payload)
                    if response.get('status') == 'success':
                        for result in response.get('results', []):
                            sym = result.get('symbol')
                            exch = result.get('exchange')
                            ltp = float(result.get('data', {}).get('ltp') or 0)
                            if sym and exch and ltp > 0:
                                ltp_cache[(sym, exch)] = ltp
                except Exception:
                    pass

            # Calculate P&L for each open position using cached LTPs
            for execution in open_executions:
                ltp = ltp_cache.get((execution.symbol, execution.exchange), 0)
                if ltp > 0:
                    if execution.leg.action == 'BUY':
                        pnl = (ltp - execution.entry_price) * execution.quantity
                    else:
                        pnl = (execution.entry_price - ltp) * execution.quantity
                    execution.unrealized_pnl = pnl
                    unrealized_pnl += pnl
                else:
                    # Use cached unrealized P&L if no LTP available
                    unrealized_pnl += execution.unrealized_pnl or 0

            try:
                db.session.commit()
            except Exception:
                db.session.rollback()

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