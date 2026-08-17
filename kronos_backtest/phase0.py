"""
phase0.py — Kronos Phase 0 validation

Phase 0 answers one question: is the plumbing right before any P&L number is
believed? It checks the things that fail silently (§6 of the plan):

  1. Holidays load as datetime.date and the calendar helpers actually skip them.
  2. Monthly contracts are identified by calendar rule, and data coverage is
     what the plan claims.
  3. Both E2 readings satisfy the invariants Pari's rule encodes, for every
     expiry weekday in the sample including holiday-shifted ones.
  4. Entry is actually fillable: at each candidate entry DTE, do tradeable
     short and wing strikes exist?

Check 4 is a measurement, not a pass/fail — its output is the entry-DTE
feasibility curve, and it lives here rather than in a scratch script because
the figures it replaces were wrong precisely for having lived in one.

Nothing here simulates a trade. A failed check is a finding, not a crash.
"""

import os
import json
import logging
from datetime import date, timedelta

import pandas as pd

from configs import (
    ENTRY_TIME, ENTRY_DTE_TARGET, ENTRY_DTE_MIN,
    SHORT_DELTA_TARGET, WING_DELTA_TARGET, SWEEP_ENTRY_DTE, MIN_FEASIBLE_FRACTION,
    EXPECTED_MONTHLY_COUNT, MIN_LEAD_DAYS_EXPECTED, KNOWN_SHORT_COVERAGE,
    FEASIBILITY_DTE_GRID, FEASIBILITY_STALENESS_GRID, FEASIBILITY_WING_TARGETS,
    MAX_PRICE_STALENESS_MINUTES, E2_TRADING_DAYS_BEFORE,
    PHASE0_REPORT_FILE, OUTPUT_DIR,
)
import loader
import expiry_rules as er
from greeks import scan_chain, atm_strike

logger = logging.getLogger(__name__)

FEASIBILITY_FILE = os.path.join(OUTPUT_DIR, "phase0_feasibility.csv")


class Report:
    """Collects pass/fail lines so one run reports everything, not just the first fault."""

    def __init__(self):
        self.checks = []

    def add(self, ok: bool, label: str, detail: str = ''):
        self.checks.append((bool(ok), label, detail))
        logger.info(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" — {detail}" if detail else ''))
        return ok

    @property
    def failures(self):
        return [c for c in self.checks if not c[0]]


# ---------------------------------------------------------------------------
# 1. Holidays
# ---------------------------------------------------------------------------

def check_holidays(rep: Report, holidays: set) -> None:
    logger.info("1. Holiday calendar")

    rep.add(all(isinstance(d, date) for d in holidays),
            "holidays are datetime.date", f"{len(holidays)} entries")

    # The membership test must actually fire, or every calendar rule silently
    # degrades to weekday arithmetic.
    midweek = sorted(d for d in holidays if d.weekday() < 5)
    if not midweek:
        rep.add(False, "no mid-week holiday found to test against")
        return
    h = midweek[len(midweek) // 2]
    rep.add(not er.is_trading_day(h, holidays),
            "a mid-week holiday is not a trading day", str(h))
    prev = er.previous_trading_day(h + timedelta(days=1), holidays)
    rep.add(prev is not None and prev < h,
            "previous_trading_day steps over a mid-week holiday", f"{h} -> {prev}")


# ---------------------------------------------------------------------------
# 2. Monthly universe
# ---------------------------------------------------------------------------

def check_universe(rep: Report, universe: pd.DataFrame, warnings: list) -> None:
    logger.info("2. Monthly universe")

    excluded_future = [w for w in warnings if 'download_status False' in w]
    for w in warnings:
        if w not in excluded_future:
            logger.info(f"       note: {w}")
    if excluded_future:
        logger.info(f"       note: {len(excluded_future)} not-yet-downloaded future "
                    f"expiries excluded (download_status False)")

    n = len(universe)
    rep.add(n == EXPECTED_MONTHLY_COUNT,
            f"monthly count matches plan §3 ({EXPECTED_MONTHLY_COUNT})", f"found {n}")

    months = [(d.year, d.month) for d in universe['expiry_date']]
    rep.add(len(months) == len(set(months)), "one monthly per calendar month")

    by_month = {}
    for d in loader.load_contract_list()['expiry_date']:
        by_month.setdefault((d.year, d.month), []).append(d)
    mismatched = [d for d in universe['expiry_date'] if d != max(by_month[(d.year, d.month)])]
    rep.add(not mismatched, "every monthly is the last expiry of its month",
            f"{len(mismatched)} mismatched" if mismatched else '')

    logger.info("     Expiry weekday distribution:")
    for wd, cnt in universe['expiry_weekday'].value_counts().items():
        logger.info(f"       {wd:<10} {cnt:>3}")

    # Data coverage. lead_days is the earliest print anywhere in the contract's
    # strike files — a coverage measure, never an identity test and never a
    # substitute for the feasibility scan in check 4.
    short = universe[universe['lead_days'].fillna(0) < MIN_LEAD_DAYS_EXPECTED]
    found = {r.expiry_date.strftime('%Y-%m-%d'): int(r.lead_days) for r in short.itertuples()}
    rep.add(found == KNOWN_SHORT_COVERAGE,
            f"coverage tripwire: contracts under {MIN_LEAD_DAYS_EXPECTED} days are the "
            f"known set (not an entry constraint — entry is at {ENTRY_DTE_TARGET} DTE)",
            f"found {found or '{}'}" +
            ('' if found == KNOWN_SHORT_COVERAGE else f", expected {KNOWN_SHORT_COVERAGE}"))
    for expiry_s, lead in sorted(found.items()):
        enterable = 'enters normally' if lead >= ENTRY_DTE_TARGET else 'BELOW the entry DTE'
        logger.info(f"       {expiry_s}: {lead} days of coverage — cannot enter above "
                    f"{lead} DTE ({enterable})")
    logger.info(f"     Lead time (days of coverage before expiry): "
                f"min {int(universe['lead_days'].min())}, "
                f"median {int(universe['lead_days'].median())}, "
                f"max {int(universe['lead_days'].max())}")

    covid = universe[universe['expiry_date'].apply(lambda d: d.year == 2020)]
    logger.info(f"     2020 contracts in the universe: {len(covid)} "
                f"(lead {int(covid['lead_days'].min())}-{int(covid['lead_days'].max())} days) "
                f"— the plan's '2020 hole' was a measurement artifact")


# ---------------------------------------------------------------------------
# 3. Entry and exit dates
# ---------------------------------------------------------------------------

def _holds_final_weekend(exit_d: date, expiry: date) -> bool:
    """True if the position is still open across the weekend before expiry."""
    if exit_d is None:
        return None
    return exit_d > er.preceding_saturday(expiry)


def _trading_days_between(a: date, b: date, holidays: set) -> int:
    """Number of trading days strictly after a, up to and including b."""
    n, d = 0, a
    while d < b:
        d += timedelta(days=1)
        if er.is_trading_day(d, holidays):
            n += 1
    return n


def build_calendar_table(universe: pd.DataFrame, holidays: set) -> pd.DataFrame:
    rows = []
    for expiry in universe['expiry_date']:
        entry = er.entry_date_for(expiry, holidays, ENTRY_DTE_TARGET, ENTRY_DTE_MIN)
        e1  = er.exit_date_e1(expiry, holidays)
        e2a = er.exit_date_e2a(expiry, holidays)
        e2b = er.exit_date_e2b(expiry, holidays)
        e3  = er.exit_date_e3(expiry, holidays)
        # A window is holiday-free when no exchange holiday falls on a weekday
        # between the exit and expiry — the only case where the plain weekday
        # answer in plan §4's table is the right one.
        window = [expiry - timedelta(days=i) for i in range(1, (expiry - e2b).days + 1)]
        holiday_free = not any(d.weekday() < 5 and d in holidays for d in window)
        rows.append({
            'expiry_date':    expiry,
            'expiry_weekday': expiry.strftime('%a'),
            'entry_date':     entry,
            'entry_weekday':  entry.strftime('%a') if entry else '',
            'entry_dte':      (expiry - entry).days if entry else None,
            'e1_exit':  e1,  'e1_weekday':  e1.strftime('%a'),
            'e2a_exit': e2a, 'e2a_weekday': e2a.strftime('%a'),
            'e2b_exit': e2b, 'e2b_weekday': e2b.strftime('%a'),
            'e3_exit':  e3,  'e3_weekday':  e3.strftime('%a'),
            'e2a_trading_days_to_expiry': _trading_days_between(e2a, expiry, holidays),
            'e2b_trading_days_to_expiry': _trading_days_between(e2b, expiry, holidays),
            'e2a_holds_final_weekend': _holds_final_weekend(e2a, expiry),
            'e2b_holds_final_weekend': _holds_final_weekend(e2b, expiry),
            'e2a_vs_e2b_differ': e2a != e2b,
            'holiday_free_window': holiday_free,
        })
    return pd.DataFrame(rows)


def check_calendar(rep: Report, cal: pd.DataFrame, holidays: set) -> None:
    logger.info("3. Entry and exit date rules")

    rep.add(cal['entry_date'].notna().all(),
            f"every contract resolves an entry date at {ENTRY_DTE_TARGET} DTE",
            f"{int(cal['entry_date'].isna().sum())} missing")
    rep.add(bool(cal['entry_dte'].dropna().between(ENTRY_DTE_MIN, ENTRY_DTE_TARGET).all()),
            f"entry DTE within [{ENTRY_DTE_MIN}, {ENTRY_DTE_TARGET}]",
            f"observed {int(cal['entry_dte'].min())}-{int(cal['entry_dte'].max())}")

    for col in ('entry_date', 'e1_exit', 'e2a_exit', 'e2b_exit', 'e3_exit'):
        bad = [d for d in cal[col] if d is not None and not er.is_trading_day(d, holidays)]
        rep.add(not bad, f"{col} is always a trading day",
                f"{len(bad)} violations: {bad[:3]}" if bad else '')

    for col in ('e1_exit', 'e2a_exit', 'e2b_exit'):
        rep.add(cal[cal[col] >= cal['expiry_date']].empty,
                f"{col} is strictly before expiry")

    rep.add(bool((cal['e1_exit'] <= cal['e2b_exit']).all()
                 and (cal['e2b_exit'] <= cal['e2a_exit']).all()
                 and (cal['e2a_exit'] < cal['e3_exit']).all()),
            "exit dates ordered E1 <= E2b <= E2a < E3")

    rep.add(cal.loc[cal['entry_date'].notna(), 'entry_date'].lt(
                cal.loc[cal['entry_date'].notna(), 'e1_exit']).all(),
            "entry precedes the earliest exit for every contract")

    # The two invariants Pari's E2 rule actually encodes. Asserting weekdays
    # instead would be weekday arithmetic by the back door — the very thing the
    # holiday calendar is here to avoid.
    rep.add(not cal['e2b_holds_final_weekend'].any(),
            "E2b is flat over the final pre-expiry weekend",
            f"{int(cal['e2b_holds_final_weekend'].sum())} violations")
    rep.add(bool((cal['e2b_trading_days_to_expiry'] >= E2_TRADING_DAYS_BEFORE).all()),
            f"E2b is at least {E2_TRADING_DAYS_BEFORE} trading days before expiry "
            f"(never open into ELM on T-1)",
            f"min {int(cal['e2b_trading_days_to_expiry'].min())}")
    rep.add(bool((cal['e2a_trading_days_to_expiry'] == E2_TRADING_DAYS_BEFORE).all()),
            f"E2a is exactly {E2_TRADING_DAYS_BEFORE} trading days before expiry")

    # Plan §4's weekday table holds only where no holiday intrudes; the shifted
    # cases are the point of resolving against the calendar, so list them.
    expected_e2b = {'Mon': 'Thu', 'Tue': 'Fri', 'Wed': 'Fri', 'Thu': 'Fri', 'Fri': 'Fri'}
    clean = cal[cal['holiday_free_window']]
    bad = clean[clean.apply(
        lambda r: r['e2b_weekday'] != expected_e2b[r['expiry_weekday']], axis=1)]
    rep.add(bad.empty,
            "E2b matches plan §4's weekday table on every holiday-free window",
            f"{len(clean)} clean contracts" if bad.empty else
            f"{len(bad)} mismatches: "
            + ', '.join(f"{r.expiry_date}({r.expiry_weekday})->{r.e2b_weekday}"
                        for r in bad.head(5).itertuples()))

    logger.info("     E2a vs E2b by expiry weekday:")
    for wd, grp in cal.groupby('expiry_weekday', sort=False):
        logger.info(f"       {wd} expiry ({len(grp):>2}): "
                    f"E2a -> {grp['e2a_weekday'].value_counts().to_dict()}, "
                    f"E2b -> {grp['e2b_weekday'].value_counts().to_dict()}, "
                    f"differ on {int(grp['e2a_vs_e2b_differ'].sum())}")

    differ = int(cal['e2a_vs_e2b_differ'].sum())
    logger.info(f"     E2a and E2b disagree on {differ} / {len(cal)} contracts "
                f"({differ / len(cal):.0%}) — the sub-arm split Phase 4 measures")

    shifted = cal[~cal['holiday_free_window']]
    logger.info(f"     Holiday-shifted windows (plain weekday arithmetic would be wrong): "
                f"{len(shifted)}")
    for r in shifted.itertuples():
        logger.info(f"       {r.expiry_date} ({r.expiry_weekday}): "
                    f"E2a {r.e2a_exit} ({r.e2a_weekday}), E2b {r.e2b_exit} ({r.e2b_weekday})")


# ---------------------------------------------------------------------------
# 4. Entry feasibility — can the structure actually be filled?
# ---------------------------------------------------------------------------

def _min_delta(chain: list, max_age) -> tuple:
    """Lowest delta reachable using only prints no older than max_age."""
    fresh = [c for c in chain
             if c['delta'] is not None and (max_age is None or c['age_min'] <= max_age)]
    if not fresh:
        return None, None
    best = min(fresh, key=lambda c: c['delta'])
    return best['delta'], best['strike']


def measure_feasibility(universe: pd.DataFrame, holidays: set,
                        nifty_1m: pd.DataFrame, vix_1m: pd.DataFrame,
                        refresh: bool = False) -> pd.DataFrame:
    """
    For each entry DTE on the grid and each contract, walk both chains at the
    entry timestamp and record the lowest delta reachable at several staleness
    bounds. Cached — the scan reads several GB of option files.
    """
    if os.path.exists(FEASIBILITY_FILE) and not refresh:
        logger.info(f"     (cached: {FEASIBILITY_FILE} — delete or pass --refresh to re-scan)")
        return pd.read_csv(FEASIBILITY_FILE)

    rows = []
    for dte in FEASIBILITY_DTE_GRID:
        logger.info(f"     scanning at {dte} DTE...")
        for expiry in universe['expiry_date']:
            entry = er.entry_date_for(expiry, holidays, dte, dte - 5)
            if entry is None:
                rows.append({'entry_dte_target': dte, 'expiry_date': expiry,
                             'entry_date': None, 'spot': None})
                continue
            ts   = pd.Timestamp(f"{entry} {ENTRY_TIME}:00")
            spot = loader.get_1min_value(nifty_1m, ts, 'close')
            if spot is None:
                rows.append({'entry_dte_target': dte, 'expiry_date': expiry,
                             'entry_date': entry, 'spot': None})
                continue

            cache = {}
            rec = {
                'entry_dte_target': dte, 'expiry_date': expiry, 'entry_date': entry,
                'entry_dte_actual': (expiry - entry).days,
                'spot': round(spot, 2),
                'vix': loader.get_1min_value(vix_1m, ts, 'close'),
                'atm': atm_strike(spot),
            }
            for side in ('ce', 'pe'):
                chain = scan_chain(spot, expiry, ts, side, cache, max_staleness=None)
                rec[f'{side}_priced_strikes'] = len(chain)
                for stale in FEASIBILITY_STALENESS_GRID:
                    tag = 'inf' if stale is None else int(stale)
                    d, k = _min_delta(chain, stale)
                    rec[f'{side}_min_delta_s{tag}'] = d
                    rec[f'{side}_min_strike_s{tag}'] = k
            rows.append(rec)

    df = pd.DataFrame(rows)
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    df.to_csv(FEASIBILITY_FILE, index=False)
    return df


def check_feasibility(rep: Report, feas: pd.DataFrame, n_contracts: int) -> None:
    logger.info("4. Entry feasibility — is the structure fillable at entry?")
    logger.info(f"     A strike counts as fillable only if it has a print no older than the")
    logger.info(f"     staleness bound. These files are trade-derived, so an unbounded")
    logger.info(f"     fallback can return a price from days earlier — untradeable, and the")
    logger.info(f"     IV backed out of it is meaningless.")

    stale_tag = int(MAX_PRICE_STALENESS_MINUTES)
    ce, pe = f'ce_min_delta_s{stale_tag}', f'pe_min_delta_s{stale_tag}'

    logger.info(f"     Both-sides availability at the configured "
                f"{MAX_PRICE_STALENESS_MINUTES:.0f}-minute bound:")
    header = "       DTE  " + "  ".join(f"<={t:<6.3f}" for t in
                                        [SHORT_DELTA_TARGET] + FEASIBILITY_WING_TARGETS)
    logger.info(header)
    for dte, grp in feas.groupby('entry_dte_target'):
        ok = grp[grp['spot'].notna()]
        cells = []
        for tgt in [SHORT_DELTA_TARGET] + FEASIBILITY_WING_TARGETS:
            both = int(((ok[ce] <= tgt) & (ok[pe] <= tgt)).sum())
            cells.append(f"{both:>3}/{len(ok):<3}  ")
        logger.info(f"       {dte:>3}  " + "".join(cells))

    logger.info(f"     Sensitivity to the staleness bound "
                f"(both sides, wing target {FEASIBILITY_WING_TARGETS[0]}):")
    for stale in FEASIBILITY_STALENESS_GRID:
        tag = 'inf' if stale is None else int(stale)
        c, p = f'ce_min_delta_s{tag}', f'pe_min_delta_s{tag}'
        if c not in feas.columns:
            continue
        cells = []
        for dte, grp in feas.groupby('entry_dte_target'):
            ok = grp[grp['spot'].notna()]
            both = int(((ok[c] <= FEASIBILITY_WING_TARGETS[0])
                        & (ok[p] <= FEASIBILITY_WING_TARGETS[0])).sum())
            cells.append(f"{dte}DTE {both:>2}/{len(ok):<2}")
        label = 'unbounded' if stale is None else f"{int(stale)} min"
        logger.info(f"       {label:>10}: " + "  ".join(cells))

    ref_ok = feas[(feas['entry_dte_target'] == ENTRY_DTE_TARGET) & feas['spot'].notna()]
    fillable = ((ref_ok[ce] <= WING_DELTA_TARGET) & (ref_ok[pe] <= WING_DELTA_TARGET)
                & (ref_ok[ce] <= SHORT_DELTA_TARGET) & (ref_ok[pe] <= SHORT_DELTA_TARGET))
    share = fillable.mean() if len(ref_ok) else 0.0
    rep.add(share >= MIN_FEASIBLE_FRACTION,
            f"at the configured {ENTRY_DTE_TARGET} DTE / {SHORT_DELTA_TARGET} short / "
            f"{WING_DELTA_TARGET} wing, at least {MIN_FEASIBLE_FRACTION:.0%} of contracts "
            f"are fillable", f"{int(fillable.sum())}/{len(ref_ok)} = {share:.0%}")

    # Which side fails, and in which years — a regime-clustered failure would
    # bias the sample even at an acceptable overall rate.
    miss = ref_ok[~fillable]
    if len(miss):
        years = pd.to_datetime(miss['expiry_date']).dt.year.value_counts().sort_index()
        logger.info(f"     Not fillable at the configured settings: {len(miss)} contracts, "
                    f"by year {years.to_dict()}")
        logger.info(f"       (CE side short {int((ref_ok[ce] > WING_DELTA_TARGET).sum())}, "
                    f"PE side short {int((ref_ok[pe] > WING_DELTA_TARGET).sum())})")

    sets = []
    for dte in SWEEP_ENTRY_DTE:
        g = feas[(feas['entry_dte_target'] == dte) & feas['spot'].notna()]
        sets.append(set(g[(g[ce] <= WING_DELTA_TARGET) & (g[pe] <= WING_DELTA_TARGET)
                          & (g[ce] <= SHORT_DELTA_TARGET)
                          & (g[pe] <= SHORT_DELTA_TARGET)]['expiry_date']))
    fixed = set.intersection(*sets) if sets else set()
    logger.info(f"     Phase 2 fixed contract set (fillable at every DTE in "
                f"{SWEEP_ENTRY_DTE}): {len(fixed)}/{n_contracts}")
    rep.add(len(fixed) >= 0.6 * n_contracts,
            "Phase 2 fixed contract set retains at least 60% of the universe",
            f"{len(fixed)}/{n_contracts}")
    logger.info(f"     Feasibility detail written: {FEASIBILITY_FILE}")


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def run(refresh: bool = False) -> int:
    """Run every Phase 0 check. Returns the number of failures."""
    os.makedirs(OUTPUT_DIR, exist_ok=True)
    rep = Report()

    logger.info("Loading...")
    holidays = loader.load_holidays()
    nifty_1m, vix_1m = loader.load_index_data()
    universe, warnings = loader.load_monthly_universe(holidays)
    logger.info(f"  Monthly universe: {len(universe)} contracts "
                f"({universe['expiry_date'].min()} -> {universe['expiry_date'].max()})")
    logger.info("")

    check_holidays(rep, holidays)
    logger.info("")
    check_universe(rep, universe, warnings)
    logger.info("")

    cal = build_calendar_table(universe, holidays)
    check_calendar(rep, cal, holidays)
    cal.to_csv(PHASE0_REPORT_FILE, index=False)
    logger.info(f"     Calendar table written: {PHASE0_REPORT_FILE}")
    logger.info("")

    feas = measure_feasibility(universe, holidays, nifty_1m, vix_1m, refresh=refresh)
    check_feasibility(rep, feas, len(universe))
    logger.info("")

    failures = rep.failures
    logger.info(f"Phase 0: {len(rep.checks) - len(failures)} passed, {len(failures)} failed")
    for _, label, detail in failures:
        logger.info(f"  FAILED: {label}" + (f" — {detail}" if detail else ''))
    return len(failures)
