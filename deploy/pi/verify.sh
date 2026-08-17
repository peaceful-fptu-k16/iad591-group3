#!/usr/bin/env bash
set -u

failures=0
CONTROLLER_HOSTNAME="edge-controller"
CONTROLLER_FQDN="${CONTROLLER_HOSTNAME}.local"

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

SYSTEM_HOSTNAME="$(hostname --short)"
if [[ "${SYSTEM_HOSTNAME}" == "${CONTROLLER_HOSTNAME}" ]]; then
  echo "[OK] System hostname: ${SYSTEM_HOSTNAME}"
else
  echo "[FAIL] System hostname is '${SYSTEM_HOSTNAME}', expected '${CONTROLLER_HOSTNAME}'"
  failures=$((failures + 1))
fi

PI_LAN_IPS="$(hostname -I 2>/dev/null || true)"
PI_LAN_IP="$(awk '{print $1}' <<<"${PI_LAN_IPS}")"
if [[ -n "${PI_LAN_IP}" ]]; then
  echo "[OK] Raspberry Pi LAN IP: ${PI_LAN_IP}"
else
  echo "[FAIL] Raspberry Pi has no LAN address"
  failures=$((failures + 1))
fi

MDNS_IP=""
for _ in {1..15}; do
  MDNS_IP="$(avahi-resolve-host-name -4 "${CONTROLLER_FQDN}" 2>/dev/null | awk 'NR == 1 {print $2}')"
  [[ -n "${MDNS_IP}" ]] && break
  sleep 1
done

if [[ -z "${MDNS_IP}" ]]; then
  echo "[FAIL] ${CONTROLLER_FQDN} does not resolve through mDNS"
  failures=$((failures + 1))
elif [[ " ${PI_LAN_IPS} " == *" ${MDNS_IP} "* ]]; then
  echo "[OK] ${CONTROLLER_FQDN} -> ${MDNS_IP} (this Raspberry Pi)"
else
  echo "[FAIL] ${CONTROLLER_FQDN} -> ${MDNS_IP}, not one of this Pi's addresses: ${PI_LAN_IPS}"
  echo "       Another device may already be using the hostname '${CONTROLLER_HOSTNAME}'."
  failures=$((failures + 1))
fi

HTTP_OK=false
for _ in {1..15}; do
  if curl --fail --silent --max-time 3 \
      "http://${CONTROLLER_FQDN}:8000/health" >/dev/null 2>&1; then
    HTTP_OK=true
    break
  fi
  sleep 1
done
if [[ "${HTTP_OK}" == true ]]; then
  echo "[OK] Dashboard/API responds through http://${CONTROLLER_FQDN}:8000/"
else
  echo "[FAIL] Dashboard/API is not reachable through ${CONTROLLER_FQDN}:8000"
  failures=$((failures + 1))
fi

if mosquitto_pub -h "${CONTROLLER_FQDN}" -p 1883 \
    -t water-controller/verify -m ok; then
  echo "[OK] MQTT accepts messages through ${CONTROLLER_FQDN}:1883"
else
  echo "[FAIL] MQTT publish through ${CONTROLLER_FQDN}:1883"
  failures=$((failures + 1))
fi

if [[ ! -f /etc/avahi/services/water-controller.service ]]; then
  echo "[FAIL] Missing /etc/avahi/services/water-controller.service"
  failures=$((failures + 1))
elif avahi-browse --resolve --terminate _http._tcp 2>/dev/null |
       grep -Fq "Water Controller on ${CONTROLLER_HOSTNAME}" &&
     avahi-browse --resolve --terminate _mqtt._tcp 2>/dev/null |
       grep -Fq "Water Controller on ${CONTROLLER_HOSTNAME}"; then
  echo "[OK] Avahi advertises _http._tcp:8000 and _mqtt._tcp:1883"
else
  echo "[FAIL] Avahi HTTP/MQTT service records are not visible"
  failures=$((failures + 1))
fi

echo
ip -brief -4 address
echo
echo "Dashboard: http://${CONTROLLER_FQDN}:8000/"
echo "MQTT:      ${CONTROLLER_FQDN}:1883"
[[ -n "${PI_LAN_IP}" ]] && echo "LAN IP:    http://${PI_LAN_IP}:8000/"

exit "${failures}"
