"""
Seed the Hestia-hosted Prometheus engine's saved state from the standalone process's state, at cutover.

    python -m prometheus_engine.seed_state [standalone_data_dir]            # show what would be written
    python -m prometheus_engine.seed_state [standalone_data_dir] --write    # write hestia_data/state/prometheus_state.json

Reads `prometheus_state.csv` and `trade_counter.txt` from the standalone process's data directory (default
prometheus_production/data). Flat (idle or watching) becomes a blank engine that carries the trade counter, so trade ids continue.
A position still open converts field for field (levels persisted verbatim, as the standalone process does); Hestia's own ledger, read
from the broker at start, still wins if the two disagree. Refuses to overwrite an existing Hestia state without --force, and refuses
to run while the standalone process is alive (its state is changing under you).
"""

from __future__ import annotations

import json
import os
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional, Sequence, Tuple

import pandas as pd

from prometheus_engine.state import EngineState

REPO = Path(__file__).resolve().parents[1]
DEFAULT_STANDALONE = REPO / 'prometheus_production' / 'data'


def _clean(v):
    return None if v is None or (isinstance(v, float) and pd.isna(v)) or v == '' else v


LOT_SIZE = 10          # CRUDEOILM; the closing P&L of an already-booked lot is points x lots x lot size


def convert(row: dict, trade_counter: int, lot_size: int = LOT_SIZE) -> EngineState:
    """An EngineState from one row of the standalone prometheus_state.csv."""
    g = {k: _clean(v) for k, v in row.items()}
    if g.get('status') != 'in_trade':
        return EngineState(status='watching', trade_counter=trade_counter, last_processed_boundary=g.get('last_processed_boundary'))
    direction = g['direction']
    entry_row = {'trade_id': trade_counter, 'contract_expiry': g.get('contract_expiry'),
                 'direction': f"{direction}-rollover" if g.get('recalibration_basis_price') is not None else direction,
                 'units': int(g['units']), 'entry_ts': g.get('entry_ts'), 'entry_price': float(g['entry_price']),
                 'signal_ts': g.get('signal_ts'), 'signal_close': g.get('signal_close'), 'sl_price': g.get('sl_price'),
                 'lot1_target': g.get('lot1_target'), 'lot2_target': g.get('lot2_target'),
                 'lot2_target_source': g.get('lot2_target_source')}
    for lot in (1, 2):                                          # a lot already booked keeps its exit fields in the trade row
        if g.get(f'lot{lot}_status') not in (None, 'open', 'never_opened'):
            px, lots = g.get(f'lot{lot}_exit_price'), int(g.get(f'lot{lot}_lots') or 0)
            pts = None if px is None else round((float(px) - float(g['entry_price'])) * (1 if direction == 'bullish' else -1), 2)
            entry_row.update({f'lot{lot}_exit_ts': g.get(f'lot{lot}_exit_ts'), f'lot{lot}_exit_price': px,
                              f'lot{lot}_exit_reason': g.get(f'lot{lot}_exit_reason'), f'lot{lot}_pnl_points': pts,
                              f'lot{lot}_pnl_rs': None if pts is None else round(pts * lots * lot_size, 2)})
    return EngineState(
        status='in_trade', direction=direction, units=int(g['units']), entry_price=float(g['entry_price']),
        basis_price=g.get('recalibration_basis_price'), entry_ts=g.get('entry_ts'), signal_ts=g.get('signal_ts'),
        signal_close=g.get('signal_close'), contract_token=str(g['token']), contract_symbol=g.get('symbol'),
        contract_expiry=g.get('contract_expiry'), sl_price=g.get('sl_price'), lot1_target=g.get('lot1_target'),
        lot1_lots=int(g.get('lot1_lots') or 0), lot1_status=g.get('lot1_status') or 'never_opened',
        lot1_exit_price=g.get('lot1_exit_price'), lot2_target=g.get('lot2_target'), lot2_source=g.get('lot2_target_source'),
        lot2_lots=int(g.get('lot2_lots') or 0), lot2_status=g.get('lot2_status') or 'never_opened',
        lot2_exit_price=g.get('lot2_exit_price'), trade_counter=trade_counter, trade_row=entry_row,
        last_processed_boundary=g.get('last_processed_boundary'))


def read_standalone(data_dir: Path) -> Tuple[EngineState, dict]:
    row = pd.read_csv(data_dir / 'prometheus_state.csv').iloc[0].to_dict()
    counter = int((data_dir / 'trade_counter.txt').read_text().strip())
    return convert(row, counter), row


def standalone_alive(data_dir: Path) -> Optional[int]:
    try:
        pid = int((data_dir / 'prometheus.pid').read_text().strip())
    except (FileNotFoundError, ValueError):
        return None
    try:
        os.kill(pid, 0)
        return pid
    except OSError:
        return None


def main(argv: Sequence[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith('--')]
    data_dir = Path(args[0]) if args else DEFAULT_STANDALONE
    write, force = '--write' in argv, '--force' in argv
    pid = standalone_alive(data_dir)
    if pid is not None:
        print(f'refusing: the standalone Prometheus is running (pid {pid}); its state is changing')
        return 1
    state, row = read_standalone(data_dir)
    print(f'standalone state: {row.get("status")}'
          + (f' {row.get("direction")} {row.get("units")} unit(s) @ {row.get("entry_price")}' if row.get('status') == 'in_trade' else '')
          + f'; trade counter {state.trade_counter}; watermark {state.last_processed_boundary}')
    print(f'engine state to seed: {state.status}, trade counter {state.trade_counter}')
    if not write:
        print('(dry run: pass --write to save it)')
        return 0
    sys.path.insert(0, str(REPO))
    import hestia_config as cfg
    from hestia_core.state_store import StateStore
    target = Path(cfg.STATE_DIR) / 'prometheus_state.json'
    if target.exists() and not force:
        print(f'refusing: {target} already exists (pass --force to replace it)')
        return 1
    Path(cfg.STATE_DIR).mkdir(parents=True, exist_ok=True)
    StateStore(cfg.STATE_DIR).save_engine_state('prometheus', state.to_json())
    print(f'wrote {target}')
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv))
