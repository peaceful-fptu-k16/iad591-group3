#!/usr/bin/env bash
set -u

failures=0

check_service() {
  local service="$1"
  if systemctl is-active --quiet "${service}"; then
    echo "[OK] ${service} is active"
  else
    echo "[FAIL] ${service} is not active"
    failures=$((failures + 1))
  fi
}

check_service ssh.service
check_service mosquitto.service
check_service avahi-daemon.service
check_service water-controller.service

PI_LAN_IP="$(hostname -I | awk '{print $1}')"
if [[ -n "${PI_LAN_IP}" ]]; then
  echo "[OK] Raspberry Pi LAN IP: ${PI_LAN_IP}"
else
  echo "[FAIL] Raspberry Pi has no LAN address"
  failures=$((failures + 1))
fi

if curl --fail --silent --show-error http://127.0.0.1:8000/health; then
  echo
  echo "[OK] FastAPI health endpoint"
else
  echo "[FAIL] FastAPI health endpoint"
  failures=$((failures + 1))
fi

if mosquitto_pub -h 127.0.0.1 -p 1883 -t water-controller/verify -m ok; then
  echo "[OK] Mosquitto accepts local messages"
else
  echo "[FAIL] Mosquitto publish test"
  failures=$((failures + 1))
fi

if avahi-resolve-host-name edge-controller.local >/dev/null 2>&1; then
  echo "[OK] edge-controller.local resolves through mDNS"
else
  echo "[FAIL] edge-controller.local does not resolve locally"
  failures=$((failures + 1))
fi

echo
ip -brief -4 address
echo
echo "Dashboard: http://water-monitor.local:8000/"
[[ -n "${PI_LAN_IP}" ]] && echo "LAN IP:    http://${PI_LAN_IP}:8000/"

exit "${failures}"
