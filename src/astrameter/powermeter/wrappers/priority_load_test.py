import asyncio
from unittest.mock import AsyncMock, Mock

from .priority_load import PriorityLoadPowermeter, find_priority_load


class FakeClock:
    def __init__(self) -> None:
        self.t = 1000.0

    def __call__(self) -> float:
        return self.t


def make(grid, load, clock, **kw):
    g = Mock()
    g.get_powermeter_watts = AsyncMock(return_value=grid)
    g.start = AsyncMock()
    g.stop = AsyncMock()
    ld = Mock()
    ld.get_powermeter_watts = AsyncMock(return_value=load)
    ld.start = AsyncMock()
    ld.stop = AsyncMock()
    return PriorityLoadPowermeter(g, ld, clock=clock, **kw), g, ld


async def test_grid_passes_through_and_load_measured_same_poll():
    pm, _, _ = make([66.0], [1417.0], FakeClock())
    assert await pm.get_powermeter_watts() == [66.0]
    assert pm.load_watts() == 1417.0


async def test_no_sample_yet_is_zero():
    pm, _, _ = make([66.0], [1417.0], FakeClock())
    assert pm.load_watts() == 0.0


async def test_standby_below_min_ignored():
    pm, _, _ = make([300.0], [12.0], FakeClock(), min_watts=50.0)
    await pm.get_powermeter_watts()
    assert pm.load_watts() == 0.0


async def test_meter_error_reuses_fresh_sample_then_zero():
    clock = FakeClock()
    pm, _, ld = make([300.0], [1400.0], clock, max_age=2.0)
    await pm.get_powermeter_watts()
    ld.get_powermeter_watts.side_effect = OSError("offline")
    clock.t += 1
    assert await pm.get_powermeter_watts() == [300.0]
    assert pm.load_watts() == 1400.0
    clock.t += 1.5
    await pm.get_powermeter_watts()
    assert pm.load_watts() == 0.0


async def test_slow_load_meter_does_not_block_poll():
    async def hang():
        await asyncio.sleep(10)
        return [1400.0]

    pm, _, ld = make([300.0], [0.0], FakeClock(), load_timeout=0.05)
    ld.get_powermeter_watts = hang
    assert await asyncio.wait_for(pm.get_powermeter_watts(), 1.0) == [300.0]
    assert pm.load_watts() == 0.0


async def test_start_stop_lifecycle():
    pm, g, ld = make([0.0], [0.0], FakeClock())
    await pm.start()
    await pm.stop()
    g.start.assert_awaited_once()
    ld.start.assert_awaited_once()
    ld.stop.assert_awaited_once()
    g.stop.assert_awaited_once()


def test_find_priority_load_unwraps_chain():
    pm, _, _ = make([0.0], [0.0], FakeClock())
    outer = Mock(spec=["wrapped_powermeter"])
    outer.wrapped_powermeter = Mock(spec=["wrapped_powermeter"])
    outer.wrapped_powermeter.wrapped_powermeter = pm
    assert find_priority_load(outer) is pm
    plain = Mock(spec=[])
    assert find_priority_load(plain) is None
