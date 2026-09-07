"""
Equity module ratio engine.

Pure calculation helpers for the equity (CNC delivery) module: order quantity
ratios, splitting a total quantity across accounts, invested percent (Dashboard)
and stake percent (Holdings).

This module is deliberately pure Python. It must not import Flask, SQLAlchemy or
anything from the app package. The route layer converts ORM rows into plain
numbers, dicts and lists before calling in here, which keeps every formula in the
module unit-testable without a database or an application context.

Units and conventions:
    - Allocation, holdings value and cost figures are rupee amounts (float).
    - Allocation is the manually set investable corpus for an account. The live
      Available Cash of an account is never an input to any formula here.
    - Every function whose name ends in "_percent" or that returns a ratio returns
      a PERCENT value, so 40.0 means 40 percent, not 0.40.
    - Nothing is pre-rounded. Rounding is a presentation concern and belongs to
      the caller or the template.
    - A zero, missing or negative denominator yields 0.0. These functions never
      raise ZeroDivisionError and never return infinity or NaN.

Formulas implemented (PRD sections 9.1 to 9.3):
    ratio(account)        = allocation(account) / total allocation of active accounts
    invested_pct(account) = holdings_value(account) / allocation(account)
    stake_pct(stock)      = stock_at_cost / stake denominator
                            (all active accounts total, or one account's own
                            allocation when the view is filtered to that account)
"""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from typing import Any, Dict, Hashable, Mapping, Optional

# Tolerance applied before flooring a quantity to whole lots. Binary floating
# point can render an exact share as 99.99999999999999, and flooring that would
# drop a full lot for no reason. The tolerance is far smaller than one lot, so a
# genuine shortfall is still floored down.
_FLOOR_TOLERANCE = 1e-9


def _as_amount(value: Any) -> float:
    """
    Coerce a rupee amount to a non-negative finite float.

    None, non-numeric junk, NaN, infinity and negative values all become 0.0.
    An investable corpus, a holdings value and a cost basis cannot be negative,
    so clamping keeps a bad row from poisoning a whole account set.
    """
    if value is None:
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount) or amount < 0.0:
        return 0.0
    return amount


def _as_ratio(value: Any) -> float:
    """Coerce a ratio to a non-negative finite percent value (40.0 means 40 percent)."""
    return _as_amount(value)


def _as_quantity(value: Any) -> int:
    """Coerce a quantity to a non-negative whole number, rounding down."""
    if value is None:
        return 0
    try:
        quantity = float(value)
    except (TypeError, ValueError):
        return 0
    if not math.isfinite(quantity) or quantity <= 0.0:
        return 0
    return int(math.floor(quantity + _FLOOR_TOLERANCE))


def _as_lot_size(value: Any) -> int:
    """Coerce a lot size to a whole number of at least 1. Missing means 1."""
    lot_size = _as_quantity(value)
    return lot_size if lot_size >= 1 else 1


def _as_signed_amount(value: Any) -> float:
    """
    Coerce a rupee amount to a finite float, KEEPING its sign.

    Use this for figures that are legitimately negative, a profit and loss
    number above all. Unlike _as_amount it never clamps, because a loss has to
    stay a loss.
    """
    if value is None:
        return 0.0
    try:
        amount = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(amount):
        return 0.0
    return amount


def percent_of(numerator: Any, denominator: Any) -> float:
    """
    Return numerator / denominator as a PERCENT value.

    Both sides are treated as magnitudes: a negative numerator clamps to zero,
    which is correct for a cost basis, a holdings value or a pledged quantity,
    none of which can be negative. For a signed figure such as a profit and
    loss number use signed_percent_of instead, or a loss will read as 0.0.

    A zero, missing or negative denominator returns 0.0 instead of raising.
    """
    denom = _as_amount(denominator)
    if denom <= 0.0:
        return 0.0
    return (_as_amount(numerator) / denom) * 100.0


def signed_percent_of(numerator: Any, denominator: Any) -> float:
    """
    Return numerator / denominator as a PERCENT value, keeping the numerator's sign.

    The denominator stays a magnitude, so a zero, missing or negative
    denominator returns 0.0 instead of raising.
    """
    denom = _as_amount(denominator)
    if denom <= 0.0:
        return 0.0
    return (_as_signed_amount(numerator) / denom) * 100.0


def total_allocation(allocations: Mapping[Hashable, Any]) -> float:
    """
    Sum the allocation of every account in the mapping.

    The caller passes ACTIVE accounts only. An empty mapping totals 0.0.
    """
    if not allocations:
        return 0.0
    return sum(_as_amount(amount) for amount in allocations.values())


def compute_order_qty_ratios(allocations: Mapping[Hashable, Any]) -> Dict[Hashable, float]:
    """
    Compute the Order Qty Ratio of every account as a PERCENT.

        ratio(account) = allocation(account) / sum(allocation of all active accounts)

    Args:
        allocations: mapping of account key (id, name, whatever the caller uses)
            to the account's equity fund allocation in rupees. The caller passes
            ACTIVE accounts only, and recomputes whenever any allocation changes.

    Returns:
        Mapping of the same account keys, in the same order, to a percent value.
        Ratios are expected to total 100 percent. When the total allocation is
        zero every ratio is 0.0: the zero denominator is never divided by and
        never raises.

    Example:
        20L, 10L, 10L, 5L, 5L returns 40.0, 20.0, 20.0, 10.0, 10.0.
    """
    if not allocations:
        return {}

    total = total_allocation(allocations)
    if total <= 0.0:
        return {key: 0.0 for key in allocations}

    return {key: percent_of(amount, total) for key, amount in allocations.items()}


@dataclass(frozen=True)
class QuantitySplit:
    """
    Result of splitting a total quantity across accounts.

    Attributes:
        quantities: account key to the whole quantity allocated to that account,
            already rounded DOWN to that account's tradable lot, then topped up
            from the rounding remainder so the parts add to the whole.
        leftover: the quantity that STILL could not be allocated after that top
            up. With ordinary lot sizes it is always zero; it can only be
            non-zero when a lot size is larger than what is left, which is a
            genuine constraint rather than a rounding artefact.
    """

    quantities: Dict[Hashable, int] = field(default_factory=dict)
    leftover: int = 0

    @property
    def allocated(self) -> int:
        """Total quantity actually allocated across all accounts."""
        return sum(self.quantities.values())


def split_quantity_by_ratio(
    total_quantity: Any,
    ratios: Mapping[Hashable, Any],
    lot_sizes: Optional[Mapping[Hashable, Any]] = None,
) -> QuantitySplit:
    """
    Split a total quantity across accounts by Order Qty Ratio.

    Each account gets total_quantity * ratio / 100, rounded DOWN to the nearest
    tradable lot. Flooring every account always loses something: 100 shares
    across 66.67 and 33.33 percent gives 66 and 33, and the hundredth share
    goes nowhere. Asking for 100 and getting 99 is not what was asked for.

    So the shares left by that rounding are handed back out, one lot at a time,
    to the account with the LARGEST FRACTIONAL REMAINDER - the one that came
    closest to earning another share and was rounded down hardest. This is the
    largest remainder method, and it is the standard way of making rounded parts
    add up to their whole.

    Ties are broken by the larger allocation, then by the order the accounts
    were given, so the same inputs always produce the same split. That matters
    more than it sounds: the preview the admin approves and the order actually
    sent are compared for equality, and a split that could vary between two
    identical passes would fail that comparison at random.

    Args:
        total_quantity: total shares to distribute. Fractions round down. Zero or
            negative yields all zeros with no leftover.
        ratios: account key to ratio as a PERCENT (40.0 means 40 percent),
            normally the output of compute_order_qty_ratios. Insertion order
            decides who is served first when capacity runs short, so pass an
            ordered mapping if the order matters.
        lot_sizes: optional account key to tradable lot size. A missing, zero or
            invalid entry is treated as a lot size of 1.

    Returns:
        QuantitySplit with per-account quantities and whatever still could not
        be allocated. The allocated total never exceeds total_quantity, so
        leftover is never negative even if the supplied ratios total more than
        100 percent.
    """
    quantities: Dict[Hashable, int] = {key: 0 for key in (ratios or {})}
    total = _as_quantity(total_quantity)
    if total <= 0 or not ratios:
        return QuantitySplit(quantities=quantities, leftover=total)

    remaining = total
    remainders: Dict[Hashable, float] = {}
    order: Dict[Hashable, int] = {}

    for position, (key, ratio) in enumerate(ratios.items()):
        lot_size = _as_lot_size(lot_sizes.get(key) if lot_sizes else None)
        share = total * _as_ratio(ratio) / 100.0
        if share > remaining:
            share = float(remaining)
        lots = math.floor(share / lot_size + _FLOOR_TOLERANCE)
        quantity = int(lots * lot_size) if lots > 0 else 0
        quantities[key] = quantity
        # How much of a lot this account was rounded down by. This is what
        # decides who gets the leftover back.
        remainders[key] = (share - quantity) / lot_size
        order[key] = position
        remaining -= quantity

    # Hand the rounding remainder back out, one lot at a time.
    #
    # Sorted once and walked in order rather than re-sorted each pass: an
    # account that has just been topped up has had its claim met, and letting
    # it win again on the same fractional part would give one account the whole
    # remainder while the next was rounded down and ignored.
    if remaining > 0:
        claimants = sorted(
            (key for key in quantities if remainders.get(key, 0.0) > 0),
            key=lambda key: (-remainders[key], -quantities[key], order[key]),
        )
        for key in claimants:
            lot_size = _as_lot_size(lot_sizes.get(key) if lot_sizes else None)
            if lot_size > remaining:
                continue
            quantities[key] += lot_size
            remaining -= lot_size
            if remaining <= 0:
                break

    return QuantitySplit(quantities=quantities, leftover=remaining)


def invested_percent(holdings_value: Any, allocation: Any) -> float:
    """
    Invested percent for one account on the Dashboard (M1).

        invested_pct(account) = holdings_value(account) / allocation(account)

    Args:
        holdings_value: current market value of the account's equity holdings.
        allocation: the account's equity fund allocation in rupees.

    Returns:
        Percent value. An allocation of zero yields 0.0, not an error and not
        infinity.

    Example:
        14.20L of holdings against a 20.00L allocation returns 71.0.
    """
    return percent_of(holdings_value, allocation)


def stock_at_cost(avg_cost: Any, quantity: Any) -> float:
    """
    Cost basis of a holding: avg_cost * quantity.

    Sum this across the accounts in view to get the stock_at_cost that feeds
    stake_percent.
    """
    return _as_amount(avg_cost) * float(_as_quantity(quantity))


def stake_denominator(
    allocations: Mapping[Hashable, Any],
    account_key: Optional[Hashable] = None,
) -> float:
    """
    Pick the correct denominator for stake percent, so the two Holdings view
    modes cannot diverge.

    Args:
        allocations: mapping of account key to allocation in rupees, covering ALL
            active accounts.
        account_key: None for the All Accounts view, which uses the total
            allocation across all active accounts. Otherwise the key of the one
            account being viewed, which uses that account's own allocation.

    Returns:
        The denominator in rupees. An unknown account key yields 0.0, which in
        turn makes stake percent 0.0 rather than raising.
    """
    if account_key is None:
        return total_allocation(allocations)
    return _as_amount((allocations or {}).get(account_key))


def stake_percent(stock_at_cost_value: Any, denominator: Any) -> float:
    """
    Stake percent for one stock on the Holdings screen (M7).

        stake_pct(stock) = stock_at_cost / denominator

    stock_at_cost is avg_cost * qty summed over the accounts in view. The
    denominator comes from stake_denominator: the total allocation of all active
    accounts in the All Accounts view, or that one account's own allocation when
    the view is filtered to a single account.

    Returns:
        Percent value. A zero denominator yields 0.0.

    Example:
        RELIANCE at 385 qty and 1298.40 average cost, against a 50L total
        allocation, returns 10.0 (to two decimals).
    """
    return percent_of(stock_at_cost_value, denominator)


def stake_percent_for_view(
    stock_at_cost_value: Any,
    allocations: Mapping[Hashable, Any],
    account_key: Optional[Hashable] = None,
) -> float:
    """
    Stake percent resolved for the current Holdings view in one call.

    Prefer this over calling stake_denominator and stake_percent separately: it
    is the single place that decides the denominator, so the All Accounts view
    and the single account view cannot drift apart.

    Args:
        stock_at_cost_value: avg_cost * qty summed over the accounts in view.
        allocations: mapping of account key to allocation for ALL active accounts.
        account_key: None for All Accounts, otherwise the account being viewed.
    """
    return stake_percent(stock_at_cost_value, stake_denominator(allocations, account_key))


# A pledged holding is lodged as collateral at a haircut, so the margin it
# yields is worth less than the stock behind it. Grossing the collateral back
# up by this factor recovers the value of the stock actually pledged. Brokers
# commonly apply about 10 percent for liquid equity, hence 0.90.
DEFAULT_PLEDGE_HAIRCUT = 0.90


def pledge_percent(
    collateral: Any,
    holdings_value: Any,
    haircut: Any = DEFAULT_PLEDGE_HAIRCUT,
) -> float:
    """
    Return the share of a holdings value that is pledged, as a PERCENT.

        pledge percent = (collateral / haircut) / holdings_value

    Collateral is the account's available margin minus its raw cash, which is
    how those two figures are quoted on the dashboard. Worked example from the
    live server, in lakh: available margin 47.33, cash 46.39, so collateral is
    0.94; 0.94 / 0.90 is 1.04444; against a holdings value of 1.03 that is
    101 percent.

    The result is deliberately NOT capped at 100. A real haircut that differs
    from the assumed one, or a holdings value that has moved since the stock
    was pledged, can legitimately push it a little above 100 percent, and
    clamping would hide that rather than report it.

    A missing or non-positive collateral, haircut or holdings value returns 0.0
    rather than raising.
    """
    pledged_collateral = _as_amount(collateral)
    if pledged_collateral <= 0.0:
        return 0.0
    rate = _as_amount(haircut)
    if rate <= 0.0:
        return 0.0
    return percent_of(pledged_collateral / rate, holdings_value)


def collateral_from_margin(available_margin: Any, available_cash: Any) -> float:
    """
    Collateral implied by an account's available margin and its raw cash.

    Kept next to pledge_percent so the two halves of the dashboard formula sit
    together. A negative difference (cash reported above margin, which some
    brokers do transiently) is treated as zero collateral, not as a negative
    pledge.
    """
    return max(0.0, _as_amount(available_margin) - _as_amount(available_cash))
