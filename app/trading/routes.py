from flask import render_template, request, jsonify, current_app, Response, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.trading import trading_bp
from app.models import TradingAccount, TradingHoursTemplate, TradingSession, MarketHoliday, SpecialTradingSession
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.option_chain import OptionChainManager
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.background_service import option_chain_service
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
    current_app.logger.info(f"[SSE] Starting stream for {underlying} with expiry {expiry}")
    current_app.logger.info(f"[SSE] Active managers: {list(option_chain_service.active_managers.keys())}")
    
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