from datetime import datetime
from flask import render_template, redirect, url_for, flash, request, current_app, jsonify
from flask_login import login_required, current_user
from app.accounts import accounts_bp
from app.accounts.forms import AddAccountForm, EditAccountForm
from app.models import TradingAccount, ActivityLog
from app import db
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit
from app.utils.background_service import option_chain_service
import json

def log_activity(action, details=None, account_id=None):
    """Helper function to log account activities"""
    try:
        log_entry = ActivityLog(
            user_id=current_user.id,
            account_id=account_id,
            action=action,
            details=details,
            ip_address=request.remote_addr,
            user_agent=request.headers.get('User-Agent'),
            status='success'
        )
        db.session.add(log_entry)
        db.session.commit()
        
        current_app.logger.debug(
            f'Account activity: {action}',
            extra={
                'event': 'account_activity',
                'action': action,
                'user_id': current_user.id,
                'account_id': account_id
            }
        )
    except Exception as e:
        current_app.logger.error(f'Failed to log activity: {str(e)}')

@accounts_bp.route('/manage')
@login_required
def manage():
    accounts = current_user.accounts.all()
    return render_template('accounts/manage.html', accounts=accounts)

@accounts_bp.route('/add', methods=['GET', 'POST'])
@login_required
def add():
    form = AddAccountForm()
    
    if form.validate_on_submit():
        try:
            # Test connection first
            test_client = ExtendedOpenAlgoAPI(
                api_key=form.api_key.data,
                host=form.host_url.data
            )
            
            # Try ping endpoint first to test connection
            ping_response = test_client.ping()
            
            if ping_response.get('status') != 'success':
                error_message = ping_response.get('message', 'Unknown error')
                if 'apikey' in error_message.lower():
                    flash('Invalid OpenAlgo API key. Please check your API key and try again.', 'error')
                elif '403' in error_message or 'forbidden' in error_message.lower():
                    flash('Access denied. Please check your OpenAlgo API key is valid and active.', 'error')
                elif 'timeout' in error_message.lower() or 'connection' in error_message.lower():
                    flash('Cannot connect to OpenAlgo server. Please check the Host URL and ensure OpenAlgo is running.', 'error')
                else:
                    flash(f'Failed to connect to OpenAlgo: {error_message}', 'error')
                
                current_app.logger.error(f'Ping failed for new account: {ping_response}')
                return render_template('accounts/add.html', form=form)
            
            # Get broker info from ping response
            broker_info = ping_response.get('data', {}).get('broker', form.broker_name.data)
            
            # If primary account is being set, unset other primary accounts
            if form.is_primary.data:
                current_user.accounts.update({'is_primary': False})
            
            # Create account
            account = TradingAccount(
                user_id=current_user.id,
                account_name=form.account_name.data,
                broker_name=broker_info,  # Use broker info from ping response
                host_url=form.host_url.data,
                websocket_url=form.websocket_url.data,
                is_primary=form.is_primary.data,
                connection_status='connected',
                last_connected=datetime.utcnow()
            )
            
            # Encrypt and store API key
            account.set_api_key(form.api_key.data)
            
            # Try to fetch initial funds data (optional)
            try:
                funds_response = test_client.funds()
                if funds_response.get('status') == 'success':
                    account.last_funds_data = funds_response.get('data', {})
                    account.last_data_update = datetime.utcnow()
            except Exception:
                # If funds fetch fails, continue without it
                pass
            
            db.session.add(account)
            db.session.commit()
            
            log_activity('account_added', {
                'account_name': account.account_name,
                'broker_name': account.broker_name
            }, account.id)
            
            # If this is a primary account, trigger background service
            if account.is_primary:
                option_chain_service.on_primary_account_connected(account)
                current_app.logger.debug(f'Triggered option chain service for primary account: {account.account_name}')
            
            flash(f'Account "{account.account_name}" added successfully!', 'success')
            return redirect(url_for('accounts.manage'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Failed to add account: {str(e)}', exc_info=True)
            
            # More specific error message based on exception type
            if 'connection' in str(e).lower() or 'timeout' in str(e).lower():
                flash('Failed to connect to OpenAlgo server. Please check the host URL and try again.', 'error')
            elif 'api' in str(e).lower() or 'key' in str(e).lower():
                flash('Invalid API key. Please check your OpenAlgo API key and try again.', 'error')
            else:
                flash(f'Failed to add account: {str(e)}', 'error')
    
    return render_template('accounts/add.html', form=form)

@accounts_bp.route('/edit/<int:account_id>', methods=['GET', 'POST'])
@login_required
def edit(account_id):
    account = TradingAccount.query.filter_by(
        id=account_id, 
        user_id=current_user.id
    ).first_or_404()
    
    form = EditAccountForm(original_name=account.account_name)
    
    if form.validate_on_submit():
        try:
            # If API key is provided, test new connection
            if form.api_key.data:
                test_client = ExtendedOpenAlgoAPI(
                    api_key=form.api_key.data,
                    host=form.host_url.data
                )
                
                # Use ping endpoint to test connection
                ping_response = test_client.ping()
                
                if ping_response.get('status') != 'success':
                    flash('Failed to connect with new credentials. Please check them.', 'error')
                    return render_template('accounts/edit.html', form=form, account=account)
                
                # Update API key
                account.set_api_key(form.api_key.data)
                account.connection_status = 'connected'
                account.last_connected = datetime.utcnow()
                
                # Update broker info from ping response
                broker_info = ping_response.get('data', {}).get('broker')
                if broker_info:
                    account.broker_name = broker_info
            
            # If primary account is being set, unset other primary accounts
            if form.is_primary.data and not account.is_primary:
                current_user.accounts.filter(TradingAccount.id != account_id).update({'is_primary': False})
            
            # Update account details
            account.account_name = form.account_name.data
            account.broker_name = form.broker_name.data
            account.host_url = form.host_url.data
            account.websocket_url = form.websocket_url.data
            account.is_primary = form.is_primary.data
            account.is_active = form.is_active.data
            account.updated_at = datetime.utcnow()
            
            db.session.commit()
            
            log_activity('account_updated', {
                'account_name': account.account_name
            }, account.id)
            
            # If this became the primary account, trigger background service
            if account.is_primary:
                option_chain_service.on_primary_account_connected(account)
                current_app.logger.debug(f'Triggered option chain service for primary account: {account.account_name}')
            
            flash(f'Account "{account.account_name}" updated successfully!', 'success')
            return redirect(url_for('accounts.manage'))
            
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f'Failed to update account: {str(e)}')
            flash('Failed to update account. Please try again.', 'error')
    
    # Pre-populate form
    if request.method == 'GET':
        form.account_name.data = account.account_name
        form.broker_name.data = account.broker_name
        form.host_url.data = account.host_url
        form.websocket_url.data = account.websocket_url
        form.is_primary.data = account.is_primary
        form.is_active.data = account.is_active
    
    return render_template('accounts/edit.html', form=form, account=account)

@accounts_bp.route('/delete/<int:account_id>', methods=['POST'])
@login_required
def delete(account_id):
    account = TradingAccount.query.filter_by(
        id=account_id,
        user_id=current_user.id
    ).first_or_404()

    try:
        account_name = account.account_name
        was_primary = account.is_primary

        log_activity('account_deleted', {
            'account_name': account_name
        }, account.id)

        # If deleting primary account, notify background service
        if was_primary:
            option_chain_service.on_account_disconnected(account)
            current_app.logger.debug(f'Notified option chain service of primary account deletion: {account_name}')

        # Delete all related records first to avoid foreign key constraint errors
        # Import models needed for deletion
        from app.models import Order, Position, Holding, StrategyExecution, MarginTracker, ActivityLog

        # Delete orders
        Order.query.filter_by(account_id=account_id).delete()

        # Delete positions
        Position.query.filter_by(account_id=account_id).delete()

        # Delete holdings
        Holding.query.filter_by(account_id=account_id).delete()

        # Delete strategy executions
        StrategyExecution.query.filter_by(account_id=account_id).delete()

        # Delete margin trackers
        MarginTracker.query.filter_by(account_id=account_id).delete()

        # Set account_id to NULL in activity logs (nullable=True)
        ActivityLog.query.filter_by(account_id=account_id).update({'account_id': None})

        # Finally delete the account
        db.session.delete(account)
        db.session.commit()

        flash(f'Account "{account_name}" deleted successfully!', 'success')

    except Exception as e:
        db.session.rollback()
        current_app.logger.error(f'Failed to delete account: {str(e)}')
        flash('Failed to delete account. Please try again.', 'error')

    return redirect(url_for('accounts.manage'))

@accounts_bp.route('/test-connection/<int:account_id>')
@login_required
@heavy_rate_limit()
def test_connection(account_id):
    account = TradingAccount.query.filter_by(
        id=account_id, 
        user_id=current_user.id
    ).first_or_404()
    
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )
        
        # Test connection with ping endpoint
        ping_response = client.ping()
        
        if ping_response.get('status') == 'success':
            account.connection_status = 'connected'
            account.last_connected = datetime.utcnow()
            
            # Also fetch funds data for dashboard
            funds_response = client.funds()
            if funds_response.get('status') == 'success':
                account.last_funds_data = funds_response.get('data', {})
                account.last_data_update = datetime.utcnow()
            
            db.session.commit()
            
            broker_info = ping_response.get('data', {}).get('broker', 'Unknown')
            
            return jsonify({
                'status': 'success',
                'message': f'Connection successful - Broker: {broker_info}',
                'data': ping_response.get('data', {})
            })
        else:
            account.connection_status = 'failed'
            db.session.commit()
            
            return jsonify({
                'status': 'error',
                'message': 'Connection failed: ' + ping_response.get('message', 'Unknown error')
            })
            
    except Exception as e:
        account.connection_status = 'error'
        db.session.commit()
        
        current_app.logger.error(f'Connection test failed: {str(e)}')
        
        return jsonify({
            'status': 'error',
            'message': f'Connection error: {str(e)}'
        })

@accounts_bp.route('/refresh-data/<int:account_id>')
@login_required
@heavy_rate_limit()
def refresh_data(account_id):
    account = TradingAccount.query.filter_by(
        id=account_id, 
        user_id=current_user.id
    ).first_or_404()
    
    try:
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )
        
        # Fetch latest data
        funds_response = client.funds()
        positions_response = client.positionbook()
        holdings_response = client.holdings()
        
        if funds_response.get('status') == 'success':
            account.last_funds_data = funds_response.get('data', {})
            account.connection_status = 'connected'
            account.last_connected = datetime.utcnow()
        
        if positions_response.get('status') == 'success':
            account.last_positions_data = positions_response.get('data', [])
            
        if holdings_response.get('status') == 'success':
            account.last_holdings_data = holdings_response.get('data', {})
        
        account.last_data_update = datetime.utcnow()
        db.session.commit()
        
        return jsonify({
            'status': 'success',
            'message': 'Data refreshed successfully',
            'last_update': account.last_data_update.isoformat()
        })
        
    except Exception as e:
        current_app.logger.error(f'Data refresh failed: {str(e)}')
        
        return jsonify({
            'status': 'error',
            'message': f'Failed to refresh data: {str(e)}'
        })

@accounts_bp.route('/test-connection-preview', methods=['POST'])
@login_required
@heavy_rate_limit()
def test_connection_preview():
    """Test connection with user-provided credentials before account creation"""
    try:
        data = request.get_json()
        host_url = data.get('host_url')
        api_key = data.get('api_key')
        
        if not host_url or not api_key:
            return jsonify({
                'status': 'error',
                'message': 'Host URL and API Key are required'
            })
        
        # Test connection with ping
        test_client = ExtendedOpenAlgoAPI(api_key=api_key, host=host_url)
        ping_response = test_client.ping()
        
        if ping_response.get('status') == 'success':
            broker = ping_response.get('data', {}).get('broker', 'Unknown')
            return jsonify({
                'status': 'success',
                'message': 'Connection successful',
                'broker': broker
            })
        else:
            error_message = ping_response.get('message', 'Unknown error')
            return jsonify({
                'status': 'error',
                'message': error_message
            })
            
    except Exception as e:
        current_app.logger.error(f'Preview connection test failed: {str(e)}', exc_info=True)
        return jsonify({
            'status': 'error',
            'message': f'Connection test failed: {str(e)}'
        })