"""A live core on the simulated kernel with the LiveData service over a candle double: the harness for the data-service tests."""
import sys
from datetime import date, datetime, timedelta
from pathlib import Path

import pandas as pd

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO))

from smartapi_double import CandleSmartConnect, FakeClock, FakeFeed  # noqa: E402
from hestia_fake_helpers import FRONT, NEXT, SPEC, RecEngine, minutes_frame  # noqa: E402
from hestia_core.broker_router import BrokerRouter  # noqa: E402
from hestia_core.core import CoreConfig, HestiaCore  # noqa: E402
from hestia_core.executors import InlineExecutor  # noqa: E402
from hestia_core.fake_kernel import EngineTask, SimKernel  # noqa: E402
from hestia_core.gateway import BrokerGateway  # noqa: E402
from hestia_core.history import merge_and_save  # noqa: E402
from hestia_core.live_data import LiveData, LiveDataConfig  # noqa: E402
from hestia_core.mcx_market import ContractCatalog, MarketCalendar  # noqa: E402
from hestia_core.paper_broker import PaperBroker  # noqa: E402
from hestia_core.replay import BrokerReply, SimBroker  # noqa: E402

DAY = date(2026, 9, 3)


def write_master(path, refs, lot=10):
    lines = ['token,symbol,name,expiry,strike,lotsize,instrumenttype,exch_seg,tick_size,freeze_qty,is_cas_enabled']
    for r in refs:
        lines.append(f"{r.token},{r.symbol},{r.instrument},{r.expiry.strftime('%d%b%Y').upper()},0,{lot},FUTCOM,MCX,50,{lot * 20},False")
    Path(path).write_text('\n'.join(lines) + '\n')


class LiveWorld:
    """`frames` are the full 1-minute frames the double serves; the pipeline files hold only the days before today."""

    def __init__(self, tmp_path, engines, refs=(FRONT, NEXT), start=datetime(2026, 9, 3, 8, 50), cfg=None, core_cfg=None,
                 feed=None, seed_first=True, pipeline_days=(date(2026, 9, 1), date(2026, 9, 2)), calendar=None, today_from=None, shadow=None, rescue=None):
        self.tmp = Path(tmp_path)
        self.tmp.mkdir(parents=True, exist_ok=True)
        self.refs = list(refs)
        write_master(self.tmp / 'master.csv', self.refs)
        self.frames = {}
        for k, ref in enumerate(self.refs):
            df = minutes_frame(100.0 + 5 * k, seed=11 + k)
            if today_from:                                # a session that opens later than 09:00 (an evening-only day)
                cut = pd.Timestamp(f'{DAY} {today_from}')
                df = df[~((df['time_stamp'].dt.date == DAY) & (df['time_stamp'] < cut))].reset_index(drop=True)
            self.frames[ref.token] = df
            past = df[df['time_stamp'].dt.date.isin(pipeline_days)]
            merge_and_save(self.tmp / 'mcx' / ref.instrument / f"{ref.expiry:%Y-%m-%d}_futures.csv", past)
        self.kernel = SimKernel(start)
        self.clock = FakeClock()
        self.sc = CandleSmartConnect(lambda: self.kernel.now, self.frames)
        self.gateway = BrokerGateway(self.sc, clock=self.clock.monotonic, sleep=self.clock.sleep)
        self.catalog = ContractCatalog(self.tmp / 'master.csv', self.tmp / 'mcx')
        self.calendar = calendar or MarketCalendar(None)
        self.feed = feed
        data_cfg = cfg or LiveDataConfig(seed_retry_attempts=1, ltp_refresh_s=0)
        data_cfg.seed_days = 2                       # the pipeline files hold the two days before today
        self.data = LiveData(self.kernel, self.gateway, InlineExecutor(), self.catalog, self.calendar, feed, self.tmp / 'cache',
                             data_cfg, sleep=self.clock.sleep, shadow=shadow, rescue=rescue)
        margin = lambda tok, net, avg: abs(net) * avg * 5.0                       # noqa: E731
        sim = SimBroker(self.kernel, lambda c: BrokerReply('fill'), self.data.price, margin, 1e9, 0.0, 30.0, 2.0, 1.0)
        paper = PaperBroker(self.kernel, self.data.price, margin)
        self.core = HestiaCore(self.kernel, self.data, BrokerRouter(sim, paper),
                               core_cfg or CoreConfig(ledger_reconcile_interval_s=None, reconcile_interval_s=None), EngineTask)
        for item in engines:
            self.core.register(item[0], item[1], **(item[2] if len(item) > 2 else {}))
        self.prepared = None
        if seed_first:
            self.prepared = self.data.prepare(sorted({r.instrument for r in self.refs}))

    def start(self, day=DAY):
        self.data.begin_session(day)
        self.core.begin_session()
        return self

    def run_until(self, t):
        self.kernel.run_until(t)

    def close(self):
        self.core.close()
