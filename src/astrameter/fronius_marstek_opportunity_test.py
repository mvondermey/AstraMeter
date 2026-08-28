from __future__ import annotations

import csv
from datetime import date, datetime

import pytest

from .fronius_marstek_opportunity import (
    OpportunitySummary,
    Sample,
    load_samples,
    summarize_day,
    upsert_csv,
)


def test_load_samples_reads_rotated_logs_and_deduplicates(tmp_path) -> None:
    log_file = tmp_path / "controller.log"
    log_file.write_text(
        "2026-08-28 12:00:05,000 INFO cycle ok P_Grid=-3000W "
        "mode=Passive previous_output=-2000W target=-2500W soc=50%\n",
        encoding="utf-8",
    )
    (tmp_path / "controller.log.1").write_text(
        "2026-08-28 12:00:00,000 INFO cycle ok P_Grid=-2000W "
        "mode=Passive previous_output=-1900W target=-2000W soc=49%\n"
        "2026-08-28 12:00:05,000 INFO cycle ok P_Grid=-3000W "
        "mode=Passive previous_output=-2000W target=-2500W soc=50%\n",
        encoding="utf-8",
    )
    (tmp_path / "controller.log.unrelated").write_text("ignored\n", encoding="utf-8")

    samples = load_samples(log_file)

    assert [sample.p_grid for sample in samples] == [-2000, -3000]


def test_summarize_day_integrates_power_limit_and_full_battery() -> None:
    samples = [
        Sample(datetime(2026, 8, 28, 12, 0, 0), -2000, 50),
        Sample(datetime(2026, 8, 28, 12, 0, 5), -3000, 50),
        Sample(datetime(2026, 8, 28, 12, 0, 10), -4000, 99),
        Sample(datetime(2026, 8, 28, 12, 0, 15), -1000, 100),
        Sample(datetime(2026, 8, 28, 12, 0, 20), 0, 100),
    ]

    summary = summarize_day(samples, date(2026, 8, 28))

    assert summary.samples == 5
    assert summary.observed_hours == pytest.approx(20 / 3600, abs=0.0001)
    assert summary.above_limit_minutes == pytest.approx(10 / 60, abs=0.01)
    assert summary.episodes == 1
    assert summary.longest_episode_minutes == pytest.approx(10 / 60, abs=0.01)
    assert summary.extra_power_energy_wh == 2.8
    assert summary.peak_surplus_w == 4000
    assert summary.full_battery_surplus_minutes == pytest.approx(10 / 60, abs=0.01)
    assert summary.full_battery_surplus_wh == 6.9
    assert summary.second_battery_opportunity_wh == 7.6


def test_summarize_day_caps_missing_data_gap() -> None:
    samples = [
        Sample(datetime(2026, 8, 28, 12, 0, 0), -3000, 50),
        Sample(datetime(2026, 8, 28, 13, 0, 0), -3000, 50),
    ]

    summary = summarize_day(samples, date(2026, 8, 28), max_sample_gap=10)

    assert summary.observed_hours == pytest.approx(10 / 3600, abs=0.0001)
    assert summary.above_limit_minutes == pytest.approx(10 / 60, abs=0.01)
    assert summary.extra_power_energy_wh == 1.4


def test_summarize_day_rejects_invalid_limits() -> None:
    with pytest.raises(ValueError, match="power_limit"):
        summarize_day([], date(2026, 8, 28), power_limit=0)
    with pytest.raises(ValueError, match="max_sample_gap"):
        summarize_day([], date(2026, 8, 28), max_sample_gap=0)


def test_upsert_csv_replaces_same_date_and_preserves_other_days(tmp_path) -> None:
    output = tmp_path / "opportunity.csv"

    def summary(day: str, energy: float) -> OpportunitySummary:
        return OpportunitySummary(
            date=day,
            samples=1,
            observed_hours=1,
            above_limit_minutes=1,
            above_limit_share_percent=1,
            episodes=1,
            longest_episode_minutes=1,
            extra_power_energy_wh=energy,
            peak_surplus_w=3000,
            full_battery_surplus_minutes=0,
            full_battery_surplus_wh=0,
            second_battery_opportunity_wh=energy,
        )

    upsert_csv(output, summary("2026-08-27", 10))
    upsert_csv(output, summary("2026-08-28", 20))
    upsert_csv(output, summary("2026-08-27", 30))

    with output.open(newline="", encoding="utf-8") as handle:
        rows = list(csv.DictReader(handle))
    assert [row["date"] for row in rows] == ["2026-08-27", "2026-08-28"]
    assert rows[0]["extra_power_energy_wh"] == "30"
