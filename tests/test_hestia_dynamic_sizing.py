"""Dynamic sizing as a share of the account's total margin (owner's rule, 2026-10-17; plans/hestia-dynamic-sizing.md):
units = floor((total margin / live engines sharing it) / the engine's margin per unit), limited by the unit cap, zero below one
unit (the entry is skipped), and zero without a fresh total-margin reading."""
import importlib
from types import SimpleNamespace

import pytest

from hestia_core.angel_broker import AngelConfig
from hestia_core.core import HestiaCore
from hestia_core.interface import OpenRequest, SizingConfig
from hestia_core.sizing import dynamic_units
from test_hestia_angel_broker import Rig

# ---- the arithmetic -------------------------------------------------------------------------------------------------------------

def test_the_share_is_the_total_over_the_sharing_engines_rounded_down():
    d = dynamic_units(5_441_384.89, 4, None, 125_173.0, 100)
    assert d.units == 10 and d.share == pytest.approx(5_441_384.89 / 4) and d.reason == ''          # 13,60,346 / 1,25,173 = 10.9
    assert dynamic_units(5_441_384.89, 3, None, 125_173.0, 100).units == 14                           # a third engine switched off
    assert dynamic_units(5_441_384.89, 1, None, 125_173.0, 100).units == 43


def test_the_unit_cap_and_the_allocation_limit_still_apply():
    assert dynamic_units(5_441_384.89, 4, None, 27_344.0, 20).units == 20                             # cap
    assert dynamic_units(5_441_384.89, 4, 300_000.0, 125_173.0, 100).units == 2                       # allocation limits the share
    assert dynamic_units(5_441_384.89, 4, 9e9, 125_173.0, 100).units == 10                            # a larger allocation changes nothing


def test_below_one_unit_is_zero_with_a_reason_and_no_fresh_reading_is_zero_too():
    d = dynamic_units(400_000.0, 4, None, 125_173.0, 100)
    assert d.units == 0 and 'below one unit' in d.reason
    assert dynamic_units(0.0, 4, None, 125_173.0, 100).units == 0
    assert dynamic_units(-5.0, 4, None, 125_173.0, 100).units == 0
    assert dynamic_units(1e6, 0, None, 100_000.0, 100).units == 10                                    # a zero count is treated as one
    with pytest.raises(ValueError):
        dynamic_units(1e6, 4, None, 0.0, 100)


# ---- the Angel total ------------------------------------------------------------------------------------------------------------

def _rms(rig, **fields):
    rig.sc.rmsLimit = lambda: {'status': True, 'data': fields}


def test_the_total_is_cash_plus_payin_plus_margin_used_from_one_answer():
    r = Rig()
    _rms(r, availablecash='5210479.8100', availableintradaypayin='121153.5800', utiliseddebits='109751.5000',
         collateral='5144319.2000', utilisedpayout='187314.1900', utilisedspan=None)
    assert r.port.total_margin('a') == 0.0                                  # nothing read yet: fails closed
    r.port.refresh_cash_blocking()
    assert r.port.total_margin('a') == pytest.approx(5_441_384.89) and r.port.free_cash('a') == pytest.approx(5_210_479.81)


def test_missing_none_or_blank_margin_fields_count_as_zero_and_never_cost_the_cash_value():
    r = Rig()
    _rms(r, availablecash='1000000', availableintradaypayin=None, utiliseddebits='')
    r.port.refresh_cash_blocking()
    assert r.port.free_cash('a') == 1_000_000.0 and r.port.total_margin('a') == 1_000_000.0
    r2 = Rig()                                                              # the stock double answers availablecash only
    r2.port.refresh_cash_blocking()
    assert r2.port.free_cash('a') == 1_000_000.0 and r2.port.total_margin('a') == 1_000_000.0


def test_a_stale_total_reads_zero_and_the_total_is_logged_when_it_changes(caplog):
    r = Rig()
    _rms(r, availablecash='100', availableintradaypayin='0', utiliseddebits='50')
    with caplog.at_level('INFO', logger='hestia_angel'):
        r.port.refresh_cash_blocking()
        r.port.refresh_cash_blocking()                                      # unchanged: no second line
    assert [m for m in caplog.messages if m.startswith('margin:')] == [
        'margin: availablecash 100.00 + availableintradaypayin 0.00 + utiliseddebits 50.00 = total 150.00']
    r.clock.sleep(AngelConfig().cash_max_age_s + 1)
    assert r.port.total_margin('a') == 0.0


def test_a_split_that_moves_at_a_constant_total_is_not_logged_again(caplog):
    r = Rig()
    with caplog.at_level('INFO', logger='hestia_angel'):
        _rms(r, availablecash='5206091.16', availableintradaypayin='0', utiliseddebits='114140.15')      # 09:00:12 on 2026-10-19
        r.port.refresh_cash_blocking()
        _rms(r, availablecash='5206155.96', availableintradaypayin='0', utiliseddebits='114075.35')      # 09:00:42: same total
        r.port.refresh_cash_blocking()
        _rms(r, availablecash='5231821.55', availableintradaypayin='0', utiliseddebits='88409.76')
        r.port.refresh_cash_blocking()
    assert len([m for m in caplog.messages if m.startswith('margin:')]) == 1


# ---- who shares the account -----------------------------------------------------------------------------------------------------

def _core(pools, states):
    c = object.__new__(HestiaCore)
    c._factories = {n: None for n in pools}
    c.engine_state = dict(states)
    c.broker = SimpleNamespace(pool_of=lambda e: pools[e])
    return c


def test_sharing_counts_live_engines_that_are_not_killed_and_a_paper_engine_is_alone():
    pools = {'prometheus': 'live', 'selene': 'live', 'helios': 'live', 'typhon': 'live', 'janus': 'paper:janus'}
    c = _core(pools, {n: 'running' for n in pools})
    states = c.engine_state
    assert c.sharing_engines('helios') == 4 and c.sharing_engines('janus') == 1
    states['typhon'] = 'killed'                                              # a KILL or a startup DISABLE/KILL gate
    assert c.sharing_engines('helios') == 3
    states['selene'] = 'failed'                                              # still counted: the cautious side
    assert c.sharing_engines('helios') == 3
    for n in ('prometheus', 'selene', 'helios'):
        states[n] = 'killed'
    assert c.sharing_engines('helios') == 1                                  # never below one


# ---- a zero-unit share sends nothing --------------------------------------------------------------------------------------------

@pytest.mark.parametrize('name', ['prometheus', 'selene', 'helios', 'typhon'])
def test_a_share_below_one_unit_sends_no_entry_and_a_flip_only_closes(name):
    m = importlib.import_module(f'test_{name}_engine_ports')
    sz = SizingConfig(dynamic=True, static_units=2, unit_cap=50)
    e = m.engine(m.hm(11, 0), m.watching(), ltp=65000.0, cash=1_000_000.0, sizing=sz)
    e.infos = {m.FRONT.token: type('I', (), {'lot_size': 1})()}
    e.ctx.total_margin = 10.0
    e._send_entry('bullish', m.hm(10, 45), 100.0)
    assert e.ctx.sent == []
    assert any('below one unit' in text for _, text in e.ctx.alerts)
    f = m.engine(m.hm(11, 0), m.in_trade('bullish'), ltp=65000.0, cash=1_000_000.0, sizing=sz)
    f.infos = {m.FRONT.token: type('I', (), {'lot_size': 1, 'ref': m.FRONT})()}
    f.ctx.total_margin = 10.0
    f._start_flip('bearish', m.hm(10, 45), 100.0)
    assert f.ctx.sent and not any(isinstance(r, OpenRequest) or getattr(r, 'open_lots', 0) for r in f.ctx.sent)
