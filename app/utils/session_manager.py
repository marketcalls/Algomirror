"""
Session Manager for On-Demand Option Chain Loading
Manages WebSocket sessions for users viewing option chains.

Key Features:
- Creates sessions when users visit option chain page
- Heartbeat mechanism to keep sessions alive
- Auto-expires sessions after 5 minutes without heartbeat
- Unsubscribes from symbols when session expires
"""
import logging
import secrets
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Set
import pytz

from app import db
from app.models import WebSocketSession, TradingAccount
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)


class SessionManager:
    """
    Manages on-demand WebSocket sessions for option chain viewing.

    Architecture:
    - Creates session when user opens option chain page
    - Subscribes to selected underlying + expiry
    - Heartbeat every 30 seconds from frontend
    - Auto-expires after 5 minutes without heartbeat
    - Cleanup scheduler removes expired sessions
    """

    _instance = None

    def __new__(cls):
        """Singleton pattern - only one instance"""
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        """Initialize the session manager"""
        if self._initialized:
            return

        self._initialized = True
        self.websocket_manager = None
        self.option_chain_manager = None
        self.session_timeout_minutes = 5

        logger.info("SessionManager initialized")

    def create_session(
        self,
        user_id: int,
        underlying: str,
        expiry: str,
        num_strikes: int = 20
    ) -> Optional[WebSocketSession]:
        """
        Create a new option chain viewing session.

        Args:
            user_id: User ID
            underlying: Underlying symbol (NIFTY, BANKNIFTY, SENSEX)
            expiry: Expiry date
            num_strikes: Number of strikes to subscribe (default 20 ITM + 20 OTM = 40)

        Returns:
            WebSocketSession object if successful, None otherwise
        """
        try:
            # Generate unique session ID
            session_id = secrets.token_urlsafe(32)

            # Create session in database
            now = datetime.utcnow()
            expires_at = now + timedelta(minutes=self.session_timeout_minutes)

            session = WebSocketSession(
                user_id=user_id,
                session_id=session_id,
                underlying=underlying,
                expiry=expiry,
                subscribed_symbols=[],
                is_active=True,
                last_heartbeat=now,
                expires_at=expires_at
            )

            db.session.add(session)
            db.session.commit()

            logger.info(
                f"Created session {session_id} for user {user_id}: "
                f"{underlying} {expiry}"
            )

            # Subscribe to option chain symbols
            self._subscribe_session(session, num_strikes)

            return session

        except Exception as e:
            logger.error(f"Error creating session: {e}")
            db.session.rollback()
            return None

    def _subscribe_session(self, session: WebSocketSession, num_strikes: int):
        """
        Subscribe to option chain symbols for a session.

        Args:
            session: WebSocketSession object
            num_strikes: Number of strikes to subscribe
        """
        try:
            if not self.websocket_manager or not self.option_chain_manager:
                logger.warning("WebSocket or option chain manager not initialized")
                return

            # Get primary account for API calls
            primary_account = TradingAccount.query.filter_by(
                user_id=session.user_id,
                is_primary=True,
                is_active=True
            ).first()

            if not primary_account:
                logger.warning(f"No primary account for user {session.user_id}")
                return

            # Get option chain data
            client = ExtendedOpenAlgoAPI(
                api_key=primary_account.get_api_key(),
                host=primary_account.host_url
            )

            # Get strikes around ATM
            exchange = 'BFO' if session.underlying == 'SENSEX' else 'NFO'

            # Get index LTP to determine ATM
            index_response = client.quotes(
                symbol=session.underlying,
                exchange=exchange,
                token=None
            )

            if index_response.get('status') != 'success':
                logger.error(f"Failed to get index LTP: {index_response.get('message')}")
                return

            index_ltp = index_response.get('data', {}).get('ltp', 0)
            if not index_ltp:
                logger.error("Index LTP not available")
                return

            # Calculate ATM strike
            strike_interval = self._get_strike_interval(session.underlying)
            atm_strike = round(index_ltp / strike_interval) * strike_interval

            # Generate strike list (num_strikes ITM + ATM + num_strikes OTM)
            strikes = []
            for i in range(-num_strikes, num_strikes + 1):
                strike = atm_strike + (i * strike_interval)
                strikes.append(strike)

            # Subscribe to CE and PE for each strike
            subscribed_symbols = []
            for strike in strikes:
                # Call symbol
                ce_symbol = f"{session.underlying}{session.expiry.replace('-', '')}{int(strike)}CE"
                # Put symbol
                pe_symbol = f"{session.underlying}{session.expiry.replace('-', '')}{int(strike)}PE"

                # Subscribe to both
                self.websocket_manager.subscribe({
                    'symbol': ce_symbol,
                    'exchange': exchange,
                    'mode': 'depth'
                })
                subscribed_symbols.append(ce_symbol)

                self.websocket_manager.subscribe({
                    'symbol': pe_symbol,
                    'exchange': exchange,
                    'mode': 'depth'
                })
                subscribed_symbols.append(pe_symbol)

            # Update session with subscribed symbols
            session.subscribed_symbols = subscribed_symbols
            db.session.commit()

            logger.info(
                f"Session {session.session_id}: Subscribed to {len(subscribed_symbols)} symbols "
                f"({num_strikes * 2 + 2} strikes)"
            )

        except Exception as e:
            logger.error(f"Error subscribing session: {e}")
            db.session.rollback()

    def _get_strike_interval(self, underlying: str) -> int:
        """Get strike interval for underlying"""
        intervals = {
            'NIFTY': 50,
            'BANKNIFTY': 100,
            'SENSEX': 100,
            'FINNIFTY': 50,
            'MIDCPNIFTY': 25
        }
        return intervals.get(underlying, 100)

    def update_heartbeat(self, session_id: str) -> bool:
        """
        Update session heartbeat to keep it alive.

        Args:
            session_id: Session ID

        Returns:
            True if updated successfully, False otherwise
        """
        try:
            session = WebSocketSession.query.filter_by(
                session_id=session_id,
                is_active=True
            ).first()

            if not session:
                logger.warning(f"Session {session_id} not found or inactive")
                return False

            # Update heartbeat and extend expiry
            session.update_heartbeat()
            db.session.commit()

            logger.debug(f"Heartbeat updated for session {session_id}")
            return True

        except Exception as e:
            logger.error(f"Error updating heartbeat: {e}")
            db.session.rollback()
            return False

    def destroy_session(self, session_id: str) -> bool:
        """
        Destroy a session and unsubscribe from all symbols.

        Args:
            session_id: Session ID

        Returns:
            True if destroyed successfully, False otherwise
        """
        try:
            session = WebSocketSession.query.filter_by(
                session_id=session_id
            ).first()

            if not session:
                logger.warning(f"Session {session_id} not found")
                return False

            # Unsubscribe from all symbols
            self._unsubscribe_session(session)

            # Mark as inactive and delete
            db.session.delete(session)
            db.session.commit()

            logger.info(f"Session {session_id} destroyed")
            return True

        except Exception as e:
            logger.error(f"Error destroying session: {e}")
            db.session.rollback()
            return False

    def _unsubscribe_session(self, session: WebSocketSession):
        """
        Unsubscribe from all symbols for a session.

        Args:
            session: WebSocketSession object
        """
        try:
            if not self.websocket_manager:
                return

            exchange = 'BFO' if session.underlying == 'SENSEX' else 'NFO'

            for symbol in session.subscribed_symbols:
                self.websocket_manager.unsubscribe({
                    'symbol': symbol,
                    'exchange': exchange
                })

            logger.info(
                f"Unsubscribed {len(session.subscribed_symbols)} symbols "
                f"for session {session.session_id}"
            )

        except Exception as e:
            logger.error(f"Error unsubscribing session: {e}")

    def cleanup_expired_sessions(self):
        """
        Clean up expired sessions.
        Should be called periodically by scheduler (e.g., every minute).
        """
        try:
            now = datetime.utcnow()

            # Find expired sessions
            expired_sessions = WebSocketSession.query.filter(
                WebSocketSession.is_active == True,
                WebSocketSession.expires_at < now
            ).all()

            if not expired_sessions:
                return

            logger.info(f"Cleaning up {len(expired_sessions)} expired sessions")

            for session in expired_sessions:
                # Unsubscribe from symbols
                self._unsubscribe_session(session)

                # Delete session
                db.session.delete(session)

            db.session.commit()

            logger.info(f"Cleaned up {len(expired_sessions)} expired sessions")

        except Exception as e:
            logger.error(f"Error cleaning up expired sessions: {e}")
            db.session.rollback()

    def get_active_sessions(self, user_id: Optional[int] = None) -> List[WebSocketSession]:
        """
        Get active sessions, optionally filtered by user.

        Args:
            user_id: Optional user ID to filter

        Returns:
            List of active WebSocketSession objects
        """
        try:
            query = WebSocketSession.query.filter_by(is_active=True)

            if user_id:
                query = query.filter_by(user_id=user_id)

            return query.all()

        except Exception as e:
            logger.error(f"Error getting active sessions: {e}")
            return []

    def set_websocket_manager(self, websocket_manager):
        """
        Set WebSocket manager reference.

        Args:
            websocket_manager: ProfessionalWebSocketManager instance
        """
        self.websocket_manager = websocket_manager
        logger.info("WebSocket manager set for SessionManager")

    def set_option_chain_manager(self, option_chain_manager):
        """
        Set option chain manager reference.

        Args:
            option_chain_manager: OptionChainManager instance
        """
        self.option_chain_manager = option_chain_manager
        logger.info("Option chain manager set for SessionManager")

    def get_status(self) -> Dict:
        """
        Get current session manager status.

        Returns:
            Dict with status information
        """
        try:
            total_sessions = WebSocketSession.query.filter_by(is_active=True).count()

            # Group by underlying
            sessions_by_underlying = {}
            active_sessions = self.get_active_sessions()

            for session in active_sessions:
                underlying = session.underlying
                if underlying not in sessions_by_underlying:
                    sessions_by_underlying[underlying] = 0
                sessions_by_underlying[underlying] += 1

            return {
                'total_active_sessions': total_sessions,
                'sessions_by_underlying': sessions_by_underlying,
                'websocket_manager_available': self.websocket_manager is not None,
                'option_chain_manager_available': self.option_chain_manager is not None
            }

        except Exception as e:
            logger.error(f"Error getting status: {e}")
            return {
                'total_active_sessions': 0,
                'sessions_by_underlying': {},
                'websocket_manager_available': False,
                'option_chain_manager_available': False
            }


# Global instance
session_manager = SessionManager()
