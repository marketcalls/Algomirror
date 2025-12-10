from flask import render_template, request, jsonify, flash, redirect, url_for
from flask_login import login_required, current_user
from app import db
from app.margin import margin_bp
from app.models import (
    MarginRequirement, TradeQuality, MarginTracker,
    TradingAccount, Strategy, StrategyLeg
)
from app.utils.margin_calculator import MarginCalculator
from app.utils.rate_limiter import api_rate_limit, heavy_rate_limit
from datetime import datetime
import logging

logger = logging.getLogger(__name__)

@margin_bp.route('/')
@login_required
def dashboard():
    """Margin dashboard showing requirements and current usage"""
    from app.utils.openalgo_client import ExtendedOpenAlgoAPI
    from datetime import datetime, timedelta
    import pytz

    # Get user's margin requirements
    margin_requirements = MarginRequirement.query.filter_by(
        user_id=current_user.id
    ).all()

    # Create defaults if not exists
    if not margin_requirements:
        MarginRequirement.get_or_create_defaults(current_user.id)
        margin_requirements = MarginRequirement.query.filter_by(
            user_id=current_user.id
        ).all()

    # Get trade qualities
    trade_qualities = TradeQuality.query.filter_by(
        user_id=current_user.id
    ).all()

    if not trade_qualities:
        TradeQuality.get_or_create_defaults(current_user.id)
        trade_qualities = TradeQuality.query.filter_by(
            user_id=current_user.id
        ).all()

    # Get ALL active accounts
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    margin_trackers = []
    ist_tz = pytz.timezone('Asia/Kolkata')

    # Process each account - similar to funds route
    for account in accounts:
        try:
            # Fetch real-time funds data from API
            client = ExtendedOpenAlgoAPI(
                api_key=account.get_api_key(),
                host=account.host_url
            )
            response = client.funds()

            funds_data = {}
            if response.get('status') == 'success':
                funds_data = response.get('data', {})
                # Update cached data
                account.last_funds_data = funds_data
                account.last_data_update = datetime.utcnow()
                db.session.commit()
            elif account.last_funds_data:
                # Use cached data if API fails
                funds_data = account.last_funds_data

            # Extract margin-related values from funds data using correct field names
            total_cash = float(funds_data.get('availablecash', 0))
            margin_used = float(funds_data.get('utiliseddebits', 0))
            collateral = float(funds_data.get('collateral', 0))

            # Convert UTC to IST for display
            last_updated_ist = None
            if account.last_data_update:
                utc_time = pytz.utc.localize(account.last_data_update)
                last_updated_ist = utc_time.astimezone(ist_tz)

            # Add to trackers list
            margin_trackers.append({
                'account_id': account.id,
                'account_name': account.account_name,
                'broker': account.broker_name,
                'total_margin': total_cash + collateral,  # Total available including collateral
                'used_margin': margin_used,
                'free_margin': (total_cash + collateral) - margin_used,
                'available_cash': total_cash,
                'collateral': collateral,
                'utilized_debits': margin_used,
                'm2m_realized': float(funds_data.get('m2mrealized', 0)),
                'm2m_unrealized': float(funds_data.get('m2munrealized', 0)),
                'last_updated': last_updated_ist
            })

        except Exception as e:
            logger.error(f"Error fetching margin for {account.account_name}: {e}")
            # Still show account with zero values if fetch fails
            margin_trackers.append({
                'account_id': account.id,
                'account_name': account.account_name,
                'broker': account.broker_name,
                'total_margin': 0,
                'used_margin': 0,
                'free_margin': 0,
                'span_margin': 0,
                'exposure_margin': 0,
                'option_premium': 0,
                'collateral': 0,
                'last_updated': None
            })

    return render_template('margin/dashboard.html',
                         margin_requirements=margin_requirements,
                         trade_qualities=trade_qualities,
                         margin_trackers=margin_trackers)

@margin_bp.route('/requirements', methods=['GET', 'POST'])
@login_required
def requirements():
    """Manage margin requirements"""
    if request.method == 'POST':
        try:
            data = request.get_json()
            instrument = data.get('instrument')

            # Find or create margin requirement
            margin_req = MarginRequirement.query.filter_by(
                user_id=current_user.id,
                instrument=instrument
            ).first()

            if not margin_req:
                margin_req = MarginRequirement(
                    user_id=current_user.id,
                    instrument=instrument
                )
                db.session.add(margin_req)

            # Update values based on instrument
            if instrument == 'SENSEX':
                margin_req.sensex_ce_pe_sell_expiry = float(data.get('ce_pe_sell_expiry', 180000))
                margin_req.sensex_ce_pe_sell_non_expiry = float(data.get('ce_pe_sell_non_expiry', 220000))
                margin_req.sensex_ce_and_pe_sell_expiry = float(data.get('ce_and_pe_sell_expiry', 225000))
                margin_req.sensex_ce_and_pe_sell_non_expiry = float(data.get('ce_and_pe_sell_non_expiry', 290000))
                margin_req.sensex_futures_expiry = float(data.get('futures_expiry', 185000))
                margin_req.sensex_futures_non_expiry = float(data.get('futures_non_expiry', 185000))
            else:
                margin_req.ce_pe_sell_expiry = float(data.get('ce_pe_sell_expiry', 205000))
                margin_req.ce_pe_sell_non_expiry = float(data.get('ce_pe_sell_non_expiry', 250000))
                margin_req.ce_and_pe_sell_expiry = float(data.get('ce_and_pe_sell_expiry', 250000))
                margin_req.ce_and_pe_sell_non_expiry = float(data.get('ce_and_pe_sell_non_expiry', 320000))
                margin_req.futures_expiry = float(data.get('futures_expiry', 215000))
                margin_req.futures_non_expiry = float(data.get('futures_non_expiry', 215000))

            db.session.commit()

            return jsonify({
                'status': 'success',
                'message': f'Margin requirements updated for {instrument}'
            })

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error updating margin requirements: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 400

    # GET request - show requirements page
    requirements = MarginRequirement.query.filter_by(
        user_id=current_user.id
    ).all()

    if not requirements:
        MarginRequirement.get_or_create_defaults(current_user.id)
        requirements = MarginRequirement.query.filter_by(
            user_id=current_user.id
        ).all()

    # Get trade qualities
    trade_qualities = TradeQuality.query.filter_by(
        user_id=current_user.id
    ).all()

    if not trade_qualities:
        TradeQuality.get_or_create_defaults(current_user.id)
        trade_qualities = TradeQuality.query.filter_by(
            user_id=current_user.id
        ).all()

    # Convert to dict for easy template access
    qualities_dict = {q.quality_grade: q for q in trade_qualities}

    # Convert requirements to dict keyed by instrument for easy template access
    requirements_dict = {r.instrument: r for r in requirements}

    # Get option buying premium values (use model defaults as fallback)
    option_buying_premium = MarginRequirement.DEFAULT_OPTION_BUYING_PREMIUM
    sensex_option_buying_premium = MarginRequirement.DEFAULT_SENSEX_OPTION_BUYING_PREMIUM

    nifty_req = requirements_dict.get('NIFTY')
    if nifty_req:
        option_buying_premium = nifty_req.option_buying_premium or MarginRequirement.DEFAULT_OPTION_BUYING_PREMIUM

    sensex_req = requirements_dict.get('SENSEX')
    if sensex_req:
        sensex_option_buying_premium = sensex_req.sensex_option_buying_premium or MarginRequirement.DEFAULT_SENSEX_OPTION_BUYING_PREMIUM

    return render_template('margin/requirements.html',
                         requirements=requirements,
                         requirements_dict=requirements_dict,
                         trade_qualities=trade_qualities,
                         qualities_dict=qualities_dict,
                         option_buying_premium=option_buying_premium,
                         sensex_option_buying_premium=sensex_option_buying_premium)

@margin_bp.route('/qualities', methods=['GET', 'POST'])
@login_required
def qualities():
    """Manage trade quality settings"""
    if request.method == 'POST':
        try:
            data = request.get_json()

            for quality_data in data.get('qualities', []):
                quality = TradeQuality.query.filter_by(
                    user_id=current_user.id,
                    quality_grade=quality_data.get('grade')
                ).first()

                if not quality:
                    quality = TradeQuality(
                        user_id=current_user.id,
                        quality_grade=quality_data.get('grade')
                    )
                    db.session.add(quality)

                quality.margin_percentage = float(quality_data.get('percentage', 50))
                quality.risk_level = quality_data.get('risk_level', 'moderate')
                quality.description = quality_data.get('description', '')
                # Handle margin_source: 'available' for option sellers, 'cash' for option buyers
                quality.margin_source = quality_data.get('margin_source', 'available')

            db.session.commit()

            return jsonify({
                'status': 'success',
                'message': 'Trade qualities updated successfully'
            })

        except Exception as e:
            db.session.rollback()
            logger.error(f"Error updating trade qualities: {e}")
            return jsonify({
                'status': 'error',
                'message': str(e)
            }), 400

    # GET request
    qualities = TradeQuality.query.filter_by(
        user_id=current_user.id
    ).all()

    if not qualities:
        TradeQuality.get_or_create_defaults(current_user.id)
        qualities = TradeQuality.query.filter_by(
            user_id=current_user.id
        ).all()

    return render_template('margin/qualities.html',
                         qualities=qualities)

@margin_bp.route('/calculator')
@login_required
def calculator():
    """Interactive margin and lot size calculator"""
    from app.models import TradingSettings

    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    qualities = TradeQuality.query.filter_by(
        user_id=current_user.id
    ).all()

    # Get lot sizes from TradingSettings
    lot_sizes = {}
    settings = TradingSettings.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    for setting in settings:
        lot_sizes[setting.symbol] = setting.lot_size

    # Create defaults if not exists
    if not settings:
        TradingSettings.get_or_create_defaults(current_user.id)
        settings = TradingSettings.query.filter_by(
            user_id=current_user.id,
            is_active=True
        ).all()
        for setting in settings:
            lot_sizes[setting.symbol] = setting.lot_size

    # Get option buying premium from margin requirements (use model default as fallback)
    option_buying_premium = MarginRequirement.DEFAULT_OPTION_BUYING_PREMIUM
    margin_req = MarginRequirement.query.filter_by(
        user_id=current_user.id,
        instrument='NIFTY'
    ).first()
    if margin_req and margin_req.option_buying_premium:
        option_buying_premium = margin_req.option_buying_premium

    return render_template('margin/calculator.html',
                         accounts=accounts,
                         qualities=qualities,
                         lot_sizes=lot_sizes,
                         option_buying_premium=option_buying_premium)

@margin_bp.route('/calculate-lots', methods=['POST'])
@login_required
@api_rate_limit()
def calculate_lots():
    """API endpoint to calculate lot sizes for both option buying and selling"""
    try:
        data = request.get_json()
        available_margin = data.get('available_margin')
        instrument = data.get('instrument')
        trade_type = data.get('trade_type')
        quality_grade = data.get('quality_grade')
        is_expiry = data.get('is_expiry', False)
        margin_source = data.get('margin_source', 'available')  # 'available' or 'cash'
        premium_per_lot = data.get('premium_per_lot')  # For option buying

        # Validate inputs
        if not available_margin or available_margin <= 0:
            return jsonify({
                'status': 'error',
                'message': 'Invalid margin amount'
            }), 400

        # Calculate lot size with provided margin
        calculator = MarginCalculator(current_user.id)

        # Create a dummy account object for calculation (we only need margin)
        class DummyAccount:
            def __init__(self, margin):
                self.available_margin = margin
                self.id = 0
                self.account_name = "Manual Calculation"

        dummy_account = DummyAccount(available_margin)

        # Get quality percentage from quality grade
        quality = calculator.trade_qualities.get(quality_grade)
        if not quality:
            return jsonify({
                'status': 'error',
                'message': 'Invalid quality grade'
            }), 400

        margin_percentage = quality.margin_percentage / 100

        # Option Buying mode: Use premium_per_lot instead of margin requirements
        if margin_source == 'cash' and trade_type == 'buy':
            # If premium_per_lot not provided, get from database
            if not premium_per_lot:
                margin_req = calculator.margin_requirements.get(instrument)
                if margin_req:
                    if instrument == 'SENSEX':
                        premium_per_lot = margin_req.sensex_option_buying_premium or MarginRequirement.DEFAULT_SENSEX_OPTION_BUYING_PREMIUM
                    else:
                        premium_per_lot = margin_req.option_buying_premium or MarginRequirement.DEFAULT_OPTION_BUYING_PREMIUM
                else:
                    premium_per_lot = MarginRequirement.DEFAULT_OPTION_BUYING_PREMIUM  # Default fallback

            # Calculate: effective_margin / premium_per_lot
            effective_margin = available_margin * margin_percentage
            raw_lot_size = effective_margin / premium_per_lot
            lot_size = int(raw_lot_size)

            details = {
                "available_margin": available_margin,
                "margin_percentage": quality.margin_percentage,
                "effective_margin": effective_margin,
                "margin_per_lot": premium_per_lot,
                "raw_lot_size": raw_lot_size,
                "final_lot_size": lot_size,
                "margin_required": lot_size * premium_per_lot,
                "margin_remaining": available_margin - (lot_size * premium_per_lot),
                "quality_grade": quality_grade,
                "quality_percentage": quality.margin_percentage,
                "is_expiry": is_expiry,
                "calculation": f"Cash {available_margin:,.2f} x {quality.margin_percentage}% / {premium_per_lot:,.2f} = {raw_lot_size:.3f} = {lot_size} lots"
            }

            return jsonify({
                'status': 'success',
                'lot_size': lot_size,
                'details': details
            })

        # Option Selling mode: Use margin requirements table
        lot_size, details = calculator.calculate_lot_size_custom(
            account=dummy_account,
            instrument=instrument,
            trade_type=trade_type,
            margin_percentage=margin_percentage,
            available_margin=available_margin,
            is_expiry=is_expiry,
            margin_source=margin_source
        )

        # Add quality_grade to details for display
        details['quality_grade'] = quality_grade
        details['quality_percentage'] = quality.margin_percentage
        details['is_expiry'] = is_expiry

        return jsonify({
            'status': 'success',
            'lot_size': lot_size,
            'details': details
        })

    except Exception as e:
        logger.error(f"Error calculating lots: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400

@margin_bp.route('/tracker')
@login_required
def tracker():
    """Real-time margin tracker"""
    accounts = TradingAccount.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    trackers = []
    calculator = MarginCalculator(current_user.id)

    for account in accounts:
        # Get or create tracker
        tracker = MarginTracker.query.filter_by(account_id=account.id).first()

        if not tracker:
            # Fetch initial margin data
            available_margin = calculator.get_available_margin(account)
            tracker = MarginTracker(
                account_id=account.id,
                total_available_margin=available_margin,
                free_margin=available_margin
            )
            db.session.add(tracker)
            db.session.commit()

        trackers.append({
            'account': account,
            'tracker': tracker
        })

    return render_template('margin/tracker.html',
                         trackers=trackers)

@margin_bp.route('/refresh-tracker/<int:account_id>', methods=['POST'])
@login_required
@heavy_rate_limit()
def refresh_tracker(account_id):
    """Refresh margin data for specific account"""
    from app.utils.openalgo_client import ExtendedOpenAlgoAPI
    import pytz

    try:
        account = TradingAccount.query.filter_by(
            id=account_id,
            user_id=current_user.id
        ).first()

        if not account:
            return jsonify({
                'status': 'error',
                'message': 'Account not found'
            }), 404

        # Fetch real-time funds data
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )
        response = client.funds()

        if response.get('status') == 'success':
            funds_data = response.get('data', {})

            # Update cached data
            account.last_funds_data = funds_data
            account.last_data_update = datetime.utcnow()
            db.session.commit()

            # Convert to IST
            ist_tz = pytz.timezone('Asia/Kolkata')
            utc_time = pytz.utc.localize(account.last_data_update)
            last_updated_ist = utc_time.astimezone(ist_tz)

            return jsonify({
                'status': 'success',
                'data': {
                    'total_margin': float(funds_data.get('availablecash', 0)) + float(funds_data.get('collateral', 0)),
                    'used_margin': float(funds_data.get('utiliseddebits', 0)),
                    'free_margin': float(funds_data.get('availablecash', 0)) + float(funds_data.get('collateral', 0)) - float(funds_data.get('utiliseddebits', 0)),
                    'available_cash': float(funds_data.get('availablecash', 0)),
                    'collateral': float(funds_data.get('collateral', 0)),
                    'utilized_debits': float(funds_data.get('utiliseddebits', 0)),
                    'm2m_realized': float(funds_data.get('m2mrealized', 0)),
                    'm2m_unrealized': float(funds_data.get('m2munrealized', 0)),
                    'last_updated': last_updated_ist.strftime('%d-%b %I:%M %p IST')
                }
            })
        else:
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch margin data from broker'
            }), 400

    except Exception as e:
        logger.error(f"Error refreshing tracker: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400

@margin_bp.route('/validate-strategy', methods=['POST'])
@login_required
@api_rate_limit()
def validate_strategy():
    """Validate if strategy can be executed with available margin"""
    try:
        data = request.get_json()
        strategy_id = data.get('strategy_id')
        quality_grade = data.get('quality_grade', 'B')

        # Get strategy
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first()

        if not strategy:
            return jsonify({
                'status': 'error',
                'message': 'Strategy not found'
            }), 404

        # Get strategy legs
        legs = StrategyLeg.query.filter_by(
            strategy_id=strategy.id
        ).all()

        # Get selected accounts
        accounts = TradingAccount.query.filter(
            TradingAccount.id.in_(strategy.selected_accounts or []),
            TradingAccount.user_id == current_user.id
        ).all()

        if not accounts:
            return jsonify({
                'status': 'error',
                'message': 'No accounts selected for strategy'
            }), 400

        # Validate margin
        calculator = MarginCalculator(current_user.id)
        validation_results = calculator.validate_margin_for_strategy(
            strategy_legs=legs,
            accounts=accounts,
            quality_grade=quality_grade
        )

        # Determine overall feasibility
        all_feasible = all(
            result['is_feasible']
            for result in validation_results.values()
        )

        return jsonify({
            'status': 'success',
            'is_feasible': all_feasible,
            'validation_results': validation_results
        })

    except Exception as e:
        logger.error(f"Error validating strategy: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400

@margin_bp.route('/update-quality/<string:quality_grade>', methods=['POST'])
@login_required
@api_rate_limit()
def update_trade_quality(quality_grade):
    """Update trade quality settings"""
    try:
        data = request.get_json()

        # Validate quality grade
        if quality_grade not in ['A', 'B', 'C']:
            return jsonify({
                'status': 'error',
                'message': 'Invalid quality grade'
            }), 400

        # Get the trade quality record
        quality = TradeQuality.query.filter_by(
            user_id=current_user.id,
            quality_grade=quality_grade
        ).first()

        if not quality:
            return jsonify({
                'status': 'error',
                'message': 'Quality grade not found'
            }), 404

        # Update fields
        if 'margin_percentage' in data:
            margin_pct = float(data['margin_percentage'])
            if margin_pct < 0 or margin_pct > 100:
                return jsonify({
                    'status': 'error',
                    'message': 'Margin percentage must be between 0 and 100'
                }), 400
            quality.margin_percentage = margin_pct

        if 'risk_level' in data:
            quality.risk_level = data['risk_level']

        if 'margin_source' in data:
            # 'available' for option sellers, 'cash' for option buyers
            quality.margin_source = data['margin_source']

        if 'description' in data:
            quality.description = data['description']

        quality.updated_at = datetime.utcnow()
        db.session.commit()

        logger.debug(f"Updated trade quality {quality_grade} for user {current_user.id}")

        return jsonify({
            'status': 'success',
            'message': f'Grade {quality_grade} settings updated successfully'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating trade quality: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400


@margin_bp.route('/delete-quality/<string:quality_grade>', methods=['DELETE'])
@login_required
@api_rate_limit()
def delete_trade_quality(quality_grade):
    """Delete a custom trade quality grade (cannot delete A, B, C)"""
    try:
        # Prevent deletion of default grades
        if quality_grade in ['A', 'B', 'C']:
            return jsonify({
                'status': 'error',
                'message': 'Cannot delete default grades (A, B, C)'
            }), 400

        # Find and delete the quality
        quality = TradeQuality.query.filter_by(
            user_id=current_user.id,
            quality_grade=quality_grade
        ).first()

        if not quality:
            return jsonify({
                'status': 'error',
                'message': f'Grade {quality_grade} not found'
            }), 404

        db.session.delete(quality)
        db.session.commit()

        logger.debug(f"Deleted trade quality {quality_grade} for user {current_user.id}")

        return jsonify({
            'status': 'success',
            'message': f'Grade {quality_grade} deleted successfully'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error deleting trade quality: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400


@margin_bp.route('/update-option-buying-premium', methods=['POST'])
@login_required
@api_rate_limit()
def update_option_buying_premium():
    """Update option buying premium per lot values"""
    try:
        data = request.get_json()
        option_buying_premium = data.get('option_buying_premium')
        sensex_option_buying_premium = data.get('sensex_option_buying_premium')

        # Validate inputs
        if option_buying_premium is None or float(option_buying_premium) < 0:
            return jsonify({
                'status': 'error',
                'message': 'Invalid NIFTY/BANKNIFTY premium value'
            }), 400

        if sensex_option_buying_premium is None or float(sensex_option_buying_premium) < 0:
            return jsonify({
                'status': 'error',
                'message': 'Invalid SENSEX premium value'
            }), 400

        # Update NIFTY margin requirement
        nifty_req = MarginRequirement.query.filter_by(
            user_id=current_user.id,
            instrument='NIFTY'
        ).first()
        if nifty_req:
            nifty_req.option_buying_premium = float(option_buying_premium)
        else:
            MarginRequirement.get_or_create_defaults(current_user.id)
            nifty_req = MarginRequirement.query.filter_by(
                user_id=current_user.id,
                instrument='NIFTY'
            ).first()
            if nifty_req:
                nifty_req.option_buying_premium = float(option_buying_premium)

        # Update BANKNIFTY margin requirement (same premium as NIFTY)
        banknifty_req = MarginRequirement.query.filter_by(
            user_id=current_user.id,
            instrument='BANKNIFTY'
        ).first()
        if banknifty_req:
            banknifty_req.option_buying_premium = float(option_buying_premium)

        # Update SENSEX margin requirement
        sensex_req = MarginRequirement.query.filter_by(
            user_id=current_user.id,
            instrument='SENSEX'
        ).first()
        if sensex_req:
            sensex_req.sensex_option_buying_premium = float(sensex_option_buying_premium)

        db.session.commit()

        logger.debug(f"Updated option buying premium for user {current_user.id}: NIFTY/BN={option_buying_premium}, SENSEX={sensex_option_buying_premium}")

        return jsonify({
            'status': 'success',
            'message': 'Option buying premium saved successfully'
        })

    except Exception as e:
        db.session.rollback()
        logger.error(f"Error updating option buying premium: {e}")
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 400