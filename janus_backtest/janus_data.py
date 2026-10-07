"""
Per-session data for the Camarilla study: one record per trading session with that session's 1-minute bars of the EFFECTIVE (front) contract and
the Camarilla levels computed from the SAME contract's own previous session, so a level never mixes two contracts' prices on a roll day.
Real, un-adjusted prices. Fyers history only (the closer match to the Zerodha chart; the Angel One tail from 2026-09-02 is a later extension).

Contract choice per date reuses prometheus_backtest/data_loader_p3's early-roll rule (imported, not copied). Sessions in a configured data void
(janus_configs.FYERS_VOID, currently None: the 2026 void was filled on 2026-10-05) are skipped, never filled. Fyers zero-volume placeholder minutes are kept as delivered: they carry
the previous close in all four prices, so they can never extend a high or low.
"""

import sys

import pandas as pd

import janus_configs as configs
from janus_levels import camarilla_levels

sys.path.insert(0, configs.PROMETHEUS_DIR)
import data_loader_p3 as _p3  # noqa: E402


def _closed_dates() -> set:
    h = pd.read_csv(configs.HOLIDAYS_FILE)
    h['d'] = pd.to_datetime(h['Date'], format='%d %b %Y').dt.date
    return set(h[(h['Morning Session'] == 'Closed') & (h['Evening Session'] == 'Closed')]['d']) | _p3._load_fully_closed_dates()


def _in_void(d) -> bool:
    if configs.FYERS_VOID is None:
        return False
    return pd.Timestamp(configs.FYERS_VOID[0]).date() <= d <= pd.Timestamp(configs.FYERS_VOID[1]).date()


def load_sessions(symbol: str) -> list:
    """Sessions oldest first. Each: dict(date, symbol, expiry, pick, bars, prev, levels). `pick` is 'effective' when the early-roll contract had
    data that day and 'naive_front' when only the plain nearest-expiry contract did (the handful of old rollover weeks)."""
    start = pd.Timestamp(configs.DATA_START[symbol]).date()
    end = pd.Timestamp(configs.DATA_END).date() if configs.DATA_END else None
    calendar = _p3._discover_expiries(symbol)
    closed = _closed_dates()
    frames = {e: _p3._read_contract_file(configs.FYERS_DATA_DIR, symbol, e) for e in calendar}
    by_day = {}
    for e, df in frames.items():
        if len(df):
            df = df.copy()
            df['_d'] = df['time_stamp'].dt.date
            by_day[e] = {d: g.drop(columns='_d').set_index('time_stamp') for d, g in df.groupby('_d')}

    dates = sorted({d for days in by_day.values() for d in days})
    dates = [d for d in dates if d >= start and (end is None or d <= end) and d.weekday() < 5 and not _in_void(d)]
    out = []
    for d in dates:
        eff = _p3._effective_contract_for_date(d, calendar, closed)
        naive = _p3._naive_front_month_for_date(d, calendar)
        pick, expiry = None, None
        if eff is not None and d in by_day.get(eff, {}):
            pick, expiry = 'effective', eff
        elif naive is not None and d in by_day.get(naive, {}):
            pick, expiry = 'naive_front', naive
        if expiry is None:
            continue
        bars = by_day[expiry][d]
        prior = [x for x in by_day[expiry] if x < d]
        if len(bars) < configs.MIN_BARS_PER_SESSION or not prior:
            continue
        pd_ = max(prior)
        pbars = by_day[expiry][pd_]
        if (d - pd_).days > configs.MAX_PREV_SESSION_GAP_DAYS or len(pbars) < configs.MIN_BARS_PER_SESSION:
            continue
        prev = {'date': pd_, 'high': float(pbars['high'].max()), 'low': float(pbars['low'].min()), 'close': float(pbars['close'].iloc[-1])}
        out.append({'date': d, 'symbol': symbol, 'expiry': expiry, 'pick': pick, 'bars': bars, 'prev': prev,
                    'levels': camarilla_levels(prev['high'], prev['low'], prev['close'])})
    return out
