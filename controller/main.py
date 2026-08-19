"""Water Controller Node: FastAPI dashboard, SQLite registry, and MQTT bridge."""

from __future__ import annotations

import asyncio
import os
import re
from contextlib import asynccontextmanager, suppress
from pathlib import Path
from typing import Any

import paho.mqtt.client as mqtt
from fastapi import FastAPI, HTTPException, Query, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse, PlainTextResponse
from fastapi.staticfiles import StaticFiles

from device_registry import DeviceRegistry, utc_now
from mdns_service import MDNSAdvertiser
from ml_service import FloodRiskModel
from models import (
    AlertSettingsRequest,
    DeviceUpdateRequest,
    IntersectionCreateRequest,
    LinkCreateRequest,
    RegistrationRequest,
    WeatherLocationCreateRequest,
)
from weather_service import WEATHER_REFRESH_SECONDS, WeatherProviderError, WeatherService

BASE_DIR = Path(__file__).resolve().parent
DATABASE_PATH = Path(os.getenv("WATER_DB_PATH", BASE_DIR / "data" / "water_controller.db"))
ML_MODEL_PATH = Path(os.getenv("WATER_ML_MODEL_PATH", BASE_DIR / "ml" / "model.json"))
MQTT_HOST = os.getenv("WATER_MQTT_HOST", "127.0.0.1")
MQTT_PORT = int(os.getenv("WATER_MQTT_PORT", "1883"))
MQTT_CLIENT_ID = os.getenv("WATER_MQTT_CLIENT_ID", "water-controller-node")
AUTO_LOAD_HANOI_DISTRICTS = os.getenv(
    "WATER_AUTO_LOAD_HANOI_DISTRICTS", "true"
).lower() not in {"0", "false", "no", "off"}
HANOI_URBAN_DISTRICTS = (
    "Ba Đình",
    "Bắc Từ Liêm",
    "Cầu Giấy",
    "Đống Đa",
    "Hà Đông",
    "Hai Bà Trưng",
    "Hoàn Kiếm",
    "Hoàng Mai",
    "Long Biên",
    "Nam Từ Liêm",
    "Tây Hồ",
    "Thanh Xuân",
)
HANOI_DISTRICT_FALLBACK_COORDINATES = {
    "Ba Đình": (21.0358, 105.8287),
    "Bắc Từ Liêm": (21.0730, 105.7700),
    "Cầu Giấy": (21.0328, 105.7907),
    "Đống Đa": (21.0183, 105.8291),
    "Hà Đông": (20.9712, 105.7788),
    "Hai Bà Trưng": (21.0060, 105.8575),
    "Hoàn Kiếm": (21.0286, 105.8506),
    "Hoàng Mai": (20.9740, 105.8530),
    "Long Biên": (21.0365, 105.8985),
    "Nam Từ Liêm": (21.0128, 105.7608),
    "Tây Hồ": (21.0692, 105.8115),
    "Thanh Xuân": (20.9930, 105.8110),
}

registry = DeviceRegistry(DATABASE_PATH)


class DashboardSockets:
    def __init__(self) -> None:
        self.connections: set[WebSocket] = set()

    async def connect(self, socket: WebSocket) -> None:
        await socket.accept()
        self.connections.add(socket)

    def disconnect(self, socket: WebSocket) -> None:
        self.connections.discard(socket)

    async def broadcast(self, payload: dict[str, Any]) -> None:
        dead: list[WebSocket] = []
        for socket in self.connections:
            try:
                await socket.send_json(payload)
            except Exception:
                dead.append(socket)
        for socket in dead:
            self.disconnect(socket)


sockets = DashboardSockets()


class MQTTBridge:
    def __init__(self) -> None:
        self.connected = False
        self.loop: asyncio.AbstractEventLoop | None = None
        self.client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=MQTT_CLIENT_ID,
        )
        self.client.on_connect = self._on_connect
        self.client.on_disconnect = self._on_disconnect
        self.client.on_message = self._on_message
        self.client.reconnect_delay_set(min_delay=1, max_delay=30)

    def start(self, loop: asyncio.AbstractEventLoop) -> None:
        self.loop = loop
        self.client.connect_async(MQTT_HOST, MQTT_PORT, keepalive=30)
        self.client.loop_start()

    def stop(self) -> None:
        self.client.disconnect()
        self.client.loop_stop()

    def _on_connect(self, client: mqtt.Client, userdata: Any, flags: Any, reason_code: Any, properties: Any) -> None:
        del userdata, flags, properties
        self.connected = reason_code == 0
        if self.connected:
            client.subscribe("devices/+/status")
            client.subscribe("devices/+/distance_cm")
        self._schedule_broadcast()

    def _on_disconnect(
        self,
        client: mqtt.Client,
        userdata: Any,
        disconnect_flags: Any,
        reason_code: Any,
        properties: Any,
    ) -> None:
        del client, userdata, disconnect_flags, reason_code, properties
        self.connected = False
        self._schedule_broadcast()

    def _on_message(self, client: mqtt.Client, userdata: Any, message: mqtt.MQTTMessage) -> None:
        del client, userdata
        parts = message.topic.split("/")
        if len(parts) != 3 or parts[0] != "devices":
            return
        device_id, metric = parts[1], parts[2]
        payload = message.payload.decode("utf-8", errors="replace").strip()
        if registry.update_telemetry(device_id, metric, payload):
            self._schedule_broadcast()

    def _schedule_broadcast(self) -> None:
        if self.loop is None or self.loop.is_closed():
            return
        self.loop.call_soon_threadsafe(
            lambda: asyncio.create_task(sockets.broadcast(dashboard_payload()))
        )

    def publish_command(self, topic: str, command: str) -> bool:
        """Publish a QoS 1 command and wait until the broker accepts it."""
        if not self.connected:
            return False
        message = self.client.publish(f"{topic}/command", command, qos=1, retain=False)
        if message.rc != mqtt.MQTT_ERR_SUCCESS:
            return False
        try:
            message.wait_for_publish(timeout=2.0)
        except RuntimeError:
            return False
        return message.is_published()


mqtt_bridge = MQTTBridge()
mdns_advertiser = MDNSAdvertiser(port=8000)
weather_service = WeatherService()
flood_risk_model = FloodRiskModel(ML_MODEL_PATH)


def dashboard_payload() -> dict[str, Any]:
    devices = registry.list_devices()
    links = registry.list_links()
    settings = registry.get_settings()
    weather = weather_service.snapshot(settings)
    devices_by_id = {device["device_id"]: device for device in devices}
    forecasts_by_id = {int(item["id"]): item for item in weather["locations"]}
    linked_device_ids = {
        device_id
        for link in links
        for device_id in (link["source_device_id"], link["target_device_id"])
    }
    rise_rates = {
        device_id: registry.get_rise_rate_cm_per_min(device_id)
        for device_id in linked_device_ids
    }
    ml_predictions = [
        flood_risk_model.evaluate_link(
            link,
            devices_by_id,
            forecasts_by_id,
            rise_rates,
            weather_stale=bool(weather["stale"]),
        )
        for link in links
    ]
    alerts: list[dict[str, Any]] = []
    for device in devices:
        if device["flood_alert_level"] in {1, 2}:
            alerts.append({
                "kind": "flood",
                "level": device["flood_alert_level"],
                "entity_id": device["device_id"],
                "title": f"Flood risk at {device['label']}",
                "value": device["fill_percent"],
                "unit": "%",
            })
    for link in links:
        if link["blockage_alert_level"] in {1, 2}:
            alerts.append({
                "kind": "blockage",
                "level": link["blockage_alert_level"],
                "entity_id": link["id"],
                "title": f"Blockage risk {link['source_device_id']} → {link['target_device_id']}",
                "value": abs(link["water_level_delta_cm"]),
                "unit": "cm",
            })
    for forecast in weather["locations"]:
        if forecast["rain_alert_level"] in {1, 2}:
            alerts.append({
                "kind": "weather",
                "level": forecast["rain_alert_level"],
                "entity_id": forecast["id"],
                "title": f"Heavy rain risk at {forecast['name']}",
                "value": forecast["rain_6h_mm"],
                "unit": "mm/6h",
            })
    for prediction in ml_predictions:
        if prediction["active"] and prediction["alert_level"] == 1:
            alerts.append({
                "kind": "ml_flood_risk",
                "level": 1,
                "entity_id": prediction["link_id"],
                "title": (
                    "ML early flood risk "
                    f"{prediction['source_device_id']} → {prediction['target_device_id']}"
                ),
                "value": prediction["probability"] * 100.0,
                "unit": "% synthetic probability",
            })
    alerts.sort(key=lambda alert: alert["level"], reverse=True)
    return {
        "devices": devices,
        "links": links,
        "weather_locations": registry.list_weather_locations(),
        "intersections": registry.list_intersections(),
        "alerts": alerts,
        "settings": settings,
        "weather": weather,
        "ml": {
            **flood_risk_model.status(),
            "predictions": ml_predictions,
            "policy": "rain_gated_l1_only",
        },
        "mqtt_connected": mqtt_bridge.connected,
        "generated_at": utc_now(),
    }


async def weather_refresh_loop() -> None:
    while True:
        await weather_service.refresh(registry.list_weather_locations())
        await sockets.broadcast(dashboard_payload())
        await asyncio.sleep(WEATHER_REFRESH_SECONDS)


async def bootstrap_hanoi_districts() -> None:
    """Populate the standard Hanoi urban districts without blocking startup."""
    if not AUTO_LOAD_HANOI_DISTRICTS:
        return

    existing_names = {
        location["name"].strip().casefold()
        for location in registry.list_weather_locations()
    }
    missing = [
        district
        for district in HANOI_URBAN_DISTRICTS
        if district.casefold() not in existing_names
    ]
    semaphore = asyncio.Semaphore(3)

    async def add_district(district: str) -> None:
        async with semaphore:
            try:
                results = await weather_service.geocode(f"{district}, Hà Nội")
                if results:
                    location = dict(results[0])
                else:
                    latitude, longitude = HANOI_DISTRICT_FALLBACK_COORDINATES[district]
                    location = {
                        "latitude": latitude,
                        "longitude": longitude,
                        "timezone": "Asia/Bangkok",
                        "admin1": "Hà Nội",
                        "admin2": district,
                        "admin3": "",
                        "country": "Việt Nam",
                    }
                location["name"] = district
                location["display_name"] = f"{district}, Hà Nội, Việt Nam"
                registry.add_weather_location(location)
                source = "Open-Meteo" if results else "fallback coordinates"
                message = f"[WEATHER] Auto-loaded district {district!r} ({source})"
                print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
            except Exception as error:
                message = f"[WEATHER] Could not auto-load {district!r}: {error!r}"
                print(message.encode("ascii", "backslashreplace").decode("ascii"), flush=True)

    await asyncio.gather(*(add_district(district) for district in missing))
    await weather_service.refresh(registry.list_weather_locations())
    await sockets.broadcast(dashboard_payload())


@asynccontextmanager
async def lifespan(app_instance: FastAPI):
    del app_instance
    mqtt_bridge.start(asyncio.get_running_loop())
    # zeroconf performs synchronous registration checks; keep them off the
    # FastAPI event loop so startup and WebSocket broadcasts remain responsive.
    await asyncio.to_thread(mdns_advertiser.start)
    await bootstrap_hanoi_districts()
    weather_task = asyncio.create_task(weather_refresh_loop())
    try:
        yield
    finally:
        weather_task.cancel()
        with suppress(asyncio.CancelledError):
            await weather_task
        await asyncio.to_thread(mdns_advertiser.stop)
        mqtt_bridge.stop()


app = FastAPI(title="Water Controller Node", version="2.6.0", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=BASE_DIR / "static"), name="static")


@app.get("/", include_in_schema=False)
async def dashboard() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "index.html")


@app.get("/settings", include_in_schema=False)
async def settings_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "settings.html")


@app.get("/weather", include_in_schema=False)
async def weather_page() -> FileResponse:
    return FileResponse(BASE_DIR / "static" / "weather.html")


@app.post("/api/devices/register", response_class=PlainTextResponse)
async def register_device(request: RegistrationRequest) -> str:
    record = registry.register(
        hardware_id=request.hardware_id,
        hostname=request.hostname,
        ip=str(request.ip),
        device_type=request.type,
        firmware=request.firmware,
    )
    await sockets.broadcast(dashboard_payload())
    return f"{record['device_id']}|{record['topic']}"


@app.get("/api/devices")
async def list_devices() -> list[dict[str, Any]]:
    return registry.list_devices()


@app.patch("/api/devices/{device_id}")
async def update_device(device_id: str, request: DeviceUpdateRequest) -> dict[str, Any]:
    try:
        record = registry.update_device(
            device_id, request.model_dump(exclude_unset=True, exclude_none=True)
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Device not found") from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await sockets.broadcast(dashboard_payload())
    return record


@app.post("/api/devices/{device_id}/wifi-reset")
async def reprovision_device_wifi(device_id: str) -> dict[str, Any]:
    record = registry.get_by_device_id(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy node")
    if not mqtt_bridge.connected:
        raise HTTPException(
            status_code=503,
            detail="MQTT đang mất kết nối; chưa thể gửi lệnh đổi Wi-Fi",
        )
    if not record["online"]:
        raise HTTPException(
            status_code=409,
            detail="Node đang offline. Hãy bật node và chờ online trước khi đổi Wi-Fi",
        )
    delivered = await asyncio.to_thread(
        mqtt_bridge.publish_command, record["topic"], "wifi_reset"
    )
    if not delivered:
        raise HTTPException(
            status_code=503,
            detail="Broker chưa xác nhận lệnh đổi Wi-Fi",
        )
    return {
        "device_id": device_id,
        "hardware_id": record["hardware_id"],
        "wifi_reset_sent": True,
        "registration_preserved": True,
    }


@app.delete("/api/devices/{device_id}")
async def kick_and_delete_device(device_id: str) -> dict[str, Any]:
    record = registry.get_by_device_id(device_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy node")
    if not mqtt_bridge.connected:
        raise HTTPException(
            status_code=503,
            detail="MQTT đang mất kết nối; chưa thể gửi lệnh factory reset",
        )
    if not record["online"]:
        raise HTTPException(
            status_code=409,
            detail="Node đang offline. Hãy bật node và chờ trạng thái online trước khi kick",
        )

    delivered = await asyncio.to_thread(
        mqtt_bridge.publish_command, record["topic"], "factory_reset"
    )
    if not delivered:
        raise HTTPException(
            status_code=503,
            detail="Broker chưa xác nhận lệnh factory reset; node chưa bị xóa",
        )

    # The current ESP32 firmware waits 100 ms after clearing NVS before reboot.
    # Keep the registry briefly so its callback can consume the command first.
    await asyncio.sleep(0.5)
    if not registry.delete_device(device_id):
        raise HTTPException(status_code=404, detail="Node đã bị xóa trước đó")
    await sockets.broadcast(dashboard_payload())
    return {
        "device_id": device_id,
        "hardware_id": record["hardware_id"],
        "factory_reset_sent": True,
        "links_deleted": True,
    }


@app.post("/api/devices/{device_id}/factory-reset")
async def factory_reset_device_by_id(device_id: str) -> dict[str, Any]:
    """Reset a node by MQTT ID, including nodes missing from the registry."""
    if re.fullmatch(r"water-[A-Za-z0-9_-]{1,48}", device_id) is None:
        raise HTTPException(
            status_code=400,
            detail="Device ID không hợp lệ; định dạng yêu cầu water-xxx",
        )
    if not mqtt_bridge.connected:
        raise HTTPException(
            status_code=503,
            detail="MQTT đang mất kết nối; chưa thể gửi lệnh factory reset",
        )

    record = registry.get_by_device_id(device_id)
    delivered = await asyncio.to_thread(
        mqtt_bridge.publish_command, f"devices/{device_id}", "factory_reset"
    )
    if not delivered:
        raise HTTPException(
            status_code=503,
            detail="Broker chưa xác nhận lệnh factory reset",
        )

    await asyncio.sleep(0.5)
    registry_deleted = registry.delete_device(device_id) if record else False
    if registry_deleted:
        await sockets.broadcast(dashboard_payload())
    return {
        "device_id": device_id,
        "factory_reset_sent": True,
        "registry_deleted": registry_deleted,
    }


@app.get("/api/links")
async def list_links() -> list[dict[str, Any]]:
    return registry.list_links()


@app.get("/api/settings")
async def get_settings() -> dict[str, float]:
    return registry.get_settings()


@app.put("/api/settings")
async def update_settings(request: AlertSettingsRequest) -> dict[str, float]:
    settings = registry.update_settings(request.model_dump())
    await sockets.broadcast(dashboard_payload())
    return settings


@app.get("/api/weather/geocode")
async def geocode_weather_location(
    q: str = Query(min_length=2, max_length=120),
) -> list[dict[str, Any]]:
    try:
        return await weather_service.geocode(q)
    except WeatherProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/weather/locations")
async def list_weather_locations() -> list[dict[str, Any]]:
    return registry.list_weather_locations()


@app.post("/api/weather/locations", status_code=201)
async def add_weather_location(request: WeatherLocationCreateRequest) -> dict[str, Any]:
    location = registry.add_weather_location(request.model_dump())
    await weather_service.refresh(registry.list_weather_locations())
    await sockets.broadcast(dashboard_payload())
    return location


@app.delete("/api/weather/locations/{location_id}", status_code=204)
async def delete_weather_location(location_id: int) -> None:
    if not registry.delete_weather_location(location_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy khu vực")
    await weather_service.refresh(registry.list_weather_locations())
    await sockets.broadcast(dashboard_payload())


@app.get("/api/intersections/search")
async def search_intersections(
    weather_location_id: int = Query(gt=0),
    q: str = Query(min_length=2, max_length=160),
) -> list[dict[str, Any]]:
    district = registry.get_weather_location(weather_location_id)
    if district is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy quận/khu vực")
    try:
        return await weather_service.search_intersections(q, district)
    except WeatherProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/intersections/discover")
async def discover_intersections(
    weather_location_id: int = Query(gt=0),
) -> list[dict[str, Any]]:
    district = registry.get_weather_location(weather_location_id)
    if district is None:
        raise HTTPException(status_code=404, detail="Không tìm thấy quận/khu vực")
    try:
        return await weather_service.discover_intersections(district)
    except WeatherProviderError as error:
        raise HTTPException(status_code=502, detail=str(error)) from error


@app.get("/api/intersections")
async def list_intersections(
    weather_location_id: int | None = Query(default=None, gt=0),
) -> list[dict[str, Any]]:
    return registry.list_intersections(weather_location_id)


@app.post("/api/intersections", status_code=201)
async def add_intersection(request: IntersectionCreateRequest) -> dict[str, Any]:
    try:
        intersection = registry.add_intersection(request.model_dump())
    except KeyError as error:
        raise HTTPException(status_code=404, detail="Không tìm thấy quận/khu vực") from error
    await sockets.broadcast(dashboard_payload())
    return intersection


@app.delete("/api/intersections/{intersection_id}", status_code=204)
async def delete_intersection(intersection_id: int) -> None:
    if not registry.delete_intersection(intersection_id):
        raise HTTPException(status_code=404, detail="Không tìm thấy nút giao")
    await sockets.broadcast(dashboard_payload())


@app.get("/api/weather")
async def weather_data() -> dict[str, Any]:
    return weather_service.snapshot(registry.get_settings())


@app.post("/api/weather/refresh")
async def refresh_weather() -> dict[str, Any]:
    await weather_service.refresh(registry.list_weather_locations())
    await sockets.broadcast(dashboard_payload())
    return weather_service.snapshot(registry.get_settings())


@app.post("/api/links", status_code=201)
async def create_link(request: LinkCreateRequest) -> dict[str, Any]:
    try:
        link = registry.create_link(
            request.source_device_id, request.target_device_id, request.label
        )
    except KeyError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error
    await sockets.broadcast(dashboard_payload())
    return link


@app.delete("/api/links/{link_id}", status_code=204)
async def delete_link(link_id: int) -> None:
    if not registry.delete_link(link_id):
        raise HTTPException(status_code=404, detail="Link not found")
    await sockets.broadcast(dashboard_payload())


@app.get("/api/dashboard")
async def dashboard_data() -> dict[str, Any]:
    return dashboard_payload()


@app.get("/api/ml")
async def ml_status() -> dict[str, Any]:
    payload = dashboard_payload()
    return payload["ml"]


@app.websocket("/ws")
async def dashboard_socket(socket: WebSocket) -> None:
    await sockets.connect(socket)
    try:
        await socket.send_json(dashboard_payload())
        while True:
            await socket.receive_text()
    except WebSocketDisconnect:
        sockets.disconnect(socket)


@app.get("/health")
async def health() -> dict[str, Any]:
    return {
        "status": "ok",
        "mqtt_connected": mqtt_bridge.connected,
        "mdns": mdns_advertiser.status(),
        "weather": {
            "locations": len(registry.list_weather_locations()),
            "updated_at": weather_service.updated_at,
            "stale": weather_service.stale,
            "error": weather_service.error,
        },
        "ml": flood_risk_model.status(),
        "database": str(DATABASE_PATH),
    }
