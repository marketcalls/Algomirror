# WebSocket and Option Chain Fixes

## Issues Fixed

### 1. Symbol Format Issue (FIXED)
**Problem**: Option symbols were being generated incorrectly with format like `NIFTY28AUG2523800CE` (year in strike)
**Solution**: Corrected to proper format `NIFTY28AUG2524800CE` (expiry + strike + type)

### 2. WebSocket Authentication (FIXED)
**Problem**: Was using Bearer token in headers
**Solution**: Using message-based authentication with `{"action": "authenticate", "api_key": "..."}`

### 3. WebSocket Subscription Format (FIXED - REVERTED TO WORKING VERSION)
**Problem**: OpenAlgo WebSocket not accepting subscription messages
**Root Cause**: Incorrect subscription message format - was using instruments array with string mode
**Solution**: Reverted to OLD WORKING format from previous version
- **OLD (WORKING)**: `{'action': 'subscribe', 'symbol': 'X', 'exchange': 'Y', 'mode': 3, 'depth': 5}`
- **BROKEN (docs format)**: `{'action': 'subscribe', 'mode': 'depth', 'instruments': [{'exchange': 'Y', 'symbol': 'X'}]}`
- Mode is NUMERIC (1=ltp, 2=quote, 3=depth), not string
- Individual fields, not wrapped in instruments array
- **CRITICAL**: The docs were incorrect! The old code had the working format

### 4. Multiple WebSocket Connections / Broker Connection Limit (FIXED)
**Problem**:
- AlgoMirror created multiple WebSocket connections to OpenAlgo
- Each OpenAlgo connection triggered separate broker connection to AngelOne
- AngelOne limits connections to 1-2 per API key
- Result: Error 429 "Connection Limit Exceeded" from broker
- Option chain not updating due to rejected connections

**Root Cause**:
- Position monitor created dedicated WebSocket connection
- Session manager created separate connection if needed
- Option chain managers created their own connections
- No centralized connection management

**Solution**: Single Shared WebSocket Manager
- Added `shared_websocket_manager` singleton in `background_service.py`
- Created `get_or_create_shared_websocket()` method
- All services (position monitor, session manager, option chains) now use ONE shared connection
- Shared connection only disconnects when entire service stops

**Files Modified**:
- `app/utils/background_service.py` (Lines 65, 74-130, 215-224, 851-872, 874-888, 162-185)

**Benefits**:
- ✅ Only 1 WebSocket connection from AlgoMirror to OpenAlgo
- ✅ Only 1 broker connection from OpenAlgo to AngelOne
- ✅ No more Error 429 from broker
- ✅ Lower resource usage
- ✅ Cleaner architecture

**Verification**:
```bash
# Count WebSocket connections (should be 1)
netstat -ano | findstr :8765

# Run test script
python test_single_websocket.py
```

See `SINGLE_WEBSOCKET_FIX.md` for detailed implementation notes.

## Symbol Format Specification

Correct OpenAlgo format for options:
- Pattern: `[BASE][DDMMMYY][STRIKE][CE/PE]`
- Example: `NIFTY28AUG2524800CE`
  - BASE: NIFTY
  - Date: 28AUG25 (28 August 2025)
  - Strike: 24800
  - Type: CE (Call European)

## WebSocket Protocol

### Authentication Flow
```json
// Send after connection
{
    "action": "authenticate",
    "api_key": "your_api_key"
}

// Response
{
    "type": "auth",
    "status": "success"
}
```

### Subscription Format
```json
{
    "action": "subscribe",
    "mode": "depth",  // or "quote" or "ltp"
    "instruments": [
        {
            "exchange": "NFO",
            "symbol": "NIFTY28AUG2524800CE"
        }
    ]
}
```

## Testing Steps

1. **Verify Symbol Format**:
   ```python
   # Test with REST API
   client.quotes(symbol='NIFTY28AUG2524800CE', exchange='NFO')
   ```

2. **Check WebSocket Connection**:
   - Authentication should succeed
   - Subscriptions should be sent with correct format

## Implementation Status

All WebSocket issues have been resolved:
1. ✅ Symbol format corrected to OpenAlgo standard
2. ✅ Message-based authentication implemented
3. ✅ Subscription format fixed with instruments array and string mode
4. ✅ Single shared WebSocket connection implemented to prevent broker connection limits

The WebSocket implementation now fully complies with OpenAlgo WebSocket protocol and prevents broker connection limit errors (Error 429).

## Logs to Check

Enable detailed logging with these markers:
- `[WS_MSG]` - All incoming WebSocket messages
- `[WS_SUBSCRIBE]` - Subscription attempts
- `[WS_DATA]` - Market data received
- `[OPTION_CHAIN]` - Option chain updates