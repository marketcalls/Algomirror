"""
Test WebSocket Failover Mechanism
This script tests the complete failover process including API and WebSocket connections
"""

import time
import logging
import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from app import create_app
from app.models import TradingAccount
from app.utils.websocket_manager import ProfessionalWebSocketManager
from app.utils.openalgo_client import ExtendedOpenAlgoAPI
from app.utils.background_service import option_chain_service

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_complete_failover():
    """Test complete failover including API and WebSocket connections"""
    
    app = create_app()
    
    with app.app_context():
        # Get accounts
        primary = TradingAccount.query.filter_by(
            is_primary=True,
            is_active=True
        ).first()
        
        backup_accounts = TradingAccount.query.filter_by(
            is_active=True,
            is_primary=False
        ).order_by(TradingAccount.created_at).all()
        
        if not primary:
            logger.error("No primary account found")
            return False
            
        logger.info(f"Primary account: {primary.account_name} ({primary.host_url})")
        logger.info(f"Backup accounts: {[f'{acc.account_name} ({acc.host_url})' for acc in backup_accounts]}")
        
        # Test 1: API Failover
        logger.info("\n" + "="*60)
        logger.info("TEST 1: API FAILOVER")
        logger.info("="*60)
        
        # Try primary API (should fail if server is down)
        primary_client = ExtendedOpenAlgoAPI(
            api_key=primary.get_api_key(),
            host=primary.host_url
        )
        
        logger.info(f"Attempting primary API ping to {primary.host_url}...")
        try:
            ping_response = primary_client.ping()
            if ping_response.get('status') == 'success':
                logger.warning("Primary API responded (server must be running)")
                logger.info(f"Response: {ping_response}")
            else:
                logger.info(f"Primary API failed: {ping_response}")
        except Exception as e:
            logger.info(f"Primary API connection failed (expected if server down): {e}")
            
            # Test backup API
            if backup_accounts:
                backup = backup_accounts[0]
                logger.info(f"\nTrying backup API at {backup.host_url}...")
                
                backup_client = ExtendedOpenAlgoAPI(
                    api_key=backup.get_api_key(),
                    host=backup.host_url
                )
                
                try:
                    backup_ping = backup_client.ping()
                    if backup_ping.get('status') == 'success':
                        logger.info(f"✓ Backup API successful: {backup_ping}")
                        
                        # Test getting expiry data
                        logger.info("\nTesting expiry fetch from backup...")
                        expiry_response = backup_client.expiry(
                            symbol='NIFTY',
                            exchange='NFO',
                            instrumenttype='options'
                        )
                        
                        if expiry_response.get('status') == 'success':
                            expiries = expiry_response.get('data', [])
                            logger.info(f"✓ Got {len(expiries)} expiries from backup: {expiries[:3]}...")
                        else:
                            logger.warning(f"Failed to get expiries: {expiry_response}")
                            
                    else:
                        logger.error(f"Backup API failed: {backup_ping}")
                except Exception as e:
                    logger.error(f"Backup API error: {e}")
        
        # Test 2: WebSocket Failover
        logger.info("\n" + "="*60)
        logger.info("TEST 2: WEBSOCKET FAILOVER")
        logger.info("="*60)
        
        ws_manager = ProfessionalWebSocketManager()
        
        # Create connection pool
        ws_manager.create_connection_pool(
            primary_account=primary,
            backup_accounts=backup_accounts
        )
        
        logger.info(f"Attempting WebSocket connection to primary ({primary.websocket_url})...")
        
        # Try to connect (should trigger failover if primary is down)
        connected = ws_manager.connect(
            primary.websocket_url,
            primary.get_api_key()
        )
        
        # Wait for connection/failover
        time.sleep(3)
        
        # Check connection status
        if ws_manager.ws and ws_manager.ws.sock:
            logger.info("✓ WebSocket connected")
            
            # Check which account we're connected to
            current = ws_manager.connection_pool.get('current_account')
            if current:
                if current == primary:
                    logger.info(f"Connected to PRIMARY: {current.account_name}")
                else:
                    logger.info(f"✓ FAILOVER SUCCESS - Connected to BACKUP: {current.account_name}")
                    
            # Check authentication
            if ws_manager.authenticated:
                logger.info("✓ WebSocket authenticated")
                
                # Test subscription
                test_symbol = {
                    'symbol': 'NIFTY',
                    'exchange': 'NSE_INDEX',
                    'mode': 'quote'
                }
                
                logger.info(f"Testing subscription to {test_symbol['symbol']}...")
                if ws_manager.subscribe(test_symbol):
                    logger.info("✓ Subscription successful")
                    
                    # Wait for data
                    time.sleep(2)
                    
                    # Check if we received any data
                    if ws_manager.latest_data.get('NIFTY:NSE_INDEX'):
                        logger.info(f"✓ Receiving data for NIFTY")
                else:
                    logger.warning("Subscription failed")
            else:
                logger.warning("Not authenticated")
        else:
            logger.error("WebSocket connection failed completely")
            
        # Test 3: Background Service Failover
        logger.info("\n" + "="*60)
        logger.info("TEST 3: BACKGROUND SERVICE FAILOVER")
        logger.info("="*60)
        
        # Simulate primary account connected (even though server is down)
        logger.info("Testing background service with failover...")
        option_chain_service.primary_account = primary
        option_chain_service.backup_accounts = backup_accounts
        
        # Try to start option chain (should use failover)
        success = option_chain_service.start_option_chain('NIFTY')
        
        if success:
            logger.info("✓ Option chain started successfully (used failover)")
            
            # Check which account is being used
            active_managers = option_chain_service.active_managers
            if active_managers:
                logger.info(f"Active option chains: {list(active_managers.keys())}")
        else:
            logger.warning("Option chain failed to start")
            
        # Check metrics
        if ws_manager.connection_pool:
            metrics = ws_manager.connection_pool.get('metrics', {})
            logger.info(f"\nConnection Metrics:")
            logger.info(f"  Account switches: {metrics.get('account_switches', 0)}")
            logger.info(f"  Total failures: {metrics.get('total_failures', 0)}")
            logger.info(f"  Messages received: {metrics.get('messages_received', 0)}")
            
            # Check failover history
            history = ws_manager.connection_pool.get('failover_history', [])
            if history:
                logger.info("\nFailover History:")
                for event in history[-3:]:  # Show last 3 events
                    logger.info(f"  {event['timestamp']}: {event.get('from_account', 'None')} -> {event.get('to_account', 'Unknown')} ({event.get('reason', 'N/A')})")
        
        # Cleanup
        logger.info("\nCleaning up...")
        option_chain_service.stop_all_option_chains()
        if ws_manager.ws:
            ws_manager.disconnect()
            
        return True

if __name__ == "__main__":
    logger.info("COMPREHENSIVE FAILOVER TEST")
    logger.info("="*60)
    logger.info("This test verifies API and WebSocket failover mechanisms")
    logger.info("Ensure primary server (8765) is DOWN and backup (8766) is UP")
    logger.info("="*60)
    
    try:
        if test_complete_failover():
            logger.info("\n✓ All tests completed")
        else:
            logger.error("\n✗ Tests failed")
    except Exception as e:
        logger.error(f"Test error: {e}", exc_info=True)
        sys.exit(1)