"""Open-Meteo geocoding and cached district-level weather forecasts."""

from __future__ import annotations

import asyncio
import json
import os
import re
import time
import unicodedata
from datetime import UTC, datetime
from typing import Any
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen


GEOCODING_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"
OVERPASS_URL = os.getenv(
    "WATER_OVERPASS_URL", "https://overpass-api.de/api/interpreter"
)
OVERPASS_FALLBACK_URL = os.getenv(
    "WATER_OVERPASS_FALLBACK_URL", "https://overpass.private.coffee/api/interpreter"
)
OVERPASS_URLS = tuple(dict.fromkeys((OVERPASS_URL, OVERPASS_FALLBACK_URL)))
OVERPASS_USER_AGENT = os.getenv(
    "WATER_OVERPASS_USER_AGENT", "water-controller-node/2.2 (local ESP32 flood monitor)"
)
WEATHER_REFRESH_SECONDS = int(os.getenv("WATER_WEATHER_REFRESH_SECONDS", "900"))
HTTP_TIMEOUT_SECONDS = float(os.getenv("WATER_WEATHER_HTTP_TIMEOUT", "10"))


class WeatherProviderError(RuntimeError):
    pass


def _utc_now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds").replace("+00:00", "Z")


def _request_json(
    url: str, parameters: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    request_url = f"{url}?{urlencode(parameters)}"
    request_headers = {"User-Agent": "water-controller-node/2.2"}
    request_headers.update(headers or {})
    request = Request(request_url, headers=request_headers)
    try:
        with urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            return json.load(response)
    except HTTPError as exc:
        raise WeatherProviderError(f"Dịch vụ bản đồ/thời tiết trả HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise WeatherProviderError(f"Không kết nối được dịch vụ bản đồ/thời tiết: {exc}") from exc


def _post_form_json(
    url: str, parameters: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    request_headers = {"User-Agent": "water-controller-node/2.2"}
    request_headers.update(headers or {})
    request = Request(
        url,
        data=urlencode(parameters).encode("utf-8"),
        headers=request_headers,
    )
    try:
        with urlopen(request, timeout=max(HTTP_TIMEOUT_SECONDS, 75)) as response:
            return json.load(response)
    except HTTPError as exc:
        raise WeatherProviderError(f"OpenStreetMap Overpass trả HTTP {exc.code}") from exc
    except (URLError, TimeoutError, OSError, json.JSONDecodeError) as exc:
        raise WeatherProviderError(f"Không kết nối được OpenStreetMap Overpass: {exc}") from exc


def _request_overpass(
    parameters: dict[str, Any], headers: dict[str, str] | None = None
) -> Any:
    errors: list[str] = []
    for url in OVERPASS_URLS:
        try:
            return _post_form_json(url, parameters, headers)
        except WeatherProviderError as error:
            errors.append(f"{url}: {error}")
    raise WeatherProviderError(
        "Các máy chủ OpenStreetMap Overpass đều tạm thời không phản hồi: "
        + " | ".join(errors)
    )


def _condition_label(code: int | None) -> str:
    if code == 0:
        return "Trời quang"
    if code in {1, 2, 3}:
        return "Có mây"
    if code in {45, 48}:
        return "Sương mù"
    if code in {51, 53, 55, 56, 57}:
        return "Mưa phùn"
    if code in {61, 63, 65, 66, 67, 80, 81, 82}:
        return "Mưa"
    if code in {71, 73, 75, 77, 85, 86}:
        return "Tuyết"
    if code in {95, 96, 99}:
        return "Dông"
    return "Không xác định"


def _plain_text(value: str) -> str:
    return "".join(
        character
        for character in unicodedata.normalize("NFKD", value).lower()
        if not unicodedata.combining(character)
    ).replace("đ", "d")


class WeatherService:
    def __init__(self) -> None:
        self._forecasts: list[dict[str, Any]] = []
        self.updated_at: str | None = None
        self.error: str | None = None
        self.stale = False
        self._lock = asyncio.Lock()
        self._intersection_lock = asyncio.Lock()
        self._last_overpass_request = 0.0
        self._intersection_cache: dict[str, list[dict[str, Any]]] = {}

    async def geocode(self, query: str) -> list[dict[str, Any]]:
        payload = await asyncio.to_thread(
            _request_json,
            GEOCODING_URL,
            {
                "name": query,
                "count": 10,
                "language": "vi",
                "format": "json",
                "countryCode": "VN",
            },
        )
        results = []
        for item in payload.get("results", []):
            if item.get("country_code") != "VN":
                continue
            administrative_text = " ".join(
                str(item.get(key, "")) for key in ("name", "admin1", "admin2", "admin3", "admin4")
            )
            if "hanoi" not in _plain_text(administrative_text).replace(" ", ""):
                continue
            admin_parts = [
                value for value in (item.get("admin4"), item.get("admin3"), item.get("admin2"), item.get("admin1"))
                if value and value != item.get("name")
            ]
            results.append({
                "name": item.get("name", query),
                "display_name": ", ".join([item.get("name", query), *admin_parts, "Việt Nam"]),
                "latitude": float(item["latitude"]),
                "longitude": float(item["longitude"]),
                "timezone": item.get("timezone", "Asia/Bangkok"),
                "admin1": item.get("admin1", ""),
                "admin2": item.get("admin2", ""),
                "admin3": item.get("admin3", ""),
                "country": item.get("country", "Việt Nam"),
            })
        return results

    async def search_intersections(
        self, query: str, district: dict[str, Any]
    ) -> list[dict[str, Any]]:
        cache_key = f"{district['id']}:{_plain_text(query).strip()}"
        if cache_key in self._intersection_cache:
            return self._intersection_cache[cache_key]

        async with self._intersection_lock:
            if cache_key in self._intersection_cache:
                return self._intersection_cache[cache_key]
            elapsed = time.monotonic() - self._last_overpass_request
            if elapsed < 2.0:
                await asyncio.sleep(2.0 - elapsed)
            latitude = float(district["latitude"])
            longitude = float(district["longitude"])
            south, north = latitude - 0.04, latitude + 0.04
            west, east = longitude - 0.04, longitude + 0.04
            street_pattern = re.escape(query.strip()).replace('"', '\\"')
            road_types = "^(trunk|primary|secondary|tertiary|unclassified|residential)$"
            overpass_query = (
                f'[out:json][timeout:25][bbox:{south},{west},{north},{east}];'
                f'way["highway"~"{road_types}"]["name"~"{street_pattern}",i]->.target;'
                'node(w.target)->.candidate;'
                f'way(bn.candidate)["highway"~"{road_types}"]["name"]->.ways;'
                '(.candidate;.ways;);out body;'
            )
            payload = await asyncio.to_thread(
                _request_overpass,
                {"data": overpass_query},
                {"User-Agent": OVERPASS_USER_AGENT},
            )
            self._last_overpass_request = time.monotonic()

        results = self._parse_intersections(
            payload, district, normalized_query=_plain_text(query).strip(), limit=100
        )
        self._cache_intersections(cache_key, results)
        return results

    async def discover_intersections(
        self, district: dict[str, Any]
    ) -> list[dict[str, Any]]:
        """Load all named road intersections around a district and cache them."""
        cache_key = f"{district['id']}:*"
        if cache_key in self._intersection_cache:
            return self._intersection_cache[cache_key]

        async with self._intersection_lock:
            if cache_key in self._intersection_cache:
                return self._intersection_cache[cache_key]
            elapsed = time.monotonic() - self._last_overpass_request
            if elapsed < 2.0:
                await asyncio.sleep(2.0 - elapsed)
            latitude = float(district["latitude"])
            longitude = float(district["longitude"])
            south, north = latitude - 0.04, latitude + 0.04
            west, east = longitude - 0.04, longitude + 0.04
            road_types = "^(trunk|primary|secondary|tertiary|unclassified|residential)$"
            overpass_query = (
                f'[out:json][timeout:60][bbox:{south},{west},{north},{east}];'
                f'way["highway"~"{road_types}"]["name"]->.roads;'
                '(.roads;>;);out body qt;'
            )
            payload = await asyncio.to_thread(
                _request_overpass,
                {"data": overpass_query},
                {"User-Agent": OVERPASS_USER_AGENT},
            )
            self._last_overpass_request = time.monotonic()

        results = self._parse_intersections(payload, district, limit=None)
        self._cache_intersections(cache_key, results)
        return results

    @staticmethod
    def _parse_intersections(
        payload: dict[str, Any],
        district: dict[str, Any],
        normalized_query: str | None = None,
        limit: int | None = 100,
    ) -> list[dict[str, Any]]:
        elements = payload.get("elements", [])
        nodes = {
            item["id"]: (float(item["lat"]), float(item["lon"]))
            for item in elements
            if item.get("type") == "node" and "lat" in item and "lon" in item
        }
        street_names: dict[int, set[str]] = {node_id: set() for node_id in nodes}
        for item in elements:
            if item.get("type") != "way":
                continue
            street_name = item.get("tags", {}).get("name")
            if not street_name:
                continue
            for node_id in item.get("nodes", []):
                if node_id in street_names:
                    street_names[node_id].add(street_name)

        grouped: dict[str, list[list[tuple[float, float]]]] = {}
        for node_id, names in street_names.items():
            if len(names) < 2:
                continue
            ordered_names = sorted(names)
            if normalized_query and normalized_query not in _plain_text(" ".join(ordered_names)):
                continue
            intersection_name = " × ".join(ordered_names)
            point = nodes[node_id]
            clusters = grouped.setdefault(intersection_name, [])
            for cluster in clusters:
                center_latitude = sum(item[0] for item in cluster) / len(cluster)
                center_longitude = sum(item[1] for item in cluster) / len(cluster)
                if (
                    abs(point[0] - center_latitude) <= 0.00045
                    and abs(point[1] - center_longitude) <= 0.00045
                ):
                    cluster.append(point)
                    break
            else:
                clusters.append([point])

        results = []
        for intersection_name, clusters in grouped.items():
            for coordinates in clusters:
                node_latitude = sum(item[0] for item in coordinates) / len(coordinates)
                node_longitude = sum(item[1] for item in coordinates) / len(coordinates)
                results.append({
                    "weather_location_id": district["id"],
                    "name": intersection_name,
                    "display_name": f"{intersection_name}, {district['name']}, Hà Nội",
                    "latitude": node_latitude,
                    "longitude": node_longitude,
                })
        results.sort(key=lambda item: (item["name"], item["latitude"], item["longitude"]))
        return results if limit is None else results[:limit]

    def _cache_intersections(self, cache_key: str, results: list[dict[str, Any]]) -> None:
        if len(self._intersection_cache) >= 100:
            self._intersection_cache.pop(next(iter(self._intersection_cache)))
        self._intersection_cache[cache_key] = results

    async def refresh(self, locations: list[dict[str, Any]]) -> dict[str, Any]:
        async with self._lock:
            if not locations:
                self._forecasts = []
                self.updated_at = _utc_now()
                self.error = None
                self.stale = False
                return self.snapshot({})
            try:
                payload = await asyncio.to_thread(self._fetch_forecasts, locations)
                self._forecasts = payload
                self.updated_at = _utc_now()
                self.error = None
                self.stale = False
            except (WeatherProviderError, KeyError, TypeError, ValueError) as exc:
                self.error = str(exc)
                self.stale = bool(self._forecasts)
            return self.snapshot({})

    def _fetch_forecasts(self, locations: list[dict[str, Any]]) -> list[dict[str, Any]]:
        payload = _request_json(
            FORECAST_URL,
            {
                "latitude": ",".join(str(item["latitude"]) for item in locations),
                "longitude": ",".join(str(item["longitude"]) for item in locations),
                "current": "temperature_2m,relative_humidity_2m,precipitation,rain,weather_code",
                "hourly": "precipitation_probability,precipitation,rain,showers,weather_code",
                "forecast_hours": 24,
                "timezone": "auto",
            },
        )
        responses = payload if isinstance(payload, list) else [payload]
        if len(responses) != len(locations):
            raise WeatherProviderError("Open-Meteo trả về sai số lượng địa điểm")
        return [self._parse_forecast(location, data) for location, data in zip(locations, responses)]

    @staticmethod
    def _parse_forecast(location: dict[str, Any], data: dict[str, Any]) -> dict[str, Any]:
        hourly = data.get("hourly", {})
        precipitation = [float(value or 0) for value in hourly.get("precipitation", [])]
        probabilities = [int(value or 0) for value in hourly.get("precipitation_probability", [])]

        def rainfall(hours: int) -> float:
            return round(sum(precipitation[:hours]), 2)

        current = data.get("current", {})
        code = current.get("weather_code")
        return {
            **location,
            "provider": "Open-Meteo",
            "condition": _condition_label(int(code)) if code is not None else "Không xác định",
            "weather_code": code,
            "temperature_c": current.get("temperature_2m"),
            "humidity_percent": current.get("relative_humidity_2m"),
            "current_precipitation_mm": current.get("precipitation"),
            "rain_1h_mm": rainfall(1),
            "rain_3h_mm": rainfall(3),
            "rain_6h_mm": rainfall(6),
            "rain_24h_mm": rainfall(24),
            "max_probability_6h": max(probabilities[:6], default=0),
            "max_probability_24h": max(probabilities[:24], default=0),
            "hourly": [
                {
                    "time": time,
                    "precipitation_mm": precipitation[index] if index < len(precipitation) else 0,
                    "probability_percent": probabilities[index] if index < len(probabilities) else 0,
                }
                for index, time in enumerate(hourly.get("time", [])[:24])
            ],
        }

    def snapshot(self, settings: dict[str, float]) -> dict[str, Any]:
        level1 = settings.get("rain_level1_6h_mm", 10.0)
        level2 = settings.get("rain_level2_6h_mm", 30.0)
        forecasts = []
        for forecast in self._forecasts:
            rain = forecast["rain_6h_mm"]
            if rain >= level2:
                level, status = 2, "critical"
            elif rain >= level1:
                level, status = 1, "warning"
            else:
                level, status = 0, "normal"
            forecasts.append({**forecast, "rain_alert_level": level, "rain_status": status})
        return {
            "locations": forecasts,
            "updated_at": self.updated_at,
            "refresh_seconds": WEATHER_REFRESH_SECONDS,
            "stale": self.stale,
            "error": self.error,
        }
