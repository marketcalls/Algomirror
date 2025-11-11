"""
Test Option Chain WebSocket Connection
Tests real-time data streaming for NIFTY 11NOV25 option chain
"""
import websocket
import json
import time
import threading
import requests
from datetime import datetime

# Configuration
API_KEY = "b857b2270508b4715bb31dc0d541ab36f9582cbf55f22a11e4c1cf1717def5ad"
REST_API_URL = "http://127.0.0.1:5000"
WS_URL = "ws://127.0.0.1:8765"

# Test settings
UNDERLYING = "NIFTY"
EXPIRY = "11NOV25"
NUM_STRIKES = 5  # Number of strikes above and below ATM

# Test state
test_results = {
    'connection': False,
    'authentication': False,
    'subscriptions_sent': 0,
    'data_received': 0,
    'symbols_tested': [],
    'errors': []
}

data_lock = threading.Lock()
received_data = {}
auth_event = threading.Event()  # Event for authentication synchronization


def format_symbol(base, expiry, strike, option_type):
    """Format option symbol in OpenAlgo format"""
    # NIFTY11NOV2525500CE
    return f"{base}{expiry}{strike}{option_type}"


def get_spot_price():
    """Get current NIFTY spot price"""
    try:
        response = requests.post(
            f"{REST_API_URL}/api/v1/quotes",
            headers={"Content-Type": "application/json"},
            json={
                "apikey": API_KEY,
                "symbol": UNDERLYING,
                "exchange": "NSE"
            },
            timeout=10
        )

        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                ltp = float(data['data'].get('ltp', 0))
                print(f"[OK] Current {UNDERLYING} spot: {ltp}")
                return ltp

        print(f"[WARN] Could not fetch spot price, using default 25000")
        return 25000
    except Exception as e:
        print(f"[ERROR] Error fetching spot price: {e}")
        return 25000


def generate_option_symbols(spot_price, num_strikes=5):
    """Generate option symbols around ATM"""
    # Round to nearest 50 for NIFTY
    atm_strike = round(spot_price / 50) * 50

    symbols = []

    # Generate strikes around ATM
    for i in range(-num_strikes, num_strikes + 1):
        strike = int(atm_strike + (i * 50))

        # CE and PE for each strike
        ce_symbol = format_symbol(UNDERLYING, EXPIRY, strike, "CE")
        pe_symbol = format_symbol(UNDERLYING, EXPIRY, strike, "PE")

        symbols.append({
            'symbol': ce_symbol,
            'exchange': 'NFO',
            'strike': strike,
            'type': 'CE'
        })
        symbols.append({
            'symbol': pe_symbol,
            'exchange': 'NFO',
            'strike': strike,
            'type': 'PE'
        })

    # Add underlying
    symbols.append({
        'symbol': UNDERLYING,
        'exchange': 'NSE',
        'strike': 0,
        'type': 'SPOT'
    })

    return symbols, atm_strike


def subscribe_to_options(ws):
    """Subscribe to option chain after authentication"""
    # Get spot price and generate symbols
    print(f"\n[SPOT] Fetching {UNDERLYING} spot price...")
    spot_price = get_spot_price()

    print(f"\n[CHAIN] Generating option chain symbols for {EXPIRY}...")
    symbols, atm_strike = generate_option_symbols(spot_price, NUM_STRIKES)

    print(f"[ATM] ATM Strike: {atm_strike}")
    print(f"[INFO] Subscribing to {len(symbols)} symbols...\n")

    # Subscribe to each symbol
    for sym in symbols:
        subscription = {
            "action": "subscribe",
            "symbol": sym['symbol'],
            "exchange": sym['exchange'],
            "mode": 3,  # Depth mode for option chain
            "depth": 5
        }

        ws.send(json.dumps(subscription))
        test_results['subscriptions_sent'] += 1
        test_results['symbols_tested'].append(sym['symbol'])

        print(f"[SUB] Subscribed: {sym['symbol']} ({sym['type']})")

        # Small delay to avoid overwhelming server
        time.sleep(0.05)

    print(f"\n[OK] Sent {test_results['subscriptions_sent']} subscriptions")
    print("[WAIT] Waiting for data... (will run for 30 seconds)\n")


def on_message(ws, message):
    """Handle incoming WebSocket messages"""
    try:
        data = json.loads(message)
        msg_type = data.get('type')

        if msg_type == 'auth':
            # Authentication response
            if data.get('status') == 'success':
                test_results['authentication'] = True
                auth_event.set()  # Signal that authentication is complete
                print("[OK] WebSocket authenticated successfully")

                # Now subscribe to options
                subscribe_to_options(ws)
            else:
                test_results['errors'].append("Authentication failed")
                auth_event.set()  # Also signal on failure so we don't wait forever
                print(f"[ERROR] Authentication failed: {data}")

        elif msg_type == 'data':
            # Market data received
            symbol = data.get('symbol', 'UNKNOWN')
            ltp = data.get('ltp', 0)

            with data_lock:
                test_results['data_received'] += 1
                received_data[symbol] = data

            print(f"[DATA] {symbol} | LTP: {ltp} | Mode: {data.get('mode', 'unknown')}")

        else:
            # Other message types
            print(f"[MSG] {message}")

    except json.JSONDecodeError:
        print(f"[WARN] Non-JSON message: {message}")
    except Exception as e:
        print(f"[ERROR] Error processing message: {e}")


def on_error(ws, error):
    """Handle WebSocket errors"""
    test_results['errors'].append(str(error))
    print(f"[ERROR] WebSocket error: {error}")


def on_close(ws, close_status_code, close_msg):
    """Handle WebSocket close"""
    print(f"[CLOSE] WebSocket closed - Code: {close_status_code}, Message: {close_msg}")


def on_open(ws):
    """Handle WebSocket connection open"""
    test_results['connection'] = True
    print("[OK] WebSocket connected")

    # Send authentication
    auth_message = {
        "action": "authenticate",
        "api_key": API_KEY
    }

    print(f"[AUTH] Sending authentication...")
    ws.send(json.dumps(auth_message))
    print("[WAIT] Waiting for authentication response...")


def run_test():
    """Run the WebSocket test"""
    print("=" * 80)
    print("Option Chain WebSocket Test")
    print("=" * 80)
    print(f"Target: {UNDERLYING} {EXPIRY}")
    print(f"OpenAlgo REST API: {REST_API_URL}")
    print(f"OpenAlgo WebSocket: {WS_URL}")
    print(f"API Key: {API_KEY[:20]}...")
    print("=" * 80)
    print()

    # Test REST API connectivity first
    print("[TEST] Testing REST API connectivity...")
    try:
        response = requests.post(
            f"{REST_API_URL}/api/v1/ping",
            headers={"Content-Type": "application/json"},
            json={"apikey": API_KEY},
            timeout=5
        )

        if response.status_code == 200:
            data = response.json()
            if data.get('status') == 'success':
                broker = data.get('data', {}).get('broker', 'unknown')
                print(f"[OK] REST API working - Broker: {broker}")
            else:
                print(f"[WARN] REST API responded but status not success: {data}")
        else:
            print(f"[ERROR] REST API error - Status: {response.status_code}")
            return False
    except Exception as e:
        print(f"[ERROR] Cannot connect to REST API: {e}")
        return False

    print()
    print("[CONNECT] Connecting to WebSocket...")
    print()

    # Create WebSocket connection
    ws = websocket.WebSocketApp(
        WS_URL,
        on_open=on_open,
        on_message=on_message,
        on_error=on_error,
        on_close=on_close
    )

    # Run WebSocket in background thread
    ws_thread = threading.Thread(target=ws.run_forever)
    ws_thread.daemon = True
    ws_thread.start()

    # Wait for test to complete (30 seconds)
    time.sleep(30)

    # Close WebSocket
    ws.close()
    time.sleep(1)

    # Print results
    print()
    print("=" * 80)
    print("Test Results")
    print("=" * 80)
    print(f"Connection: {test_results['connection']}")
    print(f"Authentication: {test_results['authentication']}")
    print(f"Subscriptions Sent: {test_results['subscriptions_sent']}")
    print(f"Data Messages Received: {test_results['data_received']}")
    print(f"Unique Symbols with Data: {len(received_data)}")

    if test_results['errors']:
        print(f"\nErrors ({len(test_results['errors'])}):")
        for error in test_results['errors']:
            print(f"  - {error}")

    print()
    print("=" * 80)
    print("Data Summary")
    print("=" * 80)

    if received_data:
        # Show sample data
        print(f"\nReceived data for {len(received_data)} symbols:")
        print()

        # Sort by symbol name
        sorted_symbols = sorted(received_data.keys())

        for symbol in sorted_symbols[:10]:  # Show first 10
            data = received_data[symbol]
            ltp = data.get('ltp', 0)
            volume = data.get('volume', 0)
            oi = data.get('oi', 0)

            print(f"  {symbol:30s} | LTP: {ltp:10.2f} | Volume: {volume:10d} | OI: {oi:10d}")

        if len(sorted_symbols) > 10:
            print(f"\n  ... and {len(sorted_symbols) - 10} more symbols")
    else:
        print("\n[WARN] No data received from WebSocket")

    print()
    print("=" * 80)
    print("Test Verdict")
    print("=" * 80)

    if test_results['connection'] and test_results['authentication'] and test_results['data_received'] > 0:
        print("[SUCCESS] Option chain WebSocket is working correctly!")
        print(f"   - Connected to WebSocket")
        print(f"   - Authenticated successfully")
        print(f"   - Sent {test_results['subscriptions_sent']} subscriptions")
        print(f"   - Received {test_results['data_received']} data updates")
        print(f"   - {len(received_data)} symbols streaming data")
        return True
    elif not test_results['connection']:
        print("[FAILURE] Could not connect to WebSocket")
        print("   - Check if OpenAlgo WebSocket server is running on port 8765")
        return False
    elif not test_results['authentication']:
        print("[FAILURE] Authentication failed")
        print("   - Check API key is correct")
        print("   - Check OpenAlgo server logs")
        return False
    elif test_results['data_received'] == 0:
        print("[PARTIAL] Connected and authenticated but no data received")
        print("   - Subscriptions were sent but no data came back")
        print("   - Check OpenAlgo adapter connection to broker")
        print("   - Check for Error 429 in OpenAlgo logs")
        return False
    else:
        print("[FAILURE] Unknown issue")
        return False


if __name__ == "__main__":
    try:
        success = run_test()
        exit(0 if success else 1)
    except KeyboardInterrupt:
        print("\n\n[INTERRUPT] Test interrupted by user")
        exit(1)
    except Exception as e:
        print(f"\n\n[ERROR] Test failed with error: {e}")
        import traceback
        traceback.print_exc()
        exit(1)
