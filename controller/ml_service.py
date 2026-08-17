"""Rain-gated logistic-regression inference with a deterministic safety fallback."""

from __future__ import annotations

import json
import math
import os
from pathlib import Path
from typing import Any


class FloodRiskModel:
    """Load a portable JSON artifact and score linked upstream/downstream nodes."""

    def __init__(self, artifact_path: Path | str) -> None:
        self.artifact_path = Path(artifact_path)
        self.enabled = os.getenv("WATER_ML_ENABLED", "true").lower() not in {
            "0", "false", "no", "off"
        }
        self.artifact: dict[str, Any] | None = None
        self.error: str | None = None
        self.reload()

    def reload(self) -> None:
        self.artifact = None
        self.error = None
        if not self.enabled:
            return
        try:
            artifact = json.loads(self.artifact_path.read_text(encoding="utf-8"))
            required = {
                "model_type", "feature_order", "mean", "scale", "coefficients",
                "intercept", "decision_threshold", "field_validated",
            }
            missing = required.difference(artifact)
            if missing:
                raise ValueError(f"model artifact is missing: {', '.join(sorted(missing))}")
            feature_count = len(artifact["feature_order"])
            if artifact["model_type"] != "logistic_regression":
                raise ValueError("unsupported model_type")
            if not all(
                len(artifact[key]) == feature_count
                for key in ("mean", "scale", "coefficients")
            ):
                raise ValueError("model vectors do not match feature_order")
            self.artifact = artifact
        except (OSError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.error = str(exc)

    @staticmethod
    def _rain_is_present(forecast: dict[str, Any]) -> bool:
        current = float(forecast.get("current_precipitation_mm") or 0.0)
        rain_1h = float(forecast.get("rain_1h_mm") or 0.0)
        rain_6h = float(forecast.get("rain_6h_mm") or 0.0)
        probability = float(forecast.get("max_probability_6h") or 0.0)
        return current >= 0.1 or rain_1h >= 0.5 or (rain_6h >= 1.0 and probability >= 50.0)

    def _predict_probability(self, features: dict[str, float]) -> float:
        assert self.artifact is not None
        score = float(self.artifact["intercept"])
        for index, name in enumerate(self.artifact["feature_order"]):
            value = float(features[name])
            scale = float(self.artifact["scale"][index]) or 1.0
            standardized = (value - float(self.artifact["mean"][index])) / scale
            score += float(self.artifact["coefficients"][index]) * standardized
        score = max(-60.0, min(60.0, score))
        return 1.0 / (1.0 + math.exp(-score))

    def evaluate_link(
        self,
        link: dict[str, Any],
        devices: dict[str, dict[str, Any]],
        forecasts: dict[int, dict[str, Any]],
        rise_rates: dict[str, float],
        *,
        weather_stale: bool,
    ) -> dict[str, Any]:
        base = {
            "link_id": link["id"],
            "source_device_id": link["source_device_id"],
            "target_device_id": link["target_device_id"],
            "status": "unavailable",
            "active": False,
            "probability": None,
            "alert_level": None,
            "reason": None,
        }
        if not self.enabled:
            return {**base, "reason": "ml_disabled"}
        if self.artifact is None:
            return {**base, "reason": "model_unavailable"}

        source = devices.get(link["source_device_id"])
        target = devices.get(link["target_device_id"])
        if source is None or target is None:
            return {**base, "reason": "linked_device_missing"}
        if not source["online"] or not target["online"]:
            return {**base, "reason": "linked_device_offline"}
        if source["distance_cm"] is None or target["distance_cm"] is None:
            return {**base, "reason": "distance_missing"}
        if weather_stale:
            return {**base, "reason": "weather_stale"}

        location_id = source.get("weather_location_id") or target.get("weather_location_id")
        if location_id is None:
            return {**base, "reason": "weather_location_missing"}
        forecast = forecasts.get(int(location_id))
        if forecast is None:
            return {**base, "reason": "weather_forecast_missing"}
        if not self._rain_is_present(forecast):
            return {
                **base,
                "status": "dry_weather",
                "reason": "rain_gate_inactive",
                "alert_level": 0,
            }

        distance_a = float(source["distance_cm"])
        distance_b = float(target["distance_cm"])
        features = {
            "distance_a_cm": distance_a,
            "distance_b_cm": distance_b,
            "rise_a_cm_min": float(rise_rates.get(source["device_id"], 0.0)),
            "rise_b_cm_min": float(rise_rates.get(target["device_id"], 0.0)),
            "distance_delta_abs_cm": abs(distance_a - distance_b),
            "current_precipitation_mm": float(forecast.get("current_precipitation_mm") or 0.0),
            "rain_1h_mm": float(forecast.get("rain_1h_mm") or 0.0),
            "rain_6h_mm": float(forecast.get("rain_6h_mm") or 0.0),
            "max_probability_6h": float(forecast.get("max_probability_6h") or 0.0),
        }
        try:
            probability = self._predict_probability(features)
        except (KeyError, TypeError, ValueError) as exc:
            return {**base, "reason": f"feature_error: {exc}"}

        threshold = float(self.artifact["decision_threshold"])
        risk = probability >= threshold
        return {
            **base,
            "status": "risk" if risk else "normal",
            "active": True,
            "probability": round(probability, 4),
            # A synthetic model may add an early-warning L1 but never create or
            # clear a critical L2 warning. Deterministic water thresholds remain primary.
            "alert_level": 1 if risk else 0,
            "reason": "synthetic_model" if risk else "below_threshold",
            "threshold": threshold,
            "weather_location_id": int(location_id),
            "features": {key: round(value, 4) for key, value in features.items()},
        }

    def status(self) -> dict[str, Any]:
        artifact = self.artifact or {}
        return {
            "enabled": self.enabled,
            "loaded": self.artifact is not None,
            "artifact": str(self.artifact_path),
            "version": artifact.get("version"),
            "model_type": artifact.get("model_type"),
            "field_validated": artifact.get("field_validated", False),
            "training_data": artifact.get("training_data"),
            "decision_threshold": artifact.get("decision_threshold"),
            "error": self.error,
        }
