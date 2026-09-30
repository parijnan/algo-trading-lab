"""
One-shot: reset Selene to flat so it can go from paper to live without a stale paper position.

A paper engine's open position lives only in Hestia's ledger and the engine's own state. Switch the engine to live with that position
still recorded and the next start finds the broker book empty, raises criticals and drops the position with no trade record. This
resets Selene deliberately instead, after the session, with the paper trade's details saved so its row can be written later with the
true exit.

    python -m selene_engine.cutover_reset [--dry-run] [--wait-min N]

Runs nothing unless ALL hold, otherwise exits 1 having changed nothing:
  * (if --only-date is given) today is that date, so a leftover one-shot cron line can never reset a live engine next year;
  * Hestia is not running (flag gone, no process), so it cannot overwrite the files;
  * hestia_config says Selene is LIVE on this host (the paper=False change has been pulled) -- resetting a still-paper engine would
    only throw its trade away;
  * Selene has no request in flight, no pending flip and is not frozen (an unexpected state is left for a human).
Other engines' ledger rows and state files are never touched, and are compared before and after.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import List, Optional

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from hestia_core.state_store import StateStore                    # noqa: E402
from selene_engine.state import EngineState                        # noqa: E402

ENGINE = 'selene'
KEEP_FIELDS = ('trade_counter', 'last_processed_boundary', 'roll_executed_date', 'attempts')


@dataclass
class Result:
    ok: bool
    changed: bool = False
    messages: List[str] = field(default_factory=list)


def reset(state_dir: Path, now: Optional[datetime] = None, dry_run: bool = False, engine: str = ENGINE) -> Result:
    """Back up, then reset `engine` to flat and drop its ledger rows. Verifies by reading everything back."""
    now = now or datetime.now()
    tag = now.strftime('%Y%m%d_%H%M%S')
    store = StateStore(state_dir)
    res = Result(ok=False)

    def say(msg):
        res.messages.append(msg)

    blob = store.load_engine_state(engine)
    ledger = store.load_ledger()
    mine = {k: v for k, v in ledger.items() if k[0] == engine}
    if blob is None:
        say(f'{engine}: no saved state, nothing to reset')
        res.ok = True
        return res
    st = EngineState.from_json(blob)
    if st.frozen or st.pending or st.pending_flip or st.pending_missed_flip:
        say(f'{engine}: state is frozen or has a request/flip in flight; refusing to touch it (frozen={st.frozen}, '
            f'pending={list(st.pending)}, pending_flip={bool(st.pending_flip)}, pending_missed_flip={bool(st.pending_missed_flip)})')
        return res
    held = {k[1]: v[0] for k, v in mine.items() if v[0]}
    if st.status != 'in_trade' and not held:
        say(f'{engine}: already flat (status={st.status}, no ledger position); nothing to do')
        res.ok = True
        return res

    say(f'{engine}: open position to reset: status={st.status} direction={st.direction} units={st.units} lots={st.lots} '
        f'entry={st.entry_price} sl={st.sl_price} contract={st.contract_symbol} ledger={held}')
    saved = {'saved_at': now.isoformat(timespec='seconds'), 'engine': engine, 'state': json.loads(st.to_json()),
             'ledger_rows': [{'engine': k[0], 'token': k[1], 'net': v[0], 'avg': v[1], 'ts': v[2].isoformat() if v[2] else None}
                             for k, v in mine.items()]}
    if dry_run:
        say('dry run: nothing written')
        res.ok = True
        return res

    state_file, ledger_file = Path(state_dir) / f'{engine}_state.json', Path(state_dir) / 'ledger.json'
    detail = Path(state_dir) / f'{engine}_paper_trade_{tag}.json'
    detail.write_text(json.dumps(saved, indent=2))
    shutil.copy2(state_file, Path(state_dir) / f'{engine}_state.json.bak_cutover_{tag}')
    if ledger_file.exists():
        shutil.copy2(ledger_file, Path(state_dir) / f'ledger.json.bak_cutover_{tag}')
    say(f'backed up state and ledger; trade details saved to {detail}')

    others_before = {k: v for k, v in ledger.items() if k[0] != engine}
    fresh = EngineState(**{f: getattr(st, f) for f in KEEP_FIELDS})
    store.save_engine_state(engine, fresh.to_json())
    store.save_ledger(others_before)
    res.changed = True

    after_state = EngineState.from_json(store.load_engine_state(engine))
    after_ledger = store.load_ledger()
    problems = []
    if after_state.status != 'watching' or after_state.direction or after_state.entry_price is not None or after_state.lots:
        problems.append(f'state not flat after reset: {after_state}')
    if after_state.trade_counter != st.trade_counter:
        problems.append(f'trade_counter changed {st.trade_counter} -> {after_state.trade_counter}')
    if any(k[0] == engine for k in after_ledger):
        problems.append('ledger still has rows for the engine')
    others_after = {k: v for k, v in after_ledger.items()}
    if {k: v[:2] for k, v in others_after.items()} != {k: v[:2] for k, v in others_before.items() if v[0]}:
        problems.append('another engine\'s ledger rows changed')
    if problems:
        for p in problems:
            say('VERIFY FAILED: ' + p)
        say(f'restore with: cp {state_dir}/{engine}_state.json.bak_cutover_{tag} {state_file} ; cp '
            f'{state_dir}/ledger.json.bak_cutover_{tag} {ledger_file}')
        return res
    say(f'{engine} reset to flat (trade_counter kept at {st.trade_counter}); other engines untouched; verified by read-back')
    res.ok = True
    return res


def hestia_running(flag_dir: Path) -> bool:
    if (Path(flag_dir) / 'hestia_active.flag').exists():
        return True
    out = subprocess.run(['pgrep', '-f', 'python.*hestia.py'], capture_output=True, text=True)
    return bool(out.stdout.strip())


def main(argv=None) -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument('--dry-run', action='store_true')
    ap.add_argument('--wait-min', type=float, default=180.0, help='how long to wait for Hestia to exit before giving up')
    ap.add_argument('--only-date', help='YYYY-MM-DD: do nothing on any other date (a one-shot cron line re-fires every year)')
    args = ap.parse_args(argv)
    import hestia_config as hc

    log_path = hc.REPO_ROOT / 'logs' / f'selene_cutover_{datetime.now():%Y%m%d}.log'
    log_path.parent.mkdir(exist_ok=True)

    def log(msg):
        line = f'{datetime.now():%Y-%m-%d %H:%M:%S} {msg}'
        print(line)
        with open(log_path, 'a') as f:
            f.write(line + '\n')

    if args.only_date and args.only_date != datetime.now().date().isoformat():
        log(f'today is not {args.only_date}; changed nothing')
        return 1
    entry = hc.ENGINES.get(ENGINE)
    if entry is None or entry.paper:
        log(f'{ENGINE} is still PAPER in hestia_config on this host (the paper=False change is not here yet); changed nothing')
        return 1
    if not entry.enabled:
        log(f'{ENGINE} is not enabled on this host; changed nothing')
        return 1
    deadline = time.time() + args.wait_min * 60
    while hestia_running(hc.FLAG_DIR):
        if time.time() > deadline:
            log('Hestia is still running after the wait limit; changed nothing')
            return 1
        time.sleep(30)
    res = reset(hc.STATE_DIR, dry_run=args.dry_run)
    for m in res.messages:
        log(m)
    return 0 if res.ok else 1


if __name__ == '__main__':
    sys.exit(main())
