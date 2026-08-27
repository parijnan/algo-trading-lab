"""
Prometheus - Phase 2 — two-lot scale-out state machine.

Fill conventions:
  - Entry and trend_flip exits: the flip is only knowable once its 15-min
    bar closes, so both fill at the OPEN of the FOLLOWING bar (same
    discipline as Prometheus v1). Rule 7 makes a flip that closes an open
    trade ALSO the entry for the opposite direction — both resolve at that
    same next-bar open, at the same price.
  - Lot 1 (100pt) / Lot 2 (pivot) targets: same-bar immediate, since they're
    price facts knowable the instant the bar's high/low touches them. Both
    are PROFIT-taking exits, so a gap-through fills at the bar's open
    (better for the trade, not worse) rather than the exact threshold —
    see _target_fill_price.
  - Points-based stop_loss (optional, configs.STOP_LOSS_POINTS): same-bar
    immediate, a single shared level protecting whichever lot(s) are still
    open (not per-lot). This is a LOSS-taking exit, so gap-through fills at
    the bar's open only when that's WORSE than the stop level — the mirror
    of _target_fill_price, not the same function (see _stop_fill_price;
    the two rules are genuinely opposite, so sharing one function invites
    a sign error).
  - EOD square-off: same-bar immediate, price-agnostic (a clock fact).

Booking order within a bar is not ambiguous for the two profit targets:
lot 2's target is enforced (via assertion) to be farther from entry than
lot 1's, so a single bar can only reach lot 2's target after having already
passed lot 1's. But when stop_loss is enabled, a single 15-min bar can span
both the stop and a profit target (crude's 15-min ranges are wide enough for
this to be a real case, not a corner case) — convention: the stop is
checked, and wins, before either target. This can't be inferred from
tick data we don't have, so it's an explicit choice, not a derived fact.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

import configs_p2 as configs


@dataclass
class TradeState:
    status:            str               = 'watching'   # watching | in_trade
    direction:         Optional[str]     = None          # bullish | bearish
    entry_price:       Optional[float]   = None
    entry_ts:          Optional[pd.Timestamp] = None
    signal_ts:         Optional[pd.Timestamp] = None      # bar whose close triggered the flip
    signal_close:      Optional[float]   = None
    contract_expiry:   Optional[str]     = None
    lot1_target:       Optional[float]   = None
    lot2_target:       Optional[float]   = None
    lot2_target_source: Optional[str]    = None           # pivot level name, or 'no_pivot_fallback'
    sl_price:          Optional[float]   = None           # None when STOP_LOSS_POINTS is disabled
    lot1_open:         bool              = True
    lot2_open:         bool              = True


def _time_lt(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() < datetime.strptime(hhmm, '%H:%M').time()


def _target_fill_price(direction: str, level: float, bar_open: float) -> float:
    """Profit-taking exit: fill at the level, or the bar's open if the open
    already gapped past it in the trade's favour — you can't fill worse
    than a favourable gap, and can't fill at a level price has already
    jumped beyond before the bar even opened."""
    if direction == 'bullish':
        return bar_open if bar_open > level else level
    return bar_open if bar_open < level else level


def _stop_fill_price(direction: str, level: float, bar_open: float) -> float:
    """Loss-taking exit: fill at the level, or the bar's open if the open
    already gapped past it AGAINST the trade — mirror of
    _target_fill_price, deliberately not the same function (a stop and a
    target gap-through in opposite directions: a stop's gap-through is
    worse for the trade, a target's is better)."""
    if direction == 'bullish':
        return bar_open if bar_open < level else level
    return bar_open if bar_open > level else level


def _resolve_thresholds(entry_price: float) -> tuple:
    """
    Resolve lot1/SL distances (in price points) for this entry, per
    configs.THRESHOLD_MODE. Single source of truth for the unit — 'pct'
    mode is NOT a parallel set of configs, it's a different way of
    computing the same two price distances everything downstream (target
    fills, the pivot-qualifying gate, the assertion) already works in.
    Returns (lot1_distance, sl_distance_or_None).
    """
    if configs.THRESHOLD_MODE == 'pct':
        lot1_distance = entry_price * configs.TARGET1_PCT / 100
        sl_distance = (entry_price * configs.SL_PCT / 100
                        if configs.SL_PCT is not None else None)
    else:
        lot1_distance = configs.LOT1_TARGET_POINTS
        sl_distance = configs.STOP_LOSS_POINTS
    return lot1_distance, sl_distance


def _find_target2(entry_price: float, direction: str, day_pivots, qualifying_distance: float) -> tuple:
    """Lot 2's target, per configs.TARGET2_MODE. 'flat'/'flat_pct' return a
    fixed distance with no level lookup — a control to check whether the
    pivot mechanism (rule 6) is actually doing anything beyond picking a
    number a comparable distance out."""
    if configs.TARGET2_MODE == 'flat':
        level = (entry_price + configs.TARGET2_FLAT_POINTS if direction == 'bullish'
                 else entry_price - configs.TARGET2_FLAT_POINTS)
        return level, 'flat'
    if configs.TARGET2_MODE == 'flat_pct':
        dist = entry_price * configs.TARGET2_FLAT_PCT / 100
        level = entry_price + dist if direction == 'bullish' else entry_price - dist
        return level, 'flat_pct'
    return _find_pivot_target(entry_price, direction, day_pivots, qualifying_distance)


def _find_pivot_target(entry_price: float, direction: str, day_pivots, qualifying_distance: float) -> tuple:
    """Nearest pivot/R/S level beyond qualifying_distance (a resolved price
    distance — see _resolve_thresholds) from entry, in the trade's
    favourable direction. Returns (level, source_name) or (None, None) if
    none qualifies."""
    if day_pivots is None or pd.isna(day_pivots.get('pp')):
        return None, None

    candidates = {lvl: day_pivots[lvl] for lvl in configs.PIVOT_LEVELS}
    if direction == 'bullish':
        qualifying = {lvl: v for lvl, v in candidates.items()
                      if v > entry_price + qualifying_distance}
        if not qualifying:
            return None, None
        best = min(qualifying, key=qualifying.get)
    else:
        qualifying = {lvl: v for lvl, v in candidates.items()
                      if v < entry_price - qualifying_distance}
        if not qualifying:
            return None, None
        best = max(qualifying, key=qualifying.get)

    level = qualifying[best]
    assert abs(level - entry_price) > qualifying_distance, \
        f"pivot target {best}={level} is not beyond the qualifying distance {qualifying_distance} from entry {entry_price}"
    return level, best


def _signal_allowed(ts: pd.Timestamp, mins_to_close: float) -> bool:
    """
    Gates whether a flip on THIS bar may become a pending entry, checked at
    signal time (not fill time): MAX_ENTRY_BEFORE_CLOSE_MIN is about runway
    remaining when the signal fires, so it belongs here. MIN_ENTRY_TIME is
    checked separately, at the fill bar, in run_backtest — see its comment
    there for why the two aren't the same check.
    """
    if mins_to_close < configs.MAX_ENTRY_BEFORE_CLOSE_MIN:
        return False
    return True


def _new_lot_record(lot_num: int, direction: str, exit_ts, exit_price: float,
                     reason: str, entry_price: float) -> dict:
    pnl_points = (exit_price - entry_price) if direction == 'bullish' else (entry_price - exit_price)
    return {
        f'lot{lot_num}_exit_ts':     exit_ts,
        f'lot{lot_num}_exit_price':  round(exit_price, 2),
        f'lot{lot_num}_exit_reason': reason,
        f'lot{lot_num}_pnl_points':  round(pnl_points, 2),
        f'lot{lot_num}_pnl_rs':      round(pnl_points * configs.LOT_SIZE * configs.LOTS_PER_LEG, 2),
    }


def run_backtest(df_15m: pd.DataFrame, daily_pivots: pd.DataFrame) -> pd.DataFrame:
    """
    df_15m must carry 'trend'/'trend_flip' (compute_st), 'is_day_end', and
    'mins_to_close'. daily_pivots is indexed by date (see data_loader_p2).
    Returns one row per trade (both lots' exits as columns).
    """
    state = TradeState()
    trades: list = []
    trade_id = 0

    pending_close_reason: Optional[str] = None
    pending_entry_direction: Optional[str] = None
    pending_signal_ts: Optional[pd.Timestamp] = None
    pending_signal_close: Optional[float] = None

    for ts, bar in df_15m.iterrows():
        # --- Fill anything deferred from the previous bar (trend_flip) ---
        if pending_close_reason is not None and state.status == 'in_trade':
            for lot_num, is_open in ((1, state.lot1_open), (2, state.lot2_open)):
                if is_open:
                    trades[-1].update(_new_lot_record(
                        lot_num, state.direction, ts, bar['open'],
                        pending_close_reason, state.entry_price))
            state = TradeState()
            pending_close_reason = None

        if pending_entry_direction is not None and state.status == 'watching':
            if _time_lt(ts, configs.MIN_ENTRY_TIME):
                # Fill would land before MIN_ENTRY_TIME — drop rather than
                # open early. Checked here, at the FILL bar, not at signal
                # time: a flip detected on the day's first bar (e.g. 09:00)
                # still fills at MIN_ENTRY_TIME exactly (09:15) and is
                # allowed — MIN_ENTRY_TIME gates when the position opens,
                # not which bar the signal came from.
                pending_entry_direction = None
            else:
                direction = pending_entry_direction
                pending_entry_direction = None
                entry_price = bar['open']
                lot1_distance, sl_distance = _resolve_thresholds(entry_price)
                lot1_target = (entry_price + lot1_distance if direction == 'bullish'
                               else entry_price - lot1_distance)
                sl_price = None
                if sl_distance is not None:
                    sl_price = (entry_price - sl_distance if direction == 'bullish'
                                else entry_price + sl_distance)

                day_pivots = (daily_pivots.loc[ts.normalize()]
                              if ts.normalize() in daily_pivots.index else None)
                lot2_target, lot2_source = _find_target2(entry_price, direction, day_pivots, lot1_distance)
                if lot2_target is None:
                    lot2_target, lot2_source = lot1_target, 'no_pivot_fallback'

                trade_id += 1
                state = TradeState(
                    status='in_trade', direction=direction, entry_price=entry_price, entry_ts=ts,
                    signal_ts=pending_signal_ts, signal_close=pending_signal_close,
                    contract_expiry=bar['contract_expiry'],
                    lot1_target=lot1_target, lot2_target=lot2_target, lot2_target_source=lot2_source,
                    sl_price=sl_price, lot1_open=True, lot2_open=True,
                )
                trades.append({
                    'trade_id': trade_id, 'contract_expiry': state.contract_expiry,
                    'direction': direction, 'entry_ts': ts, 'entry_price': entry_price,
                    'signal_ts': state.signal_ts, 'signal_close': state.signal_close,
                    # positive = entry filled worse than the signal bar's close
                    'entry_slippage_points': round(
                        (entry_price - state.signal_close) if direction == 'bullish'
                        else (state.signal_close - entry_price), 2) if state.signal_close is not None else None,
                    'sl_price': round(sl_price, 2) if sl_price is not None else None,
                    'lot1_target': round(lot1_target, 2),
                    'lot2_target': round(lot2_target, 2),
                    'lot2_target_source': lot2_source,
                })

        flip = bool(bar['trend_flip']) and not pd.isna(bar['trend'])
        direction_now = ('bullish' if bar['trend'] else 'bearish') if not pd.isna(bar['trend']) else None
        mins_to_close = bar['mins_to_close']

        # --- In-trade: EOD safety net, then lot targets, then trend-flip SL ---
        if state.status == 'in_trade':
            if mins_to_close <= configs.EOD_SQUAREOFF_BEFORE_CLOSE_MIN:
                for lot_num, is_open in ((1, state.lot1_open), (2, state.lot2_open)):
                    if is_open:
                        trades[-1].update(_new_lot_record(
                            lot_num, state.direction, ts, bar['open'], 'eod_squareoff', state.entry_price))
                state = TradeState()
            elif state.sl_price is not None and (
                    (bar['low'] <= state.sl_price if state.direction == 'bullish'
                     else bar['high'] >= state.sl_price)):
                # Stop wins any same-bar tie against a profit target — see
                # module docstring. Closes whichever lot(s) remain open.
                fill = _stop_fill_price(state.direction, state.sl_price, bar['open'])
                for lot_num, is_open in ((1, state.lot1_open), (2, state.lot2_open)):
                    if is_open:
                        trades[-1].update(_new_lot_record(
                            lot_num, state.direction, ts, fill, 'stop_loss', state.entry_price))
                state = TradeState()
            else:
                if state.lot1_open:
                    hit = (bar['high'] >= state.lot1_target if state.direction == 'bullish'
                           else bar['low'] <= state.lot1_target)
                    if hit:
                        fill = _target_fill_price(state.direction, state.lot1_target, bar['open'])
                        trades[-1].update(_new_lot_record(1, state.direction, ts, fill, 'target_100', state.entry_price))
                        state.lot1_open = False

                if state.lot2_open:
                    hit = (bar['high'] >= state.lot2_target if state.direction == 'bullish'
                           else bar['low'] <= state.lot2_target)
                    if hit:
                        fill = _target_fill_price(state.direction, state.lot2_target, bar['open'])
                        if state.lot2_target_source == 'no_pivot_fallback':
                            reason = 'target_100_no_pivot'
                        elif state.lot2_target_source == 'flat':
                            reason = 'target_flat'
                        elif state.lot2_target_source == 'flat_pct':
                            reason = 'target_flat_pct'
                        else:
                            reason = 'target_pivot'
                        trades[-1].update(_new_lot_record(2, state.direction, ts, fill, reason, state.entry_price))
                        state.lot2_open = False

                if not state.lot1_open and not state.lot2_open:
                    state = TradeState()
                elif flip and direction_now and direction_now != state.direction:
                    # Rule 7: this flip closes the remaining lot(s) AND is
                    # itself the next entry in the opposite direction —
                    # both resolve at the next bar's open.
                    pending_close_reason = 'trend_flip'
                    if _signal_allowed(ts, mins_to_close):
                        pending_entry_direction = direction_now
                        pending_signal_ts, pending_signal_close = ts, bar['close']

        # --- Fresh entry detection (only reachable when watching) ---
        if (state.status == 'watching' and pending_entry_direction is None
                and flip and direction_now and not bool(bar['is_day_end'])):
            if _signal_allowed(ts, mins_to_close):
                pending_entry_direction = direction_now
                pending_signal_ts, pending_signal_close = ts, bar['close']

    return pd.DataFrame(trades)
