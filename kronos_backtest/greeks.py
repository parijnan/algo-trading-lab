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
    STRIKE_SCAN_MAX_GAP, STRIKE_FALLBACK_STEPS,
    SHORT_FALLBACK_DIRECTION, WING_FALLBACK_DIRECTION,
)
from loader import (
    load_option_data, get_option_price, get_option_price_with_age,
    liquidity_stats, is_liquid,
)

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
        volume, bars, oi = liquidity_stats(opt_df_cache[key], entry_ts)
        out.append({
            'strike': strike,
            'price':  price,
            'age_min': age,
            'delta':  compute_delta(spot, strike, dte_days, price, option_type),
            'volume': volume,
            'bars':   bars,
            'oi':     oi,
        })

    return out


def _leg_cache(expiry, strike, option_type, opt_df_cache):
    key = (expiry, strike, option_type)
    if key not in opt_df_cache:
        opt_df_cache[key] = load_option_data(expiry, strike, option_type)
    return opt_df_cache[key]


def _evaluate_strike(spot, expiry, entry_ts, option_type, strike,
                     opt_df_cache, max_staleness):
    """(price, delta, liquid, volume, bars, oi) for one strike, or None if unpriced."""
    df = _leg_cache(expiry, strike, option_type, opt_df_cache)
    price = get_option_price(df, entry_ts, 'open', max_staleness_minutes=max_staleness)
    if price is None or price <= MIN_OPTION_PRICE:
        return None
    dte_days = max((expiry - entry_ts.date()).days, 0.5)
    delta = compute_delta(spot, strike, dte_days, price, option_type)
    if delta is None:
        return None
    volume, bars, oi = liquidity_stats(df, entry_ts)
    return price, delta, is_liquid(volume, bars, oi), volume, bars, oi


def _fallback_offsets(option_type: str, leg: str) -> list:
    """
    Strike offsets to try when the target strike is not liquid, in order.

    Direction errs toward safety on both legs: a short that has to move goes
    further OTM (less risk, less credit), a wing goes closer to the money (more
    protection, more cost). The one substitution never made is the pair that
    widens the spread on both legs at once.
    """
    if STRIKE_FALLBACK_STEPS <= 0:
        return []
    direction = SHORT_FALLBACK_DIRECTION if leg == 'short' else WING_FALLBACK_DIRECTION
    otm_sign = 1 if option_type == 'ce' else -1          # OTM is up for CE, down for PE
    sign = otm_sign if direction == 'outward' else -otm_sign
    return [sign * STRIKE_STEP * i for i in range(1, STRIKE_FALLBACK_STEPS + 1)]


def select_strike(spot: float, expiry, entry_ts, option_type: str,
                  target_delta: float, opt_df_cache: dict,
                  leg: str = 'short', max_staleness=-1) -> dict:
    """
    Pick a tradeable strike at the target delta.

    Walks outward from ATM to the first strike at or below target_delta, then
    checks that it is actually liquid — a print inside the staleness bound says
    the strike traded once, not that an order would fill. If it is not liquid,
    substitutes a neighbour per _fallback_offsets before giving up.

    Returns a dict with strike, price (pre-slippage), delta, liquidity stats and
    `substituted` (offset in strikes from the delta-target strike, 0 if none),
    or None if no tradeable strike exists. `substituted` is recorded per trade
    so Phase 1 can report how often the fallback fires.
    """
    misses = 0
    for strike in strike_candidates(spot, option_type):
        ev = _evaluate_strike(spot, expiry, entry_ts, option_type, strike,
                              opt_df_cache, max_staleness)
        if ev is None:
            misses += 1
            if misses >= STRIKE_SCAN_MAX_GAP:
                break
            continue
        misses = 0
        price, delta, liquid, volume, bars, oi = ev
        if delta > target_delta:
            continue

        # Target strike found. Take it if liquid, else try neighbours.
        candidates = [(0, strike, price, delta, liquid, volume, bars, oi)]
        if not liquid:
            for offset in _fallback_offsets(option_type, leg):
                alt = strike + offset
                alt_ev = _evaluate_strike(spot, expiry, entry_ts, option_type, alt,
                                          opt_df_cache, max_staleness)
                if alt_ev is None:
                    continue
                a_price, a_delta, a_liquid, a_vol, a_bars, a_oi = alt_ev
                if a_liquid:
                    candidates.append((offset // STRIKE_STEP, alt, a_price, a_delta,
                                       a_liquid, a_vol, a_bars, a_oi))
                    break

        chosen = next((c for c in candidates if c[4]), None)
        if chosen is None:
            return None
        sub, k, px, d, liq, vol, bars_, oi_ = chosen
        return {'strike': k, 'price': px, 'delta': d, 'liquid': liq,
                'volume': vol, 'bars': bars_, 'oi': oi_, 'substituted': sub}

    return None
