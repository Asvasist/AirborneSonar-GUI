"""Find the board and use the selected wired interface without changing its IP."""
from dataclasses import dataclass
import ipaddress
import json
import os
import secrets
import socket
import struct
import re
import subprocess
import sys
import time

DISCOVERY_PORT = 5002
FALLBACK_BOARD = "169.254.59.46"


@dataclass(frozen=True)
class Interface:
    address: str
    prefix: int
    index: int = 0

    @property
    def network(self):
        return ipaddress.IPv4Interface(f"{self.address}/{self.prefix}").network


def rank_interfaces(rows):
    """Usable IPv4 interfaces, link-local (direct cable) first."""
    if isinstance(rows, dict):
        rows = [rows]
    candidates = []
    for row in rows or []:
        ip = ipaddress.IPv4Address(row["IPAddress"])
        if ip.is_loopback or ip.is_multicast or ip.is_unspecified:
            continue
        candidates.append(Interface(str(ip), int(row["PrefixLength"]), int(row.get("InterfaceIndex", 0))))
    candidates.sort(key=lambda item: (not ipaddress.IPv4Address(item.address).is_link_local, item.address))
    return candidates


def choose_interface(rows):
    """Prefer a usable link-local address for this direct-cable setup."""
    ranked = rank_interfaces(rows)
    if not ranked:
        raise OSError("Selected Ethernet adapter has no ready IPv4 address. Check the cable and link.")
    return ranked[0]


def _prefix(mask):
    """Prefix length from '0xffff0000' (macOS) or '255.255.0.0'."""
    value = int(mask, 16) if mask.lower().startswith("0x") else int(ipaddress.IPv4Address(mask))
    return bin(value).count("1")


def parse_ifconfig(text):
    """macOS/BSD `ifconfig` output -> rows with Name, IPAddress, PrefixLength."""
    rows, name = [], None
    for line in text.splitlines():
        if line and not line[0].isspace() and ":" in line:
            name = line.split(":", 1)[0]
        elif name and line.strip().startswith("inet "):
            words = line.split()
            if "netmask" in words:
                rows.append({"Name": name, "IPAddress": words[1],
                             "PrefixLength": _prefix(words[words.index("netmask") + 1]),
                             "InterfaceIndex": 0})
    return rows


def parse_ip_addr(text):
    """Linux `ip -o -4 addr show` output -> rows with Name, IPAddress, PrefixLength."""
    rows = []
    for match in re.finditer(r"^\d+:\s+(\S+)\s+inet\s+(\d+\.\d+\.\d+\.\d+)/(\d+)", text, re.M):
        rows.append({"Name": match.group(1), "IPAddress": match.group(2),
                     "PrefixLength": int(match.group(3)), "InterfaceIndex": 0})
    return rows


def _posix_rows():
    for command, parser in ((["/sbin/ifconfig"], parse_ifconfig), (["ifconfig"], parse_ifconfig),
                            (["ip", "-o", "-4", "addr", "show"], parse_ip_addr)):
        try:
            output = subprocess.run(command, capture_output=True, text=True, timeout=10, check=True).stdout
        except (OSError, subprocess.SubprocessError):
            continue
        rows = parser(output)
        if rows:
            return rows
    raise OSError("Cannot read the network interfaces. Type the laptop's Ethernet IP address "
                  "(for example 169.254.10.20) into the 'Laptop IP' box.")


def _posix_interface(name):
    """macOS/Linux: the wired interface carrying the 169.254.x.x direct-cable address."""
    rows = _posix_rows()
    wanted = (name or "").strip()
    if wanted.lower() not in ("", "auto", "ethernet"):
        rows = [row for row in rows if row["Name"] == wanted]
    ranked = rank_interfaces(rows)
    if not ranked:
        raise OSError("No Ethernet interface with an IPv4 address was found. Check the cable and adapter, "
                      "or type the laptop's Ethernet IP into the 'Laptop IP' box.")
    links = [item for item in ranked if ipaddress.IPv4Address(item.address).is_link_local]
    if len(links) > 1:  # e.g. Wi-Fi and the Ethernet adapter both self-assigned: ask the board.
        for item in links:
            try:
                if discover(item, timeout=1.0):
                    return item
            except (OSError, ValueError):
                continue
    return ranked[0]


def local_interface(name="Ethernet", local_ip=None):
    if local_ip:
        value = ipaddress.IPv4Interface(local_ip if "/" in local_ip else local_ip + "/16")
        return Interface(str(value.ip), value.network.prefixlen)
    if os.name != "nt":
        return _posix_interface(name)
    # Only read the named adapter. Quoting prevents adapter names becoming code.
    quoted = name.replace("'", "''")
    command = ("$ErrorActionPreference='Stop'; "
               "[Console]::OutputEncoding=[Text.Encoding]::UTF8; "
               f"Get-NetIPAddress -InterfaceAlias '{quoted}' -AddressFamily IPv4 -AddressState Preferred | "
               "Select-Object IPAddress,PrefixLength,InterfaceIndex | ConvertTo-Json -Compress")
    try:
        result = subprocess.run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", command],
                                capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=15,
                                creationflags=subprocess.CREATE_NO_WINDOW)
    except subprocess.TimeoutExpired as error:
        raise OSError("Timed out reading the Ethernet adapter; retrying will refresh its address.") from error
    if result.returncode:
        raise OSError(f"Cannot read adapter {name!r}: {result.stderr.strip()}. "
                      "Use --interface with its Windows name, or --local-ip ADDRESS/PREFIX.")
    return choose_interface(json.loads(result.stdout.lstrip("\ufeff").strip() or "[]"))


def bind_interface(sock, interface):
    sock.bind((interface.address, 0))
    if os.name == "nt" and interface.index:
        # Winsock IP_UNICAST_IF takes a 4-byte interface index in network order.
        # This selects Ethernet for this socket; it does not edit system routes.
        sock.setsockopt(socket.IPPROTO_IP, getattr(socket, "IP_UNICAST_IF", 31),
                        struct.pack("!I", interface.index))


def discover(interface, timeout=2.0):
    nonce = secrets.token_bytes(8)
    request, expected = b"SONARWHO" + nonce, b"SONARIP2" + nonce
    found = set()
    with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
        bind_interface(sock, interface)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        sock.sendto(request, (str(interface.network.broadcast_address), DISCOVERY_PORT))
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            sock.settimeout(max(0.001, deadline - time.monotonic()))
            try:
                data, peer = sock.recvfrom(64)
            except (socket.timeout, ConnectionResetError):
                # Windows can report ICMP port-unreachable for older firmware
                # without discovery; the legacy TCP fallback still applies.
                break
            if (data == expected and peer[1] == DISCOVERY_PORT and
                    ipaddress.IPv4Address(peer[0]) in interface.network and peer[0] != interface.address):
                found.add(peer[0])
    if len(found) > 1:
        raise ValueError("Multiple sonar boards replied; select one with --board IP.")
    return next(iter(found), None)


def connect_board(args, log=print):
    """Refresh the laptop address on every reconnect, including DHCP/APIPA changes."""
    interface = local_interface(args.interface, args.local_ip)
    board = args.board
    if board is None:
        board = discover(interface)
        if board is None:
            # Older firmware does not implement discovery. Keep it diagnosable.
            board = FALLBACK_BOARD
            log("No discovery reply; trying the legacy board address. Verify the new firmware banner.")
    if ipaddress.IPv4Address(board) not in interface.network or board == interface.address:
        raise OSError(f"Board {board} is not a distinct address on Ethernet {interface.address}/{interface.prefix}. "
                      "This direct-cable firmware uses 169.254.0.0/16; unrelated subnets need matching "
                      "board configuration. --board selects an address, it does not change the board's IP.")
    log(f"Connecting to {board}:{args.port} through {args.interface} ({interface.address})...")
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        bind_interface(sock, interface)
        sock.settimeout(10)
        sock.connect((board, args.port))
        return sock, board, interface.address
    except BaseException:
        sock.close()
        raise


def permission_help(error):
    if sys.platform == "darwin" and isinstance(error, PermissionError):
        return ("macOS blocked this connection (permission denied). Open System Settings > Privacy & Security > "
                "Local Network, allow this app (or Terminal), then reconnect. Also check any VPN or firewall "
                "that blocks local networks. No network or security settings were changed.")
    if getattr(error, "winerror", None) == 10013 or isinstance(error, PermissionError):
        return ("Windows blocked this socket (10013 / permission denied). Check the VPN's local-network "
                "access or kill-switch policy and the Python outbound firewall rule. Changing the board IP "
                "cannot remove a Windows permission block. No network/security settings were changed.")
    return None
