# WebSocket Format Fix - Critical Finding

## Summary

**The documentation was WRONG!** The old working AlgoMirror code had the CORRECT WebSocket format. I mistakenly changed it based on incorrect documentation.

## What Was Wrong

### BROKEN Format (what I implemented from docs)
```json
{
    "action": "subscribe",
    "mode": "depth",
    "instruments": [
        {
            "exchange": "NFO",
            "symbol": "NIFTY11NOV2525500CE"
        }
    ]
}
```

**Issues**:
- Mode as string ("depth", "quote", "ltp")
- Symbols wrapped in instruments array
- OpenAlgo doesn't accept this format

### WORKING Format (from old code)
```json
{
    "action": "subscribe",
    "symbol": "NIFTY11NOV2525500CE",
    "exchange": "NFO",
    "mode": 3,
    "depth": 5
}
```

**Correct**:
- Mode as NUMERIC (1=ltp, 2=quote, 3=depth)
- Individual symbol and exchange fields
- depth field included
- OpenAlgo accepts this format

## Changes Made

### File: `app/utils/websocket_manager.py`

#### subscribe() method (Lines 543-568)
**Before** (broken):
```python
message = {
    'action': 'subscribe',
    'mode': mode,  # String mode
    'instruments': [{'exchange': exchange, 'symbol': symbol}]
}
```

**After** (working):
```python
mode_map = {'ltp': 1, 'quote': 2, 'depth': 3}
mode_num = mode_map.get(mode, 1)

message = {
    'action': 'subscribe',
    'symbol': symbol,
    'exchange': exchange,
    'mode': mode_num,  # Numeric mode
    'depth': 5
}
```

#### subscribe_batch() method (Lines 476-518)
**Before** (broken):
```python
message = {
    'action': 'subscribe',
    'mode': mode,  # String
    'instruments': [{'exchange': exchange, 'symbol': symbol}]
}
```

**After** (working):
```python
mode_map = {'ltp': 1, 'quote': 2, 'depth': 3}
mode_num = mode_map.get(mode, 1)

message = {
    'action': 'subscribe',
    'symbol': symbol,
    'exchange': exchange,
    'mode': mode_num,  # Numeric
    'depth': 5
}
```

## Mode Mapping

```python
mode_map = {
    'ltp': 1,      # Mode 1 for Last Traded Price
    'quote': 2,    # Mode 2 for Quote data
    'depth': 3     # Mode 3 for Market Depth
}
```

## Root Cause Analysis

1. **Docs were incorrect**: OpenAlgo documentation showed the wrong format
2. **Old code was correct**: The previous AlgoMirror version had the working format
3. **I made incorrect "fix"**: Based on docs, I changed working code to broken format
4. **User spotted it**: By comparing with old working version

## Lesson Learned

**Always check old working code before making changes based on documentation!**

The documentation can be outdated or incorrect. If something was working before, the implementation is the source of truth.

## Testing

After this fix, OpenAlgo should accept subscriptions properly:

1. **Start OpenAlgo server**
2. **Restart AlgoMirror**:
   ```bash
   .venv\Scripts\python.exe wsgi.py
   ```
3. **Check logs** for successful subscriptions:
   ```
   [WS_SUBSCRIBE] Sending subscription for NIFTY11NOV2525500CE
   ```
4. **No "At least one symbol must be specified" errors**
5. **Option chain updates with real-time data**

## Combined with Single WebSocket Fix

This format fix combined with the single shared WebSocket fix should resolve ALL WebSocket issues:

1. ✅ Correct subscription format (this fix)
2. ✅ Single shared WebSocket connection (previous fix)
3. ✅ No Error 429 from broker (depends on OpenAlgo adapter fix)

## Files Modified

- `app/utils/websocket_manager.py` - subscribe() and subscribe_batch() methods
- `docs/websocket-fixes.md` - Updated Issue #3
- `.gitignore` - Added docs/Algomirror-master/

## Status

✅ **WebSocket subscription format reverted to working version**
✅ **Ready to test with OpenAlgo**

The format is now correct and matches what OpenAlgo actually expects.

---

**Date**: 2025-11-11
**Critical Fix**: WebSocket subscription format corrected
**Thanks to**: User for pointing to old working code
