"""
Supertrend Indicator Module
Uses OpenAlgo's ta.supertrend which matches TradingView/Pine Script exactly

Direction convention (matching Pine Script/TradingView):
    - direction = -1: Bullish (Up direction, green) - price above supertrend
    - direction = 1: Bearish (Down direction, red) - price below supertrend
    - direction = 0/NaN: No signal (warmup period)
"""
import numpy as np
import pandas as pd
import logging

logger = logging.getLogger(__name__)


def calculate_supertrend(high, low, close, period=7, multiplier=3):
    """
    Calculate Supertrend indicator using OpenAlgo's ta.supertrend
    which matches TradingView/Pine Script exactly

    Args:
        high: High price array (numpy array or pandas Series)
        low: Low price array (numpy array or pandas Series)
        close: Close price array (numpy array or pandas Series)
        period: ATR period (default: 7)
        multiplier: ATR multiplier/factor (default: 3)

    Returns:
        Tuple of (trend, direction, long, short)
        - trend: Supertrend line values (numpy array)
        - direction: 0 for no signal, -1 for bullish, 1 for bearish (numpy int32 array)
        - long: Long (support) line - visible when bullish
        - short: Short (resistance) line - visible when bearish
    """
    try:
        from openalgo import ta

        # Create DataFrame for easy calculation
        df = pd.DataFrame({
            'high': high,
            'low': low,
            'close': close
        })

        # Use OpenAlgo's ta.supertrend - matches TradingView exactly
        df['supertrend'], df['direction'] = ta.supertrend(
            df['high'], df['low'], df['close'],
            period=period, multiplier=multiplier
        )

        # Convert to numpy arrays
        trend = df['supertrend'].values
        direction_raw = df['direction'].values

        # Convert direction: NaN -> 0, keep -1 and 1
        direction = np.zeros(len(direction_raw), dtype=np.int32)
        for i in range(len(direction_raw)):
            if pd.isna(direction_raw[i]):
                direction[i] = 0  # No signal (warmup)
            else:
                direction[i] = int(direction_raw[i])

        # Create long/short arrays for compatibility
        n = len(trend)
        long = np.full(n, np.nan)
        short = np.full(n, np.nan)

        for i in range(n):
            if not np.isnan(trend[i]):
                if direction[i] == -1:  # Bullish
                    long[i] = trend[i]
                elif direction[i] == 1:  # Bearish
                    short[i] = trend[i]

        logger.debug(f"Supertrend calculated using OpenAlgo ta: period={period}, multiplier={multiplier}")

        return trend, direction, long, short

    except ImportError:
        logger.error("OpenAlgo library not installed. Please install with: pip install openalgo")
        n = len(close)
        nan_array = np.full(n, np.nan)
        return nan_array, np.zeros(n, dtype=np.int32), nan_array, nan_array
    except Exception as e:
        logger.error(f"Error calculating Supertrend: {e}", exc_info=True)
        n = len(close) if hasattr(close, '__len__') else 0
        nan_array = np.full(n, np.nan)
        return nan_array, np.zeros(n, dtype=np.int32), nan_array, nan_array


def get_supertrend_signal(direction):
    """
    Get current Supertrend signal

    Direction convention (matching Pine Script/TradingView):
         0 = No signal (warmup period) -> NEUTRAL
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

    if current_dir == 0:  # No signal (warmup period)
        return 'NEUTRAL'
    elif current_dir == -1:  # Bullish
        return 'BUY'
    elif current_dir == 1:  # Bearish
        return 'SELL'
    else:
        return 'NEUTRAL'


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
        from openalgo import ta

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

        # Create spread DataFrame
        spread_df = pd.DataFrame({
            'high': combined_high,
            'low': combined_low,
            'close': combined_close
        })

        # Calculate Supertrend using OpenAlgo
        spread_df['supertrend'], spread_df['direction'] = ta.supertrend(
            spread_df['high'], spread_df['low'], spread_df['close'],
            period=period, multiplier=multiplier
        )

        # Convert direction NaN to 0
        direction = spread_df['direction'].fillna(0).astype(int).values

        return {
            'high': combined_high,
            'low': combined_low,
            'close': combined_close,
            'supertrend': spread_df['supertrend'].values,
            'direction': direction,
            'long': np.where(direction == -1, spread_df['supertrend'].values, np.nan),
            'short': np.where(direction == 1, spread_df['supertrend'].values, np.nan),
            'signal': get_supertrend_signal(direction)
        }

    except Exception as e:
        logger.error(f"Error calculating spread Supertrend: {e}", exc_info=True)
        return None
