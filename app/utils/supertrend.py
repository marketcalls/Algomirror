"""
Supertrend Indicator Module
Uses pandas_ta's Supertrend which matches TradingView Pine Script exactly

Direction convention (matching Pine Script/TradingView):
    - direction = -1: Bullish (Up direction, green) - price above supertrend (lower band)
    - direction = 1: Bearish (Down direction, red) - price below supertrend (upper band)
"""
import numpy as np
import pandas as pd
import pandas_ta as ta
import logging

logger = logging.getLogger(__name__)


def calculate_supertrend(high, low, close, period=7, multiplier=3):
    """
    Calculate Supertrend indicator using pandas_ta (matches TradingView exactly)

    Args:
        high: High price array (numpy array or pandas Series)
        low: Low price array (numpy array or pandas Series)
        close: Close price array (numpy array or pandas Series)
        period: ATR period (default: 7)
        multiplier: ATR multiplier/factor (default: 3)

    Returns:
        Tuple of (trend, direction, long, short)
        - trend: Supertrend line values
        - direction: -1 for bullish (green/up), 1 for bearish (red/down)
        - long: Long (support) line - visible when bullish
        - short: Short (resistance) line - visible when bearish
    """
    try:
        # Create DataFrame for pandas_ta
        df = pd.DataFrame({
            'high': high,
            'low': low,
            'close': close
        })

        # Calculate Supertrend using pandas_ta
        # Returns columns: SUPERT_{length}_{multiplier}, SUPERTd_{length}_{multiplier},
        #                  SUPERTl_{length}_{multiplier}, SUPERTs_{length}_{multiplier}
        st = ta.supertrend(df['high'], df['low'], df['close'], length=period, multiplier=float(multiplier))

        if st is None or st.empty:
            logger.error("pandas_ta supertrend returned None or empty")
            nan_array = np.full(len(close), np.nan)
            return nan_array, nan_array, nan_array, nan_array

        # Extract columns
        col_prefix = f"SUPERT_{period}_{float(multiplier)}"
        trend = st[f'{col_prefix}'].values
        direction_raw = st[f'SUPERTd_{period}_{float(multiplier)}'].values
        long_line = st[f'SUPERTl_{period}_{float(multiplier)}'].values
        short_line = st[f'SUPERTs_{period}_{float(multiplier)}'].values

        # pandas_ta uses: 1 for bullish (up), -1 for bearish (down)
        # Pine Script uses: -1 for bullish, 1 for bearish
        # Convert to match Pine Script convention
        direction = np.where(direction_raw == 1, -1.0, 1.0)

        logger.debug(f"Supertrend calculated: period={period}, multiplier={multiplier}")

        return trend, direction, long_line, short_line

    except Exception as e:
        logger.error(f"Error calculating Supertrend: {e}", exc_info=True)
        nan_array = np.full(len(close), np.nan)
        return nan_array, nan_array, nan_array, nan_array


def get_supertrend_signal(direction):
    """
    Get current Supertrend signal

    Direction convention (matching Pine Script):
        -1 = Bullish (Up direction, green) -> BUY signal
         1 = Bearish (Down direction, red) -> SELL signal

    Args:
        direction: Direction array from calculate_supertrend

    Returns:
        String: 'BUY', 'SELL', or 'NEUTRAL'
    """
    if len(direction) == 0:
        return 'NEUTRAL'

    current_dir = direction[-1]

    if np.isnan(current_dir):
        return 'NEUTRAL'
    elif current_dir == -1:  # Bullish (Pine: direction < 0)
        return 'BUY'
    else:  # Bearish (Pine: direction > 0, i.e., direction == 1)
        return 'SELL'


def calculate_spread_supertrend(leg_prices_dict, high_col='high', low_col='low', close_col='close',
                                period=7, multiplier=3):
    """
    Calculate Supertrend for a combined spread of multiple legs

    Args:
        leg_prices_dict: Dict of {leg_name: DataFrame} with OHLC data
        high_col: Column name for high price
        low_col: Column name for low price
        close_col: Column name for close price
        period: ATR period
        multiplier: ATR multiplier

    Returns:
        Dict with spread OHLC and Supertrend data
    """
    try:
        if not leg_prices_dict:
            logger.error("No leg prices provided")
            return None

        # Calculate combined spread
        combined_high = None
        combined_low = None
        combined_close = None

        for leg_name, df in leg_prices_dict.items():
            if combined_close is None:
                combined_high = df[high_col].copy()
                combined_low = df[low_col].copy()
                combined_close = df[close_col].copy()
            else:
                combined_high += df[high_col]
                combined_low += df[low_col]
                combined_close += df[close_col]

        # Calculate Supertrend on combined spread
        trend, direction, long_line, short_line = calculate_supertrend(
            combined_high.values,
            combined_low.values,
            combined_close.values,
            period=period,
            multiplier=multiplier
        )

        return {
            'high': combined_high,
            'low': combined_low,
            'close': combined_close,
            'supertrend': trend,
            'direction': direction,
            'long': long_line,
            'short': short_line,
            'signal': get_supertrend_signal(direction)
        }

    except Exception as e:
        logger.error(f"Error calculating spread Supertrend: {e}", exc_info=True)
        return None
