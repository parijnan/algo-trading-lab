"""
hestia_config.py: configuration for Hestia, the MCX multi-strategy host (plans/selene-production.md, plans/hestia-p4-live-services.md).

Named hestia_config (not configs.py) so it can never collide with a strategy directory's own modules in sys.modules. It is read by
hestia.py at startup; nothing here is modified at runtime. Runtime files live under hestia_data/ (gitignored): engine state and the
request journal, flag files, the per-token intraday cache, per-engine trade logs, and the Angel One session lock.

Adding an engine is an entry in ENGINES plus the engine package: no host, listener or report code changes.
"""

from dataclasses import dataclass
from pathlib import Path

REPO_ROOT = Path(__file__).parent

# ---- paths -----------------------------------------------------------------------------------------------------------------
HESTIA_DIR = REPO_ROOT / 'hestia_data'
STATE_DIR = HESTIA_DIR / 'state'              # engine decision state, ledger.json, requests_<date>.jsonl, <engine>_sizing.json
FLAG_DIR = HESTIA_DIR / 'flags'               # hestia_active.flag and <engine>_command.flag (EXIT | KILL | DISABLE)
CACHE_DIR = HESTIA_DIR / 'cache'              # per-token intraday cache and private backfill files
TRADES_DIR = HESTIA_DIR / 'trades'            # <engine>_trades.csv in Prometheus's 26-column format
LOG_DIR = REPO_ROOT / 'logs'                  # hestia_<YYYYMMDD>.log (the host); engines log under their own names
SESSION_LOCK_FILE = HESTIA_DIR / 'angel_session.lock'
# Other processes that log in to the same Angel One account without knowing about the session lock. Hestia will not start while
# one of these pid files names a live process (a second login would evict its order capability). Read only.
LEGACY_PID_FILES = {'standalone Prometheus': REPO_ROOT / 'prometheus_production' / 'data' / 'prometheus.pid'}

# Where the Slack listener sends Prometheus's operator commands and sizing overrides (hestia_core/slack_bridge.py). False while the
# standalone process trades; True at cutover, False again on rollback.
SLACK_PROMETHEUS_VIA_HESTIA = False

PIPELINE_DATA_DIR = REPO_ROOT / 'data_pipeline' / 'data'
MCX_DATA_DIR = PIPELINE_DATA_DIR / 'mcx'
INSTRUMENT_MASTER_FILE = PIPELINE_DATA_DIR / 'mcx_instrument_master.csv'
MCX_HOLIDAYS_FILE = PIPELINE_DATA_DIR / 'mcx_holidays.csv'
CREDS_FILE = REPO_ROOT / 'data' / 'user_credentials.csv'

# ---- Slack (the same channels as leto_config, shared by every strategy) -------------------------------------------------------
SLACK_TRADEBOT_CHANNEL = '#tradebot-updates'   # session lifecycle, engine start and stop
SLACK_TRADE_ALERTS = '#trade-alerts'           # entries, exits, closed trades
SLACK_TRADE_UPDATES = '#trade-updates'         # periodic in-trade updates
SLACK_ERRORS_CHANNEL = '#error-alerts'         # warnings and worse
ALERT_CHANNELS = {
    'info': SLACK_TRADEBOT_CHANNEL, 'warning': SLACK_ERRORS_CHANNEL, 'error': SLACK_ERRORS_CHANNEL,
    'critical': SLACK_ERRORS_CHANNEL, 'trade': SLACK_TRADE_ALERTS, 'trade-alerts': SLACK_TRADE_ALERTS,
    'trade-updates': SLACK_TRADE_UPDATES, 'tradebot-updates': SLACK_TRADEBOT_CHANNEL,
}
ALERT_COOLDOWN_S = 30.0                        # identical alert text from the same engine is sent at most this often

# ---- the core: dispatch, retries, roll window, supervision, reconciliation ---------------------------------------------------
CORE = dict(
    roll_window_days=5,                        # no new entries within 5 trading days of expiry (one rule for every commodity)
    restart_limit=3, restart_window_s=1800.0, restart_backoff_s=(5.0, 30.0, 120.0),
    silence_warn_s=180.0, silence_critical_s=300.0,
    reconcile_interval_s=60.0,                 # UNCONFIRMED orders are re-read this often on their own
    ledger_reconcile_interval_s=300.0,         # ledger versus the broker's position book
)

# ---- the live data service ---------------------------------------------------------------------------------------------------
LIVE_DATA = dict(
    seed_days=18,                              # calendar days of 1-minute history behind the Supertrend seed
    deferred_bar_cutoff_min=1.0,               # wait this long past a 15-minute boundary for an incomplete window
    poll_stagger_s=5.0,                        # offset between tokens inside a minute (candle calls never bunch up)
    seed_contracts_per_instrument=2,           # front live contract and the next (for a roll)
)
MCX_FO_WS_EXCHANGE_TYPE = 5                    # websocket_feed exchange type for MCX F&O

# ---- the Angel One adapter and the paper broker ------------------------------------------------------------------------------
ANGEL = dict(order_timeout_s=30.0, poll_interval_s=1.0)
PAPER_CASH = 1_000_000.0                        # each paper engine's own pool

# ---- lifecycle -----------------------------------------------------------------------------------------------------------------
LIFECYCLE = dict(engine_join_timeout_s=60.0, drain_timeout_s=60.0, flush_timeout_s=10.0, terminate_despite_hung=False)
FLAG_POLL_S = 1.0
EXIT_RETRY_S = 60.0
BOOTSTRAP_WAIT_S = 60.0                         # how long startup waits for the broker's position book
EVENING_SESSION_WAKE_BUFFER_MIN = 5             # on an evening-only day, wake this long before the 17:00 open


@dataclass(frozen=True)
class EngineEntry:
    instrument: str
    factory: str                                # 'package.module:callable', called with no arguments, returns an Engine
    enabled: bool = False
    paper: bool = False                         # True: orders go to the paper broker and the engine has its own cash pool
    lots_per_unit: int = 1
    dynamic: bool = False                       # sizing: static units unless the engine's own dynamic rule is switched on
    static_units: int = 1
    unit_cap: int = 50                          # HARD LIMIT enforced at admission; an override file can never raise it


# The registry. Engines are ported or written in later phases (plans/selene-production.md section 10: P5 Prometheus, P7 Selene);
# until then every entry is disabled and Hestia starts with no engines.
ENGINES = {
    'prometheus': EngineEntry(instrument='CRUDEOILM', factory='prometheus_engine.engine:build', enabled=False, lots_per_unit=2),
    'selene': EngineEntry(instrument='SILVERMIC', factory='selene_engine.engine:build', enabled=False, paper=True),
}


# ---- machine-local overrides (Delos only) ------------------------------------------------------------------------------------
# Nothing is enabled in the committed configuration, so a `python hestia.py` on any other machine (the laptop) exits before it logs
# in: the session lock and the legacy pid check are per machine and could not see Delos's process, and a second login would evict its
# order capability. The machine allowed to trade carries a gitignored hestia_local.py:
#
#     ENGINES = {'prometheus': dict(enabled=True, paper=True, static_units=5, unit_cap=10)}
#     SLACK_PROMETHEUS_VIA_HESTIA = True
#
# Only fields of an existing registry entry can be changed; an unknown engine or field is an error, not a silent no-op.

def apply_local_overrides(engines, local):
    """The registry with `local.ENGINES` applied (a dict of engine name to field overrides); `local` may be None."""
    import dataclasses
    changes = getattr(local, 'ENGINES', None) or {}
    out = dict(engines)
    for name, fields in changes.items():
        if name not in out:
            raise KeyError(f'hestia_local.ENGINES names {name!r}, which is not in the registry {sorted(out)}')
        out[name] = dataclasses.replace(out[name], **fields)
    return out


try:
    import hestia_local as _local          # noqa: E402  (gitignored; absent on every machine that must not trade)
except ImportError:
    _local = None
ENGINES = apply_local_overrides(ENGINES, _local)
SLACK_PROMETHEUS_VIA_HESTIA = bool(getattr(_local, 'SLACK_PROMETHEUS_VIA_HESTIA', SLACK_PROMETHEUS_VIA_HESTIA))
LOCAL_OVERRIDES_PRESENT = _local is not None
