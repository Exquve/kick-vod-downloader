"""LAN IP seçimi: sanal adaptörler (WSL, Hyper-V, VPN, VirtualBox...) atlanır."""
from __future__ import annotations

import ipaddress
import re
import socket

import psutil

VIRTUAL = re.compile(
    r"vEthernet|WSL|Hyper-?V|VirtualBox|VBox|VMware|VMnet|VPN|TAP-|\bTAP\b|\bTUN\b|WireGuard|"
    r"Tailscale|ZeroTier|Hamachi|Radmin|NordLynx|OpenVPN|Proton|Mullvad|Loopback|Bluetooth|"
    r"Docker|Npcap|Pseudo|Teredo|isatap|virbr|veth|br-|docker|utun|wg\d|tun\d|tap\d|\blo\b",
    re.I,
)
CGNAT = ipaddress.ip_network("100.64.0.0/10")  # Tailscale vb.


def _rank(ip: str) -> int | None:
    a = ipaddress.ip_address(ip)
    if a.is_loopback or a.is_link_local:
        return None
    if ip.startswith("192.168."):
        return 0
    if ip.startswith("10."):
        return 1
    if a in ipaddress.ip_network("172.16.0.0/12"):
        return 2
    return None


def lan_candidates(addrs: dict | None = None, stats: dict | None = None) -> list[tuple[int, str, str]]:
    addrs = psutil.net_if_addrs() if addrs is None else addrs
    stats = psutil.net_if_stats() if stats is None else stats
    out = []
    for name, lst in addrs.items():
        if VIRTUAL.search(name):
            continue
        st = stats.get(name)
        if st is not None and not getattr(st, "isup", True):
            continue
        for a in lst:
            if a.family != socket.AF_INET:
                continue
            r = _rank(a.address)
            if r is not None:
                out.append((r, a.address, name))
    out.sort()
    return out


def route_ip() -> str | None:
    """İnternete giden varsayılan rotanın kaynak IP'si (paket gönderilmez)."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))
            return s.getsockname()[0]
    except OSError:
        return None


def pick_lan_ip(addrs: dict | None = None, stats: dict | None = None,
                route: str | None | bool = False) -> tuple[str, str]:
    """(ip, adaptör adı) döner."""
    cands = lan_candidates(addrs, stats)
    r = route_ip() if route is False else route
    for _, ip, name in cands:
        if ip == r:
            return ip, name
    if cands:
        return cands[0][1], cands[0][2]
    if r and _rank(r) is not None:
        return r, ""
    return "127.0.0.1", ""


def tailscale_ip(addrs: dict | None = None) -> str | None:
    addrs = psutil.net_if_addrs() if addrs is None else addrs
    for name, lst in addrs.items():
        for a in lst:
            if a.family == socket.AF_INET:
                try:
                    if ipaddress.ip_address(a.address) in CGNAT and (
                            "tailscale" in name.lower() or name.lower().startswith("ts")):
                        return a.address
                except ValueError:
                    pass
    return None
