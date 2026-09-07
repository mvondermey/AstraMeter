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
from concurrent.futures import ThreadPoolExecutor
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger("astrameter.direct")
SET_MODE_METHOD = "ES.SetMode"
GET_MODE_METHOD = "ES.GetMode"
VALID_MODES = {"auto", "ai", "manual", "passive", "ups"}
MIN_REQUEST_GAP = 10.0
REQUEST_ATTEMPTS = 3
MIN_CONSTRAINT_TARGET_W = 500


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


def battery_is_constrained(
    current_output: float,
    previous_target: int | None,
    direction: int,
    deadband: int,
) -> bool:
    """Return whether a battery delivered less than half its prior command."""
    if previous_target is None or direction == 0:
        return False
    return (
        previous_target * direction >= max(MIN_CONSTRAINT_TARGET_W, deadband * 4)
        and abs(current_output) < abs(previous_target) * 0.5
    )


def calculate_feedback_targets(
    p_grid: float,
    current_outputs: list[float],
    deadband: int,
    max_power: int,
    gain: float = 1.0,
    previous_targets: list[int | None] | None = None,
) -> list[int]:
    """Correct each battery and move rejected power to responsive batteries.

    A battery is treated as constrained when it delivers less than half of a
    substantial previous command. This lets a full/empty battery drop out of
    the allocation on the next cycle instead of receiving an equal share of
    every correction indefinitely.
    """
    if not current_outputs:
        raise ValueError("current_outputs must contain at least one battery")
    if not math.isfinite(p_grid):
        raise ValueError("Fronius P_Grid is not finite")
    if not all(math.isfinite(output) for output in current_outputs):
        raise ValueError("Marstek ongrid_power is not finite")
    if not math.isfinite(gain) or not 0 < gain <= 1:
        raise ValueError("feedback gain must be greater than zero and at most one")
    if previous_targets is not None and len(previous_targets) != len(current_outputs):
        raise ValueError("previous_targets must match current_outputs")
    correction = 0.0 if abs(p_grid) < deadband else gain * p_grid
    if previous_targets is None or all(target is None for target in previous_targets):
        correction_per_battery = correction / len(current_outputs)
        initial_targets = [
            max(-max_power, min(max_power, round(output + correction_per_battery)))
            for output in current_outputs
        ]
        initial_aggregate = sum(current_outputs) + correction
        if initial_aggregate < 0:
            return [min(0, target) for target in initial_targets]
        if initial_aggregate > 0:
            return [max(0, target) for target in initial_targets]
        return [0 for _target in initial_targets]

    aggregate_target = max(
        -len(current_outputs) * max_power,
        min(len(current_outputs) * max_power, sum(current_outputs) + correction),
    )
    if aggregate_target == 0:
        return [0 for _output in current_outputs]

    direction = -1 if aggregate_target < 0 else 1
    candidates = list(range(len(current_outputs)))
    if previous_targets is not None:
        responsive: list[int] = []
        for index, (output, previous) in enumerate(
            zip(current_outputs, previous_targets, strict=True)
        ):
            if not battery_is_constrained(output, previous, direction, deadband):
                responsive.append(index)
        # Keep probing all batteries if none responded; firmware may simply be
        # ramping, or every battery may currently be at its SOC boundary.
        if responsive:
            candidates = responsive

    # Rebalance the complete aggregate target on every established feedback
    # cycle.  Using each battery's current output as its baseline would only
    # distribute the latest grid correction.  After a temporarily unavailable
    # battery returned, that left the surviving battery carrying all power and
    # the recovered battery permanently at zero.
    targets = [0 for _output in current_outputs]
    candidate_targets = distribute_target(
        round(aggregate_target), len(candidates), max_power
    )
    for index, target in zip(candidates, candidate_targets, strict=True):
        targets[index] = target
    return targets


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

    def request_many(
        self,
        method: str,
        requests: list[tuple[str, dict[str, Any]]],
    ) -> list[dict[str, Any] | Exception]:
        """Send one request per battery together and demultiplex the replies."""
        if not requests:
            return []
        if len({target for target, _params in requests}) != len(requests):
            raise ValueError("parallel requests require unique target addresses")
        if self.request_attempts < 1:
            raise ValueError("request_attempts must be at least 1")

        results: list[dict[str, Any] | Exception | None] = [None] * len(requests)
        pending = set(range(len(requests)))
        for attempt in range(1, self.request_attempts + 1):
            delay = required_request_delay(
                self._last_request_finished,
                time.monotonic(),
                self.minimum_request_gap,
            )
            if delay:
                time.sleep(delay)

            sock = self._get_socket()
            request_ids: dict[int, tuple[int, str]] = {}
            for index in tuple(pending):
                target, params = requests[index]
                request_id = self._next_id()
                message = json.dumps(
                    {"id": request_id, "method": method, "params": params},
                    separators=(",", ":"),
                ).encode()
                try:
                    sock.sendto(message, (target, self.port))
                    request_ids[request_id] = (index, target)
                except OSError as exc:
                    results[index] = exc
                    pending.remove(index)

            deadline = time.monotonic() + self.timeout
            while request_ids and time.monotonic() < deadline:
                sock.settimeout(max(0.01, deadline - time.monotonic()))
                try:
                    data, address = sock.recvfrom(65535)
                except TimeoutError:
                    break
                try:
                    payload = json.loads(data.decode("utf-8"))
                except (UnicodeDecodeError, json.JSONDecodeError):
                    continue
                response_id = payload.get("id")
                if not isinstance(response_id, int):
                    continue
                match = request_ids.get(response_id)
                if match is None or address[0] != match[1]:
                    continue
                index, _target = match
                results[index] = payload
                pending.remove(index)
                del request_ids[response_id]

            sock.settimeout(self.timeout)
            self._last_request_finished = time.monotonic()
            if not pending:
                break
            if attempt < self.request_attempts:
                for index in pending:
                    target, _params = requests[index]
                    LOGGER.warning(
                        "%s to %s timed out (attempt %d/%d); retrying after API gap",
                        method,
                        target,
                        attempt,
                        self.request_attempts,
                    )

        if pending:
            self.close()
            for index in pending:
                results[index] = TimeoutError("timed out")
        return [
            result if result is not None else RuntimeError("missing request result")
            for result in results
        ]

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

    def set_passives(
        self, commands: list[tuple[str, str, int, int]]
    ) -> list[bool | Exception]:
        """Set multiple batteries concurrently through the shared UDP socket."""
        replies = self.request_many(
            SET_MODE_METHOD,
            [
                (
                    target,
                    {
                        "id": 0,
                        "config": {
                            "mode": "Passive",
                            "passive_cfg": {"power": power, "cd_time": duration},
                        },
                    },
                )
                for target, _expected_id, power, duration in commands
            ],
        )
        results: list[bool | Exception] = []
        for reply, (_target, expected_id, _power, _duration) in zip(
            replies, commands, strict=True
        ):
            if isinstance(reply, Exception):
                results.append(reply)
            elif not device_matches(reply, expected_id):
                results.append(ConnectionError("Unexpected ES.SetMode device response"))
            else:
                results.append(reply.get("result", {}).get("set_result") is True)
        return results

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

    def get_modes(
        self, batteries: list[tuple[str, str]]
    ) -> list[dict[str, Any] | Exception]:
        """Read multiple batteries concurrently through the shared UDP socket."""
        replies = self.request_many(
            GET_MODE_METHOD,
            [(target, {"id": 0}) for target, _expected_id in batteries],
        )
        results: list[dict[str, Any] | Exception] = []
        for reply, (_target, expected_id) in zip(replies, batteries, strict=True):
            if isinstance(reply, Exception):
                results.append(reply)
                continue
            try:
                if not device_matches(reply, expected_id):
                    raise ConnectionError("Unexpected ES.GetMode device response")
                result = reply.get("result")
                if not isinstance(result, dict) or result.get("id") != 0:
                    raise ValueError("ES.GetMode returned an invalid result")
                mode = result.get("mode")
                if not isinstance(mode, str) or mode.lower() not in VALID_MODES:
                    raise ValueError(f"ES.GetMode returned an invalid mode: {mode!r}")
                soc = result.get("bat_soc")
                if not isinstance(soc, (int, float)) or not 0 <= soc <= 100:
                    raise ValueError(f"ES.GetMode returned an invalid SOC: {soc!r}")
                results.append(result)
            except (ConnectionError, ValueError) as exc:
                results.append(exc)
        return results


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
        metavar="IP,DEVICE_ID[,PORT]",
        help="additional Marstek at a fixed IP and optional API port (repeatable)",
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
    previous_targets: dict[tuple[str, str], int] = {}
    additional_batteries: list[tuple[str, str]] = []
    batteries: list[tuple[str, str]] = []
    battery_ports: dict[tuple[str, str], int] = {}

    for value in args.additional_battery:
        try:
            parts = [part.strip() for part in value.split(",")]
            if len(parts) not in (2, 3):
                raise ValueError
            battery_ip, battery_id = parts[:2]
            battery_ip = str(ipaddress.ip_address(battery_ip))
            if not battery_id:
                raise ValueError
            battery_port = int(parts[2]) if len(parts) == 3 else args.port
            if not 1 <= battery_port <= 65535:
                raise ValueError
        except ValueError as exc:
            raise ValueError(
                "--additional-battery must use IP,DEVICE_ID[,PORT]"
            ) from exc
        battery = (battery_ip, battery_id)
        additional_batteries.append(battery)
        battery_ports[battery] = battery_port

    if not client.ip:
        raise ConnectionError("Marstek IP is not known; state file is empty")
    batteries = [(client.ip, args.device_id), *additional_batteries]
    battery_ports[(client.ip, args.device_id)] = args.port
    clients_by_port = {args.port: client}
    for battery in additional_batteries:
        battery_port = battery_ports[battery]
        if battery_port not in clients_by_port:
            clients_by_port[battery_port] = MarstekClient(
                battery[1],
                battery_port,
                args.state_file,
                minimum_request_gap=args.api_request_gap,
            )
    for index, (battery_ip, battery_id) in enumerate(batteries):
        battery_port = battery_ports[(battery_ip, battery_id)]
        battery_client = clients_by_port[battery_port]
        try:
            if index == 0:
                battery_client.ensure_ip()
            else:
                battery_client.ensure_device(battery_ip, battery_id)
            LOGGER.info(
                "Marstek %s found at %s:%d", battery_id, battery_ip, battery_port
            )
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
    executor = ThreadPoolExecutor(max_workers=len(clients_by_port))

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
                if len(batteries) == 1:
                    try:
                        mode_results: list[dict[str, Any] | Exception] = [
                            client.get_mode()
                        ]
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        mode_results = [exc]
                elif len(clients_by_port) > 1:
                    mode_results = [RuntimeError("mode not read")] * len(batteries)
                    mode_groups: dict[int, list[tuple[int, tuple[str, str]]]] = {}
                    for battery_index, battery in enumerate(batteries):
                        mode_groups.setdefault(battery_ports[battery], []).append(
                            (battery_index, battery)
                        )
                    mode_futures = {
                        executor.submit(
                            clients_by_port[port].get_modes,
                            [battery for _index, battery in members],
                        ): members
                        for port, members in mode_groups.items()
                    }
                    for future, members in mode_futures.items():
                        try:
                            group_results = future.result()
                        except Exception as exc:
                            group_results = [exc] * len(members)
                        for (battery_index, _battery), result in zip(
                            members, group_results, strict=True
                        ):
                            mode_results[battery_index] = result
                else:
                    mode_results = client.get_modes(batteries)
                for (battery_ip, battery_id), status in zip(
                    batteries, mode_results, strict=True
                ):
                    if isinstance(status, Exception):
                        LOGGER.warning(
                            "Skipping Marstek %s at %s after ES.GetMode failed: %s",
                            battery_id,
                            battery_ip,
                            status,
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
                        [previous_targets.get(battery) for battery in active_batteries],
                    )
                else:
                    target = calculate_target(p_grid, args.deadband, total_limit)
                    targets = distribute_target(
                        target, len(active_batteries), args.max_power
                    )

                target_direction = (
                    -1 if sum(targets) < 0 else 1 if sum(targets) > 0 else 0
                )
                if len(batteries) == 1:
                    try:
                        set_results: list[bool | Exception] = [
                            client.set_passive(targets[0], args.command_ttl)
                        ]
                    except (OSError, ValueError, json.JSONDecodeError) as exc:
                        set_results = [exc]
                elif len(clients_by_port) > 1:
                    set_results = [RuntimeError("setpoint not sent")] * len(
                        active_batteries
                    )
                    command_groups: dict[
                        int, list[tuple[int, tuple[str, str], int]]
                    ] = {}
                    for command_index, (battery, battery_target) in enumerate(
                        zip(active_batteries, targets, strict=True)
                    ):
                        command_groups.setdefault(battery_ports[battery], []).append(
                            (command_index, battery, battery_target)
                        )
                    command_futures = {
                        executor.submit(
                            clients_by_port[port].set_passives,
                            [
                                (
                                    battery[0],
                                    battery[1],
                                    battery_target,
                                    args.command_ttl,
                                )
                                for _index, battery, battery_target in members
                            ],
                        ): members
                        for port, members in command_groups.items()
                    }
                    for future, members in command_futures.items():
                        try:
                            group_results = future.result()
                        except Exception as exc:
                            group_results = [exc] * len(members)
                        for (command_index, _battery, _target), result in zip(
                            members, group_results, strict=True
                        ):
                            set_results[command_index] = result
                else:
                    set_results = client.set_passives(
                        [
                            (battery_ip, battery_id, battery_target, args.command_ttl)
                            for (battery_ip, battery_id), battery_target in zip(
                                active_batteries, targets, strict=True
                            )
                        ]
                    )

                accepted_batteries: list[tuple[str, str]] = []
                for (
                    (battery_ip, battery_id),
                    battery_target,
                    current_output,
                    accepted,
                ) in zip(
                    active_batteries,
                    targets,
                    current_outputs,
                    set_results,
                    strict=True,
                ):
                    if isinstance(accepted, Exception):
                        LOGGER.warning(
                            "Skipping Marstek %s at %s after ES.SetMode failed: %s",
                            battery_id,
                            battery_ip,
                            accepted,
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
                    battery = (battery_ip, battery_id)
                    if not battery_is_constrained(
                        current_output,
                        previous_targets.get(battery),
                        target_direction,
                        args.deadband,
                    ):
                        previous_targets[battery] = battery_target
                if not accepted_batteries:
                    raise RuntimeError("ES.SetMode failed for every available battery")
                LOGGER.info(
                    "cycle ok P_Grid=%.0fW batteries=%s modes=%s "
                    "previous_outputs=%sW targets=%sW socs=%s%%",
                    p_grid,
                    [
                        f"{battery_ip}:{battery_ports[(battery_ip, battery_id)]}"
                        for battery_ip, battery_id in active_batteries
                    ],
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
            if len(batteries) == 1:
                try:
                    reset_results: list[bool | Exception] = [client.set_passive(0, 10)]
                except (OSError, ValueError, json.JSONDecodeError):
                    reset_results = [TimeoutError("reset failed")]
            elif len(clients_by_port) > 1:
                reset_results = [RuntimeError("reset not sent")] * len(batteries)
                reset_groups: dict[int, list[tuple[int, tuple[str, str]]]] = {}
                for battery_index, battery in enumerate(batteries):
                    reset_groups.setdefault(battery_ports[battery], []).append(
                        (battery_index, battery)
                    )
                reset_futures = {
                    executor.submit(
                        clients_by_port[port].set_passives,
                        [
                            (battery[0], battery[1], 0, 10)
                            for _index, battery in members
                        ],
                    ): members
                    for port, members in reset_groups.items()
                }
                for future, members in reset_futures.items():
                    try:
                        group_results = future.result()
                    except Exception as exc:
                        group_results = [exc] * len(members)
                    for (battery_index, _battery), result in zip(
                        members, group_results, strict=True
                    ):
                        reset_results[battery_index] = result
            else:
                reset_results = client.set_passives(
                    [
                        (battery_ip, battery_id, 0, 10)
                        for battery_ip, battery_id in batteries
                    ]
                )
            for (battery_ip, battery_id), reset_result in zip(
                batteries, reset_results, strict=True
            ):
                if isinstance(reset_result, Exception) or not reset_result:
                    LOGGER.warning(
                        "Could not reset Marstek %s at %s to 0W; "
                        "previous command will expire",
                        battery_id,
                        battery_ip,
                    )
                    continue
                reset_count += 1
            LOGGER.info(
                "Controller stopped; reset %d/%d Marstek setpoints to 0W",
                reset_count,
                len(batteries),
            )
    finally:
        executor.shutdown(wait=True)
        for battery_client in clients_by_port.values():
            battery_client.close()
    return 0


def main() -> int:
    return run(build_parser().parse_args())


if __name__ == "__main__":
    raise SystemExit(main())
