"""
Branch 4 — Realized vs Implied Vol

For each trade: compare realized vol (RV) over the holding period with the entry
implied vol (IV) at the sell strike.  rv_iv_ratio > 1 means spot moved more than
the market priced — vol was underpriced, we got hurt.

RV estimator: quadratic variation
    rv_ann% = sqrt( Σ r_i² / T_years ) × 100
    r_i     = log(S_i / S_{i-1}) for ALL consecutive bars (overnight gaps included)
    T_years = elapsed calendar time from entry to last bar / 365

Why QV instead of std × sqrt(bars): the trade logs span multiple calendar days with
overnight (and weekend) gaps.  std × sqrt(n_bars) mis-weights gap moves as 1-minute
returns.  QV is gap-robust and matches mibian's T = dte/365 calendar convention.

Scope:
  Athena (124 trades): all exits are pre_expiry (no exit-type variation).
    Segment by win/loss and period instead.
  Artemis (173 trades): index_sl, option_sl, elm exits per side.
    Exit type for the trade = exit reason of the first-exiting side (chronologically).
    elm is its own bucket — it is SEBI-regulatory, not a vol or SL outcome.

Entry IV:
  Athena: near_iv from Branch 3 output (avg of ce_sell_iv + pe_sell_iv at bar 0).
  Artemis: avg of (pe_sell_iv + ce_sell_iv) at bar 0 from the IV cache.

Run from repo root:
  python research/greek_analysis/realized_vs_implied/run.py
"""

import os
import sys
import glob
import numpy as np
import pandas as pd
from scipy import stats

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))

# Athena paths
ATHENA_SUMMARY   = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_summary.csv')
ATHENA_LOGS_DIR  = os.path.join(REPO_ROOT, 'athena_backtest', 'data', 'trade_logs')
BRANCH3_CSV      = os.path.join(_HERE, '..', 'iv_term_structure', 'data', 'iv_term_structure.csv')
IV_CACHE_DIR     = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache')

# Artemis paths
NIFTY_SUMMARY    = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty.csv')
SENSEX_SUMMARY   = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex.csv')
NIFTY_LOGS_DIR   = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_nifty')
SENSEX_LOGS_DIR  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_sensex')
IV_CACHE_ARTEMIS = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache_artemis')

OUTPUT_DIR        = os.path.join(_HERE, 'data')
ATHENA_CSV        = os.path.join(OUTPUT_DIR, 'rv_iv_athena.csv')
ARTEMIS_CSV       = os.path.join(OUTPUT_DIR, 'rv_iv_artemis.csv')

PERIOD_CUT = '2023-01-01'


# ---------------------------------------------------------------------------
# Quadratic-variation realized vol estimator
# ---------------------------------------------------------------------------

def _realized_vol(spot_series: pd.Series, entry_time: pd.Timestamp,
                  exit_time: pd.Timestamp) -> float | None:
    """
    Annualized realized vol via quadratic variation.
    Returns % (e.g. 17.4 means 17.4% pa).
    Returns None if fewer than 2 valid bars or T ≈ 0.
    """
    s = spot_series.dropna()
    s = s[s > 0]
    if len(s) < 2:
        return None
    T_years = (exit_time - entry_time).total_seconds() / (365.0 * 86400.0)
    if T_years < 1e-6:
        return None
    log_ret = np.log(s.values[1:] / s.values[:-1])
    qv      = float(np.sum(log_ret ** 2))
    return float(np.sqrt(qv / T_years) * 100.0)


# ---------------------------------------------------------------------------
# Athena
# ---------------------------------------------------------------------------

def _load_athena_log(trade_id: int, entry_date: str) -> pd.DataFrame:
    path = os.path.join(ATHENA_LOGS_DIR, f'trade_{trade_id:04d}_{entry_date}.csv')
    df   = pd.read_csv(path, parse_dates=['time_stamp'])
    return df.drop_duplicates(subset='time_stamp', keep='first').reset_index(drop=True)


def build_athena() -> pd.DataFrame:
    summary = pd.read_csv(ATHENA_SUMMARY, parse_dates=['entry_time', 'exit_time'])
    summary['trade_id']   = range(1, len(summary) + 1)
    summary['entry_date'] = summary['entry_time'].dt.date.astype(str)

    branch3 = pd.read_csv(BRANCH3_CSV)
    branch3 = branch3[['trade_id', 'near_iv']].copy()

    rows = []
    for _, row in summary.iterrows():
        tid = int(row['trade_id'])
        log = _load_athena_log(tid, str(row['entry_date']))

        rv = _realized_vol(log['spot'], row['entry_time'], row['exit_time'])
        if rv is None:
            print(f"  WARN Athena trade {tid}: RV computation failed")
            continue

        rows.append({
            'trade_id':   tid,
            'entry_date': str(row['entry_date']),
            'entry_vix':  float(row['entry_vix']),
            'total_pl':   float(row['total_pl_points']),
            'is_win':     float(row['total_pl_points']) > 0,
            'period':     '2020-22' if str(row['entry_date']) < PERIOD_CUT else '2023+',
            'rv_ann':     round(rv, 4),
            'exit_type':  'pre_expiry',
        })

    df = pd.DataFrame(rows)
    df = df.merge(branch3, on='trade_id', how='left')
    df['rv_iv_ratio'] = (df['rv_ann'] / df['near_iv']).round(5)
    return df


# ---------------------------------------------------------------------------
# Artemis
# ---------------------------------------------------------------------------

def _load_artemis_summary(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['entry_time', 'pe_exit_time', 'ce_exit_time'])
    df = df[df['week_outcome'] == 'traded'].reset_index(drop=True)
    df['trade_id']    = range(1, len(df) + 1)
    df['entry_date']  = df['entry_time'].dt.date.astype(str)
    df['expiry_date'] = pd.to_datetime(df['expiry']).dt.date.astype(str)
    return df


def _artemis_exit_type(row: pd.Series) -> tuple[str, pd.Timestamp]:
    """Return (exit_reason, first_exit_time) for the side that exited first."""
    pe_t = row.get('pe_exit_time')
    ce_t = row.get('ce_exit_time')
    pe_r = str(row.get('pe_exit_reason', ''))
    ce_r = str(row.get('ce_exit_reason', ''))

    pe_valid = pd.notna(pe_t)
    ce_valid = pd.notna(ce_t)

    if pe_valid and ce_valid:
        if pe_t <= ce_t:
            return pe_r, pe_t
        else:
            return ce_r, ce_t
    elif pe_valid:
        return pe_r, pe_t
    elif ce_valid:
        return ce_r, ce_t
    else:
        return 'unknown', pd.NaT


def _load_artemis_log(trade_id: int, expiry_date: str, logs_dir: str) -> pd.DataFrame:
    path = os.path.join(logs_dir, f'trade_{trade_id:04d}_{expiry_date}.csv')
    df   = pd.read_csv(path, parse_dates=['time_stamp'])
    return df.drop_duplicates(subset='time_stamp', keep='first').reset_index(drop=True)


def _load_artemis_entry_iv(trade_id: int, expiry_date: str,
                            instrument: str) -> float | None:
    path = os.path.join(IV_CACHE_ARTEMIS,
                        f'trade_{trade_id:04d}_{expiry_date}_{instrument}.parquet')
    try:
        iv_df = pd.read_parquet(path)
    except FileNotFoundError:
        return None
    if iv_df.empty:
        return None
    r = iv_df.iloc[0]
    pe_iv = r.get('pe_sell_iv')
    ce_iv = r.get('ce_sell_iv')
    vals  = [v for v in [pe_iv, ce_iv] if pd.notna(v)]
    return float(np.mean(vals)) if vals else None


def _run_artemis_instrument(instrument: str, summary_path: str,
                             logs_dir: str) -> list[dict]:
    summary  = _load_artemis_summary(summary_path)
    n_trades = len(summary)
    print(f"\n  {instrument.upper()} — {n_trades} trades")

    rows = []
    for _, row in summary.iterrows():
        tid         = int(row['trade_id'])
        expiry_date = str(row['expiry_date'])

        log = _load_artemis_log(tid, expiry_date, logs_dir)
        if len(log) < 2:
            print(f"  WARN {instrument} trade {tid}: too few bars")
            continue

        exit_type, first_exit_time = _artemis_exit_type(row)
        exit_time = first_exit_time if pd.notna(first_exit_time) else row['entry_time']

        rv = _realized_vol(log['spot'], row['entry_time'], exit_time)
        if rv is None:
            print(f"  WARN {instrument} trade {tid}: RV computation failed")
            continue

        near_iv = _load_artemis_entry_iv(tid, expiry_date, instrument)
        if near_iv is None:
            print(f"  WARN {instrument} trade {tid}: IV cache missing")
            continue

        rows.append({
            'instrument':  instrument,
            'trade_id':    tid,
            'entry_date':  str(row['entry_date']),
            'entry_vix':   float(row['entry_vix']),
            'total_pl':    float(row['total_pl_points']),
            'is_win':      float(row['total_pl_points']) > 0,
            'period':      '2020-22' if str(row['entry_date']) < PERIOD_CUT else '2023+',
            'exit_type':   exit_type,
            'near_iv':     round(near_iv, 4),
            'rv_ann':      round(rv, 4),
        })

    df = pd.DataFrame(rows)
    if not df.empty:
        df['rv_iv_ratio'] = (df['rv_ann'] / df['near_iv']).round(5)
    return df.to_dict('records')


# ---------------------------------------------------------------------------
# Analysis helpers
# ---------------------------------------------------------------------------

def _spearman(x: pd.Series, y: pd.Series, label: str) -> None:
    mask = x.notna() & y.notna()
    xm, ym = x[mask], y[mask]
    coef, p = stats.spearmanr(xm, ym)
    stars = '***' if p < 0.01 else ('**' if p < 0.05 else ('*' if p < 0.10 else ''))
    print(f"  {label:<32s}  IC={coef:+.4f}  p={p:.4f} {stars}  n={len(xm)}")


def _rv_summary(grp: pd.DataFrame, label: str) -> None:
    if grp.empty:
        return
    print(f"  {label:<30s}  n={len(grp):3d}  "
          f"rv={grp['rv_ann'].mean():+.2f}  "
          f"iv={grp['near_iv'].mean():+.2f}  "
          f"ratio={grp['rv_iv_ratio'].mean():.4f}  "
          f"median_ratio={grp['rv_iv_ratio'].median():.4f}")


def _print_athena(df: pd.DataFrame) -> None:
    print("\n" + "=" * 72)
    print("BRANCH 4 — REALIZED VS IMPLIED VOL — ATHENA")
    print("=" * 72)
    print(f"\n  Trades: {len(df)}")
    print(f"\n  Full-sample RV vs IV:")
    _rv_summary(df, 'All trades')
    _rv_summary(df[df['is_win']], 'Winners')
    _rv_summary(df[~df['is_win']], 'Losers')

    print(f"\n  Spearman IC (full sample):")
    _spearman(df['rv_iv_ratio'], df['total_pl'], 'rv_iv_ratio vs P&L')
    _spearman(df['rv_ann'],      df['total_pl'], 'rv_ann vs P&L')
    _spearman(df['near_iv'],     df['total_pl'], 'entry_iv vs P&L')
    _spearman(df['entry_vix'],   df['total_pl'], 'entry_vix vs P&L')

    print(f"\n  Period split:")
    for period, grp in df.groupby('period'):
        print(f"\n    {period} (n={len(grp)}):")
        _rv_summary(grp, '  All')
        _rv_summary(grp[grp['is_win']], '  Winners')
        _rv_summary(grp[~grp['is_win']], '  Losers')
        _spearman(grp['rv_iv_ratio'], grp['total_pl'], '  rv_iv_ratio vs P&L')

    print(f"\n  Branch 1 cross-check:")
    print(f"  Expected: losers have rv_iv_ratio > 1 (vol underpriced, vega-driven loss).")
    wins   = df[df['is_win']]
    losses = df[~df['is_win']]
    pct_ratio_gt1_win  = (wins['rv_iv_ratio']   > 1).mean() * 100
    pct_ratio_gt1_loss = (losses['rv_iv_ratio'] > 1).mean() * 100
    print(f"  rv_iv_ratio > 1: winners {pct_ratio_gt1_win:.1f}%,  losers {pct_ratio_gt1_loss:.1f}%")


def _print_artemis(df: pd.DataFrame) -> None:
    print("\n" + "=" * 72)
    print("BRANCH 4 — REALIZED VS IMPLIED VOL — ARTEMIS")
    print("=" * 72)

    for instrument in ['nifty', 'sensex']:
        dfi = df[df['instrument'] == instrument]
        if dfi.empty:
            continue
        print(f"\n  {instrument.upper()} (n={len(dfi)}):")
        _rv_summary(dfi, '  All trades')
        _rv_summary(dfi[dfi['is_win']], '  Winners')
        _rv_summary(dfi[~dfi['is_win']], '  Losers')

        print(f"\n  Spearman IC:")
        _spearman(dfi['rv_iv_ratio'], dfi['total_pl'], '  rv_iv_ratio vs P&L')
        _spearman(dfi['entry_vix'],   dfi['total_pl'], '  entry_vix vs P&L')

        print(f"\n  By exit type (first-exiting side):")
        for exit_type, grp in dfi.groupby('exit_type'):
            _rv_summary(grp[grp['is_win']],   f'  {exit_type:12s} wins  ')
            _rv_summary(grp[~grp['is_win']],  f'  {exit_type:12s} losses')

        print(f"\n  Branch 1 cross-check:")
        print(f"  Expected: index_sl losers have highest rv_iv_ratio (directional spot move).")
        wins   = dfi[dfi['is_win']]
        losses = dfi[~dfi['is_win']]
        pct_gt1_win  = (wins['rv_iv_ratio']   > 1).mean() * 100
        pct_gt1_loss = (losses['rv_iv_ratio'] > 1).mean() * 100
        print(f"  rv_iv_ratio > 1: winners {pct_gt1_win:.1f}%,  losers {pct_gt1_loss:.1f}%")

        print(f"\n  Period split:")
        for period, grp in dfi.groupby('period'):
            print(f"    {period} (n={len(grp)})  ratio={grp['rv_iv_ratio'].mean():.4f}  ", end='')
            coef, p = stats.spearmanr(grp['rv_iv_ratio'], grp['total_pl'])
            print(f"IC(rv_iv_ratio,PL)={coef:+.4f}  p={p:.3f}")


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def run():
    print("Realized vs Implied Vol — Branch 4")
    os.makedirs(OUTPUT_DIR, exist_ok=True)

    print("\nBuilding Athena dataset…")
    df_a = build_athena()
    df_a.to_csv(ATHENA_CSV, index=False)
    print(f"Saved {len(df_a)} rows → {ATHENA_CSV}")

    print("\nBuilding Artemis dataset…")
    records  = _run_artemis_instrument('nifty',  NIFTY_SUMMARY,  NIFTY_LOGS_DIR)
    records += _run_artemis_instrument('sensex', SENSEX_SUMMARY, SENSEX_LOGS_DIR)
    df_art = pd.DataFrame(records)
    df_art.to_csv(ARTEMIS_CSV, index=False)
    print(f"Saved {len(df_art)} rows → {ARTEMIS_CSV}")

    _print_athena(df_a)
    _print_artemis(df_art)


if __name__ == '__main__':
    run()
