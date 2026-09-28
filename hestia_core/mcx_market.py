"""
MCX market facts for Hestia's live data service: session times (with the US-DST-dependent closing time and the evening-only
special sessions), the holiday calendar, and the contract catalog read from the pipeline's instrument master.

Ported from prometheus_functions / prometheus_configs (never imported: those modules run configuration at import time), and
pinned against them by tests/test_hestia_mcx_market.py on the real holiday file and every year of DST transitions.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import date, datetime, time, timedelta
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

import pandas as pd

from hestia_core import trading_calendar as hcal
from hestia_core.interface import ContractInfo, ContractRef

MORNING_OPEN = '09:00'
EVENING_OPEN = '17:00'


def us_dst_transition_dates(year: int) -> Tuple[date, date]:
    """US DST starts on the 2nd Sunday of March and ends on the 1st Sunday of November."""
    def nth_sunday(y: int, month: int, n: int) -> date:
        d = date(y, month, 1)
        first_sunday = d + timedelta(days=(6 - d.weekday()) % 7)
        return first_sunday + timedelta(weeks=n - 1)
    return nth_sunday(year, 3, 2), nth_sunday(year, 11, 1)


def closing_time_str(d: date) -> str:
    """MCX closes at 23:30 while US DST is in force and 23:55 otherwise."""
    start, end = us_dst_transition_dates(d.year)
    return '23:30' if start <= d < end else '23:55'


def closing_time(d: date) -> time:
    h, m = closing_time_str(d).split(':')
    return time(int(h), int(m))


class MarketCalendar:
    """Holidays and session times. `holidays_file` is the pipeline's mcx_holidays.csv (date, morning_session_closed,
    evening_session_closed, holiday_name); a missing file is treated as weekends-only, with `missing` set so the host can
    alert (a missing calendar silently corrupts the trading-day count behind the roll decision)."""

    def __init__(self, holidays_file: Optional[os.PathLike] = None, morning_open: str = MORNING_OPEN,
                 evening_open: str = EVENING_OPEN):
        self.morning_open, self.evening_open = morning_open, evening_open
        self.rows: Dict[date, Tuple[bool, bool]] = {}
        self.missing = True
        if holidays_file is not None and Path(holidays_file).exists():
            self.missing = False
            df = pd.read_csv(holidays_file)
            df['date'] = pd.to_datetime(df['date']).dt.date
            for _, r in df.iterrows():
                self.rows[r['date']] = (_truthy(r['morning_session_closed']), _truthy(r['evening_session_closed']))

    def fully_closed_dates(self) -> Set[date]:
        return {d for d, (m, e) in self.rows.items() if m and e}

    def fully_closed(self, d: date) -> bool:
        return d.weekday() >= 5 or self.rows.get(d, (False, False)) == (True, True)

    def evening_only(self, d: date) -> bool:
        if d.weekday() >= 5:
            return False
        m, e = self.rows.get(d, (False, False))
        return m and not e

    def session_open(self, d: date) -> datetime:
        hh, mm = (self.evening_open if self.evening_only(d) else self.morning_open).split(':')
        return datetime.combine(d, time(int(hh), int(mm)))

    def session_close(self, d: date) -> datetime:
        return datetime.combine(d, closing_time(d))

    def trading_days_left(self, today: date, expiry: date) -> int:
        return hcal.count_trading_days_inclusive(today, expiry, self.fully_closed_dates())


def _truthy(v) -> bool:
    return v is True or str(v).strip().lower() in ('true', '1')


@dataclass(frozen=True)
class ContractRow:
    ref: ContractRef
    lot_size: int
    tick_size: float
    freeze_qty: int              # units, as the master lists it
    filepath: str

    @property
    def freeze_lots(self) -> int:
        return max(1, self.freeze_qty // self.lot_size)


class ContractCatalog:
    """Contracts from the pipeline's instrument master, and the path of each one's own 1-minute file."""

    def __init__(self, master_file: os.PathLike, data_root: os.PathLike):
        self.data_root = Path(data_root)
        df = pd.read_csv(master_file)
        df['expiry_parsed'] = df['expiry'].apply(lambda s: datetime.strptime(str(s).strip(), '%d%b%Y'))
        self._rows: List[ContractRow] = []
        for _, r in df.iterrows():
            exp = r['expiry_parsed']
            ref = ContractRef(str(r['name']), str(r['token']), str(r['symbol']), exp.date())
            path = self.data_root / str(r['name']) / f"{exp.strftime('%Y-%m-%d')}_futures.csv"
            self._rows.append(ContractRow(ref, int(r['lotsize']), float(r['tick_size']) / 100.0, int(r['freeze_qty']), str(path)))
        self._by_token = {row.ref.token: row for row in self._rows}

    def live_rows(self, instrument: str, today: date) -> List[ContractRow]:
        return sorted((r for r in self._rows if r.ref.instrument == instrument and r.ref.expiry >= today),
                      key=lambda r: r.ref.expiry)

    def row(self, token: str) -> Optional[ContractRow]:
        return self._by_token.get(str(token))

    def infos(self, instrument: str, today: date, calendar: MarketCalendar) -> Tuple[ContractInfo, ...]:
        return tuple(ContractInfo(r.ref, r.lot_size, r.tick_size, r.freeze_lots,
                                  calendar.trading_days_left(today, r.ref.expiry)) for r in self.live_rows(instrument, today))
