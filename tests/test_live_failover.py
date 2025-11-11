"""
Test WebSocket Failover with Live Backup Server
This script tests failover from primary (8765) to backup (8766) 
"""

import time
import logging
import sys
from app import create_app
from app.models import TradingAccount
from app.utils.websocket_manager import ProfessionalWebSocketManager

# Configure logging
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

def test_live_failover():
    """Test failover to live backup server on port 8766"""
    
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
            
        logger.info(f"Primary account: {primary.account_name} (port 8765)")
        logger.info(f"Backup accounts: {[f'{acc.account_name} (port {acc.websocket_url.split(":")[-1]})' for acc in backup_accounts]}")
        
        # Create WebSocket manager
        ws_manager = ProfessionalWebSocketManager()
        
        # Create connection pool
        ws_manager.create_connection_pool(
            primary_account=primary,
            backup_accounts=backup_accounts
        )
        
        logger.info("\n--- ATTEMPTING PRIMARY CONNECTION (8765) ---")
        
        # Try primary (should fail if not running)
        try:
            connected = ws_manager.connect(
                primary.websocket_url,
                primary.get_api_key()
            )
            
            if connected:
                logger.warning("Primary connected (unexpected if server is down)")
            else:
                logger.info("Primary connection failed (expected)")
                
        except Exception as e:
            logger.info(f"Primary connection error: {e}")
        
        # Wait for connection attempt
        time.sleep(3)
        
        # Check if authenticated
        if not ws_manager.authenticated:
            logger.info("Not authenticated with primary, triggering failover")
            
            # Manually trigger failover since primary is down
            logger.info("\n--- TRIGGERING FAILOVER TO BACKUP ---")
            ws_manager.handle_connection_failure()
            
            # Wait for failover connection
            time.sleep(3)
            
            # Check current account
            current = ws_manager.connection_pool.get('current_account')
            if current and current != primary:
                logger.info(f"✓ FAILOVER SUCCESS: Now connected to {current.account_name}")
                
                # Check if authenticated with backup
                if ws_manager.authenticated:
                    logger.info("✓ AUTHENTICATED with backup server")
                    
                    # Test subscription
                    test_sub = {
                        'symbol': 'BANKNIFTY',
                        'exchange': 'NSE_INDEX', 
                        'mode': 'quote'
                    }
                    
                    if ws_manager.subscribe(test_sub):
                        logger.info("✓ Successfully subscribed to test symbol")
                    else:
                        logger.warning("Failed to subscribe to test symbol")
                        
                else:
                    logger.warning("✗ NOT AUTHENTICATED with backup")
                    
            else:
                logger.error("✗ FAILOVER FAILED: Still on primary")
                
        else:
            logger.info("Authenticated with primary (server must be running)")
            
        # Check metrics
        metrics = ws_manager.connection_pool.get('metrics', {})
        logger.info(f"\nConnection Metrics:")
        logger.info(f"  Account switches: {metrics.get('account_switches', 0)}")
        logger.info(f"  Total failures: {metrics.get('total_failures', 0)}")
        logger.info(f"  Messages received: {metrics.get('messages_received', 0)}")
        
        # Check failover history
        history = ws_manager.connection_pool.get('failover_history', [])
        if history:
            logger.info("\nFailover History:")
            for event in history:
                logger.info(f"  {event['timestamp']}: {event['from_account']} -> {event['to_account']} ({event['reason']})")
                
        # Cleanup
        if ws_manager.ws:
            ws_manager.disconnect()
            
        return True

if __name__ == "__main__":
    logger.info("Testing WebSocket Failover with Live Backup Server")
    logger.info("=" * 60)
    
    try:
        if test_live_failover():
            logger.info("\n✓ Test completed")
        else:
            logger.error("\n✗ Test failed")
    except Exception as e:
        logger.error(f"Test error: {e}", exc_info=True)
        sys.exit(1)