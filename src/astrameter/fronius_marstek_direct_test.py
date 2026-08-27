from __future__ import annotations

import pytest

from .fronius_marstek_direct import (
    calculate_target,
    device_matches,
    extract_p_grid,
    retry_delay,
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


def test_retry_delay_backs_off_and_is_capped() -> None:
    assert retry_delay(0, 5) == 5
    assert retry_delay(1, 5) == 10
    assert retry_delay(2, 5) == 20
    assert retry_delay(5, 5) == 120


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
