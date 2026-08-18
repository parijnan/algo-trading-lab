"""
engine.py — Kronos backtest engine

One iron condor at a time on the Nifty monthly expiry: short CE and PE at
SHORT_DELTA_TARGET, long wings further OTM at WING_DELTA_TARGET, entered around
ENTRY_DTE_TARGET and closed per EXIT_POLICY.

Four properties of this engine matter more than the loop itself, and each exists
because Phase 0 found the failure it prevents:

  * Single slot. The next position cannot open until the previous one actually
    closes — not until its *scheduled* exit, because a profit-target exit frees
    the slot sooner. That path dependency is why concurrency lives here and not
    in expiry_rules.py.

  * The wing is always strictly further OTM than its short. Selected by scanning
    from beyond the short strike, and bounded so an inward liquidity
    substitution cannot cross it.

  * Marks carry an age. A leg that has not traded for hours forward-fills a dead
    price into every minute's P&L, and a stop computed off it is fictional.
    Minutes where any leg's mark is staler than MAX_PRICE_STALENESS_MINUTES are
    not valid trigger points, and the count is reported per trade.

  * Slippage is applied eight times per round trip — four legs in, four out, and
    the direction reverses on exit. Getting the exit sign wrong manufactures an
    edge, so the total slippage bill is reported next to the result.

Function-based by repo convention. No classes in the backtest layer.
"""

import os
import logging

import pandas as pd

from configs import (
    ENTRY_TIME, ENTRY_DTE_TARGET, ENTRY_DTE_MIN, STRIKE_STEP,
    SHORT_DELTA_TARGET, WING_DELTA_TARGET,
    EXIT_POLICY, EXIT_OFFSET_MODE, EXIT_TIME, E3_FORCE_CLOSE_TIME,
    ENABLE_PROFIT_TARGET, PROFIT_TARGET_PCT_CREDIT,
    ENABLE_LOSS_EXIT, LOSS_MULTIPLE_CREDIT,
    MANAGEMENT_CADENCE, DAILY_CHECK_TIME,
    ALLOW_CONCURRENT_TRADES, ON_COLLISION, DEFERRED_ENTRY_MIN_DTE,
    MAX_PRICE_STALENESS_MINUTES,
    CAS_GO_LIVE_DATE, CAS_BLACKOUT_START, CAS_BLACKOUT_END,
    SLIPPAGE_POINTS, LOT_SIZE, RECORD_ENTRY_VIX,
    TRADE_LOGS_DIR, TRADE_SUMMARY_FILE, SKIP_SUMMARY_FILE,
)
import loader
import expiry_rules as er
from greeks import select_strike, is_further_otm, otm_sign

logger = logging.getLogger(__name__)

LEGS = (('ce', 'short'), ('ce', 'wing'), ('pe', 'short'), ('pe', 'wing'))


# ---------------------------------------------------------------------------
# Slippage — the sign reverses between entry and exit
# ---------------------------------------------------------------------------

def fill_price(raw: float, action: str) -> float:
    """`action` is 'buy' or 'sell'. Buys pay up, sells receive less."""
    return (raw + SLIPPAGE_POINTS) if action == 'buy' else max(raw - SLIPPAGE_POINTS, 0.0)


def leg_action(kind: str, phase: str) -> str:
    """A short leg is sold on entry and bought back on exit; a wing the reverse."""
    if kind == 'short':
        return 'sell' if phase == 'entry' else 'buy'
    return 'buy' if phase == 'entry' else 'sell'


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

def open_position(expiry, entry_ts, spot, opt_df_cache) -> tuple:
    """
    Select all four legs. Returns (position, None) or (None, skip_reason).

    The short is chosen first and the wing scanned from one strike beyond it,
    bounded so it can never land on or inside the short.
    """
    position = {}
    for option_type in ('ce', 'pe'):
        short = select_strike(spot, expiry, entry_ts, option_type,
                              SHORT_DELTA_TARGET, opt_df_cache, leg='short')
        if short is None:
            return None, f'no_liquid_short_{option_type}'

        beyond = short['strike'] + otm_sign(option_type) * STRIKE_STEP
        wing = select_strike(spot, expiry, entry_ts, option_type,
                             WING_DELTA_TARGET, opt_df_cache, leg='wing',
                             start_strike=beyond, bound_strike=short['strike'])
        if wing is None:
            return None, f'no_liquid_wing_{option_type}'
        if not is_further_otm(wing['strike'], short['strike'], option_type):
            return None, f'wing_not_beyond_short_{option_type}'

        position[(option_type, 'short')] = short
        position[(option_type, 'wing')]  = wing

    for key, leg in position.items():
        leg['entry_fill'] = fill_price(leg['price'], leg_action(key[1], 'entry'))

    return position, None


def net_credit(position: dict) -> float:
    """Post-slippage credit received per unit. Negative would mean a net debit."""
    return (position[('ce', 'short')]['entry_fill'] + position[('pe', 'short')]['entry_fill']
            - position[('ce', 'wing')]['entry_fill'] - position[('pe', 'wing')]['entry_fill'])


def spread_width(position: dict) -> float:
    """
    Widest side, in points. Only one side of a condor can be breached, so this
    is the structure's defined risk before credit.
    """
    return max(abs(position[(t, 'wing')]['strike'] - position[(t, 'short')]['strike'])
               for t in ('ce', 'pe'))


# ---------------------------------------------------------------------------
# Marking
# ---------------------------------------------------------------------------

def build_mark_frame(position, expiry, entry_ts, exit_ts, minute_grid, opt_df_cache):
    """
    Per-minute close, open and mark-age for all four legs over the trade window.

    Age is carried because these files are trade-derived: forward-filling a leg
    that has not traded for hours puts a dead price into the P&L. Minutes where
    any leg is staler than the bound are excluded from trigger evaluation.
    """
    grid = minute_grid[(minute_grid >= entry_ts) & (minute_grid <= exit_ts)]
    if len(grid) == 0:
        return None

    frame = pd.DataFrame(index=grid)
    for option_type, kind in LEGS:
        leg = position[(option_type, kind)]
        df = opt_df_cache[(expiry, leg['strike'], option_type)]
        w = df[(df['datetime'] >= entry_ts) & (df['datetime'] <= exit_ts)]
        w = w.drop_duplicates('datetime').set_index('datetime').sort_index()
        tag = f'{option_type}_{kind}'
        frame[f'{tag}_close'] = w['close'].reindex(grid, method='ffill')
        frame[f'{tag}_open']  = w['open'].reindex(grid, method='ffill')
        stamps = pd.Series(w.index, index=w.index).reindex(grid, method='ffill')
        frame[f'{tag}_age']   = (pd.Series(grid, index=grid) - stamps).dt.total_seconds() / 60.0

    frame['max_age'] = frame[[f'{t}_{k}_age' for t, k in LEGS]].max(axis=1)
    frame['pl'] = sum(
        (position[(t, k)]['entry_fill'] - frame[f'{t}_{k}_close']) if k == 'short'
        else (frame[f'{t}_{k}_close'] - position[(t, k)]['entry_fill'])
        for t, k in LEGS
    )
    return frame


def valid_marks(frame: pd.DataFrame) -> pd.Series:
    """
    Minutes at which the position can honestly be valued and acted on.

    Excludes stale marks, and — for dates on or after CAS went live — the
    auction window, where option prints are not real prices (§5.3 of the
    feeder). No trade in the current sample reaches that date, so the CAS
    condition never fires here; it exists so a live build inherits it.
    """
    ok = frame['max_age'].notna() & (frame['max_age'] <= MAX_PRICE_STALENESS_MINUTES)
    ok &= frame['pl'].notna()

    cas_live = frame.index.normalize() >= pd.Timestamp(CAS_GO_LIVE_DATE)
    in_auction = ((frame.index.time >= pd.Timestamp(CAS_BLACKOUT_START).time())
                  & (frame.index.time <= pd.Timestamp(CAS_BLACKOUT_END).time()))
    return ok & ~(cas_live & in_auction)


# ---------------------------------------------------------------------------
# Management
# ---------------------------------------------------------------------------

def find_trigger(frame: pd.DataFrame, credit: float) -> tuple:
    """
    First minute at which the profit target or loss exit fires.
    Returns (timestamp, reason) or (None, None). Evaluated on 1-min closes;
    the fill happens on the next bar's open, per the repo's convention.
    """
    ok = valid_marks(frame)
    if MANAGEMENT_CADENCE == 'daily_close':
        ok &= (frame.index.time == pd.Timestamp(DAILY_CHECK_TIME).time())

    hits = []
    if ENABLE_PROFIT_TARGET and credit > 0:
        pt = frame.index[ok & (frame['pl'] >= PROFIT_TARGET_PCT_CREDIT * credit)]
        if len(pt):
            hits.append((pt[0], 'profit_target'))
    if ENABLE_LOSS_EXIT and credit > 0:
        sl = frame.index[ok & (frame['pl'] <= -LOSS_MULTIPLE_CREDIT * credit)]
        if len(sl):
            hits.append((sl[0], 'loss_exit'))

    if not hits:
        return None, None
    return min(hits, key=lambda h: h[0])


def exit_fills(position, frame, at_ts) -> dict:
    """Post-slippage exit price per leg, taken from the bar's open."""
    fills = {}
    for option_type, kind in LEGS:
        tag = f'{option_type}_{kind}'
        raw = frame.at[at_ts, f'{tag}_open']
        if pd.isna(raw):
            raw = frame.at[at_ts, f'{tag}_close']
        fills[(option_type, kind)] = fill_price(float(raw), leg_action(kind, 'exit'))
    return fills


def realised_pl(position: dict, fills: dict) -> float:
    """Round-trip P&L per unit, both ends post-slippage."""
    return sum(
        (position[(t, k)]['entry_fill'] - fills[(t, k)]) if k == 'short'
        else (fills[(t, k)] - position[(t, k)]['entry_fill'])
        for t, k in LEGS
    )


# ---------------------------------------------------------------------------
# Main loop
# ---------------------------------------------------------------------------

def run_backtest(universe, holidays, nifty_1m, vix_1m) -> tuple:
    """
    Sequential over contracts, because the slot is single and the next entry
    depends on when the previous position actually closed.
    Returns (trades_df, skips_df).
    """
    trades, skips = [], []
    slot_free_date = None
    minute_grid = nifty_1m.index

    for expiry in universe['expiry_date']:
        target_entry = er.entry_date_for(expiry, holidays, ENTRY_DTE_TARGET, ENTRY_DTE_MIN)
        if target_entry is None:
            skips.append({'expiry_date': expiry, 'reason': 'no_entry_window'})
            continue

        entry_date, deferred = target_entry, False
        if not ALLOW_CONCURRENT_TRADES and slot_free_date is not None \
                and entry_date <= slot_free_date:
            if ON_COLLISION == 'skip':
                skips.append({'expiry_date': expiry, 'reason': 'slot_occupied'})
                continue
            entry_date = er.next_trading_day(slot_free_date + pd.Timedelta(days=1).to_pytimedelta(),
                                             holidays)
            deferred = True
            if entry_date is None or (expiry - entry_date).days < DEFERRED_ENTRY_MIN_DTE:
                skips.append({'expiry_date': expiry, 'reason': 'no_runway_after_deferral'})
                continue

        exit_date = er.resolve_exit_date(expiry, holidays, EXIT_POLICY, EXIT_OFFSET_MODE)
        if exit_date <= entry_date:
            skips.append({'expiry_date': expiry, 'reason': 'exit_not_after_entry'})
            continue

        entry_ts = pd.Timestamp(f"{entry_date} {ENTRY_TIME}:00")
        close_at = E3_FORCE_CLOSE_TIME if EXIT_POLICY.upper() == 'E3' else EXIT_TIME
        exit_ts  = pd.Timestamp(f"{exit_date} {close_at}:00")

        spot = loader.get_1min_value(nifty_1m, entry_ts, 'close')
        if spot is None:
            skips.append({'expiry_date': expiry, 'reason': 'no_spot_at_entry'})
            continue

        cache = {}
        position, reason = open_position(expiry, entry_ts, spot, cache)
        if position is None:
            skips.append({'expiry_date': expiry, 'reason': reason})
            continue

        credit = net_credit(position)
        if credit <= 0:
            skips.append({'expiry_date': expiry, 'reason': 'non_positive_credit'})
            continue

        frame = build_mark_frame(position, expiry, entry_ts, exit_ts, minute_grid, cache)
        if frame is None or frame['pl'].notna().sum() == 0:
            skips.append({'expiry_date': expiry, 'reason': 'no_marks_in_window'})
            continue

        trig_ts, trig_reason = find_trigger(frame, credit)
        if trig_ts is not None:
            after = frame.index[frame.index > trig_ts]
            fill_ts = after[0] if len(after) else trig_ts
            exit_reason = trig_reason
        else:
            fill_ts = frame.index[-1]
            exit_reason = f'time_exit_{EXIT_POLICY.lower()}'

        fills = exit_fills(position, frame, fill_ts)
        pl_points = realised_pl(position, fills)

        ok = valid_marks(frame)
        held = frame.loc[:fill_ts]
        width = spread_width(position)
        capital_at_risk = (width - credit) * LOT_SIZE

        trades.append({
            'expiry_date': expiry,
            'entry_date': entry_date, 'entry_ts': entry_ts,
            'entry_dte_target': ENTRY_DTE_TARGET,
            'entry_dte_realised': (expiry - entry_date).days,
            'deferred': deferred,
            'scheduled_exit_date': exit_date,
            'exit_ts': fill_ts, 'exit_date': fill_ts.date(),
            'exit_reason': exit_reason,
            'days_held': (fill_ts.date() - entry_date).days,
            'entry_spot': round(spot, 2),
            'entry_vix': loader.get_1min_value(vix_1m, entry_ts, 'close') if RECORD_ENTRY_VIX else None,
            'ce_short_strike': position[('ce', 'short')]['strike'],
            'ce_wing_strike':  position[('ce', 'wing')]['strike'],
            'pe_short_strike': position[('pe', 'short')]['strike'],
            'pe_wing_strike':  position[('pe', 'wing')]['strike'],
            'ce_short_delta': round(position[('ce', 'short')]['delta'], 4),
            'pe_short_delta': round(position[('pe', 'short')]['delta'], 4),
            'substitutions': sum(abs(position[k]['substituted']) for k in position),
            'credit_points': round(credit, 2),
            'width_points': width,
            'max_loss_points': round(width - credit, 2),
            'pl_points': round(pl_points, 2),
            'pl_rs': round(pl_points * LOT_SIZE, 2),
            'capital_at_risk_rs': round(capital_at_risk, 2),
            'slippage_cost_points': round(8 * SLIPPAGE_POINTS, 2),
            'slippage_cost_rs': round(8 * SLIPPAGE_POINTS * LOT_SIZE, 2),
            'peak_pl_points': round(held.loc[ok.loc[held.index], 'pl'].max(), 2)
                              if ok.loc[held.index].any() else None,
            'trough_pl_points': round(held.loc[ok.loc[held.index], 'pl'].min(), 2)
                                if ok.loc[held.index].any() else None,
            'minutes_in_window': len(held),
            'minutes_stale_skipped': int((~ok.loc[held.index]).sum()),
        })

        slot_free_date = fill_ts.date()
        _save_trade_log(expiry, frame.loc[:fill_ts])

    return pd.DataFrame(trades), pd.DataFrame(skips)


def _save_trade_log(expiry, frame) -> None:
    os.makedirs(TRADE_LOGS_DIR, exist_ok=True)
    frame.to_csv(os.path.join(TRADE_LOGS_DIR, f"{expiry:%Y-%m-%d}.csv"))


def save_trades(trades: pd.DataFrame, skips: pd.DataFrame = None) -> None:
    os.makedirs(os.path.dirname(TRADE_SUMMARY_FILE), exist_ok=True)
    trades.to_csv(TRADE_SUMMARY_FILE, index=False)
    if skips is not None and not skips.empty:
        skips.to_csv(SKIP_SUMMARY_FILE, index=False)
