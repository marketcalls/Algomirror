# AlgoMirror - Technical Architecture Guide

## System Architecture Overview

AlgoMirror follows a modular, microservice-ready architecture with clear separation of concerns, enabling scalability, maintainability, and high availability.

## Core Architecture Components

### 1. Application Layer Architecture

```
┌─────────────────────────────────────────────────────────┐
│                    Web Interface                        │
│            (HTML/CSS/JavaScript + WebSocket)            │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                 Flask Application                       │
│  ┌─────────────────────────────────────────────────┐   │
│  │              Blueprint Architecture              │   │
│  ├──────────┬──────────┬──────────┬──────────────┤   │
│  │   Auth   │   Main   │ Trading  │   Accounts   │   │
│  │  Routes  │  Routes  │  Routes  │    Routes    │   │
│  └──────────┴──────────┴──────────┴──────────────┘   │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                   Service Layer                         │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐   │
│  │  WebSocket   │ │ Option Chain │ │   OpenAlgo   │   │
│  │   Manager    │ │   Manager    │ │    Client    │   │
│  └──────────────┘ └──────────────┘ └──────────────┘   │
└─────────────────────┬───────────────────────────────────┘
                      │
┌─────────────────────▼───────────────────────────────────┐
│                  Data Access Layer                      │
│  ┌──────────────┐ ┌──────────────┐ ┌──────────────┐   │
│  │  SQLAlchemy  │ │    Redis     │ │  Encryption  │   │
│  │     ORM      │ │    Cache     │ │   Service    │   │
│  └──────────────┘ └──────────────┘ └──────────────┘   │
└──────────────────────────────────────────────────────────┘
```

### 2. WebSocket Architecture with Failover

```
┌─────────────────────────────────────────────────────────┐
│              Professional WebSocket Manager              │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  ┌─────────────────┐                                  │
│  │ Connection Pool │                                   │
│  ├─────────────────┤                                  │
│  │ Primary Account ├──► WebSocket Primary             │
│  │                 ├──► WebSocket Backup              │
│  ├─────────────────┤                                  │
│  │ Backup Account 1├──► (Standby)                     │
│  ├─────────────────┤                                  │
│  │ Backup Account 2├──► (Standby)                     │
│  └─────────────────┘                                  │
│                                                        │
│  ┌──────────────────────────────────────┐            │
│  │         Failover Controller          │            │
│  ├──────────────────────────────────────┤            │
│  │ • Connection Health Monitor          │            │
│  │ • Exponential Backoff Strategy       │            │
│  │ • Account Priority Queue             │            │
│  │ • Subscription State Manager         │            │
│  └──────────────────────────────────────┘            │
│                                                       │
│  ┌──────────────────────────────────────┐           │
│  │        Data Processing Pipeline       │           │
│  ├──────────────────────────────────────┤           │
│  │ Quote Handler → Option Chain Updates  │           │
│  │ Depth Handler → Market Depth Updates  │           │
│  │ LTP Handler   → Price Updates         │           │
│  └──────────────────────────────────────┘           │
└───────────────────────────────────────────────────────┘
```

### 3. Database Schema Architecture

```sql
-- Core Tables
Users
├── id (PK)
├── username (unique)
├── email (unique)
├── password_hash
├── is_admin (boolean)
└── created_at

TradingAccounts
├── id (PK)
├── user_id (FK → Users)
├── account_name
├── broker
├── api_key_encrypted
├── host_url
├── websocket_url
├── is_primary (boolean)
└── is_active (boolean)

-- Trading Data Tables
Orders
├── id (PK)
├── account_id (FK → TradingAccounts)
├── order_id
├── symbol
├── quantity
├── price
├── status
└── timestamp

Positions
├── id (PK)
├── account_id (FK → TradingAccounts)
├── symbol
├── quantity
├── avg_price
├── current_price
├── pnl
└── updated_at

-- Audit & Monitoring
ActivityLog
├── id (PK)
├── user_id (FK → Users)
├── action
├── details
├── ip_address
└── timestamp

-- Option Chain Data (In-Memory)
OptionChainCache
├── underlying (NIFTY/BANKNIFTY)
├── strikes (JSON)
├── expiry
├── last_update
└── market_depth (JSON)
```

### 4. Security Architecture

```
┌──────────────────────────────────────────┐
│         Security Layer Stack             │
├──────────────────────────────────────────┤
│                                          │
│  Application Security                    │
│  ├── Zero-Trust Architecture            │
│  ├── No Default Accounts                │
│  └── First User = Admin                 │
│                                          │
│  Data Security                          │
│  ├── Fernet Encryption (AES-128)        │
│  ├── In-Memory Decryption Only          │
│  └── Secure Key Management              │
│                                          │
│  Network Security                       │
│  ├── HTTPS Enforcement                  │
│  ├── CSRF Protection                    │
│  ├── Content Security Policy            │
│  └── XSS Prevention                     │
│                                          │
│  Access Control                         │
│  ├── Session-Based Auth                 │
│  ├── Rate Limiting (Multi-Tier)         │
│  ├── API Key Validation                 │
│  └── Audit Logging                      │
└──────────────────────────────────────────┘
```

## Detailed Component Architecture

### 1. Flask Application Factory Pattern

```python
# app/__init__.py structure
def create_app(config_name='development'):
    app = Flask(__name__)
    
    # Configuration
    app.config.from_object(config[config_name])
    
    # Extensions
    db.init_app(app)
    login_manager.init_app(app)
    limiter.init_app(app)
    
    # Blueprints
    from app.auth import auth_bp
    from app.main import main_bp
    from app.trading import trading_bp
    from app.accounts import accounts_bp
    from app.api import api_bp
    
    app.register_blueprint(auth_bp, url_prefix='/auth')
    app.register_blueprint(main_bp, url_prefix='/')
    app.register_blueprint(trading_bp, url_prefix='/trading')
    app.register_blueprint(accounts_bp, url_prefix='/accounts')
    app.register_blueprint(api_bp, url_prefix='/api')
    
    return app
```

### 2. WebSocket Manager Architecture

```python
class ProfessionalWebSocketManager:
    """
    Core Components:
    1. Connection Pool Management
    2. Failover Controller
    3. Data Processor
    4. Subscription Manager
    5. Health Monitor
    """
    
    def __init__(self):
        # Connection Management
        self.connection_pool = {}
        self.max_connections = 10
        self.heartbeat_interval = 30
        
        # Failover Strategy
        self.reconnect_attempts = 3
        self.backoff_strategy = ExponentialBackoff()
        self.account_failover_enabled = True
        
        # Data Processing
        self.data_processor = WebSocketDataProcessor()
        
        # Subscription State
        self.subscriptions = set()
        self.subscription_queue = deque()
        
        # Threading
        self.ws_thread = None
        self.reconnect_thread = None
        self._lock = threading.Lock()
```

### 3. Option Chain Manager Architecture

```python
class OptionChainManager:
    """
    Manages real-time option chain data with:
    1. Strike calculation and tagging
    2. WebSocket subscription management
    3. Market depth processing
    4. Background monitoring
    """
    
    def __init__(self, underlying, expiry):
        self.underlying = underlying  # NIFTY/BANKNIFTY
        self.expiry = expiry
        self.strike_step = 50 if underlying == 'NIFTY' else 100
        
        # Data structures
        self.option_data = {}  # Strike-wise data
        self.subscription_map = {}  # Symbol to strike mapping
        self.depth_cache = TTLCache(maxsize=100, ttl=30)
        
        # WebSocket integration
        self.websocket_manager = None
        self.monitoring_active = False
```

### 4. Rate Limiting Architecture

```python
# Multi-tier rate limiting strategy
rate_limits = {
    'global': '1000/minute',
    'auth': '10/minute',
    'api': '100/minute',
    'heavy': '20/minute'
}

# Decorator implementation
@auth_rate_limit()  # 10 req/min for auth endpoints
@api_rate_limit()   # 100 req/min for API endpoints
@heavy_rate_limit() # 20 req/min for heavy operations
```

## Data Flow Architecture

### 1. Real-Time Data Flow

```
User Request → Flask Route → Service Layer → OpenAlgo API
                    ↓
              WebSocket Stream
                    ↓
            Data Processor → Option Chain Manager
                    ↓
            Client WebSocket → Real-time UI Updates
```

### 2. Failover Data Flow

```
Primary WebSocket Failure
        ↓
Exponential Backoff Retry (3 attempts)
        ↓
Account Failover Decision
        ↓
Switch to Backup Account
        ↓
Resubscribe All Symbols
        ↓
Resume Data Flow
```

### 3. Option Chain Data Flow

```
Account Connection
        ↓
Auto-start Option Chains (NIFTY & BANKNIFTY)
        ↓
Calculate ATM & Strike Range
        ↓
Subscribe via WebSocket (Depth Mode)
        ↓
Process Market Depth Updates
        ↓
Update In-Memory Cache
        ↓
Broadcast to Connected Clients
```

## Performance Architecture

### 1. Caching Strategy

```python
# Three-tier caching
cache_layers = {
    'L1': 'In-Memory (TTLCache)',     # 30 second TTL
    'L2': 'Redis Cache',               # 5 minute TTL
    'L3': 'Database',                  # Persistent storage
}

# Cache implementation
from cachetools import TTLCache

class CacheManager:
    def __init__(self):
        self.memory_cache = TTLCache(maxsize=1000, ttl=30)
        self.redis_client = redis.Redis()
    
    def get(self, key):
        # Try L1 (memory)
        if key in self.memory_cache:
            return self.memory_cache[key]
        
        # Try L2 (Redis)
        value = self.redis_client.get(key)
        if value:
            self.memory_cache[key] = value
            return value
        
        # Fall back to L3 (database)
        return None
```

### 2. Connection Pool Management

```python
# Database connection pooling
SQLALCHEMY_ENGINE_OPTIONS = {
    'pool_size': 10,
    'pool_recycle': 3600,
    'pool_pre_ping': True,
    'max_overflow': 20
}

# WebSocket connection pooling
websocket_pool = {
    'max_connections': 10,
    'connection_timeout': 5,
    'idle_timeout': 300,
    'keepalive_interval': 30
}
```

### 3. Resource Optimization

```python
class ResourceOptimizer:
    def __init__(self):
        self.memory_threshold = 500  # MB
        self.cpu_threshold = 70      # %
        self.subscription_limit = 200
    
    def optimize(self):
        if self.get_memory_usage() > self.memory_threshold:
            self.reduce_subscriptions()
        
        if self.get_cpu_usage() > self.cpu_threshold:
            self.throttle_updates()
```

## Scalability Architecture

### 1. Horizontal Scaling

```
Load Balancer (Nginx)
        ↓
┌────────┬────────┬────────┐
│ App    │ App    │ App    │
│ Server │ Server │ Server │
│ 1      │ 2      │ 3      │
└────────┴────────┴────────┘
        ↓
   Shared Redis
        ↓
   PostgreSQL
```

### 2. Microservice Architecture (Future)

```
API Gateway
     ↓
┌─────────────┬─────────────┬─────────────┐
│   Auth      │   Trading   │   Market    │
│   Service   │   Service   │   Data      │
│             │             │   Service   │
└─────────────┴─────────────┴─────────────┘
                    ↓
            Message Queue (RabbitMQ/Kafka)
                    ↓
              Event Processing
```

## Deployment Architecture

### 1. Development Environment

```yaml
Services:
  - Flask Development Server (port 8000)
  - SQLite Database (file-based)
  - In-memory rate limiting
  - Local WebSocket connections
  - Debug logging enabled
```

### 2. Production Environment

```yaml
Services:
  - Gunicorn WSGI Server (4 workers)
  - PostgreSQL Database (connection pooling)
  - Redis (caching) + Database sessions
  - Alternative: Filesystem sessions (single-user)
  - Nginx Reverse Proxy (SSL termination)
  - WebSocket Load Balancing
  - Structured JSON logging
  - Monitoring & Alerting
```

### 3. Docker Architecture

```dockerfile
# Multi-stage build
FROM python:3.9-slim AS builder
# Build dependencies

FROM python:3.9-slim
# Runtime environment
# Copy built assets
# Run Gunicorn
```

## Monitoring Architecture

### 1. Application Monitoring

```python
metrics = {
    'request_duration': histogram,
    'active_connections': gauge,
    'failed_requests': counter,
    'websocket_health': gauge,
    'database_pool_size': gauge
}
```

### 2. Infrastructure Monitoring

```yaml
Monitoring Stack:
  - Prometheus (metrics collection)
  - Grafana (visualization)
  - AlertManager (alerting)
  - ELK Stack (log aggregation)
```

### 3. Business Metrics

```python
business_metrics = {
    'active_users': daily_count,
    'total_orders': hourly_count,
    'failover_events': event_log,
    'api_usage': rate_tracker,
    'account_connections': status_monitor
}
```

## Security Architecture Details

### 1. Encryption Implementation

```python
from cryptography.fernet import Fernet

class EncryptionService:
    def __init__(self):
        self.cipher = Fernet(self.get_or_create_key())
    
    def encrypt(self, data: str) -> bytes:
        return self.cipher.encrypt(data.encode())
    
    def decrypt(self, encrypted_data: bytes) -> str:
        return self.cipher.decrypt(encrypted_data).decode()
```

### 2. Authentication Flow

```
Login Request
     ↓
Validate Credentials
     ↓
Create Session
     ↓
Set Secure Cookie (HTTPOnly, Secure, SameSite)
     ↓
Return CSRF Token
```

### 3. API Security

```python
# API key validation
def validate_api_key(api_key):
    # Decrypt stored key
    # Verify with OpenAlgo
    # Cache validation result
    # Return success/failure
```

## Best Practices & Patterns

### 1. Design Patterns Used
- **Factory Pattern**: Flask app creation
- **Singleton Pattern**: WebSocket manager
- **Observer Pattern**: Event handlers
- **Strategy Pattern**: Failover strategies
- **Decorator Pattern**: Rate limiting

### 2. SOLID Principles
- **Single Responsibility**: Each module has one purpose
- **Open/Closed**: Extensible via blueprints
- **Liskov Substitution**: Broker interfaces
- **Interface Segregation**: Minimal interfaces
- **Dependency Inversion**: Service abstractions

### 3. Code Organization
- Clear separation of concerns
- Modular blueprint architecture
- Reusable utility functions
- Comprehensive error handling
- Consistent naming conventions

## Testing Architecture

### 1. Unit Testing
```python
tests/
├── test_models.py
├── test_websocket.py
├── test_option_chain.py
├── test_encryption.py
└── test_failover.py
```

### 2. Integration Testing
```python
# Test WebSocket failover
# Test account switching
# Test data persistence
# Test rate limiting
```

### 3. Load Testing
```bash
# Using locust for load testing
locust -f tests/load_test.py --host=http://localhost:8000
```

## Maintenance & Operations

### 1. Database Migrations
```bash
# Alembic migration workflow
flask db init
flask db migrate -m "Add new column"
flask db upgrade
flask db downgrade
```

### 2. Backup Strategy
```yaml
Backup Schedule:
  - Database: Daily automated backups
  - Configuration: Version controlled
  - Logs: Rotated and archived
  - User data: Encrypted backups
```

### 3. Disaster Recovery
```yaml
Recovery Plan:
  - RPO: 1 hour (Recovery Point Objective)
  - RTO: 15 minutes (Recovery Time Objective)
  - Failover: Automatic with manual override
  - Data restoration: From latest backup
```

This architecture ensures AlgoMirror remains scalable, maintainable, and reliable while providing enterprise-grade features for multi-account trading management.