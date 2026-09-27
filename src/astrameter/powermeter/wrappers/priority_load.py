import asyncio
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

    The consumer meter gets at most ``load_timeout`` seconds so it never delays
    a battery poll; if it misses, the last reading is reused while younger than
    ``max_age``, else 0 W is assumed (batteries behave as without this feature).
    """

    def __init__(
        self,
        wrapped_powermeter: Powermeter,
        load_powermeter: Powermeter,
        min_watts: float = 50.0,
        max_age: float = 2.0,
        load_timeout: float = 0.5,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        super().__init__(wrapped_powermeter)
        self.load_powermeter = load_powermeter
        self.min_watts = min_watts
        self.max_age = max_age
        self.load_timeout = load_timeout
        self._clock = clock
        self._load_watts = 0.0
        self._load_at: float | None = None
        self._last_error_log = 0.0
        # Raw meter status (e.g. Shelly Switch.GetStatus) so other local tools
        # can read the consumer from here instead of polling the meter again.
        self._status: dict | None = None
        # Reliability counters (since start): misses = failed samples, of which
        # timeouts hit load_timeout (slow meter, not an error reply);
        # stale_events = gaps between good samples longer than max_age, i.e.
        # moments where load_watts() fell back to 0 W.
        self.samples_ok = 0
        self.misses = 0
        self.timeouts = 0
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
            "stale_events": self.stale_events,
            "max_gap_s": self.max_gap,
            "min_watts": self.min_watts,
            "max_age": self.max_age,
        }

    async def _read(self) -> float:
        get_status = getattr(self.load_powermeter, "get_status", None)
        if get_status is not None:
            status = await get_status()
            self._status = status
            return float(status["apower"])
        values = await self.load_powermeter.get_powermeter_watts()
        return float(sum(values))

    async def _sample_once(self) -> None:
        try:
            watts = await asyncio.wait_for(self._read(), self.load_timeout)
            now = self._clock()
            if self._load_at is not None:
                gap = now - self._load_at
                self.max_gap = max(self.max_gap, gap)
                if gap > self.max_age:
                    self.stale_events += 1
            self._load_watts = watts
            self._load_at = now
            self.samples_ok += 1
        except Exception as e:
            self.misses += 1
            if isinstance(e, TimeoutError):
                self.timeouts += 1
            now = self._clock()
            if now - self._last_error_log > 300:
                self._last_error_log = now
                logger.warning(
                    f"Priority load meter unreachable, counting 0 W: {e!r} "
                    f"(since start: {self.misses} misses, {self.timeouts} timeouts, "
                    f"{self.stale_events} "
                    f"stale gaps, longest gap {self.max_gap:.1f} s)",
                    exc_info=False,
                )

    async def get_powermeter_watts(self) -> list[float]:
        grid, _ = await asyncio.gather(
            self.wrapped_powermeter.get_powermeter_watts(), self._sample_once()
        )
        return list(grid)

    async def start(self):
        await self.wrapped_powermeter.start()
        await self.load_powermeter.start()

    async def stop(self):
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
