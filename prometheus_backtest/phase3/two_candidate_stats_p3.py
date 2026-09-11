"""
Prometheus - Phase 3: per-trade Calmar/drawdown/P&L stats for the
"Two calibrated candidates" comparison table in prometheus_backtest/README.md
-- reproduces that table's own methodology (each trade's lot1+lot2 P&L
combined into ONE cash-flow event, credited at the later of the two lots'
own exit timestamps, at static 1-unit sizing rather than dynamic/
compounding) directly from each multiplier's bespoke_trade_summary.csv.

Methodology changed 2026-09-11 (user decision): previously each lot was its
own separate chronological cash-flow event ("per-lot-exit-event"). Switched
to per-trade because the actual capital-sizing mechanism this drawdown
figure is meant to inform (_calculate_units(), mirrored in
dynamic_sizing_sim.py) only ever reads capital at trade boundaries -- it
resizes units once per trade, at entry, using capital AFTER the prior
trade's BOTH lots have closed, never at a lot1-only intermediate point. The
per-lot split real intra-trade settlement timing but answered a different
question (real-time margin-call risk) than the one this table exists to
answer (capital efficiency / base-capital sizing) -- see prometheus_backtest/
README.md's "Drawdown methodology: per-trade vs. per-lot-exit" note for the
full reasoning and the numeric comparison between the two.

Not a new methodology -- this script exists because the README's own note
says the two-candidate table numbers were "computed directly from the
refreshed bespoke_trade_summary.csv files, not from a re-published
artifact", i.e. ad-hoc each time. Written as a reusable script so the next
refresh (parameter change, data refresh) doesn't have to redo that by hand.

Usage: python two_candidate_stats_p3.py
"""

import os

import pandas as pd

HERE = os.path.dirname(os.path.abspath(__file__))
SWEEP_DIR = os.path.join(HERE, 'data_sweep')

CANDIDATES = [
    ('2.0', 'Mult 2.0 (SL 2.2/T1 2.2/T2 5.0)'),
    ('2.5', 'Mult 2.5 (SL 1.0/T1 1.25/T2 4.0)'),
]


def per_trade_stats(mult_label: str) -> dict:
    path = os.path.join(SWEEP_DIR, f'mult_{mult_label}', 'bespoke_trade_summary.csv')
    df = pd.read_csv(path, parse_dates=['entry_ts', 'lot1_exit_ts', 'lot2_exit_ts'])

    events = []
    for _, t in df.iterrows():
        trade_exit_ts = max(t['lot1_exit_ts'], t['lot2_exit_ts'])
        events.append((trade_exit_ts, t['total_pnl_rs']))
    ev = pd.DataFrame(events, columns=['ts', 'delta_rs']).sort_values('ts').reset_index(drop=True)
    ev['equity'] = ev['delta_rs'].cumsum()
    ev['peak'] = ev['equity'].cummax()
    ev['drawdown_rs'] = ev['equity'] - ev['peak']
    max_dd = ev['drawdown_rs'].min()

    trade_pnl = df['total_pnl_rs']
    wins = trade_pnl[trade_pnl > 0]
    losses = trade_pnl[trade_pnl <= 0]
    total_pnl = trade_pnl.sum()

    return {
        'n_trades': len(df),
        'win_pct': round(len(wins) / len(df) * 100, 2),
        'total_pnl_rs': round(total_pnl, 0),
        'avg_win_rs': round(wins.mean(), 0) if len(wins) else float('nan'),
        'avg_loss_rs': round(losses.mean(), 0) if len(losses) else float('nan'),
        'max_win_rs': round(wins.max(), 0) if len(wins) else float('nan'),
        'max_loss_rs': round(losses.min(), 0) if len(losses) else float('nan'),
        'max_drawdown_rs': round(max_dd, 0),
        'calmar': round(total_pnl / abs(max_dd), 2) if max_dd else float('nan'),
    }


def main():
    rows = []
    for mult_label, name in CANDIDATES:
        stats = per_trade_stats(mult_label)
        stats['candidate'] = name
        rows.append(stats)
        print(f"{name}:")
        print(f"  Total trades: {stats['n_trades']}")
        print(f"  Win %:        {stats['win_pct']}%")
        print(f"  Total P&L:    Rs {stats['total_pnl_rs']:,.0f}")
        print(f"  Avg win/loss: Rs {stats['avg_win_rs']:,.0f} / Rs {stats['avg_loss_rs']:,.0f}")
        print(f"  Max win/loss: Rs {stats['max_win_rs']:,.0f} / Rs {stats['max_loss_rs']:,.0f}")
        print(f"  Max drawdown: Rs {stats['max_drawdown_rs']:,.0f}")
        print(f"  Calmar:       {stats['calmar']}")
        print()

    out_df = pd.DataFrame(rows)
    out_path = os.path.join(SWEEP_DIR, 'two_candidate_stats.csv')
    out_df.to_csv(out_path, index=False)
    print(f'Saved to {out_path}')


if __name__ == '__main__':
    main()
