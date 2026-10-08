"""The session report's alert tally must include the live-data layer's alerts (incomplete 15-minute bars, stale feeds, failed seeds), which are posted
to Slack straight through the router and used to bypass core.alerts (found 2026-10-08: the report said 1 warning while #error-alerts showed 2)."""
from datetime import datetime
from types import SimpleNamespace

from hestia_core.core import Alert
from hestia_core.live_data import LiveData


class StubCore:
    def __init__(self):
        self.alerts, self.sunk = [], []

    def note_alert(self, level, engine, text):
        self.alerts.append((level, engine, text))

    def _alert(self, level, engine, text):
        self.sunk.append((level, engine, text))


def live_data(alert_fn, core):
    d = LiveData.__new__(LiveData)
    d._alert_fn, d.core = alert_fn, core
    return d


def test_a_live_data_alert_is_posted_once_and_counted_once():
    posted, core = [], StubCore()
    live_data(lambda level, text: posted.append((level, text)), core)._alert('warning', 'GOLDPETAL30OCT26FUT: 15m bar 14:00-14:15 still incomplete (14/15)')
    assert posted == [('warning', 'GOLDPETAL30OCT26FUT: 15m bar 14:00-14:15 still incomplete (14/15)')]
    assert core.alerts == [('warning', None, 'GOLDPETAL30OCT26FUT: 15m bar 14:00-14:15 still incomplete (14/15)')]
    assert core.sunk == []                                                  # not routed a second time through the core's sinks


def test_before_the_core_is_attached_a_live_data_alert_is_only_posted():
    posted = []
    live_data(lambda level, text: posted.append(text), None)._alert('warning', 'early')
    assert posted == ['early']


def test_with_no_alert_function_the_core_routes_it():
    core = StubCore()
    live_data(None, core)._alert('critical', 'x')
    assert core.sunk == [('critical', None, 'x')] and core.alerts == []


def test_note_alert_adds_to_the_tally_and_calls_no_sink():
    from hestia_core.core import HestiaCore
    core = HestiaCore.__new__(HestiaCore)
    core.alerts, core.alert_sinks = [], [lambda a: (_ for _ in ()).throw(AssertionError('sink must not be called'))]
    core.kernel = SimpleNamespace(now=datetime(2026, 10, 8, 14, 16))
    HestiaCore.note_alert(core, 'warning', None, 'incomplete bar')
    assert [(a.level, a.engine, a.text) for a in core.alerts] == [('warning', None, 'incomplete bar')]
