"""hestia_core.trend_guard: the seed is compared with the last bar the engine traded live, and a position held against the Supertrend is
reported (2026-10-27: Typhon seeded bearish from a pipeline file while it held a live long, and nothing said so)."""
import dataclasses
from datetime import datetime

import pytest

from typhon_engine_helpers import FLIP_PATH, FRONT, SESSION_DATE, SESSION_OPEN, ZIGZAG, alert_texts, hm, scripted_world
from hestia_core.trend_guard import live_mark, seed_disagreement, trend_name
from typhon_engine.state import EngineState


class _Bar:
    def __init__(self, ts, close=100.0):
        self.ts, self.close = ts, close


class _St:
    def __init__(self, trend, value):
        self.trend, self.value = trend, value


TS = datetime(2026, 10, 26, 21, 45)
SERIES = [(_Bar(datetime(2026, 10, 26, 21, 30)), _St('bearish', 292.4)), (_Bar(TS, 292.5), _St('bullish', 286.75))]
MARK = {'ts': TS.isoformat(), 'token': 'T', 'trend': 'bullish', 'st': 286.75, 'close': 292.5}


def test_a_matching_bar_and_every_case_that_cannot_be_compared_say_nothing():
    assert seed_disagreement(MARK, SERIES, 'T', 0.10) is None
    assert seed_disagreement(dict(MARK, st=286.80), SERIES, 'T', 0.10) is None, 'inside the tolerance'
    assert seed_disagreement(None, SERIES, 'T', 0.10) is None
    assert seed_disagreement(MARK, SERIES, 'OTHER', 0.10) is None, 'another contract (a roll): nothing to compare'
    assert seed_disagreement(dict(MARK, ts='2026-10-25T12:00:00'), SERIES, 'T', 0.10) is None, 'the bar is not in the seeded series'
    assert seed_disagreement(MARK, [(_Bar(TS), _St(None, None))], 'T', 0.10) is None, 'still warming up there'


def test_a_different_trend_or_a_value_beyond_the_tolerance_is_reported_with_both_values():
    text = seed_disagreement(dict(MARK, trend='bearish', st=293.73), SERIES, 'T', 0.10)
    assert 'bullish' in text and 'bearish' in text and '286.75' in text and '293.73' in text and '26 Oct 21:45' in text
    far = seed_disagreement(dict(MARK, st=292.40), SERIES, 'T', 0.10)
    assert far and '292.40' in far and '286.75' in far and 'both are bullish' in far
    assert seed_disagreement(dict(MARK, st=292.40), SERIES, 'T', 5.0) is None, 'the tolerance is a setting'


def test_the_mark_is_a_plain_dict_that_survives_the_state_round_trip():
    mark = live_mark('T', _Bar(TS, 292.5), _St('bullish', 286.75))
    assert mark == MARK
    assert EngineState.from_json(EngineState(last_live_bar=mark).to_json()).last_live_bar == mark
    assert EngineState.from_json('{"status": "watching"}').last_live_bar is None, 'a state file from before the field existed loads'
    assert trend_name(None) is None and trend_name('Bullish') == 'bullish'


def _seeded_latest(made):
    bar, st = made[-1].ctx.st_series(made[-1].contract, 300)[-1]
    return bar, st


def test_the_engine_records_the_last_live_bar_and_a_restart_on_the_same_data_stays_quiet():
    made = []
    h = scripted_world(ZIGZAG, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(14, 0))
    mark = made[-1].state.last_live_bar
    assert mark and mark['token'] == FRONT.token and mark['trend'] in ('bullish', 'bearish') and mark['st'] > 0
    assert mark['ts'] == made[-1].state.last_processed_boundary, 'recorded with the watermark it belongs to'
    assert not [t for t in alert_texts(h) if 'SEED DISAGREES' in t or 'AGAINST THE TREND' in t]


def _restart_with_mark(mutate):
    """Run a first session far enough to see what the seed produces, then start a fresh engine whose saved state carries a mark for the
    seeded series' own last bar, changed by `mutate`."""
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 1))
    bar, st = _seeded_latest(made)
    mark = mutate(live_mark(FRONT.token, bar, st))
    made2 = []
    h2 = scripted_world(FLIP_PATH, made=made2)
    h2._saved_state['typhon'] = EngineState(last_live_bar=mark).to_json()
    h2.start_session(SESSION_DATE, SESSION_OPEN)
    h2.run_until(hm(9, 1))
    texts = alert_texts(h2)
    assert any('ST_15 seeded' in t for t in texts), 'the restarted engine did start, so silence below means something'
    return texts


def test_a_seed_that_disagrees_with_the_last_live_bar_alerts_critically_at_start():
    flipped = lambda m: dict(m, trend='bearish' if m['trend'] == 'bullish' else 'bullish', st=m['st'] * 1.01)   # noqa: E731
    texts = _restart_with_mark(flipped)
    assert any(t.startswith('SEED DISAGREES') for t in texts), texts
    assert not any('SEED DISAGREES' in t for t in _restart_with_mark(lambda m: m)), 'the same bar as live: quiet'
    assert not any('SEED DISAGREES' in t for t in _restart_with_mark(lambda m: dict(m, token='another'))), 'another contract: quiet'


def test_a_position_against_the_trend_is_reported_at_start_and_not_again_within_the_hour():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 1))
    bar, st = _seeded_latest(made)
    against = 'bearish' if trend_name(st.trend) == 'bullish' else 'bullish'
    sign = -1 if against == 'bearish' else 1
    made2 = []
    h2 = scripted_world(FLIP_PATH, made=made2)
    s = EngineState(status='in_trade', direction=against, units=1, entry_price=bar.close, entry_ts='2026-09-02T22:00:00',
                    contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(),
                    sl_price=bar.close * (1.5 if against == 'bearish' else 0.5), lots=1, trade_counter=3)
    h2._saved_state['typhon'] = s.to_json()
    h2.seed_position('typhon', FRONT, sign, bar.close)
    h2.start_session(SESSION_DATE, SESSION_OPEN)
    h2.run_until(hm(9, 50))
    hits = [t for t in alert_texts(h2) if t.startswith('POSITION AGAINST THE TREND')]
    assert len(hits) == 1 and 'at start' in hits[0] and against in hits[0], hits


def test_a_position_that_agrees_with_the_trend_is_quiet():
    made = []
    h = scripted_world(FLIP_PATH, made=made)
    h.start_session(SESSION_DATE, SESSION_OPEN)
    h.run_until(hm(9, 1))
    bar, st = _seeded_latest(made)
    agree = trend_name(st.trend)
    sign = -1 if agree == 'bearish' else 1
    made2 = []
    h2 = scripted_world(FLIP_PATH, made=made2)
    s = EngineState(status='in_trade', direction=agree, units=1, entry_price=bar.close, entry_ts='2026-09-02T22:00:00',
                    contract_token=FRONT.token, contract_symbol=FRONT.symbol, contract_expiry=FRONT.expiry.isoformat(),
                    sl_price=bar.close * (1.5 if agree == 'bearish' else 0.5), lots=1, trade_counter=3)
    h2._saved_state['typhon'] = s.to_json()
    h2.seed_position('typhon', FRONT, sign, bar.close)
    h2.start_session(SESSION_DATE, SESSION_OPEN)
    h2.run_until(hm(9, 50))
    assert not [t for t in alert_texts(h2) if 'AGAINST THE TREND' in t or 'SEED DISAGREES' in t]
