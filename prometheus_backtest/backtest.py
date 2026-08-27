"""
Prometheus — state-machine trade simulation.

Mirrors iris_production/iris.py's live watching -> in_trade -> watching
cycle: a 5-min entry-timeframe Supertrend flip, gated by 15-min regime
alignment, opens a position; it is held until the signal is invalidated
(an opposing flip) or the session forces a square-off. This is a deliberate
departure from iris_backtest's vectorized precompute-then-scan approach.

Fill convention: a Supertrend flip is only known once its bar has closed,
so both entries and trend-flip exits fill at the OPEN of the following bar
(one bar of unavoidable signal-confirmation lag). Administrative/price-based
exits (EOD square-off, day-end safety net, and — once enabled —
stop-loss/target/max-hold) are clock- or price-facts we can act on
immediately, so they fill at the same bar's open with no deferral.

stop_loss / profit_target / max_hold are implemented in the same pluggable
shape as trend_flip/eod_squareoff but excluded from
configs.EXIT_CHECKS_ENABLED in v1 — enable them one at a time later.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

import pandas as pd

import configs


@dataclass
class PrometheusState:
    status:          str               = 'watching'   # watching | in_trade
    direction:       Optional[str]     = None          # bullish | bearish
    entry_price:     Optional[float]   = None
    entry_ts:        Optional[pd.Timestamp] = None
    contract_expiry: Optional[str]     = None


def _time_lt(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() < datetime.strptime(hhmm, '%H:%M').time()


def _time_gt(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() > datetime.strptime(hhmm, '%H:%M').time()


def _time_ge(ts: pd.Timestamp, hhmm: str) -> bool:
    return ts.time() >= datetime.strptime(hhmm, '%H:%M').time()


def _in_skip_window(ts: pd.Timestamp) -> bool:
    for start_str, end_str in configs.SKIP_ENTRY_WINDOWS:
        start = datetime.strptime(start_str, '%H:%M').time()
        end   = datetime.strptime(end_str, '%H:%M').time()
        if start <= ts.time() < end:
            return True
    return False


def _regime_aligned(direction: str, regime_trend) -> bool:
    if regime_trend is None:
        return False
    return (direction == 'bullish') == bool(regime_trend)


# ---------------------------------------------------------------------------
# Pluggable exit checks (excluding trend_flip, which needs the entry-tf flip
# info handled separately in run_backtest since it alone is deferred to the
# next bar). Signature: (state, bar) -> Optional[str] exit reason.
# ---------------------------------------------------------------------------

def check_eod_squareoff(state: PrometheusState, bar: pd.Series) -> Optional[str]:
    if bool(bar['is_day_end']):
        return 'eod_squareoff (day_end)'
    if _time_ge(bar.name, configs.EOD_SQUAREOFF_TIME):
        return f'eod_squareoff ({configs.EOD_SQUAREOFF_TIME})'
    return None


def check_stop_loss(state: PrometheusState, bar: pd.Series) -> Optional[str]:
    # Uses bar low/high for a realistic intrabar touch, unlike flip/EOD
    # checks which only need the bar's own open/close.
    if configs.STOP_LOSS_POINTS is None or state.entry_price is None:
        return None
    if state.direction == 'bullish':
        if bar['low'] <= state.entry_price - configs.STOP_LOSS_POINTS:
            return 'stop_loss'
    else:
        if bar['high'] >= state.entry_price + configs.STOP_LOSS_POINTS:
            return 'stop_loss'
    return None


def check_profit_target(state: PrometheusState, bar: pd.Series) -> Optional[str]:
    if configs.PROFIT_TARGET_POINTS is None or state.entry_price is None:
        return None
    if state.direction == 'bullish':
        if bar['high'] >= state.entry_price + configs.PROFIT_TARGET_POINTS:
            return 'profit_target'
    else:
        if bar['low'] <= state.entry_price - configs.PROFIT_TARGET_POINTS:
            return 'profit_target'
    return None


def check_max_hold(state: PrometheusState, bar: pd.Series) -> Optional[str]:
    if configs.MAX_HOLD_MIN is None or state.entry_ts is None:
        return None
    if (bar.name - state.entry_ts).total_seconds() >= configs.MAX_HOLD_MIN * 60:
        return f'max_hold ({configs.MAX_HOLD_MIN}m)'
    return None


_EXIT_CHECK_FUNCS = {
    'eod_squareoff':  check_eod_squareoff,
    'stop_loss':      check_stop_loss,
    'profit_target':  check_profit_target,
    'max_hold':       check_max_hold,
}


def _exit_fill_price(reason: str, state: PrometheusState, bar: pd.Series) -> float:
    """
    stop_loss/profit_target are threshold breaches detected off bar low/high
    but must fill at the threshold itself — the breach happens mid-bar, and
    the bar's open is a price that existed *before* the breach, so filling
    there is optimistic. max_hold/eod_squareoff are clock facts with no
    natural fill price of their own, so they fill at the bar's open.
    """
    if reason == 'stop_loss':
        return (state.entry_price - configs.STOP_LOSS_POINTS if state.direction == 'bullish'
                else state.entry_price + configs.STOP_LOSS_POINTS)
    if reason == 'profit_target':
        return (state.entry_price + configs.PROFIT_TARGET_POINTS if state.direction == 'bullish'
                else state.entry_price - configs.PROFIT_TARGET_POINTS)
    return bar['open']


def _close_trade(trades: list, state: PrometheusState, exit_ts: pd.Timestamp,
                  exit_price: float, reason: str) -> None:
    pnl_points = ((exit_price - state.entry_price) if state.direction == 'bullish'
                  else (state.entry_price - exit_price))
    trades[-1].update({
        'exit_ts':      exit_ts,
        'exit_price':   exit_price,
        'exit_reason':  reason,
        'hold_min':     round((exit_ts - state.entry_ts).total_seconds() / 60, 1),
        'pnl_points':   round(pnl_points, 2),
        'pnl_rs':       round(pnl_points * configs.LOT_SIZE * configs.LOTS, 2),
    })


def run_backtest(df_5m: pd.DataFrame, df_15m: pd.DataFrame) -> pd.DataFrame:
    """
    Iterate the 5-minute entry-timeframe bars chronologically — the native
    decision cadence Iris itself runs on — maintaining a watching/in_trade
    state machine. df_5m must already carry 'trend'/'trend_flip'
    (compute_st) and 'is_day_end'; df_15m must carry 'trend' (compute_st).
    Returns a trade log DataFrame.
    """
    regime_series = df_15m['trend']

    state = PrometheusState()
    trades: list = []
    trade_id = 0
    regime_trend = None
    pending_entry_direction: Optional[str] = None
    pending_exit_reason: Optional[str] = None

    entry_tf_delta  = pd.Timedelta(minutes=configs.ENTRY_TF_MIN)
    regime_tf_delta = pd.Timedelta(minutes=configs.REGIME_TF_MIN)

    for ts, bar in df_5m.iterrows():
        # Roll the 15-min regime forward to the latest 15m bar that has
        # actually CLOSED by this 5m bar's own close (ts + ENTRY_TF_MIN).
        # Bars are left-labelled (a bar labelled 10:00 covers [10:00,10:15)
        # and closes at 10:15) — using `index <= ts` would read a 15m bar
        # that hasn't closed yet, a look-ahead bug. Mirrors iris.py's
        # _update_15m_regime, which only fires once next_5m_close lands on
        # a 15-min boundary.
        decision_ts = ts + entry_tf_delta
        eligible = regime_series[regime_series.index + regime_tf_delta <= decision_ts]
        if not eligible.empty and not pd.isna(eligible.iloc[-1]):
            regime_trend = bool(eligible.iloc[-1])

        # --- Fill anything deferred from the previous bar's signal ---
        if pending_exit_reason is not None and state.status == 'in_trade':
            _close_trade(trades, state, ts, bar['open'], pending_exit_reason)
            state = PrometheusState()
            pending_exit_reason = None

        if pending_entry_direction is not None and state.status == 'watching':
            trade_id += 1
            direction = pending_entry_direction
            pending_entry_direction = None
            state = PrometheusState(
                status='in_trade', direction=direction,
                entry_price=bar['open'], entry_ts=ts,
                contract_expiry=bar['contract_expiry'],
            )
            trades.append({
                'trade_id': trade_id, 'contract_expiry': state.contract_expiry,
                'direction': direction, 'entry_ts': ts, 'entry_price': state.entry_price,
            })

        flip = bool(bar['trend_flip']) and not pd.isna(bar['trend'])
        direction_now = ('bullish' if bar['trend'] else 'bearish') if not pd.isna(bar['trend']) else None

        # --- Exit detection on this bar ---
        if state.status == 'in_trade':
            # Priority 1: administrative/price-based exits — clock or price
            # facts we act on immediately, never deferred.
            reason = None
            for name in configs.EXIT_CHECKS_ENABLED:
                if name == 'trend_flip':
                    continue
                reason = _EXIT_CHECK_FUNCS[name](state, bar)
                if reason:
                    break
            if reason:
                exit_price = _exit_fill_price(reason, state, bar)
                _close_trade(trades, state, ts, exit_price, reason)
                state = PrometheusState()
            elif ('trend_flip' in configs.EXIT_CHECKS_ENABLED and flip
                  and direction_now and direction_now != state.direction):
                # Signal invalidated — confirmed on this bar's close, filled
                # at the next bar's open.
                pending_exit_reason = 'trend_flip'

        # --- Entry detection on this bar's close, filled next bar ---
        if (state.status == 'watching' and pending_entry_direction is None
                and flip and direction_now and not bool(bar['is_day_end'])):
            if _regime_aligned(direction_now, regime_trend):
                if (not _time_lt(ts, configs.MIN_ENTRY_TIME)
                        and not _in_skip_window(ts)
                        and not _time_gt(ts, configs.MAX_ENTRY_TIME)):
                    pending_entry_direction = direction_now

    return pd.DataFrame(trades)
