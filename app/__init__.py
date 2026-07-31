# AlgoMirror Flask Application Factory
# Uses standard threading for background tasks (no eventlet - deprecated and Python 3.13+ incompatible)

import os
import logging
import warnings
from logging.handlers import RotatingFileHandler
from flask import Flask

# Suppress numba warning about nopython parameter
warnings.filterwarnings('ignore', message='nopython is set for njit and is ignored', category=RuntimeWarning)
from flask_sqlalchemy import SQLAlchemy
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_wtf.csrf import CSRFProtect, CSRFError
from flask_cors import CORS
from flask_talisman import Talisman
from flask_session import Session
from pythonjsonlogger import jsonlogger
from config import config

# expire_on_commit=False prevents SQLAlchemy from expiring all attributes after commit
# Without this, every attribute access after commit triggers a lazy-load query
# With PostgreSQL, each lazy-load is a TCP round-trip (~1-5ms)
db = SQLAlchemy(session_options={'expire_on_commit': False})
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
sess = Session()
limiter = None
_registration_cache = {}  # Cached result for registration check (single-user app)

# Enable WAL mode for ALL SQLite connections (class-level, fires for every connection)
# WAL allows concurrent reads during writes - prevents 504 timeouts
from sqlalchemy import event
from sqlalchemy.engine import Engine

@event.listens_for(Engine, "connect")
def _set_sqlite_pragma(dbapi_connection, connection_record):
    import sqlite3
    if isinstance(dbapi_connection, sqlite3.Connection):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA busy_timeout=30000")  # 30s timeout for background service contention
        cursor.close()

def setup_logging(app):
    """Set up centralized logging with JSON format"""

    if not os.path.exists('logs'):
        os.mkdir('logs')

    # CRITICAL: Disable all propagation to root and set levels FIRST
    # This must happen before any handlers are added
    noisy_loggers = [
        'app.utils.websocket_manager',
        'app.utils.background_service',
        'app.utils.option_chain',
        'app.trading.routes',
        'werkzeug'
    ]

    # Set logging levels for noisy modules - do this ALWAYS, not just first time
    for logger_name in noisy_loggers:
        noisy_logger = logging.getLogger(logger_name)
        noisy_logger.setLevel(logging.WARNING)  # Block DEBUG and INFO
        noisy_logger.propagate = False  # CRITICAL: Don't propagate to root
        noisy_logger.handlers = []  # Clear any existing handlers

    # Clear all root and app handlers first
    root_logger = logging.getLogger()
    root_logger.handlers.clear()
    root_logger.setLevel(logging.INFO)

    # Check if we already set up our custom handler
    from logging.handlers import RotatingFileHandler as RFH
    custom_handler_exists = any(
        isinstance(h, (logging.FileHandler, RFH))
        for h in app.logger.handlers
    )
    if custom_handler_exists:
        return

    # Clear Flask's default handlers
    app.logger.handlers.clear()
    app.logger.propagate = False

    # JSON formatter for structured logging
    # Use simple FileHandler on Windows to avoid rotation issues
    import platform
    is_windows = platform.system() == 'Windows'

    if is_windows:
        # On Windows, use simple FileHandler to avoid rotation conflicts
        from logging import FileHandler
        logHandler = FileHandler('logs/algomirror.log', mode='a')
    else:
        # On Unix systems, use RotatingFileHandler
        logHandler = RotatingFileHandler(
            'logs/algomirror.log',
            maxBytes=10485760,
            backupCount=10
        )
    formatter = jsonlogger.JsonFormatter(
        fmt='%(asctime)s %(levelname)s %(name)s %(message)s',
        datefmt='%Y-%m-%d %H:%M:%S'
    )
    logHandler.setFormatter(formatter)

    # Create a filter to suppress noisy loggers at DEBUG/INFO level
    class NoisyLoggerFilter(logging.Filter):
        def filter(self, record):
            # Block DEBUG and INFO from noisy modules
            if record.name in noisy_loggers and record.levelno < logging.WARNING:
                return False
            return True

    logHandler.addFilter(NoisyLoggerFilter())

    # Set log level from config
    log_level = getattr(logging, app.config['LOG_LEVEL'].upper(), logging.INFO)
    logHandler.setLevel(log_level)

    # Add handler to app logger
    app.logger.addHandler(logHandler)
    app.logger.setLevel(log_level)

    app.logger.info('AlgoMirror startup', extra={'event': 'startup'})

    # Also log to console in development
    if app.debug:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        app.logger.addHandler(console_handler)

    # Attach centralized JSON error log + sensitive-data redaction filter
    # for the /diagnose feature. ERROR+ records from any logger land in
    # logs/errors.jsonl. The filter wraps every existing handler too, so
    # an accidental logger.error(f"...{api_key}...") gets redacted before
    # it touches disk.
    try:
        from app.utils.json_error_log import attach_json_error_handler
        attach_json_error_handler(app, log_dir='logs', filename='errors.jsonl')
    except Exception as e:
        app.logger.warning(f'Failed to attach JSON error handler: {e}')

def create_app(config_name=None):
    global limiter
    
    if config_name is None:
        config_name = os.environ.get('FLASK_ENV', 'development')
    
    app = Flask(__name__)
    app.config.from_object(config[config_name])
    
    # Initialize extensions
    db.init_app(app)
    login_manager.init_app(app)
    migrate.init_app(app, db)
    csrf.init_app(app)

    # Configure session to use database if sqlalchemy type
    if app.config.get('SESSION_TYPE') == 'sqlalchemy':
        app.config['SESSION_SQLALCHEMY'] = db

    sess.init_app(app)

    # Import models and create tables
    with app.app_context():
        from app import models
        db.create_all()

        # Verify WAL mode is active (should print "wal" on startup)
        if 'sqlite' in app.config.get('SQLALCHEMY_DATABASE_URI', ''):
            from sqlalchemy import text
            result = db.session.execute(text("PRAGMA journal_mode"))
            mode = result.scalar()
            print(f"[SQLite] Journal mode: {mode}", flush=True)
            db.session.rollback()  # Don't leave open transaction
    
    # Initialize rate limiter
    from app.utils.rate_limiter import init_rate_limiter
    limiter = init_rate_limiter(app)
    
    # Setup CORS with specific origins
    CORS(app, 
         origins=app.config['CORS_ORIGINS'],
         supports_credentials=True,
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
         allow_headers=['Content-Type', 'X-CSRFToken'])
    
    # Setup CSP with Talisman (configurable via environment)
    if app.config.get('CSP_ENABLED', False):
        csp = app.config['CSP'].copy()

        # Add upgrade-insecure-requests if enabled
        if app.config.get('CSP_UPGRADE_INSECURE_REQUESTS', False):
            csp['upgrade-insecure-requests'] = True

        # Add report-uri if configured
        if app.config.get('CSP_REPORT_URI'):
            csp['report-uri'] = [app.config['CSP_REPORT_URI']]

        # Determine if we should use report-only mode
        report_only = app.config.get('CSP_REPORT_ONLY', False)

        # Apply Talisman with CSP
        Talisman(app,
                force_https=(not app.debug),  # Only force HTTPS in production
                strict_transport_security=(not app.debug),
                content_security_policy=csp,
                content_security_policy_report_only=report_only)
    
    # Setup logging
    setup_logging(app)
    
    # Login manager configuration
    login_manager.login_view = 'auth.login'
    login_manager.login_message = 'Please log in to access this page.'
    login_manager.login_message_category = 'info'
    
    # Register blueprints
    from app.auth import auth_bp
    from app.main import main_bp
    from app.accounts import accounts_bp
    from app.trading import trading_bp
    from app.trading.settings_routes import settings_bp
    from app.strategy import strategy_bp
    from app.margin import margin_bp
    from app.api import api_bp
    from app.tradingview import tradingview_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(main_bp)
    app.register_blueprint(accounts_bp, url_prefix='/accounts')
    app.register_blueprint(trading_bp, url_prefix='/trading')
    app.register_blueprint(settings_bp)  # Already has url_prefix in blueprint definition
    app.register_blueprint(strategy_bp)  # url_prefix defined in blueprint
    app.register_blueprint(margin_bp)  # url_prefix defined in blueprint
    app.register_blueprint(api_bp, url_prefix='/api')
    app.register_blueprint(tradingview_bp)  # url_prefix defined in blueprint (/tradingview)

    # Diagnose blueprint — admin-only debugging UI + APIs at /diagnose
    from app.diagnose import diagnose_bp
    app.register_blueprint(diagnose_bp)

    # Context processor for global template variables (cached - single-user app)
    @app.context_processor
    def inject_registration_status():
        """Make registration_available variable available to all templates.
        Cached in-memory since this is a single-user app - value only changes on registration."""
        if 'available' not in _registration_cache:
            from app.models import User
            _registration_cache['available'] = (User.query.count() == 0)
        return dict(registration_available=_registration_cache['available'])

    # Cache-busting for static assets. nginx serves /static/ with
    # `Cache-Control: public, max-age=2592000, immutable` (30 days) for
    # performance, which means browsers never even re-check compiled.css
    # after a deploy - a query param keyed on the file's mtime changes the
    # URL (and therefore the browser's cache key) every time the file
    # actually changes, without needing to touch that nginx policy.
    @app.template_global()
    def asset_version(filename):
        try:
            path = os.path.join(app.static_folder, filename)
            return str(int(os.path.getmtime(path)))
        except OSError:
            return '0'

    # CSRF error handler - redirects to login with message when session expires
    @app.errorhandler(CSRFError)
    def handle_csrf_error(e):
        from flask import flash, redirect, url_for, request
        app.logger.warning(f'CSRF error on {request.path}: {e.description}')
        flash('Your session has expired. Please refresh and try again.', 'warning')
        # Redirect to login page
        return redirect(url_for('auth.login'))

    # 404 handler — quiet for asset/probe paths, logged for everything else.
    # Captures the path so the /diagnose page can show broken routes.
    @app.errorhandler(404)
    def handle_404(e):
        from flask import request, jsonify
        safe_prefixes = (
            '/favicon', '/robots.txt', '/sitemap', '/manifest',
            '/sw.js', '/.well-known', '/apple-touch-icon',
            '/service-worker', '/workbox',
        )
        if not request.path.startswith(safe_prefixes):
            app.logger.warning(f'404 Not Found: {request.method} {request.path}')
        if request.is_json or request.path.startswith('/api/') or request.path.startswith('/diagnose/api/'):
            return jsonify({'status': 'error', 'message': 'Not found'}), 404
        return e, 404

    # 500 handler — log with full traceback so it lands in errors.jsonl.
    # Returns plain text/JSON to avoid leaking traceback to the browser.
    @app.errorhandler(500)
    def handle_500(e):
        from flask import request, jsonify
        app.logger.error(f'500 Server Error on {request.method} {request.path}: {e}', exc_info=True)
        if request.is_json or request.path.startswith('/api/') or request.path.startswith('/diagnose/api/'):
            return jsonify({'status': 'error', 'message': 'Internal server error'}), 500
        return 'Internal server error', 500

    # Unhandled exception → logged with traceback, then 500.
    @app.errorhandler(Exception)
    def handle_exception(e):
        from werkzeug.exceptions import HTTPException
        if isinstance(e, HTTPException):
            return e
        from flask import request, jsonify
        app.logger.error(f'Unhandled exception on {request.method} {request.path}: {e}', exc_info=True)
        if request.is_json or request.path.startswith('/api/') or request.path.startswith('/diagnose/api/'):
            return jsonify({'status': 'error', 'message': 'Internal server error'}), 500
        return 'Internal server error', 500

    # Create database tables
    with app.app_context():
        db.create_all()
        app.logger.debug('Database tables created', extra={'event': 'db_init'})

    # Initialize ping monitor
    from app.utils.ping_monitor import ping_monitor
    ping_monitor.init_app(app)

    # Initialize option chain background service
    from app.utils.background_service import option_chain_service
    option_chain_service.start_service()

    # Initialize order status poller (Phase 2)
    from app.utils.order_status_poller import order_status_poller
    order_status_poller.set_flask_app(app)  # Set app reference to avoid creating new app in thread
    order_status_poller.start()
    app.logger.debug('Order status poller started', extra={'event': 'poller_init'})

    # Recover any pending orders from database (handles app restarts)
    with app.app_context():
        recovered = order_status_poller.recover_pending_orders()
        if recovered > 0:
            app.logger.debug(f'Recovered {recovered} pending orders to polling queue', extra={'event': 'poller_recovery'})

    # Initialize Supertrend exit monitoring service
    from app.utils.supertrend_exit_service import supertrend_exit_service
    supertrend_exit_service.set_flask_app(app)
    supertrend_exit_service.start_service()
    app.logger.debug('Supertrend exit monitoring service started', extra={'event': 'supertrend_exit_init'})

    # Load existing primary and backup accounts within app context
    with app.app_context():
        from app.models import TradingAccount
        primary = TradingAccount.query.filter_by(
            is_primary=True,
            is_active=True
        ).first()
        
        backup_accounts = TradingAccount.query.filter_by(
            is_active=True,
            is_primary=False
        ).order_by(TradingAccount.created_at).all()
        
        if primary:
            app.logger.debug(f'Found primary account: {primary.account_name}')
            if backup_accounts:
                app.logger.debug(f'Found {len(backup_accounts)} backup accounts')

            # Register Flask app with background service
            option_chain_service.set_flask_app(app)

            # Set primary and backup accounts
            option_chain_service.primary_account = primary
            option_chain_service.backup_accounts = backup_accounts.copy()

            # Check if within trading hours and trigger option chains
            if primary.connection_status == 'connected':
                app.logger.debug(f"Testing authentication for primary account: {primary.account_name}")
                try:
                    # Test API connection before starting option chains
                    from app.utils.openalgo_client import ExtendedOpenAlgoAPI
                    test_client = ExtendedOpenAlgoAPI(
                        api_key=primary.get_api_key(),
                        host=primary.host_url
                    )
                    # Quick ping test
                    app.logger.debug(f"Sending ping to {primary.host_url}")
                    ping_response = test_client.ping()
                    app.logger.debug(f"Ping response: {ping_response}")

                    if ping_response.get('status') == 'success':
                        app.logger.debug(f"Authentication successful, starting essential services in background")
                        # Start position monitor and risk manager (NOT option chains)
                        # Option chains load on-demand only when user visits the page
                        import threading
                        def delayed_start(flask_app, primary_acct):
                            import time
                            time.sleep(2)  # Wait for app to fully initialize
                            try:
                                with flask_app.app_context():
                                    option_chain_service.on_primary_account_connected(primary_acct)
                            except Exception as e:
                                flask_app.logger.error(f"Error starting services: {e}")
                        threading.Thread(target=delayed_start, args=(app, primary), daemon=True).start()
                    else:
                        # Authentication failed - update connection status
                        app.logger.warning(f"Primary account {primary.account_name} authentication failed: {ping_response.get('message', 'Unknown error')}")
                        app.logger.warning(f"Marking {primary.account_name} as disconnected")
                        primary.connection_status = 'disconnected'
                        db.session.commit()
                        app.logger.debug(f"Account {primary.account_name} marked as disconnected")
                except Exception as e:
                    app.logger.error(f"Error testing primary account connection: {e}", exc_info=True)
                    app.logger.warning(f"Marking {primary.account_name} as disconnected due to error")
                    primary.connection_status = 'disconnected'
                    db.session.commit()
            else:
                app.logger.debug(f"Primary account {primary.account_name} status is '{primary.connection_status}', not starting services")

    app.logger.debug('Background service initialized (option chains load on-demand)', extra={'event': 'service_init'})
    
    return app