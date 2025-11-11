# OpenAlgo Angel Adapter Fix Required

## Problem

OpenAlgo is creating **dozens of Angel broker adapter instances** instead of reusing a single shared adapter per user. This causes:

- Error 429 "Connection Limit Exceeded" from AngelOne broker
- Cascade of failed connection attempts
- WebSocket connections repeatedly failing
- Option chains not updating

## Evidence from Logs

```
[2025-11-11 09:45:49,179] INFO in broker_factory: Creating adapter for broker: angel
[2025-11-11 09:45:49,185] INFO in angel_adapter: Connecting to Angel WebSocket (attempt 1)
[2025-11-11 09:46:39,625] INFO in angel_adapter: Connecting to Angel WebSocket (attempt 1)
[2025-11-11 09:46:39,626] INFO in angel_adapter: Connecting to Angel WebSocket (attempt 1)
[2025-11-11 09:46:39,627] INFO in angel_adapter: Connecting to Angel WebSocket (attempt 1)
... [repeated 50+ times in 1 second]
```

Each adapter instance tries to connect to AngelOne, exceeding the broker's connection limit (1-2 connections per API key).

## Root Cause

OpenAlgo's broker adapter management is creating new adapters instead of reusing existing ones. This happens when:

1. **New WebSocket connection** from client → creates new adapter
2. **Subscription request** → may create new adapter
3. **Reconnection attempt** → creates new adapter
4. **Failed connection retry** → creates new adapter

## Expected Behavior

**One user = One Angel adapter**, reused for:
- All WebSocket connections from that user
- All subscription requests
- All market data streams
- Connection lifecycle (connect, disconnect, reconnect)

## Required Fix in OpenAlgo

### 1. Singleton Adapter per User

**File**: `broker_factory.py` or equivalent

```python
class BrokerAdapterManager:
    """Manages broker adapters - one per user per broker"""

    _user_adapters = {}  # {(user_id, broker): adapter_instance}
    _lock = threading.Lock()

    @classmethod
    def get_or_create_adapter(cls, user_id: str, broker: str, credentials: dict):
        """Get existing adapter or create new one - SINGLETON per user"""
        key = (user_id, broker)

        with cls._lock:
            # Return existing adapter if available and connected
            if key in cls._user_adapters:
                adapter = cls._user_adapters[key]
                if adapter.is_connected():
                    logger.info(f"Reusing existing {broker} adapter for user {user_id}")
                    return adapter
                else:
                    # Adapter exists but disconnected - remove it
                    logger.info(f"Removing disconnected {broker} adapter for user {user_id}")
                    del cls._user_adapters[key]

            # Create new adapter
            logger.info(f"Creating NEW {broker} adapter for user {user_id}")
            adapter = cls._create_adapter(broker, credentials)
            cls._user_adapters[key] = adapter
            return adapter

    @classmethod
    def _create_adapter(cls, broker: str, credentials: dict):
        """Create broker-specific adapter"""
        if broker == 'angel':
            from adapters.angel_adapter import AngelAdapter
            return AngelAdapter(**credentials)
        # ... other brokers

    @classmethod
    def remove_adapter(cls, user_id: str, broker: str):
        """Remove adapter when user disconnects"""
        key = (user_id, broker)
        with cls._lock:
            if key in cls._user_adapters:
                adapter = cls._user_adapters[key]
                adapter.disconnect()
                del cls._user_adapters[key]
                logger.info(f"Removed {broker} adapter for user {user_id}")
```

### 2. WebSocket Server Changes

**File**: `websocket_server.py` or equivalent

```python
class WebSocketHandler:
    def on_connect(self, websocket, user_id, broker, credentials):
        """Client connects - get or create adapter"""
        # DO NOT create new adapter on every connection
        # Use singleton manager
        self.adapter = BrokerAdapterManager.get_or_create_adapter(
            user_id=user_id,
            broker=broker,
            credentials=credentials
        )

        logger.info(f"WebSocket connected for user {user_id}, using shared adapter")

    def on_subscribe(self, symbols):
        """Subscribe to symbols - reuse existing adapter"""
        # Use the SAME adapter obtained in on_connect
        self.adapter.subscribe(symbols)

    def on_disconnect(self):
        """Client disconnects - DO NOT remove adapter"""
        # Keep adapter alive for other connections from same user
        # Only remove when user explicitly logs out or session expires
        logger.info("WebSocket disconnected, adapter remains active for other connections")
```

### 3. Connection Lifecycle Management

```python
class AngelAdapter:
    def __init__(self, api_key, client_id, ...):
        self.api_key = api_key
        self.ws = None
        self.is_connected_flag = False
        self.reconnect_attempts = 0
        self.max_reconnect_attempts = 5  # LIMIT reconnection attempts

    def connect(self):
        """Connect to Angel WebSocket - with retry limit"""
        if self.is_connected_flag:
            logger.info("Already connected, not reconnecting")
            return True

        if self.reconnect_attempts >= self.max_reconnect_attempts:
            logger.error(f"Max reconnection attempts ({self.max_reconnect_attempts}) reached")
            raise ConnectionError("Failed to connect to Angel after max retries")

        try:
            self.ws = SmartWebSocket(...)
            self.ws.connect()
            self.is_connected_flag = True
            self.reconnect_attempts = 0
            logger.info("Connected to Angel WebSocket")
            return True
        except Exception as e:
            self.reconnect_attempts += 1
            logger.error(f"Connection failed (attempt {self.reconnect_attempts}): {e}")
            raise

    def is_connected(self):
        """Check if adapter is connected"""
        return self.is_connected_flag and self.ws is not None

    def disconnect(self):
        """Explicitly disconnect"""
        if self.ws:
            self.ws.close()
        self.is_connected_flag = False
        logger.info("Disconnected from Angel WebSocket")
```

## Testing the Fix

### 1. Verify Single Adapter

Check OpenAlgo logs after fix:
```
[INFO] Creating NEW angel adapter for user rajandran
[INFO] Reusing existing angel adapter for user rajandran
[INFO] Reusing existing angel adapter for user rajandran
... (should be "Reusing" not "Creating NEW")
```

### 2. Count Broker Connections

Only **1 active connection** to AngelOne at any time:
```bash
# In OpenAlgo code, add logging
logger.info(f"Active Angel adapters: {len(BrokerAdapterManager._user_adapters)}")
# Should always be 1 per user
```

### 3. No Error 429

After fix, Error 429 should NOT appear in logs.

## AlgoMirror Side (Already Fixed)

AlgoMirror now uses **single shared WebSocket connection** to OpenAlgo:
- ✅ Position monitor uses shared connection
- ✅ Session manager uses shared connection
- ✅ Option chains use shared connection

This reduces triggers for OpenAlgo to create adapters, but **OpenAlgo still needs the fix above** to prevent creating multiple adapters per connection.

## Deployment

1. **Apply fix to OpenAlgo** (adapter singleton management)
2. **Restart OpenAlgo server**
3. **Restart AlgoMirror** (already has single connection fix)
4. **Test with single user connection**
5. **Monitor logs for "Reusing existing adapter" messages**
6. **Verify no Error 429 from broker**

## Priority

**CRITICAL** - This bug prevents all real-time data streaming for users with connection-limited brokers (AngelOne, and likely others).

---

**Date**: 2025-11-11
**Status**: ⚠️ Fix required in OpenAlgo server
**AlgoMirror Status**: ✅ Already fixed (single shared WebSocket)
