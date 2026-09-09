"""
Prometheus - Phase 3: full backtest-refresh pipeline, both instruments.

Runs the full deterministic re-backtest chain (raw signal sweep -> bespoke
exit overlay -> per-lot-exit-event stats -> dynamic sizing x2 -> risk of
ruin) against whatever data currently sits in data_pipeline/data/mcx/, for
both CRUDEOILM (phase3/) and CRUDEOIL (phase3_crudeoil/) -- production's
live config (SL 2.2% / T1 2.2% / T2 5.0%, mult 2.0) plus mult 2.5 for
comparison, matching what bespoke_2lot_p3.py's own runs list already does.

This is a full recompute, not an incremental append -- but because every
stage is a deterministic, purely-causal function of the (now-longer) 1-min
price history, re-running from scratch reproduces every previously-computed
trade identically (same trade_id, same entry/exit price and time -- ST is a
trailing indicator, it cannot look ahead, so extending the tail of the data
never changes a decision made earlier in it) and simply adds new trades at
the end. The *effect* is append-like even though the *mechanism* is a fresh
computation each time. A genuinely incremental recompute would need to
reconstruct Supertrend's recursive internal state and handle
contract-rollover boundaries exactly at the append point -- fiddly, and
unnecessary given how cheap a full recompute is (~130k 1-min bars x 8
multipliers x 2 instruments, single-digit minutes).

One real consequence of the full recompute: whichever trade was "still
open at data end" in the previous run will very likely close for real once
the extra days resolve its next signal flip -- that's expected, not a bug.

Does NOT touch prometheus_backtest/README.md, prometheus_production/README.md,
root README.md, or any published artifact -- those need human/Claude
judgment to update correctly (numbers need to land in the right prose
context, not blind find-replace), so this script only produces the fresh
numbers. Updating docs, rebuilding artifacts, and committing is the rest of
the workflow this script is one step of -- see
.claude/skills/prometheus-refresh/SKILL.md for the full checklist.

Usage:
  python prometheus_backtest/refresh_pipeline.py
  python prometheus_backtest/refresh_pipeline.py --skip-sweep
    (skip the slowest stage for both instruments -- use if sweep_p3.py was
    already run this session and only the downstream stages need re-deriving,
    e.g. after an exit-parameter change with no new price data)
  python prometheus_backtest/refresh_pipeline.py --only crudeoilm
  python prometheus_backtest/refresh_pipeline.py --only crudeoil
    (run one instrument only)
"""

import argparse
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent

INSTRUMENTS = {
    'crudeoilm': HERE / 'phase3',
    'crudeoil': HERE / 'phase3_crudeoil',
}

STAGES = [
    ('sweep_p3.py', 'Raw signal sweep (all 8 multipliers) -- SLOWEST stage'),
    ('bespoke_2lot_p3.py', 'Bespoke exit overlay (mult 2.0 T1=2.2%, mult 2.5)'),
    ('two_candidate_stats_p3.py', 'Per-lot-exit-event Calmar/drawdown stats'),
    ('dynamic_sizing_sim.py', 'Dynamic-sizing equity simulation (no slippage)'),
    ('dynamic_sizing_sim_slippage.py', 'Dynamic-sizing equity simulation (slippage-adjusted)'),
    ('risk_of_ruin_p3.py', 'Risk-of-ruin Monte Carlo'),
]


def run_stage(script_dir: Path, script_name: str, label: str) -> str:
    script_path = script_dir / script_name
    print(f'\n{"=" * 70}\n{script_dir.name} :: {script_name} -- {label}\n{"=" * 70}')
    t0 = time.time()
    result = subprocess.run([sys.executable, str(script_path)], cwd=str(script_dir),
                             capture_output=True, text=True)
    elapsed = time.time() - t0
    print(result.stdout)
    if result.returncode != 0:
        print(result.stderr, file=sys.stderr)
        raise SystemExit(f'{script_name} in {script_dir} failed (exit {result.returncode}) after {elapsed:.1f}s')
    print(f'-- {script_name} done in {elapsed:.1f}s --')
    return result.stdout


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--skip-sweep', action='store_true',
                         help='skip sweep_p3.py (the slowest stage) for both instruments')
    parser.add_argument('--only', choices=['crudeoilm', 'crudeoil'],
                         help='run only one instrument instead of both')
    args = parser.parse_args()

    instruments = {args.only: INSTRUMENTS[args.only]} if args.only else INSTRUMENTS

    all_output = {}
    for name, script_dir in instruments.items():
        print(f'\n{"#" * 70}\n# {name.upper()} ({script_dir})\n{"#" * 70}')
        outputs = {}
        for script_name, label in STAGES:
            if args.skip_sweep and script_name == 'sweep_p3.py':
                print(f'\n-- skipping {script_name} for {name} (--skip-sweep) --')
                continue
            outputs[script_name] = run_stage(script_dir, script_name, label)
        all_output[name] = outputs

    print(f'\n\n{"#" * 70}\n# ALL STAGES COMPLETE -- {len(instruments)} instrument(s)\n{"#" * 70}')
    print('\nNext steps (not done by this script -- see .claude/skills/prometheus-refresh/SKILL.md):')
    print('  1. Read the headline numbers printed above for each stage.')
    print('  2. Update prometheus_backtest/README.md, prometheus_production/README.md, and the')
    print('     root README.md wherever they cite these figures (grep for the OLD numbers to find')
    print('     every spot -- this session has repeatedly found stray copies, e.g. the root README.md')
    print('     has its own separate copy of the CRUDEOILM dynamic-sizing writeup).')
    print('  3. Rebuild the published artifacts (Trade Ledger, both Dynamic Sizing Simulation pages).')
    print('  4. Run: python -m pytest tests/')
    print('  5. Commit and push.')


if __name__ == '__main__':
    main()
