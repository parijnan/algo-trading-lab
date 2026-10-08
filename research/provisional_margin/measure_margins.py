"""
Provisional-margin measurement (plans/hestia-provisional-all-engines.md section 1). For each instrument, over its real one-minute history, per contract on the
dates that contract is the effective trading contract:

  * U  - the bound on |tick close - real close| of a 15-minute bar: the larger of the final traded minute's high-low range and the move from that minute's
         close to the next minute's open (counted only when that minute opens within GAP_WINDOW_MIN of the boundary), as a percent of the bar's close.
  * d  - on a bar whose REAL close did not flip the trend, the distance (percent of close) from the real close to the previous supertrend on the side it stayed.
  * r  = max(0, U - d): the margin a provisional action would need so that no tick-close error within the bound could make it cross the line when the real
         close did not (a harmful action). Risk population = non-flip bars with a traded final minute; real flips are never in it.
  * clearance - on a real flip bar, |real close - previous supertrend| / close * 100; coverage(m) = share of real flips with clearance > m.

m* = smallest grid value >= max(r) over bars before FIT_END; verified out of sample on bars from FIT_END (count of bars with r > m* must be 0).

    python research/provisional_margin/measure_margins.py            # all four
    python research/provisional_margin/measure_margins.py SILVERMIC
"""

import math
import os
import sys

import numpy as np
import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import margin_configs as cfg  # noqa: E402
from hestia_core.indicators import compute_st  # noqa: E402
import data_loader_p3 as p3  # noqa: E402


def build_bars(minutes: pd.DataFrame, bar_min: int = cfg.BAR_MINUTES, gap_window_min: int = cfg.GAP_WINDOW_MIN) -> pd.DataFrame:
    """15-minute bars on the clock (labelled by window start) from one contract's one-minute rows, plus the final-minute facts the bound needs.
    Columns: open, high, low, close, volume, n_min, final_traded (a row at start+bar_min-1 with volume > 0), final_range (its high-low), gap_next (|next minute's open
    - that minute's close| if the next row opens within gap_window_min of the boundary, else 0), U_pct (NaN unless final_traded)."""
    m = minutes.sort_values('time_stamp').reset_index(drop=True).copy()
    m['win'] = m['time_stamp'].dt.floor(f'{bar_min}min')
    g = m.groupby('win')
    bars = pd.DataFrame({'open': g['open'].first(), 'high': g['high'].max(), 'low': g['low'].min(), 'close': g['close'].last(), 'volume': g['volume'].sum(), 'n_min': g.size()})
    nxt_ts, nxt_open = m['time_stamp'].shift(-1), m['open'].shift(-1)
    is_final = m['time_stamp'] == m['win'] + pd.Timedelta(minutes=bar_min - 1)
    boundary = m['win'] + pd.Timedelta(minutes=bar_min)
    gap_ok = (nxt_ts - boundary >= pd.Timedelta(0)) & (nxt_ts - boundary <= pd.Timedelta(minutes=gap_window_min))
    fin = pd.DataFrame({'win': m['win'], 'final_traded': is_final & (m['volume'] > 0), 'final_range': (m['high'] - m['low']).where(is_final),
                        'gap_next': (nxt_open - m['close']).abs().where(is_final & gap_ok, 0.0).where(is_final)})
    fin = fin[is_final].set_index('win')
    bars = bars.join(fin[['final_traded', 'final_range', 'gap_next']])
    bars['final_traded'] = bars['final_traded'].eq(True)
    bars['U_pct'] = (bars[['final_range', 'gap_next']].max(axis=1) / bars['close'] * 100).where(bars['final_traded'])
    return bars


def add_signal(bars: pd.DataFrame, period: int, multiplier: float) -> pd.DataFrame:
    """Supertrend with the live code (hestia_core.indicators.compute_st), then per bar: the previous supertrend, whether the real bar flipped, the clearance or
    distance d, and the required margin r. Bars without a valid previous supertrend are left out of every population."""
    b = bars.reset_index().rename(columns={'win': 'start'})
    st = compute_st(b[['open', 'high', 'low', 'close']], period, multiplier)
    b['supertrend'], b['trend'] = st['supertrend'], st['trend']
    b['prev_st'] = b['supertrend'].shift(1)
    valid = b['supertrend'].notna() & b['prev_st'].notna()
    b['flip'] = valid & (b['trend'] != b['trend'].shift(1))
    b['dist_pct'] = ((b['close'] - b['prev_st']).abs() / b['close'] * 100).where(valid)
    b['risk_bar'] = valid & ~b['flip'] & b['U_pct'].notna()
    b['r_pct'] = (b['U_pct'] - b['dist_pct']).clip(lower=0).where(b['risk_bar'])
    b['flip_bar'] = b['flip'] & b['final_traded']
    return b


def ceil_to_grid(x: float, step: float = cfg.GRID_STEP_PCT) -> float:
    """Smallest multiple of `step` that is >= x (guarding float noise: 0.0500000001 stays 0.05)."""
    return round(math.ceil(round(x / step, 9)) * step, 6)


def rule(b: pd.DataFrame, fit_end: str = cfg.FIT_END, step: float = cfg.GRID_STEP_PCT) -> dict:
    """The pre-registered rule on a table from add_signal (with a 'start' column). Returns m*, the out-of-sample check and the numbers reported beside it."""
    split = pd.Timestamp(fit_end)
    risk, flips = b[b['risk_bar']], b[b['flip_bar']]
    fit, oos = risk[risk['start'] < split], risk[risk['start'] >= split]
    m_fit = float(fit['r_pct'].max()) if len(fit) else 0.0
    m_star = ceil_to_grid(m_fit, step)
    violations = int((oos['r_pct'] > m_star).sum())
    m_final = m_star if violations == 0 else ceil_to_grid(float(risk['r_pct'].max()), step)
    out = {'fit_bars': len(fit), 'oos_bars': len(oos), 'flip_bars': len(flips), 'r_fit_max': m_fit, 'm_star_fit': m_star, 'oos_violations_at_m_star': violations,
           'm_star': m_final, 'm_star_basis': 'fit period' if violations == 0 else 'ALL history (out-of-sample check failed)'}
    for p in cfg.REPORT_PERCENTILES:
        out[f'r_p{p}'] = float(np.percentile(risk['r_pct'], p)) if len(risk) else float('nan')
    out['r_max_all'] = float(risk['r_pct'].max()) if len(risk) else float('nan')
    out['coverage_at_m_star'] = float((flips['dist_pct'] > m_final).mean()) if len(flips) else float('nan')
    out['coverage_at_m_star_oos'] = float((flips[flips['start'] >= split]['dist_pct'] > m_final).mean()) if (flips['start'] >= split).any() else float('nan')
    out['clearance_median'] = float(flips['dist_pct'].median()) if len(flips) else float('nan')
    return out


def grid_table(b: pd.DataFrame, step: float = cfg.GRID_STEP_PCT, top: float = cfg.GRID_MAX_PCT) -> pd.DataFrame:
    """For each margin on the grid: coverage of real flips, and the number of risk bars whose r exceeds it (worst-case harmful provisional actions)."""
    risk, flips = b[b['risk_bar']], b[b['flip_bar']]
    ms = np.round(np.arange(0.0, top + step / 2, step), 6)
    return pd.DataFrame({'margin_pct': ms, 'coverage': [float((flips['dist_pct'] > m).mean()) if len(flips) else float('nan') for m in ms],
                         'risk_bars_over': [int((risk['r_pct'] > m).sum()) for m in ms]})


def _closed_dates() -> set:
    h = pd.read_csv(cfg.HOLIDAYS_FILE)
    h['d'] = pd.to_datetime(h['Date'], format='%d %b %Y').dt.date
    return set(h[(h['Morning Session'] == 'Closed') & (h['Evening Session'] == 'Closed')]['d']) | p3._load_fully_closed_dates()


def instrument_table(symbol: str) -> pd.DataFrame:
    """All 15-minute bars of the instrument on dates its contract was the effective one, with the signal columns, per contract (no splice)."""
    _, period, mult = cfg.INSTRUMENTS[symbol]
    start = pd.Timestamp(cfg.DATA_START[symbol]).date()
    calendar, closed = p3._discover_expiries(symbol), _closed_dates()
    frames = {e: p3._read_contract_file(p3.FYERS_DATA_DIR, symbol, e) for e in calendar}
    days = sorted({d for f in frames.values() if len(f) for d in f['time_stamp'].dt.date.unique()})
    effective = {}
    for d in days:
        if d < start or d.weekday() >= 5:
            continue
        eff = p3._effective_contract_for_date(d, calendar, closed)
        naive = p3._naive_front_month_for_date(d, calendar)
        pick = eff if (eff is not None and len(frames.get(eff, ())) and d in set(frames[eff]['time_stamp'].dt.date)) else naive
        if pick is not None and len(frames.get(pick, ())):
            effective.setdefault(pick, set()).add(d)
    out = []
    for e, dset in effective.items():
        b = add_signal(build_bars(frames[e]), period, mult)
        b = b[b['start'].dt.date.isin(dset)].copy()
        b.insert(0, 'expiry', e)
        out.append(b)
    t = pd.concat(out).sort_values('start').reset_index(drop=True)
    return t


def main(argv):
    os.makedirs(cfg.OUTPUT_DIR, exist_ok=True)
    rows = []
    for symbol in (argv or list(cfg.INSTRUMENTS)):
        engine, period, mult = cfg.INSTRUMENTS[symbol]
        print(f'{symbol} ({engine}, ST {period}/{mult}) ...', flush=True)
        t = instrument_table(symbol)
        t.to_csv(os.path.join(cfg.OUTPUT_DIR, f'bars_{symbol}.csv'), index=False)
        r = rule(t)
        r.update({'symbol': symbol, 'engine': engine, 'st': f'{period}/{mult}', 'bars': len(t), 'first': str(t['start'].min()), 'last': str(t['start'].max())})
        rows.append(r)
        grid_table(t).to_csv(os.path.join(cfg.OUTPUT_DIR, f'grid_{symbol}.csv'), index=False)
        top = t[t['risk_bar']].nlargest(cfg.TOP_BARS, 'r_pct')[['expiry', 'start', 'close', 'prev_st', 'dist_pct', 'U_pct', 'r_pct', 'final_range', 'gap_next', 'volume']]
        top.to_csv(os.path.join(cfg.OUTPUT_DIR, f'top_r_{symbol}.csv'), index=False)
    res = pd.DataFrame(rows)
    res.to_csv(os.path.join(cfg.OUTPUT_DIR, 'margin_results.csv'), index=False)
    pd.set_option('display.width', 250)
    pd.set_option('display.max_columns', None)
    print(res.T.to_string())


if __name__ == '__main__':
    main(sys.argv[1:])
