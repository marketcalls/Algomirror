"""
Test WebSocket Connection to OpenAlgo
Tests the exact protocol that AlgoMirror uses
"""
import json
import websocket
import time
import threading

# Configuration
WS_URL = "ws://127.0.0.1:8765"
API_KEY = "your_api_key_here"  # Replace with actual API key

authenticated = False
messages_received = []

def on_open(ws):
    """WebSocket opened callback"""
    print("[TEST] ✅ WebSocket connection opened")

    # Send authentication message (OpenAlgo style)
    auth_msg = {
        "action": "authenticate",
        "api_key": API_KEY
    }
    print(f"[TEST] 📤 Sending authentication: {json.dumps(auth_msg, indent=2)}")
    ws.send(json.dumps(auth_msg))

def on_message(ws, message):
    """Handle incoming WebSocket messages"""
    global authenticated, messages_received

    try:
        data = json.loads(message)
        print(f"\n[TEST] 📥 Received message: {json.dumps(data, indent=2)}")
        messages_received.append(data)

        # Handle authentication response
        if data.get("type") == "auth":
            if data.get("status") == "success":
                authenticated = True
                print("[TEST] ✅ Authentication successful!")

                # Now test subscription
                test_subscription(ws)
            else:
                print(f"[TEST] ❌ Authentication failed: {data}")
                return

        # Handle subscription response
        if data.get("type") == "subscribe":
            print(f"[TEST] 📊 Subscription response: status={data.get('status')}, message={data.get('message')}")

        # Handle market data
        if data.get("type") == "market_data":
            print(f"[TEST] 💹 Market data received for {data.get('symbol')}")

    except json.JSONDecodeError as e:
        print(f"[TEST] ❌ Invalid JSON: {message[:100]}...")
    except Exception as e:
        print(f"[TEST] ❌ Error processing message: {e}")

def on_error(ws, error):
    """WebSocket error callback"""
    print(f"[TEST] ❌ WebSocket error: {error}")

def on_close(ws, close_status_code, close_msg):
    """WebSocket closed callback"""
    print(f"[TEST] 🔌 WebSocket closed - Code: {close_status_code}, Message: {close_msg}")

def test_subscription(ws):
    """Test subscription with OpenAlgo format"""
    print("\n[TEST] 🧪 Testing subscription format...")

    # Test 1: Subscribe with instruments array (current AlgoMirror format)
    subscription_msg = {
        'action': 'subscribe',
        'mode': 'ltp',
        'instruments': [
            {
                'exchange': 'NSE',
                'symbol': 'RELIANCE'
            }
        ]
    }

    print(f"[TEST] 📤 Sending subscription: {json.dumps(subscription_msg, indent=2)}")
    ws.send(json.dumps(subscription_msg))

    # Wait a bit for response
    time.sleep(2)

    # Test 2: Try option chain symbol
    option_subscription = {
        'action': 'subscribe',
        'mode': 'depth',
        'instruments': [
            {
                'exchange': 'NFO',
                'symbol': 'NIFTY11NOV2525500CE'
            }
        ]
    }

    print(f"[TEST] 📤 Sending option subscription: {json.dumps(option_subscription, indent=2)}")
    ws.send(json.dumps(option_subscription))

def main():
    """Main test function"""
    print("=" * 60)
    print("WebSocket Connection Test for OpenAlgo")
    print("=" * 60)
    print(f"WS URL: {WS_URL}")
    print(f"API Key: {API_KEY[:8]}...{API_KEY[-8:]}")
    print("=" * 60)

    # Create WebSocket connection
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # Run WebSocket in separate thread
    ws_thread = threading.Thread(target=ws.run_forever)
    ws_thread.daemon = True
    ws_thread.start()

    # Wait for messages
    print("\n[TEST] ⏳ Running test for 10 seconds...")
    time.sleep(10)

    # Close connection
    print("\n[TEST] 🛑 Closing WebSocket connection...")
    ws.close()

    # Summary
    print("\n" + "=" * 60)
    print("Test Summary")
    print("=" * 60)
    print(f"✅ Authenticated: {authenticated}")
    print(f"📊 Total messages received: {len(messages_received)}")

    if messages_received:
        print("\n📋 Message types received:")
        for msg in messages_received:
            print(f"  - {msg.get('type', 'unknown')}: {msg.get('status', 'N/A')}")

    print("=" * 60)

if __name__ == "__main__":
    # Check if API key is set
    if API_KEY == "your_api_key_here":
        print("❌ ERROR: Please set your OpenAlgo API key in the script")
        print("   Edit API_KEY variable at the top of this file")
        exit(1)

    main()
