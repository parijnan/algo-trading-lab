"""
greeks.py — IV, delta and target-delta strike selection

Same mibian approach as the rest of the repo: back IV out of the traded price,
then evaluate delta at that IV. One deliberate difference from
athena_backtest.select_strike — the scan width is a fraction of spot rather than
a flat 5,000 points, because spot ranges roughly 7,500 to 26,000 across Kronos's
sample and a fixed width is not the same search at both ends.
"""

import logging

import mibian

from configs import (
    RISK_FREE_RATE, STRIKE_STEP, STRIKE_SCAN_WIDTH_PCT, MIN_OPTION_PRICE,
    STRIKE_SCAN_MAX_GAP,
)
from loader import load_option_data, get_option_price, get_option_price_with_age

logger = logging.getLogger(__name__)


def compute_iv(spot: float, strike: int, dte_days: float,
               option_price: float, option_type: str) -> float:
    """Implied volatility (%) backed out of a traded price. None on failure."""
    try:
        kwargs = ({'callPrice': option_price} if option_type == 'ce'
                  else {'putPrice': option_price})
        implied = mibian.BS([spot, strike, RISK_FREE_RATE, dte_days], **kwargs)
        iv = implied.impliedVolatility
        if iv is None or iv <= 0 or iv > 500:
            return None
        return iv
    except Exception:
        return None


def compute_delta(spot: float, strike: int, dte_days: float,
                  option_price: float, option_type: str) -> float:
    """Absolute delta at the price-implied IV. None on failure."""
    iv = compute_iv(spot, strike, dte_days, option_price, option_type)
    if iv is None:
        return None
    try:
        bs = mibian.BS([spot, strike, RISK_FREE_RATE, dte_days], volatility=iv)
        return abs(bs.callDelta if option_type == 'ce' else bs.putDelta)
    except Exception:
        return None


def atm_strike(spot: float) -> int:
    return int(round(spot / STRIKE_STEP) * STRIKE_STEP)


def strike_candidates(spot: float, option_type: str):
    """Strikes from ATM outward — up for CE, down for PE."""
    atm   = atm_strike(spot)
    width = int(round(spot * STRIKE_SCAN_WIDTH_PCT / STRIKE_STEP)) * STRIKE_STEP
    if option_type == 'ce':
        return range(atm, atm + width + STRIKE_STEP, STRIKE_STEP)
    return range(atm, atm - width - STRIKE_STEP, -STRIKE_STEP)


def scan_chain(spot: float, expiry, entry_ts, option_type: str,
               opt_df_cache: dict, max_staleness=None) -> list:
    """
    Walk the chain outward from ATM, returning one record per strike that has a
    usable print: {strike, price, age_min, delta}.

    Stops after STRIKE_SCAN_MAX_GAP consecutive strikes with no print at all —
    past a gap that wide the chain is unquoted rather than sparse, and every
    strike further out is deeper OTM and thinner still.

    max_staleness is passed through to the price lookup; None means unbounded,
    which is only appropriate for measurement (Phase 0 reports how much the
    answer moves with the bound).
    """
    dte_days = max((expiry - entry_ts.date()).days, 0.5)
    out, misses = [], 0

    for strike in strike_candidates(spot, option_type):
        key = (expiry, strike, option_type)
        if key not in opt_df_cache:
            opt_df_cache[key] = load_option_data(expiry, strike, option_type)

        price, age = get_option_price_with_age(opt_df_cache[key], entry_ts, 'open')
        usable = (price is not None and price > MIN_OPTION_PRICE
                  and (max_staleness is None or age <= max_staleness))
        if not usable:
            misses += 1
            if misses >= STRIKE_SCAN_MAX_GAP:
                break
            continue

        misses = 0
        out.append({
            'strike': strike,
            'price':  price,
            'age_min': age,
            'delta':  compute_delta(spot, strike, dte_days, price, option_type),
        })

    return out


def select_strike(spot: float, expiry, entry_ts, option_type: str,
                  target_delta: float, opt_df_cache: dict,
                  max_staleness=-1) -> tuple:
    """
    First strike outward from ATM whose absolute delta is at or below
    target_delta. Returns (strike, raw_price, delta) or (None, None, None).
    Price is pre-slippage.
    """
    dte_days = max((expiry - entry_ts.date()).days, 0.5)
    misses = 0

    for strike in strike_candidates(spot, option_type):
        key = (expiry, strike, option_type)
        if key not in opt_df_cache:
            opt_df_cache[key] = load_option_data(expiry, strike, option_type)

        price = get_option_price(opt_df_cache[key], entry_ts, 'open',
                                 max_staleness_minutes=max_staleness)
        if price is None or price <= MIN_OPTION_PRICE:
            misses += 1
            if misses >= STRIKE_SCAN_MAX_GAP:
                break
            continue
        misses = 0

        delta = compute_delta(spot, strike, dte_days, price, option_type)
        if delta is None:
            continue
        if delta <= target_delta:
            return strike, price, delta

    return None, None, None
