# Water Controller Node

Webapp FastAPI tập trung cho mạng ESP32 đo mực nước. Controller đăng ký node, subscribe MQTT, lưu trạng thái mới nhất trong SQLite, cập nhật dashboard qua WebSocket và quản lý graph liên kết giữa các sensor.

## Cài và chạy trên Raspberry Pi

Production nên dùng `deploy/pi/install.sh` để cài Mosquitto, app và systemd
service. Nếu cần chạy thủ công để debug ngay trên Pi:

```bash
cd ~/water-controller-project/controller
python3 -m venv .venv
./.venv/bin/python -m pip install -r requirements.txt
./.venv/bin/python -m uvicorn main:app --host 0.0.0.0 --port 8000
```

Mosquitto mặc định là `127.0.0.1:1883`. Có thể đổi bằng biến môi trường `WATER_MQTT_HOST`, `WATER_MQTT_PORT`; đường dẫn database có thể đổi bằng `WATER_DB_PATH`.

Mở:

```text
Dashboard  http://edge-controller.local:8000/
Settings   http://edge-controller.local:8000/settings
Weather    http://edge-controller.local:8000/weather
Swagger    http://edge-controller.local:8000/docs
Health     http://edge-controller.local:8000/health
```

Khi chạy Python thủ công, controller có thể bật bộ quảng bá Zeroconf bằng
`WATER_MDNS_ENABLED`, `WATER_MDNS_HOSTNAMES`, `WATER_MDNS_ADDRESS` và
`WATER_HTTP_PORT`. Bản triển khai Raspberry Pi chuẩn tắt bộ quảng bá này và
dùng `avahi-daemon` làm nguồn duy nhất cho hostname
`edge-controller.local`, dashboard `:8000` và MQTT `:1883`. Địa chỉ IP trực
tiếp vẫn dùng được khi client hoặc router không hỗ trợ mDNS.

## Sử dụng dashboard

- Node đăng ký mới tự xuất hiện trên bản đồ.
- Nhấn node để đổi tên và nhập chiều cao bể.
- Chọn node đang online rồi nhấn **Kick & xóa node** để gửi `factory_reset`,
  xóa Wi-Fi/NVS trên ESP32 và xóa node cùng mọi link liên quan khỏi controller.
- Trong cấu hình node, chọn **Quận/khu vực → Nút giao tuyến phố** để gắn vị
  trí thực tế và tọa độ cho sensor.
- Kéo node trên canvas để lưu vị trí.
- Chọn node nguồn/đích rồi **Tạo link** để theo dõi chênh lệch.
- Dashboard cập nhật qua WebSocket khi nhận MQTT; polling 10 giây là fallback.
- Khối Logistic Regression trên dashboard chỉ hoạt động khi hai node đã link,
  online, có khu vực weather và Open-Meteo cho thấy đang/có khả năng mưa.

## Cảnh báo L0-L1-L2

Trang `/settings` cấu hình bốn ngưỡng toàn mạng:

- Blockage L1/L2 theo `abs(water level nguồn - water level đích)` của từng link.
- Flood L1/L2 theo phần trăm đầy của từng node.
- L0 tự động áp dụng khi giá trị nhỏ hơn ngưỡng L1.
- Rain forecast L1/L2 theo tổng lượng mưa Open-Meteo trong 6 giờ tới tại
  từng quận/khu vực đã cấu hình.

Node/link chưa hiệu chuẩn chiều cao bể có status `uncalibrated` và không phát cảnh báo. Thay đổi settings được lưu SQLite, tính lại tức thời và broadcast qua WebSocket.

Model tại `ml/model.json` được train từ 480 mẫu synthetic và có
`field_validated=false`. Nó chỉ bổ sung L1; cảnh báo vật lý L2 luôn do luật
mực nước quyết định. `telemetry_history` lưu khoảng cách theo timestamp để tính
tốc độ dâng. Chạy `python ml/generate_synthetic_data.py` rồi
`python ml/train_model.py` để tái tạo artifact (cần NumPy theo
`ml/requirements-train.txt`).

HY-SRF05 đo khoảng cách từ sensor xuống mặt nước:

```text
water_level_cm = tank_height_cm - distance_cm
```

Kết quả được clamp vào `0..tank_height_cm`. Link hiển thị `water level nguồn − water level đích`; nếu một node chưa có chiều cao bể, nó hiển thị chênh lệch distance thay thế.

## API chính

- `POST /api/devices/register`: đăng ký idempotent theo hardware ID.
- `GET /api/devices`: danh sách node và telemetry mới nhất.
- `PATCH /api/devices/{device_id}`: label, tank height và vị trí map.
- `POST /api/devices/{device_id}/wifi-reset`: đưa firmware 1.2+ về provisioning
  Wi-Fi nhưng giữ device ID, vị trí và link.
- `DELETE /api/devices/{device_id}`: kick node online bằng MQTT `factory_reset`,
  sau đó xóa node và cascade toàn bộ link liên quan.
- `GET /api/dashboard`: snapshot node/link/MQTT.
- `GET /api/ml`: trạng thái model, rain gate và xác suất theo từng link.
- `GET/PUT /api/settings`: đọc hoặc cập nhật ngưỡng cảnh báo.
- `POST /api/links`: tạo hoặc cập nhật link có hướng.
- `DELETE /api/links/{id}`: xóa link.
- `WS /ws`: realtime dashboard snapshots.

SQLite nằm tại `controller/data/water_controller.db`. Hardware ID và device ID có unique constraint nên mapping vẫn ổn định sau khi controller restart.

## Open-Meteo theo quận/khu vực Hà Nội

Khi khởi động, controller tự nạp 12 quận nội thành qua Open-Meteo Geocoding
vào SQLite rồi gọi Weather API theo lịch 15 phút. Trang `/weather` chỉ hiển
thị dự báo và cung cấp danh mục quản trị tùy chọn; không cần nhập quận thủ
công. Có thể tắt bootstrap bằng `WATER_AUTO_LOAD_HANOI_DISTRICTS=false`, đổi
chu kỳ bằng `WATER_WEATHER_REFRESH_SECONDS` và timeout bằng
`WATER_WEATHER_HTTP_TIMEOUT`.

Để gán vị trí node, mở form cấu hình node trên Dashboard và chọn quận.
Controller tự tải toàn bộ giao lộ có tên trong vùng từ OpenStreetMap; chọn một
giao lộ trong dropdown và lưu. Có thể nhập tên phố vào ô tìm nhanh để lọc cục
bộ danh sách mà không gọi API lại. Lần tải đầu được cache trong RAM, còn giao
lộ đã chọn sẽ tự lưu vào SQLite cùng cấu hình node.

API liên quan:

- `GET /api/weather/geocode?q=...`: tìm tọa độ địa điểm tại Hà Nội.
- `GET/POST /api/weather/locations`: liệt kê hoặc thêm khu vực.
- `DELETE /api/weather/locations/{id}`: ngừng theo dõi khu vực.
- `GET /api/weather`: snapshot dự báo đã cache.
- `POST /api/weather/refresh`: yêu cầu cập nhật ngay.
- `GET /api/intersections/search?weather_location_id=...&q=...`: nhập một tên
  phố và lấy các giao lộ thật dọc tuyến từ OpenStreetMap Overpass.
- `GET /api/intersections/discover?weather_location_id=...`: tải toàn bộ giao
  lộ có tên quanh quận để dùng trực tiếp trong dropdown cấu hình node.
- `GET/POST /api/intersections`: danh mục nút giao đã chọn.
- `DELETE /api/intersections/{id}`: xóa nút giao và gỡ vị trí khỏi node liên quan.

Danh sách toàn quận được cache và các truy vấn Overpass được giới hạn tuần tự.
Có thể đổi Overpass instance bằng `WATER_OVERPASS_URL` và định danh request
bằng `WATER_OVERPASS_USER_AGENT`. Nếu instance chính lỗi/timeout, controller
tự thử `WATER_OVERPASS_FALLBACK_URL` (mặc định là
`https://overpass.private.coffee/api/interpreter`). Nếu tải toàn quận vẫn lỗi,
ô tìm nhanh tự chuyển sang truy vấn theo tên phố để không chặn việc gán node.

Weather data by [Open-Meteo.com](https://open-meteo.com/).
Map data © [OpenStreetMap contributors](https://www.openstreetmap.org/copyright).
