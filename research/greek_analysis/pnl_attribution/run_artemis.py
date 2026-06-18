"""
Artemis Branch 1 — P&L Attribution

Decompose each Artemis trade's base-lot P&L into:
  delta_contrib:  Δspot × net_delta
  gamma_contrib:  ½ × Δspot² × net_gamma
  theta_contrib:  Δt_days × net_theta
  vega_contrib:   ΔIV_leg × net_vega  (per-leg IV, summed across legs)
  residual:       actual_mtm − (delta + gamma + theta + vega)

Attribution covers base lots only (pe_pl + ce_pl). Add lot P&L is tracked
as a separate summary field because the additional buy leg LTP is not logged
per bar, making per-bar Greek decomposition impossible for that component.

Artemis-specific structure vs Athena:
  - Single weekly expiry (not dual sell/buy expiries)
  - 4 legs: pe_sell, pe_buy, ce_sell, ce_buy — all with variable strikes
    per bar (change after SL-triggered adjustment or re-entry)
  - Status-gated: leg attribution skipped when its side is 'closed'
  - actual_mtm = Σ Δ(pe_pl) + Δ(ce_pl) over consecutive bar pairs.
    The first bar's within-bar P&L (entry open → first bar close) is
    unattributed and lands in residual.

Run from repo root:
  python research/greek_analysis/pnl_attribution/run_artemis.py

Cold run: ~60–90 min (IV via mibian, 173 trades × ~1350 bars × 4 legs).
Warm run: <1 min (cached parquet).
"""

import os
import sys
import pandas as pd

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', '..', '..'))

from research.greek_analysis.greek_engine import (
    RISK_FREE_RATE, compute_iv, compute_greeks, get_dte_days,
)

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
_HERE     = os.path.dirname(__file__)
REPO_ROOT = os.path.abspath(os.path.join(_HERE, '..', '..', '..'))

NIFTY_SUMMARY  = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_nifty.csv')
SENSEX_SUMMARY = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex.csv')
NIFTY_LOGS_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_nifty')
SENSEX_LOGS_DIR = os.path.join(REPO_ROOT, 'artemis_backtest', 'data', 'trade_logs_sensex')

IV_CACHE_DIR = os.path.join(REPO_ROOT, 'research', 'greek_analysis', 'data', 'iv_cache_artemis')
OUTPUT_DIR   = os.path.join(_HERE, 'data')
OUTPUT_PATH  = os.path.join(OUTPUT_DIR, 'pnl_attribution_artemis.csv')

LOT_SIZE = {'nifty': 65, 'sensex': 20}

# IV column names for the cache parquet
IV_COLS = ['time_stamp', 'pe_sell_iv', 'pe_buy_iv', 'ce_sell_iv', 'ce_buy_iv']

# Each entry: (iv_col, ltp_col, strike_col, option_type, direction, status_col)
#   status_col: the trade log column that holds 'closed' when this side is done
LEGS = [
    ('pe_sell_iv', 'pe_sell_ltp', 'pe_sell_strike', 'pe', -1, 'pe_status'),
    ('pe_buy_iv',  'pe_buy_ltp',  'pe_buy_strike',  'pe', +1, 'pe_status'),
    ('ce_sell_iv', 'ce_sell_ltp', 'ce_sell_strike', 'ce', -1, 'ce_status'),
    ('ce_buy_iv',  'ce_buy_ltp',  'ce_buy_strike',  'ce', +1, 'ce_status'),
]


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def _load_summary(path: str) -> pd.DataFrame:
    df = pd.read_csv(path, parse_dates=['entry_time', 'expiry'])
    df = df[df['week_outcome'] == 'traded'].reset_index(drop=True)
    df['trade_id']    = range(1, len(df) + 1)
    df['entry_date']  = df['entry_time'].dt.date.astype(str)
    df['expiry_date'] = df['expiry'].dt.date.astype(str)
    return df


def _log_path(trade_id: int, expiry_date: str, logs_dir: str) -> str:
    return os.path.join(logs_dir, f'trade_{trade_id:04d}_{expiry_date}.csv')


def _load_log(trade_id: int, expiry_date: str, logs_dir: str) -> pd.DataFrame:
    return pd.read_csv(_log_path(trade_id, expiry_date, logs_dir),
                       parse_dates=['time_stamp'])


# ---------------------------------------------------------------------------
# IV computation and caching
# ---------------------------------------------------------------------------

def _iv_cache_path(trade_id: int, expiry_date: str, instrument: str) -> str:
    return os.path.join(IV_CACHE_DIR,
                        f'trade_{trade_id:04d}_{expiry_date}_{instrument}.parquet')


def _compute_iv_for_trade(log: pd.DataFrame, expiry_date: str) -> pd.DataFrame:
    rows = []
    for _, bar in log.iterrows():
        ts   = bar['time_stamp']
        spot = float(bar['spot'])
        dte  = get_dte_days(ts, expiry_date)

        def _iv(ltp_col, strike_col, opt_type):
            ltp    = bar.get(ltp_col, 0.0)
            strike = bar.get(strike_col, float('nan'))
            if pd.isna(ltp) or float(ltp) <= 0.0 or pd.isna(strike):
                return None
            return compute_iv(float(ltp), spot, float(strike), dte, opt_type)

        rows.append({
            'time_stamp': ts,
            'pe_sell_iv': _iv('pe_sell_ltp', 'pe_sell_strike', 'pe'),
            'pe_buy_iv':  _iv('pe_buy_ltp',  'pe_buy_strike',  'pe'),
            'ce_sell_iv': _iv('ce_sell_ltp', 'ce_sell_strike', 'ce'),
            'ce_buy_iv':  _iv('ce_buy_ltp',  'ce_buy_strike',  'ce'),
        })
    return pd.DataFrame(rows)


def _load_or_compute_iv(trade_id: int, expiry_date: str, instrument: str,
                         log: pd.DataFrame) -> pd.DataFrame:
    path = _iv_cache_path(trade_id, expiry_date, instrument)
    if os.path.exists(path):
        cached = pd.read_parquet(path)
        if len(cached) == len(log) and set(IV_COLS).issubset(set(cached.columns)):
            return cached
    iv_df = _compute_iv_for_trade(log, expiry_date)
    iv_df.to_parquet(path, index=False)
    return iv_df


# ---------------------------------------------------------------------------
# Attribution
# ---------------------------------------------------------------------------

def _attribute_trade(log: pd.DataFrame, iv_df: pd.DataFrame,
                     expiry_date: str) -> dict:
    """
    Bar-to-bar Taylor decomposition for one Artemis trade.

    actual_mtm = Σ Δ(pe_pl) + Δ(ce_pl) across consecutive bar pairs.
    This equals pe_pl[-1] - pe_pl[0] + ce_pl[-1] - ce_pl[0].
    The first-bar within-bar P&L (pe_pl[0] + ce_pl[0]) is not attributed
    and appears in residual.
    """
    totals = dict(delta=0.0, gamma=0.0, theta=0.0, vega=0.0,
                  actual=0.0, iv_fails=0)

    n = len(log)
    for i in range(n - 1):
        bar_t  = log.iloc[i]
        bar_t1 = log.iloc[i + 1]
        iv_t   = iv_df.iloc[i]
        iv_t1  = iv_df.iloc[i + 1]

        ts_t   = bar_t['time_stamp']
        ts_t1  = bar_t1['time_stamp']
        spot_t = float(bar_t['spot'])
        d_spot = float(bar_t1['spot']) - spot_t
        d_t    = (ts_t1 - ts_t).total_seconds() / 86400.0

        # Actual base-lot MtM for this bar interval
        pe_pl_t  = float(bar_t['pe_pl']  or 0.0)
        pe_pl_t1 = float(bar_t1['pe_pl'] or 0.0)
        ce_pl_t  = float(bar_t['ce_pl']  or 0.0)
        ce_pl_t1 = float(bar_t1['ce_pl'] or 0.0)
        totals['actual'] += (pe_pl_t1 - pe_pl_t) + (ce_pl_t1 - ce_pl_t)

        for iv_col, ltp_col, strike_col, opt_type, direction, status_col in LEGS:
            # Skip if this side is closed — LTPs are frozen, no attributable P&L
            if str(bar_t.get(status_col, '')) == 'closed':
                continue

            ltp_t  = bar_t.get(ltp_col, 0.0)
            ltp_t1 = bar_t1.get(ltp_col, 0.0)
            if pd.isna(ltp_t) or float(ltp_t) <= 0.0:
                continue
            if pd.isna(ltp_t1) or float(ltp_t1) <= 0.0:
                continue

            iv_t_val  = iv_t.get(iv_col)
            iv_t1_val = iv_t1.get(iv_col)

            strike = bar_t.get(strike_col, float('nan'))
            if pd.isna(strike):
                continue

            dte_t = get_dte_days(ts_t, expiry_date)

            # pd.isna catches both None and float('nan') — the IV DataFrame
            # stores failed computations as NaN, not None
            if pd.isna(iv_t_val) or pd.isna(iv_t1_val):
                totals['iv_fails'] += 1
                continue

            greeks = compute_greeks(float(iv_t_val), spot_t, float(strike), dte_t, opt_type)
            if greeks is None:
                totals['iv_fails'] += 1
                continue

            # Guard against NaN Greeks from mibian edge cases
            if any(v is None or (isinstance(v, float) and pd.isna(v))
                   for v in greeks.values()):
                totals['iv_fails'] += 1
                continue

            d_iv = iv_t1_val - iv_t_val
            totals['delta'] += direction * greeks['delta'] * d_spot
            totals['gamma'] += direction * 0.5 * greeks['gamma'] * d_spot ** 2
            totals['theta'] += direction * greeks['theta'] * d_t
            totals['vega']  += direction * greeks['vega']  * d_iv

    actual  = totals['actual']
    delta_c = totals['delta']
    gamma_c = totals['gamma']
    theta_c = totals['theta']
    vega_c  = totals['vega']
    resid   = actual - (delta_c + gamma_c + theta_c + vega_c)

    return {
        'delta_contrib': delta_c,
        'gamma_contrib': gamma_c,
        'theta_contrib': theta_c,
        'vega_contrib':  vega_c,
        'actual_mtm':    actual,
        'residual':      resid,
        'iv_fail_bars':  totals['iv_fails'],
        'n_bars':        n - 1,
    }


# ---------------------------------------------------------------------------
# Per-instrument runner
# ---------------------------------------------------------------------------

def _run_instrument(instrument: str, summary_path: str,
                    logs_dir: str) -> list[dict]:
    summary = _load_summary(summary_path)
    n = len(summary)
    print(f"\n{instrument.upper()} — {n} traded weeks")

    records = []
    for _, row in summary.iterrows():
        trade_id    = int(row['trade_id'])
        expiry_date = str(row['expiry_date'])

        print(f"  {trade_id:3d}/{n}  {row['entry_date']}  "
              f"entry={row['entry_spot']:.0f}", end='', flush=True)

        log   = _load_log(trade_id, expiry_date, logs_dir)
        iv_df = _load_or_compute_iv(trade_id, expiry_date, instrument, log)
        attr  = _attribute_trade(log, iv_df, expiry_date)

        base_pl  = float(row['pe_pl_points']) + float(row['ce_pl_points'])
        add_pl   = 0.5 * (float(row.get('pe_add_pl_points') or 0.0) +
                          float(row.get('ce_add_pl_points') or 0.0))
        total_pl = float(row['total_pl_points'])

        pct_unexp = (abs(attr['residual']) / abs(attr['actual_mtm']) * 100
                     if attr['actual_mtm'] != 0 else float('nan'))

        records.append({
            'instrument':      instrument,
            'trade_id':        trade_id,
            'entry_date':      row['entry_date'],
            'entry_spot':      row['entry_spot'],
            'entry_vix':       row['entry_vix'],
            'pe_exit_reason':  row['pe_exit_reason'],
            'ce_exit_reason':  row['ce_exit_reason'],
            'base_pl_points':  round(base_pl,               2),
            'add_pl_points':   round(add_pl,                2),
            'total_pl_points': round(total_pl,              2),
            'actual_mtm':      round(attr['actual_mtm'],    2),
            'delta_contrib':   round(attr['delta_contrib'],  2),
            'gamma_contrib':   round(attr['gamma_contrib'],  2),
            'theta_contrib':   round(attr['theta_contrib'],  2),
            'vega_contrib':    round(attr['vega_contrib'],   2),
            'residual':        round(attr['residual'],        2),
            'pct_unexplained': round(pct_unexp,              1),
            'iv_fail_bars':    attr['iv_fail_bars'],
            'n_bars':          attr['n_bars'],
        })

        print(f"  base={base_pl:+.1f}  "
              f"θ={attr['theta_contrib']:+.1f}  v={attr['vega_contrib']:+.1f}  "
              f"Δ={attr['delta_contrib']:+.1f}  Γ={attr['gamma_contrib']:+.1f}  "
              f"resid={attr['residual']:+.1f}  ({pct_unexp:.0f}% unexp)")

    return records


# ---------------------------------------------------------------------------
# Summary printing
# ---------------------------------------------------------------------------

def _print_summary(df: pd.DataFrame) -> None:
    print()
    print('=' * 64)
    print('ARTEMIS P&L ATTRIBUTION — BRANCH 1 (base lots, all trades)')
    print('=' * 64)

    total_mtm = df['actual_mtm'].sum()
    for col in ['delta_contrib', 'gamma_contrib', 'theta_contrib',
                'vega_contrib', 'residual']:
        total = df[col].sum()
        mean  = df[col].mean()
        pct   = total / total_mtm * 100 if total_mtm != 0 else float('nan')
        print(f'  {col:>18s}:  sum={total:+8.1f}  mean={mean:+6.2f}  {pct:+5.1f}% of MtM')

    print()
    print(f'  actual_mtm (base, sum Δpl):   {total_mtm:+.1f} pts')
    print(f'  add lot P&L (summary):        {df["add_pl_points"].sum():+.1f} pts')
    print(f'  base_pl_points (summary):     {df["base_pl_points"].sum():+.1f} pts')
    print(f'  total_pl_points (summary):    {df["total_pl_points"].sum():+.1f} pts')
    print(f'  IV fail bars: {df["iv_fail_bars"].sum()} / {df["n_bars"].sum()} '
          f'({df["iv_fail_bars"].sum() / df["n_bars"].sum() * 100:.1f}%)')
    print(f'  Mean %unexplained: {df["pct_unexplained"].mean():.1f}%')

    print()
    print('  Winning vs losing trades (by total_pl_points):')
    wins  = df[df['total_pl_points'] > 0]
    loses = df[df['total_pl_points'] <= 0]
    for label, grp in [('wins  ', wins), ('losses', loses)]:
        if len(grp) == 0:
            continue
        print(f'    {label}  n={len(grp):3d}  '
              f'θ={grp["theta_contrib"].mean():+.2f}  '
              f'v={grp["vega_contrib"].mean():+.2f}  '
              f'Δ={grp["delta_contrib"].mean():+.2f}  '
              f'Γ={grp["gamma_contrib"].mean():+.2f}  '
              f'resid={grp["residual"].mean():+.2f}')

    print()
    print('  By instrument:')
    for instr, grp in df.groupby('instrument'):
        lot_size = LOT_SIZE[instr]
        total_rs = grp['total_pl_points'].sum() * lot_size
        print(f'    {instr:8s}  n={len(grp):3d}  '
              f'θ={grp["theta_contrib"].mean():+.2f}  '
              f'v={grp["vega_contrib"].mean():+.2f}  '
              f'Δ={grp["delta_contrib"].mean():+.2f}  '
              f'Γ={grp["gamma_contrib"].mean():+.2f}  '
              f'total_pl=+{grp["total_pl_points"].sum():.0f}pts/₹{total_rs:,.0f}')

    print()
    print('  Period split (Nifty only):')
    nifty = df[df['instrument'] == 'nifty'].copy()
    nifty['year'] = pd.to_datetime(nifty['entry_date']).dt.year
    for label, grp in [('2020-2022', nifty[nifty['year'] <= 2022]),
                        ('2023+    ', nifty[nifty['year'] >= 2023])]:
        if len(grp) == 0:
            continue
        print(f'    {label}  n={len(grp):3d}  '
              f'θ={grp["theta_contrib"].mean():+.2f}  '
              f'v={grp["vega_contrib"].mean():+.2f}  '
              f'Δ={grp["delta_contrib"].mean():+.2f}  '
              f'resid={grp["residual"].mean():+.2f}')


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run():
    print('Artemis P&L Attribution — Branch 1')
    print(f'Output: {OUTPUT_PATH}')

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    os.makedirs(IV_CACHE_DIR, exist_ok=True)

    records = []
    records += _run_instrument('nifty',  NIFTY_SUMMARY,  NIFTY_LOGS_DIR)
    records += _run_instrument('sensex', SENSEX_SUMMARY, SENSEX_LOGS_DIR)

    df_out = pd.DataFrame(records)
    df_out.to_csv(OUTPUT_PATH, index=False)
    print(f'\nSaved {len(df_out)} rows → {OUTPUT_PATH}')
    _print_summary(df_out)


if __name__ == '__main__':
    run()
