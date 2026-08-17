# Deploy Water Controller lên Raspberry Pi

Kiến trúc duy nhất của project là Raspberry Pi, ESP32 và thiết bị mở dashboard
cùng kết nối một Wi-Fi router. Pi không phát access point riêng.

## 1. Topology

```text
Wi-Fi router 2,4 GHz
  ├── Raspberry Pi
  │     ├── edge-controller.local
  │     ├── FastAPI/dashboard :8000
  │     ├── Mosquitto MQTT    :1883
  │     └── SQLite
  ├── ESP32 water-001
  ├── ESP32 water-002
  └── điện thoại/tablet/laptop
```

Không dùng Guest Wi-Fi hoặc router bật client isolation. ESP32 chỉ tìm Pi qua
mDNS `edge-controller.local`; gateway DHCP là router và không được dùng làm
địa chỉ controller.

## 2. Chuẩn bị Raspberry Pi

Dùng Raspberry Pi OS Lite 64-bit. Trong Raspberry Pi Imager:

- tạo username/password, mặc định tài liệu dùng `admin`;
- bật SSH;
- đặt WLAN country `VN`;
- cấu hình Wi-Fi router đang dùng cho ESP32.

Đăng nhập Pi và clone repository:

```bash
ssh admin@PI_IP
sudo apt update
sudo apt install -y git
git clone https://github.com/agu4004/edge_flood_detection.git ~/water-controller-project
cd ~/water-controller-project
```

## 3. Cài controller

```bash
sudo bash deploy/pi/install.sh
sudo reboot
```

Installer thực hiện:

1. cài Python, Mosquitto, Avahi, Git và OpenSSH server;
2. cài app vào `/opt/water-controller`;
3. tạo Python venv và cài dependencies;
4. cấu hình Mosquitto tại cổng `1883`;
5. thêm deployment public key vào `authorized_keys` của user `admin`;
6. đặt hostname `edge-controller`;
7. enable và khởi động các systemd service.

Installer không tạo, xóa hoặc sửa cấu hình Wi-Fi. Nếu username SSH không phải
`admin`, truyền rõ khi cài:

```bash
sudo WATER_SSH_USER='pi-user' bash deploy/pi/install.sh
```

Public key được version-control tại:

```text
deploy/pi/ssh/water-controller-deploy.pub
```

Private key tương ứng không nằm trong Git. Trên máy quản trị đang giữ private
key, có thể đăng nhập bằng:

```bash
ssh -i PATH_TO_PRIVATE_KEY admin@edge-controller.local
```

## 4. Kiểm tra deployment

```bash
cd ~/water-controller-project
sudo bash deploy/pi/verify.sh
```

Script kiểm tra SSH, Mosquitto, Avahi, Water Controller, IP LAN, mDNS,
FastAPI health và MQTT publish. Dashboard truy cập bằng:

```text
http://edge-controller.local:8000/
http://water-monitor.local:8000/
http://PI_LAN_IP:8000/
```

## 5. Firmware ESP32

Firmware production:

```text
esp32/water_edge_node/water_edge_node.ino
```

Firmware 1.3.0 tìm controller bằng `edge-controller.local`. Có thể build và
upload từ Pi khi ESP32 nối USB:

```bash
arduino-cli core install esp32:esp32
arduino-cli lib install PubSubClient
arduino-cli compile --upload --port /dev/ttyUSB0 \
  --fqbn esp32:esp32:esp32 ./esp32/water_edge_node
```

Provision từng ESP32 fresh hoặc đã `wifi_reset`:

1. kết nối `WaterSensor-Setup`, password `12345678`;
2. mở `http://192.168.4.1/`;
3. nhập SSID/password của cùng Wi-Fi router đang kết nối Pi;
4. ESP32 nhận IP DHCP và resolve `edge-controller.local`;
5. ESP32 đăng ký FastAPI, kết nối MQTT và xuất hiện trên dashboard.

## 6. Quản trị server

```bash
sudo systemctl status water-controller --no-pager
sudo systemctl restart water-controller
sudo systemctl status mosquitto --no-pager
sudo journalctl -u water-controller -f
sudo journalctl -u mosquitto -f
```

```bash
curl http://127.0.0.1:8000/health
curl http://127.0.0.1:8000/api/devices
mosquitto_sub -h 127.0.0.1 -t 'devices/#' -v
```

## 7. Cập nhật từ Git

```bash
cd ~/water-controller-project
git pull --ff-only
sudo bash deploy/pi/install.sh
```

Installer giữ database live trong `/opt/water-controller/data/` và không thay
đổi Wi-Fi. Để thay database bằng snapshot repository:

```bash
sudo WATER_REPLACE_DATABASE=1 bash deploy/pi/install.sh
```

## 8. Khôi phục database demo

Repository có `controller/data/water_controller.demo.db` gồm 2 node và 1 link:

```bash
sudo systemctl stop water-controller
sudo install -o watercontroller -g watercontroller -m 0640 \
  ~/water-controller-project/controller/data/water_controller.demo.db \
  /opt/water-controller/data/water_controller.db
sudo systemctl start water-controller
```

## 9. Lưu ý prototype

Mosquitto đang dùng `allow_anonymous true`; chỉ triển khai trong Wi-Fi LAN tin
cậy. Weather và Overpass cần Internet, còn sensor, MQTT, SQLite và dashboard
vẫn hoạt động khi Internet tạm mất miễn là router LAN vẫn hoạt động.
