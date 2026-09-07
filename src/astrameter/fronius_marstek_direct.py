"""Direct Fronius-to-Marstek controller.

The Fronius ``P_Grid`` value is used as the requested Marstek passive power:
positive values discharge the battery, negative values charge it.  This is the
correct relationship when the Fronius Smart Meter does not see the Marstek AC
power, as is typical for a meter configured at the load position.  When the
meter does see the battery, ``--meter-sees-battery`` enables closed-loop
feedback based on the battery's reported output plus the remaining grid error.
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


class FroniusReadError(RuntimeError):
    """A Fronius read failed without implying a Marstek connectivity problem."""


def calculate_target(p_grid: float, deadband: int, max_power: int) -> int:
    """Convert Fronius grid exchange into a bounded Marstek setpoint."""
    if not math.isfinite(p_grid):
        raise ValueError("Fronius P_Grid is not finite")
    if abs(p_grid) < deadband:
        return 0
    return max(-max_power, min(max_power, round(p_grid)))


def calculate_feedback_target(
    p_grid: float,
    current_output: float,
    deadband: int,
    max_power: int,
    gain: float = 1.0,
) -> int:
    """Correct the current battery output by the measured grid error.

    Both values use the same sign convention: positive discharges and negative
    charges.  Inside the grid deadband the existing output is held instead of
    being reset to zero.
    """
    if not math.isfinite(p_grid):
        raise ValueError("Fronius P_Grid is not finite")
    if not math.isfinite(current_output):
        raise ValueError("Marstek ongrid_power is not finite")
    if not math.isfinite(gain) or not 0 < gain <= 1:
        raise ValueError("feedback gain must be greater than zero and at most one")
    correction = 0.0 if abs(p_grid) < deadband else p_grid
    target = round(current_output + gain * correction)
    return max(-max_power, min(max_power, target))


def calculate_feedback_targets(
    p_grid: float,
    current_outputs: list[float],
    deadband: int,
    max_power: int,
    gain: float = 1.0,
) -> list[int]:
    """Correct each battery without commanding opposing power directions."""
    if not current_outputs:
        raise ValueError("current_outputs must contain at least one battery")
    if not math.isfinite(p_grid):
        raise ValueError("Fronius P_Grid is not finite")
    if not all(math.isfinite(output) for output in current_outputs):
        raise ValueError("Marstek ongrid_power is not finite")
    if not math.isfinite(gain) or not 0 < gain <= 1:
        raise ValueError("feedback gain must be greater than zero and at most one")
    correction = 0.0 if abs(p_grid) < deadband else gain * p_grid
    correction_per_battery = correction / len(current_outputs)
    targets = [
        max(-max_power, min(max_power, round(output + correction_per_battery)))
        for output in current_outputs
    ]
    aggregate_target = sum(current_outputs) + correction
    if aggregate_target < 0:
        return [min(0, target) for target in targets]
    if aggregate_target > 0:
        return [max(0, target) for target in targets]
    return [0 for _target in targets]


def distribute_target(
    total_target: int, battery_count: int, max_power: int
) -> list[int]:
    """Split one aggregate target evenly across identical batteries."""
    if battery_count < 1:
        raise ValueError("battery_count must be at least 1")
    if max_power < 1:
        raise ValueError("max_power must be at least 1")
    bounded = max(
        -battery_count * max_power, min(battery_count * max_power, total_target)
    )
    sign = -1 if bounded < 0 else 1
    magnitude = abs(bounded)
    base, remainder = divmod(magnitude, battery_count)
    return [
        sign * (base + (1 if index < remainder else 0))
        for index in range(battery_count)
    ]


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
        self._socket: socket.socket | None = None

    def _get_socket(self) -> socket.socket:
        """Return the single source-port-pinned socket used by this client."""
        if self._socket is None:
            udp_socket = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                # Venus E replies reliably only when the client listens on the
                # configured OpenAPI port as well as sending to that port.
                udp_socket.bind(("0.0.0.0", self.port))
                udp_socket.settimeout(self.timeout)
            except BaseException:
                udp_socket.close()
                raise
            self._socket = udp_socket
        return self._socket

    def close(self) -> None:
        """Release the local OpenAPI UDP port."""
        if self._socket is not None:
            self._socket.close()
            self._socket = None

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
                sock = self._get_socket()
                sock.sendto(message, (destination, self.port))
                while True:
                    data, _address = sock.recvfrom(65535)
                    payload = json.loads(data.decode("utf-8"))
                    if payload.get("id") == request_id:
                        return payload
            except TimeoutError:
                if attempt >= self.request_attempts:
                    # A Venus E may stop replying to an otherwise valid,
                    # source-port-pinned UDP socket while remaining reachable
                    # in the app and via ICMP.  Do not carry that socket into
                    # the next control cycle; the next request will bind a
                    # fresh socket to the configured OpenAPI port.
                    self.close()
                    raise
                LOGGER.warning(
                    "%s to %s timed out (attempt %d/%d); retrying after API gap",
                    method,
                    destination,
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

    def ensure_device(self, target: str, expected_id: str) -> str:
        """Validate a fixed-IP additional battery without LAN broadcast."""
        target = str(ipaddress.ip_address(target))
        reply = self.request("Wifi.GetStatus", {"id": 0}, target=target)
        if not device_matches(reply, expected_id):
            raise ConnectionError(f"Unexpected device at {target}")
        return target

    def set_passive(
        self,
        power: int,
        duration: int,
        target: str | None = None,
        expected_id: str | None = None,
    ) -> bool:
        config = {
            "mode": "Passive",
            "passive_cfg": {"power": power, "cd_time": duration},
        }
        reply = self.request(
            SET_MODE_METHOD, {"id": 0, "config": config}, target=target
        )
        if expected_id and not device_matches(reply, expected_id):
            raise ConnectionError("ES.SetMode response came from an unexpected device")
        return reply.get("result", {}).get("set_result") is True

    def get_mode(
        self, target: str | None = None, expected_id: str | None = None
    ) -> dict[str, Any]:
        """Read and validate mode state before issuing a write command."""
        reply = self.request(GET_MODE_METHOD, {"id": 0}, target=target)
        if not device_matches(reply, expected_id or self.device_id):
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
    parser.add_argument(
        "--additional-battery",
        action="append",
        default=[],
        metavar="IP,DEVICE_ID",
        help="additional Marstek at a fixed IP (repeatable)",
    )
    parser.add_argument("--port", type=int, default=30000)
    parser.add_argument("--interval", type=float, default=5.0)
    parser.add_argument("--api-request-gap", type=float, default=MIN_REQUEST_GAP)
    parser.add_argument("--deadband", type=int, default=50)
    parser.add_argument("--max-power", type=int, default=2500)
    parser.add_argument("--command-ttl", type=int, default=45)
    parser.add_argument(
        "--meter-sees-battery",
        action="store_true",
        help="use battery output plus Fronius grid error as the next setpoint",
    )
    parser.add_argument(
        "--feedback-gain",
        type=float,
        default=1.0,
        help="fraction of each measured grid error applied in closed-loop mode",
    )
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
    if args.api_request_gap <= 0:
        raise ValueError("--api-request-gap must be greater than zero")
    if not 0 < args.feedback_gain <= 1:
        raise ValueError("--feedback-gain must be greater than zero and at most one")
    client = MarstekClient(
        args.device_id,
        args.port,
        args.state_file,
        minimum_request_gap=args.api_request_gap,
    )
    failures = 0
    additional_batteries: list[tuple[str, str]] = []
    batteries: list[tuple[str, str]] = []

    for value in args.additional_battery:
        try:
            battery_ip, battery_id = (part.strip() for part in value.split(",", 1))
            battery_ip = str(ipaddress.ip_address(battery_ip))
            if not battery_id:
                raise ValueError
        except ValueError as exc:
            raise ValueError("--additional-battery must use IP,DEVICE_ID") from exc
        additional_batteries.append((battery_ip, battery_id))

    if not client.ip:
        raise ConnectionError("Marstek IP is not known; state file is empty")
    batteries = [(client.ip, args.device_id), *additional_batteries]
    for index, (battery_ip, battery_id) in enumerate(batteries):
        try:
            if index == 0:
                client.ensure_ip()
            else:
                client.ensure_device(battery_ip, battery_id)
            LOGGER.info("Marstek %s found at %s:%d", battery_id, battery_ip, args.port)
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.warning(
                "Initial validation failed for Marstek %s at %s: %s; "
                "direct mode checks will continue",
                battery_id,
                battery_ip,
                exc,
            )
    # Venus E 3.0 firmware is sensitive to back-to-back UDP requests.
    # Leave a full device processing window after the startup identity checks.
    time.sleep(5.0)

    while not stop_event.is_set():
        started = time.monotonic()
        try:
            try:
                p_grid = read_fronius(args.fronius_host)
            except (
                OSError,
                ValueError,
                KeyError,
                json.JSONDecodeError,
            ) as exc:
                raise FroniusReadError(str(exc)) from exc
            if args.dry_run:
                target = calculate_target(p_grid, args.deadband, args.max_power)
                LOGGER.info("dry-run P_Grid=%.0fW target=%dW", p_grid, target)
            else:
                active_batteries: list[tuple[str, str]] = []
                mode_statuses: list[dict[str, Any]] = []
                for battery_ip, battery_id in batteries:
                    try:
                        if len(batteries) == 1:
                            status = client.get_mode()
                        else:
                            status = client.get_mode(
                                target=battery_ip, expected_id=battery_id
                            )
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        LOGGER.warning(
                            "Skipping Marstek %s at %s after ES.GetMode failed: %s",
                            battery_id,
                            battery_ip,
                            exc,
                        )
                        continue
                    active_batteries.append((battery_ip, battery_id))
                    mode_statuses.append(status)
                if not active_batteries:
                    raise RuntimeError("ES.GetMode failed for every configured battery")

                current_outputs = [
                    float(status.get("ongrid_power", 0)) for status in mode_statuses
                ]
                total_limit = args.max_power * len(active_batteries)
                if args.meter_sees_battery:
                    targets = calculate_feedback_targets(
                        p_grid,
                        current_outputs,
                        args.deadband,
                        args.max_power,
                        args.feedback_gain,
                    )
                else:
                    target = calculate_target(p_grid, args.deadband, total_limit)
                    targets = distribute_target(
                        target, len(active_batteries), args.max_power
                    )

                accepted_batteries: list[tuple[str, str]] = []
                for (battery_ip, battery_id), battery_target in zip(
                    active_batteries, targets, strict=True
                ):
                    try:
                        if len(batteries) == 1:
                            accepted = client.set_passive(
                                battery_target, args.command_ttl
                            )
                        else:
                            accepted = client.set_passive(
                                battery_target,
                                args.command_ttl,
                                target=battery_ip,
                                expected_id=battery_id,
                            )
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        LOGGER.warning(
                            "Skipping Marstek %s at %s after ES.SetMode failed: %s",
                            battery_id,
                            battery_ip,
                            exc,
                        )
                        continue
                    if not accepted:
                        LOGGER.warning(
                            "Marstek %s at %s rejected ES.SetMode",
                            battery_id,
                            battery_ip,
                        )
                        continue
                    accepted_batteries.append((battery_ip, battery_id))
                if not accepted_batteries:
                    raise RuntimeError("ES.SetMode failed for every available battery")
                LOGGER.info(
                    "cycle ok P_Grid=%.0fW batteries=%s modes=%s "
                    "previous_outputs=%sW targets=%sW socs=%s%%",
                    p_grid,
                    [battery_ip for battery_ip, _battery_id in active_batteries],
                    [status["mode"] for status in mode_statuses],
                    current_outputs,
                    targets,
                    [status.get("bat_soc") for status in mode_statuses],
                )
            failures = 0
        except FroniusReadError as exc:
            failures += 1
            LOGGER.warning("Fronius read failed (%d): %s", failures, exc)
        except (
            OSError,
            ValueError,
            KeyError,
            json.JSONDecodeError,
            RuntimeError,
        ) as exc:
            failures += 1
            LOGGER.warning("Control cycle failed (%d): %s", failures, exc)
        if args.once:
            break
        stop_event.wait(max(0.2, args.interval - (time.monotonic() - started)))

    try:
        if not args.dry_run and client.ip:
            time.sleep(5.0)
            reset_count = 0
            for battery_ip, battery_id in batteries:
                try:
                    if len(batteries) == 1:
                        client.set_passive(0, 10)
                    else:
                        client.set_passive(
                            0, 10, target=battery_ip, expected_id=battery_id
                        )
                    reset_count += 1
                except (OSError, ValueError, json.JSONDecodeError):
                    LOGGER.warning(
                        "Could not reset Marstek %s at %s to 0W; "
                        "previous command will expire",
                        battery_id,
                        battery_ip,
                    )
            LOGGER.info(
                "Controller stopped; reset %d/%d Marstek setpoints to 0W",
                reset_count,
                len(batteries),
            )
    finally:
        client.close()
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
