#!/usr/bin/env bash
set -Eeuo pipefail

APP_DIR="/opt/water-controller"
APP_USER="watercontroller"
SSH_USER="${WATER_SSH_USER:-admin}"
SCRIPT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run this installer as root: sudo bash install.sh" >&2
  exit 1
fi

if [[ -d "${SCRIPT_DIR}/controller/static" ]]; then
  SOURCE_DIR="${SCRIPT_DIR}/controller"
elif [[ -d "${SCRIPT_DIR}/../../controller/static" ]]; then
  SOURCE_DIR="$(cd -- "${SCRIPT_DIR}/../../controller" && pwd)"
else
  echo "Cannot find the packaged controller directory." >&2
  exit 1
fi

echo "[1/7] Installing Raspberry Pi packages"
apt-get update
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  python3 python3-venv python3-pip \
  mosquitto mosquitto-clients \
  avahi-daemon avahi-utils curl git openssh-server

echo "[2/7] Creating application account and directories"
if ! id "${APP_USER}" >/dev/null 2>&1; then
  useradd --system --user-group --home-dir "${APP_DIR}" --shell /usr/sbin/nologin "${APP_USER}"
fi
install -d -o "${APP_USER}" -g "${APP_USER}" -m 0750 \
  "${APP_DIR}" "${APP_DIR}/data" "${APP_DIR}/static" "${APP_DIR}/ml"

echo "[3/7] Installing Water Controller application"
systemctl stop water-controller.service 2>/dev/null || true
for source_file in "${SOURCE_DIR}"/*.py "${SOURCE_DIR}/requirements.txt"; do
  install -o "${APP_USER}" -g "${APP_USER}" -m 0640 "${source_file}" "${APP_DIR}/"
done
cp -a "${SOURCE_DIR}/static/." "${APP_DIR}/static/"
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/static"
if [[ -d "${SOURCE_DIR}/ml" ]]; then
  cp -a "${SOURCE_DIR}/ml/." "${APP_DIR}/ml/"
  chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/ml"
fi

SOURCE_DB="${SOURCE_DIR}/data/water_controller.db"
TARGET_DB="${APP_DIR}/data/water_controller.db"
if [[ -f "${SOURCE_DB}" ]]; then
  if [[ ! -f "${TARGET_DB}" || "${WATER_REPLACE_DATABASE:-0}" == "1" ]]; then
    if [[ -f "${TARGET_DB}" ]]; then
      BACKUP_DIR="/var/backups/water-controller/$(date -u +%Y%m%dT%H%M%SZ)"
      install -d -m 0750 "${BACKUP_DIR}"
      cp -a "${TARGET_DB}" "${BACKUP_DIR}/"
      echo "Existing database backed up to ${BACKUP_DIR}"
    fi
    install -o "${APP_USER}" -g "${APP_USER}" -m 0640 "${SOURCE_DB}" "${TARGET_DB}"
  else
    echo "Keeping the existing Pi database. Set WATER_REPLACE_DATABASE=1 to replace it."
  fi
fi

if [[ ! -x "${APP_DIR}/.venv/bin/python" ]]; then
  python3 -m venv "${APP_DIR}/.venv"
fi
chown -R "${APP_USER}:${APP_USER}" "${APP_DIR}/.venv"
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/python" -m pip install --upgrade pip
runuser -u "${APP_USER}" -- "${APP_DIR}/.venv/bin/python" -m pip install -r "${APP_DIR}/requirements.txt"

echo "[4/7] Configuring Mosquitto"
install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/mosquitto-water-controller.conf" \
  /etc/mosquitto/conf.d/water-controller.conf

echo "[5/7] Installing the deployment SSH public key for ${SSH_USER}"
SSH_PUBLIC_KEY_FILE="${SCRIPT_DIR}/ssh/water-controller-deploy.pub"
if [[ ! -f "${SSH_PUBLIC_KEY_FILE}" ]]; then
  echo "Missing SSH public key: ${SSH_PUBLIC_KEY_FILE}" >&2
  exit 1
fi
if ! SSH_PASSWD_ENTRY="$(getent passwd "${SSH_USER}")"; then
  echo "SSH user ${SSH_USER} does not exist. Set WATER_SSH_USER to the Raspberry Pi login user." >&2
  exit 1
fi
SSH_GROUP="$(id -gn "${SSH_USER}")"
SSH_HOME="$(cut -d: -f6 <<<"${SSH_PASSWD_ENTRY}")"
SSH_DIR="${SSH_HOME}/.ssh"
AUTHORIZED_KEYS="${SSH_DIR}/authorized_keys"
SSH_PUBLIC_KEY="$(<"${SSH_PUBLIC_KEY_FILE}")"
install -d -o "${SSH_USER}" -g "${SSH_GROUP}" -m 0700 "${SSH_DIR}"
touch "${AUTHORIZED_KEYS}"
if ! grep -qxF "${SSH_PUBLIC_KEY}" "${AUTHORIZED_KEYS}"; then
  printf '%s\n' "${SSH_PUBLIC_KEY}" >>"${AUTHORIZED_KEYS}"
fi
chown "${SSH_USER}:${SSH_GROUP}" "${AUTHORIZED_KEYS}"
chmod 0600 "${AUTHORIZED_KEYS}"

echo "[6/7] Configuring hostname, mDNS and systemd"
if command -v raspi-config >/dev/null 2>&1; then
  raspi-config nonint do_hostname edge-controller
else
  hostnamectl set-hostname edge-controller
fi
install -o root -g root -m 0644 \
  "${SCRIPT_DIR}/water-controller.service" \
  /etc/systemd/system/water-controller.service
systemctl daemon-reload
systemctl enable ssh.service mosquitto.service avahi-daemon.service water-controller.service
systemctl restart ssh.service mosquitto.service avahi-daemon.service water-controller.service

PI_LAN_IP="$(hostname -I | awk '{print $1}')"
echo "[7/7] Installation complete"
echo
echo "The installer did not create or modify any Wi-Fi access point."
echo "Keep the Raspberry Pi and every ESP32 on the same non-guest Wi-Fi network."
echo "  Pi IP:      ${PI_LAN_IP:-check with hostname -I}"
echo "  Dashboard:  http://water-monitor.local:8000/"
echo "  Controller: edge-controller.local"
echo "  SSH:        ssh ${SSH_USER}@edge-controller.local"
echo
echo "Reboot once if the hostname has just changed: sudo reboot"
