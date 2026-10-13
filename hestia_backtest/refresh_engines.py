"""
Step 2 of the hestia-backtest skill: re-run each engine's backtest on the data as it stands today, at its live config, and print each engine's headline numbers.

  Selene, Helios   production-parity backtest, window DATA_START to END (parity_backtest_<engine>.py, its own main)
  Typhon           production-parity backtest at the decided config (mult, SL and target from typhon_configs), written as parity_decided_legs/trades.csv
  Prometheus       Fyers-history Phase 3 track: sweep_p3.py (raw signal, mult 2.0 and 2.5) then bespoke_2lot_p3.py (live exits)

END defaults to the last complete session in the live contract tree (data_check.last_complete_session). Each engine runs in its own process, one after another, and its output goes to
<log-dir>/<engine>.log; the numbers printed here are read from the files each one writes. Full re-computation is deterministic and append-like (a causal indicator never changes an earlier
decision when data is added), so earlier trades must not change; compare the counts with the previous run.

    python hestia_backtest/refresh_engines.py [--only Selene Helios Typhon Prometheus] [--end YYYY-MM-DD] [--log-dir DIR]
"""

import argparse
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402
import data_check  # noqa: E402

TYPHON_DECIDED = '''
import os, sys
sys.path.insert(0, os.getcwd())
import pandas as pd
import typhon_configs as configs
from parity_backtest_typhon import simulate, to_trades, load_contracts
end = sys.argv[1]
contracts = load_contracts(configs.SYMBOL, configs.DATA_START, end)
legs = simulate(configs.DATA_START, end, configs.DECIDED_MULTIPLIER, sl_pct=configs.DECIDED_SL_PCT, target_pct=configs.DECIDED_TARGET_PCT, contracts=contracts)
trades = to_trades(legs)
os.makedirs(configs.DATA_SWEEP_DIR, exist_ok=True)
legs.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_decided_legs.csv'), index=False)
trades.to_csv(os.path.join(configs.DATA_SWEEP_DIR, 'parity_decided_trades.csv'), index=False)
print('typhon decided', len(trades), 'trades through', end, '| open at end:', legs.attrs.get('open_at_end'))
'''


def jobs(end: str) -> dict:
    py = sys.executable
    return {
        'Selene': ([py, '-B', 'parity_backtest_selene.py', end], 'selene_backtest'),
        'Helios': ([py, '-B', 'parity_backtest_helios.py', end], 'helios_backtest'),
        'Typhon': ([py, '-B', '-c', TYPHON_DECIDED, end], 'typhon_backtest'),
        'Prometheus': ([py, '-B', '-c', 'import subprocess,sys; sys.exit(subprocess.call([sys.executable,"-B","sweep_p3.py"]) or subprocess.call([sys.executable,"-B","bespoke_2lot_p3.py"]))'],
                       os.path.join('prometheus_backtest', 'phase3_fyers')),
    }


def main():
    p = argparse.ArgumentParser()
    p.add_argument('--only', nargs='+', choices=list(cfg.ENGINES))
    p.add_argument('--end')
    p.add_argument('--log-dir', default=os.path.join(cfg.OUT_DIR, 'logs'))
    a = p.parse_args()
    os.makedirs(a.log_dir, exist_ok=True)
    end = a.end or min(data_check.last_complete_session(e['instrument']) for e in cfg.ENGINES.values())
    print(f'backtest end date {end}', flush=True)
    for name, (cmd, cwd) in jobs(end).items():
        if a.only and name not in a.only:
            continue
        t0 = time.time()
        with open(os.path.join(a.log_dir, f'{name}.log'), 'w') as log:
            rc = subprocess.call(cmd, cwd=os.path.join(cfg.REPO, cwd), stdout=log, stderr=subprocess.STDOUT)
        print(f'{name:11s} exit code {rc}  {time.time() - t0:5.0f}s  log {os.path.join(a.log_dir, name + ".log")}', flush=True)
        if rc != 0:
            print(f'  FAILED: read the log before building the ledger', flush=True)


if __name__ == '__main__':
    main()
