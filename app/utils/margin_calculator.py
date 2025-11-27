"""
Margin Calculator Module
Handles lot size calculations based on available margin and trade quality
"""

import logging
from datetime import datetime, date
from typing import Dict, Tuple, Optional
from app.models import (
    MarginRequirement, TradeQuality, TradingSettings,
    MarginTracker, TradingAccount, MarketHoliday
)
from app.utils.openalgo_client import ExtendedOpenAlgoAPI

logger = logging.getLogger(__name__)


class MarginCalculator:
    """Calculate lot sizes based on margin requirements and trade quality"""

    def __init__(self, user_id: int):
        self.user_id = user_id
        self.margin_requirements = self._load_margin_requirements()
        self.trade_qualities = self._load_trade_qualities()
        self.trading_settings = self._load_trading_settings()

    def _load_margin_requirements(self) -> Dict:
        """Load user's margin requirements"""
        requirements = {}
        margins = MarginRequirement.query.filter_by(
            user_id=self.user_id,
            is_active=True
        ).all()

        for margin in margins:
            requirements[margin.instrument] = margin

        # Create defaults if not exists
        if not requirements:
            MarginRequirement.get_or_create_defaults(self.user_id)
            return self._load_margin_requirements()

        return requirements

    def _load_trade_qualities(self) -> Dict:
        """Load user's trade quality settings"""
        qualities = {}
        trade_quals = TradeQuality.query.filter_by(
            user_id=self.user_id,
            is_active=True
        ).all()

        for qual in trade_quals:
            qualities[qual.quality_grade] = qual

        # Create defaults if not exists
        if not qualities:
            TradeQuality.get_or_create_defaults(self.user_id)
            return self._load_trade_qualities()

        return qualities

    def _load_trading_settings(self) -> Dict:
        """Load user's trading settings (lot sizes)"""
        settings = {}
        trade_settings = TradingSettings.query.filter_by(
            user_id=self.user_id,
            is_active=True
        ).all()

        for setting in trade_settings:
            settings[setting.symbol] = setting

        # Create defaults if not exists
        if not settings:
            TradingSettings.get_or_create_defaults(self.user_id)
            trade_settings = TradingSettings.query.filter_by(
                user_id=self.user_id,
                is_active=True
            ).all()
            for setting in trade_settings:
                settings[setting.symbol] = setting

        return settings

    def is_expiry_day(self, instrument: str = 'NIFTY') -> bool:
        """Check if today is expiry day for the instrument"""
        today = date.today()
        day_of_week = today.weekday()

        # Check for special holidays
        holiday = MarketHoliday.query.filter_by(
            holiday_date=today,
            market='NSE' if instrument != 'SENSEX' else 'BSE'
        ).first()

        if holiday:
            return False

        # Standard expiry days
        if instrument == 'NIFTY':
            return day_of_week == 1  # Tuesday
        elif instrument == 'BANKNIFTY':
            return day_of_week == 1  # Tuesday
        elif instrument == 'SENSEX':
            return day_of_week == 3  # Thursday
        else:
            return False

    def get_margin_requirement(self,
                              instrument: str,
                              trade_type: str,
                              is_expiry: Optional[bool] = None) -> float:
        """
        Get margin requirement for specific trade type

        Args:
            instrument: NIFTY, BANKNIFTY, or SENSEX
            trade_type: 'sell_c_p' (Sell C/P), 'sell_c_and_p' (Sell C and P), 'buy', 'futures'
            is_expiry: Override expiry day detection if provided
        """
        # For option buying, no margin is blocked (only premium is paid)
        if trade_type == 'buy':
            # Option buying doesn't block any margin
            return 0.0

        if is_expiry is None:
            is_expiry = self.is_expiry_day(instrument)

        if instrument not in self.margin_requirements:
            logger.error(f"No margin requirements found for {instrument}")
            return 0

        margin_req = self.margin_requirements[instrument]

        # Map trade types to database fields
        if instrument == 'SENSEX':
            margin_map = {
                ('sell_c_p', True): margin_req.sensex_ce_pe_sell_expiry,
                ('sell_c_p', False): margin_req.sensex_ce_pe_sell_non_expiry,
                ('sell_c_and_p', True): margin_req.sensex_ce_and_pe_sell_expiry,
                ('sell_c_and_p', False): margin_req.sensex_ce_and_pe_sell_non_expiry,
                ('futures', True): margin_req.sensex_futures_expiry,
                ('futures', False): margin_req.sensex_futures_non_expiry,
            }
        else:
            margin_map = {
                ('sell_c_p', True): margin_req.ce_pe_sell_expiry,
                ('sell_c_p', False): margin_req.ce_pe_sell_non_expiry,
                ('sell_c_and_p', True): margin_req.ce_and_pe_sell_expiry,
                ('sell_c_and_p', False): margin_req.ce_and_pe_sell_non_expiry,
                ('futures', True): margin_req.futures_expiry,
                ('futures', False): margin_req.futures_non_expiry,
            }

        key = (trade_type, is_expiry)
        margin = margin_map.get(key, 0)

        logger.info(f"Margin for {instrument} {trade_type} (expiry={is_expiry}): {margin}")
        return margin

    def calculate_lot_size(self,
                          account: TradingAccount,
                          instrument: str,
                          trade_type: str,
                          quality_grade: str,
                          available_margin: Optional[float] = None) -> Tuple[int, Dict]:
        """
        Calculate optimal lot size based on margin requirements

        Args:
            account: Trading account object
            instrument: NIFTY, BANKNIFTY, or SENSEX
            trade_type: 'sell_c_p', 'sell_c_and_p', 'buy', 'futures'
            quality_grade: 'A', 'B', or 'C'
            available_margin: Override available margin if provided

        Returns:
            Tuple of (lot_size, calculation_details)
        """
        try:
            # Get available margin if not provided
            if available_margin is None:
                # Check if it's a manual calculation (dummy account)
                if hasattr(account, 'available_margin'):
                    available_margin = account.available_margin
                else:
                    available_margin = self.get_available_margin(account)

            # Get quality percentage
            if quality_grade not in self.trade_qualities:
                logger.error(f"Invalid quality grade: {quality_grade}")
                return 0, {"error": "Invalid quality grade"}

            quality = self.trade_qualities[quality_grade]
            quality_percentage = quality.margin_percentage / 100

            # Get margin requirement per lot
            margin_per_lot = self.get_margin_requirement(instrument, trade_type)

            # Special case for option buying - no margin blocked
            if margin_per_lot == 0:
                details = {
                    "available_margin": available_margin,
                    "quality_grade": quality_grade,
                    "quality_percentage": quality.margin_percentage,
                    "effective_margin": available_margin * quality_percentage,
                    "margin_per_lot": 0,
                    "raw_lot_size": 0,
                    "final_lot_size": 0,
                    "margin_required": 0,
                    "margin_remaining": available_margin,
                    "calculation": "Option buying doesn't block any margin - lots not limited by margin"
                }
                logger.info(f"Option buying for {account.account_name}: No margin blocked")
                return 0, details

            if margin_per_lot < 0:
                return 0, {"error": "Invalid margin requirement"}

            # Calculate effective available margin
            effective_margin = available_margin * quality_percentage

            # Calculate raw lot size
            raw_lot_size = effective_margin / margin_per_lot

            # Round down to nearest integer
            lot_size = int(raw_lot_size)

            # Prepare calculation details
            details = {
                "available_margin": available_margin,
                "quality_grade": quality_grade,
                "quality_percentage": quality.margin_percentage,
                "effective_margin": effective_margin,
                "margin_per_lot": margin_per_lot,
                "raw_lot_size": raw_lot_size,
                "final_lot_size": lot_size,
                "margin_required": lot_size * margin_per_lot,
                "margin_remaining": available_margin - (lot_size * margin_per_lot),
                "calculation": f"{available_margin:.2f} × {quality.margin_percentage}% / {margin_per_lot:.2f} = {raw_lot_size:.3f} = {lot_size} lots"
            }

            logger.info(f"Lot calculation for {account.account_name}: {details['calculation']}")
            return lot_size, details

        except Exception as e:
            logger.error(f"Error calculating lot size: {e}")
            return 0, {"error": str(e)}

    def calculate_lot_size_custom(self,
                                  account: TradingAccount,
                                  instrument: str,
                                  trade_type: str,
                                  margin_percentage: float,
                                  available_margin: Optional[float] = None,
                                  is_expiry: Optional[bool] = None) -> Tuple[int, Dict]:
        """
        Calculate optimal lot size with custom margin percentage (for risk profiles)

        Args:
            account: Trading account object
            instrument: NIFTY, BANKNIFTY, or SENSEX
            trade_type: 'sell_c_p', 'sell_c_and_p', 'buy', 'futures'
            margin_percentage: Percentage of margin to use (0.0 to 1.0, e.g., 0.65 for 65%)
            available_margin: Override available margin if provided
            is_expiry: Override expiry day detection (True=expiry, False=non-expiry, None=auto-detect)

        Returns:
            Tuple of (lot_size, calculation_details)
        """
        try:
            logger.info(f"[LOT CALC DEBUG] Starting custom lot calculation for {account.account_name}")
            logger.info(f"[LOT CALC DEBUG] Instrument: {instrument}, Trade Type: {trade_type}, Margin %: {margin_percentage*100}%")

            # Get available margin if not provided
            if available_margin is None:
                # Check if it's a manual calculation (dummy account)
                if hasattr(account, 'available_margin'):
                    available_margin = account.available_margin
                    logger.info(f"[LOT CALC DEBUG] Using account.available_margin: ₹{available_margin:,.2f}")
                else:
                    available_margin = self.get_available_margin(account)
                    logger.info(f"[LOT CALC DEBUG] Fetched available margin: ₹{available_margin:,.2f}")
            else:
                logger.info(f"[LOT CALC DEBUG] Using provided available margin: ₹{available_margin:,.2f}")

            # Get margin requirement per lot (use is_expiry override if provided)
            margin_per_lot = self.get_margin_requirement(instrument, trade_type, is_expiry=is_expiry)
            logger.info(f"[LOT CALC DEBUG] Margin per lot: ₹{margin_per_lot:,.2f} (is_expiry={is_expiry})")

            # Special case for option buying - no margin blocked
            if margin_per_lot == 0:
                details = {
                    "available_margin": available_margin,
                    "margin_percentage": margin_percentage * 100,
                    "effective_margin": available_margin * margin_percentage,
                    "margin_per_lot": 0,
                    "raw_lot_size": 0,
                    "final_lot_size": 0,
                    "margin_required": 0,
                    "margin_remaining": available_margin,
                    "calculation": "Option buying doesn't block any margin - lots not limited by margin"
                }
                logger.info(f"[LOT CALC DEBUG] Option buying for {account.account_name}: No margin blocked")
                return 0, details

            if margin_per_lot < 0:
                logger.error(f"[LOT CALC DEBUG] Invalid margin requirement: {margin_per_lot}")
                return 0, {"error": "Invalid margin requirement"}

            # Calculate effective available margin using custom percentage
            effective_margin = available_margin * margin_percentage
            logger.info(f"[LOT CALC DEBUG] Effective margin (₹{available_margin:,.2f} × {margin_percentage*100}%): ₹{effective_margin:,.2f}")

            # Calculate raw lot size
            raw_lot_size = effective_margin / margin_per_lot
            logger.info(f"[LOT CALC DEBUG] Raw lot size (₹{effective_margin:,.2f} / ₹{margin_per_lot:,.2f}): {raw_lot_size:.3f}")

            # Round down to nearest integer
            lot_size = int(raw_lot_size)
            logger.info(f"[LOT CALC DEBUG] Final lot size (rounded down): {lot_size}")

            # Prepare calculation details
            details = {
                "available_margin": available_margin,
                "margin_percentage": margin_percentage * 100,  # Convert to percentage for display
                "effective_margin": effective_margin,
                "margin_per_lot": margin_per_lot,
                "raw_lot_size": raw_lot_size,
                "final_lot_size": lot_size,
                "margin_required": lot_size * margin_per_lot,
                "margin_remaining": available_margin - (lot_size * margin_per_lot),
                "calculation": f"{available_margin:.2f} × {margin_percentage*100}% / {margin_per_lot:.2f} = {raw_lot_size:.3f} = {lot_size} lots"
            }

            logger.info(f"[LOT CALC DEBUG] Margin required: ₹{details['margin_required']:,.2f}, Remaining: ₹{details['margin_remaining']:,.2f}")
            logger.info(f"Custom margin lot calculation for {account.account_name}: {details['calculation']}")
            return lot_size, details

        except Exception as e:
            logger.error(f"[LOT CALC DEBUG] Error calculating lot size with custom margin: {e}", exc_info=True)
            return 0, {"error": str(e)}

    def get_available_margin(self, account: TradingAccount) -> float:
        """Get real-time available margin from account"""
        try:
            logger.info(f"[MARGIN DEBUG] Getting available margin for account: {account.account_name} (ID: {account.id})")

            # Check if account has margin tracker
            tracker = MarginTracker.query.filter_by(account_id=account.id).first()

            # If tracker exists and is recent (< 5 minutes), use cached data
            if tracker and tracker.last_updated:
                time_diff = (datetime.utcnow() - tracker.last_updated).seconds
                logger.info(f"[MARGIN DEBUG] Found tracker, last updated {time_diff} seconds ago")
                if time_diff < 300:  # 5 minutes
                    logger.info(f"[MARGIN DEBUG] Using cached margin: ₹{tracker.free_margin:,.2f}")
                    return tracker.free_margin

            # Fetch fresh margin data from API
            logger.info(f"[MARGIN DEBUG] Fetching fresh margin data from API: {account.host_url}")
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )

            response = client.funds()
            logger.info(f"[MARGIN DEBUG] API Response status: {response.get('status')}")

            if response.get('status') == 'success':
                funds_data = response.get('data', {})
                logger.info(f"[MARGIN DEBUG] Funds data received: {funds_data}")

                # Create or update margin tracker
                if not tracker:
                    tracker = MarginTracker(account_id=account.id)
                    from app import db
                    db.session.add(tracker)
                    logger.info(f"[MARGIN DEBUG] Created new MarginTracker for account {account.id}")

                tracker.update_margins(funds_data)
                from app import db
                db.session.commit()

                logger.info(f"[MARGIN DEBUG] Updated tracker - Free margin: ₹{tracker.free_margin:,.2f}, Used margin: ₹{tracker.used_margin:,.2f}")
                return tracker.free_margin

            else:
                logger.warning(f"[MARGIN DEBUG] API call failed, status: {response.get('status')}, message: {response.get('message')}")
                # Fallback to cached data if available
                if account.last_funds_data:
                    fallback_margin = account.last_funds_data.get('totalcash', 0)
                    logger.info(f"[MARGIN DEBUG] Using fallback margin from last_funds_data: ₹{fallback_margin:,.2f}")
                    return fallback_margin
                logger.warning(f"[MARGIN DEBUG] No fallback data available, returning 0")
                return 0

        except Exception as e:
            logger.error(f"[MARGIN DEBUG] Error fetching available margin: {e}", exc_info=True)
            return 0

    def calculate_multi_trade_lots(self,
                                  account: TradingAccount,
                                  trades: list,
                                  quality_grade: str) -> Dict:
        """
        Calculate lot sizes for multiple trades considering margin depletion

        Args:
            account: Trading account
            trades: List of trade dictionaries with 'instrument' and 'trade_type'
            quality_grade: Quality grade to apply

        Returns:
            Dictionary with lot sizes and details for each trade
        """
        results = {}
        remaining_margin = self.get_available_margin(account)

        for i, trade in enumerate(trades):
            instrument = trade.get('instrument')
            trade_type = trade.get('trade_type')

            # Calculate lot size with current remaining margin
            lot_size, details = self.calculate_lot_size(
                account=account,
                instrument=instrument,
                trade_type=trade_type,
                quality_grade=quality_grade,
                available_margin=remaining_margin
            )

            # Update remaining margin
            if lot_size > 0:
                margin_used = lot_size * details['margin_per_lot']
                remaining_margin -= margin_used

            results[f"trade_{i+1}"] = {
                "instrument": instrument,
                "trade_type": trade_type,
                "lot_size": lot_size,
                "margin_used": lot_size * details.get('margin_per_lot', 0),
                "details": details
            }

        results['summary'] = {
            "total_trades": len(trades),
            "initial_margin": self.get_available_margin(account),
            "final_margin": remaining_margin,
            "total_margin_used": self.get_available_margin(account) - remaining_margin
        }

        return results

    def validate_margin_for_strategy(self,
                                    strategy_legs: list,
                                    accounts: list,
                                    quality_grade: str = 'B') -> Dict:
        """
        Validate if accounts have sufficient margin for strategy execution

        Args:
            strategy_legs: List of strategy legs
            accounts: List of trading accounts
            quality_grade: Quality grade for margin calculation

        Returns:
            Validation results with feasibility for each account
        """
        validation_results = {}

        for account in accounts:
            account_result = {
                "account_name": account.account_name,
                "available_margin": self.get_available_margin(account),
                "legs": [],
                "total_margin_required": 0,
                "is_feasible": True,
                "recommended_lots": {}
            }

            remaining_margin = account_result["available_margin"]

            for leg in strategy_legs:
                # Determine trade type from leg
                if leg.product_type == 'options':
                    if leg.action == 'SELL':
                        # Check if it's a spread (both CE and PE)
                        trade_type = 'sell_c_and_p' if self._is_spread_leg(leg, strategy_legs) else 'sell_c_p'
                    else:
                        trade_type = 'buy'
                elif leg.product_type == 'futures':
                    trade_type = 'futures'
                else:
                    trade_type = 'buy'

                # Calculate required margin for this leg
                margin_per_lot = self.get_margin_requirement(leg.instrument, trade_type)

                # Get lot size from leg or calculate
                lots = leg.lots if leg.lots else 1
                margin_required = margin_per_lot * lots

                leg_result = {
                    "instrument": leg.instrument,
                    "trade_type": trade_type,
                    "lots": lots,
                    "margin_per_lot": margin_per_lot,
                    "margin_required": margin_required,
                    "can_execute": remaining_margin >= margin_required
                }

                account_result["legs"].append(leg_result)
                account_result["total_margin_required"] += margin_required

                if not leg_result["can_execute"]:
                    account_result["is_feasible"] = False
                    # Calculate maximum possible lots
                    max_lots = int(remaining_margin / margin_per_lot) if margin_per_lot > 0 else 0
                    account_result["recommended_lots"][leg.instrument] = max_lots

                remaining_margin -= margin_required

            validation_results[account.id] = account_result

        return validation_results

    def _is_spread_leg(self, current_leg, all_legs) -> bool:
        """Check if current leg is part of a spread (both CE and PE)"""
        for other_leg in all_legs:
            if (other_leg.instrument == current_leg.instrument and
                other_leg.product_type == 'options' and
                other_leg.action == 'SELL' and
                other_leg != current_leg):
                # Check if one is CE and other is PE
                if ((current_leg.option_type == 'CE' and other_leg.option_type == 'PE') or
                    (current_leg.option_type == 'PE' and other_leg.option_type == 'CE')):
                    return True
        return False

    def update_margin_allocation(self,
                                account: TradingAccount,
                                trade_id: int,
                                margin_amount: float,
                                action: str = 'allocate') -> bool:
        """
        Update margin allocation for a trade

        Args:
            account: Trading account
            trade_id: Trade/Strategy execution ID
            margin_amount: Amount to allocate/release
            action: 'allocate' or 'release'
        """
        try:
            tracker = MarginTracker.query.filter_by(account_id=account.id).first()

            if not tracker:
                tracker = MarginTracker(account_id=account.id)
                # Initialize with current margin
                tracker.free_margin = self.get_available_margin(account)
                from app import db
                db.session.add(tracker)

            if action == 'allocate':
                tracker.allocate_margin(trade_id, margin_amount)
            elif action == 'release':
                tracker.release_margin(trade_id)

            from app import db
            db.session.commit()
            return True

        except Exception as e:
            logger.error(f"Error updating margin allocation: {e}")
            return False