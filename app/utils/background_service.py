"""
Background Service for Automatic Option Chain Monitoring
Automatically starts option chains when primary account connects

Uses standard threading for background tasks.
"""

import logging
import threading
from datetime import datetime, time, timedelta, date
from typing import Optional, Dict, Any, List
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger
import pytz
from flask import current_app
from sqlalchemy import and_

# Cross-platform compatibility
from app.utils.compat import sleep, spawn, spawn_n, create_lock

from app.models import TradingAccount, TradingHoursTemplate, TradingSession, MarketHoliday, SpecialTradingSession
from app.utils.option_chain import OptionChainManager
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.position_monitor import position_monitor
from app.utils.risk_manager import risk_manager
from app.utils.session_manager import session_manager

logger = logging.getLogger(__name__)


class OptionChainBackgroundService:
    """
    Background service that automatically starts option chain monitoring
    when a primary account connects
    """

    _instance = None
    _lock = threading.Lock()  # Use threading.Lock for singleton pattern (works on both platforms)
    
    def __new__(cls):
        if cls._instance is None:
            with cls._lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance
    
    def __init__(self):
        if self._initialized:
            return

        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
        self.active_managers = {}
        self.websocket_managers = {}
        self.is_running = False
        self.primary_account = None
        self.backup_accounts = []
        self._initialized = True
        self.cached_holidays = {}  # Cache holidays to avoid DB queries
        self.cached_sessions = {}  # Cache trading sessions
        self.cached_special_sessions = {}  # Cache special sessions
        self.cache_refresh_time = None

        # NEW: Position monitoring and risk management
        self.position_monitor_running = False
        self.risk_manager_running = False
        self.flask_app = None  # Store Flask app for app context in threads
        self.shared_websocket_manager = None  # Single shared WebSocket manager for all services

        logger.debug("Option Chain Background Service initialized")

    def set_flask_app(self, app):
        """Store Flask app instance for use in background threads"""
        self.flask_app = app
        logger.debug("Flask app instance registered with background service")

    def get_or_create_shared_websocket(self):
        """
        Get or create the single shared WebSocket manager for all services.

        This ensures only ONE WebSocket connection to OpenAlgo, which prevents
        broker connection limit errors (e.g., AngelOne Error 429).

        Returns:
            ProfessionalWebSocketManager instance or None if creation fails
        """
        # Return existing if already created and connected
        if self.shared_websocket_manager and \
           self.shared_websocket_manager.authenticated:
            logger.debug("Using existing shared WebSocket manager")
            return self.shared_websocket_manager

        # Create new shared WebSocket manager if needed
        if not self.primary_account:
            logger.warning("No primary account available for WebSocket connection")
            return None

        try:
            logger.debug("Creating shared WebSocket manager for all services")

            ws_manager = ProfessionalWebSocketManager()
            ws_manager.create_connection_pool(
                primary_account=self.primary_account,
                backup_accounts=self.backup_accounts
            )

            if hasattr(self.primary_account, 'websocket_url'):
                connected = ws_manager.connect(
                    ws_url=self.primary_account.websocket_url,
                    api_key=self.primary_account.get_api_key()
                )

                # Wait for authentication
                auth_wait_time = 0
                while not ws_manager.authenticated and auth_wait_time < 5:
                    sleep(0.5)
                    auth_wait_time += 0.5

                if not ws_manager.authenticated:
                    logger.error("Shared WebSocket authentication failed")
                    return None

                # Store as shared instance
                self.shared_websocket_manager = ws_manager
                logger.debug("✅ Shared WebSocket manager created and authenticated")
                return ws_manager
            else:
                logger.error("Primary account missing websocket_url")
                return None

        except Exception as e:
            logger.error(f"Error creating shared WebSocket manager: {e}")
            return None

    def start_service(self):
        """Start the background service"""
        if not self.is_running:
            # Check if scheduler is actually already running
            if self.scheduler.running:
                self.is_running = True
                logger.debug("Background service already running")
                return

            self.scheduler.start()
            self.is_running = True
            logger.debug("Background service started")

            # Schedule market hours check
            self.schedule_market_hours()

            # NEW: Schedule risk manager to run every 1 second
            self.scheduler.add_job(
                func=self.run_risk_checks,
                trigger='interval',
                seconds=1,
                id='risk_manager_check',
                replace_existing=True
            )
            logger.debug("Risk manager scheduled (1-second interval)")

            # NEW: Schedule session cleanup to run every minute
            self.scheduler.add_job(
                func=self.cleanup_sessions,
                trigger='interval',
                minutes=1,
                id='session_cleanup',
                replace_existing=True
            )
            logger.debug("Session cleanup scheduled (1-minute interval)")
    
    def stop_service(self):
        """Stop the background service"""
        if self.is_running:
            # Stop all option chain managers
            for underlying in list(self.active_managers.keys()):
                self.stop_option_chain(underlying)

            # NEW: Stop position monitor and risk manager
            self.stop_position_monitor()
            self.stop_risk_manager()

            # Disconnect shared WebSocket manager
            if self.shared_websocket_manager:
                try:
                    self.shared_websocket_manager.disconnect()
                    logger.debug("Shared WebSocket manager disconnected")
                except Exception as e:
                    logger.error(f"Error disconnecting shared WebSocket: {e}")
                finally:
                    self.shared_websocket_manager = None

            self.scheduler.shutdown(wait=False)
            self.is_running = False
            logger.debug("Background service stopped")
    
    def on_primary_account_connected(self, account: TradingAccount):
        """
        Called when primary account successfully connects
        Automatically starts NIFTY, BANKNIFTY, and SENSEX option chains
        """
        try:
            logger.debug(f"Primary account connected: {account.account_name}")
            self.primary_account = account
            
            # Get backup accounts for failover
            self.backup_accounts = TradingAccount.query.filter_by(
                user_id=account.user_id,
                is_active=True,
                is_primary=False
            ).order_by(TradingAccount.created_at).all()
            
            # Check if within trading hours
            if self.is_trading_hours():
                # DISABLED: Automatic option chain loading (now on-demand via SessionManager)
                # Option chains will load only when users visit /trading/option-chain
                # This reduces WebSocket subscriptions from 1000+ to just open positions (5-50)

                def start_services():
                    sleep(2)  # Give Flask time to start

                    # Run services within Flask app context
                    if self.flask_app:
                        with self.flask_app.app_context():
                            # COMMENTED OUT: Automatic option chain subscriptions
                            # self.start_option_chain('NIFTY')  # Will load 4 expiries (328 symbols)
                            # self.start_option_chain('BANKNIFTY')  # Will load 4 expiries (328 symbols)
                            # self.start_option_chain('SENSEX')  # Will load 4 expiries (328 symbols)
                            # Total: ~984 symbols subscribed automatically

                            logger.debug("Option chains DISABLED - using on-demand loading via SessionManager")

                            # START: Position monitor and risk manager (essential services)
                            self.start_position_monitor()
                            self.start_risk_manager()

                            # Initialize SessionManager with SHARED WebSocket manager
                            # This ensures all services use the SAME connection
                            if self.shared_websocket_manager:
                                session_manager.set_websocket_manager(self.shared_websocket_manager)
                                logger.debug("SessionManager initialized with shared WebSocket connection")
                            else:
                                logger.warning("No shared WebSocket manager available for SessionManager")

                            logger.debug("Position monitor and risk manager started")
                    else:
                        logger.error("Flask app not set - cannot start services")

                # Spawn background task (greenlet on Linux, thread on Windows)
                spawn(start_services)
            else:
                logger.debug("Outside trading hours, services will start at market open")
                
        except Exception as e:
            logger.error(f"Error starting option chains on account connection: {e}")
    
    def on_account_disconnected(self, account: TradingAccount):
        """
        Called when an account disconnects
        Triggers failover if it was the primary account
        """
        if account == self.primary_account:
            logger.warning(f"Primary account disconnected: {account.account_name}")
            self.attempt_failover()
    
    def attempt_failover(self):
        """Attempt to failover to backup account"""
        if not self.backup_accounts:
            logger.error("No backup accounts available for failover")
            self.stop_all_option_chains()
            return
        
        # Try next backup account
        next_account = self.backup_accounts.pop(0)
        logger.debug(f"Attempting failover to: {next_account.account_name}")
        
        try:
            # Test connection
            client = ExtendedOpenAlgoAPI(
                api_key=next_account.get_api_key(),
                host=next_account.host_url
            )
            
            ping_response = client.ping()
            if ping_response.get('status') == 'success':
                # Update primary account
                self.primary_account = next_account
                
                # Restart all active option chains with new account
                active_underlyings = set()
                for key in self.active_managers.keys():
                    underlying = key.split('_')[0]
                    active_underlyings.add(underlying)
                
                for underlying in active_underlyings:
                    self.restart_option_chain(underlying)
                
                logger.debug(f"Failover successful to: {next_account.account_name}")
            else:
                # Try next backup
                self.attempt_failover()
                
        except Exception as e:
            logger.error(f"Failover failed for {next_account.account_name}: {e}")
            self.attempt_failover()
    
    def start_option_chain(self, underlying: str, expiry: str = None):
        """Start option chain monitoring for specified underlying and expiry"""
        if not self.primary_account:
            logger.warning("No primary account available, attempting failover")
            # Try to failover to a backup account
            if self.backup_accounts:
                self.attempt_failover()
                # After failover attempt, check if we have a primary now
                if not self.primary_account:
                    logger.error("Failover failed - no accounts available")
                    return False
            else:
                logger.error("No primary or backup accounts available")
                return False
        
        try:
            # Create API client - try primary first, then backup
            client = ExtendedOpenAlgoAPI(
                api_key=self.primary_account.get_api_key(),
                host=self.primary_account.host_url
            )
            
            # Get expiry dates if not provided
            if not expiry:
                expiry_response = client.expiry(
                    symbol=underlying,
                    exchange='BFO' if underlying == 'SENSEX' else 'NFO',
                    instrumenttype='options'
                )
                
                # If primary fails, try backup accounts
                if expiry_response.get('status') != 'success':
                    logger.warning(f"Primary account failed to get expiry for {underlying}, trying backup accounts")
                    
                    for backup in self.backup_accounts:
                        logger.debug(f"Trying backup account: {backup.account_name}")
                        backup_client = ExtendedOpenAlgoAPI(
                            api_key=backup.get_api_key(),
                            host=backup.host_url
                        )
                        
                        expiry_response = backup_client.expiry(
                            symbol=underlying,
                            exchange='BFO' if underlying == 'SENSEX' else 'NFO',
                            instrumenttype='options'
                        )
                        
                        if expiry_response.get('status') == 'success':
                            logger.debug(f"Successfully got expiry from backup account: {backup.account_name}")
                            client = backup_client  # Use backup client for further operations
                            break
                    else:
                        logger.error(f"All accounts failed to get expiry for {underlying}")
                        return False
                
                expiries = expiry_response.get('data', [])
                if not expiries:
                    logger.error(f"No expiries available for {underlying}")
                    return False
                
                # Get first 4 expiries for streaming
                expiries_to_use = expiries[:4] if len(expiries) >= 4 else expiries
            else:
                expiries_to_use = [expiry]
            
            all_managers_started = True
            
            # Start manager for each expiry
            for exp in expiries_to_use:
                manager_key = f"{underlying}_{exp}"
                
                if manager_key in self.active_managers:
                    logger.debug(f"Option chain already running for {manager_key}")
                    continue
            
                # Create or get WebSocket manager for this underlying
                ws_manager_key = underlying
                if ws_manager_key not in self.websocket_managers:
                    ws_manager = ProfessionalWebSocketManager()
                    ws_manager.create_connection_pool(
                        primary_account=self.primary_account,
                        backup_accounts=self.backup_accounts
                    )
                    
                    # Connect WebSocket with failover support
                    if hasattr(self.primary_account, 'websocket_url'):
                        connected = ws_manager.connect(
                            ws_url=self.primary_account.websocket_url,
                            api_key=self.primary_account.get_api_key()
                        )
                        
                        # If primary failed, ws_manager would have tried failover
                        # Check if we're now connected to a different account
                        current_account = ws_manager.connection_pool.get('current_account')
                        if current_account and current_account != self.primary_account:
                            logger.debug(f"WebSocket failover occurred: now using {current_account.account_name}")
                        
                        # Wait for authentication to complete (max 5 seconds)
                        auth_wait_time = 0
                        while not ws_manager.authenticated and auth_wait_time < 5:
                            sleep(0.5)
                            auth_wait_time += 0.5
                            
                        if not ws_manager.authenticated:
                            logger.error(f"WebSocket authentication failed for {underlying} after failover attempts")
                            all_managers_started = False
                            continue
                        
                        logger.debug(f"WebSocket authenticated for {underlying}")
                    
                    self.websocket_managers[ws_manager_key] = ws_manager
                else:
                    ws_manager = self.websocket_managers[ws_manager_key]
                
                # Create option chain manager for this expiry
                option_manager = OptionChainManager(
                    underlying=underlying,
                    expiry=exp,
                    websocket_manager=ws_manager
                )
                
                # Initialize with API client
                option_manager.initialize(client)
                
                # Start monitoring
                option_manager.start_monitoring()
                
                # Store managers with unique key
                self.active_managers[manager_key] = option_manager
                
                logger.debug(f"Option chain started for {manager_key}")
            
            return all_managers_started
            
        except Exception as e:
            logger.error(f"Error starting option chain for {underlying}: {e}")
            return False
    
    def stop_option_chain(self, underlying: str, expiry: str = None):
        """Stop option chain monitoring for specified underlying and optionally expiry"""
        try:
            if expiry:
                # Stop specific expiry
                manager_key = f"{underlying}_{expiry}"
                if manager_key in self.active_managers:
                    manager = self.active_managers[manager_key]
                    manager.stop_monitoring()
                    del self.active_managers[manager_key]
                    logger.debug(f"Option chain stopped for {manager_key}")
            else:
                # Stop all expiries for this underlying
                keys_to_remove = [k for k in self.active_managers.keys() if k.startswith(f"{underlying}_")]
                for key in keys_to_remove:
                    manager = self.active_managers[key]
                    manager.stop_monitoring()
                    del self.active_managers[key]
                    logger.debug(f"Option chain stopped for {key}")
                
                # Disconnect WebSocket for this underlying
                if underlying in self.websocket_managers:
                    ws_manager = self.websocket_managers[underlying]
                    ws_manager.disconnect()
                    del self.websocket_managers[underlying]
                
        except Exception as e:
            logger.error(f"Error stopping option chain for {underlying}: {e}")
    
    def restart_option_chain(self, underlying: str, expiry: str = None):
        """Restart option chain with current primary account"""
        logger.debug(f"Restarting option chain for {underlying} {expiry or 'all expiries'}")
        self.stop_option_chain(underlying, expiry)
        self.start_option_chain(underlying, expiry)
    
    def stop_all_option_chains(self):
        """Stop all active option chains"""
        for underlying in list(self.active_managers.keys()):
            self.stop_option_chain(underlying)
    
    def schedule_market_hours(self):
        """Schedule option chains based on trading hours template"""
        try:
            # Refresh cache of trading hours
            self.refresh_trading_hours_cache()
            
            # Get trading sessions from database or use defaults
            sessions = self.get_trading_sessions()
            
            for session in sessions:
                if not session.get('is_active'):
                    continue
                    
                day = session['day_of_week']
                start_time = session['start_time']
                end_time = session['end_time']
                
                # Schedule WebSocket start 15 minutes before market open
                pre_market_time = (datetime.combine(date.today(), start_time) - timedelta(minutes=15)).time()
                
                # Schedule pre-market WebSocket start
                self.scheduler.add_job(
                    func=self.on_pre_market_open,
                    trigger=CronTrigger(
                        day_of_week=day,
                        hour=pre_market_time.hour,
                        minute=pre_market_time.minute,
                        timezone=pytz.timezone('Asia/Kolkata')
                    ),
                    id=f"pre_market_open_{day}",
                    replace_existing=True
                )
                
                # Schedule market close
                self.scheduler.add_job(
                    func=self.on_market_close,
                    trigger=CronTrigger(
                        day_of_week=day,
                        hour=end_time.hour,
                        minute=end_time.minute,
                        timezone=pytz.timezone('Asia/Kolkata')
                    ),
                    id=f"market_close_{day}",
                    replace_existing=True
                )
                
                logger.debug(f"Scheduled {['Mon','Tue','Wed','Thu','Fri','Sat','Sun'][day]}: "
                          f"WebSocket {pre_market_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')} "
                          f"(Market {start_time.strftime('%H:%M')} - {end_time.strftime('%H:%M')})") 
            
            # Schedule special sessions
            self.schedule_special_sessions()
            
            # Schedule cache refresh daily at 5 AM
            self.scheduler.add_job(
                func=self.refresh_trading_hours_cache,
                trigger=CronTrigger(
                    hour=5,
                    minute=0,
                    timezone=pytz.timezone('Asia/Kolkata')
                ),
                id="refresh_cache",
                replace_existing=True
            )
            
            logger.debug("Market hours scheduled from trading template")
            
        except Exception as e:
            logger.error(f"Error scheduling market hours: {e}")
            # Fallback to default NSE hours
            self.schedule_default_hours()
    
    def on_pre_market_open(self):
        """Called 15 minutes before market opens for service initialization"""
        now = datetime.now(pytz.timezone('Asia/Kolkata'))
        logger.debug(f"Pre-market start at {now.strftime('%H:%M:%S')} - Starting services")

        if self.primary_account:
            if not self.is_holiday():
                # Run services within Flask app context
                if self.flask_app:
                    with self.flask_app.app_context():
                        # DISABLED: Automatic option chain subscriptions
                        # Option chains now load on-demand only (via SessionManager)
                        # self.start_option_chain('NIFTY')
                        # self.start_option_chain('BANKNIFTY')
                        # self.start_option_chain('SENSEX')

                        logger.debug("Option chains DISABLED - using on-demand loading")

                        # START: Position monitor and risk manager (essential services)
                        self.start_position_monitor()
                        self.start_risk_manager()

                        # Initialize SessionManager with WebSocket manager for on-demand option chains
                        ws_manager = self.websocket_managers.get('POSITION_MONITOR') or self.websocket_managers.get('NIFTY')
                        if ws_manager:
                            session_manager.set_websocket_manager(ws_manager)
                            logger.debug("SessionManager initialized with WebSocket manager in pre-market")
                        else:
                            logger.warning("No WebSocket manager available for SessionManager in pre-market")

                        logger.debug("Position monitor and risk manager started in pre-market")
                else:
                    logger.error("Flask app not set - cannot start pre-market services")
            else:
                logger.debug("Market holiday - services not started")
    
    def on_market_open(self):
        """Called when market opens (legacy, kept for compatibility)"""
        logger.debug("Market opened - option chains should already be running from pre-market")
    
    def on_market_close(self):
        """Called when market closes"""
        logger.debug("Market closed - stopping option chains")
        self.stop_all_option_chains()

        # NEW: Stop position monitor and risk manager
        self.stop_position_monitor()
        self.stop_risk_manager()
    
    def is_trading_hours(self) -> bool:
        """Check if current time is within trading hours (including 15-min pre-market)"""
        try:
            now = datetime.now(pytz.timezone('Asia/Kolkata'))
            current_day = now.weekday()
            current_time = now.time()
            current_date = now.date()
            
            # Check for special trading sessions first
            if self.has_special_session(current_date, current_time):
                return True
            
            # Check if holiday (skip if special session already checked)
            if self.is_holiday(current_date):
                return False
            
            # Get trading sessions for current day
            sessions = self.get_trading_sessions()
            for session in sessions:
                if session['day_of_week'] == current_day and session['is_active']:
                    # Include 15-minute pre-market buffer for WebSocket
                    pre_market_time = (datetime.combine(date.today(), session['start_time']) - timedelta(minutes=15)).time()
                    if pre_market_time <= current_time <= session['end_time']:
                        return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking trading hours: {e}")
            return False
    
    def has_special_session(self, check_date, check_time) -> bool:
        """Check if there's a special trading session at the given date and time"""
        try:
            # Use cached special sessions
            if check_date not in self.cached_special_sessions:
                return False
            
            for session in self.cached_special_sessions.get(check_date, []):
                # Include 15-minute pre-market buffer
                pre_market_time = (datetime.combine(check_date, session['start_time']) - timedelta(minutes=15)).time()
                if pre_market_time <= check_time <= session['end_time']:
                    logger.debug(f"Special trading session active: {session['session_name']}")
                    return True
            
            return False
            
        except Exception as e:
            logger.debug(f"Could not check special sessions: {e}")
            return False
    
    def is_holiday(self, check_date: Optional[date] = None) -> bool:
        """Check if given date is a market holiday"""
        try:
            if check_date is None:
                check_date = datetime.now(pytz.timezone('Asia/Kolkata')).date()
            
            # Use cached holidays
            if check_date in self.cached_holidays:
                holiday_info = self.cached_holidays[check_date]
                # Check if it's a holiday without special session
                if not holiday_info.get('is_special_session', False):
                    logger.debug(f"Market holiday: {holiday_info.get('holiday_name', 'Unknown')}")
                    return True
            
            return False
            
        except Exception as e:
            logger.error(f"Error checking holiday: {e}")
            return False
    
    def get_status(self) -> Dict[str, Any]:
        """Get current service status"""
        return {
            'is_running': self.is_running,
            'primary_account': self.primary_account.account_name if self.primary_account else None,
            'backup_accounts': len(self.backup_accounts),
            'active_option_chains': list(self.active_managers.keys()),
            'is_trading_hours': self.is_trading_hours(),
            'is_holiday': self.is_holiday(),
            'websocket_status': {
                underlying: ws.get_status() 
                for underlying, ws in self.websocket_managers.items()
            }
        }
    
    def refresh_trading_hours_cache(self):
        """Refresh cached trading hours, holidays, and special sessions"""
        try:
            # This method will be called within Flask app context
            logger.debug("Refreshing trading hours cache")
            
            # Cache holidays for the current year
            now = datetime.now(pytz.timezone('Asia/Kolkata'))
            year_start = date(now.year, 1, 1)
            year_end = date(now.year, 12, 31)
            
            # Note: These queries will only work within Flask app context
            # They'll be called from Flask routes or scheduled jobs with context
            try:
                from app import create_app, db
                from app.models import MarketHoliday, SpecialTradingSession, TradingSession
                
                app = create_app()
                with app.app_context():
                    # Cache holidays
                    holidays = MarketHoliday.query.filter(
                        and_(
                            MarketHoliday.holiday_date >= year_start,
                            MarketHoliday.holiday_date <= year_end
                        )
                    ).all()
                    
                    self.cached_holidays = {}
                    for holiday in holidays:
                        self.cached_holidays[holiday.holiday_date] = {
                            'holiday_name': holiday.holiday_name,
                            'market': holiday.market,
                            'is_special_session': holiday.is_special_session
                        }
                    
                    # Cache special sessions
                    special_sessions = SpecialTradingSession.query.filter(
                        and_(
                            SpecialTradingSession.session_date >= year_start,
                            SpecialTradingSession.session_date <= year_end,
                            SpecialTradingSession.is_active == True
                        )
                    ).all()
                    
                    self.cached_special_sessions = {}
                    for session in special_sessions:
                        if session.session_date not in self.cached_special_sessions:
                            self.cached_special_sessions[session.session_date] = []
                        self.cached_special_sessions[session.session_date].append({
                            'session_name': session.session_name,
                            'start_time': session.start_time,
                            'end_time': session.end_time,
                            'market': session.market
                        })
                    
                    # Cache regular trading sessions
                    sessions = TradingSession.query.filter_by(is_active=True).all()
                    self.cached_sessions = []
                    for session in sessions:
                        self.cached_sessions.append({
                            'day_of_week': session.day_of_week,
                            'start_time': session.start_time,
                            'end_time': session.end_time,
                            'is_active': session.is_active
                        })
                    
                    self.cache_refresh_time = datetime.now(pytz.timezone('Asia/Kolkata'))
                    logger.debug(f"Cache refreshed: {len(self.cached_holidays)} holidays, "
                              f"{len(self.cached_special_sessions)} special session dates, "
                              f"{len(self.cached_sessions)} regular sessions")
                              
            except Exception as e:
                logger.warning(f"Could not refresh cache from database: {e}")
                # Use default cache if database not available
                self.set_default_cache()
                
        except Exception as e:
            logger.error(f"Error refreshing trading hours cache: {e}")
            self.set_default_cache()
    
    def set_default_cache(self):
        """Set default NSE trading hours if database not available"""
        logger.debug("Using default NSE trading hours")
        self.cached_sessions = [
            {'day_of_week': i, 'start_time': time(9, 15), 'end_time': time(15, 30), 'is_active': True}
            for i in range(5)  # Monday to Friday
        ]
        self.cached_holidays = {}
        self.cached_special_sessions = {}
    
    def get_trading_sessions(self) -> List[Dict]:
        """Get trading sessions from cache or defaults"""
        if not self.cached_sessions:
            self.set_default_cache()
        return self.cached_sessions
    
    def schedule_special_sessions(self):
        """Schedule jobs for special trading sessions"""
        try:
            for session_date, sessions in self.cached_special_sessions.items():
                for session in sessions:
                    # Schedule pre-market start (15 minutes before)
                    pre_market_time = (datetime.combine(session_date, session['start_time']) - timedelta(minutes=15))
                    
                    if pre_market_time > datetime.now(pytz.timezone('Asia/Kolkata')):
                        self.scheduler.add_job(
                            func=self.on_special_session_start,
                            trigger='date',
                            run_date=pre_market_time,
                            timezone=pytz.timezone('Asia/Kolkata'),
                            id=f"special_start_{session_date}_{session['session_name']}",
                            replace_existing=True,
                            args=[session['session_name']]
                        )
                        
                        # Schedule session end
                        end_time = datetime.combine(session_date, session['end_time'])
                        self.scheduler.add_job(
                            func=self.on_special_session_end,
                            trigger='date',
                            run_date=end_time,
                            timezone=pytz.timezone('Asia/Kolkata'),
                            id=f"special_end_{session_date}_{session['session_name']}",
                            replace_existing=True,
                            args=[session['session_name']]
                        )
                        
                        logger.debug(f"Scheduled special session: {session['session_name']} on {session_date}")
                        
        except Exception as e:
            logger.error(f"Error scheduling special sessions: {e}")
    
    def on_special_session_start(self, session_name: str):
        """Called when a special trading session starts"""
        logger.debug(f"Special session started: {session_name}")
        if self.primary_account:
            self.start_option_chain('NIFTY')
            self.start_option_chain('BANKNIFTY')
            self.start_option_chain('SENSEX')
    
    def on_special_session_end(self, session_name: str):
        """Called when a special trading session ends"""
        logger.debug(f"Special session ended: {session_name}")
        self.stop_all_option_chains()

    def schedule_default_hours(self):
        """Fallback to schedule default NSE hours"""
        logger.debug("Scheduling default NSE hours as fallback")
        for day in range(5):  # Monday to Friday
            # Schedule pre-market start (9:00 AM - 15 minutes before market)
            self.scheduler.add_job(
                func=self.on_pre_market_open,
                trigger=CronTrigger(
                    day_of_week=day,
                    hour=9,
                    minute=0,
                    timezone=pytz.timezone('Asia/Kolkata')
                ),
                id=f"pre_market_open_{day}",
                replace_existing=True
            )

            # Schedule market close (3:30 PM)
            self.scheduler.add_job(
                func=self.on_market_close,
                trigger=CronTrigger(
                    day_of_week=day,
                    hour=15,
                    minute=30,
                    timezone=pytz.timezone('Asia/Kolkata')
                ),
                id=f"market_close_{day}",
                replace_existing=True
            )

    # NEW METHODS FOR POSITION MONITORING AND RISK MANAGEMENT

    def start_position_monitor(self):
        """Start position monitoring (subscribes to open positions only)"""
        if self.position_monitor_running:
            logger.debug("Position monitor already running")
            return

        try:
            # Use the single shared WebSocket manager for all services
            # This prevents creating multiple connections to OpenAlgo
            ws_manager = self.get_or_create_shared_websocket()

            if not ws_manager:
                logger.error("Failed to get shared WebSocket manager for position monitor")
                return

            # Start position monitor with shared WebSocket manager and Flask app
            position_monitor.start(ws_manager, app=self.flask_app)
            self.position_monitor_running = True
            logger.debug("✅ Position monitor started with shared WebSocket connection")

        except Exception as e:
            logger.error(f"Error starting position monitor: {e}")

    def stop_position_monitor(self):
        """Stop position monitoring"""
        if not self.position_monitor_running:
            return

        try:
            position_monitor.stop()
            self.position_monitor_running = False
            logger.debug("Position monitor stopped")

            # DO NOT disconnect shared WebSocket - other services may be using it
            # Shared WebSocket is only disconnected when service stops completely

        except Exception as e:
            logger.error(f"Error stopping position monitor: {e}")

    def start_risk_manager(self):
        """Start risk manager"""
        if self.risk_manager_running:
            logger.debug("Risk manager already running")
            return

        try:
            risk_manager.start()
            self.risk_manager_running = True
            logger.debug("Risk manager started")
        except Exception as e:
            logger.error(f"Error starting risk manager: {e}")

    def stop_risk_manager(self):
        """Stop risk manager"""
        if not self.risk_manager_running:
            return

        try:
            risk_manager.stop()
            self.risk_manager_running = False
            logger.debug("Risk manager stopped")
        except Exception as e:
            logger.error(f"Error stopping risk manager: {e}")

    def run_risk_checks(self):
        """Run risk checks (called by scheduler every 1 second)"""
        if not self.risk_manager_running:
            return

        try:
            # Run within Flask app context for database access
            if self.flask_app:
                def _do_risk_check():
                    try:
                        with self.flask_app.app_context():
                            risk_manager.run_risk_checks()
                    except Exception as e:
                        logger.error(f"Error in risk check: {e}")
                # Spawn background task (greenlet on Linux, thread on Windows)
                spawn_n(_do_risk_check)
            else:
                logger.warning("Flask app not available for risk checks")
        except Exception as e:
            logger.error(f"Error running risk checks: {e}")

    def cleanup_sessions(self):
        """Clean up expired option chain sessions (called by scheduler every 1 minute)"""
        try:
            # Run within Flask app context for database access
            if self.flask_app:
                def _do_cleanup():
                    try:
                        with self.flask_app.app_context():
                            session_manager.cleanup_expired_sessions()
                    except Exception as e:
                        logger.error(f"Error in cleanup: {e}")
                # Spawn background task (greenlet on Linux, thread on Windows)
                spawn_n(_do_cleanup)
            else:
                logger.warning("Flask app not available for session cleanup")
        except Exception as e:
            logger.error(f"Error cleaning up sessions: {e}")


# Global service instance
option_chain_service = OptionChainBackgroundService()