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
RUNNING_ROW_DIR = HESTIA_DIR / 'trades' / 'running_rows'   # <engine>/trade_NNNN_<entry_ts>.csv, one row/minute while in-trade
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
    poll_stagger_s=0.0,                        # offset between tokens inside a minute; 5.0 until 2026-10-01, then 1.0, then 0.0 at the owner's request: when no Prometheus order is in flight, any other engine's flip should act at the lowest possible delay. With 0 all active tokens poll at the same instant; the gateway's single HTTP lock and 3/s candle budget serialise them, in thread-race rather than registration order
    seed_contracts_per_instrument=2,           # front live contract and the next (for a roll)
)
# ---- the candle source (plans/hestia-fyers-candle-source.md) ---------------------------------------------------------------
# 'angel' (the default): Angel One only, no Fyers code runs. 'shadow' (Phase 1): Angel One stays the only source any engine sees; Fyers is
# queried in parallel and the comparison is RECORDED under hestia_data/shadow/. 'rescue' (Phase 2): the same recording, plus Fyers fills a window
# Angel One failed to give (smart mode, Phase 3, makes Fyers the first source for `smart_instruments`). A host or
# hestia_local.py overrides keys of this dict through its own CANDLE_SOURCE entry.
FYERS_TOKEN_FILE = HESTIA_DIR / 'fyers_token.json'      # written daily at 06:35 IST by the laptop job (plans/fyers-auto-token.md)
FYERS_OFF_FLAG = FLAG_DIR / 'fyers_off.flag'            # touch it to stop every Fyers call at the next poll, no restart needed
SHADOW_DIR = HESTIA_DIR / 'shadow'
CANDLE_SOURCE = dict(
    mode='angel',
    instruments=('CRUDEOILM', 'SILVERMIC', 'GOLDPETAL', 'NATGASMINI'),
    timeout_s=3.0,                                      # one Fyers call; a slow Fyers must never hold a bar boundary
    retry_s=1.0,                                        # between attempts while the just-closed minute has not appeared
    max_wait_s=6.0,                                     # stop waiting for it this long after the tick
    smart_instruments=('CRUDEOILM',),                   # 'smart' mode only: Fyers is asked FIRST for these (the pilot, plans/hestia-fyers-candle-source.md section 7d)
    smart_settle_s=0.5,                                 # a minute is trusted this long after it closes (owner's call 2026-10-10; measured: final in 97.5% of snapshots at +0.5 s, 100% from +0.8 s)
    smart_max_wait_s=3.0,                               # give up on Fyers this long after the minute closed and let Angel One's burst run
    # 'rescue' mode only (Phase 2): Fyers is asked for a window after this many failed Angel One attempts (5 = only after the whole burst has failed;
    # lower values rescue sooner and stop the Angel One attempts early), and only once the just-closed minute is this many seconds old, because Fyers's
    # first-seen value of a minute is provisional (measured 2026-10-05 with research/fyers_mcx_validation/settle_probe.py: 54% final at +0.1 s, 90% at +0.3 s,
    # 95% at +0.4 s, 100% from +0.8 s). 0 because with the default 5 attempts the Angel One burst has already taken seconds, so the minute has settled; raise it
    # only if rescue_after_attempts is lowered enough for Fyers to be asked within about a second of the minute closing.
    rescue_after_attempts=5,
    settle_s=0.0,
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


# The registry. Engines are ported or written in later phases (plans/selene-production.md section 10: P5 Prometheus, P7 Selene,
# P8 Helios); until then every entry is disabled and Hestia starts with no engines.
ENGINES = {
    'prometheus': EngineEntry(instrument='CRUDEOILM', factory='prometheus_engine.engine:build', enabled=False, lots_per_unit=2),
    'selene': EngineEntry(instrument='SILVERMIC', factory='selene_engine.engine:build', enabled=False, paper=True),
    'helios': EngineEntry(instrument='GOLDPETAL', factory='helios_engine.engine:build', enabled=False, paper=True, lots_per_unit=20),
    # NATGASMINI, decided config mult 3.0 / SL 0.8% / target 15%, one position group. 1 unit = 2 lots (owner's decision
    # 2026-09-30: brings its margin per unit level with Selene's and Helios's; keep equal to typhon_engine.engine_configs
    # lots_per_unit, which a test pins). Real-price/real-roll-execution parity calibration, plan Step 4
    # (plans/typhon-natgasmini-st-strategy.md). Registered disabled here; enabled paper on Delos via TRADING_HOSTS below.
    'typhon': EngineEntry(instrument='NATGASMINI', factory='typhon_engine.engine:build', enabled=False, paper=True, lots_per_unit=2),
}


# ---- which machine may trade -------------------------------------------------------------------------------------------------
# Nothing is enabled in the base configuration above, so a `python hestia.py` on any machine not listed here exits before it logs in:
# the session lock and the legacy pid check are per machine and could not see Delos's process, and a second login would evict its
# order capability (AB1007). A machine is listed by its hostname; its entry changes fields of existing registry entries (an unknown
# engine or field is an error, not a silent no-op) and may switch the Slack listener to Hestia. This is committed and deployed with
# the code. A gitignored hestia_local.py, if present, is applied on top of it (a temporary, per-machine change that needs no commit).

TRADING_HOSTS = {
    # Delos, from the first live session (2026-09-29): Prometheus live at 1 unit (2 lots), hard cap 10 units. The Slack Exit, Kill,
    # Disable and sizing buttons drive Hestia's files.
    # Selene DRY_RUN alongside it (P8, plans/selene-production.md): paper only, no real order ever reaches the broker
    # (BrokerRouter routes a paper engine's orders to PaperBroker, never AngelBrokerPort); 1 unit, cap 10, matching
    # Prometheus's own conservative first-session sizing even though a paper cap risks nothing real. ENABLED 2026-09-29
    # (user's go-ahead, after Prometheus's first live session confirmed smooth). Restart timing is unrestricted (the
    # user's call, same day): restart-recovery (bootstrap against the broker ledger, UNCONFIRMED settled by reading the
    # order, never re-sent blind) is trusted to handle a restart mid-position or mid-transition, not just while flat.
    # Helios DRY_RUN alongside both (plans/hestia-p8-helios-engine.md), enabled 2026-09-29: paper only, same posture as
    # Selene's own paper deployment. 1 unit (= lots_per_unit above, 20 lots), cap 10 units -- matching the other two
    # engines' own conservative first-session sizing, not derived from any Helios-specific risk analysis (position
    # sizing and risk of ruin are explicitly deferred, plan §4h's own "Not yet done").
    # Selene LIVE from 2026-10-01 (user's decision 2026-09-30, after a few paper trades): 1 unit, cap 10. Its open paper position is
    # reset to flat beforehand by cutover_reset (a one-shot Delos cron at 00:30 on 2026-10-01), because a
    # paper position cannot be closed through the live broker and would otherwise be dropped with critical alerts at start.
    # Helios LIVE from 2026-10-02 (user's decision 2026-10-01, after a paper trade was followed through its whole lifecycle; margin
    # confirmed available): 1 unit = 20 lots, cap 10 units. Its open paper position is reset to flat beforehand by
    # hestia_core/cutover_reset.py --engine helios (a one-shot Delos cron at 00:30 on 2026-10-02), as Selene's was.
    # Typhon LIVE from 2026-10-02 (user's decision 2026-10-01, after one full paper trade -- entry, netted flip exit, re-entry -- at
    # the real 2-lot size; margin confirmed): 1 unit = 2 lots, cap 10 units. Its open paper position is reset to flat beforehand by
    # hestia_core/cutover_reset.py --engine typhon (a one-shot Delos cron at 00:35 on 2026-10-02), as Selene's and Helios's were.
    # (Before that Typhon ran paper alongside the others from 2026-09-30, after replay_check.py reproduced 61/61 oracle decisions,
    # plan Step 6.)
    'delos': dict(ENGINES={'prometheus': dict(enabled=True, paper=False, static_units=1, unit_cap=10),
                           'selene': dict(enabled=True, paper=False, static_units=1, unit_cap=10),
                           'helios': dict(enabled=True, paper=False, static_units=1, unit_cap=10),
                           'typhon': dict(enabled=True, paper=False, static_units=1, unit_cap=10)},
                  SLACK_PROMETHEUS_VIA_HESTIA=True,
                  # Fyers candle SHADOW on from 2026-10-05 (user's go-ahead, Phase 1 of plans/hestia-fyers-candle-source.md), upgraded to RESCUE the same
                  # day (Phase 2, user's go-ahead after the Zerodha cross-check: finalized Fyers matched Zerodha on 12 of 13 differing fields): Angel One
                  # stays the primary source; Fyers is asked only after the whole Angel One burst has failed for a window, fills only minutes the engine
                  # lacks, and everything is recorded under hestia_data/shadow/. Stop it at once, no restart, with `touch hestia_data/flags/fyers_off.flag`
                  # (Angel One only); set mode back to 'shadow' to stop the fallback at the next start.
                  # SMART (Phase 3, Fyers first) from 2026-10-10 on the owner's go-ahead ("we're actually switching to a better source with 2 layers of backup"):
                  # CRUDEOILM (Prometheus) is asked of Fyers FIRST, a single pull 0.5 s after each minute closes; Angel One's burst, then the Fyers rescue, then the recovery
                  # queue stand behind it. The other three instruments stay Angel One first with the rescue behind them. Back to the previous behaviour: mode 'rescue' (restart),
                  # or at once with `touch hestia_data/flags/fyers_off.flag` (Angel One only, next poll, no restart).
                  CANDLE_SOURCE={'mode': 'smart', 'smart_instruments': ('CRUDEOILM',)}),
}


def apply_local_overrides(engines, local):
    """The registry with `local.ENGINES` applied (a dict of engine name to field overrides); `local` may be None."""
    import dataclasses
    changes = getattr(local, 'ENGINES', None) or {}
    out = dict(engines)
    for name, fields in changes.items():
        if name not in out:
            raise KeyError(f'the overrides name {name!r}, which is not in the registry {sorted(out)}')
        out[name] = dataclasses.replace(out[name], **fields)
    return out


def resolve_for_host(engines, hosts, hostname, local=None):
    """(engines, slack switch) for `hostname`: the base registry, then the host's entry, then the machine-local file."""
    import types
    layers = [types.SimpleNamespace(**hosts.get(hostname, {}))] + ([local] if local is not None else [])
    switch = SLACK_PROMETHEUS_VIA_HESTIA
    for layer in layers:
        engines = apply_local_overrides(engines, layer)
        switch = bool(getattr(layer, 'SLACK_PROMETHEUS_VIA_HESTIA', switch))
    return engines, switch


def resolve_candle_source(base, hosts, hostname, local=None):
    """`base` with the host's `CANDLE_SOURCE` dict, then the machine-local file's, merged over it key by key."""
    out = dict(base)
    for layer in (hosts.get(hostname, {}).get('CANDLE_SOURCE'), getattr(local, 'CANDLE_SOURCE', None)):
        if layer:
            out.update(layer)
    return out


try:
    import hestia_local as _local          # noqa: E402  (gitignored; absent unless someone adds a per-machine change)
except ImportError:
    _local = None
import socket as _socket  # noqa: E402
HOSTNAME = _socket.gethostname()
ENGINES, SLACK_PROMETHEUS_VIA_HESTIA = resolve_for_host(ENGINES, TRADING_HOSTS, HOSTNAME, _local)
CANDLE_SOURCE = resolve_candle_source(CANDLE_SOURCE, TRADING_HOSTS, HOSTNAME, _local)
LOCAL_OVERRIDES_PRESENT = _local is not None or HOSTNAME in TRADING_HOSTS
