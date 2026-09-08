import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

# Get the base directory (project root)
BASE_DIR = os.path.dirname(os.path.abspath(__file__))

def get_database_uri():
    """Resolve database URI, converting relative SQLite paths to absolute."""
    db_url = os.environ.get('DATABASE_URL') or 'sqlite:///instance/algomirror.db'

    # Handle relative SQLite paths (sqlite:/// with no drive letter)
    if db_url.startswith('sqlite:///') and not db_url.startswith('sqlite:////'):
        # Extract the relative path after sqlite:///
        relative_path = db_url[10:]  # Remove 'sqlite:///'
        # Skip if it's already an absolute path (e.g., D:/ or C:/)
        if not os.path.isabs(relative_path):
            absolute_path = os.path.join(BASE_DIR, relative_path)
            return f'sqlite:///{absolute_path}'

    # Use psycopg v3 driver for PostgreSQL (faster, async-capable)
    if db_url.startswith('postgresql://'):
        db_url = db_url.replace('postgresql://', 'postgresql+psycopg://', 1)

    return db_url

class Config:
    @staticmethod
    def init_app(app):
        """Per-environment validation hook. Overridden where a check is needed."""
        pass

    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = get_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False

    # Database engine options - conditional based on backend
    _db_uri = get_database_uri()
    if _db_uri.startswith('sqlite'):
        SQLALCHEMY_ENGINE_OPTIONS = {
            'connect_args': {
                'timeout': 30  # 30 seconds lock wait for concurrent background services
            },
            'pool_pre_ping': True,
            'pool_recycle': 3600,
        }
    else:
        # PostgreSQL - sized for up to 4-leg parallel execution across 10 accounts.
        # Peak: executor (4 leg threads + 20 account threads + 1 async = 25)
        # + poller (10) + SSE (2) + request handlers (4) + background (2) = ~43
        # pool_size=20 + max_overflow=30 = 50 max, providing headroom.
        # Requires PostgreSQL max_connections >= 100.
        SQLALCHEMY_ENGINE_OPTIONS = {
            'pool_pre_ping': True,
            'pool_recycle': 3600,
            'pool_size': 20,
            'max_overflow': 30,
            'pool_timeout': 30,
        }

    
    # Session configuration
    SESSION_TYPE = os.environ.get('SESSION_TYPE') or 'filesystem'
    SESSION_FILE_DIR = os.environ.get('SESSION_FILE_DIR') or os.path.join(os.path.dirname(os.path.abspath(__file__)), 'flask_session')
    SESSION_FILE_THRESHOLD = 500  # Max number of sessions to store
    PERMANENT_SESSION_LIFETIME = timedelta(days=30)
    SESSION_COOKIE_SECURE = os.environ.get('SESSION_COOKIE_SECURE', 'true').lower() == 'true'
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = 'Lax'

    # For SQLAlchemy sessions (when SESSION_TYPE=sqlalchemy)
    SESSION_SQLALCHEMY_TABLE = 'flask_sessions'
    SESSION_SQLALCHEMY = None  # Will be set to db in app init
    SESSION_SQLALCHEMY_EXTEND_EXISTING = True
    
    # Security
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = None
    WTF_CSRF_SSL_STRICT = True
    
    # CORS settings
    CORS_ORIGINS = os.environ.get('CORS_ORIGINS', 'http://localhost:8000').split(',')
    
    # Content Security Policy Configuration
    CSP_ENABLED = os.environ.get('CSP_ENABLED', 'FALSE').upper() == 'TRUE'
    CSP_REPORT_ONLY = os.environ.get('CSP_REPORT_ONLY', 'FALSE').upper() == 'TRUE'

    # Helper function to parse CSP directives from environment
    @staticmethod
    def parse_csp_directive(env_var, default):
        """Parse CSP directive from environment variable into list format"""
        value = os.environ.get(env_var, default)
        # Split by space and filter out empty strings
        return [item.strip() for item in value.split() if item.strip()]

    # CSP Directives - read from environment with defaults
    CSP = {
        'default-src': parse_csp_directive.__func__('CSP_DEFAULT_SRC', "'self'"),
        'script-src': parse_csp_directive.__func__('CSP_SCRIPT_SRC', "'self' 'unsafe-inline' https://cdn.socket.io https://static.cloudflareinsights.com"),
        'style-src': parse_csp_directive.__func__('CSP_STYLE_SRC', "'self' 'unsafe-inline'"),
        'img-src': parse_csp_directive.__func__('CSP_IMG_SRC', "'self' data:"),
        'connect-src': parse_csp_directive.__func__('CSP_CONNECT_SRC', "'self' wss: ws:"),
        'font-src': parse_csp_directive.__func__('CSP_FONT_SRC', "'self'"),
        'object-src': parse_csp_directive.__func__('CSP_OBJECT_SRC', "'none'"),
        'media-src': parse_csp_directive.__func__('CSP_MEDIA_SRC', "'self' data:"),
        'frame-src': parse_csp_directive.__func__('CSP_FRAME_SRC', "'self'"),
        'form-action': parse_csp_directive.__func__('CSP_FORM_ACTION', "'self'"),
        'frame-ancestors': parse_csp_directive.__func__('CSP_FRAME_ANCESTORS', "'self'"),
        'base-uri': parse_csp_directive.__func__('CSP_BASE_URI', "'self'")
    }

    # Upgrade insecure requests
    CSP_UPGRADE_INSECURE_REQUESTS = os.environ.get('CSP_UPGRADE_INSECURE_REQUESTS', 'FALSE').upper() == 'TRUE'

    # CSP Report URI (optional)
    CSP_REPORT_URI = os.environ.get('CSP_REPORT_URI', '')
    
    # Rate limiting
    RATELIMIT_STORAGE_URL = os.environ.get('REDIS_URL') or 'memory://'
    RATELIMIT_DEFAULT = "1000 per minute"
    RATELIMIT_ENABLED = True
    RATELIMIT_HEADERS_ENABLED = True
    
    # Logging
    LOG_LEVEL = os.environ.get('LOG_LEVEL', 'INFO')
    LOG_FILE = 'logs/algomirror.log'
    
    # OpenAlgo defaults
    DEFAULT_OPENALGO_HOST = 'http://127.0.0.1:5000'
    DEFAULT_OPENALGO_WS = 'ws://127.0.0.1:8765'
    
    # Ping monitoring configuration
    PING_MONITORING_INTERVAL = int(os.environ.get('PING_MONITORING_INTERVAL', 30))
    PING_MONITORING_ENABLED = os.environ.get('PING_MONITORING_ENABLED', 'true').lower() == 'true'
    PING_MAX_FAILURES = int(os.environ.get('PING_MAX_FAILURES', 3))
    PING_QUIET_MODE = os.environ.get('PING_QUIET_MODE', 'false').lower() == 'true'  # Reduces log noise
    
class DevelopmentConfig(Config):
    DEBUG = True
    SESSION_COOKIE_SECURE = False  # Allow cookies over HTTP in development
    WTF_CSRF_SSL_STRICT = False  # Don't require HTTPS for CSRF in development
    # Enable quiet mode by default in development to reduce ping noise
    PING_QUIET_MODE = os.environ.get('PING_QUIET_MODE', 'true').lower() == 'true'
    
class ProductionConfig(Config):
    DEBUG = False

    @staticmethod
    def init_app(app):
        """Refuse to boot production on SQLite.

        Exit claims (EquityHolding), risk checks and the strategy executor all
        guard against double execution with SELECT ... FOR UPDATE row locks.
        SQLite has no such statement and SQLAlchemy silently emits nothing for
        it, so every one of those locks degrades to an unlocked read and two
        concurrent workers can both sell the same holding. Fail loudly at boot
        rather than discover it during market hours.
        """
        uri = app.config.get('SQLALCHEMY_DATABASE_URI') or ''
        if uri.startswith('sqlite'):
            raise RuntimeError(
                'Refusing to start in production on SQLite: row locks '
                '(SELECT ... FOR UPDATE) are silently ignored, which allows '
                'duplicate exit orders. Set DATABASE_URL to a PostgreSQL URI.'
            )


config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}