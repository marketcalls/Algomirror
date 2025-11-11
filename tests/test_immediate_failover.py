"""
Test immediate WebSocket failover when primary server goes down
"""
import time
import requests
import subprocess
import sys

def test_failover():
    print("=== WebSocket Failover Test ===")
    print("\n1. Checking if both servers are running...")
    
    # Check primary (8765)
    try:
        response = requests.get("http://127.0.0.1:8765", timeout=2)
        print("✓ Primary server running on port 8765")
    except:
        print("✗ Primary server NOT running on port 8765")
        print("Please start primary server first")
        return
    
    # Check backup (8766)
    try:
        response = requests.get("http://127.0.0.1:8766", timeout=2)
        print("✓ Backup server running on port 8766")
    except:
        print("✗ Backup server NOT running on port 8766")
        print("Please start backup server first")
        return
    
    print("\n2. AlgoMirror should be running and connected to primary")
    print("   Monitoring at http://localhost:8000")
    
    input("\n3. Press Enter when ready to simulate primary server failure...")
    
    print("\n4. Simulating primary server failure...")
    print("   Please manually stop the primary server (port 8765)")
    print("   The system should detect connection refusal and failover to backup (8766)")
    
    print("\n5. Expected behavior:")
    print("   - WebSocket detects connection closed")
    print("   - First reconnection attempt (1 second delay)")
    print("   - Connection refused detected")
    print("   - Second reconnection attempt (2 second delay)")
    print("   - Connection refused detected again")
    print("   - Immediate failover triggered to backup account")
    print("   - Connection established to port 8766")
    print("   - All subscriptions restored")
    
    print("\n6. Monitor the AlgoMirror logs for:")
    print("   - 'Connection refused multiple times, immediately triggering failover'")
    print("   - 'Switching from Test to backup account'")
    print("   - 'Attempting to connect to backup WebSocket: ws://127.0.0.1:8766'")
    print("   - 'Successfully connected to backup account'")
    
    print("\nTest setup complete. Monitor the logs to verify failover behavior.")

if __name__ == "__main__":
    test_failover()