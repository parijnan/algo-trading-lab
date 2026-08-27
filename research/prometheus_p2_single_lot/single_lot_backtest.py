"""
Side project: single-lot variant of Prometheus Phase 2.

Same ST_15 signal, same entry-timing rules (MIN_ENTRY_TIME checked at the
FILL bar, MAX_ENTRY_BEFORE_CLOSE_MIN checked at signal time), same EOD
square-off (15 min before that day's actual close), same trend_flip
dual-duty SL/re-entry rule — all copied verbatim from backtest_p2.py. The
only change: ONE position instead of two lots, booking the whole size at a
1% target, SL at 1.8%, no pivot/flat second leg.

This is mechanically identical to Phase 2's lot1 alone (lot1's exit checks
never reference lot2's state), so results are cross-checked against
prometheus_backtest/phase2/data/trade_summary_p2.csv's lot1_pnl_rs column
at the end of this script as a consistency check, not just asserted.
"""

import os
import sys
from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(os.path.dirname(os.path.dirname(
    os.path.abspath(__file__)))), 'prometheus_backtest', 'phase2'))
import configs_p2 as configs  # noqa: E402
from data_loader_p2 import load_futures_1min, resample_ohlcv, compute_st  # noqa: E402

TARGET_PCT = 1.0
SL_PCT = 1.8
OUT_DIR = os.path.dirname(os.path.abspath(__file__))
TRADE_LOG_FILE = os.path.join(OUT_DIR, 'single_lot_trades.csv')


def _time_lt(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() < datetime.strptime(hhmm, '%H:%M').time()


def _target_fill_price(direction, level, bar_open):
    if direction == 'bullish':
        return bar_open if bar_open > level else level
    return bar_open if bar_open < level else level


def _stop_fill_price(direction, level, bar_open):
    if direction == 'bullish':
        return bar_open if bar_open < level else level
    return bar_open if bar_open > level else level


def _signal_allowed(mins_to_close):
    return mins_to_close >= configs.MAX_ENTRY_BEFORE_CLOSE_MIN


@dataclass
class State:
    status: str = 'watching'
    direction: Optional[str] = None
    entry_price: Optional[float] = None
    entry_ts: Optional[pd.Timestamp] = None
    signal_ts: Optional[pd.Timestamp] = None
    signal_close: Optional[float] = None
    target: Optional[float] = None
    sl_price: Optional[float] = None


def run_backtest(df_15m: pd.DataFrame) -> pd.DataFrame:
    state = State()
    trades = []
    trade_id = 0
    pending_close_reason = None
    pending_entry_direction = None
    pending_signal_ts = None
    pending_signal_close = None

    for ts, bar in df_15m.iterrows():
        if pending_close_reason is not None and state.status == 'in_trade':
            trades[-1].update(_close(state, ts, bar['open'], pending_close_reason))
            state = State()
            pending_close_reason = None

        if pending_entry_direction is not None and state.status == 'watching':
            if _time_lt(ts, configs.MIN_ENTRY_TIME):
                pending_entry_direction = None
            else:
                direction = pending_entry_direction
                pending_entry_direction = None
                entry_price = bar['open']
                target = (entry_price * (1 + TARGET_PCT / 100) if direction == 'bullish'
                          else entry_price * (1 - TARGET_PCT / 100))
                sl_price = (entry_price * (1 - SL_PCT / 100) if direction == 'bullish'
                            else entry_price * (1 + SL_PCT / 100))
                trade_id += 1
                state = State(status='in_trade', direction=direction, entry_price=entry_price,
                              entry_ts=ts, signal_ts=pending_signal_ts, signal_close=pending_signal_close,
                              target=target, sl_price=sl_price)
                trades.append({
                    'trade_id': trade_id, 'direction': direction, 'entry_ts': ts,
                    'entry_price': round(entry_price, 2), 'target': round(target, 2),
                    'sl_price': round(sl_price, 2),
                })

        flip = bool(bar['trend_flip']) and not pd.isna(bar['trend'])
        direction_now = ('bullish' if bar['trend'] else 'bearish') if not pd.isna(bar['trend']) else None
        mins_to_close = bar['mins_to_close']

        if state.status == 'in_trade':
            if mins_to_close <= configs.EOD_SQUAREOFF_BEFORE_CLOSE_MIN:
                trades[-1].update(_close(state, ts, bar['open'], 'eod_squareoff'))
                state = State()
            elif (bar['low'] <= state.sl_price if state.direction == 'bullish'
                  else bar['high'] >= state.sl_price):
                fill = _stop_fill_price(state.direction, state.sl_price, bar['open'])
                trades[-1].update(_close(state, ts, fill, 'stop_loss'))
                state = State()
            elif (bar['high'] >= state.target if state.direction == 'bullish'
                  else bar['low'] <= state.target):
                fill = _target_fill_price(state.direction, state.target, bar['open'])
                trades[-1].update(_close(state, ts, fill, 'target_1pct'))
                state = State()
            elif flip and direction_now and direction_now != state.direction:
                pending_close_reason = 'trend_flip'
                if _signal_allowed(mins_to_close):
                    pending_entry_direction = direction_now
                    pending_signal_ts, pending_signal_close = ts, bar['close']

        if (state.status == 'watching' and pending_entry_direction is None
                and flip and direction_now and not bool(bar['is_day_end'])):
            if _signal_allowed(mins_to_close):
                pending_entry_direction = direction_now
                pending_signal_ts, pending_signal_close = ts, bar['close']

    return pd.DataFrame(trades)


def _close(state, exit_ts, exit_price, reason):
    pnl_points = ((exit_price - state.entry_price) if state.direction == 'bullish'
                  else (state.entry_price - exit_price))
    return {
        'exit_ts': exit_ts, 'exit_price': round(exit_price, 2), 'exit_reason': reason,
        'pnl_points': round(pnl_points, 2),
        'pnl_rs': round(pnl_points * configs.LOT_SIZE * 1, 2),
    }


def main():
    df_1m = load_futures_1min(configs.SYMBOL)
    df_15m = resample_ohlcv(df_1m, '15min')
    df_15m = compute_st(df_15m, configs.ST_PERIOD, configs.ST_MULTIPLIER)
    day = df_15m.index.normalize()
    day_last_bar_ts = df_15m.groupby(day).apply(lambda g: g.index.max())
    df_15m['mins_to_close'] = (day.map(day_last_bar_ts) - df_15m.index).total_seconds() / 60
    df_15m['is_day_end'] = df_15m['mins_to_close'] <= 0

    trades = run_backtest(df_15m)
    trades.to_csv(TRADE_LOG_FILE, index=False)
    print(f'Saved {len(trades)} trade(s) to {TRADE_LOG_FILE}')
    return trades


if __name__ == '__main__':
    main()
