"""data_downloader_fyers_equities.expand_truncated_listing: Fyers's expired-contract listing for 2026-09-24 held 12 contracts against about 390 for its neighbours while its
history endpoint served every real strike. The guard compares a listing with the previous expiries and, when it is far smaller, requests the strike grid instead."""
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'data_pipeline'))
import data_downloader_fyers_equities as eq          # noqa: E402

PRE = 'BSE:SENSEX26SEP'


def listing(prefix, lo, hi, step=100):
    return [f'{prefix}{k}{t}' for k in range(lo, hi + 1, step) for t in ('PE', 'CE')]


def tracking_df(dates):
    return pd.DataFrame({'expiry_date': pd.to_datetime(dates, utc=True), 'start_date': pd.to_datetime(dates, utc=True), 'download_status': [True] * len(dates)})


DATES = ['2026-08-13', '2026-08-20', '2026-08-27', '2026-09-24']
REFS = {'2026-08-13': listing('BSE:SENSEX26813', 70000, 71900), '2026-08-20': listing('BSE:SENSEX26820', 70000, 72000), '2026-08-27': listing(PRE, 70000, 71900)}


@pytest.fixture
def api(monkeypatch):
    calls = []

    def fake(anchor, date):
        calls.append(date)
        return REFS.get(date, [])
    monkeypatch.setattr(eq, 'get_expired_option_contracts', fake)
    return calls


def run(symbols, dates=DATES):
    return eq.expand_truncated_listing('Sensex', 'BSE:ANCHOR', pd.Timestamp(dates[-1], tz='UTC'), symbols, tracking_df(dates))


def test_a_normal_sized_listing_is_returned_unchanged(api):
    normal = listing(PRE, 70000, 71900)[:25]                       # 25 against a median of 40: above half
    assert run(normal) == normal


def test_a_truncated_listing_becomes_the_strike_grid_in_the_same_symbol_format(api):
    out = run([f'{PRE}70000PE', f'{PRE}70000CE'])
    expected = listing(PRE, 70000, 72000)                           # the references span 70000-72000 (the widest is the 08-20 listing)
    assert set(out) == set(expected) and len(out) == len(expected)
    assert api == ['2026-08-27', '2026-08-20', '2026-08-13'], 'it compared with the three expiries before this one, nearest first'


def test_symbols_the_listing_held_outside_the_grid_are_kept(api):
    out = run([f'{PRE}70000PE', f'{PRE}95000CE'])                   # a far strike the references never showed
    assert f'{PRE}95000CE' in out and f'{PRE}71500CE' in out and out.count(f'{PRE}70000PE') == 1


def test_the_grid_step_is_the_most_common_gap_of_the_widest_reference(api, monkeypatch):
    refs = dict(REFS)
    refs['2026-08-27'] = listing(PRE, 70000, 70500, 50) + [f'{PRE}72000PE']        # mostly 50-point gaps with one big jump
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, d: refs.get(d, []))
    out = run([f'{PRE}70000PE'])
    steps = sorted({eq._strike_and_type(s)[0] for s in out})
    assert steps[1] - steps[0] == 100, 'the widest reference (08-20, step 100) sets the step, not the finer 08-27 listing'


def test_one_truncated_reference_does_not_hide_a_truncated_listing_the_median_is_used(api, monkeypatch):
    refs = dict(REFS)
    refs['2026-08-20'] = [f'BSE:SENSEX26820{70000}PE', f'BSE:SENSEX26820{70000}CE']      # an earlier expiry that Fyers truncated too
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, d: refs.get(d, []))
    few = listing(PRE, 70000, 70600)[:10]                                                  # 10 against a median of 40: truncated, whatever the smallest reference says
    out = run(few)
    assert len(out) > len(few) and f'{PRE}71500CE' in out


def test_with_no_earlier_listings_to_compare_the_listing_is_trusted(monkeypatch):
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, d: [])
    few = [f'{PRE}70000PE']
    assert run(few) == few
    assert run(few, dates=['2026-09-24']) == few                  # and with no earlier expiry at all


def test_a_weekly_fixed_width_format_listing_is_rebuilt_in_its_own_format(api):
    weekly = 'BSE:SENSEX26924'
    out = run([f'{weekly}70000PE'])
    assert f'{weekly}71500CE' in out and all(s.startswith(weekly) for s in out)


def test_only_the_three_most_recent_earlier_expiries_are_used(api):
    run([f'{PRE}70000PE'], dates=['2026-07-30'] + DATES)
    assert len(api) == 3


# ---- through the download loop ----------------------------------------------------------------------------------------------------

def candles():
    return pd.DataFrame({'time_stamp': pd.date_range('2026-09-01 09:15', periods=3, freq='min'), 'open': 1.0, 'high': 1.0, 'low': 1.0, 'close': 1.0, 'volume': 1})


def test_the_download_loop_requests_the_grid_for_a_truncated_listing_and_saves_the_strikes_that_traded(monkeypatch, tmp_path):
    t = tmp_path / 'options_list.csv'
    pd.DataFrame({'expiry_date': ['2026-08-13T07:00:00.000Z', '2026-08-20T07:00:00.000Z', '2026-08-27T07:00:00.000Z', '2026-09-24T07:00:00.000Z'],
                  'start_date': ['2026-07-01T09:15:00.000Z'] * 4, 'end_date': ['x'] * 4, 'download_status': [True, True, True, False]}).to_csv(t, index=False)
    asked = []

    def fetch(symbol, start, end):
        asked.append(symbol)
        return candles() if int(symbol[len(PRE):-2]) % 200 == 0 else pd.DataFrame()      # half the strikes "traded"
    monkeypatch.setattr(eq, 'resolve_options_anchor', lambda u, m: 'BSE:ANCHOR')
    monkeypatch.setattr(eq, 'get_expired_option_contracts', lambda a, d: [f'{PRE}70000PE', f'{PRE}70000CE'] if d == '2026-09-24' else REFS.get(d, []))
    monkeypatch.setattr(eq, 'get_expired_historical_data', fetch)
    eq.download_options_for_symbol('Sensex', 'SENSEX', 'BSE_FO', tmp_path / 'opt', t)
    assert len(asked) == len(listing(PRE, 70000, 72000)), 'the whole grid was requested, not the 2 listed contracts'
    saved = sorted(p.name for p in (tmp_path / 'opt' / '2026-09-24').iterdir())
    assert len(saved) == 22 and '70200ce.csv' in saved and '70100ce.csv' not in saved
    assert pd.read_csv(t)['download_status'].iloc[-1], '22 of 42 candidates traded, above the floor: complete'
