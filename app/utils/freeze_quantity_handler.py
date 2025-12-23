"""
Freeze Quantity Handler
Handles automatic split order placement when quantity exceeds freeze limits
"""

import logging
from typing import Dict, Tuple
from app.models import TradingSettings

logger = logging.getLogger(__name__)


def get_freeze_quantity(user_id: int, symbol: str) -> int:
    """
    Get freeze quantity for a symbol

    Args:
        user_id: User ID
        symbol: Symbol name (e.g., 'NIFTY', 'BANKNIFTY', 'SENSEX')

    Returns:
        Freeze quantity limit
    """
    # Extract base symbol from option/futures symbol
    base_symbol = symbol
    for underlying in ['BANKNIFTY', 'NIFTY', 'SENSEX']:  # BANKNIFTY first to avoid matching NIFTY prefix
        if symbol.startswith(underlying):
            base_symbol = underlying
            break

    # Get freeze quantity from trading settings
    setting = TradingSettings.query.filter_by(
        user_id=user_id,
        symbol=base_symbol,
        is_active=True
    ).first()

    if setting:
        logger.debug(f"Freeze quantity for {base_symbol}: {setting.freeze_quantity}")
        return setting.freeze_quantity

    # Default freeze quantities if not found (as per NSE circular Dec 2025)
    defaults = {
        'NIFTY': 1800,
        'BANKNIFTY': 600,
        'SENSEX': 1000
    }

    freeze_qty = defaults.get(base_symbol, 1800)
    logger.warning(f"Using default freeze quantity for {base_symbol}: {freeze_qty}")
    return freeze_qty


def should_split_order(user_id: int, symbol: str, quantity: int) -> Tuple[bool, int]:
    """
    Check if order should be split due to freeze quantity

    Args:
        user_id: User ID
        symbol: Symbol name
        quantity: Order quantity

    Returns:
        Tuple of (should_split: bool, freeze_quantity: int)
    """
    freeze_qty = get_freeze_quantity(user_id, symbol)
    should_split = quantity > freeze_qty

    if should_split:
        logger.debug(f"Order quantity {quantity} exceeds freeze limit {freeze_qty} for {symbol} - will use splitorder")
    else:
        logger.debug(f"Order quantity {quantity} within freeze limit {freeze_qty} for {symbol} - will use placeorder")

    return should_split, freeze_qty


def place_order_with_freeze_check(client, user_id: int, **order_params) -> Dict:
    """
    Place order with automatic freeze quantity handling

    Args:
        client: ExtendedOpenAlgoAPI instance
        user_id: User ID
        **order_params: Order parameters (strategy, symbol, action, exchange, etc.)

    Returns:
        Order response dict
    """
    symbol = order_params.get('symbol')
    quantity = int(order_params.get('quantity', 0))

    # CRITICAL: Reject orders with invalid quantity
    if quantity <= 0:
        logger.error(f"[FREEZE_HANDLER] Rejecting order for {symbol}: quantity={quantity} is invalid (must be > 0)")
        return {
            'status': 'error',
            'message': f'Invalid quantity: {quantity}. Quantity must be greater than 0.',
            'orderid': None
        }

    # Check if we need to split the order
    should_split, freeze_qty = should_split_order(user_id, symbol, quantity)

    if should_split:
        # Use splitorder for large quantities
        logger.debug(f"Placing split order: {quantity} qty with split size {freeze_qty}")

        # Extract parameters for splitorder
        # Build splitorder parameters dynamically based on order type
        splitorder_params = {
            'strategy': order_params.get('strategy', 'AlgoMirror'),
            'symbol': order_params.get('symbol'),
            'exchange': order_params.get('exchange'),
            'action': order_params.get('action'),
            'quantity': quantity,
            'splitsize': freeze_qty,
            'price_type': order_params.get('price_type', 'MARKET'),
            'product': order_params.get('product', 'MIS')
        }

        # Add price/trigger_price based on order type
        price_type = order_params.get('price_type', 'MARKET')

        # For LIMIT orders: price is required
        if price_type == 'LIMIT':
            splitorder_params['price'] = order_params.get('price', 0)

        # For SL/SL-M orders: both price and trigger_price may be needed
        elif price_type in ['SL', 'SL-M']:
            if order_params.get('price'):
                splitorder_params['price'] = order_params.get('price')
            if order_params.get('trigger_price'):
                splitorder_params['trigger_price'] = order_params.get('trigger_price')

        # For MARKET orders: no price or trigger_price needed (already handled above)

        response = client.splitorder(**splitorder_params)

        # Transform splitorder response to match placeorder format
        if response.get('status') == 'success':
            results = response.get('results', [])
            if results:
                # Use the first order ID as the primary order ID
                first_order = results[0]
                return {
                    'status': 'success',
                    'orderid': first_order.get('orderid'),
                    'message': f"Split order placed: {len(results)} orders",
                    'split_order': True,
                    'total_orders': len(results),
                    'split_details': results
                }

        return response
    else:
        # Use regular placeorder
        logger.debug(f"Placing regular order: {quantity} qty")
        return client.placeorder(**order_params)
