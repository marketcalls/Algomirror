# AlgoMirror - Multi-Account Management Platform for OpenAlgo

[![Version](https://img.shields.io/badge/version-1.0.0-blue.svg)](https://github.com/marketcalls/algomirror)
[![Python](https://img.shields.io/badge/python-3.12+-green.svg)](https://www.python.org/)
[![Flask](https://img.shields.io/badge/flask-3.1+-lightgrey.svg)](https://flask.palletsprojects.com/)
[![OpenAlgo](https://img.shields.io/badge/openalgo-compatible-orange.svg)](https://openalgo.in)
[![License](https://img.shields.io/badge/license-AGPL%20v3-red.svg)](LICENSE)

> **Enterprise-grade multi-account management platform for OpenAlgo with strategy building, real-time risk management, and comprehensive analytics**

AlgoMirror is a secure and scalable multi-account management platform built on top of OpenAlgo. It provides traders with a unified interface to manage multiple OpenAlgo trading accounts across 24+ brokers, featuring advanced strategy building, real-time position monitoring, AFL-style trailing stop loss, Supertrend-based exits, dynamic margin calculation, and comprehensive risk management with full audit logging.

---

## Table of Contents

- [Key Features](#key-features)
- [What's New](#whats-new)
- [Prerequisites](#prerequisites)
- [Quick Start Guide](#quick-start-guide)
- [Strategy Builder](#strategy-builder)
- [Risk Management](#risk-management)
- [Real-Time Position Monitoring](#real-time-position-monitoring)
- [Margin Calculator](#margin-calculator)
- [Supertrend Indicator](#supertrend-indicator)
- [OpenAlgo Integration](#openalgo-integration)
- [Project Architecture](#project-architecture)
- [Configuration Reference](#configuration-reference)
- [Production Deployment](#production-deployment)
- [Troubleshooting](#troubleshooting)
- [Tech Stack](#tech-stack)
- [Credits & Acknowledgments](#credits--acknowledgments)
- [Disclaimer](#disclaimer)
- [License](#license)

---

## Key Features

### Multi-Account Management
- Unified dashboard for unlimited trading accounts across 24+ brokers
- Primary/secondary account hierarchy with automatic failover
- Real-time synchronization and live updates across all accounts
- Cross-broker support with seamless switching
- Single shared WebSocket connection to prevent broker rate limits

### Strategy Builder
- Visual strategy construction with multi-leg support
- Instrument support: NIFTY, BANKNIFTY, SENSEX options and futures
- Strike selection: ATM, ITM, OTM with configurable offsets, or premium-based
- Risk profiles: Fixed lots, Conservative (40%), Balanced (65%), Aggressive (80%)
- Entry/exit timing with automatic square-off
- Parallel execution across multiple accounts using ThreadPoolExecutor

### Risk Management
- Max loss and max profit targets with automatic exits
- **AFL-style trailing stop loss** with peak P&L tracking and ratcheting mechanism
- Supertrend-based exits (breakout/breakdown signals)
- **Persistent exit reason tracking** for compliance
- **Risk event audit logging** with full compliance trail
- Position-level and strategy-level P&L tracking
- Real-time P&L monitoring via WebSocket

### Real-Time Position Monitoring
- **WebSocket-based position tracking** for open positions only (5-50 symbols vs 1000+)
- Primary account connection requirement for monitoring
- Trading hours awareness from database (no hardcoding)
- Automatic subscription management for active positions
- Integration with Risk Manager for threshold monitoring

### Dynamic Margin Calculator
- Automatic lot sizing based on available margin
- Trade quality grades: A (95%), B (65%), C (36%) margin utilization
- **Option buying premium configuration** for premium-based lot sizing
- Expiry vs non-expiry margin awareness
- Freeze quantity handling with automatic order splitting
- **Next month lot size support** for contract transitions

### Technical Analysis
- Pine Script v6 compatible Supertrend indicator
- Numba-optimized calculations for performance
- Configurable period, multiplier, and timeframe
- Real-time exit signal monitoring with reason capture
- Background exit service with daemon thread

### Enterprise Security
- Zero-trust architecture with no default accounts
- AES-128 Fernet encryption for all API keys
- Multi-tier rate limiting protection
- Comprehensive audit logging
- CSRF protection and Content Security Policy
- Single-user security model (first user = admin, no multi-user)

---

## What's New

### Real-Time Position Monitoring
- **WebSocket-based tracking** subscribes ONLY to symbols with open positions
- Intelligent subscription management (5-50 symbols vs 1000+ option chain symbols)
- Primary account connection check before starting monitoring
- Trading hours from TradingHoursTemplate database model (no hardcoding)
- Integration with Risk Manager for automated threshold enforcement

### AFL-Style Trailing Stop Loss
- **Peak P&L tracking** that only moves up (ratchets), never down
- Formula: `stop_level = initial_stop + (peak_pnl - initial_pnl) * trail_factor`
- Supports percentage, points, or amount-based trailing
- Persistent state tracking across position updates:
  - `trailing_sl_active`: Is TSL currently tracking
  - `trailing_sl_peak_pnl`: Highest P&L reached
  - `trailing_sl_initial_stop`: First stop level when TSL activated
  - `trailing_sl_trigger_pnl`: Current trailing stop (ratchets up)
  - `trailing_sl_triggered_at`: When TSL was triggered
  - `trailing_sl_exit_reason`: Detailed exit reason for audit

### Exit Reason Tracking
- **Persistent exit reasons** stored for all risk events:
  - Max Loss: `max_loss_exit_reason`, `max_loss_triggered_at`
  - Max Profit: `max_profit_exit_reason`, `max_profit_triggered_at`
  - Trailing SL: `trailing_sl_exit_reason`, `trailing_sl_triggered_at`
  - Supertrend: `supertrend_exit_reason`, `supertrend_exit_triggered_at`
- Full compliance trail in RiskEvent audit log

### Risk Event Audit Logging
- **RiskEvent model** tracks all threshold breaches:
  - Event type (max_loss, max_profit, trailing_sl, supertrend)
  - Threshold and current values at trigger
  - Action taken (close_all, close_partial, alert_only)
  - Exit order IDs for verification
  - Timestamp and notes
- Cascade delete when strategy/execution is removed

### Option Buying Premium Configuration
- **Premium per lot** settings for option buyers
- Separate from margin-based calculations for sellers
- Per-instrument configuration (NIFTY, BANKNIFTY, SENSEX)
- Enables accurate lot sizing for cash-based option buying

### WebSocket Session Management
- **On-demand option chain loading** with session tracking
- Heartbeat mechanism for session keep-alive
- Auto-expiry of inactive sessions (5 minutes)
- Reduces unnecessary WebSocket subscriptions

### Special Trading Sessions
- **SpecialTradingSession model** for Muhurat trading and similar events
- Date-specific session overrides
- Configurable start/end times per market
- Integrates with position monitoring for accurate trading hours

### Contract Transition Support
- **Next month lot size** field in TradingSettings
- Handles NSE lot size changes (e.g., BANKNIFTY 35 -> 30)
- Automatic lot size selection based on contract month
- Updated freeze quantities per NSE circular (Dec 2025)

### Background Service Orchestration
- **Unified OptionChainBackgroundService** manages all background tasks
- Single shared WebSocket manager to prevent broker rate limits (Error 429)
- Flask app context management for database access in threads
- Coordinated startup/shutdown of Position Monitor and Risk Manager

### Technical Improvements
- Native Python threading (moved from eventlet for Python 3.13+ compatibility)
- Gthread worker for Gunicorn in production
- Parallelized order status polling across accounts
- UV package manager support (10-100x faster than pip)
- Cross-platform compatibility module (app/utils/compat.py)

### Updated Trading Settings (2025)

| Symbol | Lot Size | Next Month Lot | Freeze Qty | Max Lots/Order |
|--------|----------|----------------|------------|----------------|
| NIFTY | 75 | 75 | 1,800 | 24 |
| BANKNIFTY | 35 | 30 | 600 | 17 |
| SENSEX | 20 | 20 | 1,000 | 50 |

---

## Prerequisites

| Requirement | Version | Purpose |
|-------------|---------|---------|
| Python | 3.12+ | Core runtime |
| Node.js | 16+ | CSS build system (Tailwind) |
| OpenAlgo | Latest | Trading platform integration |
| SQLite | Built-in | Development database |
| TA-Lib | Latest | Technical analysis library |


---

## Quick Start Guide

### Method 1: Using UV (Recommended - 10-100x Faster)

```bash
# Install UV (if not already installed)
# Option 1: Using pip (simplest)
pip install uv

# Option 2: Windows PowerShell (standalone)
powershell -c "irm https://astral.sh/uv/install.ps1 | iex"

# Option 3: macOS/Linux (standalone)
curl -LsSf https://astral.sh/uv/install.sh | sh

# Clone repository
git clone https://github.com/marketcalls/algomirror.git
cd algomirror

# Create and activate virtual environment
uv venv
.venv\Scripts\activate      # Windows
source .venv/bin/activate   # macOS/Linux

# Install dependencies
uv pip install -e .

# Install with development dependencies
uv pip install -e ".[dev]"

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Initialize database
uv run init_db.py

# Run application
uv run wsgi.py
# Application available at: http://127.0.0.1:8000
```

### Method 2: Using pip (Traditional)

```bash
# Clone repository
git clone https://github.com/marketcalls/algomirror.git
cd algomirror

# Create and activate virtual environment
python -m venv venv
venv\Scripts\activate       # Windows
source venv/bin/activate    # macOS/Linux

# Install dependencies
pip install -r requirements.txt

# Install Node dependencies and build CSS
npm install
npm run build-css

# Configure environment
cp .env.example .env
# Edit .env with your settings

# Initialize database
python init_db.py

# Run application
python wsgi.py
# Application available at: http://127.0.0.1:8000
```

### First Login

1. Navigate to `http://127.0.0.1:8000`
2. Click "Get Started" to register
3. **First user automatically becomes admin** (zero-trust security)
4. Login and start adding your OpenAlgo accounts

---

## Strategy Builder

### Creating a Strategy

1. Navigate to **Strategy** > **Builder**
2. Configure basic information:
   - Strategy name and description
   - Market condition (Expiry/Non-Expiry/Any)
   - Risk profile (Fixed Lots, Balanced, Conservative, Aggressive)
   - Entry/Exit/Square-off times

3. Add strategy legs:
   - Select instrument (NIFTY, BANKNIFTY, SENSEX)
   - Choose product type (Options, Futures)
   - Configure strike selection (ATM, ITM, OTM with offset)
   - Set action (BUY/SELL) and lots

4. Configure risk management:
   - Max loss and max profit targets
   - Trailing stop loss settings (AFL-style ratcheting)
   - Supertrend exit configuration

5. Select accounts for execution

### Risk Profile Options

| Profile | Margin Usage | Description |
|---------|--------------|-------------|
| Fixed Lots | Manual | Uses explicit lot sizes from strategy legs |
| Conservative | 40% | Lower risk with smaller positions |
| Balanced | 65% | Moderate approach (default) |
| Aggressive | 80% | Higher risk with larger positions |

### Strike Selection Methods

- **ATM**: At-the-money strike
- **ITM**: In-the-money with configurable offset
- **OTM**: Out-of-the-money with configurable offset
- **Strike Price**: Specific strike price value
- **Premium Near**: Strike nearest to specified premium

---

## Risk Management

### Strategy-Level Risk Controls

```
Max Loss Target:
- Set maximum loss threshold for entire strategy
- Automatic exit when threshold breached
- Configurable auto-exit on/off
- Exit reason persisted: max_loss_exit_reason

Max Profit Target:
- Set profit target for strategy
- Automatic exit on target hit
- Lock in profits automatically
- Exit reason persisted: max_profit_exit_reason

Trailing Stop Loss (AFL-Style):
- Types: Percentage, Points, Amount
- Peak P&L tracking (ratchets up, never down)
- Formula: stop = initial_stop + (peak - initial) * factor
- Exit reason persisted: trailing_sl_exit_reason
```

### AFL-Style Trailing Stop Loss

The trailing stop loss implements an AFL (AmiBroker Formula Language) style ratcheting mechanism:

```
State Variables:
- trailing_sl_active: Boolean - Is TSL currently tracking
- trailing_sl_peak_pnl: Float - Highest P&L reached ("High" in AFL)
- trailing_sl_initial_stop: Float - First stop level when activated
- trailing_sl_trigger_pnl: Float - Current stop (only moves UP)

Logic:
1. TSL activates when position enters profit
2. Peak P&L tracks highest profit reached
3. Stop level = Peak P&L - (trailing_sl_value based on type)
4. Stop ONLY moves UP (ratchets), never down
5. Exit when current_pnl < trailing_stop
```

### Supertrend-Based Exits

Configure Supertrend exits in strategy settings:

| Setting | Default | Description |
|---------|---------|-------------|
| Period | 10 | ATR calculation period |
| Multiplier | 3.0 | Band multiplier |
| Timeframe | 10m | Candle timeframe |
| Exit Type | breakout | Exit on breakout or breakdown |

Exit signals:
- **Breakout**: Exit when price crosses above upper band (bullish)
- **Breakdown**: Exit when price crosses below lower band (bearish)

Exit reason captured: `"Breakout at Close: 150.25, ST: 145.50"`

### Risk Event Audit Logging

All risk threshold breaches are logged in the RiskEvent table:

```
Event Types:
- max_loss: Maximum loss threshold hit
- max_profit: Profit target achieved
- trailing_sl: Trailing stop loss triggered
- supertrend: Supertrend exit signal

Logged Information:
- strategy_id: Strategy that triggered
- execution_id: Specific execution (if applicable)
- event_type: Type of risk event
- threshold_value: The threshold that was breached
- current_value: P&L or price at trigger
- action_taken: close_all, close_partial, alert_only
- exit_order_ids: JSON list of exit orders placed
- triggered_at: Timestamp
- notes: Additional context
```

---

## Real-Time Position Monitoring

### How It Works

The PositionMonitor service provides real-time P&L tracking:

1. **Subscription Optimization**: Subscribes ONLY to symbols with open positions (status='entered')
2. **Primary Account Check**: Requires primary account to be connected before starting
3. **Trading Hours Awareness**: Respects TradingHoursTemplate from database
4. **Holiday Detection**: Checks MarketHoliday table before monitoring
5. **Special Sessions**: Handles Muhurat trading via SpecialTradingSession

### Architecture

```
PositionMonitor (Singleton)
├── should_start_monitoring()
│   ├── Check primary account exists
│   ├── Check primary account connected (ping)
│   ├── Check trading hours from database
│   └── Check not a market holiday
├── get_open_positions()
│   └── Query StrategyExecution where status='entered'
├── subscribe_to_positions()
│   └── Subscribe via shared WebSocket manager
└── update_position_prices()
    └── Update last_price, last_price_updated
```

### Integration with Risk Manager

```
Background Service
├── Shared WebSocket Manager (single connection)
├── Position Monitor Thread
│   └── Updates last_price on executions
└── Risk Manager Thread
    └── Checks thresholds using updated prices
```

---

## Margin Calculator

### How It Works

1. **Get Available Margin**: Fetches from account via OpenAlgo API
2. **Apply Trade Quality**: Multiplies by grade percentage (A=95%, B=65%, C=36%)
3. **Get Margin Requirement**: Based on instrument and expiry/non-expiry
4. **Calculate Lots**: Usable margin / margin per lot
5. **Apply Freeze Limit**: Cap at max_lots_per_order from settings

### Trade Quality Grades

| Grade | Margin % | Risk Level | Description |
|-------|----------|------------|-------------|
| A | 95% | Conservative | Maximum margin utilization |
| B | 65% | Moderate | Balanced approach |
| C | 36% | Aggressive | Lower capital deployment |

### Option Buying vs Selling

**Option Sellers (Margin-Based)**:
- Uses `availablecash` + collateral from funds API
- Margin requirement per lot from MarginRequirement model
- Grade percentage applied to available margin

**Option Buyers (Premium-Based)**:
- Uses `cash` only (no collateral)
- Premium per lot from `option_buying_premium` field
- Lot size = Cash / Premium per lot

### Default Margin Requirements (per lot)

**NIFTY/BANKNIFTY:**
| Trade Type | Expiry | Non-Expiry |
|------------|--------|------------|
| CE/PE Sell | 205,000 | 250,000 |
| CE & PE Sell | 250,000 | 320,000 |
| Futures | 215,000 | 215,000 |

**SENSEX:**
| Trade Type | Expiry | Non-Expiry |
|------------|--------|------------|
| CE/PE Sell | 180,000 | 220,000 |
| CE & PE Sell | 225,000 | 290,000 |
| Futures | 185,000 | 185,000 |

---

## Supertrend Indicator

### Implementation Details

AlgoMirror uses a Pine Script v6 compatible Supertrend implementation:

```python
# Key characteristics:
- Uses TA-Lib ATR (RMA-based, matching Pine Script ta.atr)
- Numba JIT compilation for performance
- Handles NaN values from ATR warmup period
- Direction: 1 = Bullish (use lower band), -1 = Bearish (use upper band)
```

### Calculation Formula

```
ATR = ta.atr(period)  # TA-Lib RMA-based ATR
HL2 = (High + Low) / 2

Basic Upper Band = HL2 + (Multiplier * ATR)
Basic Lower Band = HL2 - (Multiplier * ATR)

Final Bands adjusted based on previous close and bands
Direction changes when close crosses bands
```

### Background Exit Service

The Supertrend Exit Service runs as a daemon thread:

1. Monitors active strategies with Supertrend exits enabled
2. Fetches OHLC data at configured intervals
3. Calculates Supertrend and checks for direction changes
4. Triggers automatic exits on signal
5. Captures exit reason: `"Breakout at Close: {close}, ST: {supertrend}"`
6. Logs risk events for audit trail

---

## OpenAlgo Integration

### Supported Brokers (24+)

- 5paisa & 5paisa (XTS)
- Aliceblue
- AngelOne
- Compositedge (XTS)
- Definedge
- Dhan
- Firstock
- Flattrade
- Fyers
- Groww
- IIFL (XTS)
- IndiaBulls
- IndMoney
- Kotak Securities
- Motilal Oswal
- Paytm
- Pocketful
- Shoonya
- Samco
- Tradejini
- Upstox
- Wisdom Capital (XTS)
- Zebu
- Zerodha

### Extended OpenAlgo Client

```python
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

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

### Connection Testing

```bash
curl -X POST http://127.0.0.1:5000/api/v1/ping \
  -H "Content-Type: application/json" \
  -d '{"apikey":"your_api_key"}'

# Expected response
{
  "status": "success",
  "data": {
    "broker": "zerodha",
    "message": "pong"
  }
}
```

---

## Project Architecture

### Platform Overview

![AlgoMirror Platform Architecture](docs/algomirror-platform-architecture.png)

*Single AlgoMirror instance connecting to multiple OpenAlgo deployments across 24+ brokers*

### Internal Architecture

![AlgoMirror Internal Architecture](docs/algomirror-internal-architecture.png)

*Detailed system components showing all 7 architectural layers*

### Directory Structure

```
algomirror/
├── app/                              # Main application package
│   ├── __init__.py                   # Flask app factory
│   ├── models.py                     # SQLAlchemy models (38KB)
│   ├── auth/                         # Authentication blueprint
│   ├── main/                         # Dashboard and landing pages
│   ├── accounts/                     # Account management
│   ├── trading/                      # Trading operations
│   ├── strategy/                     # Strategy builder and execution
│   ├── margin/                       # Margin management
│   ├── api/                          # REST API endpoints
│   ├── tradingview/                  # TradingView webhook integration
│   ├── utils/                        # Utility modules
│   │   ├── openalgo_client.py        # Extended OpenAlgo client
│   │   ├── websocket_manager.py      # WebSocket manager (28KB)
│   │   ├── supertrend.py             # Numba-optimized Supertrend
│   │   ├── supertrend_exit_service.py # Background exit monitoring
│   │   ├── margin_calculator.py      # Dynamic lot sizing (32KB)
│   │   ├── strategy_executor.py      # Parallel execution (110KB)
│   │   ├── order_status_poller.py    # Background order polling (31KB)
│   │   ├── risk_manager.py           # Risk threshold monitoring (28KB)
│   │   ├── position_monitor.py       # Position tracking (19KB)
│   │   ├── background_service.py     # Service orchestration (44KB)
│   │   ├── session_manager.py        # WebSocket session management
│   │   ├── option_chain.py           # Option chain utilities
│   │   ├── compat.py                 # Cross-platform compatibility
│   │   └── rate_limiter.py           # Rate limiting decorators
│   ├── templates/                    # Jinja2 HTML templates
│   └── static/                       # CSS, JS, images
├── migrations/                       # Database migrations
├── migrate/upgrade/                  # Manual migration scripts
├── docs/                             # Documentation
├── logs/                             # Application logs
├── instance/                         # Instance-specific files
├── config.py                         # Configuration
├── app.py                            # Entry point
├── init_db.py                        # Database initialization
├── requirements.txt                  # Python dependencies
├── pyproject.toml                    # UV/pip project config
└── package.json                      # Node dependencies
```

### Database Models

**Core Models:**
- User - Authentication and authorization (first user = admin)
- TradingAccount - OpenAlgo connections with encrypted API keys
- ActivityLog - Audit trail

**Strategy Models:**
- Strategy - Strategy configuration with risk management settings
- StrategyLeg - Individual legs with instrument details
- StrategyExecution - Execution tracking with P&L and exit reasons

**Margin & Risk Models:**
- MarginRequirement - Instrument margin settings including option buying premium
- TradeQuality - A/B/C grade configurations with margin source
- MarginTracker - Real-time margin tracking per account
- RiskEvent - Risk threshold breach audit log
- TradingSettings - Lot sizes, next month lot sizes, and freeze quantities

**Configuration Models:**
- TradingHoursTemplate - Market hours configuration
- TradingSession - Day-wise trading sessions
- MarketHoliday - Holiday calendar
- SpecialTradingSession - Muhurat trading and special sessions
- WebSocketSession - Active WebSocket sessions for option chains

### Threading Architecture

```
Main Application (Flask/Gunicorn gthread worker)
└── HTTP Request Handlers

Background Daemon Threads:
├── OptionChainBackgroundService (Orchestrator)
│   ├── Shared WebSocket Manager (single connection)
│   ├── Position Monitor Thread
│   │   └── Price updates for open positions
│   └── Risk Manager Thread
│       └── P&L threshold monitoring
├── Supertrend Exit Service Thread
│   └── Indicator-based exit signals
└── Order Status Poller Thread
    └── Parallel order status sync across accounts

ThreadPoolExecutor (Strategy Execution):
└── Concurrent order placement across accounts

Note: Uses native Python threading (not eventlet)
Compatible with Python 3.13+ and TA-Lib
```

---

## Configuration Reference

### Environment Variables

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| SECRET_KEY | Yes | dev-key | Flask session encryption |
| DATABASE_URL | No | sqlite:///algomirror.db | Database connection |
| FLASK_ENV | No | development | Environment mode |
| LOG_LEVEL | No | INFO | Logging verbosity |
| REDIS_URL | No | memory:// | Redis for caching |
| ENCRYPTION_KEY | No | Auto-generated | Fernet encryption key |
| SESSION_TYPE | No | filesystem | Session storage type |
| POSITION_MONITOR_ENABLED | No | True | Enable position monitoring |
| PING_MONITORING_ENABLED | No | True | Enable health checks |

### Rate Limiting

| Endpoint | Limit | Purpose |
|----------|-------|---------|
| Global | 1000/minute | Overall IP protection |
| Authentication | 10/minute | Login/register/password |
| API Data | 100/minute | Trading data retrieval |
| Heavy Operations | 20/minute | Connection tests/refresh |

### Password Policy

- Minimum 8 characters
- At least one uppercase letter (A-Z)
- At least one lowercase letter (a-z)
- At least one digit (0-9)
- At least one special character (!@#$%^&*()_+-=[]{}|;:,.<>?)
- Cannot be common passwords

---

## Production Deployment

### Requirements

- PostgreSQL database
- Redis for caching (optional)
- Nginx/Apache reverse proxy
- SSL certificate (Let's Encrypt)
- Gunicorn with gthread worker

### Production Configuration

```env
SECRET_KEY=randomly-generated-256-bit-key
DATABASE_URL=postgresql://user:password@127.0.0.1/algomirror_prod
REDIS_URL=redis://127.0.0.1:6379/0
FLASK_ENV=production
SESSION_TYPE=sqlalchemy
LOG_LEVEL=WARNING
```

### Gunicorn Configuration

```bash
gunicorn -w 4 -k gthread --threads 2 -b 0.0.0.0:8000 app:app
```

### Nginx Configuration

```nginx
server {
    listen 443 ssl http2;
    server_name algomirror.yourdomain.com;

    ssl_certificate /etc/letsencrypt/live/algomirror.yourdomain.com/fullchain.pem;
    ssl_certificate_key /etc/letsencrypt/live/algomirror.yourdomain.com/privkey.pem;

    add_header Strict-Transport-Security "max-age=31536000; includeSubDomains" always;
    add_header X-Frame-Options "DENY" always;
    add_header X-Content-Type-Options "nosniff" always;

    location / {
        proxy_pass http://127.0.0.1:8000;
        proxy_set_header Host $host;
        proxy_set_header X-Real-IP $remote_addr;
        proxy_set_header X-Forwarded-For $proxy_add_x_forwarded_for;
        proxy_set_header X-Forwarded-Proto $scheme;

        proxy_http_version 1.1;
        proxy_set_header Upgrade $http_upgrade;
        proxy_set_header Connection "upgrade";
    }
}
```

---

## Troubleshooting

### Common Issues

**Connection Issues:**
```bash
# Verify OpenAlgo is running
curl http://127.0.0.1:5000
curl -X POST http://127.0.0.1:5000/api/v1/ping -d '{"apikey":"test"}'
```

**Database Issues:**
```bash
# Using UV (recommended)
uv run init_db.py reset  # Warning: Deletes all data
uv run init_db.py

# Or with activated venv
python init_db.py reset
python init_db.py
```

**CSS/Styling Issues:**
```bash
npm run build-css
# Check app/static/css/compiled.css exists
```

**TA-Lib Import Errors:**
```bash
# Ensure TA-Lib C library is installed
# Windows: Download from GitHub releases
# Linux: sudo apt-get install ta-lib
# macOS: brew install ta-lib
```

**WebSocket Connection Issues:**
```bash
# Check primary account is connected
# Verify trading hours (not outside market hours)
# Check not a market holiday
# Review logs/algomirror.log for errors
```

### Debug Checklist

1. Virtual environment activated
2. All dependencies installed
3. CSS compiled (`npm run build-css`)
4. Database initialized (`uv run init_db.py` or `python init_db.py`)
5. OpenAlgo server running
6. Valid API key configured
7. Primary account set and connected
8. Within trading hours (check TradingHoursTemplate)
9. Not a market holiday (check MarketHoliday)
10. No errors in `logs/algomirror.log`

---

## Database Management

```bash
# Initialize fresh database
uv run init_db.py        # Using UV
python init_db.py        # Or with activated venv

# Reset database (deletes all data)
uv run init_db.py reset
python init_db.py reset

# Create test data (development only)
uv run init_db.py testdata
python init_db.py testdata
```

### Running Migrations

AlgoMirror uses a custom migration system located in the `migrate/` folder:

```bash
# Navigate to the migrate directory
cd migrate

# Run all pending migrations using UV
uv run migrate_all.py

# Or using Python directly
python migrate_all.py
```

The migration runner will:
1. Check which migrations have already been applied
2. Run any pending migrations in order (sorted by filename)
3. Track applied migrations in the `applied_migrations` table
4. Report success/failure for each migration

Migration files are located in `migrate/upgrade/` and are numbered sequentially (e.g., `001_initial.py`, `002_add_field.py`).

### Flask-Migrate (Alternative)

For Flask-Migrate based migrations:

```bash
# Create migration after model changes
flask db migrate -m "Description"

# Apply migrations
flask db upgrade
```

---

## Version History

### v1.0.0 (Current)

**Core Features:**
- Strategy builder with multi-leg support
- Supertrend indicator (Pine Script v6 compatible)
- Risk management (max loss/profit, trailing SL, Supertrend exits)
- Dynamic margin calculator with trade quality grades
- Parallel strategy execution with ThreadPoolExecutor
- Multi-account OpenAlgo integration (24+ brokers)

**Real-Time Monitoring:**
- WebSocket-based position monitoring (5-50 symbols vs 1000+)
- Primary account connection requirement
- Trading hours from database (TradingHoursTemplate)
- Shared WebSocket manager (prevents broker rate limits)

**Risk Management Enhancements:**
- AFL-style trailing stop loss with peak P&L tracking
- Persistent exit reason tracking for all risk events
- RiskEvent audit logging for compliance
- Max loss/profit exit reason persistence

**Margin Calculator:**
- Option buying premium configuration
- Next month lot size support for contract transitions
- Updated freeze quantities (NSE Dec 2025 circular)

**Technical Improvements:**
- Native Python threading (gthread worker)
- Python 3.13+ compatibility
- UV package manager support (10-100x faster)
- Cross-platform compatibility module
- Background service orchestration

**Security:**
- Zero-trust security architecture
- Fernet encryption for API keys
- Multi-tier rate limiting
- Single-user security model

**UI/UX:**
- Real-time dashboard
- Mobile-responsive UI with OpenAlgo theme
- Special trading session support (Muhurat)

---

## Tech Stack

### Backend
| Technology | Version | Purpose |
|------------|---------|---------|
| Python | 3.12+ | Core runtime |
| Flask | 3.1+ | Web framework |
| SQLAlchemy | 2.0+ | ORM and database |
| Flask-Login | 0.6+ | Authentication |
| Flask-WTF | 1.2+ | Forms and CSRF |
| Flask-Migrate | 4.1+ | Database migrations |
| Flask-Limiter | 3.12+ | Rate limiting |
| Flask-Talisman | 1.1+ | Security headers |
| Gunicorn | 21+ | Production WSGI server |
| APScheduler | 3.11+ | Background scheduling |

### Technical Analysis
| Technology | Version | Purpose |
|------------|---------|---------|
| TA-Lib | 0.6+ | Technical indicators |
| Numba | 0.61+ | JIT compilation for performance |
| NumPy | 2.2+ | Numerical computing |
| Pandas | 2.3+ | Data manipulation |

### Frontend
| Technology | Version | Purpose |
|------------|---------|---------|
| TailwindCSS | 3.4+ | Utility-first CSS |
| DaisyUI | 4.12+ | UI components |
| Jinja2 | 3.1+ | Templating engine |

### Security
| Technology | Purpose |
|------------|---------|
| Fernet (Cryptography) | AES-128 API key encryption |
| Flask-Talisman | CSP and security headers |
| Flask-WTF | CSRF protection |
| Werkzeug | Password hashing |

### DevOps
| Technology | Purpose |
|------------|---------|
| Docker | Containerization |
| Docker Compose | Multi-container orchestration |
| UV | Fast package management (10-100x faster than pip) |
| Node.js/npm | CSS build system |

### Database Support
| Database | Environment |
|----------|-------------|
| SQLite | Development |
| PostgreSQL | Production |
| Redis | Caching & Sessions (optional) |

---

## Credits & Acknowledgments

### Core Platform
- **[OpenAlgo](https://openalgo.in)** - Open Source Algorithmic Trading Platform by Rajandran R
  - REST API integration
  - WebSocket data streaming
  - Multi-broker support (24+ brokers)

### Libraries & Frameworks
- **[Flask](https://flask.palletsprojects.com/)** - Micro web framework by Armin Ronacher / Pallets Projects
- **[SQLAlchemy](https://www.sqlalchemy.org/)** - SQL toolkit and ORM by Mike Bayer
- **[TailwindCSS](https://tailwindcss.com/)** - Utility-first CSS framework by Adam Wathan
- **[DaisyUI](https://daisyui.com/)** - Tailwind CSS component library by Pouya Saadeghi
- **[TA-Lib](https://ta-lib.org/)** - Technical Analysis Library
- **[Numba](https://numba.pydata.org/)** - JIT compiler for Python by Anaconda
- **[UV](https://github.com/astral-sh/uv)** - Ultra-fast Python package manager by Astral

### Technical Indicators
- **Supertrend Implementation** - Based on Pine Script v6 specification
  - ATR calculation using TA-Lib (RMA-based)
  - Numba-optimized for performance

### Icons & Design
- **[Heroicons](https://heroicons.com/)** - Beautiful hand-crafted SVG icons by Tailwind Labs

### Development Tools
- **[Claude Code](https://claude.ai/code)** - AI-assisted development by Anthropic

---

## Disclaimer

**Always test thoroughly in OpenAlgo Sandbox mode before deploying to a live trading account.**

Trading in Futures & Options (F&O) involves substantial risk of loss and is not suitable for all investors. Past performance does not guarantee future results. You are solely responsible for your trading decisions.

This software is provided "as is" without warranty of any kind. The developers are not responsible for any financial losses incurred through the use of this software.

**SEBI Static IP Compliant** - This application is designed to work with SEBI-compliant broker APIs.

---

## Support

- **Documentation**: See `docs/` folder for detailed guides
- **GitHub Issues**: Report bugs and feature requests
- **OpenAlgo Discord**: Community support

---

## License

This project is licensed under the **GNU Affero General Public License v3.0 (AGPL-3.0)**.

See the [LICENSE](LICENSE) file for details.

---

**Powered by [OpenAlgo](https://openalgo.in)** - Open Source Algorithmic Trading Platform
