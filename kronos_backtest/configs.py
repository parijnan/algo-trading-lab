"""
configs.py — Kronos Backtest Configuration

Kronos: monthly Nifty short-premium, expiry-avoidant.
Design: plans/kronos-monthly-premium.md

Structure (§5 of the plan):
  Sell ~0.15 delta CE and PE on the Nifty MONTHLY expiry, buy ~0.075 delta wings
  on the same expiry (defined-risk iron condor). Enter at DTE ~35, 10:30.
  Exit per EXIT_POLICY — the whole point of the strategy is which slice of the
  decay curve it refuses to hold.

Every parameter lives here. Repo rule: no magic numbers in the backtest scripts,
one variable changed per experiment.
"""

import os

# ---------------------------------------------------------------------------
# Paths
# ---------------------------------------------------------------------------
REPO_ROOT           = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PIPELINE_DATA       = os.path.join(REPO_ROOT, "data_pipeline", "data")
PIPELINE_CFG        = os.path.join(REPO_ROOT, "data_pipeline", "config")

NIFTY_INDEX_FILE    = os.path.join(PIPELINE_DATA, "indices", "nifty.csv")
VIX_INDEX_FILE      = os.path.join(PIPELINE_DATA, "indices", "india_vix.csv")
NIFTY_OPTIONS_PATH  = os.path.join(PIPELINE_DATA, "nifty", "options")
CONTRACT_LIST_FILE  = os.path.join(PIPELINE_CFG, "options_list_nf.csv")
HOLIDAYS_FILE       = os.path.join(PIPELINE_CFG, "holidays.csv")

OUTPUT_DIR          = os.path.join(os.path.dirname(__file__), "data")
PHASE0_REPORT_FILE  = os.path.join(OUTPUT_DIR, "phase0_calendar.csv")
TRADE_LOGS_DIR      = os.path.join(OUTPUT_DIR, "trade_logs")
TRADE_SUMMARY_FILE  = os.path.join(OUTPUT_DIR, "trade_summary.csv")

# ---------------------------------------------------------------------------
# Universe
# ---------------------------------------------------------------------------
# Nifty only. Settled by data, not preference (§3): Sensex monthly contracts
# carry 14-27 days of history before expiry — less than Kronos's entry DTE — and
# Sensex option files have no open_interest column.
BACKTEST_START_DATE     = None          # None = full available history
BACKTEST_END_DATE       = None

# A contract enters the universe only if all three hold: a row exists in
# CONTRACT_LIST_FILE, its download_status is True, and its on-disk expiry
# directory contains option files. See loader.load_monthly_universe().
REQUIRE_DOWNLOAD_STATUS = True

# The trailing calendar month is dropped when it may still gain a later expiry
# — otherwise a mid-month weekly would be misidentified as that month's monthly.
# A month is treated as complete when its last expiry + this many days lands in
# the following month (i.e. no further weekly can fall inside the month).
MONTH_COMPLETE_LOOKAHEAD_DAYS = 7

# An interior month whose last expiry is followed by a gap longer than this is
# flagged as a possible data hole. Never dropped — flagged. A month's last
# expiry landing a few days shy of month end is normal (holiday-shifted
# expiries), so the test is gap-to-next-expiry, not distance-to-month-end.
MONTH_GAP_WARN_DAYS     = 14

# ---------------------------------------------------------------------------
# Entry
# ---------------------------------------------------------------------------
ENTRY_TIME              = '10:30'       # Matches Athena's convention; avoids the open's noise
ENTRY_DTE_TARGET        = 35            # Enter on the first trading day with DTE <= this...
ENTRY_DTE_MIN           = 30            # ...but never below this, else the contract is skipped
# 35, not the plan's original 45, on Phase 0 evidence: the Nifty monthly chain is
# only quoted within a band around spot, and at 45 DTE the band is too narrow to
# place a wing on both sides for a third of the sample. See §3 of the plan.
STRIKE_STEP             = 100           # Nifty strike interval

# No VIX gate (Decision C). VIX is recorded per trade as instrumentation only —
# nothing in the entry or exit path may read it.
RECORD_ENTRY_VIX        = True

# ---------------------------------------------------------------------------
# Strike selection — target delta, computed with mibian
# ---------------------------------------------------------------------------
SHORT_DELTA_TARGET      = 0.15          # Sold CE and PE (Phase 3 sweep)
WING_DELTA_TARGET       = 0.075         # Long CE and PE wings (Phase 3 sweep)
# 0.075, not the plan's original 0.05, for the same reason as the entry DTE: at
# 35 DTE a 0.05 wing is placeable on both sides for 68/87 contracts, a 0.075
# wing for 82/87. The binding constraint is the outer leg only — moving the
# SHORT closer to ATM widens the spread without costing any availability, which
# is the Phase 3 lever if the 0.15/0.075 band turns out too thin on credit.

# Strike scan width, as a fraction of spot rather than a flat point count —
# spot ranges ~7,500-26,000 across the sample, so a fixed width is not
# equivalent at both ends.
STRIKE_SCAN_WIDTH_PCT   = 0.25
MIN_OPTION_PRICE        = 0.5           # Ignore strikes priced at or below this when scanning

# The option files are trade-derived: a minute with no trade produces no bar, so
# a price lookup can fall back to a print from days earlier. That price is not
# tradeable and the IV backed out of it is meaningless. Any fallback older than
# this is treated as no price at all. Phase 0 measures how much the answer moves
# with this bound — see FEASIBILITY_STALENESS_GRID.
MAX_PRICE_STALENESS_MINUTES = 30.0

# Stop scanning outward after this many consecutive strikes with no usable price.
# Beyond a gap this wide the chain is genuinely unquoted, not merely sparse.
STRIKE_SCAN_MAX_GAP     = 6

# ---------------------------------------------------------------------------
# Exit — policy (Decision A, §4). Phase 4 compares these head-to-head.
# ---------------------------------------------------------------------------
#   'E1'  DTE exit          — close when DTE <= E1_EXIT_DTE
#   'E2'  pre-expiry-week   — close before ELM and before the final weekend
#   'E3'  hold to expiry    — force close at E3_FORCE_CLOSE_TIME on expiry day
EXIT_POLICY             = 'E2'

E1_EXIT_DTE             = 21            # E1 only (sub-swept in Phase 4)

# E2 has two defensible readings of Pari's rule; both are tested as sub-arms.
#   'trading_days'         (E2a) — exactly E2_TRADING_DAYS_BEFORE trading days before expiry.
#   'avoid_final_weekend'  (E2b) — the earlier of (last trading day before the
#                                  weekend immediately preceding expiry) and E2a.
#                                  The E2a term is what makes a Monday expiry
#                                  exit on Thursday rather than Friday: it keeps
#                                  the position clear of ELM on T-1 as well as
#                                  clear of the weekend.
# They coincide for every Tuesday and Monday expiry and diverge for Thursday and
# Wednesday ones — 75/87 contracts. E2b is the Phases 1-3 baseline.
EXIT_OFFSET_MODE        = 'avoid_final_weekend'
E2_TRADING_DAYS_BEFORE  = 2

E3_FORCE_CLOSE_TIME     = '15:15'       # E3 only — Artemis's expiry-day pattern
EXIT_TIME               = '15:15'       # Time of day for E1/E2 time exits

# ---------------------------------------------------------------------------
# Exit — management triggers
# ---------------------------------------------------------------------------
ENABLE_PROFIT_TARGET    = True
PROFIT_TARGET_PCT_CREDIT = 0.50         # Close at 50% of credit received (Phase 5 sweep)

ENABLE_LOSS_EXIT        = True
LOSS_MULTIPLE_CREDIT    = 2.0           # Close when open loss reaches 2x credit (Phase 6 sweep)

# Phase 1b: does managing on the daily close lose materially to intraday?
#   'intraday_1min' — evaluate triggers on every 1-min bar
#   'daily_close'   — evaluate once a day at DAILY_CHECK_TIME
MANAGEMENT_CADENCE      = 'intraday_1min'
DAILY_CHECK_TIME        = '15:15'

# ---------------------------------------------------------------------------
# CAS (Closing Auction Session) — hard rules, every exit policy
# Live from 2026-08-03. No CAS-era monthly expiry exists in the data yet, so
# these constrain the live design, not the backtest sample. They are encoded
# here so the backtest and any later production build share one definition.
# ---------------------------------------------------------------------------
CAS_GO_LIVE_DATE        = '2026-08-03'
CAS_BLACKOUT_START      = '15:16'       # Never mark, value or exit off option LTP...
CAS_BLACKOUT_END        = '15:29'       # ...inside this window (§5.3 of the feeder)

# ---------------------------------------------------------------------------
# Costs and sizing
# ---------------------------------------------------------------------------
SLIPPAGE_POINTS         = 1.0           # Per leg, flat. Phase 4 revisits this: monthly OTM
                                        # strikes are thinner than weekly ATM, and with four
                                        # legs the assumption can flip the sign of the result.
LOT_SIZE                = 65            # Current Nifty lot size, applied flat across 2019-2026.
                                        # A deliberate fixed-notional choice, not historical
                                        # truth — the lot size changed over the sample.
RISK_FREE_RATE          = 5.0           # Annualised (%), for mibian

# ---------------------------------------------------------------------------
# Phase sweeps — the value each phase varies. Nothing else changes per run.
# ---------------------------------------------------------------------------
SWEEP_ENTRY_DTE         = [30, 35, 40, 45]                  # Phase 2 (fixed contract set)
# 50 and 60 DTE are excluded on feasibility, not preference: at a 0.075 wing they
# support 57/87 and 43/87 contracts. Including them would drop the Phase 2 fixed
# contract set from 60 to 29 — the sweep would answer a question about how deep
# the chain is quoted rather than about the decay curve.
SWEEP_SHORT_DELTA       = [0.10, 0.125, 0.15, 0.175, 0.20]  # Phase 3
SWEEP_EXIT_POLICY       = ['E1', 'E2a', 'E2b', 'E3']        # Phase 4
SWEEP_E1_EXIT_DTE       = [14, 17, 21, 25]                  # Phase 4 sub-sweep
SWEEP_PROFIT_TARGET     = [0.30, 0.40, 0.50, 0.60, 0.75]    # Phase 5
SWEEP_LOSS_MULTIPLE     = [1.5, 2.0, 2.5, 3.0]              # Phase 6

# Phase 2 must run over the intersection of contracts fillable at every DTE in
# SWEEP_ENTRY_DTE — 60 of 87 at the configured deltas. Comparing 45 DTE on 66
# contracts against 30 DTE on 82 is a sample-composition difference, not a
# controlled test (§3). The full-universe result is reported alongside.
PHASE2_FIXED_CONTRACT_SET = True

# ---------------------------------------------------------------------------
# Phase 0 — expectations and measurement grids
#
# The monthly count is asserted against plan §3. The lead-time figures that
# once sat here (74 usable at 45 DTE, six 2020 contracts unusable) were a
# measurement artifact — the original audit took the LATEST first-print across
# a contract's strike files rather than the earliest, so a strike created
# during the COVID crash made 2020-03-26 look like it had no history. Every
# monthly in fact carries 60+ days of coverage. Phase 0 now measures what
# actually constrains entry: whether tradeable strikes exist at entry time.
# ---------------------------------------------------------------------------
EXPECTED_MONTHLY_COUNT  = 87
# A data-drift tripwire, not an entry constraint. Entry is governed by
# ENTRY_DTE_TARGET; 60 is simply well clear of it, so anything falling below
# means the pipeline's coverage changed and the sample should be re-examined.
MIN_LEAD_DAYS_EXPECTED  = 60

# ...except these, which genuinely have less. A regression guard, not a filter:
# if the data changes, Phase 0 says so instead of silently shrinking the sample.
KNOWN_SHORT_COVERAGE    = {'2025-04-30': 40}

# Feasibility scan: at each entry DTE, can a short and a wing actually be filled?
FEASIBILITY_DTE_GRID        = [30, 35, 40, 45, 50, 60]
FEASIBILITY_STALENESS_GRID  = [5.0, 30.0, 120.0, 1440.0, None]   # None = unbounded
FEASIBILITY_WING_TARGETS    = [0.075, 0.05, 0.10]   # first entry is the configured target

# Contracts where the chain cannot supply both legs simply produce no trade.
# That is acceptable if it stays rare and regime-neutral; Phase 0 fails if the
# fillable share drops below this.
MIN_FEASIBLE_FRACTION       = 0.90
