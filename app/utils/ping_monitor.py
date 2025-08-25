"""
Ping Monitoring Service for AlgoMirror

This service monitors the connection status of all trading accounts
by periodically pinging their OpenAlgo servers and updating connection status.
"""
import threading
import time
from datetime import datetime
from flask import current_app
from app import db
from app.models import TradingAccount, ActivityLog
from app.utils.openalgo_client import ExtendedOpenAlgoAPI


class PingMonitor:
    """Background service to monitor account connections"""
    
    def __init__(self, app=None):
        self.app = app
        self.monitoring_thread = None
        self.stop_monitoring = threading.Event()
        self.account_failure_counts = {}  # Track consecutive failures per account
        self.account_skip_counts = {}  # Skip checking accounts that are consistently failing
        
        if app is not None:
            self.init_app(app)
    
    def init_app(self, app):
        """Initialize the ping monitor with Flask app"""
        self.app = app
        
        # Start monitoring if enabled
        if app.config.get('PING_MONITORING_ENABLED', True):
            self.start_monitoring()
    
    def start_monitoring(self):
        """Start the background monitoring thread"""
        if self.monitoring_thread is None or not self.monitoring_thread.is_alive():
            self.stop_monitoring.clear()
            self.monitoring_thread = threading.Thread(target=self._monitor_loop, daemon=True)
            self.monitoring_thread.start()
            if self.app:
                self.app.logger.info("Ping monitoring started")
    
    def stop_monitoring_service(self):
        """Stop the background monitoring thread"""
        self.stop_monitoring.set()
        if self.monitoring_thread:
            self.monitoring_thread.join(timeout=5)
        if self.app:
            self.app.logger.info("Ping monitoring stopped")
    
    def _monitor_loop(self):
        """Main monitoring loop that runs in background thread"""
        interval = self.app.config.get('PING_MONITORING_INTERVAL', 30)
        
        while not self.stop_monitoring.is_set():
            try:
                with self.app.app_context():
                    self._check_all_accounts()
            except Exception as e:
                # Use app logger instead of current_app in case of context issues
                with self.app.app_context():
                    current_app.logger.error(f"Ping monitoring error: {str(e)}", exc_info=True)
            
            # Wait for the next interval or until stop is requested
            self.stop_monitoring.wait(interval)
    
    def _check_all_accounts(self):
        """Check ping status for all active trading accounts"""
        accounts = TradingAccount.query.filter_by(is_active=True).all()
        max_failures = self.app.config.get('PING_MAX_FAILURES', 3)
        
        for account in accounts:
            try:
                # Refresh account object to avoid stale data
                try:
                    db.session.refresh(account)
                except Exception as refresh_error:
                    current_app.logger.warning(f"Could not refresh account {account.id}: {refresh_error}")
                    # Try to continue with potentially stale data
                
                # Skip if account doesn't have API key
                api_key = None
                try:
                    api_key = account.get_api_key()
                except Exception as key_error:
                    # Mark account as having encryption issues and skip silently after first error
                    if account.id not in getattr(self, 'encryption_error_accounts', set()):
                        if not hasattr(self, 'encryption_error_accounts'):
                            self.encryption_error_accounts = set()
                        self.encryption_error_accounts.add(account.id)
                        current_app.logger.error(f"Failed to decrypt API key for account {account.id} (encryption key mismatch - account needs to be re-added): {key_error}")
                    continue
                
                if not api_key:
                    current_app.logger.debug(f"Skipping account {account.id} - no API key configured")
                    continue
                
                # Skip accounts that are consistently failing (reduce ping frequency)
                failure_count = self.account_failure_counts.get(account.id, 0)
                if failure_count >= max_failures:
                    # Only check every 10th cycle for failed accounts to reduce noise
                    skip_count = self.account_skip_counts.get(account.id, 0) + 1
                    self.account_skip_counts[account.id] = skip_count
                    
                    if skip_count % 10 != 0:  # Skip 9 out of 10 checks for failed accounts
                        continue
                
                # Create client and test ping with timeout
                try:
                    client = ExtendedOpenAlgoAPI(
                        api_key=api_key,
                        host=account.host_url
                    )
                    
                    # Add timeout for ping request
                    start_time = time.time()
                    ping_response = client.ping()
                    response_time = time.time() - start_time
                    
                    # Log slow responses
                    if response_time > 5.0:
                        current_app.logger.warning(f"Slow ping response for account {account.id}: {response_time:.2f}s")
                        
                except Exception as e:
                    error_str = str(e)
                    if "timeout" in error_str.lower():
                        raise Exception("Connection timeout - server may be down")
                    elif "connection" in error_str.lower() and ("refused" in error_str.lower() or "failed" in error_str.lower()):
                        raise Exception("OpenAlgo server not running")
                    elif "unreachable" in error_str.lower() or "no route" in error_str.lower():
                        raise Exception("Network unreachable")
                    else:
                        raise Exception(f"Connection failed - {error_str}")
                
                if ping_response.get('status') == 'success':
                    # Ping successful - reset failure count and skip count, update status
                    if account.id in self.account_failure_counts:
                        del self.account_failure_counts[account.id]
                    if account.id in self.account_skip_counts:
                        del self.account_skip_counts[account.id]
                    
                    # Update connection status if it was previously failed
                    if account.connection_status != 'connected':
                        self._update_account_status(account, 'connected', 
                                                  f"Connection restored to {account.broker_name}")
                        self._log_activity(account, 'connection_restored')
                        self._send_notification(account, 'success', 
                                              f"✓ Connection restored to {account.account_name}")
                
                else:
                    # Ping failed - increment failure count
                    self.account_failure_counts[account.id] = \
                        self.account_failure_counts.get(account.id, 0) + 1
                    
                    failure_count = self.account_failure_counts[account.id]
                    
                    if failure_count >= max_failures:
                        # Mark as disconnected after max failures
                        if account.connection_status != 'failed':
                            error_msg = ping_response.get('message', 'Connection failed')
                            self._update_account_status(account, 'failed', error_msg)
                            self._log_activity(account, 'connection_failed', {'error': error_msg})
                            self._send_notification(account, 'error', 
                                                  f"✗ Connection failed: {account.account_name}")
                    
                    elif failure_count == 1:
                        # First failure - send warning
                        self._send_notification(account, 'warning', 
                                              f"⚠ Connection issue detected: {account.account_name}")
            
            except Exception as e:
                # Handle connection errors
                self.account_failure_counts[account.id] = \
                    self.account_failure_counts.get(account.id, 0) + 1
                
                failure_count = self.account_failure_counts[account.id]
                
                if failure_count >= max_failures:
                    if account.connection_status != 'error':
                        self._update_account_status(account, 'error', f"Connection error: {str(e)}")
                        self._log_activity(account, 'connection_error', {'error': str(e)})
                        self._send_notification(account, 'error', 
                                              f"✗ Connection error: {account.account_name}")
                
                # Only log warnings if not in quiet mode
                if not self.app.config.get('PING_QUIET_MODE', False):
                    current_app.logger.warning(f"Ping check failed for account {account.id}: {str(e)}")
                elif failure_count == 1:  # Log first failure even in quiet mode
                    current_app.logger.info(f"Account {account.id} connection failed, will retry {max_failures - 1} more times")
    
    def _update_account_status(self, account, status, message=None):
        """Update account connection status in database"""
        try:
            # Refresh the account object to avoid stale data
            db.session.refresh(account)
            account.connection_status = status
            account.last_connected = datetime.utcnow() if status == 'connected' else account.last_connected
            account.updated_at = datetime.utcnow()
            
            db.session.commit()
            current_app.logger.info(f"Account {account.id} status updated to {status}: {message}")
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Failed to update account {account.id} status: {str(e)}")
            raise
    
    def _log_activity(self, account, action, details=None):
        """Log activity for audit trail"""
        try:
            log_entry = ActivityLog(
                user_id=account.user_id,
                account_id=account.id,
                action=action,
                details=details or {},
                status='info'
            )
            db.session.add(log_entry)
            db.session.commit()
        except Exception as e:
            db.session.rollback()
            current_app.logger.error(f"Failed to log activity: {str(e)}")
    
    def _send_notification(self, account, type, message):
        """Send notification to user (stored for client-side pickup)"""
        try:
            # Log the notification for immediate debugging
            current_app.logger.info(f"Notification for user {account.user_id}: {message}")
            
            # Store as activity log for user to see in UI
            self._log_activity(
                account=account, 
                action=f'ping_notification_{type}',
                details={
                    'notification_type': type,
                    'message': message,
                    'connection_status': account.connection_status,
                    'timestamp': datetime.utcnow().isoformat()
                }
            )
            
        except Exception as e:
            current_app.logger.error(f"Failed to send notification: {str(e)}")
    
    def get_account_status_summary(self, user_id):
        """Get summary of all account statuses for a user"""
        try:
            accounts = TradingAccount.query.filter_by(user_id=user_id, is_active=True).all()
            
            summary = {
                'total': len(accounts),
                'connected': 0,
                'failed': 0,
                'error': 0,
                'accounts': []
            }
            
            for account in accounts:
                # Ensure we have fresh data
                try:
                    db.session.refresh(account)
                except Exception:
                    pass  # Continue with potentially stale data
                
                status_info = {
                    'id': account.id,
                    'name': account.account_name,
                    'broker': account.broker_name,
                    'status': account.connection_status or 'unknown',
                    'last_connected': account.last_connected.isoformat() if account.last_connected else None,
                    'failure_count': self.account_failure_counts.get(account.id, 0),
                    'updated_at': account.updated_at.isoformat() if account.updated_at else None
                }
                
                summary['accounts'].append(status_info)
                
                if account.connection_status == 'connected':
                    summary['connected'] += 1
                elif account.connection_status == 'failed':
                    summary['failed'] += 1
                elif account.connection_status == 'error':
                    summary['error'] += 1
            
            return summary
            
        except Exception as e:
            current_app.logger.error(f"Failed to get account status summary: {str(e)}")
            return {
                'total': 0,
                'connected': 0,
                'failed': 0,
                'error': 0,
                'accounts': [],
                'error': str(e)
            }
    
    def force_check_account(self, account_id):
        """Force immediate ping check for specific account"""
        try:
            account = TradingAccount.query.get(account_id)
            if not account:
                return {'status': 'error', 'message': 'Account not found'}
            
            # Safely retrieve API key with error handling
            api_key = None
            try:
                api_key = account.get_api_key()
            except Exception as key_error:
                return {'status': 'error', 'message': f'Failed to decrypt API key: {str(key_error)}'}
            
            if not api_key:
                return {'status': 'error', 'message': 'No API key configured'}
            
            # Create client and test ping with timeout
            try:
                client = ExtendedOpenAlgoAPI(
                    api_key=api_key,
                    host=account.host_url
                )
                
                # Add timeout for ping request
                start_time = time.time()
                ping_response = client.ping()
                response_time = time.time() - start_time
                
                # Log response time for manual checks
                current_app.logger.info(f"Manual ping check for account {account.id} took {response_time:.2f}s")
                
            except Exception as e:
                error_str = str(e)
                if "timeout" in error_str.lower():
                    raise Exception("Connection timeout - server may be down")
                elif "connection" in error_str.lower() and ("refused" in error_str.lower() or "failed" in error_str.lower()):
                    raise Exception("OpenAlgo server not running")
                elif "unreachable" in error_str.lower() or "no route" in error_str.lower():
                    raise Exception("Network unreachable")
                else:
                    raise Exception(f"Connection failed - {error_str}")
            
            if ping_response.get('status') == 'success':
                # Reset failure count and skip count, update status
                if account.id in self.account_failure_counts:
                    del self.account_failure_counts[account.id]
                if account.id in self.account_skip_counts:
                    del self.account_skip_counts[account.id]
                
                self._update_account_status(account, 'connected', 'Manual check successful')
                return {
                    'status': 'success', 
                    'message': 'Connection successful',
                    'broker': ping_response.get('data', {}).get('broker', 'Unknown')
                }
            else:
                error_msg = ping_response.get('message', 'Connection failed')
                self._update_account_status(account, 'failed', error_msg)
                return {'status': 'error', 'message': error_msg}
        
        except Exception as e:
            error_msg = str(e)
            self._update_account_status(account, 'error', f"Check failed: {error_msg}")
            return {'status': 'error', 'message': error_msg}


# Global ping monitor instance
ping_monitor = PingMonitor()