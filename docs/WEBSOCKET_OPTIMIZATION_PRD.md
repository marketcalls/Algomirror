# AlgoMirror WebSocket Optimization & Risk Monitoring Enhancement

**Product Requirements Document (PRD)**

**Version:** 1.0
**Date:** 2025-01-10
**Status:** Draft for Review
**Owner:** Product & Engineering Team

---

## 1. Executive Summary

### Current State
- **Problem**: Background WebSocket service subscribes to 1000+ option symbols automatically, causing severe performance degradation during market hours
- **Impact**: Order execution delays of 3-10 seconds during market hours vs instant execution after hours
- **Root Cause**: Python GIL contention from processing ~1000 WebSocket messages/second competing with order placement threads

### Proposed Solution
**Intelligent On-Demand WebSocket Management**
- Monitor ONLY open positions (typically 5-50 symbols vs 1000)
- Load option chain data on-demand when user visits the page
- Implement real-time risk management monitoring (Max Loss/Profit/Trailing SL)
- Reduce CPU load by 90%+ and eliminate order delays

---

## 2. Current Architecture Analysis

### 2.1 WebSocket Service (Automatic - Always Running)
**File**: `app/utils/background_service.py`

**Current Behavior**:
```python
# Lines 104-107
self.start_option_chain('NIFTY')      # 4 expiries × 82 options = 328 symbols
self.start_option_chain('BANKNIFTY')  # 4 expiries × 82 options = 328 symbols
self.start_option_chain('SENSEX')     # 4 expiries × 82 options = 328 symbols
# Total: ~984 option symbols + 3 index quotes = 987 active subscriptions
```

**Strike Range**:
```python
# app/utils/option_chain.py:115-138
for i in range(20, 0, -1):  # 20 ITM strikes
# ATM strike (1)
for i in range(1, 21):      # 20 OTM strikes
# = 41 strikes × 2 (CE+PE) = 82 symbols per expiry
```

**Subscription Load**:
- 3 underlyings
- 4 expiries each = 12 total expiries
- 41 strikes per expiry
- 2 option types (CE + PE)
- **Total**: 3 × 4 × 41 × 2 + 3 = **987 active WebSocket subscriptions**

### 2.2 Risk Management (NOT Currently Monitored)
**File**: `app/models.py:270-272`

```python
class Strategy(db.Model):
    # Risk management fields exist but are NOT monitored in real-time
    max_loss = db.Column(db.Float)        # ❌ Not monitored
    max_profit = db.Column(db.Float)      # ❌ Not monitored
    trailing_sl = db.Column(db.Float)     # ❌ Not monitored
```

**Current Risk Monitoring**:
- ✅ Supertrend Exit: Monitored every minute via API (not WebSocket)
- ❌ Max Loss: Defined but NOT monitored
- ❌ Max Profit: Defined but NOT monitored
- ❌ Trailing SL: Defined but NOT monitored

### 2.3 Services Overview

| Service | Purpose | Frequency | Method | Impact |
|---------|---------|-----------|--------|--------|
| **Option Chain WebSocket** | Real-time option prices | Continuous | WebSocket | 🔴 HIGH CPU (1000+ subs) |
| **Order Status Poller** | Update pending orders | Every 2s | API Poll | 🟡 MEDIUM |
| **Supertrend Monitor** | Exit signal detection | Every 1m | API Poll | 🟢 LOW |
| **Position Risk Monitor** | Max Loss/Profit/Trailing SL | ❌ MISSING | ❌ MISSING | ❌ N/A |

---

## 3. Proposed Architecture

### 3.1 Position-Centric WebSocket Management

```
┌─────────────────────────────────────────────────────────────────┐
│                     WebSocket Manager (New)                      │
├─────────────────────────────────────────────────────────────────┤
│                                                                   │
│  ┌────────────────────────┐    ┌──────────────────────────────┐ │
│  │ Open Position Monitor  │    │ Option Chain On-Demand       │ │
│  │ (Always Running)       │    │ (User-Triggered)             │ │
│  ├────────────────────────┤    ├──────────────────────────────┤ │
│  │ • Subscribes ONLY to   │    │ • Loads when user visits     │ │
│  │   symbols with open    │    │   /trading/option-chain      │ │
│  │   positions            │    │ • Unsubscribes on page exit  │ │
│  │ • Typical: 5-50 symbols│    │ • Per-session subscriptions  │ │
│  │ • Monitors:            │    │ • Configurable strikes/exp   │ │
│  │   - Real-time P&L      │    │                               │ │
│  │   - Max Loss trigger   │    │                               │ │
│  │   - Max Profit trigger │    │                               │ │
│  │   - Trailing SL        │    │                               │ │
│  └────────────────────────┘    └──────────────────────────────┘ │
│                                                                   │
└─────────────────────────────────────────────────────────────────┘
```

### 3.2 New Components

#### Component 1: Position WebSocket Monitor
**File**: `app/utils/position_monitor.py` (NEW)

**Responsibilities**:
1. Query open positions from database (status='entered')
2. Subscribe to WebSocket for those symbols only
3. Calculate real-time P&L updates
4. Monitor risk thresholds:
   - Max Loss (strategy-level)
   - Max Profit (strategy-level)
   - Trailing Stop Loss (strategy-level)
5. Auto-trigger exits when thresholds breached
6. Update subscriptions dynamically as positions open/close

**Subscription Count**: ~5-50 symbols (vs current 1000+)

#### Component 2: Option Chain Session Manager
**File**: `app/trading/routes.py` (MODIFY)

**Responsibilities**:
1. Detect when user navigates to `/trading/option-chain`
2. Create session-specific WebSocket subscriptions
3. Load configurable range (default: 1 expiry, 10 strikes)
4. Unsubscribe on page navigation/close
5. Use WebSocket heartbeat to detect disconnections

**Implementation**: Session-based subscription tracking with Redis

#### Component 3: Risk Management Service
**File**: `app/utils/risk_manager.py` (NEW)

**Responsibilities**:
1. Monitor position P&L via WebSocket updates
2. Check Max Loss threshold per strategy
3. Check Max Profit threshold per strategy
4. Calculate and monitor Trailing Stop Loss
5. Trigger automatic exits when thresholds hit
6. Log all risk events for audit trail

---

## 4. Database Schema Changes

### 4.1 New Tables

#### Table: `websocket_sessions`
Tracks active WebSocket sessions for option chain viewing

```sql
CREATE TABLE websocket_sessions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    session_id VARCHAR(64) UNIQUE NOT NULL,
    underlying VARCHAR(20) NOT NULL,          -- NIFTY, BANKNIFTY, SENSEX
    expiry VARCHAR(20) NOT NULL,
    subscribed_symbols JSON,                  -- List of subscribed symbols
    is_active BOOLEAN DEFAULT TRUE,
    last_heartbeat DATETIME,
    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    expires_at DATETIME,                      -- Auto-cleanup old sessions

    FOREIGN KEY (user_id) REFERENCES users(id) ON DELETE CASCADE
);

CREATE INDEX idx_websocket_sessions_active ON websocket_sessions(is_active, user_id);
CREATE INDEX idx_websocket_sessions_expiry ON websocket_sessions(expires_at);
```

#### Table: `risk_events`
Audit log for risk threshold triggers

```sql
CREATE TABLE risk_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    strategy_id INTEGER NOT NULL,
    execution_id INTEGER,                     -- NULL for strategy-level events
    event_type VARCHAR(50) NOT NULL,          -- 'max_loss', 'max_profit', 'trailing_sl', 'supertrend'
    threshold_value FLOAT,                    -- The threshold that was breached
    current_value FLOAT,                      -- Current P&L or price
    action_taken VARCHAR(50),                 -- 'close_all', 'close_partial', 'alert_only'
    exit_order_ids JSON,                      -- List of exit orders placed
    triggered_at DATETIME DEFAULT CURRENT_TIMESTAMP,
    notes TEXT,

    FOREIGN KEY (strategy_id) REFERENCES strategies(id) ON DELETE CASCADE,
    FOREIGN KEY (execution_id) REFERENCES strategy_executions(id) ON DELETE SET NULL
);

CREATE INDEX idx_risk_events_strategy ON risk_events(strategy_id, triggered_at);
CREATE INDEX idx_risk_events_type ON risk_events(event_type, triggered_at);
```

### 4.2 Modified Tables

#### Table: `strategies`
Add new fields for risk monitoring configuration

```sql
ALTER TABLE strategies ADD COLUMN risk_monitoring_enabled BOOLEAN DEFAULT TRUE;
ALTER TABLE strategies ADD COLUMN risk_check_interval INTEGER DEFAULT 1;  -- Seconds
ALTER TABLE strategies ADD COLUMN auto_exit_on_max_loss BOOLEAN DEFAULT TRUE;
ALTER TABLE strategies ADD COLUMN auto_exit_on_max_profit BOOLEAN DEFAULT TRUE;
ALTER TABLE strategies ADD COLUMN trailing_sl_type VARCHAR(20) DEFAULT 'percentage';  -- 'percentage', 'points', 'amount'
```

#### Table: `strategy_executions`
Add fields for tracking real-time monitoring

```sql
ALTER TABLE strategy_executions ADD COLUMN last_price FLOAT;              -- Latest price from WebSocket
ALTER TABLE strategy_executions ADD COLUMN last_price_updated DATETIME;   -- When price was last updated
ALTER TABLE strategy_executions ADD COLUMN websocket_subscribed BOOLEAN DEFAULT FALSE;
ALTER TABLE strategy_executions ADD COLUMN trailing_sl_triggered FLOAT;   -- Price at which trailing SL was triggered
```

---

## 5. Implementation Plan

### Phase 1: Core Position Monitor (Week 1-2)

#### Sprint 1.1: Database Migration & Models
**Files to Create**:
- `migrations/versions/xxx_add_risk_monitoring.py`

**Files to Modify**:
- `app/models.py` - Add RiskEvent model

**Tasks**:
- [ ] Create `websocket_sessions` table migration
- [ ] Create `risk_events` table migration
- [ ] Add columns to `strategies` table
- [ ] Add columns to `strategy_executions` table
- [ ] Create RiskEvent model class
- [ ] Create WebSocketSession model class
- [ ] Run migrations and test

**Acceptance Criteria**:
- All migrations run successfully
- Models can be imported without errors
- Database schema validated

#### Sprint 1.2: Position Monitor Service
**Files to Create**:
- `app/utils/position_monitor.py`
- `tests/test_position_monitor.py`

**Tasks**:
- [ ] Create PositionMonitor singleton class
- [ ] Implement `get_open_positions()` - query DB for status='entered'
- [ ] Implement `subscribe_to_positions()` - subscribe to WebSocket
- [ ] Implement `unsubscribe_from_position()` - remove subscription
- [ ] Implement `handle_price_update()` - process WebSocket data
- [ ] Implement `update_position_pnl()` - calculate real-time P&L
- [ ] Add comprehensive logging
- [ ] Write unit tests

**Acceptance Criteria**:
- Service starts/stops cleanly
- Correctly identifies open positions
- Subscribes only to required symbols
- Updates P&L in real-time
- Tests pass with 90%+ coverage

#### Sprint 1.3: Risk Manager Service
**Files to Create**:
- `app/utils/risk_manager.py`
- `tests/test_risk_manager.py`

**Tasks**:
- [ ] Create RiskManager singleton class
- [ ] Implement `check_max_loss()` - compare strategy total P&L
- [ ] Implement `check_max_profit()` - compare strategy total P&L
- [ ] Implement `check_trailing_sl()` - calculate and compare
- [ ] Implement `trigger_exit()` - place exit orders
- [ ] Implement `log_risk_event()` - write to risk_events table
- [ ] Add alert notifications (email/SMS if configured)
- [ ] Write unit tests

**Acceptance Criteria**:
- Correctly detects threshold breaches
- Places exit orders successfully
- Logs all events to database
- Handles edge cases (multiple breaches, network errors)
- Tests pass with 90%+ coverage

### Phase 2: Option Chain On-Demand (Week 3-4)

#### Sprint 2.1: Session Management
**Files to Create**:
- `app/utils/session_manager.py`

**Files to Modify**:
- `app/trading/routes.py`

**Tasks**:
- [ ] Create WebSocketSessionManager class
- [ ] Implement `create_session()` - new option chain session
- [ ] Implement `update_heartbeat()` - keep session alive
- [ ] Implement `cleanup_expired_sessions()` - remove stale sessions
- [ ] Add session validation middleware
- [ ] Add WebSocket heartbeat mechanism
- [ ] Schedule cleanup job (APScheduler)

**Acceptance Criteria**:
- Sessions created when user visits page
- Heartbeat updates on activity
- Sessions cleaned up after inactivity (default: 5 minutes)
- Multiple users can have concurrent sessions

#### Sprint 2.2: Option Chain Frontend Integration
**Files to Modify**:
- `app/templates/trading/option_chain.html`

**Tasks**:
- [ ] Add JavaScript session management
- [ ] Implement `startOptionChainSession()` on page load
- [ ] Implement `sendHeartbeat()` every 30 seconds
- [ ] Implement `stopOptionChainSession()` on page unload
- [ ] Add configuration UI (expiries, strikes to load)
- [ ] Add loading states and error handling
- [ ] Test multi-tab behavior

**Acceptance Criteria**:
- Option chain loads data on demand
- Subscriptions created only when needed
- Clean unsubscribe on navigation
- No memory leaks in browser
- Works across all supported browsers

#### Sprint 2.3: Configuration Management
**Files to Create**:
- Migration for configuration settings

**Files to Modify**:
- `.env.example` - add configuration options
- `config.py` - add config class

**Tasks**:
- [ ] Add `OPTION_CHAIN_AUTO_LOAD` (default: False)
- [ ] Add `OPTION_CHAIN_DEFAULT_EXPIRIES` (default: 1)
- [ ] Add `OPTION_CHAIN_DEFAULT_STRIKES` (default: 10)
- [ ] Add `OPTION_CHAIN_SESSION_TIMEOUT` (default: 300 seconds)
- [ ] Add admin UI for configuration
- [ ] Document all settings

**Acceptance Criteria**:
- Configuration options work as expected
- Defaults are sensible for production
- Admin can adjust without code changes

### Phase 3: Integration & Testing (Week 5)

#### Sprint 3.1: Service Integration
**Files to Modify**:
- `app/__init__.py` - initialize new services
- `app/utils/background_service.py` - disable auto option chains

**Tasks**:
- [ ] Initialize PositionMonitor in app factory
- [ ] Initialize RiskManager in app factory
- [ ] Initialize WebSocketSessionManager
- [ ] Disable automatic option chain loading
- [ ] Update service startup order
- [ ] Add health check endpoints
- [ ] Add metrics collection

**Acceptance Criteria**:
- All services start correctly
- No circular dependencies
- Health checks return correct status
- Metrics available for monitoring

#### Sprint 3.2: End-to-End Testing
**Files to Create**:
- `tests/integration/test_position_monitoring.py`
- `tests/integration/test_risk_triggers.py`
- `tests/integration/test_option_chain_session.py`

**Tasks**:
- [ ] Test position monitoring with live data
- [ ] Test risk threshold triggers
- [ ] Test option chain session lifecycle
- [ ] Test multi-user concurrent access
- [ ] Load testing with realistic scenarios
- [ ] Document test results

**Acceptance Criteria**:
- All integration tests pass
- Performance improved (measure order latency)
- CPU usage reduced (measure with profiler)
- No regressions in existing functionality

### Phase 4: Monitoring & Documentation (Week 6)

#### Sprint 4.1: Monitoring Dashboard
**Files to Create**:
- `app/templates/admin/monitoring.html`
- `app/admin/routes.py` (modify)

**Tasks**:
- [ ] Create admin monitoring dashboard
- [ ] Display active WebSocket subscriptions
- [ ] Display position monitoring status
- [ ] Display risk events log
- [ ] Display session metrics
- [ ] Add real-time charts (subscriptions over time)

**Acceptance Criteria**:
- Dashboard shows real-time metrics
- Admin can view subscription counts
- Risk events are visible and filterable
- Performance metrics displayed

#### Sprint 4.2: Documentation
**Files to Create/Modify**:
- `docs/WEBSOCKET_OPTIMIZATION.md`
- `docs/RISK_MANAGEMENT.md`
- `CLAUDE.md` - update with new architecture
- `README.md` - update features list

**Tasks**:
- [ ] Document new architecture
- [ ] Document configuration options
- [ ] Update API documentation
- [ ] Create user guide for risk management
- [ ] Create troubleshooting guide
- [ ] Record demo video

**Acceptance Criteria**:
- Documentation is clear and complete
- All configuration options documented
- User guide covers common scenarios
- Video demo shows new features

---

## 6. Performance Metrics

### Before Optimization
| Metric | Current Value |
|--------|---------------|
| WebSocket Subscriptions | ~1000 symbols |
| Order Execution Time (Market Hours) | 3-10 seconds |
| Order Execution Time (After Hours) | <0.5 seconds |
| CPU Usage (Market Hours) | 60-80% |
| Memory Usage | 800MB-1.2GB |
| Risk Monitoring | ❌ Not implemented |

### After Optimization (Expected)
| Metric | Target Value | Improvement |
|--------|--------------|-------------|
| WebSocket Subscriptions | 5-50 symbols | **95% reduction** |
| Order Execution Time (Market Hours) | <0.5 seconds | **90% faster** |
| Order Execution Time (After Hours) | <0.5 seconds | No change |
| CPU Usage (Market Hours) | 10-20% | **75% reduction** |
| Memory Usage | 300MB-500MB | **60% reduction** |
| Risk Monitoring | ✅ Real-time | **NEW FEATURE** |

---

## 7. Risk Assessment

### Technical Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| WebSocket connection failures | Medium | High | Implement automatic reconnection, fallback to polling |
| Race conditions in position updates | Medium | Medium | Use database transactions, proper locking |
| Memory leaks in session management | Low | Medium | Implement session cleanup, monitoring |
| Risk trigger false positives | Medium | High | Add confirmation delays, configurable thresholds |
| Performance degradation with many positions | Low | Medium | Load testing, optimization |

### Business Risks

| Risk | Probability | Impact | Mitigation |
|------|-------------|--------|------------|
| Users miss option chain auto-loading | High | Low | Add prominent "Load Option Chain" button, user education |
| Risk exits during market volatility | Medium | Medium | Add "pause risk monitoring" option, configurable delays |
| Increased support requests | Medium | Low | Comprehensive documentation, video tutorials |

---

## 8. Success Criteria

### Must Have (P0)
- ✅ Position monitoring with <10 WebSocket subscriptions for typical usage
- ✅ Order execution time <1 second during market hours
- ✅ Real-time Max Loss monitoring and auto-exit
- ✅ Real-time Max Profit monitoring and auto-exit
- ✅ Option chain loads on-demand only
- ✅ No regressions in existing functionality

### Should Have (P1)
- ✅ Trailing Stop Loss implementation
- ✅ Risk event audit log
- ✅ Admin monitoring dashboard
- ✅ Session heartbeat mechanism
- ✅ Configurable option chain strikes/expiries

### Nice to Have (P2)
- 📧 Email/SMS alerts for risk events
- 📊 Historical risk metrics and analytics
- 🔔 Browser notifications for threshold triggers
- 📱 Mobile-responsive monitoring dashboard

---

## 9. Rollback Plan

### Rollback Triggers
- Order execution time >5 seconds consistently
- Critical bugs in risk monitoring (false triggers)
- Data corruption or loss
- Service crashes >3 times in 24 hours

### Rollback Steps
1. Switch feature flag `POSITION_MONITOR_ENABLED=False`
2. Re-enable automatic option chain loading
3. Revert database migrations if needed
4. Restart application services
5. Monitor logs for 24 hours
6. Investigate root cause

### Feature Flags
```python
# config.py
POSITION_MONITOR_ENABLED = os.getenv('POSITION_MONITOR_ENABLED', 'true').lower() == 'true'
RISK_MANAGER_ENABLED = os.getenv('RISK_MANAGER_ENABLED', 'true').lower() == 'true'
OPTION_CHAIN_ON_DEMAND = os.getenv('OPTION_CHAIN_ON_DEMAND', 'true').lower() == 'true'
```

---

## 10. Appendix

### A. API Endpoints (New/Modified)

#### POST `/api/v1/websocket/session/start`
Start option chain WebSocket session
```json
{
  "underlying": "NIFTY",
  "expiry": "28-DEC-25",
  "strikes": 10,
  "expiries": 1
}
```

#### POST `/api/v1/websocket/session/heartbeat`
Keep session alive
```json
{
  "session_id": "abc123..."
}
```

#### POST `/api/v1/websocket/session/stop`
Stop WebSocket session
```json
{
  "session_id": "abc123..."
}
```

#### GET `/api/v1/risk-events`
Get risk event history
```
Query params: strategy_id, event_type, from_date, to_date
```

#### GET `/api/v1/monitoring/status`
Get current monitoring status
```json
{
  "active_subscriptions": 12,
  "open_positions": 8,
  "active_sessions": 2,
  "cpu_usage": 15.2,
  "memory_usage": 450
}
```

### B. Configuration Reference

```bash
# .env

# Position Monitor
POSITION_MONITOR_ENABLED=true
POSITION_MONITOR_INTERVAL=1  # seconds

# Risk Manager
RISK_MANAGER_ENABLED=true
RISK_CHECK_INTERVAL=1  # seconds
AUTO_EXIT_ON_MAX_LOSS=true
AUTO_EXIT_ON_MAX_PROFIT=true

# Option Chain
OPTION_CHAIN_ON_DEMAND=true
OPTION_CHAIN_AUTO_LOAD=false
OPTION_CHAIN_DEFAULT_EXPIRIES=1
OPTION_CHAIN_DEFAULT_STRIKES=10
OPTION_CHAIN_SESSION_TIMEOUT=300  # seconds

# WebSocket
WEBSOCKET_HEARTBEAT_INTERVAL=30  # seconds
WEBSOCKET_RECONNECT_ATTEMPTS=3
WEBSOCKET_RECONNECT_DELAY=5  # seconds
```

---

## 11. Sign-Off

This PRD requires approval from:

- [ ] Product Manager
- [ ] Engineering Lead
- [ ] QA Lead
- [ ] DevOps Lead

**Estimated Effort**: 6 weeks (1.5 engineers)
**Target Release**: Version 2.0
**Release Date**: TBD based on approval

