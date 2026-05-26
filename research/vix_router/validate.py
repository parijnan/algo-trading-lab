"""
validate.py — Phase 0 and Phase 1 validation for the VIX-direction router.

Phase 0: Derive modal holds for Athena and Artemis (pre/post Sep-2025).
         Establish base rates P(VIX falls over h) for each horizon.
         Emits outputs/horizons.json.

Phase 1: Build VRP signal. Run the full §7 battery (Spearman ρ, decile monotonicity,
         directional hit-rate, per-year stability, regime conditioning) on the full
         daily VIX series 2019→2026. Emits outputs/signal_validation_h{N}.csv.

Run from the repo root or research/vix_router/:
    python research/vix_router/validate.py
"""

import os
import sys
import json
import numpy as np
import pandas as pd
from scipy.stats import spearmanr

_THIS_DIR  = os.path.dirname(os.path.abspath(__file__))
_REPO_ROOT = os.path.dirname(os.path.dirname(_THIS_DIR))
_OUT_DIR   = os.path.join(_THIS_DIR, 'outputs')

sys.path.insert(0, _REPO_ROOT)
sys.path.insert(0, os.path.join(_REPO_ROOT, 'research', 'range_detection'))

from research.vix_router.data_layer import load_combined_daily  # noqa: E402
from research.vix_router.signals import vrp, bb_pct, zscore, bb_zone  # noqa: E402

os.makedirs(_OUT_DIR, exist_ok=True)

SEP_2025 = pd.Timestamp('2025-09-01')

ATHENA_SUMMARY  = os.path.join(_REPO_ROOT, 'athena_backtest',  'data', 'trade_summary.csv')
ARTEMIS_SUMMARY = os.path.join(_REPO_ROOT, 'artemis_backtest', 'data', 'trade_summary_sensex.csv')


# ──────────────────────────────────────────────────────────────────────────────
# Forward VIX target (validation-only — never feeds back into build_forecast)
# ──────────────────────────────────────────────────────────────────────────────

def forward_vix_change(vix_daily: pd.DataFrame, horizon_days: int) -> pd.Series:
    """
    fwd(t) = VIX_close(t + h) − VIX_close(t), date-indexed.
    Uses future bars — strictly for validation; never pass into build_forecast.
    horizon_days: number of TRADING days (rows in vix_daily, not calendar days).
    """
    c = vix_daily['close']
    return (c.shift(-horizon_days) - c).rename(f'fwd_vix_chg_h{horizon_days}')


# ──────────────────────────────────────────────────────────────────────────────
# Phase 0 — Horizons & base rates
# ──────────────────────────────────────────────────────────────────────────────

def _trading_sessions_between(entry: pd.Timestamp, exit_: pd.Timestamp,
                               trading_dates: pd.DatetimeIndex) -> int:
    """Count trading days in [entry.date, exit_.date] exclusive of entry."""
    e = entry.normalize()
    x = exit_.normalize()
    return int(((trading_dates > e) & (trading_dates <= x)).sum())


def phase0_horizons(vix_daily: pd.DataFrame) -> dict:
    """
    Load trade summaries, compute modal/median/mean hold in trading sessions
    for Athena and Artemis (pre/post Sep-2025).
    Compute base rate P(VIX falls) for each relevant horizon.
    Returns a dict that is also saved to outputs/horizons.json.
    """
    trading_dates = vix_daily.index.normalize()

    # ── Athena ────────────────────────────────────────────────────────────────
    athena = pd.read_csv(ATHENA_SUMMARY)
    athena['entry_time'] = pd.to_datetime(athena['entry_time'])
    athena['exit_time']  = pd.to_datetime(athena['exit_time'])

    def _athena_sessions(row):
        return _trading_sessions_between(row['entry_time'], row['exit_time'], trading_dates)

    athena['sessions'] = athena.apply(_athena_sessions, axis=1)

    athena_pre  = athena[athena['entry_time'] <  SEP_2025]
    athena_post = athena[athena['entry_time'] >= SEP_2025]

    def _modal(s):
        return int(s.mode().iloc[0]) if len(s) else None

    athena_stats = {
        'pre_sep': {
            'n':      len(athena_pre),
            'modal':  _modal(athena_pre['sessions']),
            'median': float(athena_pre['sessions'].median()) if len(athena_pre) else None,
            'mean':   round(float(athena_pre['sessions'].mean()), 2) if len(athena_pre) else None,
        },
        'post_sep': {
            'n':      len(athena_post),
            'modal':  _modal(athena_post['sessions']),
            'median': float(athena_post['sessions'].median()) if len(athena_post) else None,
            'mean':   round(float(athena_post['sessions'].mean()), 2) if len(athena_post) else None,
        },
    }

    # ── Artemis ───────────────────────────────────────────────────────────────
    artemis = pd.read_csv(ARTEMIS_SUMMARY)
    artemis = artemis[artemis['week_outcome'] == 'traded'].copy()
    artemis['entry_time'] = pd.to_datetime(artemis['entry_time'])
    artemis['expiry']     = pd.to_datetime(artemis['expiry'])

    def _artemis_sessions(row):
        return _trading_sessions_between(row['entry_time'], row['expiry'], trading_dates)

    artemis['sessions'] = artemis.apply(_artemis_sessions, axis=1)
    # Artemis is all post-Sep (Sensex era starts Sep 2025)
    artemis_stats = {
        'post_sep': {
            'n':      len(artemis),
            'modal':  _modal(artemis['sessions']),
            'median': float(artemis['sessions'].median()),
            'mean':   round(float(artemis['sessions'].mean()), 2),
        }
    }

    # ── Chosen horizons ───────────────────────────────────────────────────────
    h_athena  = athena_stats['pre_sep']['modal']   # primary (most data)
    h_artemis = artemis_stats['post_sep']['modal']

    # ── Base rates ────────────────────────────────────────────────────────────
    base_rates = {}
    for h in sorted(set([h_athena, h_artemis, h_athena + 1])):
        fwd = forward_vix_change(vix_daily, h).dropna()
        n_total = len(fwd)
        n_fall  = int((fwd < 0).sum())
        n_rise  = int((fwd > 0).sum())
        n_flat  = int((fwd == 0).sum())
        base_rates[f'h{h}'] = {
            'horizon_trading_sessions': h,
            'n': n_total,
            'p_fall':    round(n_fall  / n_total, 4),
            'p_rise':    round(n_rise  / n_total, 4),
            'p_flat':    round(n_flat  / n_total, 4),
            'mean_fwd_chg': round(float(fwd.mean()), 4),
        }

    result = {
        'athena':     athena_stats,
        'artemis':    artemis_stats,
        'h_athena':   h_athena,
        'h_artemis':  h_artemis,
        'base_rates': base_rates,
    }

    out_path = os.path.join(_OUT_DIR, 'horizons.json')
    with open(out_path, 'w') as f:
        json.dump(result, f, indent=2)
    print(f'[Phase 0] horizons.json written to {out_path}')
    return result


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1 — VRP validation battery
# ──────────────────────────────────────────────────────────────────────────────

def _spearman_peryear(signal: pd.Series, target: pd.Series) -> pd.DataFrame:
    """Compute Spearman ρ per calendar year. Both series must be aligned."""
    rows = []
    for yr, grp in signal.groupby(signal.index.year):
        t_grp = target.reindex(grp.index).dropna()
        s_grp = grp.reindex(t_grp.index).dropna()
        if len(s_grp) < 20:
            continue
        rho, pval = spearmanr(s_grp, t_grp)
        rows.append({'year': yr, 'n': len(s_grp), 'rho': round(rho, 4), 'pval': round(pval, 4)})
    return pd.DataFrame(rows)


def _decile_table(signal: pd.Series, target: pd.Series) -> pd.DataFrame:
    """Decile monotonicity table: signal → mean/median fwd VIX change and hit-rate per decile."""
    aligned = pd.concat([signal.rename('sig'), target.rename('tgt')], axis=1).dropna()
    aligned['decile'] = pd.qcut(aligned['sig'], 10, labels=False, duplicates='drop')
    rows = []
    for dec, grp in aligned.groupby('decile'):
        rows.append({
            'decile'     : int(dec) + 1,
            'n'          : len(grp),
            'sig_median' : round(grp['sig'].median(), 4),
            'fwd_mean'   : round(grp['tgt'].mean(), 4),
            'fwd_median' : round(grp['tgt'].median(), 4),
            'hit_rate'   : round((grp['tgt'] < 0).mean(), 4),  # P(VIX falls | decile)
        })
    return pd.DataFrame(rows)


def _regime_stats(signal: pd.Series, target: pd.Series, vix_close: pd.Series) -> dict:
    """Repeat correlation within VIX-level regime bands (the live decision bands)."""
    aligned = pd.concat([signal.rename('sig'), target.rename('tgt'),
                         vix_close.rename('vix')], axis=1).dropna()
    out = {}
    for label, mask in [
        ('vix_lt16',    aligned['vix'] < 16),
        ('vix_16_25',   (aligned['vix'] >= 16) & (aligned['vix'] <= 25)),
        ('vix_gt25',    aligned['vix'] > 25),
    ]:
        sub = aligned[mask]
        if len(sub) < 20:
            out[label] = {'n': len(sub), 'rho': None, 'pval': None}
            continue
        rho, pval = spearmanr(sub['sig'], sub['tgt'])
        out[label] = {'n': len(sub), 'rho': round(rho, 4), 'pval': round(pval, 4)}
    return out


def phase1_vrp(vix_daily: pd.DataFrame, nifty_daily: pd.DataFrame,
               horizons: dict) -> None:
    """
    Validate VRP signal (n=10 and n=20) on full daily VIX history.
    Runs the full §7 battery per horizon. Emits signal_validation_h{N}.csv files.
    """
    from research.vix_router.signals import vrp as compute_vrp

    h_list = [horizons['h_athena'], horizons['h_artemis']]
    h_list = sorted(set(h_list))  # deduplicate if same

    for window in [10, 20]:
        vrp_s  = compute_vrp(vix_daily['close'], nifty_daily['close'], window=window)
        # No-lookahead: shift by 1 (prev day's value is known at 10:30)
        vrp_prev = vrp_s.shift(1)

        for h in h_list:
            fwd = forward_vix_change(vix_daily, h)
            aligned = pd.concat([vrp_prev.rename('vrp'),
                                  fwd.rename('fwd')], axis=1).dropna()

            # Full-sample Spearman
            rho_full, pval_full = spearmanr(aligned['vrp'], aligned['fwd'])
            n_total = len(aligned)
            hit_rate = (aligned['fwd'] < 0).mean()

            # Base rate from horizons.json (if available for this h)
            br_key = f'h{h}'
            base_rate = horizons['base_rates'].get(br_key, {}).get('p_fall', None)

            print(f'\n=== VRP (window={window}) | horizon={h} sessions ===')
            print(f'  n={n_total}, Spearman ρ={rho_full:.4f} (p={pval_full:.4f})')
            print(f'  unconditional hit-rate (VIX falls): {hit_rate:.4f}'
                  f'  (base rate = {base_rate})')

            # Per-year stability
            per_year = _spearman_peryear(aligned['vrp'], aligned['fwd'])
            print('\n  Per-year ρ:')
            print(per_year.to_string(index=False))
            stable = (per_year['rho'].dropna() > 0).all() or (per_year['rho'].dropna() < 0).all()
            print(f'  Sign-stable across years: {stable}')

            # Decile table
            dec_table = _decile_table(aligned['vrp'], aligned['fwd'])
            print('\n  Decile table (1=lowest VRP, 10=highest VRP):')
            print(dec_table.to_string(index=False))

            # Regime conditioning
            regime = _regime_stats(aligned['vrp'], aligned['fwd'],
                                   vix_daily['close'].reindex(aligned.index))
            print('\n  Regime conditioning:')
            for band, stats in regime.items():
                print(f'    {band}: n={stats["n"]}, ρ={stats["rho"]}, p={stats["pval"]}')

            # ── Emit CSV ──────────────────────────────────────────────────────
            summary_row = pd.DataFrame([{
                'signal'     : f'vrp_{window}',
                'horizon_h'  : h,
                'n'          : n_total,
                'rho_full'   : round(rho_full, 4),
                'pval_full'  : round(pval_full, 4),
                'hit_rate'   : round(hit_rate, 4),
                'base_rate'  : base_rate,
                'sign_stable': stable,
                'rho_by_year': per_year.set_index('year')['rho'].to_dict(),
            }])

            # Save decile table
            dec_out = os.path.join(_OUT_DIR, f'decile_vrp{window}_h{h}.csv')
            dec_table.to_csv(dec_out, index=False)

            # Save validation summary
            val_out = os.path.join(_OUT_DIR, f'signal_validation_h{h}.csv')
            # Append or create
            if os.path.exists(val_out):
                existing = pd.read_csv(val_out)
                # Remove any existing row with same signal
                existing = existing[existing['signal'] != f'vrp_{window}']
                combined = pd.concat([existing, summary_row], ignore_index=True)
            else:
                combined = summary_row
            combined.to_csv(val_out, index=False)
            print(f'  → saved {val_out}')


# ──────────────────────────────────────────────────────────────────────────────
# Phase 1b — BB %B and z-score (same battery, separate report)
# ──────────────────────────────────────────────────────────────────────────────

def phase1_bb_zscore(vix_daily: pd.DataFrame, nifty_daily: pd.DataFrame,
                     horizons: dict) -> None:
    """
    Validate BB %B and z-score signals. Same §7 battery as VRP.
    Appends rows to the same signal_validation_h{N}.csv files.
    """
    from research.vix_router.signals import bb_pct as compute_bb, zscore as compute_z

    h_list = sorted(set([horizons['h_athena'], horizons['h_artemis']]))
    vix_close = vix_daily['close']

    bb_s = compute_bb(vix_close, window=20)
    bb_prev = bb_s.shift(1)

    z20  = compute_z(vix_close, window=20).shift(1)
    z50  = compute_z(vix_close, window=50).shift(1)

    for sig_name, sig_s in [('bb_pct', bb_prev), ('zscore_20', z20), ('zscore_50', z50)]:
        for h in h_list:
            fwd = forward_vix_change(vix_daily, h)
            aligned = pd.concat([sig_s.rename('sig'),
                                  fwd.rename('fwd')], axis=1).dropna()

            rho_full, pval_full = spearmanr(aligned['sig'], aligned['fwd'])
            n_total = len(aligned)
            hit_rate = (aligned['fwd'] < 0).mean()
            base_rate = horizons['base_rates'].get(f'h{h}', {}).get('p_fall', None)

            print(f'\n=== {sig_name} | horizon={h} sessions ===')
            print(f'  n={n_total}, Spearman ρ={rho_full:.4f} (p={pval_full:.4f})')
            print(f'  hit-rate={hit_rate:.4f}  (base rate={base_rate})')

            per_year = _spearman_peryear(aligned['sig'], aligned['fwd'])
            print('\n  Per-year ρ:')
            print(per_year.to_string(index=False))
            stable = (per_year['rho'].dropna() > 0).all() or (per_year['rho'].dropna() < 0).all()

            dec_table = _decile_table(aligned['sig'], aligned['fwd'])
            print('\n  Decile table:')
            print(dec_table.to_string(index=False))

            regime = _regime_stats(aligned['sig'], aligned['fwd'],
                                   vix_close.reindex(aligned.index))
            print('\n  Regime conditioning:')
            for band, stats in regime.items():
                print(f'    {band}: n={stats["n"]}, ρ={stats["rho"]}, p={stats["pval"]}')

            # Append to validation CSV
            dec_out = os.path.join(_OUT_DIR, f'decile_{sig_name}_h{h}.csv')
            dec_table.to_csv(dec_out, index=False)

            summary_row = pd.DataFrame([{
                'signal'     : sig_name,
                'horizon_h'  : h,
                'n'          : n_total,
                'rho_full'   : round(rho_full, 4),
                'pval_full'  : round(pval_full, 4),
                'hit_rate'   : round(hit_rate, 4),
                'base_rate'  : base_rate,
                'sign_stable': stable,
                'rho_by_year': per_year.set_index('year')['rho'].to_dict(),
            }])

            val_out = os.path.join(_OUT_DIR, f'signal_validation_h{h}.csv')
            if os.path.exists(val_out):
                existing = pd.read_csv(val_out)
                existing = existing[existing['signal'] != sig_name]
                combined = pd.concat([existing, summary_row], ignore_index=True)
            else:
                combined = summary_row
            combined.to_csv(val_out, index=False)
            print(f'  → saved {val_out}')


# ──────────────────────────────────────────────────────────────────────────────
# Main
# ──────────────────────────────────────────────────────────────────────────────

def main():
    print('Loading daily VIX and Nifty from 1-min data (2019+) ...')
    vix_daily, nifty_daily = load_combined_daily()
    print(f'  VIX daily: {len(vix_daily)} rows  '
          f'({vix_daily.index.min().date()} → {vix_daily.index.max().date()})')
    print(f'  Nifty daily: {len(nifty_daily)} rows')

    print('\n── Phase 0: Horizons & base rates ─────────────────────────────────────')
    horizons = phase0_horizons(vix_daily)
    print(json.dumps(horizons, indent=2))

    print('\n── Phase 1a: VRP signal validation ─────────────────────────────────────')
    phase1_vrp(vix_daily, nifty_daily, horizons)

    print('\n── Phase 1b: BB %B and z-score validation ───────────────────────────────')
    phase1_bb_zscore(vix_daily, nifty_daily, horizons)

    print('\n\nDone. Check outputs/ for horizons.json, signal_validation_h*.csv, decile_*.csv')


if __name__ == '__main__':
    main()
