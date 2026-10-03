"""LiveData's two Fyers shadow hooks (plans/hestia-fyers-candle-source.md, Phase 1): `begin` at the minute tick and `angel_result` when the Angel One
poll for that tick is done. The property that matters: with a shadow attached, ill-behaved or not, every engine sees EXACTLY what it saw without
one."""
from datetime import datetime, timedelta

from hestia_data_helpers import DAY, LiveWorld
from hestia_fake_helpers import events_of, factory
from hestia_core.interface import BarComplete
from hestia_core.live_data import LiveDataConfig

import pytest

END = datetime(2026, 9, 3, 23, 40)


class RecordingShadow:
    def __init__(self, world_ref):
        self.begins, self.results, self.world_ref = [], [], world_ref

    def begin(self, ref, tick, win_from, win_to):
        calls = len(self.world_ref[0].sc.calls) if self.world_ref else None
        self.begins.append((ref.token, tick, win_from, win_to, calls))

    def angel_result(self, ref, tick, frames, stats, returned_at):
        self.results.append((ref.token, tick, frames, stats, returned_at))


class BoomShadow:
    def begin(self, *a):
        raise RuntimeError('begin boom')

    def angel_result(self, *a):
        raise RuntimeError('angel boom')


@pytest.fixture
def worlds(tmp_path):
    made = []

    def build(engines, **kw):
        w = LiveWorld(tmp_path / f'w{len(made)}', engines, **kw)
        made.append(w)
        return w
    yield build
    for w in made:
        w.close()


def bars(log):
    return [(e.boundary_ts, e.bar.close, e.st.value, e.st.trend, e.st.flip, e.quality) for e in events_of(log, BarComplete) if e.bar is not None]


def test_begin_fires_at_every_minute_tick_with_the_five_minute_window_before_the_angel_call(worlds):
    ref = []
    shadow = RecordingShadow(ref)
    w = worlds([('a', factory())], shadow=shadow)
    ref.append(w)
    w.start()
    w.sc.calls.clear()
    shadow.begins.clear()
    w.run_until(datetime(2026, 9, 3, 10, 5, 30))
    ticks = [b[1] for b in shadow.begins]
    assert len(ticks) >= 4 and all(t.second == 0 for t in ticks) and ticks == sorted(ticks)
    token, tick, win_from, win_to, calls_at_begin = shadow.begins[0]
    assert win_to == tick and win_from == tick - timedelta(minutes=5)
    assert calls_at_begin == len([c for c in w.sc.calls if c[3] < tick]), 'begin runs at the tick, before this tick\'s Angel One call'


def test_angel_result_carries_the_closed_frames_the_attempt_count_and_the_return_time(worlds):
    ref = []
    shadow = RecordingShadow(ref)
    w = worlds([('a', factory())], shadow=shadow)
    ref.append(w)
    w.start()
    w.run_until(datetime(2026, 9, 3, 10, 3))
    token, tick, frames, stats, returned = shadow.results[-1]
    assert len(frames) == len(stats) >= 1 and frames[-1] is not None and not frames[-1].empty
    assert stats[-1]['attempts'] == 1 and stats[-1]['exhausted'] is False
    assert frames[-1]['time_stamp'].max() < tick.replace(second=0), 'only closed minutes: the forming one is dropped'
    assert returned >= tick


def test_an_exhausted_angel_burst_reaches_the_shadow_as_exhausted(worlds):
    ref = []
    shadow = RecordingShadow(ref)
    w = worlds([('a', factory())], shadow=shadow, cfg=LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0))
    ref.append(w)
    bad = datetime(2026, 9, 3, 10, 0)
    w.sc.fail = lambda tok, f, t, now: bad <= now < bad + timedelta(minutes=1)
    w.start()
    w.run_until(datetime(2026, 9, 3, 10, 3))
    at_bad = [r for r in shadow.results if r[1] == bad]
    assert at_bad and at_bad[0][2][-1] is None and at_bad[0][3][-1]['exhausted'] is True and at_bad[0][3][-1]['attempts'] == 5


def test_engines_see_exactly_the_same_bars_with_a_shadow_a_misbehaving_shadow_or_none(worlds):
    runs = {}
    for name, shadow in (('none', None), ('recording', RecordingShadow([])), ('boom', BoomShadow())):
        log = []
        w = worlds([('a', factory(log=log))], shadow=shadow)
        w.start()
        w.run_until(END)
        runs[name] = bars(log)
    assert len(runs['none']) > 5 and any(b[4] for b in runs['none']), 'the day must contain a flip or the comparison is vacuous'
    assert runs['recording'] == runs['none'] and runs['boom'] == runs['none']
