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
from flask_wtf.csrf import CSRFProtect
from flask_cors import CORS
from flask_talisman import Talisman
from flask_session import Session
from pythonjsonlogger import jsonlogger
from config import config

db = SQLAlchemy()
login_manager = LoginManager()
migrate = Migrate()
csrf = CSRFProtect()
sess = Session()
limiter = None

def setup_logging(app):
    """Set up centralized logging with JSON format"""
    if not os.path.exists('logs'):
        os.mkdir('logs')
    
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
    
    # Set log level from config
    log_level = getattr(logging, app.config['LOG_LEVEL'].upper(), logging.INFO)
    logHandler.setLevel(log_level)
    
    # Add handler to app logger
    app.logger.addHandler(logHandler)
    app.logger.setLevel(log_level)
    app.logger.info('AlgoMirror startup', extra={'event': 'startup'})

    # Suppress noisy loggers
    logging.getLogger('app.utils.websocket_manager').setLevel(logging.CRITICAL)
    logging.getLogger('app.utils.background_service').setLevel(logging.CRITICAL)
    logging.getLogger('app.utils.option_chain').setLevel(logging.CRITICAL)
    logging.getLogger('app.trading.routes').setLevel(logging.CRITICAL)
    
    # Also log to console in development
    if app.debug:
        console_handler = logging.StreamHandler()
        console_handler.setFormatter(formatter)
        app.logger.addHandler(console_handler)

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
    
    # Initialize rate limiter
    from app.utils.rate_limiter import init_rate_limiter
    limiter = init_rate_limiter(app)
    
    # Setup CORS with specific origins
    CORS(app, 
         origins=app.config['CORS_ORIGINS'],
         supports_credentials=True,
         methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
         allow_headers=['Content-Type', 'X-CSRFToken'])
    
    # Setup CSP with Talisman (disabled in development for hot reload)
    if not app.debug:
        csp = {
            'default-src': app.config['CSP']['default-src'],
            'script-src': app.config['CSP']['script-src'],
            'style-src': app.config['CSP']['style-src'],
            'img-src': app.config['CSP']['img-src'],
            'font-src': app.config['CSP']['font-src'],
            'connect-src': app.config['CSP']['connect-src'],
            'frame-ancestors': app.config['CSP']['frame-ancestors'],
            'form-action': app.config['CSP']['form-action'],
            'base-uri': app.config['CSP']['base-uri']
        }
        Talisman(app, 
                force_https=True,
                strict_transport_security=True,
                content_security_policy=csp,
                content_security_policy_nonce_in=['script-src', 'style-src'])
    
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
    from app.api import api_bp

    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(main_bp)
    app.register_blueprint(accounts_bp, url_prefix='/accounts')
    app.register_blueprint(trading_bp, url_prefix='/trading')
    app.register_blueprint(settings_bp)  # Already has url_prefix in blueprint definition
    app.register_blueprint(strategy_bp)  # url_prefix defined in blueprint
    app.register_blueprint(api_bp, url_prefix='/api')
    
    # Create database tables
    with app.app_context():
        db.create_all()
        app.logger.info('Database tables created', extra={'event': 'db_init'})
    
    # Initialize ping monitor
    from app.utils.ping_monitor import ping_monitor
    ping_monitor.init_app(app)
    
    # Initialize option chain background service
    from app.utils.background_service import option_chain_service
    option_chain_service.start_service()
    
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
            app.logger.info(f'Found primary account: {primary.account_name}')
            if backup_accounts:
                app.logger.info(f'Found {len(backup_accounts)} backup accounts')
            
            # Set primary and backup accounts
            option_chain_service.primary_account = primary
            option_chain_service.backup_accounts = backup_accounts.copy()
            
            # Check if within trading hours and trigger option chains
            if primary.connection_status == 'connected':
                option_chain_service.on_primary_account_connected(primary)
        
    app.logger.info('Option chain background service started', extra={'event': 'service_init'})
    
    return app