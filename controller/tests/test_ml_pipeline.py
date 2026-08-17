from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from device_registry import DeviceRegistry
from ml_service import FloodRiskModel


BASE_DIR = Path(__file__).resolve().parents[1]


class FloodRiskModelTests(unittest.TestCase):
    def setUp(self) -> None:
        self.model = FloodRiskModel(BASE_DIR / "ml" / "model.json")
        self.link = {"id": 1, "source_device_id": "water-001", "target_device_id": "water-002"}
        self.devices = {
            "water-001": {
                "device_id": "water-001", "online": True, "distance_cm": 12.9,
                "weather_location_id": 1,
            },
            "water-002": {
                "device_id": "water-002", "online": True, "distance_cm": 13.1,
                "weather_location_id": 1,
            },
        }

    def test_dry_weather_keeps_ml_inactive(self) -> None:
        forecast = {
            "id": 1, "current_precipitation_mm": 0, "rain_1h_mm": 0,
            "rain_6h_mm": 0, "max_probability_6h": 10,
        }
        result = self.model.evaluate_link(
            self.link, self.devices, {1: forecast}, {}, weather_stale=False
        )
        self.assertEqual(result["status"], "dry_weather")
        self.assertFalse(result["active"])
        self.assertEqual(result["alert_level"], 0)

    def test_rain_and_fast_rise_add_l1(self) -> None:
        self.devices["water-001"]["distance_cm"] = 9.0
        self.devices["water-002"]["distance_cm"] = 9.5
        forecast = {
            "id": 1, "current_precipitation_mm": 2, "rain_1h_mm": 10,
            "rain_6h_mm": 40, "max_probability_6h": 95,
        }
        result = self.model.evaluate_link(
            self.link,
            self.devices,
            {1: forecast},
            {"water-001": 0.8, "water-002": 0.7},
            weather_stale=False,
        )
        self.assertTrue(result["active"])
        self.assertEqual(result["status"], "risk")
        self.assertEqual(result["alert_level"], 1)
        self.assertGreaterEqual(result["probability"], result["threshold"])

    def test_stale_weather_disables_ml(self) -> None:
        result = self.model.evaluate_link(
            self.link, self.devices, {}, {}, weather_stale=True
        )
        self.assertEqual(result["reason"], "weather_stale")
        self.assertFalse(result["active"])


class TelemetryHistoryTests(unittest.TestCase):
    def test_distance_is_recorded_for_rise_rate(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            registry = DeviceRegistry(Path(directory) / "test.db")
            record = registry.register(
                hardware_id="AABBCCDDEEFF", hostname="node.local", ip="192.0.2.1",
                device_type="water-level", firmware="test",
            )
            device_id = record["device_id"]
            self.assertTrue(registry.update_telemetry(device_id, "distance_cm", "12.9"))
            self.assertTrue(registry.update_telemetry(device_id, "distance_cm", "11.9"))
            count = registry._connection.execute(
                "SELECT COUNT(*) FROM telemetry_history WHERE device_id = ?", (device_id,)
            ).fetchone()[0]
            self.assertEqual(count, 2)
            registry.close()


if __name__ == "__main__":
    unittest.main()
