"""
Trading-day arithmetic for Hestia (mirrors prometheus_functions._count_trading_days_inclusive and next_trading_day).
A day counts as a trading day unless it is a weekend or a full MCX closure (both sessions shut). A day that closes only
one session (an evening-only or morning-only holiday) still counts, as in production. The set of fully closed dates is
passed in by the caller, so this module reads no files.
"""

from datetime import date, timedelta
from typing import AbstractSet


def is_trading_day(d: date, fully_closed: AbstractSet[date]) -> bool:
    return d.weekday() < 5 and d not in fully_closed


def count_trading_days_inclusive(start: date, end: date, fully_closed: AbstractSet[date]) -> int:
    if start > end:
        return 0
    n, d = 0, start
    while d <= end:
        if is_trading_day(d, fully_closed):
            n += 1
        d += timedelta(days=1)
    return n


def next_trading_day(d: date, fully_closed: AbstractSet[date]) -> date:
    nd = d + timedelta(days=1)
    while not is_trading_day(nd, fully_closed):
        nd += timedelta(days=1)
    return nd
