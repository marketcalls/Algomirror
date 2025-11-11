# WebSocket Optimization - Implementation Summary

## Quick Overview

**Problem**: 1000+ WebSocket subscriptions causing 3-10 second order delays during market hours
**Solution**: Monitor only open positions (5-50 symbols) + on-demand option chains
**Impact**: 95% reduction in subscriptions, 90% faster orders, new real-time risk management

---

## Critical Requirements (User-Specified)

### 1. Primary Account Connection Requirement
✅ **WebSocket starts ONLY if primary account is connected to OpenAlgo**
- Check primary account connection status before starting services
- If primary user not logged in → No WebSocket initialization
- Monitor connection status → Stop WebSocket if primary disconnects

### 2. Dynamic Market Hours (No Hardcoding)
✅ **Use Trading Hours Template from database**
- Query `TradingHoursTemplate` and `TradingSession` models
- Respect market holidays from `MarketHoliday` table
- Support special trading sessions from `SpecialTradingSession` table
- Already implemented in: `app/utils/background_service.py:346-398`

---

## Architecture Change Summary

### Current (❌ Inefficient)
```
┌─────────────────────────────────────┐
│  Auto-Start on App Launch           │
│  ├─ NIFTY (328 symbols)             │
│  ├─ BANKNIFTY (328 symbols)         │
│  └─ SENSEX (328 symbols)            │
│  = 984 option symbols + 3 indexes   │
│  = ~987 active subscriptions        │
│                                      │
│  Purpose: Option Chain + Monitoring │
│  CPU: 60-80%                        │
│  Order Delay: 3-10 seconds          │
└─────────────────────────────────────┘
```

### Proposed (✅ Optimized)
```
┌──────────────────────────────────────────────────────────────┐
│  Intelligent WebSocket Manager                               │
├──────────────────────────────────────────────────────────────┤
│                                                               │
│  Condition: PRIMARY ACCOUNT CONNECTED ✓                      │
│  Market Hours: FROM TRADING HOURS TEMPLATE ✓                 │
│                                                               │
│  ┌─────────────────────┐    ┌────────────────────────────┐  │
│  │ Position Monitor    │    │ Option Chain (On-Demand)   │  │
│  │ (Auto - Always On)  │    │ (User visits page)         │  │
│  ├─────────────────────┤    ├────────────────────────────┤  │
│  │ • Open positions    │    │ • Session-based            │  │
│  │ • 5-50 symbols      │    │ • Configurable range       │  │
│  │ • Real-time P&L     │    │ • Auto-unsubscribe         │  │
│  │ • Max Loss/Profit   │    │                            │  │
│  │ • Trailing SL       │    │                            │  │
│  └─────────────────────┘    └────────────────────────────┘  │
│                                                               │
│  CPU: 10-20% | Order Delay: <0.5s | NEW: Risk Management    │
└──────────────────────────────────────────────────────────────┘
```

---

## Database Migrations Required

### New Tables (2)
1. **`websocket_sessions`** - Track option chain user sessions
2. **`risk_events`** - Audit log for SL/Target triggers

### Modified Tables (2)
1. **`strategies`** - Add risk monitoring config fields
2. **`strategy_executions`** - Add real-time price tracking

**Migration Script**: `migrations/versions/xxx_add_risk_monitoring.py`

---

## New Files to Create

### Core Services (3 files)
```
app/utils/
├── position_monitor.py          # Monitor open positions via WebSocket
├── risk_manager.py              # Check Max Loss/Profit/Trailing SL
└── session_manager.py           # Manage option chain sessions
```

### Tests (3 files)
```
tests/
├── test_position_monitor.py
├── test_risk_manager.py
└── integration/
    └── test_risk_triggers.py
```

### Documentation (2 files)
```
docs/
├── WEBSOCKET_OPTIMIZATION.md    # Technical architecture
└── RISK_MANAGEMENT.md           # User guide
```

---

## Key Implementation Details

### 1. Primary Account Connection Check

**File**: `app/utils/position_monitor.py` (NEW)

```python
class PositionMonitor:
    def should_start_monitoring(self) -> bool:
        """Check if monitoring should start"""
        # 1. Check primary account exists
        primary_account = TradingAccount.query.filter_by(
            is_primary=True,
            is_active=True
        ).first()

        if not primary_account:
            logger.warning("No primary account found")
            return False

        # 2. Check primary account connection
        try:
            client = ExtendedOpenAlgoAPI(
                api_key=primary_account.get_api_key(),
                host=primary_account.host_url
            )
            ping_response = client.ping()

            if ping_response.get('status') != 'success':
                logger.warning(f"Primary account {primary_account.account_name} not connected")
                return False
        except Exception as e:
            logger.error(f"Failed to ping primary account: {e}")
            return False

        # 3. Check trading hours from template
        if not self.is_trading_hours():
            logger.info("Outside trading hours")
            return False

        return True
```

### 2. Trading Hours from Template (Reuse Existing)

**File**: `app/utils/background_service.py:346-398` (EXISTING)

```python
def is_trading_hours(self) -> bool:
    """
    Check if current time is within trading hours
    Uses TradingHoursTemplate from database (NO HARDCODING)
    """
    now = datetime.now(pytz.timezone('Asia/Kolkata'))
    today = now.date()
    current_time = now.time()
    day_of_week = now.weekday()  # 0=Monday, 6=Sunday

    # Check if today is a market holiday
    is_holiday = MarketHoliday.query.filter(
        MarketHoliday.holiday_date == today,
        MarketHoliday.is_active == True
    ).first()

    if is_holiday:
        logger.info(f"Market holiday: {is_holiday.holiday_name}")
        return False

    # Get trading sessions for today
    sessions = TradingSession.query.join(TradingHoursTemplate).filter(
        TradingSession.day_of_week == day_of_week,
        TradingSession.is_active == True,
        TradingHoursTemplate.is_active == True
    ).all()

    # Check if current time is within any session
    for session in sessions:
        if session.start_time <= current_time <= session.end_time:
            return True

    return False
```

### 3. Position Subscription Logic

**File**: `app/utils/position_monitor.py` (NEW)

```python
def subscribe_to_open_positions(self):
    """Subscribe to WebSocket for all open positions"""
    # Get open positions from database
    open_executions = StrategyExecution.query.filter_by(
        status='entered'
    ).all()

    # Filter out rejected/cancelled
    open_executions = [
        exec for exec in open_executions
        if not (hasattr(exec, 'broker_order_status') and
               exec.broker_order_status in ['rejected', 'cancelled'])
    ]

    logger.info(f"Found {len(open_executions)} open positions to monitor")

    # Group by symbol to avoid duplicate subscriptions
    symbols_to_subscribe = {}
    for execution in open_executions:
        key = f"{execution.symbol}_{execution.exchange}"
        if key not in symbols_to_subscribe:
            symbols_to_subscribe[key] = {
                'symbol': execution.symbol,
                'exchange': execution.exchange,
                'executions': []
            }
        symbols_to_subscribe[key]['executions'].append(execution)

    # Subscribe to each unique symbol
    for key, data in symbols_to_subscribe.items():
        self.websocket_manager.subscribe({
            'symbol': data['symbol'],
            'exchange': data['exchange'],
            'mode': 'quote'  # Need LTP for P&L calculation
        })
        logger.info(f"Subscribed to {data['symbol']} ({len(data['executions'])} positions)")

    return len(symbols_to_subscribe)
```

---

## Phased Rollout Strategy

### Phase 1: Foundation (Week 1-2)
- ✅ Database migrations
- ✅ Position Monitor service
- ✅ Primary account connection check
- ✅ Trading hours integration
- 🧪 Testing with <10 positions

### Phase 2: Risk Management (Week 3-4)
- ✅ Risk Manager service
- ✅ Max Loss monitoring
- ✅ Max Profit monitoring
- ✅ Trailing SL implementation
- 🧪 Testing with various strategies

### Phase 3: Option Chain On-Demand (Week 5)
- ✅ Session Manager
- ✅ Frontend integration
- ✅ Heartbeat mechanism
- 🧪 Multi-user testing

### Phase 4: Production Deployment (Week 6)
- ✅ Monitoring dashboard
- ✅ Documentation
- ✅ Feature flags
- 🚀 Gradual rollout to users

---

## Configuration (.env)

```bash
# Position Monitor
POSITION_MONITOR_ENABLED=true
POSITION_MONITOR_CHECK_PRIMARY_ACCOUNT=true  # NEW: Require primary connection
POSITION_MONITOR_INTERVAL=1

# Risk Manager
RISK_MANAGER_ENABLED=true
RISK_CHECK_INTERVAL=1
AUTO_EXIT_ON_MAX_LOSS=true
AUTO_EXIT_ON_MAX_PROFIT=true

# Option Chain
OPTION_CHAIN_ON_DEMAND=true
OPTION_CHAIN_AUTO_LOAD=false  # Disable automatic loading
OPTION_CHAIN_DEFAULT_EXPIRIES=1
OPTION_CHAIN_DEFAULT_STRIKES=10
OPTION_CHAIN_SESSION_TIMEOUT=300

# Trading Hours (from database - no hardcoding)
USE_TRADING_HOURS_TEMPLATE=true  # Always true - no hardcoding allowed
```

---

## Success Metrics

| Metric | Before | After | Improvement |
|--------|--------|-------|-------------|
| WebSocket Subs | 1000 | 5-50 | **95% ↓** |
| Order Latency | 3-10s | <0.5s | **90% ↓** |
| CPU Usage | 60-80% | 10-20% | **75% ↓** |
| Memory | 800MB | 300-500MB | **60% ↓** |
| Risk Monitoring | ❌ None | ✅ Real-time | **NEW** |

---

## Testing Checklist

### Unit Tests
- [ ] Position Monitor: subscription logic
- [ ] Risk Manager: threshold calculations
- [ ] Session Manager: lifecycle management
- [ ] Trading hours: from template (all scenarios)
- [ ] Primary account: connection check

### Integration Tests
- [ ] Position monitoring with live WebSocket
- [ ] Risk triggers (Max Loss/Profit)
- [ ] Option chain session (create/heartbeat/destroy)
- [ ] Primary account disconnect → services stop
- [ ] Market close → services stop

### Load Tests
- [ ] 50 concurrent positions monitored
- [ ] 10 users viewing option chain simultaneously
- [ ] Order execution during high WebSocket load
- [ ] Memory leak testing (24-hour run)

---

## Rollback Plan

**Feature Flags (Instant Rollback)**:
```python
POSITION_MONITOR_ENABLED=false  # Disable new monitoring
OPTION_CHAIN_AUTO_LOAD=true    # Re-enable old behavior
```

**Database Rollback**:
```bash
# Revert migrations
flask db downgrade -1
```

**Full Rollback Time**: <5 minutes

---

## Next Steps

1. **Review PRD**: `WEBSOCKET_OPTIMIZATION_PRD.md` (156 lines, comprehensive spec)
2. **Get Approvals**: Product, Engineering, QA, DevOps
3. **Create Jira Tickets**: Based on Phase 1-4 tasks
4. **Setup Development Environment**: Create feature branch
5. **Begin Phase 1**: Database migrations + Position Monitor

---

## Questions for Stakeholders

1. **Risk Management**: Should exits be immediate or have confirmation delay (e.g., 2-second breach)?
2. **Notifications**: Email/SMS alerts for risk events? (requires integration)
3. **Option Chain**: Default configuration for strikes (10) and expiries (1) acceptable?
4. **Monitoring**: Separate admin dashboard or integrate into existing?
5. **Timeline**: 6-week timeline acceptable or need accelerated delivery?

---

## Contact

- **PRD Document**: `WEBSOCKET_OPTIMIZATION_PRD.md`
- **Technical Questions**: Engineering Team
- **Business Questions**: Product Manager

**Status**: ✅ Ready for Review
**Priority**: 🔴 HIGH (Performance Critical)
**Complexity**: 🟡 MEDIUM (6 weeks, 1.5 engineers)
