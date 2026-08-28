from __future__ import annotations

from collections.abc import Iterator

import pytest

from . import fronius_marstek_direct as direct
from .fronius_marstek_direct import (
    MarstekClient,
    calculate_target,
    device_matches,
    extract_p_grid,
    required_request_delay,
)


def test_calculate_target_sign_deadband_and_limit() -> None:
    assert calculate_target(800, 50, 2500) == 800
    assert calculate_target(-600, 50, 2500) == -600
    assert calculate_target(49, 50, 2500) == 0
    assert calculate_target(4000, 50, 2500) == 2500
    assert calculate_target(-4000, 50, 2500) == -2500


def test_calculate_target_rejects_non_finite() -> None:
    with pytest.raises(ValueError):
        calculate_target(float("nan"), 50, 2500)


def test_run_applies_grid_target_independently_of_reported_soc(
    tmp_path, monkeypatch
) -> None:
    """Battery firmware, not the controller, enforces the configured SOC limit."""
    set_calls: list[tuple[int, int]] = []
    grid_values: Iterator[int] = iter((600, -400))

    class FakeStopEvent:
        def __init__(self) -> None:
            self.waits = 0

        def is_set(self) -> bool:
            return self.waits >= 2

        def set(self) -> None:
            self.waits = 2

        def wait(self, _timeout: float) -> None:
            self.waits += 1

    class FakeClient:
        ip = "192.168.1.95"

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ensure_ip(self) -> str:
            return self.ip

        def get_mode(self) -> dict[str, object]:
            return {
                "id": 0,
                "mode": "Passive",
                "ongrid_power": 0,
                "bat_soc": 11,
            }

        def set_passive(self, power: int, duration: int) -> bool:
            set_calls.append((power, duration))
            return True

        def close(self) -> None:
            pass

    args = direct.build_parser().parse_args(
        [
            "--state-file",
            str(tmp_path / "ip"),
            "--log-file",
            str(tmp_path / "controller.log"),
        ]
    )
    monkeypatch.setattr(direct, "MarstekClient", FakeClient)
    monkeypatch.setattr(direct, "read_fronius", lambda _host: next(grid_values))
    monkeypatch.setattr(direct.threading, "Event", FakeStopEvent)
    monkeypatch.setattr(direct.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(direct.time, "sleep", lambda _seconds: None)

    assert direct.run(args) == 0
    assert set_calls == [(600, 45), (-400, 45), (0, 10)]


def test_fronius_failure_does_not_trigger_marstek_probe(tmp_path, monkeypatch) -> None:
    """A Fronius/DNS outage must not invalidate a reachable Marstek address."""
    ensure_calls = 0
    grid_reads = 0

    class FakeStopEvent:
        def __init__(self) -> None:
            self.waits = 0

        def is_set(self) -> bool:
            return self.waits >= 2

        def set(self) -> None:
            self.waits = 2

        def wait(self, _timeout: float) -> None:
            self.waits += 1

    class FakeClient:
        ip = "192.168.1.95"

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ensure_ip(self) -> str:
            nonlocal ensure_calls
            ensure_calls += 1
            return self.ip

        def close(self) -> None:
            pass

    def fake_read_fronius(_host: str) -> float:
        nonlocal grid_reads
        grid_reads += 1
        if grid_reads == 1:
            raise OSError("temporary DNS failure")
        return 123.0

    args = direct.build_parser().parse_args(
        [
            "--dry-run",
            "--state-file",
            str(tmp_path / "ip"),
            "--log-file",
            str(tmp_path / "controller.log"),
        ]
    )
    monkeypatch.setattr(direct, "MarstekClient", FakeClient)
    monkeypatch.setattr(direct, "read_fronius", fake_read_fronius)
    monkeypatch.setattr(direct.threading, "Event", FakeStopEvent)
    monkeypatch.setattr(direct.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(direct.time, "sleep", lambda _seconds: None)

    assert direct.run(args) == 0
    assert grid_reads == 2
    assert ensure_calls == 1


def test_marstek_failure_probes_before_resuming_writes(tmp_path, monkeypatch) -> None:
    """After an API failure, one read-only probe cycle must precede new writes."""
    ensure_calls = 0
    grid_reads = 0
    get_mode_calls = 0
    set_calls: list[tuple[int, int]] = []

    class FakeStopEvent:
        def __init__(self) -> None:
            self.waits = 0

        def is_set(self) -> bool:
            return self.waits >= 3

        def set(self) -> None:
            self.waits = 3

        def wait(self, _timeout: float) -> None:
            self.waits += 1

    class FakeClient:
        ip = "192.168.1.95"

        def __init__(self, *_args, **_kwargs) -> None:
            pass

        def ensure_ip(self) -> str:
            nonlocal ensure_calls
            ensure_calls += 1
            return self.ip

        def get_mode(self) -> dict[str, object]:
            nonlocal get_mode_calls
            get_mode_calls += 1
            return {
                "id": 0,
                "mode": "Passive",
                "ongrid_power": 0,
                "bat_soc": 50,
            }

        def set_passive(self, power: int, duration: int) -> bool:
            set_calls.append((power, duration))
            if len(set_calls) == 1:
                raise OSError("temporary UDP timeout")
            return True

        def close(self) -> None:
            pass

    def fake_read_fronius(_host: str) -> float:
        nonlocal grid_reads
        grid_reads += 1
        return 500.0

    args = direct.build_parser().parse_args(
        [
            "--state-file",
            str(tmp_path / "ip"),
            "--log-file",
            str(tmp_path / "controller.log"),
        ]
    )
    monkeypatch.setattr(direct, "MarstekClient", FakeClient)
    monkeypatch.setattr(direct, "read_fronius", fake_read_fronius)
    monkeypatch.setattr(direct.threading, "Event", FakeStopEvent)
    monkeypatch.setattr(direct.signal, "signal", lambda *_args: None)
    monkeypatch.setattr(direct.time, "sleep", lambda _seconds: None)

    assert direct.run(args) == 0
    assert ensure_calls == 2
    assert grid_reads == 2
    assert get_mode_calls == 2
    assert set_calls == [(500, 45), (500, 45), (0, 10)]


def test_required_request_delay() -> None:
    assert required_request_delay(0, 10, 5) == 0
    assert required_request_delay(10, 12, 5) == 3
    assert required_request_delay(10, 15, 5) == 0
    assert required_request_delay(10, 20, 5) == 0


def test_run_rejects_nonpositive_api_request_gap(tmp_path) -> None:
    args = direct.build_parser().parse_args(
        [
            "--api-request-gap",
            "0",
            "--state-file",
            str(tmp_path / "ip"),
            "--log-file",
            str(tmp_path / "controller.log"),
        ]
    )

    with pytest.raises(ValueError, match="greater than zero"):
        direct.run(args)


def test_ensure_ip_only_validates_cached_address(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "ip"
    state_file.write_text("192.168.1.95", encoding="utf-8")
    client = MarstekClient("5037cd7f1d02", 30000, state_file)
    calls = []

    def fake_request(method, params, target=None):
        calls.append((method, params, target))
        return {"src": "VenusE 3.0-5037cd7f1d02", "result": {}}

    monkeypatch.setattr(client, "request", fake_request)

    assert client.ensure_ip() == "192.168.1.95"
    assert calls == [("Wifi.GetStatus", {"id": 0}, None)]


def test_get_mode_validates_device_and_result(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "ip"
    state_file.write_text("192.168.1.95", encoding="utf-8")
    client = MarstekClient("5037cd7f1d02", 30000, state_file)

    monkeypatch.setattr(
        client,
        "request",
        lambda method, params: {
            "src": "VenusE 3.0-5037cd7f1d02",
            "result": {
                "id": 0,
                "mode": "Passive",
                "ongrid_power": 700,
                "bat_soc": 48,
            },
        },
    )

    assert client.get_mode()["ongrid_power"] == 700


def test_get_mode_rejects_invalid_mode(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "ip"
    state_file.write_text("192.168.1.95", encoding="utf-8")
    client = MarstekClient("5037cd7f1d02", 30000, state_file)

    monkeypatch.setattr(
        client,
        "request",
        lambda method, params: {
            "src": "VenusE 3.0-5037cd7f1d02",
            "result": {"id": 0, "mode": None},
        },
    )

    with pytest.raises(ValueError, match="invalid mode"):
        client.get_mode()


def test_request_retries_one_timeout(tmp_path, monkeypatch) -> None:
    state_file = tmp_path / "ip"
    state_file.write_text("192.168.1.95", encoding="utf-8")
    client = MarstekClient(
        "5037cd7f1d02",
        30000,
        state_file,
        minimum_request_gap=0,
        request_attempts=2,
    )
    sockets = []

    class FakeSocket:
        def __init__(self):
            self.receive_calls = 0
            self.bound_to = None
            self.closed = False
            sockets.append(self)

        def bind(self, address):
            self.bound_to = address

        def settimeout(self, timeout):
            pass

        def sendto(self, message, destination):
            pass

        def recvfrom(self, size):
            self.receive_calls += 1
            if self.receive_calls == 1:
                raise TimeoutError
            return b'{"id":2,"result":{"id":0}}', ("192.168.1.95", 30000)

        def close(self):
            self.closed = True

    monkeypatch.setattr(direct.socket, "socket", lambda *args: FakeSocket())

    assert client.request("Wifi.GetStatus", {"id": 0})["id"] == 2
    assert len(sockets) == 1
    assert sockets[0].bound_to == ("0.0.0.0", 30000)

    client.close()
    assert sockets[0].closed is True


def test_extract_p_grid() -> None:
    payload = {
        "Head": {"Status": {"Code": 0}},
        "Body": {"Data": {"Site": {"P_Grid": -123.4}}},
    }
    assert extract_p_grid(payload) == -123.4


def test_device_matches_ble_mac_or_source_suffix() -> None:
    assert device_matches(
        {"src": "VenusE 3.0-other", "result": {"ble_mac": "5037cd7f1d02"}},
        "50:37:cd:7f:1d:02",
    )
    assert device_matches(
        {"src": "VenusE 3.0-5037cd7f1d02", "result": {}},
        "5037cd7f1d02",
    )
