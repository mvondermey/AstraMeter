import asyncio
import time
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

    pm, _, ld = make(
        [300.0], [0.0], FakeClock(), load_timeout=0.05, request_timeout=0.2
    )
    ld.get_powermeter_watts = hang
    assert await asyncio.wait_for(pm.get_powermeter_watts(), 1.0) == [300.0]
    assert pm.load_watts() == 0.0
    await pm.stop()


async def test_late_answer_still_counts_after_poll_returned():
    async def slow():
        await asyncio.sleep(0.1)
        return [1400.0]

    pm, _, ld = make(
        [300.0], [0.0], time.monotonic, load_timeout=0.02, request_timeout=1.0
    )
    ld.get_powermeter_watts = slow
    assert await asyncio.wait_for(pm.get_powermeter_watts(), 0.08) == [300.0]
    assert pm.load_watts() == 0.0
    await asyncio.sleep(0.2)
    assert pm.load_watts() == 1400.0
    s = pm.status()
    assert s["late"] == 1 and s["misses"] == 0 and s["samples_ok"] == 1
    await pm.stop()


async def test_failed_request_is_retried_at_once():
    pm, _, ld = make([300.0], [0.0], FakeClock())
    ld.get_powermeter_watts.side_effect = [OSError("blip"), [1400.0]]
    await pm.get_powermeter_watts()
    assert pm.load_watts() == 1400.0
    s = pm.status()
    assert s["misses"] == 1 and s["retries"] == 1 and s["samples_ok"] == 1


async def test_late_reading_is_dated_from_request_start():
    clock = FakeClock()

    async def slow():
        clock.t += 1.5
        return [1400.0]

    pm, _, ld = make([300.0], [0.0], clock, max_age=2.0)
    ld.get_powermeter_watts = slow
    await pm.get_powermeter_watts()
    s = pm.status()
    assert s["age_s"] == 1.5 and s["late"] == 1
    assert pm.load_watts() == 1400.0
    clock.t += 0.6
    assert pm.load_watts() == 0.0


async def test_retry_after_timeout_closes_gap_within_max_age():
    clock = FakeClock()
    n = 0

    async def read():
        nonlocal n
        n += 1
        if n == 2:
            clock.t += 0.8
            raise TimeoutError()
        return [1400.0]

    pm, _, ld = make([300.0], [0.0], clock, max_age=2.0)
    ld.get_powermeter_watts = read
    await pm.get_powermeter_watts()
    clock.t += 1.0
    await pm.get_powermeter_watts()
    s = pm.status()
    assert s["timeouts"] == 1 and s["retries"] == 1
    assert s["stale_events"] == 0 and abs(s["max_gap_s"] - 1.8) < 1e-9
    assert pm.load_watts() == 1400.0


async def test_retry_gap_depends_on_poll_period_and_max_age():
    # Previous reading 1.5 s old when the next poll starts (slow poll period),
    # first attempt times out after 0.8 s, retry answers at once: the reading
    # ages past 2.0 s before the retry lands, but not past 3.0 s.
    for max_age, stale in ((2.0, 1), (3.0, 0)):
        clock = FakeClock()
        calls = [0]

        async def read(clock=clock, calls=calls):
            calls[0] += 1
            if calls[0] == 2:
                clock.t += 0.8
                raise TimeoutError()
            return [1400.0]

        pm, _, ld = make([300.0], [0.0], clock, max_age=max_age)
        ld.get_powermeter_watts = read
        await pm.get_powermeter_watts()
        clock.t += 1.5
        await pm.get_powermeter_watts()
        assert pm.status()["stale_events"] == stale, max_age


async def test_stop_cancels_running_status_request():
    async def hang():
        await asyncio.sleep(10)
        return {"apower": 1400.0}

    pm, _, ld = make([300.0], [0.0], FakeClock(), load_timeout=0.02)
    ld.get_status = hang
    await pm.get_powermeter_watts()
    task = pm._fetch_task
    await pm.stop()
    assert task.cancelled() and pm._fetch_task is None


async def test_only_one_request_in_flight():
    calls = 0

    async def slow():
        nonlocal calls
        calls += 1
        await asyncio.sleep(0.6)
        return [1400.0]

    pm, _, ld = make([300.0], [0.0], FakeClock(), load_timeout=0.2, request_timeout=1.0)
    ld.get_powermeter_watts = slow
    await pm.get_powermeter_watts()
    for _ in range(2):  # later polls do not wait for the running request
        t0 = time.monotonic()
        await pm.get_powermeter_watts()
        assert time.monotonic() - t0 < 0.1
    assert calls == 1
    await pm.stop()
    assert pm._fetch_task is None


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
    assert s["misses"] == 6  # 3 polls x 2 attempts
    assert s["retries"] == 3
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

    pm, _, ld = make(
        [300.0], [0.0], FakeClock(), load_timeout=0.05, request_timeout=0.1
    )
    ld.get_status = hang
    assert await asyncio.wait_for(pm.get_powermeter_watts(), 1.0) == [300.0]
    assert pm.load_watts() == 0.0
    await asyncio.sleep(0.4)  # both attempts run out in the background
    s = pm.status()
    assert s["misses"] == 2 and s["timeouts"] == 2 and s["retries"] == 1
    assert s["meter"] is None
    await pm.stop()


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
