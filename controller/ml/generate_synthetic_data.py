"""Generate grouped synthetic rain/water events for pipeline validation only."""

from __future__ import annotations

import csv
import math
import random
from datetime import UTC, datetime, timedelta
from pathlib import Path


OUTPUT_PATH = Path(__file__).resolve().parent / "synthetic_flood_events.csv"
SEED = 20260817
SAMPLES_PER_EVENT = 20
RED_DISTANCE_CM = 8.0

FIELDNAMES = [
    "timestamp", "event_id", "split", "event_type", "site_id",
    "synthetic_data", "field_validated", "distance_a_cm", "distance_b_cm",
    "rise_a_cm_min", "rise_b_cm_min", "distance_delta_abs_cm",
    "current_precipitation_mm", "rain_1h_mm", "rain_6h_mm",
    "max_probability_6h", "node_a_valid", "node_b_valid",
    "node_a_online", "node_b_online", "weather_stale",
    "label_available", "critical_within_5min",
]

EVENTS = [
    *(('train', 'normal_dry') for _ in range(4)),
    *(('train', 'rain_no_flood') for _ in range(3)),
    *(('train', 'segment_rise') for _ in range(4)),
    *(('train', 'local_blockage') for _ in range(2)),
    ('train', 'critical_fast_rise'),
    *(('train', 'sensor_fault') for _ in range(2)),
    ('validation', 'normal_dry'),
    ('validation', 'segment_rise'),
    ('validation', 'local_blockage'),
    ('validation', 'critical_fast_rise'),
    ('test', 'normal_dry'),
    ('test', 'rain_no_flood'),
    ('test', 'segment_rise'),
    ('test', 'local_blockage'),
]


def _noise(rng: random.Random, scale: float = 0.08) -> float:
    return rng.gauss(0.0, scale)


def _event_series(event_type: str, rng: random.Random) -> list[dict[str, float | int]]:
    rows: list[dict[str, float | int]] = []
    for minute in range(SAMPLES_PER_EVENT):
        wave = math.sin(minute / 3.0)
        distance_a = 12.9 + 0.12 * wave + _noise(rng)
        distance_b = 13.1 + 0.10 * wave + _noise(rng)
        rain_now = rain_1h = rain_6h = probability = 0.0
        valid_a = valid_b = online_a = online_b = 1
        stale = 0

        if event_type == "normal_dry":
            probability = max(0.0, 15.0 + rng.uniform(-8.0, 8.0))
        elif event_type == "rain_no_flood":
            rain_now = max(0.1, 0.8 + 0.06 * minute + _noise(rng, 0.12))
            rain_1h = 4.0 + 0.25 * minute + rng.uniform(-0.3, 0.3)
            rain_6h = 16.0 + 0.65 * minute + rng.uniform(-1.0, 1.0)
            probability = min(100.0, 72.0 + minute + rng.uniform(-4.0, 4.0))
            distance_a -= max(0, minute - 10) * 0.025
            distance_b -= max(0, minute - 10) * 0.020
        elif event_type == "segment_rise":
            rain_now = max(0.1, 0.5 + 0.12 * minute + _noise(rng, 0.10))
            rain_1h = 3.0 + 0.45 * minute + rng.uniform(-0.3, 0.3)
            rain_6h = 14.0 + 0.90 * minute + rng.uniform(-1.0, 1.0)
            probability = min(100.0, 68.0 + 1.5 * minute + rng.uniform(-3.0, 3.0))
            rise = max(0, minute - 4)
            distance_a -= 0.43 * rise
            distance_b -= 0.38 * rise
        elif event_type == "local_blockage":
            rain_now = max(0.1, 0.35 + 0.08 * minute + _noise(rng, 0.08))
            rain_1h = 2.5 + 0.32 * minute + rng.uniform(-0.25, 0.25)
            rain_6h = 10.0 + 0.60 * minute + rng.uniform(-0.8, 0.8)
            probability = min(100.0, 60.0 + 1.6 * minute + rng.uniform(-4.0, 4.0))
            rise = max(0, minute - 5)
            distance_a -= 0.50 * rise
            distance_b -= 0.08 * rise
        elif event_type == "critical_fast_rise":
            rain_now = max(0.2, 1.2 + 0.18 * minute + _noise(rng, 0.12))
            rain_1h = 7.0 + 0.55 * minute + rng.uniform(-0.4, 0.4)
            rain_6h = 28.0 + 1.10 * minute + rng.uniform(-1.0, 1.0)
            probability = min(100.0, 82.0 + minute + rng.uniform(-3.0, 3.0))
            rise = max(0, minute - 7)
            distance_a -= 0.82 * rise
            distance_b -= 0.70 * rise
        elif event_type == "sensor_fault":
            rain_now = max(0.1, 0.4 + 0.04 * minute + _noise(rng, 0.08))
            rain_1h = 2.0 + 0.20 * minute
            rain_6h = 8.0 + 0.35 * minute
            probability = min(90.0, 58.0 + minute)
            if 8 <= minute <= 12:
                valid_a = 0
                if minute >= 10:
                    online_a = 0

        rows.append({
            "distance_a_cm": max(3.0, round(distance_a, 3)),
            "distance_b_cm": max(3.0, round(distance_b, 3)),
            "current_precipitation_mm": round(rain_now, 3),
            "rain_1h_mm": round(max(0.0, rain_1h), 3),
            "rain_6h_mm": round(max(0.0, rain_6h), 3),
            "max_probability_6h": round(max(0.0, probability), 1),
            "node_a_valid": valid_a,
            "node_b_valid": valid_b,
            "node_a_online": online_a,
            "node_b_online": online_b,
            "weather_stale": stale,
        })

    for index, row in enumerate(rows):
        if index == 0:
            rise_a = rise_b = 0.0
        else:
            rise_a = float(rows[index - 1]["distance_a_cm"]) - float(row["distance_a_cm"])
            rise_b = float(rows[index - 1]["distance_b_cm"]) - float(row["distance_b_cm"])
        row["rise_a_cm_min"] = round(rise_a, 3)
        row["rise_b_cm_min"] = round(rise_b, 3)
        row["distance_delta_abs_cm"] = round(
            abs(float(row["distance_a_cm"]) - float(row["distance_b_cm"])), 3
        )
    return rows


def generate(output_path: Path = OUTPUT_PATH) -> Path:
    rng = random.Random(SEED)
    started_at = datetime(2026, 7, 1, 0, 0, tzinfo=UTC)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    generated: list[dict[str, object]] = []

    for event_number, (split, event_type) in enumerate(EVENTS, start=1):
        event_id = f"SYN-{event_number:03d}"
        series = _event_series(event_type, rng)
        event_start = started_at + timedelta(hours=event_number * 3)
        for index, row in enumerate(series):
            current_is_critical = (
                float(row["distance_a_cm"]) <= RED_DISTANCE_CM
                or float(row["distance_b_cm"]) <= RED_DISTANCE_CM
            )
            has_future_window = index + 5 < len(series)
            label_available = int(has_future_window and not current_is_critical)
            label: int | str = ""
            if label_available:
                future = series[index + 1:index + 6]
                label = int(any(
                    float(item["distance_a_cm"]) <= RED_DISTANCE_CM
                    or float(item["distance_b_cm"]) <= RED_DISTANCE_CM
                    for item in future
                ))
            generated.append({
                "timestamp": (event_start + timedelta(minutes=index)).isoformat().replace("+00:00", "Z"),
                "event_id": event_id,
                "split": split,
                "event_type": event_type,
                "site_id": "SYNTHETIC-DRAIN-01",
                "synthetic_data": 1,
                "field_validated": 0,
                **row,
                "label_available": label_available,
                "critical_within_5min": label,
            })

    if len(generated) != 480:
        raise RuntimeError(f"expected 480 rows, generated {len(generated)}")
    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES)
        writer.writeheader()
        writer.writerows(generated)
    return output_path


if __name__ == "__main__":
    print(generate())
