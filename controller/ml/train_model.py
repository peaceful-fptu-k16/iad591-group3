"""Train a portable logistic-regression artifact using grouped synthetic events."""

from __future__ import annotations

import csv
import json
from datetime import UTC, datetime
from pathlib import Path

import numpy as np

from generate_synthetic_data import OUTPUT_PATH, generate


MODEL_PATH = Path(__file__).resolve().parent / "model.json"
REPORT_PATH = Path(__file__).resolve().parent / "training_report.json"
FEATURES = [
    "distance_a_cm",
    "distance_b_cm",
    "rise_a_cm_min",
    "rise_b_cm_min",
    "distance_delta_abs_cm",
    "current_precipitation_mm",
    "rain_1h_mm",
    "rain_6h_mm",
    "max_probability_6h",
]


def _load_rows(path: Path) -> list[dict[str, str]]:
    with path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    return [
        row for row in rows
        if row["label_available"] == "1"
        and row["node_a_valid"] == "1"
        and row["node_b_valid"] == "1"
        and row["node_a_online"] == "1"
        and row["node_b_online"] == "1"
        and row["weather_stale"] == "0"
    ]


def _matrix(rows: list[dict[str, str]]) -> tuple[np.ndarray, np.ndarray]:
    x = np.asarray([[float(row[name]) for name in FEATURES] for row in rows], dtype=float)
    y = np.asarray([int(row["critical_within_5min"]) for row in rows], dtype=float)
    return x, y


def _sigmoid(values: np.ndarray) -> np.ndarray:
    return 1.0 / (1.0 + np.exp(-np.clip(values, -60.0, 60.0)))


def _metrics(y: np.ndarray, probability: np.ndarray, threshold: float) -> dict[str, float | int]:
    prediction = probability >= threshold
    positive = y == 1
    tp = int(np.sum(prediction & positive))
    fp = int(np.sum(prediction & ~positive))
    fn = int(np.sum(~prediction & positive))
    tn = int(np.sum(~prediction & ~positive))
    precision = tp / (tp + fp) if tp + fp else 0.0
    recall = tp / (tp + fn) if tp + fn else 0.0
    f1 = 2 * precision * recall / (precision + recall) if precision + recall else 0.0
    positive_probability = probability[positive]
    negative_probability = probability[~positive]
    if len(positive_probability) and len(negative_probability):
        comparisons = positive_probability[:, None] - negative_probability[None, :]
        roc_auc = float(np.mean(
            (comparisons > 0).astype(float) + 0.5 * (comparisons == 0).astype(float)
        ))
    else:
        roc_auc = float("nan")
    return {
        "samples": int(len(y)), "positives": int(np.sum(positive)),
        "tp": tp, "fp": fp, "fn": fn, "tn": tn,
        "precision": round(precision, 4), "recall": round(recall, 4),
        "f1": round(f1, 4), "roc_auc": round(roc_auc, 4),
        "brier": round(float(np.mean((probability - y) ** 2)), 4),
    }


def train(dataset_path: Path = OUTPUT_PATH, model_path: Path = MODEL_PATH) -> dict[str, object]:
    if not dataset_path.exists():
        generate(dataset_path)
    rows = _load_rows(dataset_path)
    by_split = {split: [row for row in rows if row["split"] == split] for split in ("train", "validation", "test")}
    x_train, y_train = _matrix(by_split["train"])
    mean = x_train.mean(axis=0)
    scale = x_train.std(axis=0)
    scale[scale < 1e-9] = 1.0
    x_standardized = (x_train - mean) / scale

    weights = np.zeros(len(FEATURES), dtype=float)
    intercept = 0.0
    positives = max(1.0, float(np.sum(y_train == 1)))
    negatives = max(1.0, float(np.sum(y_train == 0)))
    sample_weights = np.where(
        y_train == 1,
        len(y_train) / (2.0 * positives),
        len(y_train) / (2.0 * negatives),
    )
    learning_rate = 0.03
    l2_strength = 0.02
    for _ in range(6000):
        probability = _sigmoid(x_standardized @ weights + intercept)
        error = (probability - y_train) * sample_weights
        weights -= learning_rate * ((x_standardized.T @ error) / len(y_train) + l2_strength * weights)
        intercept -= learning_rate * float(np.mean(error))

    def probabilities(split: str) -> tuple[np.ndarray, np.ndarray]:
        x, y = _matrix(by_split[split])
        return y, _sigmoid(((x - mean) / scale) @ weights + intercept)

    y_validation, p_validation = probabilities("validation")
    candidates = np.arange(0.20, 0.81, 0.01)
    threshold = max(
        candidates,
        key=lambda value: (
            _metrics(y_validation, p_validation, float(value))["f1"],
            _metrics(y_validation, p_validation, float(value))["recall"],
            -float(value),
        ),
    )

    artifact = {
        "version": "1.0.0-synthetic",
        "model_type": "logistic_regression",
        "created_at": datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z"),
        "prediction_horizon_minutes": 5,
        "training_data": "synthetic_flood_events.csv (480 rows, grouped events)",
        "synthetic_data": True,
        "field_validated": False,
        "rain_gated": True,
        "feature_order": FEATURES,
        "mean": [round(float(value), 10) for value in mean],
        "scale": [round(float(value), 10) for value in scale],
        "coefficients": [round(float(value), 10) for value in weights],
        "intercept": round(float(intercept), 10),
        "decision_threshold": round(float(threshold), 4),
        "critical_distance_cm": 8.0,
        "notes": "Software-validation artifact only; it may add L1 but never create or clear L2.",
    }
    model_path.write_text(json.dumps(artifact, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    report: dict[str, object] = {
        "warning": "Synthetic results are not evidence of field flood-prediction accuracy.",
        "dataset_rows": 480,
        "usable_rows": len(rows),
        "event_counts": {
            split: len({row["event_id"] for row in split_rows})
            for split, split_rows in by_split.items()
        },
        "decision_threshold": artifact["decision_threshold"],
        "splits": {},
    }
    for split in by_split:
        y, probability = probabilities(split)
        report["splits"][split] = _metrics(y, probability, float(threshold))
    REPORT_PATH.write_text(json.dumps(report, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return report


if __name__ == "__main__":
    print(json.dumps(train(), ensure_ascii=False, indent=2))
