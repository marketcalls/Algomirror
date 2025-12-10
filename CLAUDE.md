# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project Overview

AlgoMirror is a **proprietary** enterprise-grade multi-account management platform for OpenAlgo developed by OpenFlare Technologies. It enables users to manage multiple OpenAlgo trading accounts from different brokers (24 supported) through a unified interface with enterprise-grade security, real-time analytics, and comprehensive audit logging.

## Key Commands

### Development Setup

#### Method 1: Using UV (Recommended - Faster)
```bash
# Install UV (if not already installed)
# Windows PowerShell
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# macOS/Linux
curl -LsSf https://astral.sh/uv/install.sh | sh

# Create and activate virtual environment
uv venv

# Activate virtual environment (Windows)
.venv\Scripts\activate

# Activate virtual environment (macOS/Linux)
source .venv/bin/activate

# Install dependencies from pyproject.toml (10-100x faster than pip)
uv pip install -e .

# Install with development dependencies
uv pip install -e ".[dev]"

# Install with production dependencies
uv pip install -e ".[production]"

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with appropriate values

# Initialize database
uv run init_db.py

# Run application (port 8000)
uv run wsgi.py
```

#### Method 2: Using pip (Traditional)
```bash
# Create and activate virtual environment (Windows)
python -m venv venv
venv\Scripts\activate

# Create and activate virtual environment (macOS/Linux)
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install -r requirements.txt

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with appropriate values

# Initialize database
python init_db.py

# Run application (port 8000)
python wsgi.py
```

**Note**: UV is 10-100x faster than pip. See `UV_SETUP.md` for detailed UV usage guide.

### Database Management
```bash
# Using UV (recommended)
uv run init_db.py              # Initialize fresh database
uv run init_db.py reset        # Reset database (deletes all data)
uv run init_db.py testdata     # Create test data (development only)

# Or with activated venv
python init_db.py
python init_db.py reset
python init_db.py testdata

# Database migrations
flask db migrate -m "description"
flask db upgrade
```

### CSS Development
```bash
# Build CSS once (production)
npm run build-css

# Watch for CSS changes (development)
npm run watch-css
```

### Testing & Validation
```bash
# Run the application
uv run wsgi.py      # Using UV (recommended)
python wsgi.py      # Or with activated venv
# Runs on http://localhost:8000

# Test OpenAlgo connection
curl -X POST http://127.0.0.1:5000/api/v1/ping \
  -H "Content-Type: application/json" \
  -d '{"apikey":"your_api_key"}'

# Check if application is running
curl http://localhost:8000

# Kill and restart application (Windows)
netstat -ano | findstr :8000
taskkill /PID <PID> /F
uv run wsgi.py

# Kill and restart application (Linux/Mac)
lsof -ti:8000 | xargs kill -9
uv run wsgi.py
```

## Architecture Overview

### Core Security Architecture
The application implements **zero-trust security** with no default accounts. This is a **single-user application**:
- The first registered user automatically becomes the admin (determined at runtime in `app/auth/routes.py:86-98`)
- After the first user registers, **all subsequent registration attempts are blocked** (`app/auth/routes.py:87-98`)
- Only the admin account can log in - no additional user accounts can be created
- This is a critical security feature - NEVER create default admin accounts or allow multi-user registration

### Rate Limiting System
Multi-tier rate limiting is implemented through `app/utils/rate_limiter.py`:
- **Global**: 1000 requests/minute per IP
- **Authentication endpoints**: 10 requests/minute (login, register, password change)
- **API endpoints**: 100 requests/minute (data retrieval)
- **Heavy operations**: 20 requests/minute (data refresh, connection tests)

Rate limiting uses Flask-Limiter with in-memory storage (development) or Redis (production).
**Note**: Uses `fixed-window` strategy - do not use `fixed-window-elastic-expiry` as it's not valid in current Flask-Limiter versions.

### Encryption Architecture
API keys are encrypted using Fernet symmetric encryption (`app/models.py:8-16`):
- Encryption key is auto-generated if not provided via `ENCRYPTION_KEY` environment variable
- All OpenAlgo API keys stored in `api_key_encrypted` field
- Decryption happens only in-memory during API calls
- AES-128 military-grade encryption for all sensitive data

### Blueprint Structure
The application uses Flask blueprints for modular organization:
- `auth`: Authentication (login, register, password management)
- `main`: Dashboard and landing pages
- `accounts`: Trading account CRUD operations with connection testing
- `trading`: Funds, positions, orders, holdings views
- `api`: RESTful API endpoints for data retrieval

Each blueprint registers its own routes and rate limits independently.

### Database Models
Core models in `app/models.py`:
1. **User**: Stores user credentials with `is_admin` flag (first user = True)
2. **TradingAccount**: Encrypted OpenAlgo connection details per user
3. **ActivityLog**: Audit trail for all user actions
4. **Order**: Order history and tracking
5. **Position**: Current positions with P&L calculations
6. **Holding**: Long-term holdings and performance analytics

### Password Policy
Strong password validation matching OpenAlgo standards (`app/auth/forms.py`):
- Minimum 8 characters
- At least one uppercase (A-Z)
- At least one lowercase (a-z)  
- At least one digit (0-9)
- At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)
- Cannot be common passwords (password, 123456, etc.)

### OpenAlgo Integration
The platform integrates with OpenAlgo through:
- Host URL: OpenAlgo REST API endpoint (default: http://127.0.0.1:5000)
- WebSocket URL: Real-time data streaming endpoint (default: ws://127.0.0.1:8765)
- API Key: Encrypted and stored per account
- **Extended OpenAlgo Client**: Custom `ExtendedOpenAlgoAPI` class in `app/utils/openalgo_client.py`
- **Ping API Support**: Built-in connection testing using `/api/v1/ping` endpoint

#### Supported Brokers (24)
5paisa, 5paisa (XTS), Aliceblue, AngelOne, Compositedge (XTS), Definedge, Dhan, Firstock, Flattrade, Fyers, Groww, IIFL (XTS), IndiaBulls, IndMoney, Kotak Securities, Motilal Oswal, Paytm, Pocketful, Shoonya, Tradejini, Upstox, Wisdom Capital (XTS), Zebu, Zerodha

### Theme System
Implements exact OpenAlgo theme using DaisyUI + Tailwind CSS:
- **Two themes only**: Light and Dark (matching OpenAlgo exactly)
- **Theme toggle**: Sun/Moon icon button (no dropdown)
- Theme persistence via localStorage
- Mobile-responsive design with Progressive Web App features
- Located in `app/templates/base.html` and static files
- **CSS compiled locally** from `src/input.css` to `app/static/css/compiled.css`
- No CDN dependencies - all assets served locally for reliability
- Build with `npm run build-css` or watch with `npm run watch-css`
- **Color scheme matches OpenAlgo**:
  - Light: White background (#ffffff), Blue primary (#3b82f6)
  - Dark: Gray background (#1f2937), Same blue primary for consistency

### Logging System
Structured JSON logging (`app/__init__.py`):
- Rotating file handler (10MB max, 10 backups)
- JSON format for production parsing and analytics
- Centralized in `logs/algomirror.log`
- Activity tracking in database via `ActivityLog` model
- Performance monitoring and request duration tracking

## Critical Implementation Notes

1. **Single-User Application**: The system is designed for **ONE user only**. The first registration becomes admin automatically via `User.query.count() == 0` check. After that, registration is permanently blocked. No additional users can register. NEVER implement multi-user functionality.

2. **CSRF Protection**: All forms must include `{{ form.hidden_tag() }}` or use the CSRF token from meta tag.

3. **Rate Limiting Decorators**: Use `@auth_rate_limit()`, `@api_rate_limit()`, or `@heavy_rate_limit()` on routes as appropriate.

4. **API Key Handling**: Never log or display decrypted API keys. Use `set_api_key()` and `get_api_key()` methods only.

5. **Extended OpenAlgo Client**: Always use `ExtendedOpenAlgoAPI` from `app/utils/openalgo_client.py` instead of the base `api` class. This provides the `ping()` method for connection testing.

6. **Connection Testing**: All account additions and edits use the ping API (`/api/v1/ping`) for validation before saving.

7. **Database Migrations**: Use Flask-Migrate for schema changes:
   ```bash
   flask db migrate -m "description"
   flask db upgrade
   ```

8. **Content Security Policy**: Configured in `config.py`. Modify CSP headers carefully to maintain XSS protection.

9. **Trading Page Templates**: All trading pages (funds, orderbook, tradebook, positions, holdings) use safe attribute access with `.get()` method to handle optional fields from OpenAlgo API responses.

10. **Template Hierarchy**: 
    - `base.html`: Core HTML structure, theme handling, scripts
    - `layout.html`: Extends base.html, provides authenticated/non-authenticated layouts
    - All other templates: Extend `layout.html` for consistent UI

11. **Broker Selection**: Account forms use dropdown with all 24 supported brokers. Broker name is auto-detected from ping API response.

12. **Error Handling**: Comprehensive error handling with specific messages for connection issues, invalid API keys, and server problems.

## Environment Configuration

Required environment variables (see `.env.example`):
- `SECRET_KEY`: Flask session key (generate strong random key for production)
- `DATABASE_URL`: SQLite (dev) or PostgreSQL (production) - default: sqlite:///instance/algomirror.db
- `REDIS_URL`: Optional, for production rate limiting
- `SESSION_TYPE`: Session storage type - 'filesystem' (default, single-user) or 'sqlalchemy' (multi-user)
- `ENCRYPTION_KEY`: Optional, auto-generated if not provided
- `FLASK_ENV`: development or production
- `LOG_LEVEL`: DEBUG, INFO, WARNING, ERROR
- `CORS_ORIGINS`: Allowed CORS origins for API access

### Development vs Production Configuration
- **Development**: Uses SQLite, filesystem sessions (default), less strict security
- **Production**: Requires PostgreSQL, configurable sessions (filesystem or database), HTTPS, strict security headers

## Production Deployment

For production deployment:
1. **Database**: Use PostgreSQL instead of SQLite for scalability
2. **Caching & Sessions**: Configure Redis for rate limiting; choose session type (filesystem or database)
3. **WSGI Server**: Use Gunicorn with multiple workers
4. **Reverse Proxy**: Nginx or Apache with SSL/TLS termination
5. **Security**: Enable HTTPS with proper SSL certificates
6. **Environment**: Set `FLASK_ENV=production`
7. **Keys**: Generate strong `SECRET_KEY` and `ENCRYPTION_KEY`
8. **Monitoring**: Set up log monitoring and alerting
9. **Backup**: Configure automated database backups

### Docker Support
The application is Docker-ready with proper containerization support for production deployment.

## OpenAlgo API Integration

### Extended Client Usage
```python
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

# Initialize with ping support
client = ExtendedOpenAlgoAPI(
    api_key='your_api_key',
    host='http://127.0.0.1:5000'
)

# Test connection (AlgoMirror extension)
ping_response = client.ping()

# Standard OpenAlgo operations
funds = client.funds()
positions = client.positionbook()
orders = client.orderbook()
holdings = client.holdings()
```

### Connection Testing Process
1. User enters account details
2. Optional: "Test Connection" button validates credentials
3. Form submission automatically runs ping test
4. Broker name auto-detected from ping response
5. Account saved only if ping succeeds
6. Real-time connection status tracking

## Strategy Builder and Risk Profiles

### Risk Profile System
The strategy builder (`app/templates/strategy/builder.html` and `app/strategy/routes.py`) supports four risk profiles that control lot sizing:

1. **Fixed Lot Size** (default - `risk_profile='fixed_lots'`)
   - Uses explicit lot sizes specified in each strategy leg
   - Bypasses the MarginCalculator entirely
   - Best for strategies with predetermined position sizes
   - Selected in Risk Profile dropdown: "Fixed Lot Size (Default)"

2. **Balanced** (`risk_profile='balanced'`)
   - Uses 65% of available margin for lot size calculation
   - Calculated dynamically using MarginCalculator
   - Moderate risk approach

3. **Conservative** (`risk_profile='conservative'`)
   - Uses 40% of available margin for lot size calculation
   - Lower risk, smaller position sizes

4. **Aggressive** (`risk_profile='aggressive'`)
   - Uses 80% of available margin for lot size calculation
   - Higher risk, larger position sizes

### Strategy Execution Logic
Located in `app/strategy/routes.py:301-319`:
- If `risk_profile == 'fixed_lots'`: Margin calculator is disabled, uses explicit lot sizes
- If `risk_profile` is 'balanced', 'conservative', or 'aggressive': Uses MarginCalculator with corresponding margin percentage
- MarginCalculator (`app/margin/routes.py:287-337`) calculates optimal lot sizes based on:
  - Available margin in account
  - Margin required per lot (from MarginRequirement model)
  - Quality grade (A, B, C) with corresponding margin percentages
  - Freeze quantity limits from TradingSettings

### Important Notes
- The Risk Profile field in Basic Information section controls lot sizing behavior
- When creating a new strategy, "Fixed Lot Size" is selected by default
- Changing risk profile from "Fixed Lot Size" to another option enables dynamic lot sizing
- Strategy model field: `Strategy.risk_profile` (String, 50 chars max) - see `app/models.py:260`

## Security Features

### Authentication & Authorization
- Zero-trust architecture with no default accounts
- First user becomes admin automatically
- Strong password policy enforcement
- Session management with secure cookies (HTTPOnly, Secure, SameSite)
- CSRF protection on all forms

### Data Protection
- Fernet encryption for API keys at rest
- In-memory decryption only during API calls
- Comprehensive audit logging for compliance
- Content Security Policy (CSP) for XSS prevention
- Rate limiting to prevent abuse

### Production Security
- HTTPS enforcement in production
- Strict Transport Security (HSTS) headers
- X-Frame-Options and X-Content-Type-Options headers
- Secure session configuration
- Redis-based session storage

## Performance Optimization

### Database Performance
- Strategic database indexing on frequently queried fields
- Connection pooling for efficient database management
- Lazy loading for relationships
- JSON caching for frequently accessed account data

### Caching Strategy
- Redis caching for frequently accessed data
- Account data cached to reduce OpenAlgo API calls
- Session storage optimization
- Static asset optimization

### Monitoring & Analytics
- Built-in performance monitoring with request duration tracking
- Structured JSON logging for analytics
- Rate limit monitoring and alerting
- Real-time application health checks

## License & Ownership

This is **proprietary software** owned by OpenFlare Technologies. See LICENSE file for restrictions. Unauthorized copying, modification, or distribution is prohibited.

**Copyright © 2024 OpenFlare Technologies. All Rights Reserved.**

## Development Guidelines

### Code Style
- Follow PEP 8 for Python code
- Use type hints where appropriate
- Comprehensive error handling with specific error messages
- Consistent naming conventions across the codebase
- Modular architecture with proper separation of concerns

### Security Guidelines
- Never commit API keys or sensitive data
- Always use the encrypted storage methods for sensitive information
- Validate all user inputs with proper form validation
- Use parameterized queries to prevent SQL injection
- Implement proper CORS policies for API endpoints

### Testing Guidelines
- Test all OpenAlgo integrations with the ping API
- Verify connection testing functionality
- Test rate limiting behavior
- Validate encryption/decryption processes
- Check theme switching and responsive design