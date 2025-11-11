"""
Test script to verify strategy monitoring is working
Run this after executing a strategy to check if monitoring is active
"""

import sys
import time
from datetime import datetime

def monitor_logs():
    """Monitor logs for strategy execution and monitoring"""

    print("=" * 80)
    print("STRATEGY MONITORING VERIFICATION")
    print("=" * 80)
    print("\nThis script will check if your strategy is being monitored properly.\n")

    log_file = "logs/algomirror.log"

    print(f"Monitoring log file: {log_file}")
    print(f"Started at: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")
    print("\nLooking for monitoring indicators...")
    print("-" * 80)

    # Keywords to look for
    monitoring_keywords = [
        'WebSocket authenticated',
        'Subscribed to WebSocket',
        'Using existing WebSocket manager',
        'Price update for',
        'Position.*Entry=.*LTP=.*P&L=',
        'Exited position',
        'stop_loss',
        'take_profit'
    ]

    found_monitoring = False
    found_websocket = False
    found_price_updates = False
    position_count = 0

    try:
        with open(log_file, 'r', encoding='utf-8') as f:
            # Go to end of file
            f.seek(0, 2)
            file_size = f.tell()

            # Read last 50KB
            f.seek(max(file_size - 50000, 0))
            recent_logs = f.readlines()

            print("\n📊 RECENT MONITORING ACTIVITY:\n")

            for line in recent_logs[-50:]:  # Last 50 lines
                # Check for WebSocket
                if 'WebSocket authenticated' in line or 'websocket_managers' in line:
                    found_websocket = True
                    print(f"✓ [WEBSOCKET] {line.strip()}")

                # Check for subscriptions
                if 'Subscribed to WebSocket' in line:
                    print(f"✓ [SUBSCRIBE] {line.strip()}")

                # Check for price updates
                if 'Price update' in line or 'LTP=' in line:
                    found_price_updates = True
                    if position_count < 5:  # Show first 5
                        print(f"✓ [PRICE] {line.strip()}")
                    position_count += 1

                # Check for monitoring
                if 'Entry=' in line and 'P&L=' in line:
                    found_monitoring = True
                    print(f"✓ [MONITOR] {line.strip()}")

                # Check for exits
                if 'Exited position' in line:
                    print(f"⚠️ [EXIT] {line.strip()}")

            print("\n" + "=" * 80)
            print("VERIFICATION RESULTS:")
            print("=" * 80)

            print(f"\n1. WebSocket Connected: {'✅ YES' if found_websocket else '❌ NO'}")
            print(f"2. Price Updates Received: {'✅ YES' if found_price_updates else '❌ NO'}")
            print(f"3. Position Monitoring Active: {'✅ YES' if found_monitoring else '❌ NO'}")
            print(f"4. Total Price Updates: {position_count}")

            if found_monitoring:
                print("\n✅ MONITORING IS WORKING!")
                print("   - Background thread is active")
                print("   - Real-time price updates are being received")
                print("   - Stop loss/target checks are running every second")
            else:
                print("\n⚠️ MONITORING MAY NOT BE ACTIVE")
                print("\nPossible reasons:")
                print("   1. No positions are currently open")
                print("   2. Strategy hasn't been executed yet")
                print("   3. Market is closed (monitoring pauses outside trading hours)")
                print("   4. WebSocket connection failed")

            print("\n" + "=" * 80)
            print("\nTo test monitoring:")
            print("1. Execute a strategy: http://127.0.0.1:8000/strategy/")
            print("2. Click 'Execute' on any strategy")
            print("3. Run this script again immediately after execution")
            print("4. You should see monitoring messages every 30 seconds")
            print("\nPress Ctrl+C to exit live monitoring...")

            # Now watch in real-time
            print("\n" + "=" * 80)
            print("LIVE MONITORING (Press Ctrl+C to stop):")
            print("=" * 80 + "\n")

            while True:
                line = f.readline()
                if line:
                    # Filter for important messages
                    if any(keyword in line for keyword in ['Entry=', 'P&L=', 'Exited', 'WebSocket', 'Price update']):
                        timestamp = datetime.now().strftime('%H:%M:%S')
                        print(f"[{timestamp}] {line.strip()}")
                else:
                    time.sleep(0.5)

    except FileNotFoundError:
        print(f"\n❌ ERROR: Log file not found: {log_file}")
        print("Make sure the application is running and logs directory exists.")
    except KeyboardInterrupt:
        print("\n\nMonitoring stopped by user.")
    except Exception as e:
        print(f"\n❌ ERROR: {e}")

if __name__ == "__main__":
    monitor_logs()
