from flask import render_template, request, jsonify, current_app, Response, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.trading import trading_bp
from app.models import TradingAccount, TradingHoursTemplate, TradingSession, MarketHoliday, SpecialTradingSession
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.option_chain import OptionChainManager
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.background_service import option_chain_service
from app.utils.session_manager import session_manager
from datetime import datetime
import json
import time

def get_selected_accounts():
    """Get accounts to display based on user selection"""
    selected_account_id = request.args.get('account')
    
    if selected_account_id:
        try:
            # Convert to integer for database query
            account_id = int(selected_account_id)
            # Single account view
            account = TradingAccount.query.filter_by(
                id=account_id, 
                user_id=current_user.id,
                is_active=True
            ).first()
            return [account] if account else []
        except (ValueError, TypeError):
            # Invalid account ID, return all accounts
            return current_user.get_active_accounts()
    else:
        # Multi-account view
        return current_user.get_active_accounts()

@trading_bp.route('/funds')
@login_required
def funds():
    accounts = get_selected_accounts()
    funds_data = []
    
    for account in accounts:
        try:
            # Create API client for this account
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            
            # Fetch real-time funds data
            response = client.funds()
            
            if response.get('status') == 'success':
                account_funds = response.get('data', {})
                account_funds['account_name'] = account.account_name
                account_funds['account_id'] = account.id
                account_funds['broker'] = account.broker_name
                
                # Update cached data
                account.last_funds_data = account_funds
                account.last_data_update = datetime.utcnow()
                from app import db
                db.session.commit()
                
                funds_data.append(account_funds)
            elif account.last_funds_data:
                # Use cached data if API fails
                account_funds = account.last_funds_data.copy()
                account_funds['account_name'] = account.account_name
                account_funds['account_id'] = account.id
                account_funds['broker'] = account.broker_name
                funds_data.append(account_funds)
                
        except Exception as e:
            current_app.logger.error(f'Error fetching funds for account {account.id}: {str(e)}')
            # Use cached data if available
            if account.last_funds_data:
                account_funds = account.last_funds_data.copy()
                account_funds['account_name'] = account.account_name
                account_funds['account_id'] = account.id
                account_funds['broker'] = account.broker_name
                funds_data.append(account_funds)
    
    return render_template('trading/funds.html', 
                         funds_data=funds_data,
                         single_account=len(accounts) == 1,
                         accounts=current_user.get_active_accounts())

@trading_bp.route('/orderbook')
@login_required
def orderbook():
    accounts = get_selected_accounts()
    orderbook_data = []
    
    for account in accounts:
        try:
            # Create API client for this account
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            
            # Fetch orderbook data
            response = client.orderbook()
            
            if response.get('status') == 'success':
                data = response.get('data', {})
                orders = data.get('orders', [])
                
                # Add account info to each order
                for order in orders:
                    order['account_name'] = account.account_name
                    order['account_id'] = account.id
                    order['broker'] = account.broker_name
                
                orderbook_data.extend(orders)
                
        except Exception as e:
            current_app.logger.error(f'Error fetching orderbook for account {account.id}: {str(e)}')
    
    # Calculate statistics
    statistics = {
        'total_buy_orders': sum(1 for order in orderbook_data if order.get('action') == 'BUY'),
        'total_sell_orders': sum(1 for order in orderbook_data if order.get('action') == 'SELL'),
        'total_completed_orders': sum(1 for order in orderbook_data if order.get('order_status') == 'complete'),
        'total_open_orders': sum(1 for order in orderbook_data if order.get('order_status') in ['open', 'pending']),
        'total_rejected_orders': sum(1 for order in orderbook_data if order.get('order_status') == 'rejected'),
        'total_cancelled_orders': sum(1 for order in orderbook_data if order.get('order_status') == 'cancelled')
    }
    
    return render_template('trading/orderbook.html',
                         orderbook_data=orderbook_data,
                         statistics=statistics,
                         single_account=len(accounts) == 1,
                         accounts=current_user.get_active_accounts())

@trading_bp.route('/tradebook')
@login_required
def tradebook():
    accounts = get_selected_accounts()
    tradebook_data = []
    
    for account in accounts:
        try:
            # Create API client for this account
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            
            # Fetch tradebook data
            response = client.tradebook()
            
            if response.get('status') == 'success':
                trades = response.get('data', [])
                
                # Add account info to each trade
                for trade in trades:
                    trade['account_name'] = account.account_name
                    trade['account_id'] = account.id
                    trade['broker'] = account.broker_name
                
                tradebook_data.extend(trades)
                
        except Exception as e:
            current_app.logger.error(f'Error fetching tradebook for account {account.id}: {str(e)}')
    
    # Calculate total P&L
    total_pnl = 0
    for trade in tradebook_data:
        try:
            # Calculate P&L based on action
            quantity = float(trade.get('quantity', 0))
            avg_price = float(trade.get('average_price', 0))
            trade_value = float(trade.get('trade_value', 0))
            
            if trade.get('action') == 'SELL':
                total_pnl += trade_value
            else:
                total_pnl -= trade_value
        except (ValueError, TypeError):
            pass
    
    return render_template('trading/tradebook.html',
                         tradebook_data=tradebook_data,
                         total_pnl=total_pnl,
                         single_account=len(accounts) == 1,
                         accounts=current_user.get_active_accounts())

@trading_bp.route('/positions')
@login_required
def positions():
    accounts = get_selected_accounts()
    positions_data = []
    
    for account in accounts:
        try:
            # Create API client for this account
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            
            # Fetch position book data
            response = client.positionbook()
            
            if response.get('status') == 'success':
                positions = response.get('data', [])
                
                # Add account info to each position
                for position in positions:
                    position['account_name'] = account.account_name
                    position['account_id'] = account.id
                    position['broker'] = account.broker_name
                    
                    # Calculate additional metrics
                    try:
                        qty = float(position.get('quantity', 0))
                        avg_price = float(position.get('average_price', 0))
                        ltp = float(position.get('ltp', 0))
                        
                        if qty != 0 and avg_price != 0:
                            position['invested_value'] = abs(qty * avg_price)
                            position['current_value'] = abs(qty * ltp)
                            position['pnl_percentage'] = ((ltp - avg_price) / avg_price * 100) if avg_price else 0
                    except (ValueError, TypeError):
                        position['invested_value'] = 0
                        position['current_value'] = 0
                        position['pnl_percentage'] = 0
                
                positions_data.extend(positions)
                
                # Update cached data
                account.last_positions_data = positions
                account.last_data_update = datetime.utcnow()
                from app import db
                db.session.commit()
                
        except Exception as e:
            current_app.logger.error(f'Error fetching positions for account {account.id}: {str(e)}')
            # Use cached data if available
            if account.last_positions_data:
                positions = account.last_positions_data
                for position in positions:
                    position['account_name'] = account.account_name
                    position['account_id'] = account.id
                    position['broker'] = account.broker_name
                    
                    # Calculate additional metrics for cached data
                    try:
                        qty = float(position.get('quantity', 0))
                        avg_price = float(position.get('average_price', 0))
                        ltp = float(position.get('ltp', 0))
                        
                        if qty != 0 and avg_price != 0:
                            position['invested_value'] = abs(qty * avg_price)
                            position['current_value'] = abs(qty * ltp)
                            position['pnl_percentage'] = ((ltp - avg_price) / avg_price * 100) if avg_price else 0
                    except (ValueError, TypeError):
                        position['invested_value'] = 0
                        position['current_value'] = 0
                        position['pnl_percentage'] = 0
                        
                positions_data.extend(positions)
    
    # Calculate totals
    total_pnl = sum(float(p.get('pnl', 0)) for p in positions_data)
    total_invested = sum(float(p.get('invested_value', 0)) for p in positions_data)
    total_current = sum(float(p.get('current_value', 0)) for p in positions_data)
    
    return render_template('trading/positions.html',
                         positions_data=positions_data,
                         total_pnl=total_pnl,
                         total_invested=total_invested,
                         total_current=total_current,
                         single_account=len(accounts) == 1,
                         accounts=current_user.get_active_accounts())

@trading_bp.route('/holdings')
@login_required
def holdings():
    accounts = get_selected_accounts()
    holdings_data = []
    
    for account in accounts:
        try:
            # Create API client for this account
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            
            # Fetch holdings data
            response = client.holdings()
            
            if response.get('status') == 'success':
                data = response.get('data', {})
                holdings = data.get('holdings', [])
                
                # Add account info to each holding
                for holding in holdings:
                    holding['account_name'] = account.account_name
                    holding['account_id'] = account.id
                    holding['broker'] = account.broker_name
                
                holdings_data.extend(holdings)
                
                # Update cached data
                account.last_holdings_data = data
                account.last_data_update = datetime.utcnow()
                from app import db
                db.session.commit()
                
        except Exception as e:
            current_app.logger.error(f'Error fetching holdings for account {account.id}: {str(e)}')
            # Use cached data if available
            if account.last_holdings_data:
                data = account.last_holdings_data
                holdings = data.get('holdings', []) if isinstance(data, dict) else []
                for holding in holdings:
                    holding['account_name'] = account.account_name
                    holding['account_id'] = account.id
                    holding['broker'] = account.broker_name
                holdings_data.extend(holdings)
    
    # Calculate statistics
    statistics = {
        'totalholdingvalue': sum(float(h.get('quantity', 0)) * float(h.get('ltp', 0)) for h in holdings_data if h.get('ltp')),
        'totalinvvalue': 0,  # Will be calculated from holdings
        'totalprofitandloss': sum(float(h.get('pnl', 0)) for h in holdings_data),
        'totalpnlpercentage': 0
    }
    
    # Calculate total investment value and percentage
    for holding in holdings_data:
        try:
            pnl = float(holding.get('pnl', 0))
            pnl_percent = float(holding.get('pnlpercent', 0))
            if pnl_percent != 0:
                inv_value = abs(pnl / (pnl_percent / 100))
                statistics['totalinvvalue'] += inv_value
        except (ValueError, TypeError, ZeroDivisionError):
            pass
    
    if statistics['totalinvvalue'] > 0:
        statistics['totalpnlpercentage'] = (statistics['totalprofitandloss'] / statistics['totalinvvalue']) * 100
    
    return render_template('trading/holdings.html',
                         holdings_data=holdings_data,
                         statistics=statistics,
                         single_account=len(accounts) == 1,
                         accounts=current_user.get_active_accounts())


# Option Chain Management Routes
@trading_bp.route('/option-chain')
@login_required
def option_chain():
    """Display option chain interface"""
    underlying = request.args.get('underlying', 'NIFTY')
    expiry = request.args.get('expiry')
    
    # Get primary account for API calls
    primary_account = current_user.get_primary_account()
    
    if not primary_account:
        return render_template('trading/option_chain.html',
                             error="No primary account configured. Please set a primary account first.")
    
    try:
        # Create API client
        client = ExtendedOpenAlgoAPI(
            api_key=primary_account.get_api_key(),
            host=primary_account.host_url
        )
        
        # Get available expiry dates if not specified
        if not expiry:
            # Determine exchange based on underlying
            exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'

            expiry_response = client.expiry(
                symbol=underlying,
                exchange=exchange,
                instrumenttype='options'
            )

            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]  # Use nearest expiry
        
        # Initialize option chain manager
        option_manager = OptionChainManager(underlying, expiry)
        option_manager.initialize(client)
        
        # Get option chain data
        chain_data = option_manager.get_option_chain()
        
        return render_template('trading/option_chain.html',
                             chain_data=chain_data,
                             underlying=underlying,
                             expiry=expiry,
                             available_expiries=expiries if 'expiries' in locals() else [],
                             primary_account=primary_account)
                             
    except Exception as e:
        current_app.logger.error(f"Error loading option chain: {e}")
        return render_template('trading/option_chain.html',
                             error=f"Error loading option chain: {str(e)}")


@trading_bp.route('/api/option-chain/<underlying>')
@login_required
def api_option_chain(underlying):
    """API endpoint for real-time option chain data"""
    expiry = request.args.get('expiry')
    
    # Get primary account
    primary_account = current_user.get_primary_account()
    
    if not primary_account:
        return jsonify({'status': 'error', 'message': 'No primary account configured'}), 400
    
    try:
        # Create API client
        client = ExtendedOpenAlgoAPI(
            api_key=primary_account.get_api_key(),
            host=primary_account.host_url
        )
        
        # Get expiry if not provided
        if not expiry:
            expiry_response = client.expiry(
                symbol=underlying,
                exchange='NFO',
                instrumenttype='options'
            )
            
            if expiry_response.get('status') == 'success':
                expiries = expiry_response.get('data', [])
                if expiries:
                    expiry = expiries[0]
        
        # Get option chain manager instance
        option_manager = OptionChainManager(underlying, expiry)
        
        if not option_manager.is_active():
            option_manager.initialize(client)
        
        # Get option chain data
        chain_data = option_manager.get_option_chain()
        
        return jsonify({
            'status': 'success',
            'data': chain_data
        })
        
    except Exception as e:
        current_app.logger.error(f"API error for option chain: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/api/option-chain/stream/<underlying>')
@login_required
def option_chain_stream(underlying):
    """Get real-time option chain updates via server-sent events"""
    from app.utils.background_service import option_chain_service
    import json
    import time
    
    # Get expiry from query params
    expiry = request.args.get('expiry')

    # Log outside the generator where we have app context
    current_app.logger.debug(f"[SSE] Starting stream for {underlying} with expiry {expiry}")
    current_app.logger.debug(f"[SSE] Active managers: {list(option_chain_service.active_managers.keys())}")
    
    def generate():
        # Determine the manager key to use
        manager_key = f"{underlying}_{expiry}" if expiry else None
        
        while True:
            try:
                # Try to find the appropriate manager
                manager = None
                
                if manager_key and manager_key in option_chain_service.active_managers:
                    # Exact match with expiry
                    manager = option_chain_service.active_managers[manager_key]
                else:
                    # Find any manager for this underlying
                    for key in option_chain_service.active_managers.keys():
                        if key.startswith(f"{underlying}_"):
                            # If no expiry specified, use the first available
                            if not expiry:
                                manager = option_chain_service.active_managers[key]
                                # print(f"[SSE] Using manager {key} for {underlying}")
                                break
                            # If expiry specified but not found, try to start it
                            elif key.endswith(f"_{expiry}"):
                                manager = option_chain_service.active_managers[key]
                                break
                
                # If no manager found, try to start one
                if not manager and expiry:
                    # print(f"[SSE] No manager found for {underlying}_{expiry}, attempting to start")
                    # Try to start option chain (will handle failover internally)
                    if option_chain_service.start_option_chain(underlying, expiry):
                        manager_key = f"{underlying}_{expiry}"
                        manager = option_chain_service.active_managers.get(manager_key)
                        # print(f"[SSE] Started new manager for {manager_key}")
                    else:
                        # print(f"[SSE] Failed to start option chain for {underlying}_{expiry} - checking for backup accounts")
                        # If primary fails and we have backup accounts, trigger failover
                        if option_chain_service.backup_accounts:
                            # print(f"[SSE] Attempting failover with {len(option_chain_service.backup_accounts)} backup accounts")
                            option_chain_service.on_account_disconnected(option_chain_service.primary_account)
                            # Try again after failover
                            if option_chain_service.start_option_chain(underlying, expiry):
                                manager_key = f"{underlying}_{expiry}"
                                manager = option_chain_service.active_managers.get(manager_key)
                                # print(f"[SSE] Started manager after failover for {manager_key}")
                
                if manager:
                    chain_data = manager.get_option_chain()

                    # Send as server-sent event
                    data_json = json.dumps(chain_data)
                    yield f"data: {data_json}\n\n"
                else:
                    yield f"data: {json.dumps({'status': 'inactive', 'message': f'Option chain not active for {underlying} {expiry or ""}'})}\n\n"
                
                # Update every second
                time.sleep(1)
                
            except GeneratorExit:
                # print(f"[SSE] Client disconnected from {underlying} stream")
                break
            except Exception as e:
                # print(f"[SSE] Error streaming option chain: {e}")
                import traceback
                traceback.print_exc()
                yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"
                break
    
    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Credentials'] = 'true'
    return response


@trading_bp.route('/api/option-chain/expiry/<underlying>')
@login_required
def get_expiry_dates(underlying):
    """Get available expiry dates for an underlying"""

    # Get primary account
    primary_account = current_user.get_primary_account()

    if not primary_account:
        return jsonify({'status': 'error', 'message': 'No primary account configured'}), 400

    try:
        # Create API client
        client = ExtendedOpenAlgoAPI(
            api_key=primary_account.get_api_key(),
            host=primary_account.host_url
        )

        # Determine exchange based on underlying
        exchange = 'BFO' if underlying == 'SENSEX' else 'NFO'

        # Fetch expiry dates
        expiry_response = client.expiry(
            symbol=underlying,
            exchange=exchange,
            instrumenttype='options'
        )

        if expiry_response.get('status') == 'success':
            expiries = expiry_response.get('data', [])
            return jsonify({
                'status': 'success',
                'data': expiries
            })
        else:
            return jsonify({
                'status': 'error',
                'message': expiry_response.get('message', 'Failed to fetch expiry dates')
            }), 500

    except Exception as e:
        current_app.logger.error(f"Error fetching expiry dates for {underlying}: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/api/option-chain/status')
@login_required
def option_chain_status():
    """Check option chain monitoring status"""
    from app.utils.background_service import option_chain_service

    try:
        nifty_manager = option_chain_service.active_managers.get('NIFTY')
        banknifty_manager = option_chain_service.active_managers.get('BANKNIFTY')

        status = {
            'service_running': option_chain_service.is_running,
            'primary_account': option_chain_service.primary_account.account_name if option_chain_service.primary_account else None,
            'websockets_connected': len(option_chain_service.websocket_managers),
            'nifty': {
                'active': nifty_manager is not None,
                'strikes': len(nifty_manager.option_data) if nifty_manager else 0,
                'atm_strike': nifty_manager.atm_strike if nifty_manager else 0,
                'underlying_ltp': nifty_manager.underlying_ltp if nifty_manager else 0
            },
            'banknifty': {
                'active': banknifty_manager is not None,
                'strikes': len(banknifty_manager.option_data) if banknifty_manager else 0,
                'atm_strike': banknifty_manager.atm_strike if banknifty_manager else 0,
                'underlying_ltp': banknifty_manager.underlying_ltp if banknifty_manager else 0
            }
        }

        return jsonify({'status': 'success', 'data': status})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/api/option-chain/start', methods=['POST'])
@login_required
def start_option_chains():
    """Manually trigger option chain start"""
    from app.utils.background_service import option_chain_service
    
    try:
        # Get primary account
        primary_account = current_user.get_primary_account()
        
        if not primary_account:
            return jsonify({'status': 'error', 'message': 'No primary account configured'}), 400
        
        # Update connection status if needed
        if primary_account.connection_status != 'connected':
            primary_account.connection_status = 'connected'
            primary_account.last_connected = datetime.utcnow()
            db.session.commit()
        
        # Trigger option chain service
        option_chain_service.on_primary_account_connected(primary_account)
        
        return jsonify({
            'status': 'success',
            'message': 'Option chains started',
            'primary_account': primary_account.account_name,
            'active_chains': list(option_chain_service.active_managers.keys())
        })
        
    except Exception as e:
        current_app.logger.error(f"Error starting option chains: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/api/option-chain/sse')
@login_required
def option_chain_sse():
    """Server-Sent Events endpoint for live option chain updates"""
    
    # Capture request parameters before entering generator
    underlying = request.args.get('underlying', 'NIFTY')
    expiry = request.args.get('expiry')
    
    def generate():
        """Generate SSE stream"""
        # Get option chain manager
        option_manager = OptionChainManager(underlying, expiry)
        
        while True:
            try:
                # Get latest option chain data
                chain_data = option_manager.get_option_chain()
                
                # Send as SSE
                yield f"data: {json.dumps(chain_data)}\n\n"
                
                # Wait before next update
                time.sleep(1)  # Update every second
                
            except Exception as e:
                current_app.logger.error(f"SSE stream error: {e}")
                yield f"data: {json.dumps({'error': str(e)})}\n\n"
                break
    
    return Response(generate(), mimetype='text/event-stream')


@trading_bp.route('/trading-hours')
@login_required
def trading_hours():
    """Trading hours management page (admin only)"""
    from app.models import TradingHoursTemplate, TradingSession, MarketHoliday, SpecialTradingSession
    from flask_login import current_user
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    # Get all trading hours templates
    templates = TradingHoursTemplate.query.all()
    
    # Get upcoming holidays
    today = datetime.now().date()
    holidays = MarketHoliday.query.filter(
        MarketHoliday.holiday_date >= today
    ).order_by(MarketHoliday.holiday_date).limit(10).all()
    
    # Get upcoming special sessions
    special_sessions = SpecialTradingSession.query.filter(
        SpecialTradingSession.session_date >= today
    ).order_by(SpecialTradingSession.session_date).all()
    
    # Get background service status
    service_status = option_chain_service.get_status()
    
    return render_template('trading/trading_hours.html',
                         templates=templates,
                         holidays=holidays,
                         special_sessions=special_sessions,
                         service_status=service_status)


@trading_bp.route('/trading-hours/holiday/add', methods=['POST'])
@login_required
def add_holiday():
    """Add a new market holiday"""
    from datetime import datetime
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        # Parse form data
        holiday_date = datetime.strptime(request.form.get('holiday_date'), '%Y-%m-%d').date()
        holiday_name = request.form.get('holiday_name')
        market = request.form.get('market', 'NSE')
        is_special_session = request.form.get('is_special_session') == 'on'
        
        # Check if holiday already exists
        existing = MarketHoliday.query.filter_by(
            holiday_date=holiday_date,
            market=market
        ).first()
        
        if existing:
            flash(f'Holiday already exists for {holiday_date} on {market}', 'error')
        else:
            # Create new holiday
            holiday = MarketHoliday(
                holiday_date=holiday_date,
                holiday_name=holiday_name,
                market=market,
                holiday_type='trading',
                is_special_session=is_special_session
            )
            
            # Add special session times if provided
            if is_special_session:
                start_time = request.form.get('special_start_time')
                end_time = request.form.get('special_end_time')
                if start_time:
                    holiday.special_start_time = datetime.strptime(start_time, '%H:%M').time()
                if end_time:
                    holiday.special_end_time = datetime.strptime(end_time, '%H:%M').time()
            
            db.session.add(holiday)
            db.session.commit()
            flash(f'Holiday "{holiday_name}" added successfully!', 'success')
            
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error adding holiday: {e}')
        flash(f'Error adding holiday: {str(e)}', 'error')
    
    return redirect(url_for('trading.trading_hours'))


@trading_bp.route('/trading-hours/holiday/<int:holiday_id>/delete', methods=['POST'])
@login_required
def delete_holiday(holiday_id):
    """Delete a market holiday"""
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        holiday = MarketHoliday.query.get_or_404(holiday_id)
        holiday_name = holiday.holiday_name
        
        db.session.delete(holiday)
        db.session.commit()
        
        flash(f'Holiday "{holiday_name}" deleted successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error deleting holiday: {e}')
        flash(f'Error deleting holiday: {str(e)}', 'error')
    
    return redirect(url_for('trading.trading_hours'))


@trading_bp.route('/trading-hours/sessions/<int:template_id>')
@login_required
def get_sessions(template_id):
    """Get sessions for a template (JSON API)"""
    
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    try:
        template = TradingHoursTemplate.query.get_or_404(template_id)
        sessions = []
        
        for session in template.sessions:
            sessions.append({
                'id': session.id,
                'day_of_week': session.day_of_week,
                'start_time': session.start_time.strftime('%H:%M'),
                'end_time': session.end_time.strftime('%H:%M'),
                'is_active': session.is_active
            })
        
        return jsonify({'status': 'success', 'sessions': sessions})
        
    except Exception as e:
        current_app.logger.error(f'Error getting sessions: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/trading-hours/session/<int:session_id>/update', methods=['POST'])
@login_required
def update_session(session_id):
    """Update a single trading session via AJAX"""
    
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    try:
        session = TradingSession.query.get_or_404(session_id)
        data = request.get_json()
        
        if 'start_time' in data:
            session.start_time = datetime.strptime(data['start_time'], '%H:%M').time()
        if 'end_time' in data:
            session.end_time = datetime.strptime(data['end_time'], '%H:%M').time()
        
        db.session.commit()
        
        # Restart background service to apply changes
        option_chain_service.schedule_market_hours()
        
        return jsonify({'status': 'success', 'message': 'Session updated'})
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error updating session: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/trading-hours/sessions/update', methods=['POST'])
@login_required
def update_sessions():
    """Update all sessions for a template"""
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        template_id = request.form.get('template_id')
        template = TradingHoursTemplate.query.get_or_404(template_id)
        
        days = ['monday', 'tuesday', 'wednesday', 'thursday', 'friday', 'saturday', 'sunday']
        
        for day_num, day_name in enumerate(days):
            # Find or create session for this day
            session = TradingSession.query.filter_by(
                template_id=template.id,
                day_of_week=day_num
            ).first()
            
            is_active = request.form.get(f'{day_name}_active') == 'on'
            
            if session:
                # Update existing session
                if request.form.get(f'{day_name}_start'):
                    session.start_time = datetime.strptime(
                        request.form.get(f'{day_name}_start'), '%H:%M'
                    ).time()
                if request.form.get(f'{day_name}_end'):
                    session.end_time = datetime.strptime(
                        request.form.get(f'{day_name}_end'), '%H:%M'
                    ).time()
                session.is_active = is_active
            elif is_active:
                # Create new session if it's active
                new_session = TradingSession(
                    template_id=template.id,
                    session_name=f'{day_name.capitalize()} Regular Hours',
                    day_of_week=day_num,
                    start_time=datetime.strptime(
                        request.form.get(f'{day_name}_start', '09:15'), '%H:%M'
                    ).time(),
                    end_time=datetime.strptime(
                        request.form.get(f'{day_name}_end', '15:30'), '%H:%M'
                    ).time(),
                    session_type='normal',
                    is_active=True
                )
                db.session.add(new_session)
        
        db.session.commit()
        
        # Restart background service to apply changes
        option_chain_service.schedule_market_hours()
        
        flash('Trading sessions updated successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error updating sessions: {e}')
        flash(f'Error updating sessions: {str(e)}', 'error')
    
    return redirect(url_for('trading.trading_hours'))


@trading_bp.route('/trading-hours/template/<int:template_id>/toggle', methods=['POST'])
@login_required
def toggle_template(template_id):
    """Toggle template active status"""
    
    # Check if user is admin
    if not current_user.is_admin:
        return jsonify({'status': 'error', 'message': 'Access denied'}), 403
    
    try:
        template = TradingHoursTemplate.query.get_or_404(template_id)
        template.is_active = not template.is_active
        db.session.commit()
        
        return jsonify({'status': 'success', 'is_active': template.is_active})
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error toggling template: {e}')
        return jsonify({'status': 'error', 'message': str(e)}), 500


@trading_bp.route('/trading-hours/special-session/add', methods=['POST'])
@login_required
def add_special_session():
    """Add a new special trading session"""
    from app.models import SpecialTradingSession
    from datetime import datetime
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        # Parse form data
        session_date = datetime.strptime(request.form.get('session_date'), '%Y-%m-%d').date()
        session_name = request.form.get('session_name')
        market = request.form.get('market', 'NSE')
        start_time = datetime.strptime(request.form.get('start_time'), '%H:%M').time()
        end_time = datetime.strptime(request.form.get('end_time'), '%H:%M').time()
        description = request.form.get('description', '')
        
        # Check if session already exists for this date
        existing = SpecialTradingSession.query.filter_by(
            session_date=session_date,
            market=market,
            session_name=session_name
        ).first()
        
        if existing:
            flash(f'Special session already exists for {session_date} on {market}', 'error')
        else:
            # Create new special session
            session = SpecialTradingSession(
                session_date=session_date,
                session_name=session_name,
                market=market,
                start_time=start_time,
                end_time=end_time,
                description=description,
                is_active=True
            )
            
            db.session.add(session)
            db.session.commit()
            flash(f'Special session "{session_name}" added successfully!', 'success')
            
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error adding special session: {e}')
        flash(f'Error adding special session: {str(e)}', 'error')
    
    return redirect(url_for('trading.trading_hours'))


@trading_bp.route('/trading-hours/special-session/<int:session_id>/delete', methods=['POST'])
@login_required
def delete_special_session(session_id):
    """Delete a special trading session"""
    from app.models import SpecialTradingSession
    
    # Check if user is admin
    if not current_user.is_admin:
        flash('Access denied. Admin privileges required.', 'error')
        return redirect(url_for('main.dashboard'))
    
    try:
        session = SpecialTradingSession.query.get_or_404(session_id)
        session_name = session.session_name
        
        db.session.delete(session)
        db.session.commit()
        
        flash(f'Special session "{session_name}" deleted successfully!', 'success')
        
    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Error deleting special session: {e}')
        flash(f'Error deleting special session: {str(e)}', 'error')

    return redirect(url_for('trading.trading_hours'))


# ==================== OPTION CHAIN SESSION MANAGEMENT ====================
# On-demand WebSocket subscriptions for option chain viewing

@trading_bp.route('/api/option-chain-session/create', methods=['POST'])
@login_required
def create_option_chain_session():
    """
    Create a new option chain viewing session.
    Subscribes to symbols on-demand when user visits option chain page.
    """
    try:
        data = request.get_json()
        underlying = data.get('underlying', 'NIFTY')
        expiry = data.get('expiry')
        num_strikes = data.get('num_strikes', 20)

        if not expiry:
            return jsonify({
                'status': 'error',
                'message': 'Expiry date required'
            }), 400

        # Create session
        session = session_manager.create_session(
            user_id=current_user.id,
            underlying=underlying,
            expiry=expiry,
            num_strikes=num_strikes
        )

        if not session:
            return jsonify({
                'status': 'error',
                'message': 'Failed to create session'
            }), 500

        return jsonify({
            'status': 'success',
            'session_id': session.session_id,
            'expires_at': session.expires_at.isoformat() if session.expires_at else None,
            'subscribed_symbols': len(session.subscribed_symbols)
        })

    except Exception as e:
        current_app.logger.error(f'Error creating option chain session: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@trading_bp.route('/api/option-chain-session/heartbeat', methods=['POST'])
@login_required
def option_chain_session_heartbeat():
    """
    Update session heartbeat to keep it alive.
    Called every 30 seconds from frontend.
    """
    try:
        data = request.get_json()
        session_id = data.get('session_id')

        if not session_id:
            return jsonify({
                'status': 'error',
                'message': 'Session ID required'
            }), 400

        # Update heartbeat
        success = session_manager.update_heartbeat(session_id)

        if not success:
            return jsonify({
                'status': 'error',
                'message': 'Session not found or expired'
            }), 404

        return jsonify({
            'status': 'success',
            'message': 'Heartbeat updated'
        })

    except Exception as e:
        current_app.logger.error(f'Error updating heartbeat: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@trading_bp.route('/api/option-chain-session/destroy', methods=['POST'])
@login_required
def destroy_option_chain_session():
    """
    Destroy option chain session and unsubscribe from all symbols.
    Called when user leaves option chain page.
    """
    try:
        data = request.get_json()
        session_id = data.get('session_id')

        if not session_id:
            return jsonify({
                'status': 'error',
                'message': 'Session ID required'
            }), 400

        # Destroy session
        success = session_manager.destroy_session(session_id)

        if not success:
            return jsonify({
                'status': 'warning',
                'message': 'Session not found (may already be expired)'
            })

        return jsonify({
            'status': 'success',
            'message': 'Session destroyed'
        })

    except Exception as e:
        current_app.logger.error(f'Error destroying session: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


# =============================================================================
# RISK MONITOR ROUTES
# =============================================================================

@trading_bp.route('/risk-monitor')
@login_required
def risk_monitor():
    """Risk Monitor page showing active strategies with stoploss/target tracking"""
    from app.models import Strategy, StrategyExecution, RiskEvent

    # Get all active strategies with open positions for current user
    strategies = Strategy.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    # Filter to only strategies with open positions
    active_strategies = []
    for strategy in strategies:
        open_executions = StrategyExecution.query.filter_by(
            strategy_id=strategy.id,
            status='entered'
        ).all()

        if open_executions:
            active_strategies.append({
                'strategy': strategy,
                'executions': open_executions,
                'total_unrealized_pnl': sum(e.unrealized_pnl or 0 for e in open_executions),
                'total_quantity': sum(e.quantity or 0 for e in open_executions)
            })

    # Get recent risk events
    recent_events = RiskEvent.query.filter(
        RiskEvent.strategy_id.in_([s['strategy'].id for s in active_strategies])
    ).order_by(RiskEvent.triggered_at.desc()).limit(20).all() if active_strategies else []

    # Calculate totals
    total_positions = sum(len(s['executions']) for s in active_strategies)
    total_pnl = sum(s['total_unrealized_pnl'] for s in active_strategies)

    return render_template('trading/risk_monitor.html',
                         active_strategies=active_strategies,
                         recent_events=recent_events,
                         total_positions=total_positions,
                         total_pnl=total_pnl)


@trading_bp.route('/api/risk-status')
@login_required
def get_risk_status():
    """API endpoint to get current risk monitoring status for all active strategies"""
    from app.models import Strategy, StrategyExecution, RiskEvent
    from app.utils.background_service import option_chain_service
    import re

    def get_ltp_from_option_chain(symbol, exchange):
        """Parse symbol and fetch LTP from option chain service."""
        try:
            current_app.logger.debug(f"[RiskMonitor] Parsing symbol: {symbol}")
            match = re.match(r'^(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY)(\d{1,2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', symbol)
            if not match:
                current_app.logger.warning(f"[RiskMonitor] Symbol {symbol} did not match regex pattern")
                return None

            underlying = match.group(1)
            strike = int(match.group(3))
            option_type = match.group(4)
            current_app.logger.debug(f"[RiskMonitor] Parsed: underlying={underlying}, strike={strike}, type={option_type}")

            active_keys = list(option_chain_service.active_managers.keys())
            current_app.logger.debug(f"[RiskMonitor] Active managers: {active_keys}")

            for key in active_keys:
                if key.startswith(f"{underlying}_"):
                    manager = option_chain_service.active_managers[key]
                    chain_data = manager.get_option_chain()

                    if chain_data and chain_data.get('status') == 'success':
                        strikes = chain_data.get('strikes', [])
                        current_app.logger.debug(f"[RiskMonitor] Found {len(strikes)} strikes in chain")
                        for strike_data in strikes:
                            if strike_data.get('strike') == strike:
                                ltp = strike_data.get('ce_ltp', 0) if option_type == 'CE' else strike_data.get('pe_ltp', 0)
                                current_app.logger.debug(f"[RiskMonitor] Found LTP for {symbol}: {ltp}")
                                return ltp
                        current_app.logger.warning(f"[RiskMonitor] Strike {strike} not found in chain. Available: {[s.get('strike') for s in strikes[:5]]}...")
                    else:
                        current_app.logger.warning(f"[RiskMonitor] Chain data not available for {key}")
                    break
            else:
                current_app.logger.warning(f"[RiskMonitor] No active manager found for {underlying}")

            return None
        except Exception as e:
            current_app.logger.error(f"[RiskMonitor] Error getting LTP from option chain: {e}")
            return None

    try:
        # Get all active strategies with monitoring enabled
        strategies = Strategy.query.filter_by(
            user_id=current_user.id,
            is_active=True,
            risk_monitoring_enabled=True
        ).all()

        risk_data = []

        for strategy in strategies:
            # Get open positions for this strategy
            open_executions = StrategyExecution.query.filter_by(
                strategy_id=strategy.id,
                status='entered'
            ).all()

            # Include strategy if it has open positions OR if any exit was triggered
            # This ensures we can display the exit reason even after positions are closed
            any_exit_triggered = (
                strategy.supertrend_exit_triggered or
                strategy.trailing_sl_triggered_at or
                strategy.max_loss_triggered_at or
                strategy.max_profit_triggered_at
            )
            if not open_executions and not any_exit_triggered:
                continue

            # Calculate totals - will be recalculated with real-time LTP
            total_unrealized_pnl = 0
            total_realized_pnl = sum(e.realized_pnl or 0 for e in open_executions)

            # Build execution details
            executions_data = []
            for execution in open_executions:
                leg = execution.leg
                entry_price = execution.entry_price or 0
                action = leg.action if leg else 'BUY'

                # Get real-time LTP from option chain service
                real_time_ltp = get_ltp_from_option_chain(execution.symbol, execution.exchange)
                last_price = real_time_ltp if real_time_ltp else (execution.last_price or 0)

                # Calculate unrealized P&L with real-time LTP
                if last_price > 0 and entry_price > 0:
                    if action == 'BUY':
                        unrealized_pnl = (last_price - entry_price) * execution.quantity
                    else:  # SELL
                        unrealized_pnl = (entry_price - last_price) * execution.quantity
                else:
                    unrealized_pnl = execution.unrealized_pnl or 0

                total_unrealized_pnl += unrealized_pnl
                is_connected = real_time_ltp is not None

                # Calculate leg-level SL/TP prices and distances
                sl_price = None
                sl_distance = None
                sl_distance_pct = None
                sl_hit = False

                tp_price = None
                tp_distance = None
                tp_distance_pct = None
                tp_hit = False

                if leg and entry_price > 0:
                    # Calculate Stop Loss price
                    if leg.stop_loss_value and leg.stop_loss_value > 0:
                        if leg.stop_loss_type == 'percentage':
                            if action == 'BUY':
                                sl_price = entry_price * (1 - leg.stop_loss_value / 100)
                            else:  # SELL
                                sl_price = entry_price * (1 + leg.stop_loss_value / 100)
                        elif leg.stop_loss_type == 'points':
                            if action == 'BUY':
                                sl_price = entry_price - leg.stop_loss_value
                            else:  # SELL
                                sl_price = entry_price + leg.stop_loss_value
                        elif leg.stop_loss_type == 'premium':
                            sl_price = leg.stop_loss_value

                        # Calculate distance to SL
                        if sl_price and last_price > 0:
                            if action == 'BUY':
                                sl_distance = last_price - sl_price
                                sl_hit = last_price <= sl_price
                            else:  # SELL
                                sl_distance = sl_price - last_price
                                sl_hit = last_price >= sl_price
                            sl_distance_pct = (sl_distance / entry_price) * 100 if entry_price > 0 else 0

                    # Calculate Take Profit price
                    if leg.take_profit_value and leg.take_profit_value > 0:
                        if leg.take_profit_type == 'percentage':
                            if action == 'BUY':
                                tp_price = entry_price * (1 + leg.take_profit_value / 100)
                            else:  # SELL
                                tp_price = entry_price * (1 - leg.take_profit_value / 100)
                        elif leg.take_profit_type == 'points':
                            if action == 'BUY':
                                tp_price = entry_price + leg.take_profit_value
                            else:  # SELL
                                tp_price = entry_price - leg.take_profit_value
                        elif leg.take_profit_type == 'premium':
                            tp_price = leg.take_profit_value

                        # Calculate distance to TP
                        if tp_price and last_price > 0:
                            if action == 'BUY':
                                tp_distance = tp_price - last_price
                                tp_hit = last_price >= tp_price
                            else:  # SELL
                                tp_distance = last_price - tp_price
                                tp_hit = last_price <= tp_price
                            tp_distance_pct = (tp_distance / entry_price) * 100 if entry_price > 0 else 0

                executions_data.append({
                    'id': execution.id,
                    'symbol': execution.symbol,
                    'exchange': execution.exchange,
                    'entry_price': entry_price,
                    'last_price': last_price,
                    'quantity': execution.quantity,
                    'unrealized_pnl': round(unrealized_pnl, 2),
                    'realized_pnl': execution.realized_pnl,
                    'last_updated': datetime.utcnow().isoformat() if is_connected else None,
                    'websocket_subscribed': is_connected,
                    'trailing_sl_triggered': execution.trailing_sl_triggered,
                    'action': action,
                    'account_name': execution.account.account_name if execution.account else 'Unknown',
                    'leg_number': leg.leg_number if leg else 0,
                    # Leg-level SL data
                    'sl_type': leg.stop_loss_type if leg else None,
                    'sl_value': leg.stop_loss_value if leg else None,
                    'sl_price': round(sl_price, 2) if sl_price else None,
                    'sl_distance': round(sl_distance, 2) if sl_distance is not None else None,
                    'sl_distance_pct': round(sl_distance_pct, 2) if sl_distance_pct is not None else None,
                    'sl_hit': sl_hit,
                    # Leg-level TP data
                    'tp_type': leg.take_profit_type if leg else None,
                    'tp_value': leg.take_profit_value if leg else None,
                    'tp_price': round(tp_price, 2) if tp_price else None,
                    'tp_distance': round(tp_distance, 2) if tp_distance is not None else None,
                    'tp_distance_pct': round(tp_distance_pct, 2) if tp_distance_pct is not None else None,
                    'tp_hit': tp_hit,
                    # Trailing SL
                    'trailing_enabled': leg.enable_trailing if leg else False,
                    'trailing_type': leg.trailing_type if leg else None,
                    'trailing_value': leg.trailing_value if leg else None
                })

            # Calculate total P&L
            total_pnl = total_unrealized_pnl + total_realized_pnl

            # Calculate risk percentages
            max_loss_pct = 0
            max_profit_pct = 0

            if strategy.max_loss and strategy.max_loss != 0:
                max_loss_pct = min(100, (abs(total_pnl) / abs(strategy.max_loss)) * 100) if total_pnl < 0 else 0

            if strategy.max_profit and strategy.max_profit != 0:
                max_profit_pct = min(100, (total_pnl / strategy.max_profit) * 100) if total_pnl > 0 else 0

            risk_data.append({
                'strategy_id': strategy.id,
                'strategy_name': strategy.name,
                'risk_monitoring_enabled': strategy.risk_monitoring_enabled,
                'max_loss': strategy.max_loss,
                'max_profit': strategy.max_profit,
                'trailing_sl': strategy.trailing_sl,
                'trailing_sl_type': strategy.trailing_sl_type,
                'auto_exit_on_max_loss': strategy.auto_exit_on_max_loss,
                'auto_exit_on_max_profit': strategy.auto_exit_on_max_profit,
                'total_unrealized_pnl': round(total_unrealized_pnl, 2),
                'total_realized_pnl': round(total_realized_pnl, 2),
                'total_pnl': round(total_pnl, 2),
                'max_loss_pct': round(max_loss_pct, 1),
                'max_profit_pct': round(max_profit_pct, 1),
                'executions': executions_data,
                # Supertrend exit tracking
                'supertrend_exit_enabled': strategy.supertrend_exit_enabled,
                'supertrend_exit_triggered': strategy.supertrend_exit_triggered,
                'supertrend_exit_reason': strategy.supertrend_exit_reason,
                'supertrend_exit_triggered_at': strategy.supertrend_exit_triggered_at.isoformat() if strategy.supertrend_exit_triggered_at else None,
                # Trailing SL exit tracking (AFL-style ratcheting)
                'trailing_sl_active': strategy.trailing_sl_active,
                'trailing_sl_peak_pnl': strategy.trailing_sl_peak_pnl,
                'trailing_sl_initial_stop': strategy.trailing_sl_initial_stop,
                'trailing_sl_current_stop': strategy.trailing_sl_trigger_pnl,
                'trailing_sl_triggered_at': strategy.trailing_sl_triggered_at.isoformat() if strategy.trailing_sl_triggered_at else None,
                'trailing_sl_exit_reason': strategy.trailing_sl_exit_reason,
                # Max Loss exit tracking
                'max_loss_triggered_at': strategy.max_loss_triggered_at.isoformat() if strategy.max_loss_triggered_at else None,
                'max_loss_exit_reason': strategy.max_loss_exit_reason,
                # Max Profit exit tracking
                'max_profit_triggered_at': strategy.max_profit_triggered_at.isoformat() if strategy.max_profit_triggered_at else None,
                'max_profit_exit_reason': strategy.max_profit_exit_reason
            })

        return jsonify({
            'status': 'success',
            'data': risk_data,
            'timestamp': datetime.utcnow().isoformat()
        })

    except Exception as e:
        current_app.logger.error(f'Error getting risk status: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


@trading_bp.route('/api/risk-status/stream')
@login_required
def risk_status_stream():
    """SSE endpoint for real-time risk monitoring updates"""
    from app.models import Strategy, StrategyExecution
    from app.utils.background_service import option_chain_service
    import re

    current_app.logger.debug(f"[RiskMonitorSSE] Stream requested by user {current_user.id}")

    # Ensure position monitor is running for real-time LTP updates
    if not option_chain_service.position_monitor_running:
        current_app.logger.debug("[RiskMonitorSSE] Starting position monitor for real-time LTP")
        option_chain_service.start_position_monitor()

    # Ensure risk manager is running for SL/TP execution
    if not option_chain_service.risk_manager_running:
        current_app.logger.debug("[RiskMonitorSSE] Starting risk manager for SL/TP execution")
        option_chain_service.start_risk_manager()

    # Capture context before entering generator (request/app context not available in generator)
    user_id = current_user.id
    app = current_app._get_current_object()

    def execute_leg_exit(execution_id, strategy_id, symbol, exchange, quantity, action, exit_reason, trigger_price):
        """
        Execute exit order for a specific leg when SL/TP is hit.

        IMPORTANT: This exits on the EXECUTION's account, not the primary account.
        For multi-account strategies, each execution is tied to a specific account.

        Args:
            execution_id: ID of the StrategyExecution to close
            strategy_id: ID of the strategy
            symbol: Trading symbol
            exchange: Exchange name
            quantity: Quantity to exit
            action: Entry action (BUY/SELL) to determine exit direction
            exit_reason: 'stop_loss' or 'take_profit'
            trigger_price: Price at which the exit was triggered

        Returns:
            bool: True if exit order placed successfully
        """
        from app.models import TradingAccount, Strategy, StrategyExecution, Order
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app.utils.order_status_poller import order_status_poller

        try:
            # Use app context for database operations
            with app.app_context():
                # Get the execution first - we need its account
                execution = StrategyExecution.query.get(execution_id)
                if not execution:
                    app.logger.error(f"[RISK MONITOR] Execution {execution_id} not found")
                    return False

                # Get the strategy
                strategy = Strategy.query.get(strategy_id)
                if not strategy:
                    app.logger.error(f"[RISK MONITOR] Strategy not found for execution {execution_id}")
                    return False

                # Use the execution's account (NOT primary account)
                # Each execution is tied to a specific account in multi-account setups
                account = execution.account
                if not account or not account.is_active:
                    app.logger.error(f"[RISK MONITOR] Account not found or inactive for execution {execution_id}")
                    return False

                app.logger.debug(f"[RISK MONITOR] Using account {account.account_name} for execution {execution_id}")

                # Initialize OpenAlgo client with the execution's account
                client = ExtendedOpenAlgoAPI(
                    api_key=account.get_api_key(),
                    host=account.host_url
                )

                # Determine exit action (reverse of entry)
                entry_action = action.upper() if action else 'BUY'
                exit_action = 'SELL' if entry_action == 'BUY' else 'BUY'

                # Get product type - prefer execution's product, fallback to strategy's product_order_type
                exit_product = execution.product or strategy.product_order_type or 'MIS'

                # Place exit order using keyword arguments
                app.logger.debug(f"[RISK MONITOR] Placing exit order on {account.account_name}: symbol={symbol}, action={exit_action}, qty={quantity}, product={exit_product}")
                response = client.placeorder(
                    strategy=strategy.name,
                    symbol=symbol,
                    action=exit_action,
                    exchange=exchange,
                    price_type='MARKET',
                    product=exit_product,
                    quantity=str(quantity)
                )

                if response.get('status') == 'success':
                    order_id = response.get('orderid')

                    # Create Order record for orderbook/tradebook using execution's account
                    order = Order(
                        account_id=account.id,
                        order_id=order_id,
                        symbol=symbol,
                        exchange=exchange,
                        action=exit_action,
                        quantity=quantity,
                        order_type='MARKET',
                        product=exit_product,
                        status='pending'
                    )
                    db.session.add(order)

                    # Update execution
                    execution = StrategyExecution.query.get(execution_id)
                    if execution:
                        execution.status = 'exit_pending'
                        execution.exit_order_id = order_id
                        execution.exit_reason = exit_reason

                    db.session.commit()

                    # Add exit order to polling queue for status tracking (use execution's account)
                    order_status_poller.add_order(
                        execution_id=execution_id,
                        account=account,
                        order_id=order_id,
                        strategy_name=strategy.name
                    )

                    print(f"[RISK MONITOR] Exit order {order_id} placed for {symbol} on {account.account_name}", flush=True)
                    return True
                else:
                    error_msg = response.get('message', 'Unknown error')
                    print(f"[RISK MONITOR] ERROR: Failed to place exit order: {error_msg}", flush=True)
                    return False

        except Exception as e:
            import traceback
            print(f"[RISK MONITOR] ERROR: Exception placing exit order: {str(e)}", flush=True)
            print(f"[RISK MONITOR] Traceback: {traceback.format_exc()}", flush=True)
            return False

    def close_all_strategy_positions(strategy_id, exit_reason):
        """
        Close all open positions for a strategy when Max Loss/Max Profit is hit.

        IMPORTANT: For multi-account strategies, each execution is closed on its own account.
        This ensures orders are placed to the correct broker account.

        Args:
            strategy_id: ID of the strategy
            exit_reason: 'max_loss' or 'max_profit'

        Returns:
            bool: True if exit orders placed successfully
        """
        from app.models import TradingAccount, Strategy, StrategyExecution, Order
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        from app.utils.order_status_poller import order_status_poller

        try:
            with app.app_context():
                strategy = Strategy.query.get(strategy_id)
                if not strategy:
                    print(f"[RISK MONITOR] ERROR: Strategy {strategy_id} not found for {exit_reason}", flush=True)
                    return False

                # Get all open executions
                open_executions = StrategyExecution.query.filter_by(
                    strategy_id=strategy_id,
                    status='entered'
                ).all()

                if not open_executions:
                    print(f"[RISK MONITOR] No open positions to close for strategy {strategy_id}", flush=True)
                    return True

                print(f"[RISK MONITOR] {exit_reason.upper()}: Closing {len(open_executions)} positions across multiple accounts", flush=True)

                # Cache clients per account to avoid creating multiple instances
                account_clients = {}

                success_count = 0
                for execution in open_executions:
                    try:
                        # Use the execution's account (NOT primary account)
                        account = execution.account
                        if not account or not account.is_active:
                            print(f"[RISK MONITOR] ERROR: Account not found or inactive for execution {execution.id}", flush=True)
                            continue

                        # Get or create client for this account
                        if account.id not in account_clients:
                            account_clients[account.id] = ExtendedOpenAlgoAPI(
                                api_key=account.get_api_key(),
                                host=account.host_url
                            )
                        client = account_clients[account.id]

                        # Get entry action from leg
                        leg = execution.leg
                        entry_action = leg.action.upper() if leg else 'BUY'
                        exit_action = 'SELL' if entry_action == 'BUY' else 'BUY'

                        # Get product type - prefer execution's product, fallback to strategy's product_order_type
                        exit_product = execution.product or strategy.product_order_type or 'MIS'
                        print(f"[RISK MONITOR] {exit_reason.upper()}: Placing exit order for {execution.symbol} on {account.account_name}, action={exit_action}, qty={execution.quantity}, product={exit_product}", flush=True)
                        response = client.placeorder(
                            strategy=strategy.name,
                            symbol=execution.symbol,
                            action=exit_action,
                            exchange=execution.exchange,
                            price_type='MARKET',
                            product=exit_product,
                            quantity=str(execution.quantity)
                        )

                        if response.get('status') == 'success':
                            order_id = response.get('orderid')

                            # Create Order record for orderbook/tradebook (using execution's account)
                            order = Order(
                                account_id=account.id,
                                order_id=order_id,
                                symbol=execution.symbol,
                                exchange=execution.exchange,
                                action=exit_action,
                                quantity=execution.quantity,
                                order_type='MARKET',
                                product=exit_product,
                                status='pending'
                            )
                            db.session.add(order)

                            execution.status = 'exit_pending'
                            execution.exit_order_id = order_id
                            execution.exit_reason = exit_reason
                            success_count += 1

                            # Add exit order to polling queue (using execution's account)
                            order_status_poller.add_order(
                                execution_id=execution.id,
                                account=account,
                                order_id=order_id,
                                strategy_name=strategy.name
                            )

                            print(f"[RISK MONITOR] Exit order {order_id} placed for {execution.symbol} on {account.account_name}", flush=True)
                        else:
                            print(f"[RISK MONITOR] ERROR: Failed to place exit for {execution.symbol} on {account.account_name}: {response.get('message')}", flush=True)

                    except Exception as e:
                        print(f"[RISK MONITOR] ERROR: Exception closing {execution.symbol}: {str(e)}", flush=True)

                db.session.commit()
                print(f"[RISK MONITOR] {exit_reason.upper()} exit completed: {success_count}/{len(open_executions)} orders placed", flush=True)
                return success_count > 0

        except Exception as e:
            import traceback
            print(f"[RISK MONITOR] ERROR: Exception in close_all_strategy_positions: {str(e)}", flush=True)
            print(f"[RISK MONITOR] Traceback: {traceback.format_exc()}", flush=True)
            return False

    def get_ltp_from_option_chain(symbol, exchange):
        """
        Parse symbol and fetch LTP from option chain service.
        Symbol format: NIFTY18NOV2525950CE or BANKNIFTY18NOV2550000PE
        """
        try:
            # Parse symbol to extract underlying, strike, and option type
            # Pattern: UNDERLYING + DATE + STRIKE + OPTION_TYPE
            match = re.match(r'^(NIFTY|BANKNIFTY|SENSEX|FINNIFTY|MIDCPNIFTY)(\d{1,2}[A-Z]{3}\d{2})(\d+)(CE|PE)$', symbol)
            if not match:
                return None

            underlying = match.group(1)
            strike = int(match.group(3))
            option_type = match.group(4)

            # Find the option chain manager for this underlying
            for key in option_chain_service.active_managers.keys():
                if key.startswith(f"{underlying}_"):
                    manager = option_chain_service.active_managers[key]
                    chain_data = manager.get_option_chain()

                    if chain_data and chain_data.get('status') == 'success':
                        strikes = chain_data.get('strikes', [])
                        for strike_data in strikes:
                            if strike_data.get('strike') == strike:
                                if option_type == 'CE':
                                    return strike_data.get('ce_ltp', 0)
                                else:
                                    return strike_data.get('pe_ltp', 0)
                    break

            return None
        except Exception as e:
            return None

    def generate():
        import traceback
        refresh_counter = 0
        while True:
            try:
                # Use app context for database operations
                with app.app_context():
                    # Refresh positions every 5 seconds to catch any missed subscriptions
                    refresh_counter += 1
                    if refresh_counter >= 5:
                        try:
                            from app.utils.position_monitor import position_monitor
                            position_monitor.refresh_positions()
                        except Exception as e:
                            pass  # Non-critical, continue monitoring
                        refresh_counter = 0
                    # Get all active strategies with monitoring enabled
                    strategies = Strategy.query.filter_by(
                        user_id=user_id,
                        is_active=True,
                        risk_monitoring_enabled=True
                    ).all()

                    risk_data = []

                    for strategy in strategies:
                        # Get all positions for this strategy (including exited ones from today)
                        # Show all legs so trader can see complete SL/TP history
                        from datetime import timedelta
                        today_start = datetime.utcnow().replace(hour=0, minute=0, second=0, microsecond=0)

                        open_executions = StrategyExecution.query.filter(
                            StrategyExecution.strategy_id == strategy.id,
                            StrategyExecution.status.in_(['entered', 'exit_pending', 'exited']),
                            StrategyExecution.entry_time >= today_start
                        ).all()

                        # Include strategy if it has positions OR if any exit was triggered
                        # This ensures we display the exit reason even after all positions are closed
                        any_exit_triggered = (
                            strategy.supertrend_exit_triggered or
                            strategy.trailing_sl_triggered_at or
                            strategy.max_loss_triggered_at or
                            strategy.max_profit_triggered_at
                        )
                        if not open_executions and not any_exit_triggered:
                            continue

                        # Calculate totals - will be recalculated with real-time LTP
                        total_unrealized_pnl = 0
                        total_realized_pnl = sum(e.realized_pnl or 0 for e in open_executions)

                        # Build execution details with leg-level SL/TP
                        executions_data = []
                        for execution in open_executions:
                            leg = execution.leg
                            entry_price = execution.entry_price or 0
                            action = leg.action if leg else 'BUY'

                            # Debug logging for SL/TP (now at DEBUG level to reduce log flooding)
                            if leg:
                                current_app.logger.debug(f"[RISK SSE] Execution {execution.id}: leg_id={leg.id}, SL_type={leg.stop_loss_type}, SL_value={leg.stop_loss_value}, TP_type={leg.take_profit_type}, TP_value={leg.take_profit_value}")
                            else:
                                current_app.logger.debug(f"[RISK SSE] Execution {execution.id}: NO LEG FOUND (leg_id={execution.leg_id})")

                            # Determine price source and LTP
                            # Priority: WebSocket-updated last_price > cached last_price > calculated from P&L
                            price_source = 'offline'
                            last_price = 0

                            # Check if execution has LTP from database (WebSocket or previous update)
                            if execution.last_price and execution.last_price > 0:
                                last_price = execution.last_price
                                # Mark as realtime if we have valid price data
                                price_source = 'realtime'
                            elif execution.unrealized_pnl and entry_price > 0 and execution.quantity > 0:
                                # Back-calculate LTP from stored unrealized P&L
                                if action == 'BUY':
                                    last_price = entry_price + (execution.unrealized_pnl / execution.quantity)
                                else:  # SELL
                                    last_price = entry_price - (execution.unrealized_pnl / execution.quantity)
                                # Mark as realtime since we have calculated price
                                price_source = 'realtime'

                            # Calculate unrealized P&L with LTP
                            # Only calculate unrealized P&L for 'entered' status (open positions)
                            # For 'exit_pending' and 'exited', use realized P&L only
                            unrealized_pnl = 0
                            if execution.status == 'entered':
                                if last_price > 0 and entry_price > 0:
                                    if action == 'BUY':
                                        unrealized_pnl = (last_price - entry_price) * execution.quantity
                                    else:  # SELL
                                        unrealized_pnl = (entry_price - last_price) * execution.quantity
                                else:
                                    unrealized_pnl = execution.unrealized_pnl or 0

                            total_unrealized_pnl += unrealized_pnl

                            # Determine connection status
                            is_connected = price_source == 'realtime'

                            # Calculate leg-level SL/TP
                            sl_price = None
                            sl_distance = None
                            sl_hit = False
                            tp_price = None
                            tp_distance = None
                            tp_hit = False
                            exit_already_placed = False

                            # Skip SL/TP checking if exit order already placed
                            if execution.exit_order_id or execution.status in ['exit_pending', 'exited']:
                                exit_already_placed = True

                            # Check if already hit (persisted state)
                            if execution.sl_hit_at:
                                sl_hit = True
                                # RETRY MECHANISM: If SL was hit but position still open (no exit order), retry exit
                                if not execution.exit_order_id and execution.status == 'entered':
                                    print(f"[RISK MONITOR] RETRY: SL was hit but no exit order for {execution.symbol}, retrying...", flush=True)
                                    exit_success = execute_leg_exit(
                                        execution.id,
                                        execution.strategy_id,
                                        execution.symbol,
                                        execution.exchange,
                                        execution.quantity,
                                        action,
                                        'stop_loss_retry',
                                        execution.sl_hit_price or 0
                                    )
                                    if exit_success:
                                        print(f"[RISK MONITOR] RETRY SUCCESS: Exit order placed for {execution.symbol} (Stop Loss)", flush=True)
                                        exit_already_placed = True
                            if execution.tp_hit_at:
                                tp_hit = True
                                # RETRY MECHANISM: If TP was hit but position still open (no exit order), retry exit
                                if not execution.exit_order_id and execution.status == 'entered':
                                    print(f"[RISK MONITOR] RETRY: TP was hit but no exit order for {execution.symbol}, retrying...", flush=True)
                                    exit_success = execute_leg_exit(
                                        execution.id,
                                        execution.strategy_id,
                                        execution.symbol,
                                        execution.exchange,
                                        execution.quantity,
                                        action,
                                        'take_profit_retry',
                                        execution.tp_hit_price or 0
                                    )
                                    if exit_success:
                                        print(f"[RISK MONITOR] RETRY SUCCESS: Exit order placed for {execution.symbol} (Take Profit)", flush=True)
                                        exit_already_placed = True

                            if leg and entry_price > 0:
                                # Stop Loss calculation
                                if leg.stop_loss_value and leg.stop_loss_value > 0:
                                    if leg.stop_loss_type == 'percentage':
                                        sl_price = entry_price * (1 - leg.stop_loss_value / 100) if action == 'BUY' else entry_price * (1 + leg.stop_loss_value / 100)
                                    elif leg.stop_loss_type == 'points':
                                        sl_price = entry_price - leg.stop_loss_value if action == 'BUY' else entry_price + leg.stop_loss_value
                                    elif leg.stop_loss_type == 'premium':
                                        sl_price = leg.stop_loss_value

                                    if sl_price and last_price > 0 and not sl_hit and not exit_already_placed:
                                        sl_distance = last_price - sl_price if action == 'BUY' else sl_price - last_price
                                        # Check if SL just got hit
                                        if last_price <= sl_price if action == 'BUY' else last_price >= sl_price:
                                            sl_hit = True
                                            exit_already_placed = True  # Prevent TP from also triggering
                                            # Persist the hit state
                                            execution.sl_hit_at = datetime.utcnow()
                                            execution.sl_hit_price = last_price
                                            db.session.commit()
                                            # Log the SL hit event
                                            print(f"[RISK MONITOR] STOP LOSS HIT! Symbol: {execution.symbol}, Entry: {entry_price}, SL Price: {sl_price}, Hit Price: {last_price}, Action: {action}", flush=True)
                                            # Execute exit order for this leg
                                            exit_success = execute_leg_exit(
                                                execution.id,
                                                execution.strategy_id,
                                                execution.symbol,
                                                execution.exchange,
                                                execution.quantity,
                                                action,
                                                'stop_loss',
                                                sl_price
                                            )
                                            if exit_success:
                                                print(f"[RISK MONITOR] Exit order placed for {execution.symbol} (Stop Loss)", flush=True)
                                    elif sl_hit:
                                        # Already hit - show distance from hit price
                                        sl_distance = 0

                                # Take Profit calculation
                                if leg.take_profit_value and leg.take_profit_value > 0:
                                    if leg.take_profit_type == 'percentage':
                                        tp_price = entry_price * (1 + leg.take_profit_value / 100) if action == 'BUY' else entry_price * (1 - leg.take_profit_value / 100)
                                    elif leg.take_profit_type == 'points':
                                        tp_price = entry_price + leg.take_profit_value if action == 'BUY' else entry_price - leg.take_profit_value
                                    elif leg.take_profit_type == 'premium':
                                        tp_price = leg.take_profit_value

                                    if tp_price and last_price > 0 and not tp_hit and not exit_already_placed:
                                        tp_distance = tp_price - last_price if action == 'BUY' else last_price - tp_price
                                        # Check if TP just got hit
                                        if last_price >= tp_price if action == 'BUY' else last_price <= tp_price:
                                            tp_hit = True
                                            exit_already_placed = True  # Prevent duplicate exit orders
                                            # Persist the hit state
                                            execution.tp_hit_at = datetime.utcnow()
                                            execution.tp_hit_price = last_price
                                            db.session.commit()
                                            # Log the TP hit event
                                            print(f"[RISK MONITOR] TAKE PROFIT HIT! Symbol: {execution.symbol}, Entry: {entry_price}, TP Price: {tp_price}, Hit Price: {last_price}, Action: {action}", flush=True)
                                            # Execute exit order for this leg
                                            exit_success = execute_leg_exit(
                                                execution.id,
                                                execution.strategy_id,
                                                execution.symbol,
                                                execution.exchange,
                                                execution.quantity,
                                                action,
                                                'take_profit',
                                                tp_price
                                            )
                                            if exit_success:
                                                print(f"[RISK MONITOR] Exit order placed for {execution.symbol} (Take Profit)", flush=True)
                                    elif tp_hit:
                                        # Already hit - show distance from hit price
                                        tp_distance = 0

                            # Ensure distance is 0 for hit positions (fallback for edge cases)
                            # This handles cases where entry_price is 0, leg is None, or last_price is 0
                            if sl_hit and sl_distance is None:
                                sl_distance = 0
                            if tp_hit and tp_distance is None:
                                tp_distance = 0

                            executions_data.append({
                                'id': execution.id,
                                'symbol': execution.symbol,
                                'entry_price': entry_price,
                                'last_price': round(last_price, 2) if last_price else 0,
                                'quantity': execution.quantity,
                                'unrealized_pnl': round(unrealized_pnl, 2),
                                'last_updated': datetime.utcnow().isoformat() if is_connected else None,
                                'websocket_subscribed': is_connected,
                                'price_source': price_source,
                                'action': action,
                                'leg_number': leg.leg_number if leg else 0,
                                'sl_price': round(sl_price, 2) if sl_price is not None else None,
                                'sl_distance': round(sl_distance, 2) if sl_distance is not None else None,
                                'sl_hit': sl_hit,
                                'tp_price': round(tp_price, 2) if tp_price is not None else None,
                                'tp_distance': round(tp_distance, 2) if tp_distance is not None else None,
                                'tp_hit': tp_hit,
                                'trailing_sl_triggered': execution.trailing_sl_triggered,
                                'status': execution.status,
                                'exit_reason': execution.exit_reason
                            })

                        # Calculate total P&L
                        total_pnl = total_unrealized_pnl + total_realized_pnl

                        # Calculate risk percentages
                        max_loss_pct = 0
                        max_profit_pct = 0
                        max_loss_hit = False
                        max_profit_hit = False

                        if strategy.max_loss and strategy.max_loss != 0:
                            max_loss_pct = min(100, (abs(total_pnl) / abs(strategy.max_loss)) * 100) if total_pnl < 0 else 0
                            # Check if Max Loss threshold is breached
                            if total_pnl < 0 and abs(total_pnl) >= abs(strategy.max_loss):
                                max_loss_hit = True
                                # Check if auto-exit is enabled and not already closing
                                if strategy.auto_exit_on_max_loss:
                                    # Check if any position is still 'entered' (not already closing)
                                    still_open = any(e.status == 'entered' and not e.exit_order_id for e in open_executions)
                                    if still_open:
                                        print(f"[RISK MONITOR] MAX LOSS HIT! Strategy: {strategy.name}, P&L: {total_pnl}, Threshold: -{strategy.max_loss}", flush=True)
                                        close_all_strategy_positions(strategy.id, 'max_loss')

                        if strategy.max_profit and strategy.max_profit != 0:
                            max_profit_pct = min(100, (total_pnl / strategy.max_profit) * 100) if total_pnl > 0 else 0
                            # Check if Max Profit threshold is reached
                            if total_pnl > 0 and total_pnl >= strategy.max_profit:
                                max_profit_hit = True
                                # Check if auto-exit is enabled and not already closing
                                if strategy.auto_exit_on_max_profit:
                                    # Check if any position is still 'entered' (not already closing)
                                    still_open = any(e.status == 'entered' and not e.exit_order_id for e in open_executions)
                                    if still_open:
                                        print(f"[RISK MONITOR] MAX PROFIT HIT! Strategy: {strategy.name}, P&L: {total_pnl}, Threshold: {strategy.max_profit}", flush=True)
                                        close_all_strategy_positions(strategy.id, 'max_profit')

                        risk_data.append({
                            'strategy_id': strategy.id,
                            'strategy_name': strategy.name,
                            'max_loss': strategy.max_loss,
                            'max_profit': strategy.max_profit,
                            'trailing_sl': strategy.trailing_sl,
                            'trailing_sl_type': strategy.trailing_sl_type,
                            'total_pnl': round(total_pnl, 2),
                            'max_loss_pct': round(max_loss_pct, 1),
                            'max_profit_pct': round(max_profit_pct, 1),
                            'max_loss_hit': max_loss_hit,
                            'max_profit_hit': max_profit_hit,
                            'executions': executions_data,
                            # Supertrend exit tracking
                            'supertrend_exit_enabled': strategy.supertrend_exit_enabled,
                            'supertrend_exit_triggered': strategy.supertrend_exit_triggered,
                            'supertrend_exit_reason': strategy.supertrend_exit_reason,
                            'supertrend_exit_triggered_at': strategy.supertrend_exit_triggered_at.isoformat() if strategy.supertrend_exit_triggered_at else None,
                            # Trailing SL exit tracking (AFL-style ratcheting)
                            'trailing_sl_active': strategy.trailing_sl_active,
                            'trailing_sl_peak_pnl': strategy.trailing_sl_peak_pnl,
                            'trailing_sl_initial_stop': strategy.trailing_sl_initial_stop,
                            'trailing_sl_current_stop': strategy.trailing_sl_trigger_pnl,
                            'trailing_sl_triggered_at': strategy.trailing_sl_triggered_at.isoformat() if strategy.trailing_sl_triggered_at else None,
                            'trailing_sl_exit_reason': strategy.trailing_sl_exit_reason,
                            # Max Loss exit tracking
                            'max_loss_triggered_at': strategy.max_loss_triggered_at.isoformat() if strategy.max_loss_triggered_at else None,
                            'max_loss_exit_reason': strategy.max_loss_exit_reason,
                            # Max Profit exit tracking
                            'max_profit_triggered_at': strategy.max_profit_triggered_at.isoformat() if strategy.max_profit_triggered_at else None,
                            'max_profit_exit_reason': strategy.max_profit_exit_reason
                        })

                # Send as SSE
                data_json = json.dumps({
                    'status': 'success',
                    'data': risk_data,
                    'timestamp': datetime.utcnow().isoformat()
                })
                yield f"data: {data_json}\n\n"

                # Update every second
                time.sleep(1)

            except GeneratorExit:
                break
            except Exception as e:
                error_msg = f"[RiskMonitorSSE ERROR] {str(e)}\n{traceback.format_exc()}"
                print(error_msg, flush=True)
                yield f"data: {json.dumps({'status': 'error', 'message': str(e)})}\n\n"
                break

    response = Response(generate(), mimetype='text/event-stream')
    response.headers['Cache-Control'] = 'no-cache'
    response.headers['X-Accel-Buffering'] = 'no'
    response.headers['Connection'] = 'keep-alive'
    return response


@trading_bp.route('/api/risk-events')
@login_required
def get_risk_events():
    """Get recent risk events for user's strategies"""
    from app.models import Strategy, RiskEvent

    try:
        # Get user's strategy IDs
        strategy_ids = [s.id for s in Strategy.query.filter_by(user_id=current_user.id).all()]

        # Get recent events
        events = RiskEvent.query.filter(
            RiskEvent.strategy_id.in_(strategy_ids)
        ).order_by(RiskEvent.triggered_at.desc()).limit(50).all()

        events_data = []
        for event in events:
            events_data.append({
                'id': event.id,
                'strategy_id': event.strategy_id,
                'strategy_name': event.strategy.name if event.strategy else 'Unknown',
                'execution_id': event.execution_id,
                'event_type': event.event_type,
                'message': event.message,
                'triggered_at': event.triggered_at.isoformat() if event.triggered_at else None,
                'pnl_at_trigger': event.pnl_at_trigger,
                'action_taken': event.action_taken
            })

        return jsonify({
            'status': 'success',
            'data': events_data
        })

    except Exception as e:
        current_app.logger.error(f'Error getting risk events: {e}')
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500