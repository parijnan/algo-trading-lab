"""
Prometheus - Phase 4 entry point: filter Phase 3's mult-2.0 raw trade list
through the 1h/15m ST alignment gate (plan §17 preview), apply the same
bespoke exits (SL 2.2%/T1 2.0%/T2 5.0%) to survivors, and compare against
the unfiltered baseline using the per-lot-exit-event Calmar methodology
(prometheus_backtest/README.md's Phase 3 section, reproduced exactly so the
two are comparable).

Usage: python prometheus_backtest/phase4/run_p4.py
"""

import os
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import configs_p4 as configs  # noqa: E402
from filter_1h_p4 import build_1h_st_series, check_alignment  # noqa: E402

sys.path.insert(0, configs.PHASE3_DIR)
from exit_calib_p3 import _load_multiplier_data  # noqa: E402
from bespoke_2lot_p3 import _simulate_trade_detailed  # noqa: E402

sys.path.insert(0, configs.PROMETHEUS_DIR)
from data_loader import load_futures_1min  # noqa: E402


def _per_lot_event_stats(bespoke_df: pd.DataFrame) -> dict:
    """Same per-lot-exit-event Calmar/drawdown methodology used to refresh
    Phase 3's two-candidate table (2026-09-04) -- each lot's own exit is
    its own chronological cash-flow event, not bundled with its sibling
    lot's P&L at the trade's completion. Verified that day against the
    original published artifact by exact drawdown match."""
    n = len(bespoke_df)
    if n == 0:
        return {'n_trades': 0, 'win_pct': float('nan'), 'total_pnl_rs': 0.0,
                'max_drawdown_rs': 0.0, 'calmar': float('nan')}

    events = pd.concat([
        bespoke_df[['lot1_exit_ts', 'lot1_pnl_rs']].rename(
            columns={'lot1_exit_ts': 'ts', 'lot1_pnl_rs': 'pnl_rs'}),
        bespoke_df[['lot2_exit_ts', 'lot2_pnl_rs']].rename(
            columns={'lot2_exit_ts': 'ts', 'lot2_pnl_rs': 'pnl_rs'}),
    ], ignore_index=True).sort_values('ts').reset_index(drop=True)

    cumpl = events['pnl_rs'].cumsum()
    max_dd = (cumpl - cumpl.cummax()).min()
    total_pl = events['pnl_rs'].sum()
    calmar = total_pl / abs(max_dd) if max_dd else float('nan')

    trade_pl = bespoke_df['total_pnl_rs'].astype(float)
    wins = int((trade_pl > 0).sum())

    return {
        'n_trades': n, 'win_pct': round(wins / n * 100, 2),
        'total_pnl_rs': round(total_pl, 0), 'max_drawdown_rs': round(max_dd, 0),
        'calmar': round(calmar, 2) if pd.notna(calmar) else float('nan'),
    }


def _simulate_bespoke(trades: pd.DataFrame, paths: dict) -> pd.DataFrame:
    rows = []
    for _, t in trades.iterrows():
        tid = int(t['trade_id'])
        if tid not in paths:
            continue
        rows.append(_simulate_trade_detailed(t, paths[tid], configs.SL_PCT,
                                              configs.TARGET1_PCT, configs.TARGET2_FLAT_PCT))
    return pd.DataFrame(rows)


def run_1h_filter_variant(df_1m: pd.DataFrame, raw_trades: pd.DataFrame, paths: dict,
                          st1h_period: int, st1h_multiplier: float) -> dict:
    st1h = build_1h_st_series(df_1m, st1h_period, st1h_multiplier)
    checked = check_alignment(st1h, raw_trades)

    kept = checked[checked['aligned_1h']].reset_index(drop=True)
    blocked = checked[~checked['aligned_1h']].reset_index(drop=True)

    kept_bespoke = _simulate_bespoke(kept, paths)
    blocked_bespoke = _simulate_bespoke(blocked, paths)   # diagnostic only -- never "live" P&L

    kept_stats = _per_lot_event_stats(kept_bespoke)
    blocked_stats = _per_lot_event_stats(blocked_bespoke)
    # How often the 1h ST itself changes direction over the backtest window --
    # the degenerate-overfitting tripwire advisor() flagged: as period/mult
    # shrink toward ST_15-like responsiveness, the "regime filter" stops being
    # a regime filter and becomes a near-duplicate of the entry signal itself.
    n_1h_flips = int(st1h['trend_flip'].sum())

    return {
        'st1h_period': st1h_period, 'st1h_multiplier': st1h_multiplier,
        'kept': kept_stats, 'blocked': blocked_stats, 'n_1h_flips': n_1h_flips,
        'kept_bespoke_df': kept_bespoke, 'blocked_bespoke_df': blocked_bespoke,
        'checked_df': checked,
    }


def main():
    print(f'=== Prometheus Phase 4 — 1h/15m ST alignment filter ===')
    print(f'ST_15 fixed at period={configs.ST_15_PERIOD} multiplier={configs.ST_15_MULTIPLIER} '
          f'(Phase 3 decided candidate)')
    print(f'Exits fixed at SL={configs.SL_PCT}% T1={configs.TARGET1_PCT}% T2={configs.TARGET2_FLAT_PCT}%\n')

    df_1m = load_futures_1min(configs.SYMBOL)
    raw_trades, paths = _load_multiplier_data(configs.ST_15_MULTIPLIER)
    print(f'Loaded {len(df_1m):,} 1-min bars, {len(raw_trades)} raw ST_15 mult-{configs.ST_15_MULTIPLIER} trades '
          f'({len(paths)} with per-trade path logs)\n')

    # Unfiltered baseline -- read straight from Phase 3's already-published
    # bespoke output, not re-derived, so it's guaranteed identical to what's
    # in the README.
    baseline_path = os.path.join(configs.PHASE3_SWEEP_DIR, f'mult_{configs.ST_15_MULTIPLIER:.1f}',
                                 'bespoke_trade_summary.csv')
    baseline_df = pd.read_csv(baseline_path, parse_dates=['lot1_exit_ts', 'lot2_exit_ts'])
    baseline_stats = _per_lot_event_stats(baseline_df)
    print(f"Baseline (unfiltered, from {baseline_path}):")
    print(f"  {baseline_stats['n_trades']} trades, win%={baseline_stats['win_pct']}, "
          f"total P&L=Rs.{baseline_stats['total_pnl_rs']:,.0f}, "
          f"max DD=Rs.{baseline_stats['max_drawdown_rs']:,.0f}, Calmar={baseline_stats['calmar']}\n")

    os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
    results = []
    for period in configs.ST_1H_PERIOD_GRID:
        for mult in configs.ST_1H_MULTIPLIER_GRID:
            print(f'--- ST_1H period={period} multiplier={mult} ---')
            r = run_1h_filter_variant(df_1m, raw_trades, paths, period, mult)
            k, b = r['kept'], r['blocked']
            print(f"  KEPT:    {k['n_trades']} trades, win%={k['win_pct']}, "
                  f"total P&L=Rs.{k['total_pnl_rs']:,.0f}, max DD=Rs.{k['max_drawdown_rs']:,.0f}, "
                  f"Calmar={k['calmar']}")
            print(f"  BLOCKED: {b['n_trades']} trades, win%={b['win_pct']}, "
                  f"total P&L=Rs.{b['total_pnl_rs']:,.0f}, max DD=Rs.{b['max_drawdown_rs']:,.0f}, "
                  f"Calmar={b['calmar']} (diagnostic -- kept Calmar < blocked Calmar means the "
                  f"filter is anti-selecting)")
            print(f"  1h ST flips over backtest window: {r['n_1h_flips']} "
                  f"(near ST_15's own flip count would mean this is a near-duplicate signal, not a regime filter)")
            delta_trades = k['n_trades'] - baseline_stats['n_trades']
            delta_pnl = k['total_pnl_rs'] - baseline_stats['total_pnl_rs']
            delta_calmar = (k['calmar'] - baseline_stats['calmar']) if pd.notna(k['calmar']) else float('nan')
            print(f"  DELTA vs baseline: trades {delta_trades:+d}, P&L Rs.{delta_pnl:+,.0f}, "
                  f"Calmar {delta_calmar:+.2f}\n")

            run_dir = os.path.join(configs.DATA_SWEEP_DIR, f'1h_{period}_{mult:.1f}')
            os.makedirs(run_dir, exist_ok=True)
            r['kept_bespoke_df'].to_csv(os.path.join(run_dir, 'kept_trade_summary.csv'), index=False)
            r['blocked_bespoke_df'].to_csv(os.path.join(run_dir, 'blocked_trade_summary.csv'), index=False)
            r['checked_df'].to_csv(os.path.join(run_dir, 'alignment_detail.csv'), index=False)

            results.append({
                'st1h_period': period, 'st1h_multiplier': mult,
                'kept_trades': k['n_trades'], 'kept_win_pct': k['win_pct'],
                'kept_total_pnl_rs': k['total_pnl_rs'], 'kept_max_dd_rs': k['max_drawdown_rs'],
                'kept_calmar': k['calmar'],
                'blocked_trades': b['n_trades'], 'blocked_total_pnl_rs': b['total_pnl_rs'],
                'blocked_max_dd_rs': b['max_drawdown_rs'], 'blocked_calmar': b['calmar'],
                'n_1h_flips': r['n_1h_flips'],
                'anti_selecting': (pd.notna(k['calmar']) and pd.notna(b['calmar']) and b['calmar'] > k['calmar']),
                'delta_trades': delta_trades, 'delta_pnl_rs': delta_pnl, 'delta_calmar': delta_calmar,
            })

    summary_df = pd.DataFrame(results)
    summary_path = os.path.join(configs.DATA_SWEEP_DIR, 'sweep_p4_summary.csv')
    summary_df.to_csv(summary_path, index=False)
    print(f'Saved summary to {summary_path}')


if __name__ == '__main__':
    main()
