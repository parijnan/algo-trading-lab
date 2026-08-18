"""
run_tracking.py — number, label, and archive every backtest run

Every run gets a sequential id and a short label ("003_short_delta_020"), its
own output directory under data/runs/, a full snapshot of the config values
that produced it, and one row in the run registry. This exists because a
parameter sweep without this is unauditable: three weeks from now "which run
had the 0.20 delta short and the 3-day confirmation window" needs to be
answerable from a file, not memory.

Nothing here encodes trading logic — it is pure bookkeeping, usable by any
phase (signal validation, the engine, a sizing sweep) that wants a tracked run.
"""

import os
import csv
import json
import glob
import subprocess
import logging
from datetime import datetime

import pandas as pd

from configs import OUTPUT_DIR

logger = logging.getLogger(__name__)

RUNS_DIR     = os.path.join(OUTPUT_DIR, "runs")
REGISTRY_FILE = os.path.join(RUNS_DIR, "run_registry.csv")

REGISTRY_COLUMNS = [
    'run_id', 'label', 'phase', 'timestamp', 'git_commit', 'git_dirty',
    'total_pl_rs', 'median_trade_pl_rs', 'n_trades', 'n_skipped',
    'win_rate', 'annual_return_committed_pct', 'monthly_return_pct',
    'gross_over_cost_ratio', 'top_year_share_pct', 'verdict', 'notes',
]


# ---------------------------------------------------------------------------
# Run identity
# ---------------------------------------------------------------------------

def _existing_run_ids() -> list:
    if not os.path.isdir(RUNS_DIR):
        return []
    ids = []
    for name in os.listdir(RUNS_DIR):
        prefix = name.split('_', 1)[0]
        if prefix.isdigit():
            ids.append(int(prefix))
    return sorted(ids)


def next_run_id() -> int:
    """Next sequential run id. Never reused, even if an earlier run's directory is deleted —
    read from the registry first so a pruned directory can't cause a collision."""
    from_dirs = _existing_run_ids()
    from_registry = []
    if os.path.exists(REGISTRY_FILE):
        with open(REGISTRY_FILE, newline='') as fh:
            from_registry = [int(r['run_id']) for r in csv.DictReader(fh) if r['run_id'].isdigit()]
    ids = from_dirs + from_registry
    return (max(ids) + 1) if ids else 1


def _slugify(label: str) -> str:
    keep = (c if c.isalnum() else '_' for c in label.strip().lower())
    slug = ''.join(keep)
    while '__' in slug:
        slug = slug.replace('__', '_')
    return slug.strip('_') or 'run'


def _git_state() -> tuple:
    try:
        commit = subprocess.check_output(
            ['git', 'rev-parse', '--short', 'HEAD'], cwd=os.path.dirname(OUTPUT_DIR),
            stderr=subprocess.DEVNULL).decode().strip()
        dirty = bool(subprocess.check_output(
            ['git', 'status', '--porcelain'], cwd=os.path.dirname(OUTPUT_DIR),
            stderr=subprocess.DEVNULL).decode().strip())
        return commit, dirty
    except (subprocess.CalledProcessError, FileNotFoundError, OSError):
        return None, None


# ---------------------------------------------------------------------------
# Run context — call at the start of a tracked phase
# ---------------------------------------------------------------------------

class RunContext:
    """
    One tracked execution. Usage:

        run = RunContext.start('short_delta_020', phase='sizing_sweep')
        ... write run.trade_log_path, run.legs_log_path, run.trade_logs_dir ...
        run.finish(summary_dict)

    `run.dir` is the run's own output directory: data/runs/<id>_<label>/.
    Every path a phase writes to should live under it, never under the bare
    OUTPUT_DIR, so two runs can never silently overwrite each other's output.
    """

    def __init__(self, run_id: int, label: str, phase: str):
        self.run_id = run_id
        self.label = label
        self.phase = phase
        self.slug = f"{run_id:03d}_{_slugify(label)}"
        self.dir = os.path.join(RUNS_DIR, self.slug)
        self.trade_log_path   = os.path.join(self.dir, "trade_log.csv")
        self.legs_log_path    = os.path.join(self.dir, "legs_log.csv")
        self.skip_log_path    = os.path.join(self.dir, "skipped_contracts.csv")
        self.run_summary_json = os.path.join(self.dir, "run_summary.json")
        self.run_summary_txt  = os.path.join(self.dir, "run_summary.txt")
        self.config_snapshot_path = os.path.join(self.dir, "config_snapshot.json")
        self.trade_logs_dir   = os.path.join(self.dir, "trade_logs")
        self.started_at = datetime.now()

    @classmethod
    def start(cls, label: str, phase: str) -> "RunContext":
        run_id = next_run_id()
        ctx = cls(run_id, label, phase)
        os.makedirs(ctx.trade_logs_dir, exist_ok=True)
        ctx._write_config_snapshot()
        logger.info(f"Run {ctx.slug} (phase={phase}) -> {ctx.dir}")
        return ctx

    def _write_config_snapshot(self) -> None:
        import configs as _cfg
        snapshot = {
            k: v for k, v in vars(_cfg).items()
            if k.isupper() and not k.startswith('_')
            and isinstance(v, (str, int, float, bool, list, tuple, dict, type(None)))
        }
        with open(self.config_snapshot_path, 'w') as fh:
            json.dump(snapshot, fh, indent=2, default=str, sort_keys=True)

    def finish(self, headline: dict, notes: str = '') -> None:
        """
        `headline` supplies the registry's summary columns — pass whatever the
        phase's analysis module computed (total_pl_rs, median_trade_pl_rs, etc.).
        Missing keys are written blank rather than raising, since not every
        phase produces every metric (signal validation has no P&L, for
        instance).
        """
        commit, dirty = _git_state()
        row = {
            'run_id': self.run_id, 'label': self.label, 'phase': self.phase,
            'timestamp': self.started_at.strftime('%Y-%m-%d %H:%M:%S'),
            'git_commit': commit or '', 'git_dirty': dirty if dirty is not None else '',
            'notes': notes,
        }
        for col in REGISTRY_COLUMNS:
            if col not in row:
                row[col] = headline.get(col, '')

        with open(self.run_summary_json, 'w') as fh:
            json.dump({**headline, 'run_id': self.run_id, 'label': self.label,
                      'phase': self.phase, 'git_commit': commit, 'git_dirty': dirty},
                     fh, indent=2, default=str, sort_keys=True)

        _append_registry(row)
        logger.info(f"Run {self.slug} recorded in {REGISTRY_FILE}")


def _append_registry(row: dict) -> None:
    os.makedirs(RUNS_DIR, exist_ok=True)
    is_new = not os.path.exists(REGISTRY_FILE)
    with open(REGISTRY_FILE, 'a', newline='') as fh:
        w = csv.DictWriter(fh, fieldnames=REGISTRY_COLUMNS)
        if is_new:
            w.writeheader()
        w.writerow({c: row.get(c, '') for c in REGISTRY_COLUMNS})


# ---------------------------------------------------------------------------
# Reading back
# ---------------------------------------------------------------------------

def load_registry() -> pd.DataFrame:
    if not os.path.exists(REGISTRY_FILE):
        return pd.DataFrame(columns=REGISTRY_COLUMNS)
    return pd.read_csv(REGISTRY_FILE)


def list_runs() -> list:
    """Every run directory's slug, in id order — for CLI listing or diffing."""
    if not os.path.isdir(RUNS_DIR):
        return []
    return sorted(
        (d for d in os.listdir(RUNS_DIR) if os.path.isdir(os.path.join(RUNS_DIR, d))
         and d.split('_', 1)[0].isdigit()),
        key=lambda d: int(d.split('_', 1)[0]))


def diff_configs(run_slug_a: str, run_slug_b: str) -> dict:
    """Parameter values that differ between two runs' config snapshots."""
    def _load(slug):
        path = os.path.join(RUNS_DIR, slug, "config_snapshot.json")
        with open(path) as fh:
            return json.load(fh)
    a, b = _load(run_slug_a), _load(run_slug_b)
    keys = set(a) | set(b)
    return {k: (a.get(k, '<missing>'), b.get(k, '<missing>'))
            for k in sorted(keys) if a.get(k) != b.get(k)}
