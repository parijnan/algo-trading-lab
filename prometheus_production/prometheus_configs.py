"""
Prometheus production configuration.
All tuneable parameters live here — nothing hardcoded in prometheus.py /
prometheus_functions.py.

Calibrated 2026-08-27 against prometheus_backtest/phase2/configs_p2.py
(219 trades, 55.7% WR, Rs.44,693 total P&L, Calmar 2.99/5.20 annualized —
see prometheus_backtest/README.md and plans/prometheus-phase2-production.md).
This file is a deliberate DUPLICATE of configs_p2.py's calibrated values,
not an import — matches the repo's existing iris_configs.py/iris_backtest
convention exactly (CLAUDE.md "Module naming", plan §0): production must
never silently follow future backtest experimentation.

DRY_RUN = True  → log intended orders, place nothing; use feed LTP as fill price.
          False → live orders. See the DRY_RUN assignment below for the
          actual decision record of when/why this was flipped.
"""
from pathlib import Path

REPO_ROOT       = Path(__file__).parent.parent
PROMETHEUS_ROOT = Path(__file__).parent
DATA_DIR        = PROMETHEUS_ROOT / 'data'
LOG_DIR         = PROMETHEUS_ROOT / 'logs'
TRADE_LOGS_DIR  = DATA_DIR / 'trade_logs'

FLAG_PATH    = DATA_DIR / 'prometheus_active.flag'
PID_FILE     = DATA_DIR / 'prometheus.pid'
STATE_FILE   = DATA_DIR / 'prometheus_state.csv'
CREDS_FILE   = REPO_ROOT / 'data' / 'user_credentials.csv'   # shared root file (Leto/Apollo/Artemis
                                                              # convention — user_name/qr_code columns,
                                                              # already present on both local and Delos,
                                                              # not a per-strategy copy like Iris's own
TRADES_FILE  = DATA_DIR / 'prometheus_trades.csv'
COUNTER_FILE = DATA_DIR / 'trade_counter.txt'
SERIES_15M_FILE = DATA_DIR / 'prometheus_15m_series.csv'

# Private, Prometheus-only intraday cache (plan §15, 2026-09-04) — NOT the
# shared MCX contract CSV data_downloader_mcx.py maintains. Nothing else
# reads or writes this file, so it carries none of the write-race/single-
# source-of-truth concerns that motivated removing writes to the shared
# file. Written incrementally through the day, cleared at logoff, and
# always date-filtered on read (belt-and-suspenders against an ungraceful
# crash that skips teardown).
TODAY_1M_CACHE_FILE = DATA_DIR / 'prometheus_today_1m.csv'

# Prometheus's own circuit breaker — deliberately separate from
# data/SLACK_COMMAND.flag (§5): an operator managing NSE/BSE shouldn't
# accidentally also kill the MCX side, and vice versa.
COMMAND_FLAG_PATH = DATA_DIR / 'prometheus_command.flag'

# ── Shared MCX data pipeline paths ───────────────────────────────────────────
MCX_DATA_DIR            = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx'
INSTRUMENT_MASTER_FILE  = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx_instrument_master.csv'
MCX_HOLIDAYS_FILE       = REPO_ROOT / 'data_pipeline' / 'data' / 'mcx_holidays.csv'

# ── Instrument (Slack-switchable — §5/§6) ────────────────────────────────────
SYMBOL          = 'CRUDEOILM'
MARGIN_PER_UNIT = 100000   # Rs — coupled to SYMBOL; overridden together via
                           # btn_prometheus_instrument (instrument_override.json)

try:
    import json as _json
    _o = _json.loads((DATA_DIR / 'instrument_override.json').read_text())
    SYMBOL          = str(_o['symbol'])
    MARGIN_PER_UNIT = float(_o['margin_per_unit'])
except Exception:
    pass


def _lookup_lot_size(symbol: str) -> int:
    import pandas as pd
    df = pd.read_csv(INSTRUMENT_MASTER_FILE)
    rows = df[df['name'] == symbol]
    if rows.empty:
        raise ValueError(f"No instrument master rows found for '{symbol}' in {INSTRUMENT_MASTER_FILE}")
    return int(rows.iloc[0]['lotsize'])


LOT_SIZE     = _lookup_lot_size(SYMBOL)
LOTS_PER_LEG = 1     # 1 unit = 2 lots (1 lot each leg) — see prometheus_state.py / §6

FO_EXCHANGE  = 'MCX'
MCX_FO_WS_EXCHANGE_TYPE = 5   # websocket_feed.py exchange_type int for MCX F&O
                              # (matches mcx_live_downloader.py's MCX_FO=5, live-verified)

# ── Kill switch ───────────────────────────────────────────────────────────────
DRY_RUN = True     # Reverted to paper mode 2026-08-31 after a real incident: a trend-flip exit
                    # order failed at the broker (orderid=None) with no guard against it, and the
                    # code fabricated a fill from LTP -- both lots marked closed internally while
                    # the real 2-lot long stayed open and unmonitored for ~28min until caught
                    # manually. Fixed in commit 8b7bc5b (three-layer confirmation check: never
                    # mark a lot closed without a genuine fill; propagate that through rule 7's
                    # re-entry gate and _teardown()). Went live earlier that same day (STATIC_UNITS=1)
                    # specifically to verify the order-update WebSocket for MCX -- that part
                    # worked (four real fills resolved via WS all day). Only flip back to False
                    # after the fix above has held up under a fresh DRY_RUN pass.

# ── Session ───────────────────────────────────────────────────────────────────
SESSION_START_TIME = '09:00'   # cron starts ahead of this; poller/seed both key off it

# Min-entry guard — a genuine buffer duration, NOT a clock time (fixed
# 2026-09-04, same bug class/fix as NO_EXIT_BEFORE_BUFFER_MIN below).
# MIN_ENTRY_TIME used to be a hardcoded '09:15' clock time, live since
# Phase 3 first went live — silently gave ZERO minutes of thin-opening-
# liquidity protection on the evening-only special sessions (~7/153 days,
# confirmed against the user's own chart during Phase 4), where the real
# session open is 17:00: the clock is already long past 09:15 the instant
# trading starts on those days. Keyed off the ACTUAL first 1-min bar seen
# today (self._df_1m_today['time_stamp'].min()) via _past_min_entry_guard,
# not a hardcoded clock time — same dynamic-anchor principle as the 15m/1h
# resample's day boundary (`data_loader.py`'s `origin=day.index[0]`).
MIN_ENTRY_BUFFER_MIN = 15   # skip the first 15 min of the ACTUAL session — thin opening liquidity

# First-minute exit guard (plan §10, built 2026-09-04) — a genuine buffer
# duration, NOT a clock time. The plan's original proposal (`NO_EXIT_BEFORE
# = '09:01'`, a fixed clock time) would silently do nothing on the
# evening-only special sessions (~7/153 days, confirmed against the user's
# own chart during Phase 4) where the real session open is 17:00, not
# 09:00 — 09:01 is already hours in the past by the time trading starts on
# those days. Fixed the same way the 15m/1h resample's day anchor was
# fixed (`data_loader.py`'s `origin=day.index[0]`, NOT a hardcoded 09:00):
# keyed off the ACTUAL first 1-min bar seen today
# (`self._df_1m_today['time_stamp'].min()`), whatever time that turns out
# to be. See `_past_first_minute_guard` in prometheus.py.
NO_EXIT_BEFORE_BUFFER_MIN = 1

# CLOSING_TIME is DST-dependent and must be toggled by hand around the US
# DST changes (~2nd Sun of March -> 23:30, ~1st Sun of Nov -> 23:55) — see
# plan §3. Current value is correct for 2026-08-30 (DST in force).
CLOSING_TIME = '23:30'

# Plan §2, Phase 3: no EOD flatten, no entry cutoff before close — a position
# is *expected* to still be open at session end most days (§3's "a position
# can span a contract roll"), and there's nothing to "hold until" any more
# (positional, not intraday-only). MAX_ENTRY_BEFORE_CLOSE_MIN/LAST_ENTRY_TIME/
# EOD_SQUAREOFF_* (Phase 2 concepts) are deliberately gone, not renamed —
# `configs_p3.py` never gated entries near close, and production must match
# what was calibrated. Exit priority drops to SL -> lot1 target -> lot2
# target -> trend_flip only (`_check_exit_conditions_ltp`, prometheus.py).

# Poller/WS session-end buffer — tracks CLOSING_TIME plus the same generous
# buffer mcx_live_downloader.py uses (SESSION_END_TIME=23:55 vs its own
# MARKET_CLOSE=23:30, a 25-min buffer). This is process-lifecycle only (when
# the daily cron's run() loop stops), not position management — unaffected
# by the no-EOD-flatten change above.
SESSION_END_BUFFER_MIN = 25


def _minus_minutes(hhmm: str, minutes: int) -> str:
    from datetime import datetime as _dt, timedelta as _td
    t = _dt.strptime(hhmm, '%H:%M') - _td(minutes=minutes)
    return t.strftime('%H:%M')


SESSION_END_TIME = _minus_minutes(CLOSING_TIME, -SESSION_END_BUFFER_MIN)   # CLOSING_TIME + buffer

# ── Signal: ST_15, single timeframe (no regime gate — Phase 2 design) ───────
ST_PERIOD     = 10
ST_MULTIPLIER = 2.0   # Phase 3 live-test value, set 2026-09-04 -- Phase 3 was designed for
                       # 2.0/2.5 (vs. Phase 2's inherited 3.0); user chose 2.0 after confirming
                       # 3.0's live ST value matched the chart correctly first.

# ── Phase 4 preview: 1h/15m ST alignment entry filter (plan §17) ────────────
# Only take a 15m ST_15 flip if the 1-hour Supertrend already agrees with
# the flip's direction. Mechanism built 2026-09-04 and gated off by
# default -- ST_1H_PERIOD/ST_1H_MULTIPLIER below are PLACEHOLDERS, not
# calibrated (same starting values as ST_PERIOD/ST_MULTIPLIER, purely so
# the mechanism computes something sane while off). Real values and the
# toggle flip are deferred to Phase 4's own backtesting -- do not treat
# these as tuned.
ENTRY_FILTER_1H_ALIGN_ENABLED = False
ST_1H_PERIOD     = 10
ST_1H_MULTIPLIER = 3.0

# Calendar days of 1-min history tail-read for seeding (15-20 recommended,
# plan §1 — lands ~975-1,300 fifteen-min bars, same generosity band as
# Iris's SEED_DAYS=13 relative to its own ST_PERIOD).
SEED_DAYS = 18

# Known-bad sessions excluded from the daily ST_15 re-seed window (plan §14).
# Empty by default, manually populated by an operator when a bad session is
# identified — annotate each entry with the date it was added so staleness
# is easy to eyeball. Remove an entry once it falls outside SEED_DAYS of
# today (it's then a no-op, excluding a date the window was never going to
# include anyway).
ST_SEED_SKIP_DATES = [
    # '2026-02-01',  # example: MCX Union Budget special session, WTI not trading
]

# ── Opening-bar price-artifact protection (plan §11) ────────────────────────
# CRUDEOILM's very first 1-min candle of the session has shown a recurring
# thin-liquidity price-discovery artifact (confirmed 7 instances, 2026-03
# through 2026-09) that distorts ST_15's ATR for ~ST_PERIOD bars afterward.
# Fix: substitute CRUDEOILM's 09:00 print with CRUDEOIL's own (same
# underlying, confirmed reliable at that exact minute every time) when
# CRUDEOIL's true range is under OPENING_BAR_ARTIFACT_THRESHOLD of
# CRUDEOILM's. Gated off by default (2026-09-04) so the user can validate
# ST accuracy against the raw, uncorrected broker chart first — the check
# still runs and logs what it *would* have done either way.
OPENING_BAR_CORRECTION_ENABLED  = False
OPENING_BAR_ARTIFACT_THRESHOLD  = 0.5
CRUDEOIL_REFERENCE_SYMBOL       = 'CRUDEOIL'   # full-size contract, reference only —
                                                # Prometheus never trades this symbol

# ── Scale-out — 'pct' hardcoded per Rollout step 5: "backtest keeps both
# modes for comparison, production hardcodes 'pct'". Changed 2026-09-04 from
# Phase 2's mult-3.0 calibration (T1=1.0/T2=2.3 flat/SL=1.8, configs_p2.py,
# 2026-08-27) to the mult-2.0 candidate's own calibration
# (prometheus_backtest/phase3, README.md's Phase 3 table) — paired with
# ST_MULTIPLIER=2.0 above, not mixed with the old exits: mult 2.0's stop is a
# true tail-risk backstop (SL 2.2%, wider than Phase 2's), not an active
# trade manager like Phase 2's 1.8% was. ────────────────────────────────────
TARGET1_PCT      = 2.0
TARGET2_MODE     = 'flat_pct'
TARGET2_FLAT_PCT = 5.0
SL_PCT           = 2.2

# ── Contract rollover — capital efficiency over parity (plan §1/§6) ─────────
# Roll to the next contract out once fewer than this many TRADING days
# (mcx_holidays.csv, not calendar days) remain until the current front-month
# contract's expiry — avoids MCX's tender-margin window on energy contracts.
TENDER_ROLL_TRADING_DAYS = 5

# ── Sizing — "units," not "lots" (§6): 1 unit = 2 lots (1 lot each leg). ────
DYNAMIC_SIZING  = False
STATIC_UNITS    = 1
# Artemis's formula, not Apollo/Athena's dual-constraint one — Prometheus is
# the only strategy on the MCX side of the account, so availablecash already
# reflects whatever's genuinely free (no other own position to guard against).

try:
    import json as _json2
    _s = _json2.loads((DATA_DIR / 'sizing_override.json').read_text())
    DYNAMIC_SIZING = bool(_s['lot_calc'])     # same JSON key names as the other 3
    STATIC_UNITS   = int(_s['lot_count'])     # strategies' sizing_override.json (§5)
except Exception:
    pass

# ── Resilient polling (§3) — ported from mcx_live_downloader.py, itself
# ported from Iris's 2026-08-10 hardening. ──────────────────────────────────
POLL_INTERVAL_SEC        = 60
INNER_RETRY_ATTEMPTS     = 5   # raised from 3, 2026-09-04 -- live-test day saw frequent AB1021
                                # bursts; 4x 1s sleeps (~4s worst-case blocking of the main loop,
                                # incl. LTP-based SL/target checks while in_trade) vs. the old 2s
INNER_RETRY_INTERVAL_SEC = 1
CANDLE_CLOSE_BUFFER_SEC  = 0    # fire exactly at the boundary, no artificial margin

CANDLE_POLL_LIMIT = 3      # getCandleData calls/sec — broker-wide client-side cap
LTP_POLL_LIMIT    = 10     # REST ltpData calls/sec when WS feed is disconnected

# ── 15-min-boundary deferred-bar computation (plan §12) ──────────────────────
# Don't build a 15m bar the instant its boundary tick arrives if the 1-min
# poller hasn't actually delivered all 15 minutes yet (e.g. an AB1021
# stretch) -- wait up to this long, re-checking every 1-min cycle, before
# falling back to building it from whatever's on hand with a loud warning.
# 1 minute, not longer: minimizes flip-detection lag over fallback
# precision (user's call, 2026-09-03) -- SL/target monitoring is completely
# unaffected by the wait either way (_check_exit_conditions_ltp runs every
# tick regardless).
DEFERRED_BAR_CUTOFF_MIN = 1

# ── Provisional boundary computation (2026-09-04, user-directed) ────────────
# At a 15m-aligned boundary, if the REST 1-min window is already incomplete
# at that instant (checked once, at T+0 -- NOT "the boundary poll's own
# retry burst exhausted," a different and less precise condition), build a
# provisional 15m candle from SharedFeed's own tick-aggregated OHLC
# (feed.get_ohlc -- genuine WS ticks, not sampled; Apollo already reads this
# for Nifty/VIX, so it's existing shared infra, not new). Compute a
# provisional ST verdict from it (against a COPY of the real series --
# never mutates or persists it) and, only if PROVISIONAL_BOUNDARY_ENABLED
# and the provisional close clears the band by more than
# PROVISIONAL_MARGIN_PCT, act immediately (exit and/or entry, mirroring
# _handle_new_15m_bar's own real branching). Inside the margin, or with the
# toggle off, this is a pure shadow-log -- falls through to the existing
# §12 deferred-wait/cutoff path completely unchanged. Reconciled against
# the real bar once §12 eventually computes it: agreement clears silently;
# disagreement is CRITICAL + Slack + provisional gating disabled for the
# REST OF THIS SESSION (a restart re-arms it) -- no automated reversal.
# Suppressed entirely during a scheduled/in-progress rollover or an
# already-stuck Rule 7 transition.
#
# Built + unit-tested 2026-09-04 (mock-based, see the session's memory
# notes); PROVISIONAL_MARGIN_PCT below is still an UNCALIBRATED PLACEHOLDER.
# Turned ON 2026-09-04 while DRY_RUN=True specifically to stress-test it
# live under paper conditions -- deliberate departure from the "shadow-log
# first" sequencing (OPENING_BAR_CORRECTION_ENABLED/
# ENTRY_FILTER_1H_ALIGN_ENABLED's pattern), safe here only because DRY_RUN
# means every action this gates is a simulated fill, not a real order. DO
# NOT carry this True into a DRY_RUN=False flip without first reviewing how
# it actually behaved under DRY_RUN -- agreement rate, any disagreement
# escalations, whether the margin needs recalibrating.
PROVISIONAL_BOUNDARY_ENABLED = True
PROVISIONAL_MARGIN_PCT = 0.15   # % of price the provisional close must clear the provisional
                                 # Supertrend band by before acting -- PLACEHOLDER, not calibrated

# ── Order execution ──────────────────────────────────────────────────────────
ORDER_TIMEOUT_SEC = 30     # seconds to wait for order fill (WS fast path + REST fallback)
REJECTION_RETRY_ATTEMPTS  = 3     # place_order (§1): retries on an actual 'rejected' response
REJECTION_RETRY_COOLDOWN_SEC = 1
GHOST_RECOVERY_COOLDOWN_SEC  = 2  # place_order (§1): sleep before checking the order book
                                   # on DataException/NetworkException, mirrors Athena's pattern
GHOST_RECOVERY_LOOKBACK_SEC  = 60 # only trust an order-book match updated within this long

# ── Startup resilience (plan §15) ────────────────────────────────────────────
# _setup()'s seed_st15 call is single-shot by default (no retry loop) --
# aborts the whole day's trading on any failure. Wrap it in a bounded,
# blocking retry: safe specifically here because _setup() runs before the
# main loop starts (no position, no concurrent exit-check loop to starve --
# unlike every other retry in this codebase, which must stay non-blocking).
SEED_RETRY_ATTEMPTS = 5
SEED_RETRY_INTERVAL_SEC = 120   # 2 min apart, ~10 min total before giving up

# ── Contract rollover (plan §4/§5/§6/§7/§8/§9, 2026-09-04) ──────────────────
# ROLLOVER_TIME: the evening cutoff where a confirmed roll actually executes
# -- derived the same way EOD_SQUAREOFF_TIME was in Phase 2 (CLOSING_TIME
# minus a buffer), same DST-hand-toggle caveat carried over. Under the
# current CLOSING_TIME=23:30 this computes to 23:15, matching the plan.
ROLLOVER_BEFORE_CLOSE_MIN = 15
ROLLOVER_TIME = _minus_minutes(CLOSING_TIME, ROLLOVER_BEFORE_CLOSE_MIN)

# ROLLOVER_PREFETCH_TIME: 5 min before ROLLOVER_TIME -- bulk-fetch the new
# contract's today series and subscribe its WS feed ahead of the decision
# point (§6 step 2), so both are warm by the time ROLLOVER_TIME needs them.
ROLLOVER_PREFETCH_BUFFER_MIN = 5
ROLLOVER_PREFETCH_TIME = _minus_minutes(ROLLOVER_TIME, ROLLOVER_PREFETCH_BUFFER_MIN)

# §7: debounce on the re-alert for a stuck _pending_flip -- reuses the
# stale-tick-watchdog's existing 5-min convention rather than a new cadence,
# so a genuinely stuck flip doesn't get silently retried with no further
# visibility, but also doesn't spam Slack every tick.
PENDING_FLIP_REALERT_DEBOUNCE_SEC = 300

# ── Slack ─────────────────────────────────────────────────────────────────────
TRADE_UPDATE_SEC = 20      # matches Artemis's/Athena's convention, not Iris's 10s (§5)

# ── 15-min debug series retention (§4) — non-authoritative, purely for human
# visibility; never read back as an input to live ST computation. ───────────
SERIES_15M_RETENTION_DAYS = 20
