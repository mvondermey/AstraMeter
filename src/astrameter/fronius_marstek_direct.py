"""Direct Fronius-to-Marstek controller for a load-position Fronius meter.

The Fronius ``P_Grid`` value is used as the requested Marstek passive power:
positive values discharge the battery, negative values charge it.  This is the
correct relationship when the Fronius Smart Meter does not see the Marstek AC
power, as is typical for a meter configured at the load position.
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import math
import signal
import socket
import threading
import time
import urllib.request
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("astrameter.direct")
SET_MODE_METHOD = "ES.SetMode"
GET_MODE_METHOD = "ES.GetMode"
VALID_MODES = {"auto", "ai", "manual", "passive", "ups"}
MIN_REQUEST_GAP = 10.0
REQUEST_ATTEMPTS = 3


def calculate_target(p_grid: float, deadband: int, max_power: int) -> int:
    """Convert Fronius grid exchange into a bounded Marstek setpoint."""
    if not math.isfinite(p_grid):
        raise ValueError("Fronius P_Grid is not finite")
    if abs(p_grid) < deadband:
        return 0
    return max(-max_power, min(max_power, round(p_grid)))


def apply_min_soc_guard(target: int, soc: float, minimum_soc: float) -> int:
    """Prevent discharge requests at or below the configured reserve SOC."""
    if target > 0 and soc <= minimum_soc:
        return 0
    return target


def required_request_delay(
    last_finished: float, now: float, minimum_gap: float
) -> float:
    """Return the remaining quiet time before another Marstek API request."""
    if last_finished <= 0:
        return 0.0
    return max(0.0, minimum_gap - (now - last_finished))


def extract_p_grid(payload: dict[str, Any]) -> float:
    """Extract and validate P_Grid from a Fronius Solar API response."""
    status = payload.get("Head", {}).get("Status", {})
    if status.get("Code") != 0:
        raise ValueError(f"Fronius API status {status.get('Code')}")
    value = float(payload["Body"]["Data"]["Site"]["P_Grid"])
    if not math.isfinite(value):
        raise ValueError("Fronius P_Grid is not finite")
    return value


def device_matches(payload: dict[str, Any], expected_id: str) -> bool:
    """Match a discovery response by BLE MAC, Wi-Fi MAC, or source suffix."""
    wanted = expected_id.lower().replace(":", "").replace("-", "")
    result = payload.get("result", {})
    candidates = (
        str(result.get("ble_mac", "")),
        str(result.get("wifi_mac", "")),
        str(payload.get("src", "")).rsplit("-", 1)[-1],
    )
    return any(
        candidate.lower().replace(":", "").replace("-", "") == wanted
        for candidate in candidates
    )


class MarstekClient:
    """Small synchronous client that serializes all access to the fragile API."""

    def __init__(
        self,
        device_id: str,
        port: int,
        state_file: Path,
        timeout: float = 1.5,
        minimum_request_gap: float = MIN_REQUEST_GAP,
        request_attempts: int = REQUEST_ATTEMPTS,
    ) -> None:
        self.device_id = device_id
        self.port = port
        self.state_file = state_file
        self.timeout = timeout
        self.minimum_request_gap = minimum_request_gap
        self.request_attempts = request_attempts
        self.ip: str | None = self._load_last_ip()
        self._request_id = 0
        self._last_request_finished = 0.0

    def _load_last_ip(self) -> str | None:
        try:
            value = self.state_file.read_text(encoding="utf-8").strip()
            return str(ipaddress.ip_address(value))
        except (OSError, ValueError):
            return None

    def _next_id(self) -> int:
        self._request_id = (self._request_id + 1) % 2_147_483_647
        return self._request_id

    def request(
        self,
        method: str,
        params: dict[str, Any],
        target: str | None = None,
    ) -> dict[str, Any]:
        destination = target or self.ip
        if not destination:
            raise ConnectionError("Marstek IP is not known")
        if self.request_attempts < 1:
            raise ValueError("request_attempts must be at least 1")
        for attempt in range(1, self.request_attempts + 1):
            delay = required_request_delay(
                self._last_request_finished,
                time.monotonic(),
                self.minimum_request_gap,
            )
            if delay:
                time.sleep(delay)
            request_id = self._next_id()
            message = json.dumps(
                {"id": request_id, "method": method, "params": params},
                separators=(",", ":"),
            ).encode()
            try:
                with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
                    sock.settimeout(self.timeout)
                    sock.sendto(message, (destination, self.port))
                    while True:
                        data, _address = sock.recvfrom(65535)
                        payload = json.loads(data.decode("utf-8"))
                        if payload.get("id") == request_id:
                            return payload
            except TimeoutError:
                if attempt >= self.request_attempts:
                    raise
                LOGGER.warning(
                    "%s timed out (attempt %d/%d); retrying after API gap",
                    method,
                    attempt,
                    self.request_attempts,
                )
            finally:
                self._last_request_finished = time.monotonic()
        raise RuntimeError("unreachable request retry state")

    def ensure_ip(self) -> str:
        """Validate the cached address without broadcasting on the LAN."""
        if not self.ip:
            raise ConnectionError("Marstek IP is not known; state file is empty")
        reply = self.request("Wifi.GetStatus", {"id": 0})
        if not device_matches(reply, self.device_id):
            raise ConnectionError(f"Unexpected device at {self.ip}")
        return self.ip

    def set_passive(self, power: int, duration: int) -> bool:
        config = {
            "mode": "Passive",
            "passive_cfg": {"power": power, "cd_time": duration},
        }
        reply = self.request(SET_MODE_METHOD, {"id": 0, "config": config})
        return reply.get("result", {}).get("set_result") is True

    def get_mode(self) -> dict[str, Any]:
        """Read and validate mode state before issuing a write command."""
        reply = self.request(GET_MODE_METHOD, {"id": 0})
        if not device_matches(reply, self.device_id):
            raise ConnectionError("ES.GetMode response came from an unexpected device")
        result = reply.get("result")
        if not isinstance(result, dict) or result.get("id") != 0:
            raise ValueError("ES.GetMode returned an invalid result")
        mode = result.get("mode")
        if not isinstance(mode, str) or mode.lower() not in VALID_MODES:
            raise ValueError(f"ES.GetMode returned an invalid mode: {mode!r}")
        soc = result.get("bat_soc")
        if not isinstance(soc, (int, float)) or not 0 <= soc <= 100:
            raise ValueError(f"ES.GetMode returned an invalid SOC: {soc!r}")
        return result


def read_fronius(host: str, timeout: float = 3.0) -> float:
    url = f"http://{host}/solar_api/v1/GetPowerFlowRealtimeData.fcgi"
    with urllib.request.urlopen(url, timeout=timeout) as response:
        payload = json.load(response)
    return extract_p_grid(payload)


def configure_logging(log_file: Path, verbose: bool) -> None:
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file, maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(message)s")
    handler.setFormatter(formatter)
    console = logging.StreamHandler()
    console.setFormatter(formatter)
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        handlers=[handler, console],
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--fronius-host", default="pv.fritz.box")
    parser.add_argument("--device-id", default="5037cd7f1d02")
    parser.add_argument("--port", type=int, default=30000)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--deadband", type=int, default=50)
    parser.add_argument("--max-power", type=int, default=2500)
    parser.add_argument("--command-ttl", type=int, default=45)
    parser.add_argument("--min-soc", type=float, default=12.0)
    parser.add_argument("--reserve-interval", type=float, default=60.0)
    parser.add_argument("--once", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--verbose", action="store_true")
    parser.add_argument("--state-file", type=Path, default=Path(".marstek-direct-ip"))
    parser.add_argument(
        "--log-file", type=Path, default=Path("fronius-marstek-direct.log")
    )
    return parser


def run(args: argparse.Namespace) -> int:
    configure_logging(args.log_file, args.verbose)
    stop_event = threading.Event()

    def request_stop(_signum: int, _frame: object) -> None:
        stop_event.set()

    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)
    client = MarstekClient(args.device_id, args.port, args.state_file)
    failures = 0
    needs_probe = False
    reserve_hold = False

    try:
        marstek_ip = client.ensure_ip()
        LOGGER.info("Marstek %s found at %s:%d", args.device_id, marstek_ip, args.port)
        # Venus E 3.0 firmware is sensitive to back-to-back UDP requests.
        # Leave a full device processing window after the startup identity check.
        time.sleep(5.0)
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        LOGGER.warning("Initial Marstek validation failed: %s", exc)
        needs_probe = True

    while not stop_event.is_set():
        started = time.monotonic()
        try:
            if needs_probe:
                client.ensure_ip()
                LOGGER.info("Marstek API probe succeeded; writing resumes next cycle")
                needs_probe = False
                failures = 0
            else:
                p_grid = read_fronius(args.fronius_host)
                target = calculate_target(p_grid, args.deadband, args.max_power)
                if args.dry_run:
                    LOGGER.info("dry-run P_Grid=%.0fW target=%dW", p_grid, target)
                else:
                    try:
                        mode_status = client.get_mode()
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        raise RuntimeError(f"ES.GetMode failed: {exc}") from exc
                    LOGGER.debug(
                        "ES.GetMode mode=%s ongrid_power=%sW soc=%s%%",
                        mode_status["mode"],
                        mode_status.get("ongrid_power"),
                        mode_status.get("bat_soc"),
                    )
                    soc = float(mode_status["bat_soc"])
                    guarded_target = apply_min_soc_guard(target, soc, args.min_soc)
                    if guarded_target == 0 and target > 0:
                        if not reserve_hold:
                            try:
                                accepted = client.set_passive(0, 10)
                            except (
                                OSError,
                                ValueError,
                                json.JSONDecodeError,
                            ) as exc:
                                raise RuntimeError(
                                    f"ES.SetMode reserve stop failed: {exc}"
                                ) from exc
                            if not accepted:
                                raise RuntimeError(
                                    "Marstek rejected reserve 0W ES.SetMode"
                                )
                            LOGGER.info(
                                "reserve hold entered P_Grid=%.0fW previous_output=%sW "
                                "soc=%s%% min_soc=%s%%",
                                p_grid,
                                mode_status.get("ongrid_power"),
                                mode_status.get("bat_soc"),
                                args.min_soc,
                            )
                        else:
                            LOGGER.info(
                                "reserve hold active P_Grid=%.0fW output=%sW soc=%s%%",
                                p_grid,
                                mode_status.get("ongrid_power"),
                                mode_status.get("bat_soc"),
                            )
                        reserve_hold = True
                    else:
                        reserve_hold = False
                        try:
                            accepted = client.set_passive(
                                guarded_target, args.command_ttl
                            )
                        except (OSError, ValueError, json.JSONDecodeError) as exc:
                            raise RuntimeError(f"ES.SetMode failed: {exc}") from exc
                        if not accepted:
                            raise RuntimeError("Marstek rejected ES.SetMode")
                        LOGGER.info(
                            "cycle ok P_Grid=%.0fW mode=%s previous_output=%sW "
                            "target=%dW soc=%s%%",
                            p_grid,
                            mode_status["mode"],
                            mode_status.get("ongrid_power"),
                            guarded_target,
                            mode_status.get("bat_soc"),
                        )
                failures = 0
        except (
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            failures += 1
            needs_probe = True
            LOGGER.warning("Control cycle failed (%d): %s", failures, exc)
        if args.once:
            break
        cycle_interval = args.reserve_interval if reserve_hold else args.interval
        stop_event.wait(max(0.2, cycle_interval - (time.monotonic() - started)))

    if not args.dry_run and client.ip:
        try:
            time.sleep(5.0)
            client.set_passive(0, 10)
            LOGGER.info("Controller stopped; Marstek setpoint reset to 0W")
        except (OSError, ValueError, json.JSONDecodeError):
            LOGGER.warning(
                "Could not send final 0W setpoint; previous command will expire"
            )
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
