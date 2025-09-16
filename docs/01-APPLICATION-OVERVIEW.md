# AlgoMirror - Complete Application Overview

## Executive Summary

AlgoMirror is an enterprise-grade multi-account management platform for OpenAlgo that enables traders to manage multiple trading accounts from 22+ different brokers through a unified interface. The platform provides real-time WebSocket integration, automatic failover mechanisms, comprehensive option chain monitoring, and enterprise security features.

## Core Capabilities

### 1. Multi-Account Management
- **Unified Dashboard**: Manage unlimited trading accounts from 22+ brokers
- **Account Hierarchy**: Primary/Secondary account designation with automatic failover
- **Cross-Broker Support**: Seamless switching between different broker APIs
- **Real-time Synchronization**: Live updates across all connected accounts

### 2. Advanced Trading Features
- **Option Chain Monitoring**: Real-time NIFTY & BANKNIFTY option chains with market depth
- **WebSocket Streaming**: Professional-grade real-time data with automatic reconnection
- **Order Management**: Place, modify, and cancel orders across multiple accounts
- **Position Tracking**: Real-time P&L calculation and position monitoring
- **Holdings Analysis**: Long-term portfolio tracking with performance metrics

### 3. Enterprise Security
- **Zero-Trust Architecture**: No default accounts, first user becomes admin
- **Military-Grade Encryption**: AES-128 encryption for all API keys
- **Multi-Tier Rate Limiting**: Protection against abuse and DoS attacks
- **Comprehensive Audit Logging**: Complete activity trail for compliance

### 4. High Availability Features
- **Automatic Failover**: Multi-level failover (WebSocket → Account → Broker)
- **Connection Pooling**: Efficient resource management with warm standby connections
- **Health Monitoring**: Continuous connection health checks with auto-recovery
- **Trading Hours Management**: Automated connection scheduling based on market hours

## Technology Stack

### Backend
- **Framework**: Flask 2.3+ with Blueprint architecture
- **Database**: SQLAlchemy ORM with SQLite (dev) / PostgreSQL (production)
- **WebSocket**: Native WebSocket client with enterprise features
- **Encryption**: Cryptography library with Fernet symmetric encryption
- **Authentication**: Flask-Login with secure session management

### Frontend
- **CSS Framework**: Tailwind CSS + DaisyUI (OpenAlgo theme)
- **JavaScript**: Vanilla JS for WebSocket client and real-time updates
- **Build System**: NPM with PostCSS for Tailwind compilation
- **Theme System**: Light/Dark mode matching OpenAlgo design

### Infrastructure
- **Rate Limiting**: Flask-Limiter with Redis backend (production)
- **Session Storage**: Configurable - filesystem (single-user) or database sessions (multi-user)
- **Background Tasks**: Threading for WebSocket and monitoring services
- **Logging**: Structured JSON logging with rotation

## Project Structure

```
Algomirror/
├── app/                      # Main application package
│   ├── __init__.py          # Flask app factory and configuration
│   ├── models.py            # SQLAlchemy database models
│   ├── auth/                # Authentication blueprint
│   │   ├── routes.py        # Login, register, password management
│   │   └── forms.py         # WTForms with validation
│   ├── main/                # Main blueprint
│   │   └── routes.py        # Dashboard and landing pages
│   ├── accounts/            # Account management blueprint
│   │   └── routes.py        # CRUD operations for trading accounts
│   ├── trading/             # Trading operations blueprint
│   │   └── routes.py        # Orders, positions, holdings views
│   ├── api/                 # RESTful API blueprint
│   │   └── routes.py        # JSON endpoints for data retrieval
│   ├── utils/               # Utility modules
│   │   ├── openalgo_client.py     # Extended OpenAlgo API client
│   │   ├── websocket_manager.py   # Professional WebSocket manager
│   │   ├── option_chain.py        # Option chain management
│   │   ├── rate_limiter.py        # Rate limiting decorators
│   │   └── lot_sizing_engine.py   # Position sizing calculations
│   ├── strategies/          # Trading strategy modules
│   └── templates/           # Jinja2 HTML templates
│       ├── base.html        # Core layout with theme system
│       ├── layout.html      # Extended layout for pages
│       └── trading/         # Trading-specific templates
├── migrations/              # Database migration files
├── docs/                    # Documentation
├── logs/                    # Application logs
├── instance/                # Instance-specific files
├── src/                     # Source CSS for Tailwind
├── requirements.txt         # Python dependencies
├── package.json            # Node dependencies
├── tailwind.config.js      # Tailwind configuration
├── config.py               # Application configuration
├── app.py                  # Application entry point
├── init_db.py              # Database initialization
└── .env.example            # Environment variables template
```

## Key Architectural Decisions

### 1. No Default Accounts
The system intentionally has NO default admin accounts. The first registered user automatically becomes admin through runtime detection. This zero-trust approach eliminates common security vulnerabilities.

### 2. Blueprint Architecture
Flask blueprints provide modular organization with independent routing, making the codebase maintainable and allowing teams to work on different features independently.

### 3. Encrypted Storage
All sensitive data (API keys) are encrypted at rest using Fernet symmetric encryption. Keys are only decrypted in-memory during API calls.

### 4. WebSocket Failover Strategy
Three-tier failover ensures continuous operation:
- Level 1: WebSocket reconnection (same account)
- Level 2: Account failover (different account)
- Level 3: Broker failover (different broker)

### 5. Local Asset Serving
All CSS and JavaScript assets are compiled and served locally, eliminating CDN dependencies and ensuring reliability in restricted network environments.

## Development Workflow

### 1. Initial Setup
```bash
# Clone repository
git clone <repository-url>
cd Algomirror

# Create virtual environment
python -m venv venv
venv\Scripts\activate  # Windows
source venv/bin/activate  # Linux/Mac

# Install dependencies
pip install -r requirements.txt
npm install

# Build CSS
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Initialize database
python init_db.py

# Run application
python app.py
```

### 2. Development Mode
```bash
# Watch CSS changes
npm run watch-css

# Run with debug mode
python app.py  # Runs on http://localhost:8000
```

### 3. Database Management
```bash
# Create migration
flask db migrate -m "Description"

# Apply migration
flask db upgrade

# Reset database
python init_db.py reset

# Create test data
python init_db.py testdata
```

## Integration Points

### 1. OpenAlgo API
- REST API: Default http://127.0.0.1:5000
- WebSocket: Default ws://127.0.0.1:8765
- Authentication: API key-based
- Ping endpoint for connection testing

### 2. Broker Support
Supports 22+ brokers including:
- Zerodha, Angel One, Upstox, Groww
- Dhan, Fyers, 5paisa, Aliceblue
- IIFL XTS, Kotak Securities, Paytm
- And more...

### 3. Market Data
- Real-time option chain for NIFTY & BANKNIFTY
- Market depth with bid/ask spreads
- Live position and P&L updates
- Historical data for analytics

## Performance Considerations

### 1. Optimization Strategies
- Connection pooling for database efficiency
- In-memory caching for frequently accessed data
- Lazy loading for relationships
- Batch processing for WebSocket subscriptions

### 2. Scalability Features
- Horizontal scaling with database sessions (when configured)
- Load balancing ready architecture
- Microservice-compatible design
- Asynchronous processing for heavy operations

### 3. Resource Management
- Automatic subscription optimization
- Memory threshold monitoring
- CPU usage tracking
- Rate limiting for API protection

## Security Implementation

### 1. Authentication
- Secure password hashing with Werkzeug
- Session-based authentication
- CSRF protection on all forms
- Strong password policy enforcement

### 2. Authorization
- Role-based access (Admin/User)
- Account-level isolation
- API key encryption
- Audit logging for compliance

### 3. Network Security
- HTTPS enforcement (production)
- Content Security Policy
- XSS and injection protection
- Rate limiting and DDoS protection

## Monitoring & Maintenance

### 1. Logging System
- Structured JSON logging
- Rotating file handlers
- Multiple log levels
- Performance metrics tracking

### 2. Health Checks
- WebSocket connection monitoring
- Account availability checks
- Database connection pooling
- API endpoint monitoring

### 3. Alerting
- Connection failure notifications
- Rate limit breach alerts
- Security event logging
- Performance degradation warnings

## Production Deployment

### 1. Requirements
- PostgreSQL database
- Redis for caching (optional for database sessions)
- HTTPS with SSL certificates
- Reverse proxy (Nginx/Apache)
- WSGI server (Gunicorn)

### 2. Environment Variables
```bash
FLASK_ENV=production
SECRET_KEY=<strong-random-key>
DATABASE_URL=postgresql://...
REDIS_URL=redis://...
ENCRYPTION_KEY=<base64-encoded-key>
```

### 3. Deployment Steps
1. Set up PostgreSQL and Redis
2. Configure environment variables
3. Run database migrations
4. Set up Nginx reverse proxy
5. Configure Gunicorn with workers
6. Set up SSL certificates
7. Configure monitoring and backups

## Support & Maintenance

### Regular Tasks
- Monitor WebSocket connections
- Review audit logs
- Update broker configurations
- Database backups
- Security updates

### Troubleshooting
- Check logs in `logs/algomirror.log`
- Verify WebSocket connectivity
- Test account connections with ping
- Monitor rate limiting metrics
- Review failover history

## Future Enhancements

### Planned Features
- Advanced strategy builder
- Backtesting framework
- Risk management dashboard
- Multi-user collaboration
- Mobile application
- REST API v2

### Performance Improvements
- GraphQL API layer
- WebSocket connection pooling
- Distributed caching
- Message queue integration
- Kubernetes deployment

## License

Copyright © 2024 OpenFlare Technologies. All Rights Reserved.
This is proprietary software. Unauthorized copying, modification, or distribution is prohibited.