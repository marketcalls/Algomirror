"""
TradingView Routes
Spread monitoring with Supertrend indicator
"""
from flask import render_template, request, jsonify
from flask_login import login_required, current_user
from app.tradingview import tradingview_bp
from app.models import Strategy, StrategyLeg
from app.utils.rate_limiter import api_rate_limit
from app.utils.supertrend import calculate_supertrend
import logging
import numpy as np
import pandas as pd
from datetime import datetime, timedelta

logger = logging.getLogger(__name__)


@tradingview_bp.route('/')
@login_required
def index():
    """TradingView spread monitor index page"""
    # Get user's strategies
    strategies = Strategy.query.filter_by(
        user_id=current_user.id,
        is_active=True
    ).all()

    return render_template('tradingview/monitor.html', strategies=strategies)


@tradingview_bp.route('/strategy/<int:strategy_id>')
@login_required
def strategy_monitor(strategy_id):
    """Monitor spread for a specific strategy"""
    from app.models import StrategyExecution

    strategy = Strategy.query.filter_by(
        id=strategy_id,
        user_id=current_user.id
    ).first_or_404()

    # Get strategy legs with their actual placed symbols
    legs = StrategyLeg.query.filter_by(strategy_id=strategy_id).all()

    # Get unique symbols that were actually placed from executions
    executions = StrategyExecution.query.filter_by(
        strategy_id=strategy_id
    ).filter(
        StrategyExecution.symbol.isnot(None)
    ).all()

    # Create a list of unique symbols with their details
    placed_symbols = []
    seen_symbols = set()

    for execution in executions:
        if execution.symbol and execution.symbol not in seen_symbols:
            seen_symbols.add(execution.symbol)

            # Find the corresponding leg to get strike format
            leg = next((l for l in legs if l.id == execution.leg_id), None)
            strike_format = leg.strike_selection if leg else 'N/A'

            placed_symbols.append({
                'symbol': execution.symbol,
                'strike_format': strike_format,
                'exchange': execution.exchange,
                'status': execution.status,
                'leg_number': leg.leg_number if leg else None
            })

    logger.info(f"Found {len(placed_symbols)} unique placed symbols for strategy {strategy_id}")

    return render_template('tradingview/strategy_monitor.html',
                         strategy=strategy,
                         legs=legs,
                         placed_symbols=placed_symbols)


@tradingview_bp.route('/api/chart-data/<int:strategy_id>')
@login_required
@api_rate_limit()
def get_chart_data(strategy_id):
    """
    Get chart data with Supertrend for a strategy

    Query Parameters:
        - interval: Timeframe (1m, 5m, 15m - default: from strategy settings or 5m)
        - days: Number of days to load (1-5, default: 3)
        - period: Supertrend ATR period (default: from strategy settings or 7)
        - multiplier: Supertrend multiplier (default: from strategy settings or 3)
    """
    try:
        strategy = Strategy.query.filter_by(
            id=strategy_id,
            user_id=current_user.id
        ).first_or_404()

        # Get parameters - use strategy's Supertrend settings as defaults
        interval = request.args.get('interval', strategy.supertrend_timeframe or '5m')
        days = int(request.args.get('days', 3))
        period = int(request.args.get('period', strategy.supertrend_period or 7))
        multiplier = float(request.args.get('multiplier', strategy.supertrend_multiplier or 3.0))

        # Validate parameters
        if interval not in ['3m', '5m', '10m', '15m']:
            interval = '5m'
        if days < 1 or days > 5:
            days = 3

        # Get strategy legs
        legs = StrategyLeg.query.filter_by(strategy_id=strategy_id).all()

        if not legs:
            return jsonify({
                'status': 'error',
                'message': 'No legs found for strategy'
            }), 400

        # Fetch real historical data from OpenAlgo (no simulation fallback)
        spread_data = fetch_spread_historical_data(strategy, legs, interval, days)

        if spread_data is None or spread_data.empty:
            return jsonify({
                'status': 'error',
                'message': 'Failed to fetch real historical data from OpenAlgo. Please ensure your trading account is connected and the symbols are valid.'
            }), 500

        # Calculate Supertrend
        high = spread_data['high'].values
        low = spread_data['low'].values
        close = spread_data['close'].values

        trend, direction, long, short = calculate_supertrend(
            high, low, close, period=period, multiplier=multiplier
        )

        # Prepare chart data
        # IST offset: UTC+5:30 = 5*3600 + 30*60 = 19800 seconds
        IST_OFFSET = 19800
        chart_data = []
        for i, timestamp in enumerate(spread_data.index):
            # Skip initial NaN values
            if i < period:
                continue

            chart_data.append({
                'time': int(timestamp.timestamp()) + IST_OFFSET,  # Convert to IST for display
                'open': float(spread_data['open'].iloc[i]),
                'high': float(spread_data['high'].iloc[i]),
                'low': float(spread_data['low'].iloc[i]),
                'close': float(spread_data['close'].iloc[i]),
                'supertrend': float(trend[i]) if not np.isnan(trend[i]) else None,
                'direction': int(direction[i]) if not np.isnan(direction[i]) else 0
            })

        # Get current signal
        current_direction = direction[-1] if len(direction) > 0 else 0
        if np.isnan(current_direction):
            signal = 'NEUTRAL'
        elif current_direction > 0:
            signal = 'BUY'
        else:
            signal = 'SELL'

        logger.info(f"Returning {len(chart_data)} bars of REAL OHLC data to frontend with Supertrend signal: {signal}")

        return jsonify({
            'status': 'success',
            'data': chart_data,
            'signal': signal,
            'strategy_name': strategy.name,
            'legs_count': len(legs),
            'period': period,
            'multiplier': multiplier
        })

    except Exception as e:
        logger.error(f"Error generating chart data for strategy {strategy_id}: {e}", exc_info=True)
        return jsonify({
            'status': 'error',
            'message': str(e)
        }), 500


def fetch_spread_historical_data(strategy, legs, interval='5m', days=3):
    """
    Fetch real historical data from OpenAlgo and combine into spread

    Args:
        strategy: Strategy object
        legs: List of StrategyLeg objects
        interval: Timeframe (1m, 5m, 15m)
        days: Number of days to fetch

    Returns:
        pandas DataFrame with combined spread OHLC data
    """
    try:
        from app.models import TradingAccount
        from app.utils.openalgo_client import ExtendedOpenAlgoAPI

        # Calculate date range
        end_date = datetime.now()
        start_date = end_date - timedelta(days=days)

        # Format dates for OpenAlgo API (YYYY-MM-DD)
        start_date_str = start_date.strftime('%Y-%m-%d')
        end_date_str = end_date.strftime('%Y-%m-%d')

        # Get a trading account to fetch data
        # Use first selected account or any active account
        account_ids = strategy.selected_accounts or []
        account = None

        if account_ids:
            account = TradingAccount.query.filter_by(
                id=account_ids[0],
                user_id=strategy.user_id,
                is_active=True
            ).first()

        if not account:
            # Fallback to any active account
            account = TradingAccount.query.filter_by(
                user_id=strategy.user_id,
                is_active=True
            ).first()

        if not account:
            logger.error(f"No active trading account found for user {strategy.user_id}")
            return None

        # Initialize OpenAlgo client
        client = ExtendedOpenAlgoAPI(
            api_key=account.get_api_key(),
            host=account.host_url
        )

        # Get actual placed symbols from executions
        from app.models import StrategyExecution
        executions = StrategyExecution.query.filter_by(
            strategy_id=strategy.id
        ).filter(
            StrategyExecution.symbol.isnot(None)
        ).all()

        # Map leg_id to actual placed symbol (quantity will come from leg.lots)
        leg_symbols = {}
        for execution in executions:
            if execution.leg_id not in leg_symbols:
                leg_symbols[execution.leg_id] = {
                    'symbol': execution.symbol,
                    'exchange': execution.exchange or 'NSE'
                }

        # Fetch historical data for each leg using actual placed symbols
        # Store data with leg action (BUY/SELL) for proper spread calculation
        leg_data_dict = {}

        for leg in legs:
            try:
                # Try to get actual placed symbol first, otherwise fall back to base instrument
                if leg.id in leg_symbols:
                    symbol = leg_symbols[leg.id]['symbol']
                    exchange = leg_symbols[leg.id]['exchange']
                    logger.info(f"Using ACTUAL placed symbol: {symbol}")
                else:
                    symbol = leg.instrument
                    exchange = 'NSE'
                    logger.warning(f"No placed symbol found for leg {leg.leg_number}, using base instrument: {symbol}")

                logger.info(f"Fetching {interval} data for {symbol} from {start_date_str} to {end_date_str}")

                # Fetch historical data from OpenAlgo
                response = client.history(
                    symbol=symbol,
                    exchange=exchange,
                    interval=interval,
                    start_date=start_date_str,
                    end_date=end_date_str
                )

                # OpenAlgo returns a pandas DataFrame with OHLC data
                if isinstance(response, pd.DataFrame) and not response.empty:
                    # Verify we have required OHLC columns
                    required_cols = ['open', 'high', 'low', 'close']
                    if all(col in response.columns for col in required_cols):
                        # Use lot size (number of lots) from leg, not quantity
                        lots = leg.lots or 1  # Default to 1 lot if not set

                        # Store data with leg action and lots for spread calculation
                        leg_data_dict[f"{leg.leg_number}_{symbol}"] = {
                            'data': response,
                            'action': leg.action,  # BUY or SELL
                            'lots': lots,  # Number of lots (NOT total quantity)
                            'leg_number': leg.leg_number
                        }
                        logger.info(f"Fetched REAL {len(response)} bars for leg {leg.leg_number} ({symbol}) - {leg.action} x{lots} lots - OHLC data from OpenAlgo")
                        logger.info(f"  Sample data - Open: {response['open'].iloc[-1]:.2f}, High: {response['high'].iloc[-1]:.2f}, Low: {response['low'].iloc[-1]:.2f}, Close: {response['close'].iloc[-1]:.2f}")
                    else:
                        logger.error(f"Missing OHLC columns in response for {symbol}. Columns: {response.columns.tolist()}")
                elif isinstance(response, dict):
                    # API returned error response
                    error_msg = response.get('message', str(response))
                    logger.error(f"OpenAlgo API error for {symbol}: {error_msg}")
                    logger.info(f"Full error response: {response}")

                    # Try falling back to base instrument (NIFTY/BANKNIFTY)
                    if leg.id in leg_symbols:
                        logger.warning(f"Option symbol {symbol} failed, trying base instrument {leg.instrument}")
                        try:
                            fallback_response = client.history(
                                symbol=leg.instrument,
                                exchange='NSE',
                                interval=interval,
                                start_date=start_date_str,
                                end_date=end_date_str
                            )
                            if isinstance(fallback_response, pd.DataFrame) and not fallback_response.empty:
                                required_cols = ['open', 'high', 'low', 'close']
                                if all(col in fallback_response.columns for col in required_cols):
                                    lots = leg.lots or 1
                                    leg_data_dict[f"{leg.leg_number}_{leg.instrument}"] = {
                                        'data': fallback_response,
                                        'action': leg.action,
                                        'lots': lots,
                                        'leg_number': leg.leg_number
                                    }
                                    logger.info(f"Fetched {len(fallback_response)} bars for leg {leg.leg_number} using fallback instrument ({leg.instrument})")
                        except Exception as fallback_error:
                            logger.error(f"Fallback to base instrument also failed: {fallback_error}")
                else:
                    logger.error(f"No valid DataFrame returned for {symbol} - Response type: {type(response)}")

            except Exception as e:
                logger.error(f"Error fetching data for leg {leg.leg_number} ({leg.instrument}): {e}")
                continue

        # If we couldn't fetch any data, return None
        if not leg_data_dict:
            logger.error("No real data fetched from OpenAlgo - Cannot proceed without real market data")
            return None

        # Combine OHLC data from multiple legs into spread
        # For Supertrend to work correctly, we need proper high/low values, not just close
        logger.info(f"Calculating combined spread OHLC from {len(leg_data_dict)} legs...")

        # Initialize combined OHLC
        common_index = None
        combined_open = None
        combined_high = None
        combined_low = None
        combined_close = None

        for leg_name, leg_info in leg_data_dict.items():
            df = leg_info['data']
            action = leg_info['action']
            leg_number = leg_info['leg_number']

            # Get OHLC data
            leg_open = df['open'].copy()
            leg_high = df['high'].copy()
            leg_low = df['low'].copy()
            leg_close = df['close'].copy()

            # Apply sign based on action: SELL = positive, BUY = negative
            if action == 'BUY':
                leg_open = -leg_open
                leg_high = -leg_high
                leg_low = -leg_low
                leg_close = -leg_close
                logger.info(f"  Leg {leg_number}: BUY (negative contribution)")
            else:
                logger.info(f"  Leg {leg_number}: SELL (positive contribution)")

            # Build common index (intersection of all timestamps)
            if common_index is None:
                common_index = df.index
                combined_open = leg_open
                combined_high = leg_high
                combined_low = leg_low
                combined_close = leg_close
            else:
                common_index = common_index.intersection(df.index)
                combined_open = combined_open.add(leg_open, fill_value=0)
                combined_high = combined_high.add(leg_high, fill_value=0)
                combined_low = combined_low.add(leg_low, fill_value=0)
                combined_close = combined_close.add(leg_close, fill_value=0)

        if common_index is None or len(common_index) == 0:
            logger.error("No valid leg data found or no common timestamps")
            return None

        # Reindex to common timestamps
        combined_open = combined_open.reindex(common_index)
        combined_high = combined_high.reindex(common_index)
        combined_low = combined_low.reindex(common_index)
        combined_close = combined_close.reindex(common_index)

        # Take absolute values for spread (spread value should be positive)
        combined_open = combined_open.abs()
        combined_high = combined_high.abs()
        combined_low = combined_low.abs()
        combined_close = combined_close.abs()

        # Ensure high >= low after abs() transformation
        # After taking absolute value, the original high/low relationship might be inverted
        actual_high = pd.concat([combined_high, combined_low], axis=1).max(axis=1)
        actual_low = pd.concat([combined_high, combined_low], axis=1).min(axis=1)

        # Create OHLC DataFrame with proper high/low for ATR calculation
        combined_df = pd.DataFrame({
            'open': combined_open,
            'high': actual_high,
            'low': actual_low,
            'close': combined_close
        })

        logger.info(f"  Combined spread OHLC with proper high/low values for Supertrend")

        if combined_df is None or combined_df.empty:
            logger.error("Failed to calculate spread")
            return None

        logger.info(f"Successfully combined {len(leg_data_dict)} legs into spread OHLC with {len(combined_df)} bars (REAL DATA)")
        logger.info(f"  Latest spread - Open: {combined_df['open'].iloc[-1]:.2f}, High: {combined_df['high'].iloc[-1]:.2f}, Low: {combined_df['low'].iloc[-1]:.2f}, Close: {combined_df['close'].iloc[-1]:.2f}")

        return combined_df

    except Exception as e:
        logger.error(f"Error fetching spread historical data: {e}", exc_info=True)
        return None


