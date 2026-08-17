#include <WiFi.h>
#include <WebServer.h>
#include <DNSServer.h>
#include <Preferences.h>
#include <HTTPClient.h>
#include <PubSubClient.h>
#include <ESPmDNS.h>
#include <math.h>

// ------------------------------ Hardware ------------------------------
#define TRIG_PIN 33
#define ECHO_PIN 32
#define GREEN_LED_PIN 25
#define YELLOW_LED_PIN 26
#define RED_LED_PIN 27
#define BUZZER_PIN 14

// HY-SRF05 ECHO may be 5 V. Use a suitable voltage divider before GPIO32.
// Alarm outputs assume active-HIGH LEDs (common cathode/module) and an active
// buzzer module. Drive a bare or high-current buzzer through an NPN transistor.

// --------------------------- Prototype config -------------------------
static const char *FIRMWARE_VERSION = "1.4.0";
static const char *NVS_NAMESPACE = "water-iot";
static const char *SETUP_SSID = "WaterSensor-Setup";
static const char *SETUP_PASSWORD = "12345678";
static const char *CONTROLLER_MDNS_HOST = "edge-controller";

// The Raspberry Pi and ESP32 share the same Wi-Fi LAN. Resolve the controller
// hostname through mDNS so DHCP may change the Pi address without reflashing.

static const uint16_t CONTROLLER_HTTP_PORT = 8000;
static const uint16_t MQTT_PORT = 1883;
static const unsigned long WIFI_CONNECT_TIMEOUT_MS = 20000;
static const unsigned long WIFI_RETRY_INTERVAL_MS = 15000;
static const unsigned long CONTROLLER_DISCOVERY_RETRY_INTERVAL_MS = 10000;
static const uint32_t CONTROLLER_DISCOVERY_TIMEOUT_MS = 2000;
static const unsigned long REGISTRATION_RETRY_INTERVAL_MS = 15000;
static const unsigned long MQTT_RETRY_INTERVAL_MS = 5000;
static const unsigned long MEASURE_INTERVAL_MS = 1000;
static const float CHANGE_THRESHOLD_CM = 1.0f;

// The sensor points down toward the water, so a smaller distance means a
// higher water level. 12.9 cm is the observed normal distance for this setup.
// Keep these constants easy to calibrate after testing the real installation.
static const float NORMAL_REFERENCE_DISTANCE_CM = 12.9f;
static const float YELLOW_ALERT_DISTANCE_CM = 11.0f;
static const float RED_ALERT_DISTANCE_CM = 8.0f;
static const float ALERT_HYSTERESIS_CM = 0.5f;
static const unsigned long YELLOW_BUZZER_PERIOD_MS = 1500;
static const unsigned long YELLOW_BUZZER_ON_MS = 200;
static const unsigned long RED_BUZZER_PERIOD_MS = 500;
static const unsigned long RED_BUZZER_ON_MS = 300;

static const IPAddress SETUP_IP(192, 168, 4, 1);

volatile uint8_t lastWiFiDisconnectReason = 0;

const char *wifiStatusName(wl_status_t status) {
  switch (status) {
    case WL_IDLE_STATUS: return "IDLE";
    case WL_NO_SSID_AVAIL: return "NO_SSID_AVAILABLE";
    case WL_SCAN_COMPLETED: return "SCAN_COMPLETED";
    case WL_CONNECTED: return "CONNECTED";
    case WL_CONNECT_FAILED: return "CONNECT_FAILED";
    case WL_CONNECTION_LOST: return "CONNECTION_LOST";
    case WL_DISCONNECTED: return "DISCONNECTED";
    case WL_STOPPED: return "STOPPED";
    case WL_NO_SHIELD: return "NO_SHIELD";
    default: return "UNKNOWN";
  }
}

const char *wifiAuthModeName(wifi_auth_mode_t mode) {
  switch (mode) {
    case WIFI_AUTH_OPEN: return "OPEN";
    case WIFI_AUTH_WEP: return "WEP";
    case WIFI_AUTH_WPA_PSK: return "WPA_PSK";
    case WIFI_AUTH_WPA2_PSK: return "WPA2_PSK";
    case WIFI_AUTH_WPA_WPA2_PSK: return "WPA_WPA2_PSK";
    case WIFI_AUTH_WPA2_ENTERPRISE: return "WPA2_ENTERPRISE";
    case WIFI_AUTH_WPA3_PSK: return "WPA3_PSK";
    case WIFI_AUTH_WPA2_WPA3_PSK: return "WPA2_WPA3_PSK";
    default: return "UNKNOWN";
  }
}

const char *wifiDisconnectReasonName(uint8_t reason) {
  switch (reason) {
    case WIFI_REASON_AUTH_EXPIRE: return "AUTH_EXPIRED";
    case WIFI_REASON_4WAY_HANDSHAKE_TIMEOUT: return "4WAY_HANDSHAKE_TIMEOUT";
    case WIFI_REASON_GROUP_KEY_UPDATE_TIMEOUT: return "GROUP_KEY_UPDATE_TIMEOUT";
    case WIFI_REASON_ASSOC_NOT_AUTHED: return "ASSOCIATED_NOT_AUTHENTICATED";
    case WIFI_REASON_BEACON_TIMEOUT: return "BEACON_TIMEOUT";
    case WIFI_REASON_NO_AP_FOUND: return "NO_AP_FOUND";
    case WIFI_REASON_AUTH_FAIL: return "AUTH_FAILED";
    case WIFI_REASON_ASSOC_FAIL: return "ASSOCIATION_FAILED";
    case WIFI_REASON_HANDSHAKE_TIMEOUT: return "HANDSHAKE_TIMEOUT";
    case WIFI_REASON_CONNECTION_FAIL: return "CONNECTION_FAILED";
    case WIFI_REASON_SA_QUERY_TIMEOUT: return "SA_QUERY_TIMEOUT";
    case WIFI_REASON_NO_AP_FOUND_W_COMPATIBLE_SECURITY:
      return "NO_AP_WITH_COMPATIBLE_SECURITY";
    case WIFI_REASON_NO_AP_FOUND_IN_AUTHMODE_THRESHOLD:
      return "NO_AP_IN_AUTHMODE_THRESHOLD";
    case WIFI_REASON_NO_AP_FOUND_IN_RSSI_THRESHOLD:
      return "NO_AP_IN_RSSI_THRESHOLD";
    default: return "OTHER";
  }
}

void onWiFiStationDisconnected(WiFiEvent_t event, WiFiEventInfo_t info) {
  (void)event;
  const uint8_t reason = info.wifi_sta_disconnected.reason;
  lastWiFiDisconnectReason = reason;
  Serial.printf("[WIFI-DIAG] Station disconnected: reason=%u (%s)\n",
                reason, wifiDisconnectReasonName(reason));
}

bool printTargetNetworkDiagnostics(const String &targetSsid) {
  Serial.printf("[WIFI-DIAG] Scanning for SSID '%s'...\n", targetSsid.c_str());
  const int16_t networkCount = WiFi.scanNetworks(false, true);
  if (networkCount == WIFI_SCAN_FAILED) {
    Serial.println("[WIFI-DIAG] Scan failed");
    WiFi.scanDelete();
    return false;
  }
  if (networkCount < 0) {
    Serial.printf("[WIFI-DIAG] Scan unavailable: result=%d\n", networkCount);
    WiFi.scanDelete();
    return false;
  }

  Serial.printf("[WIFI-DIAG] Scan complete: %d network(s) visible\n", networkCount);
  bool targetFound = false;
  for (int16_t index = 0; index < networkCount; ++index) {
    if (WiFi.SSID(index) != targetSsid) continue;
    targetFound = true;
    const wifi_auth_mode_t authMode = WiFi.encryptionType(index);
    Serial.printf(
      "[WIFI-DIAG] Target found: BSSID=%s, RSSI=%d dBm, channel=%d, auth=%s (%d)\n",
      WiFi.BSSIDstr(index).c_str(), WiFi.RSSI(index), WiFi.channel(index),
      wifiAuthModeName(authMode), static_cast<int>(authMode));
  }
  if (!targetFound) {
    Serial.printf("[WIFI-DIAG] Target '%s' NOT FOUND; check SSID and 2.4 GHz availability\n",
                  targetSsid.c_str());
  }
  WiFi.scanDelete();
  return targetFound;
}

static const char LIVE_STATUS_PAGE[] PROGMEM = R"html(
<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Water Sensor</title><style>
body{font-family:Arial,sans-serif;max-width:650px;margin:35px auto;padding:18px}
main{border:1px solid #ddd;padding:22px;border-radius:10px}
dt{font-weight:bold}dd{margin:0 0 9px}.on{color:#087f23;font-weight:bold}
.off{color:#b3261e;font-weight:bold}.live{color:#555;font-size:.9rem}
</style></head><body><main><h1>ESP32 Water Sensor</h1><dl>
<dt>Hardware ID</dt><dd id="h">Loading...</dd>
<dt>Device ID</dt><dd id="d">Loading...</dd>
<dt>Hostname</dt><dd id="n">Loading...</dd>
<dt>IP</dt><dd id="i">Loading...</dd>
<dt>Wi-Fi RSSI</dt><dd id="r">Loading...</dd>
<dt>MQTT</dt><dd id="m">Loading...</dd>
<dt>Last distance</dt><dd id="x" aria-live="polite">Loading...</dd>
<dt>Local alarm</dt><dd id="a" aria-live="polite">Loading...</dd>
</dl><p id="u" class="live">Connecting...</p>
<form method="post" action="/factory-reset"
onsubmit="return confirm('Erase configuration and restart?')">
<button type="submit">Factory reset</button></form></main><script>
const e=id=>document.getElementById(id);
async function refresh(){try{
const q=await fetch('/status',{cache:'no-store'});if(!q.ok)throw Error(q.status);
const s=await q.json();e('h').textContent=s.hardware_id;e('d').textContent=s.device_id||'Unregistered';
e('n').textContent=s.hostname;e('i').textContent=s.ip;e('r').textContent=s.wifi_rssi+' dBm';
e('m').textContent=s.mqtt?'Connected':'Disconnected';e('m').className=s.mqtt?'on':'off';
e('x').textContent=s.distance_cm===null?'N/A':s.distance_cm.toFixed(1)+' cm';
e('a').textContent=s.alert_level;
e('u').textContent='Live - updated '+new Date().toLocaleTimeString();
}catch(error){e('u').textContent='Connection lost - retrying...';}}
refresh();setInterval(refresh,1000);
</script></body></html>
)html";
static const size_t LIVE_STATUS_PAGE_LENGTH = sizeof(LIVE_STATUS_PAGE) - 1;
static const size_t WEB_RESPONSE_CHUNK_SIZE = 512;

enum class NodeState {
  UNPROVISIONED,
  PROVISIONING_AP,
  WIFI_CONFIGURED,
  CONNECT_WIFI,
  DISCOVER_CONTROLLER,
  REGISTER_CONTROLLER,
  REGISTERED,
  MQTT_CONNECT,
  NORMAL_OPERATION
};

// Explicit declarations keep Arduino's .ino prototype generator from placing
// these functions before the NodeState definition.
const char *stateName(NodeState state);
void setNodeState(NodeState next);

const char *stateName(NodeState state) {
  switch (state) {
    case NodeState::UNPROVISIONED: return "UNPROVISIONED";
    case NodeState::PROVISIONING_AP: return "PROVISIONING_AP";
    case NodeState::WIFI_CONFIGURED: return "WIFI_CONFIGURED";
    case NodeState::CONNECT_WIFI: return "CONNECT_WIFI";
    case NodeState::DISCOVER_CONTROLLER: return "DISCOVER_CONTROLLER";
    case NodeState::REGISTER_CONTROLLER: return "REGISTER_CONTROLLER";
    case NodeState::REGISTERED: return "REGISTERED";
    case NodeState::MQTT_CONNECT: return "MQTT_CONNECT";
    case NodeState::NORMAL_OPERATION: return "NORMAL_OPERATION";
  }
  return "UNKNOWN";
}

NodeState nodeState = NodeState::UNPROVISIONED;

void setNodeState(NodeState next) {
  if (nodeState != next) {
    nodeState = next;
    Serial.printf("[STATE] %s\n", stateName(nodeState));
  }
}

String hardwareIdFromEfuse() {
  const uint64_t mac = ESP.getEfuseMac();
  char value[13];
  snprintf(value, sizeof(value), "%04X%08X",
           static_cast<uint16_t>(mac >> 32), static_cast<uint32_t>(mac));
  return String(value);
}

String escapeJson(const String &input) {
  String output;
  output.reserve(input.length() + 8);
  for (size_t i = 0; i < input.length(); ++i) {
    const char c = input.charAt(i);
    if (c == '\\' || c == '"') output += '\\';
    if (c == '\n') {
      output += "\\n";
    } else if (c == '\r') {
      output += "\\r";
    } else {
      output += c;
    }
  }
  return output;
}

class ConfigManager {
 public:
  String hardwareId;
  String ssid;
  String password;
  String deviceId;
  String mqttBaseTopic;

  void begin() {
    hardwareId = hardwareIdFromEfuse();
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, true)) {
      Serial.println("[CONFIG] Cannot open NVS for reading");
      return;
    }
    ssid = prefs.getString("ssid", "");
    password = prefs.getString("password", "");
    deviceId = prefs.getString("deviceId", "");
    mqttBaseTopic = prefs.getString("mqttTopic", "");
    prefs.end();

    Serial.printf("[BOOT] Hardware ID: %s\n", hardwareId.c_str());
    Serial.printf("[CONFIG] Wi-Fi configured: %s\n", ssid.isEmpty() ? "no" : "yes");
    Serial.printf("[CONFIG] Device ID: %s\n", deviceId.isEmpty() ? "(unregistered)" : deviceId.c_str());
  }

  bool hasWiFi() const { return !ssid.isEmpty(); }
  bool isRegistered() const { return !deviceId.isEmpty() && !mqttBaseTopic.isEmpty(); }

  String hostname() const {
    if (!deviceId.isEmpty()) return deviceId;
    String suffix = hardwareId.substring(hardwareId.length() - 4);
    suffix.toLowerCase();
    return "water-" + suffix;
  }

  bool saveWiFi(const String &newSsid, const String &newPassword) {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false)) return false;
    const bool ok = prefs.putString("ssid", newSsid) > 0 &&
                    prefs.putString("password", newPassword) == newPassword.length();
    prefs.end();
    if (ok) {
      ssid = newSsid;
      password = newPassword;
    }
    return ok;
  }

  bool saveRegistration(const String &newDeviceId, const String &newTopic) {
    Preferences prefs;
    if (!prefs.begin(NVS_NAMESPACE, false)) return false;
    const bool ok = prefs.putString("deviceId", newDeviceId) == newDeviceId.length() &&
                    prefs.putString("mqttTopic", newTopic) == newTopic.length();
    prefs.end();
    if (ok) {
      deviceId = newDeviceId;
      mqttBaseTopic = newTopic;
    }
    return ok;
  }

  bool factoryReset() {
    Preferences prefs;
    bool configCleared = false;
    if (prefs.begin(NVS_NAMESPACE, false)) {
      configCleared = prefs.clear();
      prefs.end();
    }
    const bool wifiCleared = WiFi.disconnect(true, true);
    Serial.printf("[CONFIG] Factory reset: app NVS=%s, WiFi credentials=%s\n",
                  configCleared ? "cleared" : "failed",
                  wifiCleared ? "cleared" : "not stored/already disconnected");
    return configCleared;
  }

  bool resetWiFi() {
    Preferences prefs;
    bool credentialsCleared = false;
    if (prefs.begin(NVS_NAMESPACE, false)) {
      const bool ssidRemoved = !prefs.isKey("ssid") || prefs.remove("ssid");
      const bool passwordRemoved = !prefs.isKey("password") || prefs.remove("password");
      credentialsCleared = ssidRemoved && passwordRemoved;
      prefs.end();
    }
    WiFi.disconnect(true, true);
    ssid = "";
    password = "";
    Serial.printf("[CONFIG] Wi-Fi reset: %s; registration preserved as %s\n",
                  credentialsCleared ? "cleared" : "failed",
                  deviceId.isEmpty() ? "unregistered" : deviceId.c_str());
    return credentialsCleared;
  }
};

ConfigManager config;

class SensorManager {
 public:
  void begin() {
    pinMode(TRIG_PIN, OUTPUT);
    pinMode(ECHO_PIN, INPUT);
    digitalWrite(TRIG_PIN, LOW);
  }

  float measure() {
    digitalWrite(TRIG_PIN, LOW);
    delayMicroseconds(2);
    digitalWrite(TRIG_PIN, HIGH);
    delayMicroseconds(10);
    digitalWrite(TRIG_PIN, LOW);

    const unsigned long duration = pulseIn(ECHO_PIN, HIGH, 30000UL);
    if (duration == 0) return -1.0f;
    return duration * 0.0343f / 2.0f;
  }
};

SensorManager sensor;

enum class LocalAlertLevel {
  UNKNOWN,
  L0_GREEN,
  L1_YELLOW,
  L2_RED
};

class LocalAlarmManager {
 private:
  LocalAlertLevel level = LocalAlertLevel::UNKNOWN;

  void setLedOutputs(bool green, bool yellow, bool red) {
    digitalWrite(GREEN_LED_PIN, green ? HIGH : LOW);
    digitalWrite(YELLOW_LED_PIN, yellow ? HIGH : LOW);
    digitalWrite(RED_LED_PIN, red ? HIGH : LOW);
  }

 public:
  void begin() {
    pinMode(GREEN_LED_PIN, OUTPUT);
    pinMode(YELLOW_LED_PIN, OUTPUT);
    pinMode(RED_LED_PIN, OUTPUT);
    pinMode(BUZZER_PIN, OUTPUT);
    setLedOutputs(false, false, false);
    digitalWrite(BUZZER_PIN, LOW);
    Serial.printf(
      "[ALARM] Ready: normal reference %.1f cm, yellow <= %.1f cm, red <= %.1f cm\n",
      NORMAL_REFERENCE_DISTANCE_CM, YELLOW_ALERT_DISTANCE_CM,
      RED_ALERT_DISTANCE_CM);
  }

  const char *label() const {
    switch (level) {
      case LocalAlertLevel::L0_GREEN: return "L0_GREEN";
      case LocalAlertLevel::L1_YELLOW: return "L1_YELLOW";
      case LocalAlertLevel::L2_RED: return "L2_RED";
      case LocalAlertLevel::UNKNOWN: return "UNKNOWN";
    }
    return "UNKNOWN";
  }

  void updateDistance(float distanceCm) {
    LocalAlertLevel next = level;
    switch (level) {
      case LocalAlertLevel::UNKNOWN:
      case LocalAlertLevel::L0_GREEN:
        if (distanceCm <= RED_ALERT_DISTANCE_CM) {
          next = LocalAlertLevel::L2_RED;
        } else if (distanceCm <= YELLOW_ALERT_DISTANCE_CM) {
          next = LocalAlertLevel::L1_YELLOW;
        } else {
          next = LocalAlertLevel::L0_GREEN;
        }
        break;
      case LocalAlertLevel::L1_YELLOW:
        if (distanceCm <= RED_ALERT_DISTANCE_CM) {
          next = LocalAlertLevel::L2_RED;
        } else if (distanceCm >= YELLOW_ALERT_DISTANCE_CM + ALERT_HYSTERESIS_CM) {
          next = LocalAlertLevel::L0_GREEN;
        }
        break;
      case LocalAlertLevel::L2_RED:
        if (distanceCm >= RED_ALERT_DISTANCE_CM + ALERT_HYSTERESIS_CM) {
          next = distanceCm >= YELLOW_ALERT_DISTANCE_CM + ALERT_HYSTERESIS_CM
                   ? LocalAlertLevel::L0_GREEN
                   : LocalAlertLevel::L1_YELLOW;
        }
        break;
    }

    if (next != level) {
      level = next;
      Serial.printf("[ALARM] %s at %.1f cm\n", label(), distanceCm);
    }
  }

  void loop() {
    const unsigned long now = millis();
    switch (level) {
      case LocalAlertLevel::L0_GREEN:
        setLedOutputs(true, false, false);
        digitalWrite(BUZZER_PIN, LOW);
        break;
      case LocalAlertLevel::L1_YELLOW:
        setLedOutputs(false, true, false);
        digitalWrite(
          BUZZER_PIN,
          now % YELLOW_BUZZER_PERIOD_MS < YELLOW_BUZZER_ON_MS ? HIGH : LOW);
        break;
      case LocalAlertLevel::L2_RED:
        setLedOutputs(false, false, true);
        digitalWrite(
          BUZZER_PIN,
          now % RED_BUZZER_PERIOD_MS < RED_BUZZER_ON_MS ? HIGH : LOW);
        break;
      case LocalAlertLevel::UNKNOWN:
        setLedOutputs(false, false, false);
        digitalWrite(BUZZER_PIN, LOW);
        break;
    }
  }
};

LocalAlarmManager localAlarm;
WebServer webServer(80);
DNSServer dnsServer;

class ProvisioningServer {
 public:
  bool active = false;

  String page() {
    String html = F(
      "<!doctype html><html><head><meta charset='utf-8'>"
      "<meta name='viewport' content='width=device-width,initial-scale=1'>"
      "<title>Water Sensor Setup</title><style>"
      "body{font-family:Arial,sans-serif;background:#eef3f7;margin:0;padding:24px}"
      ".card{max-width:440px;margin:auto;background:#fff;padding:24px;border-radius:12px;box-shadow:0 4px 18px #0002}"
      "label{display:block;margin-top:14px}input{box-sizing:border-box;width:100%;padding:11px;margin-top:5px}"
      "button{width:100%;padding:12px;margin-top:20px;background:#087ea4;color:#fff;border:0;border-radius:6px}"
      ".info{background:#eef3f7;padding:12px;border-radius:6px}</style></head><body><main class='card'>"
      "<h1>Water Sensor Setup</h1><div class='info'>Hardware ID: <strong>");
    html += config.hardwareId;
    html += F("</strong><br>Setup address: <strong>water.local</strong></div>"
              "<form method='post' action='/save'><label>WiFi SSID</label>"
              "<input name='ssid' maxlength='32' required autocomplete='off'>"
              "<label>WiFi Password</label><input name='password' type='password' maxlength='64'>"
              "<button type='submit'>Save &amp; Connect</button></form></main></body></html>");
    return html;
  }

  void redirectToPortal() {
    webServer.sendHeader("Cache-Control", "no-store");
    webServer.sendHeader("Location", "http://water.local/", true);
    webServer.send(302, "text/plain", "");
  }

  void begin() {
    active = true;
    setNodeState(NodeState::PROVISIONING_AP);
    WiFi.disconnect(true);
    delay(100);
    WiFi.mode(WIFI_AP);
    WiFi.softAPConfig(SETUP_IP, SETUP_IP, IPAddress(255, 255, 255, 0));
    if (!WiFi.softAP(SETUP_SSID, SETUP_PASSWORD)) {
      Serial.println("[PROVISION] Failed to start SoftAP; restarting");
      delay(1000);
      ESP.restart();
    }
    dnsServer.start(53, "*", SETUP_IP);

    webServer.on("/", HTTP_GET, [this]() { webServer.send(200, "text/html; charset=utf-8", page()); });
    webServer.on("/save", HTTP_POST, []() {
      if (!webServer.hasArg("ssid")) {
        webServer.send(400, "text/plain", "Missing SSID");
        return;
      }
      String ssid = webServer.arg("ssid");
      const String password = webServer.arg("password");
      ssid.trim();
      if (ssid.isEmpty() || ssid.length() > 32 || password.length() > 64 ||
          (!password.isEmpty() && password.length() < 8)) {
        webServer.send(400, "text/plain", "SSID or password is invalid");
        return;
      }
      if (!config.saveWiFi(ssid, password)) {
        webServer.send(500, "text/plain", "Cannot save configuration");
        return;
      }
      Serial.printf("[PROVISION] Saved SSID '%s'; password not logged\n", ssid.c_str());
      webServer.send(200, "text/html; charset=utf-8",
                     "<h2>Configuration saved</h2><p>ESP32 is restarting. Reconnect to your normal Wi-Fi.</p>");
      delay(800);
      ESP.restart();
    });

    const char *captivePaths[] = {
      "/generate_204", "/gen_204", "/hotspot-detect.html", "/connecttest.txt", "/ncsi.txt"
    };
    for (const char *path : captivePaths) {
      webServer.on(path, HTTP_GET, [this]() { redirectToPortal(); });
    }
    webServer.onNotFound([this]() { redirectToPortal(); });
    webServer.begin();
    Serial.printf("[PROVISION] AP '%s' at http://water.local (%s)\n",
                  SETUP_SSID, WiFi.softAPIP().toString().c_str());
  }

  void loop() {
    dnsServer.processNextRequest();
    webServer.handleClient();
  }
};

ProvisioningServer provisioning;

class WiFiConnectionManager {
 public:
  unsigned long lastAttemptAt = 0;

  bool connectAtBoot() {
    setNodeState(NodeState::CONNECT_WIFI);
    WiFi.mode(WIFI_STA);
    delay(100);
    WiFi.setHostname(config.hostname().c_str());
    lastWiFiDisconnectReason = 0;
    printTargetNetworkDiagnostics(config.ssid);
    Serial.printf("[WIFI-DIAG] Stored credentials: SSID length=%u, password length=%u (password hidden)\n",
                  static_cast<unsigned>(config.ssid.length()),
                  static_cast<unsigned>(config.password.length()));
    WiFi.begin(config.ssid.c_str(), config.password.c_str());
    const unsigned long startedAt = millis();
    Serial.printf("[WIFI] Connecting to '%s'\n", config.ssid.c_str());
    while (WiFi.status() != WL_CONNECTED && millis() - startedAt < WIFI_CONNECT_TIMEOUT_MS) {
      delay(100);
    }
    if (WiFi.status() != WL_CONNECTED) {
      const wl_status_t finalStatus = WiFi.status();
      Serial.printf(
        "[WIFI-DIAG] Connection timeout: status=%s (%d), last reason=%u (%s)\n",
        wifiStatusName(finalStatus), static_cast<int>(finalStatus),
        lastWiFiDisconnectReason, wifiDisconnectReasonName(lastWiFiDisconnectReason));
      Serial.println("[WIFI] Initial connection timed out; falling back to provisioning");
      return false;
    }
    Serial.printf("[WIFI] Connected: %s, RSSI %d dBm\n",
                  WiFi.localIP().toString().c_str(), WiFi.RSSI());
    return true;
  }

  void maintain() {
    if (WiFi.status() == WL_CONNECTED) return;
    const unsigned long now = millis();
    if (now - lastAttemptAt < WIFI_RETRY_INTERVAL_MS) return;
    lastAttemptAt = now;
    Serial.println("[WIFI] Link down; starting non-blocking reconnect");
    WiFi.reconnect();
  }
};

WiFiConnectionManager wifiManager;

class ControllerDiscovery {
 private:
  IPAddress resolvedAddress = IPAddress(0, 0, 0, 0);
  unsigned long lastAttemptAt = 0;

 public:
  bool isResolved() const {
    return resolvedAddress != IPAddress(0, 0, 0, 0);
  }

  IPAddress address() const {
    return resolvedAddress;
  }

  void invalidate() {
    resolvedAddress = IPAddress(0, 0, 0, 0);
  }

  bool resolveNow() {
    if (WiFi.status() != WL_CONNECTED) return false;
    lastAttemptAt = millis();
    setNodeState(NodeState::DISCOVER_CONTROLLER);
    Serial.printf("[CONTROLLER] Resolving %s.local via mDNS\n", CONTROLLER_MDNS_HOST);

    const IPAddress candidate =
      MDNS.queryHost(CONTROLLER_MDNS_HOST, CONTROLLER_DISCOVERY_TIMEOUT_MS);
    if (candidate == IPAddress(0, 0, 0, 0)) {
      Serial.printf("[CONTROLLER] %s.local not found; retrying in %lu seconds\n",
                    CONTROLLER_MDNS_HOST,
                    CONTROLLER_DISCOVERY_RETRY_INTERVAL_MS / 1000);
      return false;
    }

    resolvedAddress = candidate;
    Serial.printf("[CONTROLLER] %s.local -> %s\n",
                  CONTROLLER_MDNS_HOST, resolvedAddress.toString().c_str());
    return true;
  }

  void maintain() {
    if (WiFi.status() != WL_CONNECTED) {
      invalidate();
      return;
    }
    if (isResolved()) return;
    if (millis() - lastAttemptAt >= CONTROLLER_DISCOVERY_RETRY_INTERVAL_MS) {
      resolveNow();
    }
  }
};

ControllerDiscovery controllerDiscovery;

class ControllerClient {
 public:
  unsigned long lastAttemptAt = 0;

  bool registerDevice() {
    if (WiFi.status() != WL_CONNECTED || config.isRegistered() ||
        !controllerDiscovery.isResolved()) return false;
    lastAttemptAt = millis();
    setNodeState(NodeState::REGISTER_CONTROLLER);

    WiFiClient client;
    HTTPClient http;
    const IPAddress controller = controllerDiscovery.address();
    const String url = "http://" + controller.toString() + ":" +
                       String(CONTROLLER_HTTP_PORT) + "/api/devices/register";
    http.setConnectTimeout(3000);
    http.setTimeout(5000);
    if (!http.begin(client, url)) {
      Serial.println("[REGISTER] Cannot initialize HTTP client");
      return false;
    }
    http.addHeader("Content-Type", "application/json");

    String body = "{\"hardware_id\":\"" + escapeJson(config.hardwareId) +
                  "\",\"hostname\":\"" + escapeJson(config.hostname() + ".local") +
                  "\",\"ip\":\"" + WiFi.localIP().toString() +
                  "\",\"type\":\"water-level\",\"firmware\":\"" +
                  FIRMWARE_VERSION + "\"}";
    Serial.printf("[REGISTER] POST %s\n", url.c_str());
    const int status = http.POST(body);
    String response;
    if (status > 0) response = http.getString();
    http.end();

    if (status < 200 || status >= 300) {
      Serial.printf("[REGISTER] Failed, HTTP status %d; retry later\n", status);
      return false;
    }
    response.trim();
    const int separator = response.indexOf('|');
    if (separator <= 0 || separator >= static_cast<int>(response.length()) - 1) {
      Serial.println("[REGISTER] Invalid controller response");
      return false;
    }
    String assignedId = response.substring(0, separator);
    String assignedTopic = response.substring(separator + 1);
    assignedId.trim();
    assignedTopic.trim();
    if (!assignedId.startsWith("water-") || assignedTopic != "devices/" + assignedId) {
      Serial.println("[REGISTER] Rejected inconsistent device ID/topic");
      return false;
    }
    if (!config.saveRegistration(assignedId, assignedTopic)) {
      Serial.println("[REGISTER] Cannot persist assigned identity");
      return false;
    }
    setNodeState(NodeState::REGISTERED);
    Serial.printf("[REGISTER] Assigned %s (%s); rebooting for mDNS hostname\n",
                  assignedId.c_str(), assignedTopic.c_str());
    delay(500);
    ESP.restart();
    return true;
  }

  void maintain() {
    if (config.isRegistered() || WiFi.status() != WL_CONNECTED) return;
    if (!controllerDiscovery.isResolved()) return;
    if (millis() - lastAttemptAt >= REGISTRATION_RETRY_INTERVAL_MS) registerDevice();
  }
};

ControllerClient controllerClient;
WiFiClient mqttNetworkClient;
PubSubClient mqttClient(mqttNetworkClient);
float lastDistanceCm = -1.0f;
float publishedBaselineCm = -1.0f;

class MQTTManager {
 public:
  unsigned long lastAttemptAt = 0;
  uint8_t consecutiveFailures = 0;

  static void callback(char *topic, byte *payload, unsigned int length) {
    String message;
    message.reserve(length);
    for (unsigned int i = 0; i < length; ++i) message += static_cast<char>(payload[i]);
    message.trim();
    Serial.printf("[MQTT] Command on %s: %s\n", topic, message.c_str());

    if (message == "measure_now") {
      const float value = sensor.measure();
      if (value < 0) {
        Serial.println("[SENSOR] measure_now: echo timeout");
        return;
      }
      lastDistanceCm = value;
      if (instance().publishDistance(value)) publishedBaselineCm = value;
    } else if (message == "restart") {
      Serial.println("[MQTT] Restart requested");
      delay(100);
      ESP.restart();
    } else if (message == "factory_reset") {
      Serial.println("[MQTT] Factory reset requested");
      config.factoryReset();
      delay(100);
      ESP.restart();
    } else if (message == "wifi_reset") {
      Serial.println("[MQTT] Wi-Fi reprovision requested");
      config.resetWiFi();
      delay(100);
      ESP.restart();
    } else {
      Serial.println("[MQTT] Unknown command ignored");
    }
  }

  static MQTTManager &instance() {
    static MQTTManager manager;
    return manager;
  }

  void begin() {
    mqttClient.setCallback(callback);
    mqttClient.setBufferSize(512);
    mqttClient.setSocketTimeout(3);
  }

  bool connect() {
    if (!config.isRegistered() || WiFi.status() != WL_CONNECTED ||
        !controllerDiscovery.isResolved()) return false;
    lastAttemptAt = millis();
    setNodeState(NodeState::MQTT_CONNECT);
    const IPAddress controller = controllerDiscovery.address();
    mqttClient.setServer(controller, MQTT_PORT);
    const String clientId = "esp32-" + config.hardwareId.substring(config.hardwareId.length() - 8);
    const String statusTopic = config.mqttBaseTopic + "/status";
    Serial.printf("[MQTT] Connecting to %s:%u\n", controller.toString().c_str(), MQTT_PORT);
    if (!mqttClient.connect(clientId.c_str(), statusTopic.c_str(), 0, true, "offline")) {
      Serial.printf("[MQTT] Connection failed (state %d); retry later\n", mqttClient.state());
      ++consecutiveFailures;
      if (consecutiveFailures >= 3) {
        consecutiveFailures = 0;
        controllerDiscovery.invalidate();
        Serial.println("[MQTT] Invalidated controller IP; mDNS will resolve it again");
      }
      return false;
    }
    consecutiveFailures = 0;
    const String commandTopic = config.mqttBaseTopic + "/command";
    mqttClient.publish(statusTopic.c_str(), "online", true);
    if (!mqttClient.subscribe(commandTopic.c_str())) {
      Serial.println("[MQTT] Command subscription failed");
    }
    Serial.printf("[MQTT] Connected; subscribed %s\n", commandTopic.c_str());
    setNodeState(NodeState::NORMAL_OPERATION);
    return true;
  }

  bool publishDistance(float value) {
    if (value < 0 || !mqttClient.connected()) return false;
    char payload[20];
    snprintf(payload, sizeof(payload), "%.1f", value);
    const String topic = config.mqttBaseTopic + "/distance_cm";
    const bool ok = mqttClient.publish(topic.c_str(), payload, false);
    Serial.printf("[MQTT] %s %s = %s\n", ok ? "Published" : "Publish failed:", topic.c_str(), payload);
    return ok;
  }

  void maintain() {
    if (mqttClient.connected()) {
      mqttClient.loop();
      return;
    }
    if (!config.isRegistered() || WiFi.status() != WL_CONNECTED) return;
    if (!controllerDiscovery.isResolved()) return;
    if (millis() - lastAttemptAt >= MQTT_RETRY_INTERVAL_MS) connect();
  }
};

class LocalWebServer {
 public:
  bool mdnsStarted = false;
  unsigned long lastMdnsAttemptAt = 0;

  bool startMDNS() {
    if (WiFi.status() != WL_CONNECTED) return false;
    lastMdnsAttemptAt = millis();
    if (!MDNS.begin(config.hostname().c_str())) {
      Serial.println("[MDNS] Start failed; will retry while Wi-Fi is connected");
      return false;
    }
    mdnsStarted = true;
    MDNS.addService("http", "tcp", 80);
    Serial.printf("[MDNS] http://%s.local\n", config.hostname().c_str());
    return true;
  }

  String statusJson() {
    String json = "{\"hardware_id\":\"" + escapeJson(config.hardwareId) +
                  "\",\"device_id\":\"" + escapeJson(config.deviceId) +
                  "\",\"hostname\":\"" + escapeJson(config.hostname() + ".local") +
                  "\",\"ip\":\"" + WiFi.localIP().toString() +
                  "\",\"wifi_rssi\":" + String(WiFi.RSSI()) +
                  ",\"mqtt\":" + (mqttClient.connected() ? "true" : "false") +
                  ",\"alert_level\":\"" + localAlarm.label() + "\"" +
                  ",\"distance_cm\":";
    json += lastDistanceCm < 0 ? "null" : String(lastDistanceCm, 1);
    json += "}";
    return json;
  }

  void begin() {
    webServer.on("/", HTTP_GET, []() {
      webServer.setContentLength(LIVE_STATUS_PAGE_LENGTH);
      webServer.send(200, "text/html; charset=utf-8", "");
      for (size_t offset = 0; offset < LIVE_STATUS_PAGE_LENGTH;
           offset += WEB_RESPONSE_CHUNK_SIZE) {
        const size_t remaining = LIVE_STATUS_PAGE_LENGTH - offset;
        const size_t chunkSize = remaining < WEB_RESPONSE_CHUNK_SIZE
                                   ? remaining
                                   : WEB_RESPONSE_CHUNK_SIZE;
        webServer.sendContent_P(LIVE_STATUS_PAGE + offset, chunkSize);
        delay(0);
      }
    });
    webServer.on("/status", HTTP_GET, [this]() {
      webServer.sendHeader("Cache-Control", "no-store");
      webServer.send(200, "application/json", statusJson());
    });
    webServer.on("/factory-reset", HTTP_POST, []() {
      webServer.send(200, "text/plain", "Factory reset complete. Restarting...");
      config.factoryReset();
      delay(250);
      ESP.restart();
    });
    webServer.onNotFound([]() { webServer.send(404, "text/plain", "Not found"); });
    webServer.begin();

    startMDNS();
    Serial.println("[WEB] Local server started on port 80");
  }

  void loop() {
    webServer.handleClient();
    if (!mdnsStarted && WiFi.status() == WL_CONNECTED &&
        millis() - lastMdnsAttemptAt >= CONTROLLER_DISCOVERY_RETRY_INTERVAL_MS) {
      startMDNS();
    }
  }
};

LocalWebServer localWeb;
unsigned long lastMeasureAt = 0;

void runPeriodicMeasurement() {
  const unsigned long now = millis();
  if (now - lastMeasureAt < MEASURE_INTERVAL_MS) return;
  lastMeasureAt = now;
  const float value = sensor.measure();
  if (value < 0) {
    Serial.println("[SENSOR] Echo timeout");
    return;  // Invalid values never update the display or publish baseline.
  }
  lastDistanceCm = value;
  localAlarm.updateDistance(value);
  Serial.printf("[SENSOR] Distance %.1f cm\n", value);
  if (publishedBaselineCm < 0 || fabsf(value - publishedBaselineCm) >= CHANGE_THRESHOLD_CM) {
    if (MQTTManager::instance().publishDistance(value)) publishedBaselineCm = value;
  }
}

void setup() {
  Serial.begin(115200);
  delay(300);
  Serial.println("\n[BOOT] ESP32 Water Edge Node");
  WiFi.onEvent(onWiFiStationDisconnected, ARDUINO_EVENT_WIFI_STA_DISCONNECTED);
  localAlarm.begin();
  sensor.begin();
  config.begin();

  if (!config.hasWiFi()) {
    setNodeState(NodeState::UNPROVISIONED);
    provisioning.begin();
    return;
  }

  setNodeState(NodeState::WIFI_CONFIGURED);
  if (!wifiManager.connectAtBoot()) {
    provisioning.begin();
    return;
  }

  localWeb.begin();
  MQTTManager::instance().begin();
  if (controllerDiscovery.resolveNow() && !config.isRegistered()) {
    controllerClient.registerDevice();
  } else if (controllerDiscovery.isResolved() && config.isRegistered()) {
    setNodeState(NodeState::REGISTERED);
    MQTTManager::instance().connect();
  }
}

void loop() {
  if (provisioning.active) {
    provisioning.loop();
    delay(2);
    return;
  }

  localWeb.loop();
  wifiManager.maintain();
  controllerDiscovery.maintain();
  controllerClient.maintain();
  MQTTManager::instance().maintain();
  runPeriodicMeasurement();
  localAlarm.loop();
  delay(2);
}
