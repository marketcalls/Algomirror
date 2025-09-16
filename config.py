import os
from datetime import timedelta
from dotenv import load_dotenv

load_dotenv()

class Config:
    SECRET_KEY = os.environ.get('SECRET_KEY') or 'dev-secret-key-change-in-production'
    SQLALCHEMY_DATABASE_URI = os.environ.get('DATABASE_URL') or 'sqlite:///algomirror.db'
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    
    # Session configuration
    SESSION_TYPE = os.environ.get('SESSION_TYPE') or 'filesystem'
    PERMANENT_SESSION_LIFETIME = timedelta(hours=24)
    SESSION_COOKIE_SECURE = True
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
    
    # Content Security Policy
    CSP = {
        'default-src': ["'self'"],
        'script-src': ["'self'", "'unsafe-inline'", 'cdn.socket.io', 'cdn.jsdelivr.net'],
        'style-src': ["'self'", "'unsafe-inline'", 'cdn.jsdelivr.net'],
        'img-src': ["'self'", 'data:', 'https:'],
        'font-src': ["'self'", 'data:', 'cdn.jsdelivr.net'],
        'connect-src': ["'self'", 'ws:', 'wss:'],
        'frame-ancestors': ["'none'"],
        'form-action': ["'self'"],
        'base-uri': ["'self'"]
    }
    
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
    SESSION_COOKIE_SECURE = False
    WTF_CSRF_SSL_STRICT = False
    # Enable quiet mode by default in development to reduce ping noise
    PING_QUIET_MODE = os.environ.get('PING_QUIET_MODE', 'true').lower() == 'true'
    
class ProductionConfig(Config):
    DEBUG = False
    
config = {
    'development': DevelopmentConfig,
    'production': ProductionConfig,
    'default': DevelopmentConfig
}