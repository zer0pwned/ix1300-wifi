"""Socket access and host address detection.

The protocol code gets all sockets from a "net" object. The default object
uses the real network. The tests can give an in-memory network instead.
"""
import fcntl
import os
import socket
import struct
import uuid


class SystemNet:
    """Real IPv4 sockets."""

    def tcp_connect(self, addr, timeout):
        return socket.create_connection(addr, timeout=timeout)

    def tcp_listen(self, addr):
        sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        sk.bind(addr)
        sk.listen(8)
        return sk

    def udp(self, bind=None, broadcast=False):
        sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        if broadcast:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        if bind is not None:
            sk.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sk.bind(bind)
        return sk


def local_ip_for(target=None):
    """Return the local IPv4 address that the kernel uses to send to TARGET."""
    sk = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sk.connect((target or "192.0.2.1", 9))
        return sk.getsockname()[0]
    finally:
        sk.close()


def mac_for_ip(ip):
    """Return the 6-byte MAC of the interface that has IP (Linux). Else the uuid node."""
    base = "/sys/class/net"
    if os.path.isdir(base):
        probe = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            for name in sorted(os.listdir(base)):
                req = struct.pack("256s", name.encode()[:15])
                try:
                    addr = fcntl.ioctl(probe.fileno(), 0x8915, req)[20:24]   # SIOCGIFADDR
                except OSError:
                    continue
                if socket.inet_ntoa(addr) == ip:
                    with open(os.path.join(base, name, "address")) as fh:
                        return bytes.fromhex(fh.read().strip().replace(":", ""))
        finally:
            probe.close()
    return uuid.getnode().to_bytes(6, "big")


def parse_mac(text):
    raw = bytes.fromhex(text.replace(":", "").replace("-", ""))
    if len(raw) != 6:
        raise ValueError("MAC must have 6 bytes: %r" % text)
    return raw
