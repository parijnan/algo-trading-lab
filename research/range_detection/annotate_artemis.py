"""
annotate_artemis.py — Tag each Artemis trade with PA range state at entry.

For each instrument (nifty, sensex), loads daily OHLC from 1-min history,
runs the PA range detector, then joins the Artemis trade summary on entry date.

Usage:
    python annotate_artemis.py              # both instruments
    python annotate_artemis.py nifty        # single instrument
    python annotate_artemis.py sensex

Output:
    outputs/artemis_annotated_nifty.csv
    outputs/artemis_annotated_sensex.csv

Added columns:
    ep_direction      'up' | 'down' | 'initial'
    ep_bars_into      bars elapsed in current episode at entry
    ep_committed      True if episode was committed (confirmed) by entry date
    ep_established    True if committed AND bars_into >= min_range_bars
    ep_entry_spot_pct entry spot position within range (0=at low, 100=at high)
    ep_range_high     range upper bound at entry bar
    ep_range_low      range lower bound at entry bar
    ep_range_mid      midpoint at entry bar
    ep_width_pct      range width as % of midpoint at entry bar
    key_dist_pct      distance from spot to key level as % of range width
                        down → (range_high − spot) / width × 100
                        up   → (spot − range_low)  / width × 100
                        initial → None
    min_dist_pct      min(spot-to-PE-sell, CE-sell-to-spot) / spot × 100
                        endogenous containment proxy (ρ=0.32 Nifty 2019-2025)
    pe_dist_pct       (spot − pe_sell_strike) / spot × 100
    ce_dist_pct       (ce_sell_strike − spot) / spot × 100
"""

import os
import sys
import pandas as pd
from scipy import stats

BASE_DIR  = os.path.dirname(os.path.abspath(__file__))
REPO_ROOT = os.path.dirname(os.path.dirname(BASE_DIR))
sys.path.insert(0, BASE_DIR)

from resample import load_daily_extended
from range_detector_pa import compute_pa_ranges

MIN_RANGE_BARS   = 5
BREAKOUT_CONFIRM = 2

_TRADE_FILES = {
    'nifty':  os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty_rerun.csv'),
    'sensex': os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex_rerun.csv'),
}
_OUT_DIR = os.path.join(BASE_DIR, 'outputs')


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _stats(df: pd.DataFrame) -> dict:
    n    = len(df)
    wins = (df['total_pl_points'] > 0).sum() if n else 0
    return {
        'n':      n,
        'win':    round(wins / n * 100, 1) if n else 0.0,
        'avg':    round(df['total_pl_points'].mean(),   2) if n else 0.0,
        'median': round(df['total_pl_points'].median(), 2) if n else 0.0,
        'total':  round(df['total_pl_points'].sum(),    2) if n else 0.0,
    }


def _print_table(title: str, rows: list):
    hdr = f"  {'Group':<26} {'N':>5} {'Win%':>6} {'Avg':>8} {'Median':>8} {'Total':>10}"
    sep = '  ' + '-' * 67
    print(f'\n{title}')
    print(hdr)
    print(sep)
    for label, subset in rows:
        s = _stats(subset)
        print(f"  {label:<26} {s['n']:>5} {s['win']:>5.1f}%"
              f" {s['avg']:>+8.2f} {s['median']:>+8.2f} {s['total']:>+10.2f}")


def _null_ann() -> dict:
    return {k: None for k in [
        'ep_direction', 'ep_bars_into', 'ep_committed', 'ep_established',
        'ep_entry_spot_pct', 'ep_range_high', 'ep_range_low', 'ep_range_mid',
        'ep_width_pct', 'key_dist_pct', 'min_dist_pct', 'pe_dist_pct', 'ce_dist_pct',
    ]}


# ---------------------------------------------------------------------------
# Per-instrument pipeline
# ---------------------------------------------------------------------------

def run_instrument(instrument: str):
    print(f'\n{"=" * 70}')
    print(f'ARTEMIS ANNOTATION — {instrument.upper()}')
    print(f'{"=" * 70}')

    # Price data + PA ranges
    print(f'Loading {instrument} daily OHLC from 1-min history…')
    df_price = load_daily_extended(instrument)
    print(f'  {len(df_price)} bars  '
          f'{df_price.index[0].date()} → {df_price.index[-1].date()}')

    print(f'Computing PA ranges  '
          f'(min_range_bars={MIN_RANGE_BARS}, breakout_confirm={BREAKOUT_CONFIRM})…')
    result, episodes = compute_pa_ranges(
        df_price, start_idx=0,
        min_range_bars=MIN_RANGE_BARS,
        breakout_confirm=BREAKOUT_CONFIRM,
    )
    ep_dict   = {ep['episode_id']: ep for ep in episodes}
    price_idx = result.index
    print(f'  {len(episodes)} episodes total')

    # Trades
    trades = pd.read_csv(_TRADE_FILES[instrument], parse_dates=['entry_time'])
    trades['entry_date'] = trades['entry_time'].dt.normalize()
    print(f'Trades: {len(trades)}  '
          f'({trades["entry_date"].min().date()} → {trades["entry_date"].max().date()})')

    # Annotate
    ann_rows = []
    for _, trade in trades.iterrows():
        entry_date = trade['entry_date']
        spot       = float(trade['entry_spot'])
        pe_strike  = float(trade['pe_sell_strike'])
        ce_strike  = float(trade['ce_sell_strike'])

        # Endogenous containment distances
        pe_dist = (spot - pe_strike) / spot * 100
        ce_dist = (ce_strike - spot) / spot * 100

        # Last COMPLETE bar before entry_date.
        # side='left' gives pos-1 = Friday's bar for a Monday entry —
        # correct because the trade enters at 10:31am and Monday's close
        # (which determines same-day breakout direction) isn't known yet.
        pos = price_idx.searchsorted(entry_date, side='left') - 1
        if pos < 0:
            row = _null_ann()
            row['pe_dist_pct']  = round(pe_dist, 4)
            row['ce_dist_pct']  = round(ce_dist, 4)
            row['min_dist_pct'] = round(min(pe_dist, ce_dist), 4)
            ann_rows.append(row)
            continue

        bar   = result.iloc[pos]
        ep_id = int(bar['episode_id'])
        if ep_id < 0:
            row = _null_ann()
            row['pe_dist_pct']  = round(pe_dist, 4)
            row['ce_dist_pct']  = round(ce_dist, 4)
            row['min_dist_pct'] = round(min(pe_dist, ce_dist), 4)
            ann_rows.append(row)
            continue

        ep         = ep_dict[ep_id]
        bars_into  = pos - ep['start_idx'] + 1
        committed  = bars_into > BREAKOUT_CONFIRM
        established = committed and (bars_into >= MIN_RANGE_BARS)
        direction  = ep['direction']

        rh = float(bar['range_high'])
        rl = float(bar['range_low'])
        rm = float(bar['range_mid'])

        spot_pct  = (round(100 * (spot - rl) / (rh - rl), 1)
                     if rh > rl else None)
        width_pct = round((rh - rl) / rm * 100, 2) if rm > 0 else None

        if direction in ('up', 'down') and rh > rl:
            width = rh - rl
            key_dist_pct = round(
                ((rh - spot) / width * 100) if direction == 'down'
                else ((spot - rl) / width * 100),
                1,
            )
        else:
            key_dist_pct = None

        ann_rows.append({
            'ep_direction':      direction,
            'ep_bars_into':      int(bars_into),
            'ep_committed':      committed,
            'ep_established':    established,
            'ep_entry_spot_pct': spot_pct,
            'ep_range_high':     round(rh, 2),
            'ep_range_low':      round(rl, 2),
            'ep_range_mid':      round(rm, 2),
            'ep_width_pct':      width_pct,
            'key_dist_pct':      key_dist_pct,
            'pe_dist_pct':       round(pe_dist, 4),
            'ce_dist_pct':       round(ce_dist, 4),
            'min_dist_pct':      round(min(pe_dist, ce_dist), 4),
        })

    out = pd.concat([trades.reset_index(drop=True),
                     pd.DataFrame(ann_rows)], axis=1)
    out_path = os.path.join(_OUT_DIR, f'artemis_annotated_{instrument}.csv')
    out.to_csv(out_path, index=False)
    print(f'Saved: {out_path}')

    # Stats on tagged subset (has range data)
    tagged  = out[out['ep_direction'].notna()].copy()
    missing = len(out) - len(tagged)
    if missing:
        print(f'\nNote: {missing} trades had no range data (before price history start)')

    overall = _stats(tagged)
    print(f'\n  Overall: win% {overall["win"]:.1f}%  |  '
          f'avg {overall["avg"]:+.2f} pts  |  total {overall["total"]:+.2f} pts')

    # Correlation table
    print('\n── Spearman correlations with total_pl_points ─────────────────────────')
    for col in ['key_dist_pct', 'min_dist_pct', 'ep_width_pct', 'ep_entry_spot_pct']:
        mask = out[col].notna() & out['total_pl_points'].notna()
        sub  = out[mask]
        if len(sub) < 10:
            continue
        rho, p = stats.spearmanr(sub[col], sub['total_pl_points'])
        stars = '***' if p < 0.001 else ('**' if p < 0.01 else ('*' if p < 0.05 else ''))
        print(f'  {col:<22} ρ={rho:+.3f}  p={p:.4f}  n={len(sub)}{stars}')

    # key_dist within min_dist quartiles (incremental test)
    with_both = out[out['key_dist_pct'].notna() & out['min_dist_pct'].notna() &
                    out['total_pl_points'].notna()].copy()
    if len(with_both) >= 20:
        with_both['min_dist_q'] = pd.qcut(with_both['min_dist_pct'], 4,
                                           labels=['Q1','Q2','Q3','Q4'])
        print('\n── key_dist_pct × P&L correlation within min_dist_pct quartiles ────────')
        for q in ['Q1', 'Q2', 'Q3', 'Q4']:
            sub = with_both[with_both['min_dist_q'] == q]
            if len(sub) < 5:
                continue
            rho, p = stats.spearmanr(sub['key_dist_pct'], sub['total_pl_points'])
            print(f'  min_dist {q}: n={len(sub):>3}  key_dist ρ={rho:+.3f}  p={p:.3f}')

    _print_table('By range direction at entry:', [
        ('up',      tagged[tagged['ep_direction'] == 'up']),
        ('down',    tagged[tagged['ep_direction'] == 'down']),
        ('initial', tagged[tagged['ep_direction'] == 'initial']),
    ])

    _print_table('By episode state at entry:', [
        ('established',       tagged[tagged['ep_established'] == True]),
        ('committed only',    tagged[(tagged['ep_committed'] == True)
                                     & (tagged['ep_established'] == False)]),
        ('not yet committed', tagged[tagged['ep_committed'] == False]),
    ])

    _print_table('By entry spot position in range:', [
        ('lower third  (0–33%)',   tagged[tagged['ep_entry_spot_pct'] <= 33]),
        ('middle third (34–66%)',  tagged[(tagged['ep_entry_spot_pct'] > 33)
                                          & (tagged['ep_entry_spot_pct'] <= 66)]),
        ('upper third  (67–100%)', tagged[tagged['ep_entry_spot_pct'] > 66]),
    ])

    if tagged['key_dist_pct'].notna().sum() >= 20:
        q33 = tagged['key_dist_pct'].quantile(0.33)
        q67 = tagged['key_dist_pct'].quantile(0.67)
        _print_table(f'By key_dist_pct  (P33={q33:.1f}  P67={q67:.1f}):', [
            (f'far from key  (≥{q67:.1f}%)',  tagged[tagged['key_dist_pct'] >= q67]),
            (f'mid  ({q33:.1f}–{q67:.1f}%)',  tagged[(tagged['key_dist_pct'] >= q33)
                                                      & (tagged['key_dist_pct'] < q67)]),
            (f'close to key  (<{q33:.1f}%)',  tagged[tagged['key_dist_pct'] < q33]),
        ])

    print()


# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------

if __name__ == '__main__':
    instruments = sys.argv[1:] if len(sys.argv) > 1 else ['nifty', 'sensex']
    for inst in instruments:
        run_instrument(inst)
