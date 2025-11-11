# Edge Cases & Scenario Handling

## Complete Guide for WebSocket Optimization Edge Cases

**Related Documents:**
- Main PRD: `WEBSOCKET_OPTIMIZATION_PRD.md`
- Implementation Summary: `IMPLEMENTATION_SUMMARY.md`

---

## 1. Limit Order Scenarios

### 1.1 Limit Order Placed (Pending State)

**Scenario**: User submits a limit order at price X, but market is trading at price Y

**Current System Flow**:
```python
# app/models.py:444-445
status = db.Column(db.String(50))  # 'pending', 'entered', 'exited', 'stopped', 'error'
broker_order_status = db.Column(db.String(50))  # 'complete', 'open', 'rejected', 'cancelled'
```

**States**:
1. Order placed → `status='pending'`, `broker_order_status='open'`
2. Order filled → `status='entered'`, `broker_order_status='complete'`
3. Order cancelled → `status='error'`, `broker_order_status='cancelled'`
4. Order rejected → `status='error'`, `broker_order_status='rejected'`

**WebSocket Subscription Behavior**:

```python
# app/utils/position_monitor.py (NEW)

def should_monitor_execution(self, execution: StrategyExecution) -> bool:
    """
    Determine if an execution should be monitored via WebSocket

    Monitor if:
    - Status is 'entered' (position is open)
    - Status is 'pending' BUT broker_order_status is NOT 'rejected' or 'cancelled'
    """
    # Monitor open positions
    if execution.status == 'entered':
        # Double-check broker status is not rejected/cancelled
        if execution.broker_order_status in ['rejected', 'cancelled']:
            return False
        return True

    # Monitor pending limit orders (to calculate potential P&L)
    if execution.status == 'pending':
        # Only if order is still active
        if execution.broker_order_status in ['open', 'pending', 'trigger_pending']:
            return True
        # Don't monitor if cancelled/rejected
        if execution.broker_order_status in ['rejected', 'cancelled']:
            return False

    # Don't monitor exited, stopped, or error status
    return False
```

**Workflow**:
```
┌────────────────────────────────────────────────────────────────┐
│ LIMIT ORDER LIFECYCLE                                           │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. User Places Limit Order                                    │
│     ↓                                                           │
│     status='pending'                                           │
│     broker_order_status='open'                                 │
│     ✅ Subscribe to WebSocket (for price monitoring)           │
│                                                                 │
│  2a. Order Gets Filled                                         │
│      ↓                                                          │
│      Order Status Poller detects 'complete'                    │
│      status='entered'                                          │
│      broker_order_status='complete'                            │
│      ✅ Keep WebSocket subscription (now monitoring P&L)       │
│                                                                 │
│  2b. Order Gets Cancelled                                      │
│      ↓                                                          │
│      Order Status Poller detects 'cancelled'                   │
│      status='error'                                            │
│      broker_order_status='cancelled'                           │
│      ❌ Unsubscribe from WebSocket                             │
│                                                                 │
│  2c. Order Gets Rejected                                       │
│      ↓                                                          │
│      Order Status Poller detects 'rejected'                    │
│      status='error'                                            │
│      broker_order_status='rejected'                            │
│      ❌ Unsubscribe from WebSocket                             │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

### 1.2 Handling in Order Status Poller

**File**: `app/utils/order_status_poller.py:116-195` (EXISTING)

**Enhancement Required**:

```python
# app/utils/order_status_poller.py (MODIFY)

def _check_order_status(self, execution_id: int, order_info: Dict, app):
    """Check status of a single order and update WebSocket subscriptions"""
    # ... existing code ...

    with app.app_context():
        execution = StrategyExecution.query.get(execution_id)
        if not execution:
            self.remove_order(execution_id)
            return

        # Update status
        old_status = execution.status
        old_broker_status = execution.broker_order_status

        execution.broker_order_status = broker_status

        if broker_status == 'complete':
            execution.status = 'entered'
            execution.entry_price = avg_price
            execution.entry_time = datetime.utcnow()

            # NOTIFY POSITION MONITOR: New position to subscribe
            from app.utils.position_monitor import position_monitor
            position_monitor.on_order_filled(execution)

        elif broker_status in ['rejected', 'cancelled']:
            execution.status = 'error'

            # NOTIFY POSITION MONITOR: Remove subscription
            from app.utils.position_monitor import position_monitor
            position_monitor.on_order_cancelled(execution)

        db.session.commit()

        # Remove from polling queue if terminal state
        if broker_status in ['complete', 'rejected', 'cancelled']:
            self.remove_order(execution_id)
```

---

## 2. Order Cancellation Scenarios

### 2.1 User Cancels Pending Limit Order

**Trigger**: User manually cancels order from frontend OR order auto-cancelled by broker

**WebSocket Cleanup**:

```python
# app/utils/position_monitor.py (NEW)

def on_order_cancelled(self, execution: StrategyExecution):
    """
    Called when an order is cancelled or rejected
    Removes WebSocket subscription if no other positions for same symbol
    """
    symbol = execution.symbol
    exchange = execution.exchange

    # Check if there are OTHER open positions for this symbol
    other_positions = StrategyExecution.query.filter(
        StrategyExecution.symbol == symbol,
        StrategyExecution.exchange == exchange,
        StrategyExecution.status.in_(['entered', 'pending']),
        StrategyExecution.id != execution.id
    ).filter(
        ~StrategyExecution.broker_order_status.in_(['rejected', 'cancelled'])
    ).count()

    if other_positions == 0:
        # No other positions, safe to unsubscribe
        logger.info(f"Unsubscribing from {symbol} - no active positions")
        self.websocket_manager.unsubscribe({
            'symbol': symbol,
            'exchange': exchange
        })
    else:
        logger.info(f"Keeping subscription for {symbol} - {other_positions} other positions active")
```

### 2.2 Market Order Rejected (Insufficient Funds)

**Scenario**: User places market order but account has insufficient funds

**Handling**:
```python
# Order Status Poller will detect broker_order_status='rejected'
# → Update execution.status='error'
# → Trigger position_monitor.on_order_cancelled()
# → Unsubscribe if no other positions
```

---

## 3. Market Closure Scenarios

### 3.1 Market Close During Active Session

**Scenario**: Markets close at 3:30 PM, but user has open positions

**Trading Hours Check**:
```python
# app/utils/position_monitor.py (NEW)

def check_trading_hours(self):
    """
    Check if we're within trading hours using TradingHoursTemplate
    NO HARDCODING - uses database
    """
    now = datetime.now(pytz.timezone('Asia/Kolkata'))
    today = now.date()
    current_time = now.time()
    day_of_week = now.weekday()  # 0=Monday, 6=Sunday

    # 1. Check if today is a market holiday
    is_holiday = MarketHoliday.query.filter(
        MarketHoliday.holiday_date == today,
        MarketHoliday.is_active == True
    ).first()

    if is_holiday:
        logger.info(f"Market holiday: {is_holiday.holiday_name}")
        return False, 'holiday', is_holiday.holiday_name

    # 2. Check for special trading session (e.g., Muhurat Trading)
    special_session = SpecialTradingSession.query.filter(
        SpecialTradingSession.session_date == today,
        SpecialTradingSession.is_active == True
    ).first()

    if special_session:
        if special_session.start_time <= current_time <= special_session.end_time:
            logger.info(f"Special session active: {special_session.session_name}")
            return True, 'special_session', special_session.session_name
        else:
            logger.info(f"Special session outside hours: {special_session.session_name}")
            return False, 'special_session_closed', special_session.session_name

    # 3. Check regular trading sessions
    sessions = TradingSession.query.join(TradingHoursTemplate).filter(
        TradingSession.day_of_week == day_of_week,
        TradingSession.is_active == True,
        TradingHoursTemplate.is_active == True
    ).all()

    for session in sessions:
        if session.start_time <= current_time <= session.end_time:
            logger.info(f"Market open: {session.session_name}")
            return True, 'regular_session', session.session_name

    logger.info("Outside trading hours")
    return False, 'closed', 'Regular market hours'
```

**Behavior on Market Close**:

```
┌────────────────────────────────────────────────────────────────┐
│ MARKET CLOSE WORKFLOW                                           │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  Market Close Time: 3:30 PM (from TradingHoursTemplate)       │
│  ↓                                                              │
│  Position Monitor: check_trading_hours() returns False         │
│  ↓                                                              │
│  Decision:                                                      │
│    1. KEEP WebSocket subscriptions for open positions          │
│       (user may have overnight/NRML positions)                 │
│    2. STOP subscribing to NEW positions                        │
│    3. PAUSE risk monitoring (no real-time prices available)    │
│    4. SCHEDULE re-check at next market open                    │
│  ↓                                                              │
│  Next Market Open: 9:15 AM (from TradingHoursTemplate)        │
│  ↓                                                              │
│  Position Monitor: Resumes full monitoring                     │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

**Implementation**:

```python
# app/utils/position_monitor.py (NEW)

def on_market_close(self):
    """
    Called when market closes
    Keep connections alive for overnight positions but pause new subscriptions
    """
    logger.info("Market closed - adjusting monitoring behavior")

    # Keep existing subscriptions (for overnight NRML positions)
    # but mark as "after-hours" mode
    self.after_hours_mode = True

    # Pause risk monitoring (no real-time prices after hours)
    from app.utils.risk_manager import risk_manager
    risk_manager.pause_monitoring()

    logger.info(f"After-hours mode: Monitoring {len(self.subscriptions)} overnight positions")

def on_market_open(self):
    """
    Called when market opens
    Resume full monitoring
    """
    logger.info("Market opened - resuming full monitoring")

    self.after_hours_mode = False

    # Resume risk monitoring
    from app.utils.risk_manager import risk_manager
    risk_manager.resume_monitoring()

    # Re-subscribe to any new positions that appeared overnight
    self.refresh_subscriptions()
```

### 3.2 Position Monitor Scheduled Tasks

**File**: `app/utils/position_monitor.py` (NEW)

```python
class PositionMonitor:
    def __init__(self):
        # ... existing init ...
        self.scheduler = BackgroundScheduler(timezone=pytz.timezone('Asia/Kolkata'))
        self.schedule_market_hours_checks()

    def schedule_market_hours_checks(self):
        """
        Schedule checks based on TradingHoursTemplate
        Automatically start/stop monitoring based on market hours
        """
        # Get all trading sessions from database
        sessions = TradingSession.query.join(TradingHoursTemplate).filter(
            TradingSession.is_active == True,
            TradingHoursTemplate.is_active == True
        ).all()

        for session in sessions:
            day = session.day_of_week
            start_time = session.start_time
            end_time = session.end_time

            # Schedule market open
            self.scheduler.add_job(
                func=self.on_market_open,
                trigger=CronTrigger(
                    day_of_week=day,
                    hour=start_time.hour,
                    minute=start_time.minute,
                    timezone=pytz.timezone('Asia/Kolkata')
                ),
                id=f"market_open_{day}_{session.session_name}",
                replace_existing=True
            )

            # Schedule market close
            self.scheduler.add_job(
                func=self.on_market_close,
                trigger=CronTrigger(
                    day_of_week=day,
                    hour=end_time.hour,
                    minute=end_time.minute,
                    timezone=pytz.timezone('Asia/Kolkata')
                ),
                id=f"market_close_{day}_{session.session_name}",
                replace_existing=True
            )

        self.scheduler.start()
```

---

## 4. Holiday Scenarios

### 4.1 Market Holiday (No Trading)

**Scenario**: Diwali, Holi, Independence Day, etc.

**Data Source**: `http://127.0.0.1:8000/trading/trading-hours` → Market Holidays section

**Database**:
```python
# app/models.py:470-484
class MarketHoliday(db.Model):
    holiday_date = db.Column(db.Date, nullable=False, unique=True)
    holiday_name = db.Column(db.String(200), nullable=False)
    market = db.Column(db.String(50), default='NSE')
    holiday_type = db.Column(db.String(50))  # 'trading', 'settlement', 'both'
    is_special_session = db.Column(db.Boolean, default=False)
    special_start_time = db.Column(db.Time)
    special_end_time = db.Column(db.Time)
```

**Behavior**:
```python
# app/utils/position_monitor.py (NEW)

def should_start_monitoring_today(self) -> Tuple[bool, str]:
    """
    Check if monitoring should be active today
    Returns: (should_monitor, reason)
    """
    today = datetime.now(pytz.timezone('Asia/Kolkata')).date()

    # Check holiday
    holiday = MarketHoliday.query.filter_by(
        holiday_date=today,
        is_active=True
    ).first()

    if holiday:
        if holiday.is_special_session:
            # Special session (e.g., Muhurat Trading)
            logger.info(f"Special session today: {holiday.holiday_name}")
            return True, f"special_session_{holiday.holiday_name}"
        else:
            # Regular holiday - no monitoring
            logger.info(f"Market holiday: {holiday.holiday_name}")
            return False, f"holiday_{holiday.holiday_name}"

    # Check if primary account is connected
    if not self.is_primary_account_connected():
        logger.warning("Primary account not connected")
        return False, "primary_account_disconnected"

    # Check trading hours
    is_trading, status, name = self.check_trading_hours()
    if not is_trading:
        logger.info(f"Outside trading hours: {name}")
        return False, f"outside_hours_{status}"

    return True, "active"
```

### 4.2 Special Trading Sessions (Muhurat Trading)

**Scenario**: Diwali Muhurat Trading (6:00 PM - 7:00 PM)

**Database**:
```python
# app/models.py:486-505
class SpecialTradingSession(db.Model):
    session_date = db.Column(db.Date, nullable=False)
    session_name = db.Column(db.String(200), nullable=False)  # 'Muhurat Trading'
    market = db.Column(db.String(50), default='NSE')
    start_time = db.Column(db.Time, nullable=False)  # 18:00:00
    end_time = db.Column(db.Time, nullable=False)    # 19:00:00
```

**Handling**:
```python
# Position Monitor will automatically detect special session
# from TradingHoursTemplate and activate monitoring during that window
# NO HARDCODING - all from database
```

---

## 5. Primary Account Disconnection

### 5.1 Primary Account Goes Offline During Trading

**Scenario**: Primary OpenAlgo account loses connection mid-day

**Detection**:
```python
# app/utils/position_monitor.py (NEW)

def monitor_primary_account_health(self):
    """
    Continuously monitor primary account connection
    Runs every 60 seconds
    """
    while self.is_running:
        try:
            if not self.is_primary_account_connected():
                logger.error("Primary account disconnected!")

                # Attempt failover to backup account
                self.attempt_failover()

            time.sleep(60)  # Check every minute

        except Exception as e:
            logger.error(f"Error checking primary account: {e}")
            time.sleep(60)

def is_primary_account_connected(self) -> bool:
    """Check if primary account is connected to OpenAlgo"""
    from app.models import TradingAccount

    primary = TradingAccount.query.filter_by(
        is_primary=True,
        is_active=True
    ).first()

    if not primary:
        return False

    try:
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI
        client = ExtendedOpenAlgoAPI(
            api_key=primary.get_api_key(),
            host=primary.host_url
        )

        response = client.ping()
        return response.get('status') == 'success'

    except Exception as e:
        logger.error(f"Primary account ping failed: {e}")
        return False
```

**Failover Logic**:
```
┌────────────────────────────────────────────────────────────────┐
│ PRIMARY ACCOUNT FAILOVER                                        │
├────────────────────────────────────────────────────────────────┤
│                                                                 │
│  1. Primary Account Disconnected                               │
│     ↓                                                           │
│     Log alert: "Primary account offline"                       │
│     ↓                                                           │
│  2. Get Backup Accounts (is_active=True, is_primary=False)    │
│     ↓                                                           │
│  3. For each backup account:                                   │
│     a. Try ping()                                              │
│     b. If success → Use as primary                             │
│     c. If fail → Try next                                      │
│     ↓                                                           │
│  4a. Backup Connected                                          │
│      ↓                                                          │
│      Reconnect WebSocket with backup credentials               │
│      Resubscribe to all positions                              │
│      Resume monitoring                                         │
│      Log: "Failover successful to backup account X"            │
│                                                                 │
│  4b. No Backup Available                                       │
│      ↓                                                          │
│      Stop WebSocket subscriptions                              │
│      Pause monitoring                                          │
│      Log critical alert: "No accounts available"               │
│      Retry primary every 5 minutes                             │
│                                                                 │
└────────────────────────────────────────────────────────────────┘
```

---

## 6. Configuration Summary

### Environment Variables

```bash
# .env

# Position Monitor
POSITION_MONITOR_ENABLED=true
POSITION_MONITOR_CHECK_PRIMARY_ACCOUNT=true
POSITION_MONITOR_PRIMARY_CHECK_INTERVAL=60  # seconds
POSITION_MONITOR_FAILOVER_ENABLED=true

# Monitor pending limit orders?
MONITOR_PENDING_LIMIT_ORDERS=true  # Subscribe to symbols with pending orders

# Market Hours (Always from database - no hardcoding)
USE_TRADING_HOURS_TEMPLATE=true  # ALWAYS true
MARKET_HOURS_CHECK_INTERVAL=300  # Check every 5 minutes

# After-Hours Behavior
KEEP_SUBSCRIPTIONS_AFTER_HOURS=true  # For overnight NRML positions
PAUSE_RISK_MONITORING_AFTER_HOURS=true

# Order Status Poller
ORDER_STATUS_POLL_INTERVAL=2  # seconds
ORDER_STATUS_TIMEOUT=3600  # Remove from queue after 1 hour
```

---

## 7. State Transition Matrix

### Complete Order Lifecycle

| Current State | Event | New State | WebSocket Action | Risk Monitor |
|---------------|-------|-----------|------------------|--------------|
| **N/A** | User places LIMIT order | `pending` / `open` | ✅ Subscribe | 🟡 Monitor (potential) |
| **pending** | Order fills | `entered` / `complete` | ✅ Keep subscribed | ✅ Monitor P&L |
| **pending** | Order cancelled | `error` / `cancelled` | ❌ Unsubscribe | ❌ Stop monitoring |
| **pending** | Order rejected | `error` / `rejected` | ❌ Unsubscribe | ❌ Stop monitoring |
| **entered** | User exits | `exited` / `complete` | ❌ Unsubscribe* | ❌ Stop monitoring |
| **entered** | Max Loss hit | `exited` / `complete` | ❌ Unsubscribe* | ✅ Log event |
| **entered** | Max Profit hit | `exited` / `complete` | ❌ Unsubscribe* | ✅ Log event |
| **entered** | Market closes | `entered` / `complete` | ✅ Keep subscribed** | ⏸️ Pause |
| **ANY** | Holiday | ANY | ⏸️ Pause new subs | ⏸️ Pause |
| **ANY** | Primary disconnects | ANY | 🔄 Failover | ⏸️ Pause until failover |

*Unsubscribe only if no other positions for same symbol
**Keep for NRML overnight positions

---

## 8. Testing Checklist

### Unit Tests
- [ ] Limit order placed → WebSocket subscribes
- [ ] Limit order fills → Status changes to 'entered'
- [ ] Limit order cancelled → WebSocket unsubscribes
- [ ] Market closes → After-hours mode activated
- [ ] Market opens → Full monitoring resumed
- [ ] Holiday detected → No monitoring
- [ ] Special session → Monitoring active during hours
- [ ] Primary account disconnects → Failover triggered

### Integration Tests
- [ ] Place limit order, wait 30s, cancel → Verify cleanup
- [ ] Place limit order at market close → Verify no monitoring
- [ ] Place limit order on holiday → Verify rejection
- [ ] Multiple positions same symbol → Verify single subscription
- [ ] Last position closed → Verify unsubscribe
- [ ] Primary account offline → Verify backup used

### Load Tests
- [ ] 100 pending limit orders → All monitored
- [ ] 50 orders fill simultaneously → All subscribed correctly
- [ ] Market close with 100 open positions → Clean transition
- [ ] 10 users, 20 positions each → Performance acceptable

---

## 9. Monitoring & Alerts

### Dashboard Metrics

Add to admin dashboard:

```
Current Status:
├─ Market Status: OPEN / CLOSED / HOLIDAY / SPECIAL SESSION
├─ Primary Account: CONNECTED / DISCONNECTED
├─ Backup Accounts: 2 available
├─ Active Subscriptions: 23
│  ├─ Open Positions: 15
│  └─ Pending Orders: 8
├─ Risk Monitoring: ACTIVE / PAUSED
└─ After-Hours Mode: YES / NO

Recent Events:
├─ 15:30 - Market closed, switched to after-hours mode
├─ 15:25 - Max Loss triggered for Strategy #5
├─ 14:30 - Limit order filled: NIFTY24DEC25250CE
└─ 09:15 - Market opened, resumed monitoring
```

### Alert Conditions

```python
# Send alerts (email/SMS if configured) when:
1. Primary account disconnects
2. Failover occurs
3. Market closes with open positions
4. Risk threshold breached
5. Order cancelled/rejected (if configured)
```

---

## 10. Summary

### Key Principles

1. **NO HARDCODING**: All market hours from `TradingHoursTemplate` database
2. **Smart Subscriptions**: Monitor pending limit orders until filled/cancelled
3. **Graceful Degradation**: Keep connections alive for overnight positions
4. **Automatic Failover**: Switch to backup if primary disconnects
5. **Holiday Aware**: Respect market holidays and special sessions
6. **Resource Efficient**: Unsubscribe when positions close

### Files Modified/Created

**New Files**:
- `app/utils/position_monitor.py` - Main position monitoring service
- `EDGE_CASES_HANDLING.md` - This document

**Modified Files**:
- `app/utils/order_status_poller.py` - Add WebSocket integration
- `app/utils/risk_manager.py` - Add pause/resume for after-hours

**Database**: No additional migrations needed (uses existing TradingHoursTemplate)

---

## Contact

For questions about edge case handling:
- Technical Implementation: See `WEBSOCKET_OPTIMIZATION_PRD.md`
- Main Summary: See `IMPLEMENTATION_SUMMARY.md`

**Status**: ✅ Edge Cases Documented & Handled
