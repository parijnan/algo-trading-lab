"""
expiry_rules.py — Kronos calendar logic

Two things live here, both of which fail silently if they are wrong:

  1. Identifying monthly expiries. By CALENDAR RULE — the last expiry of each
     calendar month — never by a lead-time threshold. A lead-time filter would
     discard ~30 genuine monthlies that merely have short data history,
     including most of 2020 (§3 of the plan).

  2. Resolving each exit policy to a date. Against the holiday calendar, never
     by weekday arithmetic — mid-week holidays would be mishandled silently.
"""

from datetime import date, timedelta

from configs import (
    MONTH_COMPLETE_LOOKAHEAD_DAYS, MONTH_GAP_WARN_DAYS,
    E1_EXIT_DTE, E2_TRADING_DAYS_BEFORE,
)


# ---------------------------------------------------------------------------
# Trading-day primitives
# ---------------------------------------------------------------------------

def is_trading_day(d: date, holidays: set) -> bool:
    """Weekday and not an exchange holiday. `holidays` must hold datetime.date."""
    return d.weekday() < 5 and d not in holidays


def previous_trading_day(target: date, holidays: set, max_steps: int = 15) -> date:
    """Last trading day strictly before target. None if none found in max_steps."""
    d = target - timedelta(days=1)
    for _ in range(max_steps):
        if is_trading_day(d, holidays):
            return d
        d -= timedelta(days=1)
    return None


def next_trading_day(target: date, holidays: set, max_steps: int = 15) -> date:
    """First trading day on or after target. None if none found in max_steps."""
    d = target
    for _ in range(max_steps):
        if is_trading_day(d, holidays):
            return d
        d += timedelta(days=1)
    return None


def trading_days_before(target: date, n: int, holidays: set) -> date:
    """Step back n trading days from target. n=0 returns target unchanged."""
    d = target
    for _ in range(n):
        d = previous_trading_day(d, holidays)
        if d is None:
            return None
    return d


def preceding_saturday(target: date) -> date:
    """
    The most recent Saturday strictly before target — i.e. the start of the
    weekend that immediately precedes it.
    """
    # Mon(0) -> 2 days back, Tue -> 3, ... Fri(4) -> 6, Sat(5) -> 7, Sun(6) -> 1
    if target.weekday() == 6:
        days_back = 1
    else:
        days_back = target.weekday() + 2
    return target - timedelta(days=days_back)


# ---------------------------------------------------------------------------
# Monthly identification — calendar rule
# ---------------------------------------------------------------------------

def _last_day_of_month(year: int, month: int) -> date:
    if month == 12:
        return date(year, 12, 31)
    return date(year, month + 1, 1) - timedelta(days=1)


def identify_monthly_expiries(expiry_dates):
    """
    Given every expiry date available (weeklies included), return the monthly
    expiries: the last expiry of each calendar month.

    The trailing month is dropped when it may still gain a later expiry — if
    its last known expiry plus MONTH_COMPLETE_LOOKAHEAD_DAYS still falls inside
    the same month, another weekly can follow and the current maximum is not
    yet the monthly.

    Returns (monthlies, warnings):
      monthlies — sorted list of date
      warnings  — list of str, one per interior month that looks truncated
                  (a data gap, not a calendar fact — reported, never dropped)
    """
    by_month = {}
    for d in expiry_dates:
        by_month.setdefault((d.year, d.month), []).append(d)

    keys = sorted(by_month)
    all_sorted = sorted(expiry_dates)
    monthlies, warnings = [], []

    for i, key in enumerate(keys):
        last = max(by_month[key])

        if i == len(keys) - 1:
            # Trailing month: another expiry may still be added inside it, in
            # which case the current maximum is a weekly, not the monthly.
            if (last + timedelta(days=MONTH_COMPLETE_LOOKAHEAD_DAYS)).month == key[1]:
                warnings.append(
                    f"{key[0]}-{key[1]:02d}: trailing month incomplete "
                    f"(last expiry {last}, month ends {_last_day_of_month(*key)}) — excluded"
                )
                continue
        else:
            # Interior month: the list is closed, so the only thing that can go
            # wrong is a hole in it. A month's last expiry being a few days shy
            # of month end is normal (holiday-shifted expiries); a long gap
            # before the next listed expiry is not.
            nxt = next(d for d in all_sorted if d > last)
            if (nxt - last).days > MONTH_GAP_WARN_DAYS:
                warnings.append(
                    f"{key[0]}-{key[1]:02d}: {(nxt - last).days}-day gap between "
                    f"{last} and the next listed expiry {nxt} — possible data gap, kept"
                )

        monthlies.append(last)

    return sorted(monthlies), warnings


# ---------------------------------------------------------------------------
# Entry date
# ---------------------------------------------------------------------------

def entry_date_for(expiry: date, holidays: set,
                   dte_target: int, dte_min: int) -> date:
    """
    First trading day whose DTE is at or below dte_target. Returns None if
    rolling forward past holidays/weekends would push DTE below dte_min — in
    that case the contract has no valid entry at this target and is skipped.
    """
    candidate = next_trading_day(expiry - timedelta(days=dte_target), holidays)
    if candidate is None:
        return None
    if (expiry - candidate).days < dte_min:
        return None
    return candidate


# ---------------------------------------------------------------------------
# Exit dates — one function per policy arm
# ---------------------------------------------------------------------------

def exit_date_e1(expiry: date, holidays: set, exit_dte: int = None) -> date:
    """E1 — close when DTE <= exit_dte. First trading day at or below it."""
    if exit_dte is None:
        exit_dte = E1_EXIT_DTE
    return next_trading_day(expiry - timedelta(days=exit_dte), holidays)


def exit_date_e2a(expiry: date, holidays: set, n: int = None) -> date:
    """E2a — exactly n trading days before expiry."""
    if n is None:
        n = E2_TRADING_DAYS_BEFORE
    return trading_days_before(expiry, n, holidays)


def exit_date_e2b(expiry: date, holidays: set, n: int = None) -> date:
    """
    E2b — the earlier of:
      - the last trading day before the weekend immediately preceding expiry, and
      - E2a (n trading days before expiry).

    Both terms come from Pari's stated rationale: never carry the final
    pre-expiry weekend, and never be open into ELM on T-1. The weekend term
    dominates for Thursday-era expiries (exit the preceding Friday); the E2a
    term dominates for a Monday expiry (exit Thursday, not Friday).
    """
    pre_weekend = previous_trading_day(preceding_saturday(expiry), holidays)
    t_minus_n   = exit_date_e2a(expiry, holidays, n)
    if pre_weekend is None:
        return t_minus_n
    if t_minus_n is None:
        return pre_weekend
    return min(pre_weekend, t_minus_n)


def exit_date_e3(expiry: date, holidays: set) -> date:
    """E3 — expiry day itself; the force-close time is applied by the engine."""
    return expiry


def resolve_exit_date(expiry: date, holidays: set,
                      policy: str, offset_mode: str = None,
                      e1_dte: int = None) -> date:
    """
    Dispatch on EXIT_POLICY. `policy` accepts the sweep labels too ('E2a'/'E2b'),
    so Phase 4 can name an arm directly without touching EXIT_OFFSET_MODE.
    """
    p = policy.upper()
    if p == 'E1':
        return exit_date_e1(expiry, holidays, e1_dte)
    if p == 'E3':
        return exit_date_e3(expiry, holidays)
    if p == 'E2A':
        return exit_date_e2a(expiry, holidays)
    if p == 'E2B':
        return exit_date_e2b(expiry, holidays)
    if p == 'E2':
        if offset_mode == 'trading_days':
            return exit_date_e2a(expiry, holidays)
        if offset_mode == 'avoid_final_weekend':
            return exit_date_e2b(expiry, holidays)
        raise ValueError(f"EXIT_POLICY 'E2' needs EXIT_OFFSET_MODE, got {offset_mode!r}")
    raise ValueError(f"Unknown exit policy {policy!r}")
