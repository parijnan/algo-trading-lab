"""LiveData with a rescue fallback (Phase 2 of plans/hestia-fyers-candle-source.md). With Angel One failing across a boundary, a rescue that supplies the missing
minutes must leave the engine seeing exactly the bars it would have seen with no failure at all; a rescue that cannot help must leave behaviour exactly as it was;
and it must only ever be handed minutes the engine lacks."""
from datetime import datetime, timedelta

import pandas as pd
import pytest

from hestia_data_helpers import LiveWorld
from hestia_fake_helpers import events_of, factory
from hestia_core.interface import BarComplete, BarQuality
from hestia_core.live_data import LiveDataConfig

END = datetime(2026, 9, 3, 12, 40)
BOUNDARY = datetime(2026, 9, 3, 12, 0)
COLS = ['time_stamp', 'open', 'high', 'low', 'close', 'volume']


def cfg():
    return LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0, deferred_bar_cutoff_min=3.0)


class FramesRescue:
    """Answers from the world's own true minutes, like a Fyers that agrees with Angel One; records what it was handed."""
    after_attempts = 5

    def __init__(self, world_ref, answer=True):
        self.calls, self.world_ref, self.answer = [], world_ref, answer

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
        self.results = []

    def begin(self, *a):
        pass

    def angel_result(self, ref, tick, frames, stats, returned_at):
        self.results.append((ref.token, tick, list(frames), [dict(s) for s in stats]))


@pytest.fixture
def worlds(tmp_path):
    made = []

    def build(**kw):
        log = []
        ref = []
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


def test_a_rescue_that_supplies_the_missing_minutes_leaves_the_engine_seeing_exactly_what_it_would_without_any_failure(worlds):
    clean, log_clean, _ = worlds()
    clean.start()
    clean.run_until(END)
    w, log, ref = worlds()
    rescue = FramesRescue(ref)
    w.data.rescue = rescue
    stretch(w)
    w.start()
    w.run_until(END)
    assert rescue.calls, 'the rescue was asked: Angel One failed across the boundary'
    assert bars(log) == bars(log_clean), 'every bar, Supertrend value and trend identical to the failure-free run'
    at_boundary = [e for e in events_of(log, BarComplete) if e.boundary_ts == BOUNDARY]
    assert at_boundary and at_boundary[0].quality == BarQuality.COMPLETE, 'the bar is complete at the boundary, not recovered later'


def test_a_rescue_that_cannot_help_leaves_behaviour_exactly_as_it_was(worlds):
    w0, log0, _ = worlds()
    stretch(w0)
    w0.start()
    w0.run_until(END)
    w, log, ref = worlds()
    w.data.rescue = FramesRescue(ref, answer=False)
    stretch(w)
    w.start()
    w.run_until(END)
    assert bars(log) == bars(log0)
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == BOUNDARY]
    assert b.quality == BarQuality.RECOVERED, 'still the deferred, recovered bar, exactly as without a rescue'


def test_the_rescue_is_handed_only_the_minutes_the_engine_already_holds_to_exclude(worlds):
    w, log, ref = worlds()
    rescue = FramesRescue(ref)
    w.data.rescue = rescue
    stretch(w)
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 2))
    token, f, t, known = rescue.calls[0]
    assert t >= BOUNDARY and pd.Timestamp('2026-09-03 11:55') in known and pd.Timestamp('2026-09-03 09:00') in known
    assert all(k < pd.Timestamp(t).floor('min') for k in known), 'it holds closed minutes only, never the forming one'


def test_the_rescue_is_never_called_while_angel_one_answers(worlds):
    w, log, ref = worlds()
    rescue = FramesRescue(ref)
    w.data.rescue = rescue
    w.start()
    w.run_until(END)
    assert rescue.calls == []


def test_a_rescued_window_reaches_the_shadow_as_rescued_with_no_angel_frame(worlds):
    w, log, ref = worlds()
    shadow = RecordingShadow()
    w.data.shadow = shadow
    w.data.rescue = FramesRescue(ref)
    stretch(w)
    w.start()
    w.run_until(datetime(2026, 9, 3, 12, 3))
    hit = [r for r in shadow.results if any(st.get('rescued') for st in r[3])]
    assert hit and all(f is None for _tok, _tick, frames, stats in hit for f, st in zip(frames, stats) if st.get('rescued'))
    assert all(st['attempts'] == 5 and st['exhausted'] is True for _t, _k, _f, stats in hit for st in stats if st.get('rescued'))


def test_a_rescue_that_raises_never_reaches_the_engine(worlds):
    w, log, ref = worlds()

    class Boom:
        after_attempts = 5

        def fetch_window(self, *a, **k):
            raise RuntimeError('boom')
    w.data.rescue = Boom()
    stretch(w)
    w.start()
    w.run_until(END)
    (b,) = [e for e in events_of(log, BarComplete) if e.boundary_ts == BOUNDARY]
    assert b.quality == BarQuality.RECOVERED
