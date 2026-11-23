import socket
from types import SimpleNamespace as NS

from kickindirici import netinfo


def a(ip):
    return NS(family=socket.AF_INET, address=ip)


ADDRS = {
    "vEthernet (WSL (Hyper-V firewall))": [a("172.28.16.1")],
    "VirtualBox Host-Only Network": [a("192.168.56.1")],
    "ProtonVPN TUN": [a("10.2.0.2")],
    "Tailscale": [a("100.101.102.103")],
    "Wi-Fi": [a("192.168.1.42")],
    "Ethernet 3": [a("169.254.10.10")],
    "Loopback Pseudo-Interface 1": [a("127.0.0.1")],
}
STATS = {k: NS(isup=True) for k in ADDRS}


def test_skips_virtual_adapters():
    ips = [ip for _, ip, _ in netinfo.lan_candidates(ADDRS, STATS)]
    assert ips == ["192.168.1.42"]


def test_pick_prefers_route_ip_if_physical():
    addrs = dict(ADDRS, Ethernet=[a("10.0.0.5")])
    stats = dict(STATS, Ethernet=NS(isup=True))
    assert netinfo.pick_lan_ip(addrs, stats, route="10.0.0.5") == ("10.0.0.5", "Ethernet")
    # VPN route IP'si yoksayılır
    assert netinfo.pick_lan_ip(ADDRS, STATS, route="10.2.0.2") == ("192.168.1.42", "Wi-Fi")


def test_down_adapter_skipped():
    stats = dict(STATS, **{"Wi-Fi": NS(isup=False)})
    assert netinfo.pick_lan_ip(ADDRS, stats, route=None) == ("127.0.0.1", "")


def test_tailscale():
    assert netinfo.tailscale_ip(ADDRS) == "100.101.102.103"
