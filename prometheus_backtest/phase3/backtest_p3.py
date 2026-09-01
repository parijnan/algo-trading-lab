"""
Prometheus - Phase 3 — raw signal-following state machine.

Fill convention (same as Phase 2): a flip is only knowable once its 15-min
bar closes, so both entry and exit fill at the OPEN of the FOLLOWING bar.
A flip that closes an open position is ALSO the entry for the opposite
direction, at that same next-bar open (continuous, always-in-the-market
trend-following once the first signal fires) -- same "rule 7" mechanic as
Phase 2's backtest_p2.py, just without the 2-lot machinery.

No stop loss, no profit target, no EOD square-off -- the ONLY exit is the
opposite flip. Positions can span multiple days, even a contract roll.
"""

from dataclasses import dataclass
from typing import Optional

import pandas as pd

import configs_p3 as configs


def _time_lt(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() < pd.Timestamp(f'2000-01-01 {hhmm}').time()


@dataclass
class TradeState:
    status:          str               = 'watching'   # watching | in_trade
    direction:       Optional[str]     = None          # bullish | bearish
    entry_price:     Optional[float]   = None
    entry_ts:        Optional[pd.Timestamp] = None
    signal_ts:       Optional[pd.Timestamp] = None      # bar whose close triggered the flip
    signal_close:    Optional[float]   = None
    contract_expiry: Optional[str]     = None


def run_backtest(df_15m: pd.DataFrame) -> pd.DataFrame:
    """
    df_15m must carry 'trend'/'trend_flip' (compute_st) and a DatetimeIndex.
    Returns one row per trade.
    """
    state = TradeState()
    trades: list = []
    trade_id = 0

    pending_close: bool = False
    pending_entry_direction: Optional[str] = None
    pending_signal_ts: Optional[pd.Timestamp] = None
    pending_signal_close: Optional[float] = None

    for ts, bar in df_15m.iterrows():
        # --- Fill anything deferred from the previous bar ---
        if pending_close and state.status == 'in_trade':
            exit_price = bar['open']
            pnl_pts = ((exit_price - state.entry_price) if state.direction == 'bullish'
                      else (state.entry_price - exit_price))
            trades[-1].update({
                'exit_ts': ts, 'exit_price': round(exit_price, 2),
                'exit_contract_expiry': bar['contract_expiry'],
                'pnl_points': round(pnl_pts, 2),
                'pnl_rs': round(pnl_pts * configs.LOT_SIZE * configs.LOTS, 2),
            })
            state = TradeState()
            pending_close = False

        if pending_entry_direction is not None and state.status == 'watching':
            if _time_lt(ts, configs.MIN_ENTRY_TIME):
                # Fill would land before MIN_ENTRY_TIME -- drop rather than
                # open early (same discipline as Phase 2, see its comment).
                pending_entry_direction = None
            else:
                direction = pending_entry_direction
                pending_entry_direction = None
                entry_price = bar['open']

                trade_id += 1
                state = TradeState(
                    status='in_trade', direction=direction, entry_price=entry_price, entry_ts=ts,
                    signal_ts=pending_signal_ts, signal_close=pending_signal_close,
                    contract_expiry=bar['contract_expiry'],
                )
                trades.append({
                    'trade_id': trade_id, 'contract_expiry': state.contract_expiry,
                    'direction': direction, 'entry_ts': ts, 'entry_price': entry_price,
                    'signal_ts': state.signal_ts, 'signal_close': state.signal_close,
                    'entry_slippage_points': round(
                        (entry_price - state.signal_close) if direction == 'bullish'
                        else (state.signal_close - entry_price), 2) if state.signal_close is not None else None,
                })

        flip = bool(bar['trend_flip']) and not pd.isna(bar['trend'])
        direction_now = ('bullish' if bar['trend'] else 'bearish') if not pd.isna(bar['trend']) else None

        # --- In-trade: the only thing that can happen is an opposite flip ---
        if state.status == 'in_trade' and flip and direction_now and direction_now != state.direction:
            pending_close = True
            pending_entry_direction = direction_now
            pending_signal_ts, pending_signal_close = ts, bar['close']

        # --- Fresh entry detection (only reachable when watching) ---
        elif state.status == 'watching' and pending_entry_direction is None and flip and direction_now:
            pending_entry_direction = direction_now
            pending_signal_ts, pending_signal_close = ts, bar['close']

    return pd.DataFrame(trades)
