# Water Controller Node — ESP32 + Raspberry Pi Edge IoT

Hệ thống giám sát mực nước gồm nhiều ESP32/HY-SRF05 gửi dữ liệu về một
Raspberry Pi chạy FastAPI, Mosquitto MQTT, SQLite và dashboard web realtime.
Controller quản lý node, liên kết giữa các node, cảnh báo ngập/chênh lệch,
vị trí nút giao và dự báo mưa theo quận Hà Nội.

Phiên bản hiện tại:

- ESP32 firmware: `1.4.0`.
- Python: `3.11+` (đã chạy trên Python 3.13 ARM64 của Raspberry Pi OS).
- ESP32 Arduino core đã kiểm tra: `3.1.3`.
- FastAPI phục vụ HTTP trên cổng `8000`.
- Mosquitto MQTT phục vụ TCP trên cổng `1883`.
- Database chạy mặc định: `controller/data/water_controller.db`.
- Database demo cuối cùng: `controller/data/water_controller.demo.db`.

## 1. Tính năng chính

- Provisioning Wi-Fi bằng AP `WaterSensor-Setup` và captive portal.
- Lưu Wi-Fi, device ID và MQTT topic trong NVS của ESP32.
- Tự đăng ký node theo hardware ID; cùng một board sẽ nhận lại device ID cũ.
- Tìm controller qua `edge-controller.local` trên cùng mạng Wi-Fi LAN.
- Đọc HY-SRF05 mỗi giây, chỉ publish khi thay đổi ít nhất `1 cm`.
- MQTT retained status và Last Will `offline`.
- Dashboard cập nhật realtime bằng WebSocket, polling là cơ chế dự phòng.
- Quản lý nhiều node, đặt tên, chiều cao bể, vị trí và liên kết có hướng.
- Cảnh báo L0/L1/L2 cho ngập từng node, blockage giữa hai node và lượng mưa.
- Open-Meteo cho dự báo thời tiết; OpenStreetMap Overpass cho nút giao.
- Chạy như systemd service và tự khởi động cùng Raspberry Pi.

## 2. Kiến trúc

Raspberry Pi, ESP32 và thiết bị mở dashboard cùng kết nối một router Wi-Fi:

```text
Router Wi-Fi
  ├── Raspberry Pi: FastAPI + MQTT + SQLite
  ├── ESP32 water-001
  ├── ESP32 water-002
  └── Điện thoại/tablet mở dashboard
```

Firmware resolve `edge-controller.local` qua mDNS. DHCP gateway là router,
không phải Pi, nên firmware không dùng gateway làm controller. Không dùng
Wi-Fi Guest hoặc mạng có client isolation.

### Cấu hình demo đã sử dụng

Các giá trị dưới đây được giữ trong tài liệu để project cuối kỳ có thể dựng
lại đúng môi trường thử nghiệm:

| Thành phần | Giá trị |
|---|---|
| Raspberry Pi SSH user | `admin` |
| Raspberry Pi SSH password | `123` |
| Pi LAN IP từng sử dụng | `192.168.1.3` (DHCP, có thể thay đổi) |
| Wi-Fi nhà | `Long706` |
| ESP setup SSID/password | `WaterSensor-Setup` / `12345678` |

Password của Wi-Fi nhà không nằm trong source; nhập trực tiếp bằng captive
portal ESP32 hoặc cấu hình mạng của Raspberry Pi khi dựng lại hệ thống.

## 3. Luồng một node mới

```text
ESP32 chưa có Wi-Fi trong NVS
  → phát WaterSensor-Setup tại 192.168.4.1
  → người dùng nhập SSID/password
  → lưu NVS và reboot
  → kết nối Wi-Fi
  → tìm edge-controller.local
  → POST /api/devices/register
  → Pi trả device_id|mqtt_topic
  → ESP lưu registration và kết nối MQTT
  → đọc HY-SRF05 mỗi giây
  → publish khi khoảng cách thay đổi ≥ 1 cm
  → FastAPI lưu SQLite và broadcast WebSocket
  → dashboard cập nhật realtime
```

Lifecycle trong firmware:

```text
UNPROVISIONED → PROVISIONING_AP → WIFI_CONFIGURED → CONNECT_WIFI
              → DISCOVER_CONTROLLER → REGISTER_CONTROLLER → REGISTERED
              → MQTT_CONNECT → NORMAL_OPERATION
```

## 4. Cấu trúc repository

```text
.
├── README.md
├── esp32/
│   └── water_edge_node/
│       └── water_edge_node.ino       Firmware ESP32
├── controller/
│   ├── main.py                       FastAPI, MQTT bridge, WebSocket
│   ├── device_registry.py            SQLite registry và tính cảnh báo
│   ├── weather_service.py            Open-Meteo và Overpass
│   ├── mdns_service.py               mDNS aliases
│   ├── models.py                     Pydantic request models
│   ├── requirements.txt
│   ├── data/water_controller.db      Database chạy mặc định
│   ├── data/water_controller.demo.db Snapshot demo 2 node/1 link
│   └── static/                        Dashboard HTML/CSS/JavaScript
└── deploy/pi/
    ├── install.sh                    Cài app/service và SSH public key trên Pi
    ├── verify.sh                     Kiểm tra deployment trên Wi-Fi LAN
    ├── ssh/water-controller-deploy.pub
    ├── water-controller.service      systemd unit
    ├── mosquitto-water-controller.conf
    └── README.md                     Hướng dẫn deploy chi tiết
```

Các file `.venv`, log, SQLite WAL/SHM, archive build và SSH **private key** là
artifact cục bộ, không đưa vào repository. Public key dùng để cấu hình
`authorized_keys` được lưu tại `deploy/pi/ssh/`. File SQLite chính được giữ để
khôi phục đầy đủ dữ liệu demo.

## 5. Phần cứng node

```text
ESP32 GPIO33  → HY-SRF05 TRIG
ESP32 GPIO32  ← voltage divider/level shifter ← HY-SRF05 ECHO
ESP32 5V      → HY-SRF05 VCC
ESP32 GND     ↔ HY-SRF05 GND
ESP32 GPIO25  → điện trở 220–330 Ω → LED xanh
ESP32 GPIO26  → điện trở 220–330 Ω → LED vàng
ESP32 GPIO27  → điện trở 220–330 Ω → LED đỏ
ESP32 GPIO14  → ngõ vào module buzzer/transistor điều khiển còi
```

HY-SRF05 có thể đưa `5 V` ra chân ECHO. GPIO ESP32 chỉ chịu `3.3 V`; không
nối ECHO trực tiếp vào GPIO32.

Firmware điều khiển cảnh báo cục bộ theo khoảng cách (khoảng cách nhỏ hơn
nghĩa là nước dâng cao): trên `11.0 cm` là L0 xanh, từ `8.0..11.0 cm` là L1
vàng kèm còi chậm, và từ `8.0 cm` trở xuống là L2 đỏ kèm còi nhanh. Mốc bình
thường đã quan sát là `12.9 cm`; hysteresis `0.5 cm` hạn chế chuyển mức liên tục
khi mặt nước dao động quanh ngưỡng. Các ngưỡng nằm ở đầu file firmware để dễ
hiệu chỉnh sau khi lắp thực tế.

Công thức controller sử dụng sau khi cấu hình chiều cao bể:

```text
water_level_cm = tank_height_cm - distance_cm
fill_percent   = water_level_cm / tank_height_cm × 100
```

Kết quả được giới hạn trong `0..tank_height_cm`.

## 6. Firmware ESP32

Source: [`esp32/water_edge_node/water_edge_node.ino`](esp32/water_edge_node/water_edge_node.ino)

Các module/lớp chính:

- `ConfigManager`: NVS namespace `water-iot`, Wi-Fi và registration.
- `ProvisioningServer`: AP/captive DNS và form cấu hình.
- `WiFiConnectionManager`: kết nối/retry Wi-Fi.
- `ControllerDiscovery`: tìm Pi bằng mDNS `edge-controller.local`.
- `ControllerClient`: đăng ký qua HTTP.
- `MQTTManager`: status, telemetry và command.
- `SensorManager`: đo HY-SRF05.
- `LocalWebServer`: trang trạng thái trực tiếp trên node.

Cấu hình firmware mặc định:

| Giá trị | Mặc định |
|---|---|
| Setup SSID | `WaterSensor-Setup` |
| Setup password | `12345678` |
| Captive portal | `http://192.168.4.1/` hoặc `http://water.local/` |
| Controller mDNS | `edge-controller.local` |
| HTTP | `8000` |
| MQTT | `1883` |
| Chu kỳ đo | `1 giây` |
| Ngưỡng publish | `1.0 cm` |
| Wi-Fi timeout lúc boot | `20 giây` |

Firmware `1.2.1` có Wi-Fi diagnostics. Serial sẽ cho biết ESP có nhìn thấy
SSID hay không, RSSI, kênh, auth mode và disconnect reason như
`NO_AP_FOUND`, `AUTH_FAILED` hoặc `NO_AP_WITH_COMPATIBLE_SECURITY`.

### Build và upload bằng Arduino IDE

1. Cài ESP32 Arduino core trong Boards Manager.
2. Cài thư viện `PubSubClient` trong Library Manager.
3. Mở `esp32/water_edge_node/water_edge_node.ino`.
4. Chọn board, ví dụ `ESP32 Dev Module`.
5. Chọn đúng serial port và Upload.
6. Mở Serial Monitor ở `115200 baud`.

Không bật **Erase All Flash Before Sketch Upload** khi chỉ nâng firmware và
muốn giữ NVS. Bật erase chỉ khi cần node hoàn toàn fresh.

### Build bằng Arduino CLI

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install PubSubClient
arduino-cli compile --fqbn esp32:esp32:esp32 ./esp32/water_edge_node
arduino-cli compile --upload --port /dev/ttyUSB0 \
  --fqbn esp32:esp32:esp32 ./esp32/water_edge_node
```

Đóng Serial Monitor trước khi upload để tránh lỗi serial port đang bận.

### Provision node

1. Kết nối `WaterSensor-Setup`, password `12345678`.
2. Mở `http://192.168.4.1/` nếu captive portal không tự bật.
3. Nhập SSID/password mạng mà Pi đang sử dụng hoặc phát.
4. Nhấn **Save & Connect**.
5. ESP lưu NVS, reboot, đăng ký và xuất hiện trên dashboard.

Nếu cùng board đã từng đăng ký, hardware ID không đổi nên Pi trả lại device ID
cũ và giữ label, vị trí, chiều cao bể cùng các link trong database.

### Local web của node

Sau khi đăng ký:

```text
http://water-001.local/
http://water-001.local/status
```

Nếu mDNS không hoạt động, dùng IP hiển thị trên Serial Monitor hoặc trong
`GET /api/devices`.

## 7. MQTT contract

Với node `water-001`:

| Hướng | Topic | Payload |
|---|---|---|
| ESP → Pi | `devices/water-001/status` | `online` / retained `offline` |
| ESP → Pi | `devices/water-001/distance_cm` | Số cm, ví dụ `12.7` |
| Pi → ESP | `devices/water-001/command` | Command dạng text |

Command được firmware hỗ trợ:

| Command | Tác dụng |
|---|---|
| `measure_now` | Đo và publish ngay, bỏ qua threshold |
| `restart` | Reboot node |
| `wifi_reset` | Xóa riêng Wi-Fi, giữ device ID/topic |
| `factory_reset` | Xóa Wi-Fi và registration |

Ví dụ:

```bash
mosquitto_sub -h edge-controller.local -t 'devices/#' -v
mosquitto_pub -h edge-controller.local -t 'devices/water-001/command' -m measure_now
mosquitto_pub -h edge-controller.local -t 'devices/water-001/command' -m wifi_reset
```

## 8. Controller API

Các endpoint chính:

| Method | Path | Mục đích |
|---|---|---|
| `POST` | `/api/devices/register` | Đăng ký idempotent theo hardware ID |
| `GET` | `/api/devices` | Danh sách node và telemetry |
| `PATCH` | `/api/devices/{device_id}` | Label, chiều cao, map, vị trí |
| `POST` | `/api/devices/{device_id}/wifi-reset` | Đổi Wi-Fi, giữ registration |
| `DELETE` | `/api/devices/{device_id}` | Factory reset và xóa node/link |
| `GET` | `/api/dashboard` | Snapshot dashboard |
| `GET` | `/api/ml` | Trạng thái model và dự đoán theo link |
| `GET/PUT` | `/api/settings` | Ngưỡng cảnh báo |
| `GET/POST` | `/api/links` | Đọc/tạo link |
| `DELETE` | `/api/links/{link_id}` | Xóa link |
| `GET` | `/api/weather` | Dự báo đã cache |
| `POST` | `/api/weather/refresh` | Cập nhật thời tiết ngay |
| `GET` | `/api/intersections/discover` | Tải giao lộ theo quận |
| `GET` | `/api/intersections/search` | Tìm giao lộ theo tuyến phố |
| `WS` | `/ws` | Dashboard realtime |
| `GET` | `/health` | Health MQTT/mDNS/weather/database |

Swagger cung cấp request schema đầy đủ tại `/docs`.

## 9. Weather, vị trí và cảnh báo

Controller tự bootstrap 12 quận nội thành Hà Nội. Open-Meteo được cập nhật
mặc định mỗi `900 giây`. Các giao lộ có tên được lấy từ OpenStreetMap
Overpass và cache để giảm số request.

Ngưỡng trong `/settings`:

- Flood L1/L2: phần trăm đầy của từng node.
- Blockage L1/L2: trị tuyệt đối chênh lệch mực nước giữa hai node đã link.
- Rain L1/L2: tổng lượng mưa dự báo trong 6 giờ tới.
- L0 được áp dụng khi giá trị thấp hơn L1.

Không có Internet thì MQTT, SQLite, dashboard và cảnh báo sensor vẫn chạy;
weather/Overpass chỉ giữ dữ liệu cache cũ.

### Logistic Regression chỉ kích hoạt khi có mưa

Controller kèm một artifact Logistic Regression được huấn luyện từ `480` mẫu
synthetic theo 24 event. Model dùng hai node của một link có hướng, tốc độ nước
dâng trong cửa sổ 5 phút, chênh lệch khoảng cách và dữ liệu mưa Open-Meteo.

- Không mưa: model ở trạng thái `dry_weather`; hệ thống dùng cảnh báo L0/L1/L2
  và dự báo thời tiết thông thường.
- Có mưa hoặc dự báo mưa đủ ngưỡng: model tính xác suất ngập trong 5 phút.
- Model synthetic chỉ được phép bổ sung cảnh báo sớm L1; không tạo hoặc xóa L2.
- Weather stale, node offline, thiếu link/vị trí hoặc thiếu model: inference bị
  vô hiệu hóa và cảnh báo deterministic vẫn hoạt động.

Dataset, script và artifact nằm trong `controller/ml/`. Tái tạo và train:

```bash
cd controller/ml
python generate_synthetic_data.py
python train_model.py
```

Kết quả synthetic chỉ xác minh pipeline phần mềm, không phải bằng chứng độ
chính xác dự báo ngập ngoài thực địa. Muốn kích hoạt dự đoán trên dashboard,
cần hai node online, tạo link nguồn → đích và gán node vào khu vực thời tiết.

## 10. Biến môi trường controller

| Biến | Mặc định | Ý nghĩa |
|---|---|---|
| `WATER_DB_PATH` | `controller/data/water_controller.db` | SQLite path |
| `WATER_MQTT_HOST` | `127.0.0.1` | Broker host |
| `WATER_MQTT_PORT` | `1883` | Broker port |
| `WATER_MQTT_CLIENT_ID` | `water-controller-node` | MQTT client ID |
| `WATER_MDNS_ENABLED` | `true` | Bật quảng bá mDNS |
| `WATER_MDNS_HOSTNAMES` | `edge-controller.local` | Hostname khi chạy Zeroconf thủ công |
| `WATER_MDNS_ADDRESS` | tự phát hiện | IP quảng bá |
| `WATER_HTTP_PORT` | `8000` | Port quảng bá mDNS |
| `WATER_AUTO_LOAD_HANOI_DISTRICTS` | `true` | Bootstrap 12 quận |
| `WATER_WEATHER_REFRESH_SECONDS` | `900` | Chu kỳ weather |
| `WATER_WEATHER_HTTP_TIMEOUT` | `10` | HTTP timeout |
| `WATER_ML_ENABLED` | `true` | Bật inference Logistic Regression |
| `WATER_ML_MODEL_PATH` | `controller/ml/model.json` | Model artifact JSON |
| `WATER_OVERPASS_URL` | Overpass public endpoint | API chính |
| `WATER_OVERPASS_FALLBACK_URL` | private.coffee | API dự phòng |

Các giá trị mDNS trong bảng áp dụng khi chạy controller Python thủ công. Bản
cài đặt Raspberry Pi đặt `WATER_MDNS_ENABLED=false` và dùng `avahi-daemon`
làm nguồn duy nhất cho `edge-controller.local`, `_http._tcp:8000` và
`_mqtt._tcp:1883`.

## 11. Đưa project lên Git

Repository giữ source, tài liệu, deploy scripts và hai SQLite snapshot. File
`water_controller.db` hiện là trạng thái local sạch; file
`water_controller.demo.db` là snapshot deployment cuối với 2 node và 1 link.
Trước khi commit database đang chạy trên Pi, dừng service để SQLite checkpoint
WAL nhất quán.

Khởi tạo repository:

```bash
cd ~/water-controller-project
git init
git add .
git status
git commit -m "Final Water Controller IoT project"
git branch -M main
git remote add origin https://github.com/USER/REPOSITORY.git
git push -u origin main
```

Nếu remote dùng SSH:

```bash
git remote add origin git@github.com:USER/REPOSITORY.git
git push -u origin main
```

Không commit file `water_controller.db-wal` hoặc `water_controller.db-shm`;
đây là journal tạm. Hai file `.db` chính và `.demo.db` đều được Git lưu.

## 12. Deploy lên Pi từ Git

### Chuẩn bị Pi

Trong Raspberry Pi Imager:

- chọn Raspberry Pi OS Lite 64-bit;
- tạo username/password;
- bật SSH;
- đặt WLAN country `VN`;
- cho Pi vào Wi-Fi nhà nếu triển khai chế độ LAN.

SSH vào Pi:

```bash
ssh admin@PI_IP
```

Clone project:

```bash
sudo apt update
sudo apt install -y git
git clone https://github.com/USER/REPOSITORY.git ~/water-controller-project
cd ~/water-controller-project
```

### Cài controller trên Wi-Fi LAN

Trước khi chạy installer, cấu hình Raspberry Pi và các ESP32 kết nối cùng một
Wi-Fi router 2,4 GHz. Installer không tạo hoặc thay đổi access point:

```bash
sudo bash deploy/pi/install.sh
```

Installer áp dụng hostname ngay, khởi động lại Avahi và tự kiểm tra dashboard
cùng MQTT qua `edge-controller.local`; không cần reboot nếu bước kiểm tra kết
thúc thành công.

Mặc định installer thêm public key
`deploy/pi/ssh/water-controller-deploy.pub` vào
`/home/admin/.ssh/authorized_keys`. Nếu username Pi khác `admin`:

```bash
sudo WATER_SSH_USER='pi-user' bash deploy/pi/install.sh
```

Từ điện thoại/tablet cùng Wi-Fi:

```text
http://edge-controller.local:8000/
```

Kiểm tra service, IP LAN, mDNS, FastAPI và MQTT:

```bash
cd ~/water-controller-project
sudo bash deploy/pi/verify.sh
```

## 13. Chạy và quản trị server trên Pi

App được cài tại `/opt/water-controller` và chạy bằng user hệ thống
`watercontroller`.

```bash
sudo systemctl status water-controller --no-pager
sudo systemctl start water-controller
sudo systemctl stop water-controller
sudo systemctl restart water-controller
sudo systemctl enable water-controller
```

Log:

```bash
sudo journalctl -u water-controller -f
sudo journalctl -u mosquitto -f
sudo journalctl -u NetworkManager -f
```

Health và dữ liệu:

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/devices
mosquitto_pub -h 127.0.0.1 -t water-controller/verify -m ok
```

Không chạy thêm một Uvicorn thủ công trong khi systemd service đang dùng
cổng `8000`.

Khôi phục snapshot demo 2 node/1 link từ repository:

```bash
sudo systemctl stop water-controller
sudo install -o watercontroller -g watercontroller -m 0640 \
  ~/water-controller-project/controller/data/water_controller.demo.db \
  /opt/water-controller/data/water_controller.db
sudo systemctl start water-controller
```

## 14. Cập nhật code trên Pi

Nếu deploy bằng Git:

```bash
cd ~/water-controller-project
git pull --ff-only
sudo bash deploy/pi/install.sh
```

Installer giữ database live trên Pi và không thay đổi cấu hình Wi-Fi.

## 15. Kiểm thử end-to-end

1. `systemctl` xác nhận Mosquitto và Water Controller active.
2. `curl /health` trả `status: ok`, `mqtt_connected: true`.
3. Flash firmware, mở Serial Monitor `115200`.
4. Provision SSID/password.
5. Xác nhận Serial có `[WIFI] Connected`.
6. Xác nhận `edge-controller.local` resolve trên cùng Wi-Fi LAN.
7. Xác nhận registration trả device ID/topic.
8. Xác nhận `[MQTT] Connected`.
9. Mở `/api/devices` và dashboard.
10. Thay đổi khoảng cách hơn `1 cm`, xác nhận dashboard cập nhật realtime.
11. Gửi `measure_now`, `restart` và `wifi_reset`.
12. Tạo link giữa hai node, cấu hình chiều cao và kiểm tra L0/L1/L2.

## 16. Xử lý lỗi thường gặp

### ESP báo `Initial connection timed out`

Đọc các dòng `[WIFI-DIAG]` firmware 1.3.0:

- `Target ... NOT FOUND`: sai SSID, router ngoài vùng phủ hoặc không có 2.4 GHz.
- `AUTH_FAILED`: sai password.
- `NO_AP_WITH_COMPATIBLE_SECURITY`: cấu hình router WPA2/RSN, không WPA3-only.
- RSSI dưới khoảng `-80 dBm`: đưa node gần router hơn.

### ESP kết nối Wi-Fi nhưng không tìm thấy controller

- Kiểm tra `ping edge-controller.local` từ thiết bị cùng mạng có terminal.
- Kiểm tra `avahi-daemon` và hostname Pi.
- Pi và ESP32 phải cùng subnet; không dùng Guest Wi-Fi/client isolation.
- Gateway là router nên mDNS `edge-controller.local` phải hoạt động.
- Kiểm tra TCP `8000`, `1883` và multicast DNS UDP `5353`.

### Dashboard không mở

```bash
sudo systemctl status water-controller --no-pager
sudo journalctl -u water-controller -n 100 --no-pager
curl http://127.0.0.1:8000/health
ss -ltnp | grep ':8000'
```

### MQTT không kết nối

```bash
sudo systemctl status mosquitto --no-pager
sudo journalctl -u mosquitto -n 100 --no-pager
mosquitto_pub -h 127.0.0.1 -t test -m ok
```

### Upload ESP lỗi serial port đang bận

Đóng Arduino Serial Monitor hoặc tiến trình đang giữ `/dev/ttyUSB0`, sau đó
upload lại. Upload firmware bình thường không xóa NVS.

## 17. Dữ liệu và giới hạn prototype

- SQLite giữ node, device ID, vị trí, links, settings, intersections và weather
  cache; hiện mới lưu telemetry gần nhất, chưa có time-series dài hạn.
- Mosquitto đang dùng `allow_anonymous true`; chỉ phù hợp Wi-Fi LAN tin cậy.
- HTTP factory reset/local status chưa có authentication.
- Chưa có TLS MQTT, OTA, sensor filtering nâng cao hoặc deep sleep.
- Weather và Overpass phụ thuộc Internet; sensor core vẫn chạy offline.
- File database trong Git là snapshot binary; tránh nhiều người sửa đồng thời.

Tài liệu chi tiết hơn:

- [`controller/README.md`](controller/README.md)
- [`deploy/pi/README.md`](deploy/pi/README.md)

Weather data by [Open-Meteo](https://open-meteo.com/). Map data ©
[OpenStreetMap contributors](https://www.openstreetmap.org/copyright/).
