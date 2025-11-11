# Single Shared WebSocket Connection Fix

## Problem
AlgoMirror was creating multiple WebSocket connections to OpenAlgo, causing:
1. **Error 429 from AngelOne broker**: "Connection Limit Exceeded"
2. **Broker rejects connections**: AngelOne limits WebSocket connections to 1-2 per API key
3. **Resource waste**: Multiple connections consuming unnecessary resources

## Root Cause
Three services were each trying to create their own WebSocket connections:
- **Position Monitor**: Created dedicated connection in `start_position_monitor()`
- **Session Manager**: Used shared connection IF available, else created new one
- **Option Chain Manager**: Created connections per underlying symbol

When multiple connections opened, each triggered OpenAlgo to create a separate broker connection to AngelOne, exceeding the broker's connection limit.

## Solution: Single Shared WebSocket Manager

### Architecture Changes

**File: `app/utils/background_service.py`**

1. **Added Shared WebSocket Manager** (Line 65):
   ```python
   self.shared_websocket_manager = None  # Single shared WebSocket for all services
   ```

2. **Created Centralized Connection Method** (Lines 74-130):
   ```python
   def get_or_create_shared_websocket(self):
       """
       Get or create the single shared WebSocket manager for all services.
       Ensures only ONE WebSocket connection to OpenAlgo.
       """
   ```
   - Returns existing connection if authenticated
   - Creates new connection only if needed
   - Stores as singleton `self.shared_websocket_manager`

3. **Updated Position Monitor** (Lines 851-872):
   - **Before**: Created dedicated WebSocket in `start_position_monitor()`
   - **After**: Uses `get_or_create_shared_websocket()`
   - Logs: "✅ Position monitor started with shared WebSocket connection"

4. **Updated Session Manager Initialization** (Lines 218-224):
   - **Before**: Tried to find WebSocket in `websocket_managers` dict
   - **After**: Uses `self.shared_websocket_manager` directly
   - Logs: "✅ SessionManager initialized with shared WebSocket connection"

5. **Updated Stop Methods**:
   - `stop_position_monitor()`: Does NOT disconnect WebSocket (other services using it)
   - `stop_service()`: Disconnects shared WebSocket when entire service stops

### Benefits

1. **One Connection Only**: Position monitor, session manager, and option chains all share the same WebSocket
2. **Prevents Broker Limits**: Only one OpenAlgo-to-broker connection
3. **Resource Efficient**: Lower memory and network usage
4. **Cleaner Architecture**: Centralized connection management

### Verification

To verify single connection:
1. Start OpenAlgo server (both REST API and WebSocket)
2. Start AlgoMirror
3. Check logs for "✅ Shared WebSocket manager created and authenticated"
4. Run: `netstat -ano | findstr :8765` (should show 1 connection from AlgoMirror)
5. Open option chain page - should NOT create new connection
6. Check OpenAlgo logs - should show 1 broker connection to AngelOne

### Testing

Use the test script `test_single_websocket.py` to verify:
```bash
python test_single_websocket.py
```

## Current Status

**IMPORTANT**: OpenAlgo server is currently NOT running!
- Error: `[WinError 10061] No connection could be made`
- Need to start OpenAlgo before testing the fix

**Steps to Test**:
1. Start OpenAlgo server:
   - REST API on port 5000
   - WebSocket on port 8765
2. Start AlgoMirror: `.venv\Scripts\python.exe wsgi.py`
3. Monitor logs for shared connection messages
4. Visit option chain page: http://127.0.0.1:8000/trading/option-chain
5. Verify no Error 429 from broker

## Files Modified

1. `app/utils/background_service.py`:
   - Added `shared_websocket_manager` field
   - Added `get_or_create_shared_websocket()` method
   - Modified `start_position_monitor()` to use shared connection
   - Modified `stop_position_monitor()` to preserve shared connection
   - Modified `stop_service()` to disconnect shared connection
   - Updated SessionManager initialization logic

Total changes: 80 lines modified across 5 methods

## Migration Notes

**Breaking Changes**: None - backward compatible

**Deployment**:
1. Update code
2. Restart AlgoMirror
3. Verify single WebSocket connection in logs
4. Monitor for Error 429 (should not occur)

**Rollback**: Revert changes to `app/utils/background_service.py`

## Related Documentation

- `docs/websocket-fixes.md` - WebSocket protocol compliance fixes
- `docs/openalgo.md` - OpenAlgo WebSocket format specification
- `WEBSOCKET_OPTIMIZATION_PRD.md` - Full optimization PRD

---

**Date**: 2025-11-11
**Status**: ✅ Implemented, awaiting OpenAlgo server restart for testing
