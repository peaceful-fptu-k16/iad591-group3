"""SQLite-backed registry, telemetry store, and sensor-link graph."""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime, timedelta
from pathlib import Path
from threading import RLock
from typing import Any


def utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


class DeviceRegistry:
    """Persist device identity and the latest controller state in SQLite."""

    def __init__(self, database_path: Path | str) -> None:
        self.database_path = Path(database_path)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = RLock()
        self._connection = sqlite3.connect(self.database_path, check_same_thread=False)
        self._connection.row_factory = sqlite3.Row
        self._connection.execute("PRAGMA foreign_keys = ON")
        self._connection.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        with self._lock, self._connection:
            self._connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS devices (
                    hardware_id TEXT PRIMARY KEY,
                    device_id TEXT NOT NULL UNIQUE,
                    hostname TEXT NOT NULL,
                    ip TEXT NOT NULL,
                    type TEXT NOT NULL,
                    firmware TEXT NOT NULL,
                    topic TEXT NOT NULL UNIQUE,
                    label TEXT NOT NULL,
                    tank_height_cm REAL,
                    map_x REAL NOT NULL DEFAULT 50,
                    map_y REAL NOT NULL DEFAULT 50,
                    mqtt_status TEXT NOT NULL DEFAULT 'unknown',
                    distance_cm REAL,
                    last_seen TEXT,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS links (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    source_device_id TEXT NOT NULL,
                    target_device_id TEXT NOT NULL,
                    label TEXT NOT NULL DEFAULT '',
                    created_at TEXT NOT NULL,
                    UNIQUE(source_device_id, target_device_id),
                    CHECK(source_device_id <> target_device_id),
                    FOREIGN KEY(source_device_id) REFERENCES devices(device_id) ON DELETE CASCADE,
                    FOREIGN KEY(target_device_id) REFERENCES devices(device_id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS settings (
                    key TEXT PRIMARY KEY,
                    value REAL NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS weather_locations (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    timezone TEXT NOT NULL DEFAULT 'Asia/Bangkok',
                    admin1 TEXT NOT NULL DEFAULT '',
                    admin2 TEXT NOT NULL DEFAULT '',
                    admin3 TEXT NOT NULL DEFAULT '',
                    country TEXT NOT NULL DEFAULT 'Việt Nam',
                    created_at TEXT NOT NULL,
                    UNIQUE(latitude, longitude)
                );
                CREATE TABLE IF NOT EXISTS intersections (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    weather_location_id INTEGER NOT NULL,
                    name TEXT NOT NULL,
                    display_name TEXT NOT NULL,
                    latitude REAL NOT NULL,
                    longitude REAL NOT NULL,
                    created_at TEXT NOT NULL,
                    UNIQUE(weather_location_id, latitude, longitude),
                    FOREIGN KEY(weather_location_id) REFERENCES weather_locations(id) ON DELETE CASCADE
                );
                CREATE TABLE IF NOT EXISTS telemetry_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    device_id TEXT NOT NULL,
                    recorded_at TEXT NOT NULL,
                    distance_cm REAL NOT NULL,
                    FOREIGN KEY(device_id) REFERENCES devices(device_id) ON DELETE CASCADE
                );
                CREATE INDEX IF NOT EXISTS idx_telemetry_history_device_time
                    ON telemetry_history(device_id, recorded_at);
                INSERT OR IGNORE INTO settings VALUES ('blockage_level1_cm', 5.0, 'system');
                INSERT OR IGNORE INTO settings VALUES ('blockage_level2_cm', 10.0, 'system');
                INSERT OR IGNORE INTO settings VALUES ('flood_level1_percent', 70.0, 'system');
                INSERT OR IGNORE INTO settings VALUES ('flood_level2_percent', 90.0, 'system');
                INSERT OR IGNORE INTO settings VALUES ('rain_level1_6h_mm', 10.0, 'system');
                INSERT OR IGNORE INTO settings VALUES ('rain_level2_6h_mm', 30.0, 'system');
                """
            )
            device_columns = {
                row["name"] for row in self._connection.execute("PRAGMA table_info(devices)")
            }
            if "intersection_id" not in device_columns:
                self._connection.execute(
                    "ALTER TABLE devices ADD COLUMN intersection_id INTEGER"
                )

    def close(self) -> None:
        with self._lock:
            self._connection.close()

    def _next_device_id(self) -> str:
        rows = self._connection.execute("SELECT device_id FROM devices").fetchall()
        numbers = []
        for row in rows:
            try:
                numbers.append(int(row["device_id"].removeprefix("water-")))
            except ValueError:
                continue
        return f"water-{max(numbers, default=0) + 1:03d}"

    def register(
        self,
        *,
        hardware_id: str,
        hostname: str,
        ip: str,
        device_type: str,
        firmware: str,
    ) -> dict[str, Any]:
        del hostname
        now = utc_now()
        with self._lock, self._connection:
            existing = self._connection.execute(
                "SELECT device_id FROM devices WHERE hardware_id = ?", (hardware_id,)
            ).fetchone()
            if existing:
                self._connection.execute(
                    "UPDATE devices SET ip = ?, type = ?, firmware = ?, updated_at = ? WHERE hardware_id = ?",
                    (ip, device_type, firmware, now, hardware_id),
                )
            else:
                device_id = self._next_device_id()
                count = self._connection.execute("SELECT COUNT(*) FROM devices").fetchone()[0]
                map_x = 20 + (count % 4) * 20
                map_y = 25 + ((count // 4) % 3) * 25
                self._connection.execute(
                    """
                    INSERT INTO devices (
                        hardware_id, device_id, hostname, ip, type, firmware, topic,
                        label, map_x, map_y, created_at, updated_at
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        hardware_id, device_id, f"{device_id}.local", ip,
                        device_type, firmware, f"devices/{device_id}", device_id,
                        map_x, map_y, now, now,
                    ),
                )
        return self.get_by_hardware_id(hardware_id)

    def get_by_hardware_id(self, hardware_id: str) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute(
                f"{self._device_select()} WHERE d.hardware_id = ?", (hardware_id,)
            ).fetchone()
        if row is None:
            raise KeyError(hardware_id)
        return self._device_dict(row, self.get_settings())

    def get_by_device_id(self, device_id: str) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                f"{self._device_select()} WHERE d.device_id = ?", (device_id,)
            ).fetchone()
        return self._device_dict(row, self.get_settings()) if row else None

    def list_devices(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                f"{self._device_select()} ORDER BY d.device_id"
            ).fetchall()
        settings = self.get_settings()
        return [self._device_dict(row, settings) for row in rows]

    def get_settings(self) -> dict[str, float]:
        with self._lock:
            rows = self._connection.execute("SELECT key, value FROM settings").fetchall()
        return {row["key"]: float(row["value"]) for row in rows}

    def update_settings(self, changes: dict[str, float]) -> dict[str, float]:
        allowed = {
            "blockage_level1_cm", "blockage_level2_cm",
            "flood_level1_percent", "flood_level2_percent",
            "rain_level1_6h_mm", "rain_level2_6h_mm",
        }
        now = utc_now()
        with self._lock, self._connection:
            for key, value in changes.items():
                if key not in allowed:
                    continue
                self._connection.execute(
                    "UPDATE settings SET value = ?, updated_at = ? WHERE key = ?",
                    (float(value), now, key),
                )
        return self.get_settings()

    def update_device(self, device_id: str, changes: dict[str, Any]) -> dict[str, Any]:
        allowed = {"label", "tank_height_cm", "map_x", "map_y", "intersection_id"}
        values = {key: value for key, value in changes.items() if key in allowed}
        if not values:
            current = self.get_by_device_id(device_id)
            if current is None:
                raise KeyError(device_id)
            return current
        if "intersection_id" in values:
            intersection_id = values["intersection_id"]
            if intersection_id == 0:
                values["intersection_id"] = None
            elif intersection_id is not None:
                with self._lock:
                    exists = self._connection.execute(
                        "SELECT 1 FROM intersections WHERE id = ?", (intersection_id,)
                    ).fetchone()
                if exists is None:
                    raise ValueError("Intersection does not exist")
        values["updated_at"] = utc_now()
        assignments = ", ".join(f"{key} = ?" for key in values)
        parameters = [*values.values(), device_id]
        with self._lock, self._connection:
            cursor = self._connection.execute(
                f"UPDATE devices SET {assignments} WHERE device_id = ?", parameters
            )
            if cursor.rowcount == 0:
                raise KeyError(device_id)
        record = self.get_by_device_id(device_id)
        assert record is not None
        return record

    def delete_device(self, device_id: str) -> bool:
        """Delete a device; SQLite cascades deletion to all of its links."""
        with self._lock, self._connection:
            cursor = self._connection.execute(
                "DELETE FROM devices WHERE device_id = ?", (device_id,)
            )
        return cursor.rowcount > 0

    def update_telemetry(self, device_id: str, metric: str, payload: str) -> bool:
        now = utc_now()
        with self._lock, self._connection:
            if metric == "distance_cm":
                try:
                    value = float(payload)
                except ValueError:
                    return False
                if value < 0 or value > 100_000:
                    return False
                cursor = self._connection.execute(
                    "UPDATE devices SET distance_cm = ?, last_seen = ?, updated_at = ? WHERE device_id = ?",
                    (value, now, now, device_id),
                )
                if cursor.rowcount > 0:
                    self._connection.execute(
                        "INSERT INTO telemetry_history (device_id, recorded_at, distance_cm) VALUES (?, ?, ?)",
                        (device_id, now, value),
                    )
            elif metric == "status":
                status = payload.strip().lower()
                if status not in {"online", "offline"}:
                    return False
                cursor = self._connection.execute(
                    "UPDATE devices SET mqtt_status = ?, last_seen = ?, updated_at = ? WHERE device_id = ?",
                    (status, now, now, device_id),
                )
            else:
                return False
        return cursor.rowcount > 0

    def get_rise_rate_cm_per_min(
        self, device_id: str, window_seconds: int = 300
    ) -> float:
        """Return positive values when water rises (sensor distance decreases)."""
        cutoff = datetime.now(UTC) - timedelta(seconds=window_seconds)
        cutoff_text = cutoff.isoformat(timespec="seconds").replace("+00:00", "Z")
        with self._lock:
            rows = self._connection.execute(
                """
                SELECT recorded_at, distance_cm
                FROM telemetry_history
                WHERE device_id = ? AND recorded_at >= ?
                ORDER BY recorded_at
                """,
                (device_id, cutoff_text),
            ).fetchall()
        if len(rows) < 2:
            return 0.0
        started = datetime.fromisoformat(rows[0]["recorded_at"].replace("Z", "+00:00"))
        ended = datetime.fromisoformat(rows[-1]["recorded_at"].replace("Z", "+00:00"))
        elapsed_minutes = (ended - started).total_seconds() / 60.0
        if elapsed_minutes <= 0:
            return 0.0
        rate = (float(rows[0]["distance_cm"]) - float(rows[-1]["distance_cm"])) / elapsed_minutes
        return round(rate, 4)

    def create_link(self, source_device_id: str, target_device_id: str, label: str) -> dict[str, Any]:
        if source_device_id == target_device_id:
            raise ValueError("A node cannot link to itself")
        with self._lock, self._connection:
            count = self._connection.execute(
                "SELECT COUNT(*) FROM devices WHERE device_id IN (?, ?)",
                (source_device_id, target_device_id),
            ).fetchone()[0]
            if count != 2:
                raise KeyError("One or both devices do not exist")
            self._connection.execute(
                """
                INSERT INTO links (source_device_id, target_device_id, label, created_at)
                VALUES (?, ?, ?, ?)
                ON CONFLICT(source_device_id, target_device_id)
                DO UPDATE SET label = excluded.label
                """,
                (source_device_id, target_device_id, label, utc_now()),
            )
            row = self._connection.execute(
                "SELECT id FROM links WHERE source_device_id = ? AND target_device_id = ?",
                (source_device_id, target_device_id),
            ).fetchone()
        return self.get_link(row["id"])

    def get_link(self, link_id: int) -> dict[str, Any]:
        with self._lock:
            row = self._connection.execute("SELECT * FROM links WHERE id = ?", (link_id,)).fetchone()
        if row is None:
            raise KeyError(link_id)
        devices = {device["device_id"]: device for device in self.list_devices()}
        return self._link_dict(row, devices, self.get_settings())

    def list_links(self) -> list[dict[str, Any]]:
        devices = {device["device_id"]: device for device in self.list_devices()}
        with self._lock:
            rows = self._connection.execute("SELECT * FROM links ORDER BY id").fetchall()
        settings = self.get_settings()
        return [self._link_dict(row, devices, settings) for row in rows]

    def delete_link(self, link_id: int) -> bool:
        with self._lock, self._connection:
            cursor = self._connection.execute("DELETE FROM links WHERE id = ?", (link_id,))
        return cursor.rowcount > 0

    def add_weather_location(self, location: dict[str, Any]) -> dict[str, Any]:
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO weather_locations (
                    name, display_name, latitude, longitude, timezone,
                    admin1, admin2, admin3, country, created_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(latitude, longitude) DO UPDATE SET
                    name = excluded.name,
                    display_name = excluded.display_name,
                    timezone = excluded.timezone,
                    admin1 = excluded.admin1,
                    admin2 = excluded.admin2,
                    admin3 = excluded.admin3,
                    country = excluded.country
                """,
                (
                    location["name"], location["display_name"], location["latitude"],
                    location["longitude"], location["timezone"], location["admin1"],
                    location["admin2"], location["admin3"], location["country"], utc_now(),
                ),
            )
            row = self._connection.execute(
                "SELECT * FROM weather_locations WHERE latitude = ? AND longitude = ?",
                (location["latitude"], location["longitude"]),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_weather_locations(self) -> list[dict[str, Any]]:
        with self._lock:
            rows = self._connection.execute(
                "SELECT * FROM weather_locations ORDER BY name, id"
            ).fetchall()
        return [dict(row) for row in rows]

    def delete_weather_location(self, location_id: int) -> bool:
        with self._lock, self._connection:
            self._connection.execute(
                """
                UPDATE devices SET intersection_id = NULL, updated_at = ?
                WHERE intersection_id IN (
                    SELECT id FROM intersections WHERE weather_location_id = ?
                )
                """,
                (utc_now(), location_id),
            )
            cursor = self._connection.execute(
                "DELETE FROM weather_locations WHERE id = ?", (location_id,)
            )
        return cursor.rowcount > 0

    def get_weather_location(self, location_id: int) -> dict[str, Any] | None:
        with self._lock:
            row = self._connection.execute(
                "SELECT * FROM weather_locations WHERE id = ?", (location_id,)
            ).fetchone()
        return dict(row) if row else None

    def add_intersection(self, intersection: dict[str, Any]) -> dict[str, Any]:
        if self.get_weather_location(intersection["weather_location_id"]) is None:
            raise KeyError(intersection["weather_location_id"])
        with self._lock, self._connection:
            self._connection.execute(
                """
                INSERT INTO intersections (
                    weather_location_id, name, display_name, latitude, longitude, created_at
                ) VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(weather_location_id, latitude, longitude) DO UPDATE SET
                    name = excluded.name,
                    display_name = excluded.display_name
                """,
                (
                    intersection["weather_location_id"], intersection["name"],
                    intersection["display_name"], intersection["latitude"],
                    intersection["longitude"], utc_now(),
                ),
            )
            row = self._connection.execute(
                """
                SELECT i.*, w.name AS district_name
                FROM intersections i
                JOIN weather_locations w ON w.id = i.weather_location_id
                WHERE i.weather_location_id = ? AND i.latitude = ? AND i.longitude = ?
                """,
                (
                    intersection["weather_location_id"], intersection["latitude"],
                    intersection["longitude"],
                ),
            ).fetchone()
        assert row is not None
        return dict(row)

    def list_intersections(self, weather_location_id: int | None = None) -> list[dict[str, Any]]:
        query = """
            SELECT i.*, w.name AS district_name
            FROM intersections i
            JOIN weather_locations w ON w.id = i.weather_location_id
        """
        parameters: tuple[Any, ...] = ()
        if weather_location_id is not None:
            query += " WHERE i.weather_location_id = ?"
            parameters = (weather_location_id,)
        query += " ORDER BY w.name, i.name, i.id"
        with self._lock:
            rows = self._connection.execute(query, parameters).fetchall()
        return [dict(row) for row in rows]

    def delete_intersection(self, intersection_id: int) -> bool:
        with self._lock, self._connection:
            self._connection.execute(
                "UPDATE devices SET intersection_id = NULL, updated_at = ? WHERE intersection_id = ?",
                (utc_now(), intersection_id),
            )
            cursor = self._connection.execute(
                "DELETE FROM intersections WHERE id = ?", (intersection_id,)
            )
        return cursor.rowcount > 0

    @staticmethod
    def _device_select() -> str:
        return """
            SELECT d.*,
                   i.name AS intersection_name,
                   i.display_name AS intersection_display_name,
                   i.latitude AS location_latitude,
                   i.longitude AS location_longitude,
                   w.id AS weather_location_id,
                   w.name AS district_name
            FROM devices d
            LEFT JOIN intersections i ON i.id = d.intersection_id
            LEFT JOIN weather_locations w ON w.id = i.weather_location_id
        """

    @staticmethod
    def _device_dict(row: sqlite3.Row, settings: dict[str, float]) -> dict[str, Any]:
        distance = row["distance_cm"]
        tank_height = row["tank_height_cm"]
        water_level = None
        fill_percent = None
        if distance is not None and tank_height is not None and tank_height > 0:
            water_level = max(0.0, min(float(tank_height), float(tank_height) - float(distance)))
            fill_percent = water_level / float(tank_height) * 100.0
        flood_alert_level = None
        flood_status = "uncalibrated"
        if fill_percent is not None:
            if fill_percent >= settings["flood_level2_percent"]:
                flood_alert_level, flood_status = 2, "critical"
            elif fill_percent >= settings["flood_level1_percent"]:
                flood_alert_level, flood_status = 1, "warning"
            else:
                flood_alert_level, flood_status = 0, "normal"
        return {
            "hardware_id": row["hardware_id"], "device_id": row["device_id"],
            "hostname": row["hostname"], "ip": row["ip"], "type": row["type"],
            "firmware": row["firmware"], "topic": row["topic"], "label": row["label"],
            "tank_height_cm": tank_height, "map_x": row["map_x"], "map_y": row["map_y"],
            "intersection_id": row["intersection_id"],
            "intersection_name": row["intersection_name"],
            "intersection_display_name": row["intersection_display_name"],
            "weather_location_id": row["weather_location_id"],
            "district_name": row["district_name"],
            "location_latitude": row["location_latitude"],
            "location_longitude": row["location_longitude"],
            "mqtt_status": row["mqtt_status"], "online": row["mqtt_status"] == "online",
            "distance_cm": distance,
            "water_level_cm": round(water_level, 2) if water_level is not None else None,
            "fill_percent": round(fill_percent, 1) if fill_percent is not None else None,
            "flood_alert_level": flood_alert_level, "flood_status": flood_status,
            "last_seen": row["last_seen"], "created_at": row["created_at"],
            "updated_at": row["updated_at"],
        }

    @staticmethod
    def _link_dict(
        row: sqlite3.Row,
        devices: dict[str, dict[str, Any]],
        settings: dict[str, float],
    ) -> dict[str, Any]:
        source = devices[row["source_device_id"]]
        target = devices[row["target_device_id"]]
        distance_delta = None
        level_delta = None
        if source["distance_cm"] is not None and target["distance_cm"] is not None:
            distance_delta = round(source["distance_cm"] - target["distance_cm"], 2)
        if source["water_level_cm"] is not None and target["water_level_cm"] is not None:
            level_delta = round(source["water_level_cm"] - target["water_level_cm"], 2)
        blockage_alert_level = None
        blockage_status = "uncalibrated"
        if level_delta is not None:
            absolute_delta = abs(level_delta)
            if absolute_delta >= settings["blockage_level2_cm"]:
                blockage_alert_level, blockage_status = 2, "critical"
            elif absolute_delta >= settings["blockage_level1_cm"]:
                blockage_alert_level, blockage_status = 1, "warning"
            else:
                blockage_alert_level, blockage_status = 0, "normal"
        return {
            "id": row["id"], "source_device_id": row["source_device_id"],
            "target_device_id": row["target_device_id"], "label": row["label"],
            "distance_delta_cm": distance_delta, "water_level_delta_cm": level_delta,
            "blockage_alert_level": blockage_alert_level,
            "blockage_status": blockage_status,
            "created_at": row["created_at"],
        }
