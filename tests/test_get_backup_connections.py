"""
Test script to retrieve and display backup connection information
Shows current WebSocket and Host URL connections
"""

import sys
import os
import io
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

from flask import Flask
from app import create_app, db
from app.models import TradingAccount
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.background_service import option_chain_service
import logging
import json
from datetime import datetime

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def get_connection_info():
    """Retrieve and display current connection information"""
    
    app = create_app()
    
    with app.app_context():
        print("\n" + "="*80)
        print("BACKUP CONNECTION INFORMATION RETRIEVAL")
        print("="*80)
        
        # Get all trading accounts
        all_accounts = TradingAccount.query.filter_by(is_active=True).all()
        
        if not all_accounts:
            print("\n❌ No active trading accounts found")
            return
        
        print(f"\n📊 Found {len(all_accounts)} active trading account(s)")
        print("-"*80)
        
        # Display all accounts
        for idx, account in enumerate(all_accounts, 1):
            print(f"\n{idx}. Account: {account.account_name}")
            print(f"   Host URL: {account.host_url}")
            print(f"   WebSocket URL: {account.websocket_url}")
            print(f"   Active: {account.is_active}")
            print(f"   Created: {account.created_at}")
        
        # Get primary and backup accounts
        primary = TradingAccount.query.filter_by(
            is_active=True
        ).first()
        
        if not primary:
            # Fallback to any active account
            primary = all_accounts[0]
        
        backup_accounts = TradingAccount.query.filter_by(
            is_active=True
        ).filter(TradingAccount.id != primary.id).all()
        
        print("\n" + "="*80)
        print("CONNECTION CONFIGURATION")
        print("="*80)
        
        print(f"\n🔹 PRIMARY ACCOUNT:")
        print(f"   Name: {primary.account_name}")
        print(f"   Host URL: {primary.host_url}")
        print(f"   WebSocket URL: {primary.websocket_url}")
        
        if backup_accounts:
            print(f"\n🔸 BACKUP ACCOUNTS ({len(backup_accounts)}):")
            for idx, backup in enumerate(backup_accounts, 1):
                print(f"\n   Backup {idx}: {backup.account_name}")
                print(f"   Host URL: {backup.host_url}")
                print(f"   WebSocket URL: {backup.websocket_url}")
        else:
            print(f"\n⚠️  No backup accounts configured")
        
        # Initialize WebSocket Manager to check connection pool
        print("\n" + "="*80)
        print("WEBSOCKET MANAGER STATUS")
        print("="*80)
        
        ws_manager = ProfessionalWebSocketManager()
        
        # Create connection pool
        pool = ws_manager.create_connection_pool(
            primary_account=primary,
            backup_accounts=backup_accounts
        )
        
        print("\n📡 Connection Pool Created:")
        print(f"   Current Account: {pool['current_account'].account_name if pool['current_account'] else 'None'}")
        print(f"   Backup Accounts: {len(pool['backup_accounts'])}")
        print(f"   Status: {pool['status']}")
        
        # Display backup account details from pool
        if pool['backup_accounts']:
            print("\n   Backup Accounts in Pool:")
            for idx, backup in enumerate(pool['backup_accounts'], 1):
                print(f"   {idx}. {backup.account_name}")
                print(f"      Host: {backup.host_url}")
                print(f"      WebSocket: {backup.websocket_url}")
        
        # Check if there's an active WebSocket connection
        print("\n" + "="*80)
        print("ACTIVE CONNECTION TEST")
        print("="*80)
        
        # Try to get Option Chain Service status if it exists
        try:
            option_service = option_chain_service
            
            # Check if service has been initialized
            if hasattr(option_service, 'primary_account'):
                print("\n🔄 Option Chain Service Status:")
                print(f"   Primary Account: {option_service.primary_account.account_name if option_service.primary_account else 'Not set'}")
                print(f"   Backup Accounts: {len(option_service.backup_accounts) if hasattr(option_service, 'backup_accounts') else 0}")
                
                if hasattr(option_service, 'ws_manager') and option_service.ws_manager:
                    status = option_service.ws_manager.get_status()
                    print(f"\n   WebSocket Status:")
                    print(f"   Connected: {status.get('connected', False)}")
                    print(f"   Status: {status.get('status', 'Unknown')}")
                    print(f"   Current Account: {status.get('current_account', 'Unknown')}")
                    print(f"   Subscriptions: {status.get('subscriptions', 0)}")
                    
                    if status.get('metrics'):
                        metrics = status['metrics']
                        print(f"\n   Metrics:")
                        print(f"   Messages Received: {metrics.get('messages_received', 0)}")
                        print(f"   Account Switches: {metrics.get('account_switches', 0)}")
                        print(f"   Total Failures: {metrics.get('total_failures', 0)}")
                        print(f"   Reconnect Count: {metrics.get('reconnect_count', 0)}")
            else:
                print("\n⚠️  Option Chain Service not initialized")
                
        except Exception as e:
            print(f"\n⚠️  Could not get Option Chain Service status: {e}")
        
        # Summary
        print("\n" + "="*80)
        print("SUMMARY")
        print("="*80)
        
        if primary:
            print(f"\n✅ Primary Connection:")
            print(f"   REST API: {primary.host_url}")
            print(f"   WebSocket: {primary.websocket_url}")
        
        if backup_accounts:
            print(f"\n✅ Backup Connections Available: {len(backup_accounts)}")
            for idx, backup in enumerate(backup_accounts[:3], 1):  # Show first 3
                print(f"\n   Backup {idx} ({backup.account_name}):")
                print(f"   REST API: {backup.host_url}")
                print(f"   WebSocket: {backup.websocket_url}")
        else:
            print(f"\n⚠️  No backup connections configured")
        
        print("\n" + "="*80)
        print("Test completed successfully!")
        print("="*80 + "\n")

if __name__ == '__main__':
    try:
        get_connection_info()
    except Exception as e:
        logger.error(f"Error running test: {e}")
        import traceback
        traceback.print_exc()