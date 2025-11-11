"""
Test Single Shared WebSocket Connection
Verifies that AlgoMirror uses only ONE WebSocket connection for all services.
"""
import subprocess
import time
import re
import sys

def count_websocket_connections(port=8765):
    """
    Count WebSocket connections to specified port.

    Returns:
        int: Number of connections
    """
    try:
        # Run netstat to check connections to port 8765
        result = subprocess.run(
            ['netstat', '-ano'],
            capture_output=True,
            text=True
        )

        # Count connections to port 8765
        connections = [line for line in result.stdout.split('\n')
                      if f':{port}' in line and 'ESTABLISHED' in line]

        return len(connections)
    except Exception as e:
        print(f"Error checking connections: {e}")
        return -1

def check_algomirror_running():
    """Check if AlgoMirror is running on port 8000."""
    result = subprocess.run(
        ['netstat', '-ano'],
        capture_output=True,
        text=True
    )

    for line in result.stdout.split('\n'):
        if ':8000' in line and 'LISTENING' in line:
            return True
    return False

def check_openalgo_running():
    """Check if OpenAlgo is running on ports 5000 and 8765."""
    result = subprocess.run(
        ['netstat', '-ano'],
        capture_output=True,
        text=True
    )

    rest_api = False
    websocket = False

    for line in result.stdout.split('\n'):
        if ':5000' in line and 'LISTENING' in line:
            rest_api = True
        if ':8765' in line and 'LISTENING' in line:
            websocket = True

    return rest_api, websocket

def main():
    print("=" * 70)
    print("Single Shared WebSocket Connection Test")
    print("=" * 70)
    print()

    # Step 1: Check prerequisites
    print("Step 1: Checking prerequisites...")
    print("-" * 70)

    # Check OpenAlgo
    rest_api, websocket = check_openalgo_running()

    if not rest_api:
        print("[ERROR] OpenAlgo REST API (port 5000) is NOT running")
        print("   Please start OpenAlgo server first")
        return False
    else:
        print("[OK] OpenAlgo REST API (port 5000) is running")

    if not websocket:
        print("[ERROR] OpenAlgo WebSocket (port 8765) is NOT running")
        print("   Please start OpenAlgo WebSocket server first")
        return False
    else:
        print("[OK] OpenAlgo WebSocket (port 8765) is running")

    # Check AlgoMirror
    if not check_algomirror_running():
        print("[ERROR] AlgoMirror (port 8000) is NOT running")
        print("   Please start AlgoMirror: .venv\\Scripts\\python.exe wsgi.py")
        return False
    else:
        print("[OK] AlgoMirror (port 8000) is running")

    print()

    # Step 2: Count initial connections
    print("Step 2: Counting WebSocket connections...")
    print("-" * 70)

    time.sleep(2)  # Give services time to stabilize

    initial_connections = count_websocket_connections(8765)
    print(f"WebSocket connections to port 8765: {initial_connections}")
    print()

    # Step 3: Verify single connection
    print("Step 3: Verifying single shared connection...")
    print("-" * 70)

    if initial_connections == 1:
        print("[OK] SUCCESS: Only ONE WebSocket connection detected")
        print("   Position monitor, session manager, and option chains")
        print("   are all sharing the same WebSocket connection.")
    elif initial_connections == 0:
        print("[WARN]  WARNING: No WebSocket connections detected")
        print("   Services may not have started yet. Wait a few seconds.")
    elif initial_connections > 1:
        print(f"[ERROR] FAILURE: Multiple connections detected ({initial_connections})")
        print("   Expected: 1 connection")
        print("   Found: {} connections".format(initial_connections))
        print()
        print("   This indicates the shared WebSocket fix is not working.")
        print("   Check logs for errors in shared connection creation.")

    print()

    # Step 4: Recommendations
    print("Step 4: Recommendations...")
    print("-" * 70)

    if initial_connections == 1:
        print("[OK] System is working correctly!")
        print()
        print("Next steps:")
        print("1. Open option chain page: http://127.0.0.1:8000/trading/option-chain")
        print("2. Re-run this test to verify no new connections are created")
        print("3. Monitor OpenAlgo logs for Error 429 (should NOT occur)")
    elif initial_connections == 0:
        print("[WARN]  Wait for services to start and re-run test:")
        print("   python test_single_websocket.py")
    else:
        print("[ERROR] Multiple connections detected - investigate logs:")
        print("   Check: logs/algomirror.log")
        print("   Look for: 'Shared WebSocket manager created'")
        print("   Expected: Should appear ONCE")

    print()
    print("=" * 70)
    return initial_connections == 1

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)

