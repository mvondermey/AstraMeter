import asyncio
import contextlib
import time
from collections.abc import Callable

from astrameter.config.logger import logger
from astrameter.powermeter.base import Powermeter

from .base import PowermeterWrapper


class PriorityLoadPowermeter(PowermeterWrapper):
    """Measure a priority consumer (e.g. an EV charger) alongside the grid.

    The grid reading is passed through unchanged. The consumer's power is read
    concurrently on every poll (same instant as the grid) and exposed through
    :meth:`load_watts`, which the CT002 balancer uses to keep the batteries
    from discharging into that consumer (see
    ``astrameter.ct002.balancer.apply_priority_load``).

    A poll waits at most ``load_timeout`` seconds for the consumer meter so it
    never delays a battery poll. The meter request itself runs in the
    background and is not cancelled then: a late answer (up to
    ``request_timeout``) still refreshes the reading, and a failed request is
    retried right away (``attempts`` in total). Polls arriving while an earlier
    request is still running do not wait for it. A reading is dated from when
    its request started and is reused while younger than ``max_age``, else 0 W
    is assumed (batteries behave as without this feature). A new request only
    starts with the next poll, so the previous reading is already one poll
    period old by then: a retry closes the gap only if poll period +
    ``request_timeout`` + retry latency stays below ``max_age``.
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        load_powermeter: Powermeter,
        min_watts: float = 50.0,
        max_age: float = 2.0,
        load_timeout: float = 0.5,
        request_timeout: float = 0.8,
        attempts: int = 2,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self.load_powermeter = load_powermeter
        self.min_watts = min_watts
        self.max_age = max_age
        self.load_timeout = load_timeout
        self.request_timeout = request_timeout
        self.attempts = attempts
        self._clock = clock
        self._load_watts = 0.0
        self._load_at: float | None = None
        self._last_error_log = 0.0
        self._fetch_task: asyncio.Task | None = None
        # Raw meter status (e.g. Shelly Switch.GetStatus) so other tools
        # can read the consumer from here instead of polling the meter again.
        self._status: dict | None = None
        # Reliability counters (since start): misses = failed requests, of
        # which timeouts hit request_timeout; retries = second attempts after a
        # failure; late = answers that arrived after load_timeout but still
        # counted; stale_events = times a reading aged past max_age before
        # the next one arrived, i.e. moments where load_watts() fell back to
        # 0 W; max_gap = longest such age.
        self.samples_ok = 0
        self.misses = 0
        self.timeouts = 0
        self.retries = 0
        self.late = 0
        self.stale_events = 0
        self.max_gap = 0.0

    def load_watts(self) -> float:
        """Current consumer power, 0 when stale or below min_watts."""
        if self._load_at is None or self._clock() - self._load_at > self.max_age:
            return 0.0
        return self._load_watts if self._load_watts > self.min_watts else 0.0

    def status(self) -> dict:
        """Last raw reading plus reliability counters (for the web API)."""
        age = None if self._load_at is None else self._clock() - self._load_at
        return {
            "power_w": self._load_watts,
            "age_s": age,
            "load_w": self.load_watts(),
            "meter": self._status,
            "meter_ip": getattr(self.load_powermeter, "ip", None),
            "samples_ok": self.samples_ok,
            "misses": self.misses,
            "timeouts": self.timeouts,
            "retries": self.retries,
            "late": self.late,
            "stale_events": self.stale_events,
            "max_gap_s": self.max_gap,
            "min_watts": self.min_watts,
            "max_age": self.max_age,
        }

    async def _read(self) -> tuple[float, dict | None]:
        get_status = getattr(self.load_powermeter, "get_status", None)
        if get_status is not None:
            status = await get_status()
            return float(status["apower"]), status
        values = await self.load_powermeter.get_powermeter_watts()
        return float(sum(values)), None

    def _store(self, watts: float, status: dict | None, started: float) -> None:
        # Date the reading from the request start: the meter measured it at
        # some point after that, so its age is never understated. The gap is
        # measured up to now, when the new reading becomes usable.
        now = self._clock()
        if now - started > self.load_timeout:
            self.late += 1
        if self._load_at is not None:
            gap = now - self._load_at
            self.max_gap = max(self.max_gap, gap)
            if gap > self.max_age:
                self.stale_events += 1
        self._load_watts = watts
        self._load_at = started
        if status is not None:
            self._status = status
        self.samples_ok += 1

    def _log_miss(self, e: BaseException) -> None:
        now = self._clock()
        if now - self._last_error_log > 300:
            self._last_error_log = now
            logger.warning(
                f"Priority load meter unreachable, counting 0 W: {e!r} "
                f"(since start: {self.misses} misses, {self.timeouts} timeouts, "
                f"{self.retries} retries, {self.late} late answers, "
                f"{self.stale_events} stale gaps, longest gap {self.max_gap:.1f} s)",
                exc_info=False,
            )

    async def _fetch(self) -> None:
        for attempt in range(self.attempts):
            if attempt:
                self.retries += 1
            started = self._clock()
            try:
                watts, status = await asyncio.wait_for(
                    self._read(), self.request_timeout
                )
            except Exception as e:
                self.misses += 1
                if isinstance(e, asyncio.TimeoutError):
                    self.timeouts += 1
                self._log_miss(e)
                continue
            self._store(watts, status, started)
            return

    async def _sample_once(self) -> None:
        # One meter request at a time; a slow one keeps running across polls,
        # and only the poll that started it waits (up to load_timeout).
        if self._fetch_task is not None and not self._fetch_task.done():
            return
        self._fetch_task = asyncio.create_task(self._fetch())
        await asyncio.wait({self._fetch_task}, timeout=self.load_timeout)

    async def get_powermeter_watts(self) -> list[float]:
        grid, _ = await asyncio.gather(
            self.wrapped_powermeter.get_powermeter_watts(), self._sample_once()
        )
        return list(grid)

    async def start(self):
        await self.wrapped_powermeter.start()
        await self.load_powermeter.start()

    async def stop(self):
        if self._fetch_task is not None and not self._fetch_task.done():
            self._fetch_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._fetch_task
        self._fetch_task = None
        await self.load_powermeter.stop()
        await self.wrapped_powermeter.stop()


def find_priority_load(powermeter: Powermeter) -> PriorityLoadPowermeter | None:
    """Return the PriorityLoadPowermeter inside a wrapper chain, if any."""
    pm: Powermeter | None = powermeter
    while pm is not None:
        if isinstance(pm, PriorityLoadPowermeter):
            return pm
        pm = getattr(pm, "wrapped_powermeter", None)
    return None
