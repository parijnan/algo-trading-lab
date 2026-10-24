"""
Is the local Janus backtest as current as the integrated backtest? The Prometheus-or-Janus pages (the Integrated Backtest's selector, the Hestia Compounding Simulation and the Prometheus vs Janus
Equity page) put the two side by side, so the Janus run has to reach the same last session as the integrated backtest.

    python hestia_backtest/janus_sync.py          # prints the status; exit 0 up to date or ahead, 1 behind, 2 no Janus run

The Janus run is local and gitignored: janus_backtest/machine_run.py --source previous_session writes it (a few minutes) and its run_info.txt names the last session it covered.
"""

import os
import re
import sys

import pandas as pd

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import hb_config as cfg  # noqa: E402

RERUN = 'python janus_backtest/machine_run.py --source previous_session'


def janus_last_session():
    """The last session the local Janus run covered (from its run_info.txt), or None when there is no run."""
    info = os.path.join(os.path.dirname(cfg.JANUS['trades_csv']), 'run_info.txt')
    if not (os.path.exists(info) and os.path.exists(cfg.JANUS['trades_csv'])):
        return None
    m = re.search(r'^last_session\s+(\d{4}-\d{2}-\d{2})', open(info).read(), re.M)
    return pd.Timestamp(m.group(1)) if m else None


def integrated_end(path=None):
    """The last exit day in the integrated backtest, over all four engines (the day its data ends, give or take a day with no exit)."""
    d = pd.read_csv(path or os.path.join(cfg.OUT_DIR, 'integrated_trades.csv'), usecols=['exit_ts'], parse_dates=['exit_ts'])
    return d.exit_ts.max().normalize()


def status(janus_end=None, backtest_end=None):
    """('ok' | 'ahead' | 'behind' | 'missing', message). Janus must reach the integrated backtest's last exit day."""
    janus_end = janus_last_session() if janus_end is None else janus_end
    backtest_end = integrated_end() if backtest_end is None else backtest_end
    if janus_end is None:
        return 'missing', f'no local Janus run ({cfg.JANUS["trades_csv"]}); run: {RERUN}'
    if janus_end < backtest_end:
        return 'behind', (f'Janus run ends {janus_end:%Y-%m-%d}, the integrated backtest {backtest_end:%Y-%m-%d} ({(backtest_end - janus_end).days} days behind); '
                          f'check the local data with python hestia_backtest/data_check.py, then run: {RERUN}')
    if janus_end > backtest_end:
        return 'ahead', f'Janus run ends {janus_end:%Y-%m-%d}, past the integrated backtest ({backtest_end:%Y-%m-%d}); its trades after that day are left out of the pages'
    return 'ok', f'Janus run and integrated backtest both end {janus_end:%Y-%m-%d}'


def main():
    state, msg = status()
    print(f'{state.upper()}: {msg}')
    sys.exit({'ok': 0, 'ahead': 0, 'behind': 1, 'missing': 2}[state])


if __name__ == '__main__':
    main()
