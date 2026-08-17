"""Advertise the controller dashboard and host aliases over multicast DNS."""

from __future__ import annotations

import os
import socket
from ipaddress import ip_address
from typing import Any

from zeroconf import IPVersion, ServiceInfo, Zeroconf


def _enabled(value: str) -> bool:
    return value.strip().lower() not in {"0", "false", "no", "off"}


def _detect_lan_ipv4() -> str:
    configured = os.getenv("WATER_MDNS_ADDRESS", "").strip()
    if configured:
        address = ip_address(configured)
        if address.version != 4:
            raise ValueError("WATER_MDNS_ADDRESS must be an IPv4 address")
        return str(address)

    candidates: list[str] = []
    probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # UDP connect only asks the routing table which local interface it would use.
        probe.connect(("8.8.8.8", 80))
        candidates.append(probe.getsockname()[0])
    except OSError:
        pass
    finally:
        probe.close()

    try:
        candidates.extend(socket.gethostbyname_ex(socket.gethostname())[2])
    except OSError:
        pass

    for candidate in candidates:
        address = ip_address(candidate)
        if address.version == 4 and not address.is_loopback and not address.is_link_local:
            return str(address)
    raise RuntimeError("No LAN IPv4 address found; set WATER_MDNS_ADDRESS explicitly")


class MDNSAdvertiser:
    """Registers web host aliases plus an HTTP service while FastAPI is running."""

    def __init__(self, port: int = 8000) -> None:
        self.enabled = _enabled(os.getenv("WATER_MDNS_ENABLED", "true"))
        self.port = int(os.getenv("WATER_HTTP_PORT", str(port)))
        configured_names = os.getenv(
            "WATER_MDNS_HOSTNAMES",
            "edge-controller.local",
        )
        self.hostnames = [
            name.strip().lower().removesuffix(".")
            for name in configured_names.split(",")
            if name.strip()
        ]
        self.address: str | None = None
        self.error: str | None = None
        self._zeroconf: Zeroconf | None = None
        self._services: list[ServiceInfo] = []

    def start(self) -> None:
        if not self.enabled or self._zeroconf is not None:
            return
        try:
            self.address = _detect_lan_ipv4()
            self._zeroconf = Zeroconf(ip_version=IPVersion.V4Only)
            for index, hostname in enumerate(self.hostnames):
                fqdn = hostname if hostname.endswith(".local") else f"{hostname}.local"
                info = ServiceInfo(
                    "_http._tcp.local.",
                    f"Water Monitor {index + 1}._http._tcp.local.",
                    addresses=[socket.inet_aton(self.address)],
                    port=self.port,
                    properties={
                        "path": "/",
                        "description": "ESP32 water sensor monitor",
                    },
                    server=f"{fqdn}.",
                )
                self._zeroconf.register_service(info, allow_name_change=True)
                self._services.append(info)
            self.error = None
            print(
                f"[MDNS] {', '.join(self.hostnames)} -> {self.address}:{self.port}",
                flush=True,
            )
        except Exception as exc:
            self.error = str(exc) or repr(exc)
            print(f"[MDNS] Advertisement failed: {self.error}", flush=True)
            self.stop()

    def stop(self) -> None:
        if self._zeroconf is None:
            return
        for info in reversed(self._services):
            try:
                self._zeroconf.unregister_service(info)
            except Exception:
                pass
        self._services.clear()
        self._zeroconf.close()
        self._zeroconf = None

    def status(self) -> dict[str, Any]:
        return {
            "enabled": self.enabled,
            "active": self._zeroconf is not None and bool(self._services),
            "hostnames": self.hostnames,
            "address": self.address,
            "port": self.port,
            "error": self.error,
        }
