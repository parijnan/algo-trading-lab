"""LiveData with a Fyers-first source (Phase 3 of plans/hestia-fyers-candle-source.md). With Fyers serving every window the engine must see exactly the bars it
would have seen from Angel One, with no Angel One candle call for that instrument; when Fyers cannot answer, behaviour must be exactly what it was; a Fyers-first
outage must not be hidden by an Angel One outage (the point of the phase); and the shadow and rescue must keep their roles."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_data_helpers import LiveWorld
from hestia_fake_helpers import events_of, factory
from hestia_core.interface import BarComplete, BarQuality
from hestia_core.live_data import LiveDataConfig

END = datetime(2026, 9, 3, 12, 40)
BOUNDARY = datetime(2026, 9, 3, 12, 0)
START = datetime(2026, 9, 3, 8, 50)
COLS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']


def cfg():
    return LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, deferred_bar_cutoff_min=3.0)


class FramesSmart:
    """A Fyers that agrees with the world's true minutes (like Angel One's), or answers None; records what it was handed."""

    def __init__(self, world_ref, instruments=('XX',), answer=True):
        self.calls, self.world_ref, self.instruments, self.answer = [], world_ref, tuple(instruments), answer
        self.cfg = type('C', (), {'instruments': self.instruments, 'settle_s': 1.0})()

    def handles(self, instrument):
        return instrument in self.instruments

    def fetch_window(self, ref, win_from, win_to, known=()):
        known = set(known)
        self.calls.append((ref.token, win_from, win_to, known))
        if not self.answer:
            return None
        fr = self.world_ref[0].frames[ref.token]
        end = pd.Timestamp(win_to).floor('min')
        m = fr[(fr.time_stamp >= win_from) & (fr.time_stamp < end) & ~fr.time_stamp.isin(known)]
        return m[COLS].reset_index(drop=True)

    def summary(self):
        return (len(self.calls), 0)


class RecordingShadow:
    def __init__(self):
        self.begins, self.results = [], []

    def begin(self, ref, *a):
        self.begins.append(ref.token)

    def angel_result(self, ref, tick, frames, stats, returned_at):
        self.results.append(ref.token)


class FramesRescue:
    after_attempts = 5

    def __init__(self):
        self.calls = []

    def fetch_window(self, ref, win_from, win_to, known=()):
        self.calls.append((ref.token, win_from, win_to))
        return None


@pytest.fixture
def worlds(tmp_path):
    made = []

    def build(**kw):
        log, ref = [], []
        w = LiveWorld(tmp_path / f'w{len(made)}', [('a', factory(log=log))], cfg=cfg(), **kw)
        ref.append(w)
        made.append(w)
        return w, log, ref
    yield build
    for w in made:
        w.close()


def bars(log):
    return [(e.boundary_ts, e.bar.open, e.bar.high, e.bar.low, e.bar.close, e.bar.volume, e.st.value, e.st.trend, e.minutes_present)
            for e in events_of(log, BarComplete) if e.bar is not None]


def stretch(w):
    w.sc.fail = lambda tok, f, t, now: BOUNDARY <= now < BOUNDARY + timedelta(seconds=70)


def polled_after_start(w):
    return [c for c in w.sc.calls if c[3] > START + timedelta(seconds=1)]


def test_fyers_first_gives_the_engine_exactly_the_bars_angel_one_would_and_angel_one_is_never_asked(worlds):
    clean, log_clean, _ = worlds()
    clean.start()
    clean.run_until(END)
    assert polled_after_start(clean), 'the control run polls Angel One every minute'
    w, log, ref = worlds()
    smart = FramesSmart(ref)
    w.data.smart = smart
    w.start()
    w.run_until(END)
    assert smart.calls, 'Fyers was asked first'
    assert bars(log) == bars(log_clean), 'every bar, Supertrend value and trend identical to the Angel One run'
    assert polled_after_start(w) == [], 'no Angel One candle call for a Fyers-first instrument while Fyers serves'


def test_an_angel_one_outage_across_a_boundary_is_invisible_when_fyers_first_serves(worlds):
    clean, log_clean, _ = worlds()
    clean.start()
    clean.run_until(END)
    w, log, ref = worlds()
    w.data.smart = FramesSmart(ref)
    stretch(w)
    w.start()
    w.run_until(END)
    at_boundary = [e for e in events_of(log, BarComplete) if e.boundary_ts == BOUNDARY]
    assert bars(log) == bars(log_clean) and at_boundary and at_boundary[0].quality == BarQuality.COMPLETE, 'the bar is complete at the boundary; Angel One never failed anyone'


def test_a_fyers_that_cannot_answer_leaves_behaviour_exactly_as_it_was(worlds):
    clean, log_clean, _ = worlds()
    stretch(clean)
    clean.start()
    clean.run_until(END)
    w, log, ref = worlds()
    smart = FramesSmart(ref, answer=False)
    w.data.smart = smart
    stretch(w)
    w.start()
    w.run_until(END)
    assert smart.calls and polled_after_start(w), 'Fyers was tried and Angel One then polled'
    assert bars(log) == bars(log_clean)
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == BOUNDARY]
    assert b.quality == BarQuality.RECOVERED, 'the same deferred, recovered bar as with no Fyers at all'


def test_a_smart_source_that_raises_never_reaches_the_engine(worlds):
    clean, log_clean, _ = worlds()
    clean.start()
    clean.run_until(END)
    w, log, ref = worlds()

    class Boom(FramesSmart):
        def fetch_window(self, *a, **k):
            raise RuntimeError('boom')
    w.data.smart = Boom(ref)
    w.start()
    w.run_until(END)
    assert bars(log) == bars(log_clean) and polled_after_start(w), 'Angel One took every poll'


def test_an_instrument_outside_the_smart_set_is_polled_from_angel_one_as_before(worlds):
    w, log, ref = worlds()
    smart = FramesSmart(ref, instruments=('YY',))                           # the world trades XX
    w.data.smart = smart
    w.start()
    w.run_until(END)
    assert smart.calls == [] and polled_after_start(w)


def test_the_shadow_measures_only_the_tokens_fyers_does_not_serve_first(worlds):
    w, log, ref = worlds()
    shadow = RecordingShadow()
    w.data.shadow, w.data.smart = shadow, FramesSmart(ref)
    w.start()
    w.run_until(datetime(2026, 9, 3, 9, 10))
    assert shadow.begins == [] and shadow.results == [], 'a Fyers-first token is measured by the smart source, not the parallel recorder'
    w2, _, ref2 = worlds()
    shadow2 = RecordingShadow()
    w2.data.shadow, w2.data.smart = shadow2, FramesSmart(ref2, instruments=('YY',))
    w2.start()
    w2.run_until(datetime(2026, 9, 3, 9, 10))
    assert shadow2.begins and shadow2.results, 'other instruments keep the parallel recording'


def test_when_fyers_first_fails_and_angel_one_fails_the_rescue_is_still_the_second_chance(worlds):
    w, log, ref = worlds()
    rescue = FramesRescue()
    w.data.smart, w.data.rescue = FramesSmart(ref, answer=False), rescue
    stretch(w)
    w.start()
    w.run_until(END)
    assert rescue.calls, 'Fyers first missed, the Angel One burst failed, and only then was the rescue asked'


def test_the_smart_source_is_handed_only_closed_minutes_the_engine_already_holds_to_exclude(worlds):
    w, log, ref = worlds()
    smart = FramesSmart(ref)
    w.data.smart = smart
    w.start()
    w.run_until(datetime(2026, 9, 3, 9, 30))
    token, f, t, known = smart.calls[-1]
    assert pd.Timestamp('2026-09-03 09:00') in known and all(k < pd.Timestamp(t).floor('min') for k in known)
