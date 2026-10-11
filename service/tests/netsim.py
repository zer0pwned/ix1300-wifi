"""In-memory network for the tests.

Some sandboxes do not let a process bind a port, also not on 127.0.0.1.
MemNet gives the same calls as ix1300.net.SystemNet:
  tcp_connect, tcp_listen: real socketpair() connections;
  udp: queues with IPv4-style addresses.
"""
import itertools
import queue
import socket
import threading

_ports = itertools.count(40000)


def binding_allowed():
    sk = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sk.bind(("127.0.0.1", 0))
        return True
    except OSError:
        return False
    finally:
        sk.close()


class _Listener:
    def __init__(self, net, addr):
        self.net = net
        self.addr = addr
        self.q = queue.Queue()
        self.timeout = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def accept(self):
        try:
            item = self.q.get(timeout=self.timeout)
        except queue.Empty:
            raise socket.timeout("timed out")
        if item is None:
            raise OSError("listener closed")
        return item

    def getsockname(self):
        return self.addr

    def close(self):
        if not self.closed:
            self.closed = True
            self.net.tcp.pop(self.addr[1], None)
            self.q.put(None)


class _Udp:
    def __init__(self, net, addr):
        self.net = net
        self.addr = addr
        self.q = queue.Queue()
        self.timeout = None
        self.closed = False

    def settimeout(self, t):
        self.timeout = t

    def setsockopt(self, *a):
        pass

    def getsockname(self):
        return self.addr

    def sendto(self, data, addr):
        self.net.deliver(bytes(data), self.addr, addr)
        return len(data)

    def recvfrom(self, n):
        if self.closed:
            raise OSError("socket closed")
        try:
            item = self.q.get(timeout=self.timeout)
        except queue.Empty:
            raise socket.timeout("timed out")
        if item is None:
            raise OSError("socket closed")
        data, src = item
        return data[:n], src

    def close(self):
        if not self.closed:
            self.closed = True
            self.net.udp_socks.pop(self.addr[1], None)
            self.q.put(None)


class MemNet:
    def __init__(self, host="127.0.0.1"):
        self.host = host
        self.tcp = {}
        self.udp_socks = {}
        self.lock = threading.Lock()

    def _port(self, port):
        return port or next(_ports)

    def tcp_listen(self, addr):
        with self.lock:
            port = self._port(addr[1])
            if port in self.tcp:
                raise OSError("port %d in use" % port)
            lst = _Listener(self, (self.host, port))
            self.tcp[port] = lst
            return lst

    def tcp_connect(self, addr, timeout):
        lst = self.tcp.get(addr[1])
        if lst is None or addr[0] not in (self.host, "localhost"):
            raise ConnectionRefusedError("connection refused %s:%d" % addr)
        a, b = socket.socketpair()
        a.settimeout(timeout)
        lst.q.put((b, (self.host, next(_ports))))
        return a

    def udp(self, bind=None, broadcast=False):
        with self.lock:
            port = self._port(bind[1] if bind else 0)
            if port in self.udp_socks:
                raise OSError("port %d in use" % port)
            s = _Udp(self, (self.host, port))
            self.udp_socks[port] = s
            return s

    def deliver(self, data, src, dst):
        ip, port = dst
        if ip not in (self.host, "255.255.255.255", "localhost"):
            return
        s = self.udp_socks.get(port)
        if s is not None and not s.closed:
            s.q.put((data, src))


class RealNet:
    """SystemNet on 127.0.0.1 (when binding is allowed)."""

    def __init__(self):
        from ix1300.net import SystemNet
        self.sys = SystemNet()
        self.host = "127.0.0.1"

    def tcp_listen(self, addr):
        return self.sys.tcp_listen((addr[0] or "127.0.0.1", addr[1]))

    def tcp_connect(self, addr, timeout):
        return self.sys.tcp_connect(addr, timeout)

    def udp(self, bind=None, broadcast=False):
        if bind is not None:
            bind = (bind[0] or "127.0.0.1", bind[1])
        return self.sys.udp(bind, broadcast)


def make_net():
    import os
    mode = os.environ.get("IX1300_TEST_NET", "auto")
    if mode == "real" or (mode == "auto" and binding_allowed()):
        return RealNet()
    return MemNet()
