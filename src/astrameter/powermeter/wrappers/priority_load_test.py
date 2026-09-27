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
    ld = Mock(spec=["get_powermeter_watts", "start", "stop"])
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


async def test_status_meter_exposes_raw_status():
    clock = FakeClock()
    pm, _, ld = make([300.0], [0.0], clock)
    raw = {"output": True, "apower": 1412.5, "errors": [], "source": "HTTP_in"}
    ld.get_status = AsyncMock(return_value=raw)
    await pm.get_powermeter_watts()
    assert pm.load_watts() == 1412.5
    s = pm.status()
    assert s["meter"] == raw
    assert s["age_s"] == 0.0
    assert s["samples_ok"] == 1 and s["misses"] == 0
    ld.get_powermeter_watts.assert_not_awaited()


async def test_counters_track_misses_and_stale_gaps():
    clock = FakeClock()
    pm, _, ld = make([300.0], [1400.0], clock, max_age=2.0)
    await pm.get_powermeter_watts()
    ld.get_powermeter_watts.side_effect = OSError("offline")
    for _ in range(3):
        clock.t += 1
        await pm.get_powermeter_watts()
    ld.get_powermeter_watts.side_effect = None
    clock.t += 1
    await pm.get_powermeter_watts()
    s = pm.status()
    assert s["misses"] == 3
    assert s["samples_ok"] == 2
    assert s["stale_events"] == 1
    assert s["max_gap_s"] == 4.0
    # A single miss inside max_age is not a stale event.
    ld.get_powermeter_watts.side_effect = OSError("offline")
    clock.t += 1
    await pm.get_powermeter_watts()
    ld.get_powermeter_watts.side_effect = None
    clock.t += 1
    await pm.get_powermeter_watts()
    assert pm.status()["stale_events"] == 1


async def test_slow_get_status_times_out_and_counts():
    async def hang():
        await asyncio.sleep(10)
        return {"apower": 1400.0}

    pm, _, ld = make([300.0], [0.0], FakeClock(), load_timeout=0.05)
    ld.get_status = hang
    assert await asyncio.wait_for(pm.get_powermeter_watts(), 1.0) == [300.0]
    assert pm.load_watts() == 0.0
    s = pm.status()
    assert s["misses"] == 1 and s["timeouts"] == 1 and s["meter"] is None


def test_status_before_first_sample():
    pm, _, _ = make([0.0], [0.0], FakeClock())
    s = pm.status()
    assert s["age_s"] is None and s["meter"] is None and s["load_w"] == 0.0


def test_find_priority_load_unwraps_chain():
    pm, _, _ = make([0.0], [0.0], FakeClock())
    outer = Mock(spec=["wrapped_powermeter"])
    outer.wrapped_powermeter = Mock(spec=["wrapped_powermeter"])
    outer.wrapped_powermeter.wrapped_powermeter = pm
    assert find_priority_load(outer) is pm
    plain = Mock(spec=[])
    assert find_priority_load(plain) is None
