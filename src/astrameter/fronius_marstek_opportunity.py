"""Archive daily surplus that a second Marstek battery could use.

This tool is deliberately read-only with respect to Fronius and Marstek.  It
parses the direct controller's current and rotated log files and upserts one
daily row into a long-lived CSV file.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from dataclasses import asdict, dataclass
from datetime import date, datetime, time, timedelta
from itertools import pairwise
from pathlib import Path
from typing import Any

LOG_PATTERN = re.compile(
    r"^(?P<timestamp>\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3}) "
    r"INFO cycle ok P_Grid=(?P<p_grid>-?\d+(?:\.\d+)?)W .* "
    r"soc=(?P<soc>\d+(?:\.\d+)?)%$"
)
TIMESTAMP_FORMAT = "%Y-%m-%d %H:%M:%S,%f"
CSV_FIELDS = (
    "date",
    "samples",
    "observed_hours",
    "above_limit_minutes",
    "above_limit_share_percent",
    "episodes",
    "longest_episode_minutes",
    "extra_power_energy_wh",
    "peak_surplus_w",
    "full_battery_surplus_minutes",
    "full_battery_surplus_wh",
    "second_battery_opportunity_wh",
)


@dataclass(frozen=True)
class Sample:
    timestamp: datetime
    p_grid: float
    soc: float


@dataclass(frozen=True)
class OpportunitySummary:
    date: str
    samples: int
    observed_hours: float
    above_limit_minutes: float
    above_limit_share_percent: float
    episodes: int
    longest_episode_minutes: float
    extra_power_energy_wh: float
    peak_surplus_w: float
    full_battery_surplus_minutes: float
    full_battery_surplus_wh: float
    second_battery_opportunity_wh: float


def related_log_files(log_file: Path) -> list[Path]:
    """Return the active log and numeric RotatingFileHandler backups."""
    candidates = []
    for path in log_file.parent.glob(f"{log_file.name}*"):
        suffix = path.name.removeprefix(log_file.name)
        if path.is_file() and (not suffix or (suffix.startswith(".") and suffix[1:].isdigit())):
            candidates.append(path)
    return candidates


def load_samples(log_file: Path) -> list[Sample]:
    """Parse and de-duplicate cycle samples from active and rotated logs."""
    samples: dict[datetime, Sample] = {}
    for path in related_log_files(log_file):
        with path.open(encoding="utf-8", errors="replace") as handle:
            for line in handle:
                match = LOG_PATTERN.match(line.rstrip("\r\n"))
                if not match:
                    continue
                timestamp = datetime.strptime(
                    match.group("timestamp"), TIMESTAMP_FORMAT
                )
                samples[timestamp] = Sample(
                    timestamp=timestamp,
                    p_grid=float(match.group("p_grid")),
                    soc=float(match.group("soc")),
                )
    return sorted(samples.values(), key=lambda sample: sample.timestamp)


def summarize_day(
    samples: list[Sample],
    report_date: date,
    power_limit: float = 2500.0,
    full_soc: float = 99.0,
    max_sample_gap: float = 10.0,
) -> OpportunitySummary:
    """Integrate theoretical second-battery opportunity for one local day."""
    if power_limit <= 0:
        raise ValueError("power_limit must be greater than zero")
    if max_sample_gap <= 0:
        raise ValueError("max_sample_gap must be greater than zero")

    day_start = datetime.combine(report_date, time.min)
    day_end = day_start + timedelta(days=1)
    day_samples = [
        sample for sample in samples if day_start <= sample.timestamp < day_end
    ]
    peak_surplus = max(
        (max(0.0, -sample.p_grid) for sample in day_samples), default=0.0
    )

    observed_seconds = 0.0
    above_seconds = 0.0
    extra_power_wh = 0.0
    full_seconds = 0.0
    full_surplus_wh = 0.0
    second_battery_wh = 0.0
    episodes = 0
    current_episode_seconds = 0.0
    longest_episode_seconds = 0.0
    previous_interval_end: datetime | None = None

    for sample, following in pairwise(samples):
        interval_start = max(sample.timestamp, day_start)
        interval_end = min(
            following.timestamp,
            sample.timestamp + timedelta(seconds=max_sample_gap),
            day_end,
        )
        if interval_end <= interval_start:
            continue

        duration = (interval_end - interval_start).total_seconds()
        observed_seconds += duration
        surplus = max(0.0, -sample.p_grid)
        excess_power = max(0.0, surplus - power_limit)

        if excess_power > 0:
            above_seconds += duration
            extra_power_wh += excess_power * duration / 3600
            if previous_interval_end != interval_start:
                episodes += 1
                current_episode_seconds = 0.0
            current_episode_seconds += duration
            longest_episode_seconds = max(
                longest_episode_seconds, current_episode_seconds
            )
            previous_interval_end = interval_end
        else:
            current_episode_seconds = 0.0
            previous_interval_end = None

        battery_full = sample.soc >= full_soc
        if battery_full and surplus > 0:
            full_seconds += duration
            full_surplus_wh += surplus * duration / 3600
        opportunity_power = surplus if battery_full else excess_power
        second_battery_wh += opportunity_power * duration / 3600

    share = (
        100.0 * above_seconds / observed_seconds if observed_seconds > 0 else 0.0
    )
    return OpportunitySummary(
        date=report_date.isoformat(),
        samples=len(day_samples),
        observed_hours=round(observed_seconds / 3600, 4),
        above_limit_minutes=round(above_seconds / 60, 2),
        above_limit_share_percent=round(share, 2),
        episodes=episodes,
        longest_episode_minutes=round(longest_episode_seconds / 60, 2),
        extra_power_energy_wh=round(extra_power_wh, 1),
        peak_surplus_w=round(peak_surplus, 1),
        full_battery_surplus_minutes=round(full_seconds / 60, 2),
        full_battery_surplus_wh=round(full_surplus_wh, 1),
        second_battery_opportunity_wh=round(second_battery_wh, 1),
    )


def upsert_csv(output_file: Path, summary: OpportunitySummary) -> None:
    """Atomically insert or replace a daily summary row."""
    rows: dict[str, dict[str, Any]] = {}
    if output_file.exists():
        with output_file.open(newline="", encoding="utf-8") as handle:
            for row in csv.DictReader(handle):
                if row.get("date"):
                    rows[row["date"]] = row
    rows[summary.date] = {
        key: str(value) for key, value in asdict(summary).items()
    }

    output_file.parent.mkdir(parents=True, exist_ok=True)
    temporary_name: str | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w",
            newline="",
            encoding="utf-8",
            dir=output_file.parent,
            prefix=f".{output_file.name}.",
            suffix=".tmp",
            delete=False,
        ) as handle:
            temporary_name = handle.name
            writer = csv.DictWriter(handle, fieldnames=CSV_FIELDS)
            writer.writeheader()
            for row_date in sorted(rows):
                writer.writerow(rows[row_date])
        os.replace(temporary_name, output_file)
    finally:
        if temporary_name and os.path.exists(temporary_name):
            os.unlink(temporary_name)


def parse_report_date(value: str) -> date:
    today = datetime.now().date()
    if value == "today":
        return today
    if value == "yesterday":
        return today - timedelta(days=1)
    return date.fromisoformat(value)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--log-file", type=Path, default=Path("fronius-marstek-direct.log")
    )
    parser.add_argument(
        "--output", type=Path, default=Path("second-battery-opportunity.csv")
    )
    parser.add_argument("--date", type=parse_report_date, default="yesterday")
    parser.add_argument("--power-limit", type=float, default=2500.0)
    parser.add_argument("--full-soc", type=float, default=99.0)
    parser.add_argument("--max-sample-gap", type=float, default=10.0)
    return parser


def main() -> int:
    args = build_parser().parse_args()
    samples = load_samples(args.log_file)
    summary = summarize_day(
        samples,
        args.date,
        power_limit=args.power_limit,
        full_soc=args.full_soc,
        max_sample_gap=args.max_sample_gap,
    )
    upsert_csv(args.output, summary)
    print(json.dumps(asdict(summary), ensure_ascii=False, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
