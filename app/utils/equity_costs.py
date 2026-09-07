"""
Equity module brokerage and statutory cost engine.

Pure calculation helpers for the equity (CNC delivery) module: estimated costs
per order, gross P&L and net P&L. These drive the Est. Costs and Net P&L figures
on the Holdings screen and anywhere else a cost estimate is shown.

This module is deliberately pure Python. It must not import Flask, SQLAlchemy or
anything from the app package. The route layer loads the rate row that is in
effect for the account and broker (rates are versioned by effective date, because
a rate change applies to future calculations only) and passes plain numbers here.

UNITS, READ THIS FIRST:
    Every field on BrokerageRates whose name ends in "_pct" is a PERCENT value.
    stt_pct=0.10 means 0.10 percent (that is, 0.001 of turnover), and gst_pct=18.0
    means 18 percent. This module divides by 100 internally. Never pass 0.001 for
    "0.10 percent": it would silently under-charge by a factor of 100.

    brokerage_per_order and dp_amc_charge are RUPEE amounts, not percentages.

Cost formula (PRD sections 9.1 to 9.3):
    est_costs = brokerage
              + stt          on turnover
              + exchange_txn on turnover
              + sebi         on turnover
              + stamp_duty   on BUY turnover only
              + gst          gst_pct of (brokerage + exchange txn + SEBI + DP)
              + dp_amc       on SELL only, per scrip
    gross_pnl = (ltp - avg_cost) * quantity
    net_pnl   = gross_pnl - est_costs
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any, Union

# Order sides recognised by the cost engine. Defined once so the literals are not
# scattered across the module and its callers.
SIDE_BUY = "BUY"
SIDE_SELL = "SELL"

_BUY_ALIASES = frozenset({"BUY", "B", "LONG"})
_SELL_ALIASES = frozenset({"SELL", "S", "SHORT"})


def _as_number(value: Any) -> float:
    """Coerce a value to a finite float. None, junk, NaN and infinity become 0.0."""
    if value is None:
        return 0.0
    try:
        number = float(value)
    except (TypeError, ValueError):
        return 0.0
    if not math.isfinite(number):
        return 0.0
    return number


def _as_non_negative(value: Any) -> float:
    """Coerce to a finite float of at least 0.0. Rates and turnover cannot be negative."""
    number = _as_number(value)
    return number if number > 0.0 else 0.0


def normalise_side(side: Any) -> str:
    """
    Normalise an order side to SIDE_BUY or SIDE_SELL.

    Accepts any case and the common short forms (B, S). Raises ValueError on an
    unrecognised side, because guessing between buy and sell would silently apply
    the wrong stamp duty and DP charges.
    """
    text = str(side or "").strip().upper()
    if text in _BUY_ALIASES:
        return SIDE_BUY
    if text in _SELL_ALIASES:
        return SIDE_SELL
    raise ValueError("Unknown order side: %r. Expected BUY or SELL." % (side,))


@dataclass(frozen=True)
class BrokerageRates:
    """
    Brokerage and statutory rates for one account and broker.

    Configured once per account and broker, and versioned by effective date by the
    caller: a rate change applies to future cost calculations only.

    Rupee amounts:
        brokerage_per_order: flat brokerage charged for the order, for example
            0.00 for a zero brokerage plan or 20.00 for a flat plan.
        dp_amc_charge: DP and AMC charge applied on SELL only, per scrip, for
            example 13.50 or 20.00.

    PERCENT values (0.10 means 0.10 percent, not 10 percent and not 0.001):
        stt_pct: securities transaction tax on turnover, for example 0.10.
        exchange_txn_pct: exchange transaction charge on turnover, for example
            0.00297, or 0.00325 for brokers on the higher slab.
        sebi_pct: SEBI turnover fee, for example 0.0001.
        stamp_duty_pct: stamp duty on BUY turnover only, for example 0.015.
        gst_pct: GST on the service charges (brokerage + exchange transaction
            charge + SEBI turnover fee + DP charge), for
            example 18.0.
    """

    brokerage_per_order: float = 0.0
    stt_pct: float = 0.0
    exchange_txn_pct: float = 0.0
    sebi_pct: float = 0.0
    stamp_duty_pct: float = 0.0
    gst_pct: float = 0.0
    dp_amc_charge: float = 0.0


@dataclass(frozen=True)
class CostBreakdown:
    """
    Itemised estimated costs for one order, in rupees.

    total is the est_costs figure. The individual lines are kept so the UI can
    show a tooltip or a breakdown without recomputing anything.
    """

    brokerage: float = 0.0
    stt: float = 0.0
    exchange_txn: float = 0.0
    sebi: float = 0.0
    stamp_duty: float = 0.0
    gst: float = 0.0
    dp_amc: float = 0.0
    total: float = 0.0


# Accepted anywhere an estimated cost figure is expected.
CostInput = Union[CostBreakdown, float, int, None]


def turnover(price: Any, quantity: Any) -> float:
    """
    Turnover for one order: price * quantity, as a non-negative rupee amount.

    A convenience for callers, so the same definition of turnover feeds every
    cost estimate.
    """
    return abs(_as_number(price) * _as_number(quantity))


def _percent_of(amount: float, rate_pct: Any) -> float:
    """Apply a PERCENT rate to an amount. rate_pct of 0.10 means 0.10 percent."""
    return amount * _as_non_negative(rate_pct) / 100.0


def estimate_costs(
    turnover_value: Any,
    side: Any,
    rates: BrokerageRates,
    scrip_count: Any = 1,
) -> CostBreakdown:
    """
    Estimate brokerage and statutory costs for one order.

    Args:
        turnover_value: order turnover in rupees (price * quantity). Negative or
            invalid values are treated as 0.0.
        side: BUY or SELL (see normalise_side). Raises ValueError if neither.
        rates: BrokerageRates in effect for the account and broker. Percent fields
            are PERCENT values, so 0.10 means 0.10 percent.
        scrip_count: number of distinct scrips the DP and AMC charge applies to.
            Used on SELL only, where the charge is per scrip. Defaults to 1. Zero
            or negative means no DP charge.

    Returns:
        CostBreakdown with each line item and the total est_costs.

    Rules, applied exactly as specified:
        - stamp duty applies to BUY turnover only.
        - GST applies to the service charges - brokerage, exchange transaction
          charge, SEBI turnover fee and DP charge - and never to STT or stamp
          duty, which are taxes in their own right.
        - DP and AMC applies on SELL only, once per scrip.
    """
    order_side = normalise_side(side)
    amount = _as_non_negative(turnover_value)
    scrips = max(int(_as_non_negative(scrip_count)), 0)

    brokerage = _as_non_negative(rates.brokerage_per_order)
    stt = _percent_of(amount, rates.stt_pct)
    exchange_txn = _percent_of(amount, rates.exchange_txn_pct)
    sebi = _percent_of(amount, rates.sebi_pct)
    stamp_duty = _percent_of(amount, rates.stamp_duty_pct) if order_side == SIDE_BUY else 0.0
    dp_amc = _as_non_negative(rates.dp_amc_charge) * scrips if order_side == SIDE_SELL else 0.0

    # GST falls on the SERVICE charges: brokerage, the exchange transaction
    # charge, the SEBI turnover fee and the DP charge. It does not fall on STT
    # or on stamp duty, which are taxes in their own right.
    #
    # The DP charge is the one that shows. The rate is entered net of GST, so
    # 13.50 becomes 15.93 on the contract note; leaving it out of the base put
    # Est. Costs about 2.43 under the real figure on every sell, per scrip -
    # small, but wrong in the same direction every time, which is the kind of
    # error that quietly accumulates.
    gst = _percent_of(brokerage + exchange_txn + sebi + dp_amc, rates.gst_pct)

    total = brokerage + stt + exchange_txn + sebi + stamp_duty + gst + dp_amc

    return CostBreakdown(
        brokerage=brokerage,
        stt=stt,
        exchange_txn=exchange_txn,
        sebi=sebi,
        stamp_duty=stamp_duty,
        gst=gst,
        dp_amc=dp_amc,
        total=total,
    )


def estimate_total_costs(
    turnover_value: Any,
    side: Any,
    rates: BrokerageRates,
    scrip_count: Any = 1,
) -> float:
    """Estimated costs for one order as a single rupee figure (est_costs)."""
    return estimate_costs(turnover_value, side, rates, scrip_count).total


def gross_pnl(ltp: Any, avg_cost: Any, quantity: Any) -> float:
    """
    Gross profit or loss for a holding, in rupees.

        gross_pnl = (ltp - avg_cost) * quantity

    Negative results are returned as they are: a loss must stay a loss.
    """
    return (_as_number(ltp) - _as_number(avg_cost)) * _as_number(quantity)


def cost_amount(est_costs: CostInput) -> float:
    """
    Read a rupee cost figure from either a CostBreakdown or a plain number.

    Lets callers pass whatever estimate_costs or estimate_total_costs gave them
    without unpacking it first.
    """
    if isinstance(est_costs, CostBreakdown):
        return _as_number(est_costs.total)
    return _as_number(est_costs)


def net_pnl(gross: Any, est_costs: CostInput) -> float:
    """
    Net profit or loss after estimated costs, in rupees.

        net_pnl = gross_pnl - est_costs

    Costs always reduce the result, so a gross loss becomes a larger net loss.

    Args:
        gross: gross P&L, normally from gross_pnl.
        est_costs: a CostBreakdown or a plain rupee figure.
    """
    return _as_number(gross) - cost_amount(est_costs)
