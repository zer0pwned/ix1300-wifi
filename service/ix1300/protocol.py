"""Wi-Fi protocol of the ScanSnap iX1300.

UDP 52217: discovery and keepalive.
Port2 (TCP 53219): reserve and release.
Port1 (TCP 53218): SCSI command frames.
Trigger port (UDP, our port): button events from the scanner.

All integers on the wire are big-endian. Refer to PROTOCOL.md.
"""
import logging
import socket
import struct
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import jpeg
from .net import SystemNet

log = logging.getLogger("ix1300.protocol")

KEY = b"\x56\x45\x4e\x53"
IFVER = 0x0010
SEARCH_PORT = 52217
PORT1 = 53218
PORT2 = 53219
TRIGGER_PORT = 55265
PFU_KEY = b"pFusCANsNapFiPfu"

WINDOW_WIDTH = 0x2880        # 1/1200 inch (219.5 mm)
AUTO_LENGTH = 0xA543         # 1/1200 inch (about 895 mm), automatic length
PREREAD_LENGTH = 0x4350
PATHS = ("top", "front")     # paper path: top chute (ADF) or front slot (return path)
# Front slot (return path) job [HW]: E9 data length, D4 params byte 1 and byte 4.
FRONT_PREREAD_LENGTH = 0x4410
FRONT_D4_PATH = 0x06
FRONT_D4_BYTE4 = 0x00
READ_SIZE = 0x300000
# Long page mode (ScanSnap Home paper code 0x8003; PROTOCOL.md "Long page mode"). Top chute only.
# E9 data length 0x23680 (about 3,070 mm): DoSetPreReadMode 0x10017d940 [C].
# D4 params byte 13 = 0x80: mutual transfer, SetWindow 0x10017e090; firmware flag 0x207f4 [C].
# The window length stays 0xA543: DoSetWindowInfo 0x100156f00 [C].
# READ size 0x20000 (MaxTransferSize_of_MutualRead): TelRead 0x3a834 [C].
# The scanner side of these values is not verified on the hardware [NV].
LONG_PREREAD_LENGTH = 0x23680
LONG_D4_MUTUAL = 0x80
MUTUAL_READ_SIZE = 0x20000
IMAGE_PREFIX = 2             # page ID and sequence ID before the image data of a READ reply
LONG_MAX_MM = 3000           # official limit of the long page mode
LONG_DPI_VALUES = (150, 200, 300)   # 3,000 mm at 600 dpi needs more than 65,535 JPEG lines
REPLY_SLACK = 64             # bytes that a reply can have more than the requested data-in length
DPI_VALUES = (150, 200, 300, 600)

# Trigger codes (UDP packet +8).
TRG_SCAN = 0x01
TRG_STOP = 0x02
TRG_CANCEL = 0x03
TRG_POWER_OFF = 0x10
TRG_FORCE_RELEASE = 0x11
TRG_REQUEST_RELEASE = 0x12
TRG_POWER_ON = 0x30

RESERVE_RESULTS = {
    -1: "general error",
    -2: "password necessary",
    -3: "wrong password",
    -4: "scanner occupied by another host",
    -5: "IF version not supported",
    -6: "host ID not registered",
    -7: "host ID not valid",
    -8: "sequence error",
    -9: "USB mode error",
}


# --- Errors ---------------------------------------------------------------

class IxError(Exception):
    """Base class of all scanner errors."""

    result = None                # ScanResult with the complete sheets before the error (scan only)


class ProtocolError(IxError):
    """The scanner sent a frame that we cannot use, or closed the connection."""


class ReserveError(IxError):
    def __init__(self, result):
        self.result = result
        super().__init__("reserve failed: %d (%s)" % (result, RESERVE_RESULTS.get(result, "unknown")))


class ScanTimeout(IxError):
    """The scanner gave no data for too long."""


class Stopped(ScanTimeout):
    """The caller stopped a READ (Scanner.abort, for example at a service stop deadline)."""


class SenseError(IxError):
    """CHECK CONDITION with sense data that is not a normal end."""

    def __init__(self, where, sense):
        self.where = where
        self.sense = sense
        self.result = None           # ScanResult with the pages before the error
        super().__init__("%s: %s (%s)" % (where, self.describe(sense), sense))

    @staticmethod
    def describe(sense):
        return SENSE_TEXT.get((sense.key, sense.asc, sense.ascq), "scanner error")


class PaperJam(SenseError):
    pass


class NoPaper(SenseError):
    pass


class PaperLost(NoPaper):
    """"No paper" in the middle of a sheet: the job is not complete."""


class DoubleFeed(SenseError):
    pass


class CoverOpen(SenseError):
    pass


SENSE_TEXT = {
    (3, 0x80, 0x01): "paper jam: open the cover, remove the paper and load it again",
    (3, 0x80, 0x02): "cover open",
    (3, 0x80, 0x03): "no paper",
    (3, 0x80, 0x07): "double feed: fan the paper and load it again",
    (3, 0x80, 0x13): "temporary no data",
}


def sense_error(where, sense):
    cls = {
        (3, 0x80, 0x01): PaperJam,
        (3, 0x80, 0x02): CoverOpen,
        (3, 0x80, 0x03): NoPaper,
        (3, 0x80, 0x07): DoubleFeed,
    }.get((sense.key, sense.asc, sense.ascq), SenseError)
    return cls(where, sense)


# --- Helpers --------------------------------------------------------------

def pfu_encode(text):
    return "".join("%03d" % (b + 11 + PFU_KEY[i % 16]) for i, b in enumerate(text.encode())).encode()


def recv_exact(sk, n, deadline=None):
    """Receive N bytes. DEADLINE (time.monotonic) limits the total time, not only each recv."""
    buf = bytearray()
    base = sk.gettimeout() if deadline is not None else None
    try:
        while len(buf) < n:
            if deadline is not None:
                left = deadline - time.monotonic()
                if left <= 0:
                    raise ScanTimeout("frame not complete in time (%d of %d bytes)" % (len(buf), n))
                sk.settimeout(left if base is None else min(base, left))
            chunk = sk.recv(min(n - len(buf), 1 << 20))
            if not chunk:
                raise ProtocolError("connection closed after %d of %d bytes" % (len(buf), n))
            buf += chunk
    finally:
        if deadline is not None:
            sk.settimeout(base)
    return bytes(buf)


def search_packet(host_ip, host_mac, keepalive=False, trigger_port=TRIGGER_PORT):
    """32-byte discovery (keepalive=False) or keepalive (keepalive=True) packet."""
    return (KEY + struct.pack(">I", 1 if keepalive else 0) + socket.inet_aton(host_ip) + host_mac +
            b"\0\0" + struct.pack(">IH", (trigger_port - 1) & 0xFFFFFFFF, IFVER) + bytes(6))


def _cstr(b):
    return b.split(b"\0")[0].decode("ascii", "replace").strip()


@dataclass
class DeviceInfo:
    ip: str
    port1: int
    port2: int
    mac: str
    name: str
    product: str
    serial: str
    owner_ip: str
    owner_id: str
    status1: int
    status2: int
    flags_a: int
    flags_b: int
    devstatus: int
    src: tuple = ()

    @classmethod
    def parse(cls, d, src=()):
        """Parse a 144-byte search reply. Return None for other packets."""
        if len(d) < 144 or d[:4] != KEY:
            return None
        st1, st2 = struct.unpack_from(">HH", d, 4)
        if st2 == 0xFFFF:
            return None
        p1, p2 = struct.unpack_from(">II", d, 20)
        if not (0 < p1 < 65536 and 0 < p2 < 65536):
            return None                            # not a valid port: not a usable reply
        return cls(ip=socket.inet_ntoa(d[16:20]), port1=p1, port2=p2, mac=d[28:34].hex(":"),
                   name=_cstr(d[40:104]), product=_cstr(d[104:120]), serial=_cstr(d[132:144]),
                   owner_ip=socket.inet_ntoa(d[120:124]), owner_id=d[124:132].hex().upper(),
                   status1=st1, status2=st2, flags_a=d[36], flags_b=d[37],
                   devstatus=struct.unpack_from(">H", d, 38)[0], src=tuple(src))

    @property
    def tls(self):
        return bool(self.flags_a & 0x08)


def discover(host_ip, host_mac, target=None, port=SEARCH_PORT, timeout=3.0, net=None,
             broadcast="255.255.255.255", unicast=None, repeat=3, interval=0.5):
    """Search for scanners. Return the list of DeviceInfo replies.

    TARGET set: unicast search only; stop at the first reply.
    TARGET None: broadcast search to BROADCAST, and also a unicast probe to
    UNICAST if it is set. A busy scanner does not answer all broadcast
    probes [HW], so each probe goes REPEAT times, INTERVAL s apart, in TIMEOUT.
    """
    net = net or SystemNet()
    sk = net.udp(broadcast=target is None)
    found = []
    pkt = search_packet(host_ip, host_mac)
    dests = [target] if target else [broadcast] + ([unicast] if unicast else [])
    try:
        start = time.monotonic()
        end = start + timeout
        sent = 0
        while True:
            now = time.monotonic()
            if sent < max(1, repeat) and now >= start + sent * interval:
                for d in dests:
                    try:
                        sk.sendto(pkt, (d, port))
                    except OSError as e:
                        log.debug("search to %s: %s", d, e)
                sent += 1
            left = end - time.monotonic()
            if left <= 0:
                break
            wait = left
            if sent < max(1, repeat):
                wait = min(left, max(0.01, start + sent * interval - time.monotonic()))
            sk.settimeout(wait)
            try:
                data, src = sk.recvfrom(2048)
            except socket.timeout:
                continue
            dev = DeviceInfo.parse(data, src)
            if dev and src and src[0] not in (dev.ip, target, unicast):
                log.debug("search reply from %s for %s: ignored (address does not agree)", src[0], dev.ip)
                dev = None
            if dev and all(dev.mac != f.mac for f in found):
                found.append(dev)
                if target is not None or (unicast and unicast in (dev.ip, src[0] if src else None)):
                    break
    finally:
        sk.close()
    return found


def keepalive(scanner_ip, host_ip, host_mac, port=SEARCH_PORT, timeout=3.0, net=None, repeat=3, interval=0.5):
    """Send a keepalive. Return True if the scanner answered (12 bytes, 80 01).

    A scanner in Wi-Fi power-save mode can lose packets and answer late [HW].
    So the packet goes up to REPEAT times, INTERVAL s apart, and the function
    waits up to TIMEOUT s for one reply.
    """
    net = net or SystemNet()
    sk = net.udp()
    pkt = search_packet(host_ip, host_mac, keepalive=True)
    try:
        start = time.monotonic()
        end = start + timeout
        sent = 0
        while True:
            if sent < max(1, repeat) and time.monotonic() >= start + sent * interval:
                try:
                    sk.sendto(pkt, (scanner_ip, port))
                except OSError as e:
                    log.debug("keepalive send: %s", e)
                sent += 1
            left = end - time.monotonic()
            if left <= 0:
                return False
            wait = left
            if sent < max(1, repeat):
                wait = min(left, max(0.01, start + sent * interval - time.monotonic()))
            sk.settimeout(wait)
            try:
                data, src = sk.recvfrom(256)
            except socket.timeout:
                continue
            if src and src[0] != scanner_ip:
                log.debug("keepalive reply from %s: ignored", src[0])
                continue
            if len(data) >= 6 and data[:4] == KEY and data[4:6] == b"\x80\x01":
                return True
    finally:
        sk.close()


# --- Trigger packets ------------------------------------------------------

@dataclass
class Trigger:
    code: int
    cond: bytes
    seq: int
    reason: int
    src: tuple = ()

    @property
    def long_press(self):
        return bool(self.cond[2] & 0x20)


TRIGGER_SIZE = 48
TRIGGER_CODES = (TRG_SCAN, TRG_STOP, TRG_CANCEL, TRG_POWER_OFF, TRG_FORCE_RELEASE, TRG_REQUEST_RELEASE,
                 TRG_POWER_ON)


def parse_trigger(data, src=()):
    """Parse a 48-byte trigger packet. Return None for other packets (other size, key or code)."""
    if len(data) != TRIGGER_SIZE or data[4:8] != KEY:
        return None
    code = struct.unpack_from(">I", data, 8)[0]
    if code not in TRIGGER_CODES:
        return None
    return Trigger(code=code, cond=bytes(data[12:16]), seq=data[16], reason=data[17], src=tuple(src))


class TriggerDedup:
    """Drop the copies of one event.

    The scanner sends each event 3 times, 0.5 s apart, with one sequence number
    (+16; 1..255, then 1 again). A copy can wait in the socket for a long time
    (during a scan job), so there is no time limit. UDP can also change the
    order of packets. So a trigger is new only if its sequence number is not in
    the last WINDOW accepted numbers for that code and source.
    """

    WINDOW = 16

    def __init__(self):
        self.recent = {}

    def is_new(self, trg, now=None):
        key = (trg.src[0] if trg.src else "", trg.code)
        seen = self.recent.setdefault(key, [])
        if trg.seq in seen:
            return False
        seen.append(trg.seq)
        del seen[:-self.WINDOW]
        return True

    def reset(self):
        """Forget all sequence numbers (the scanner starts again at 1 after power on)."""
        self.recent.clear()


# --- Status and sense -----------------------------------------------------

@dataclass
class HwStatus:
    raw: bytes

    def _bit(self, byte, mask):
        return len(self.raw) > byte and bool(self.raw[byte] & mask)

    @property
    def hopper_empty(self):
        return self._bit(3, 0x80)

    @property
    def paper_at_exit(self):
        return self._bit(3, 0x01)

    @property
    def paper_at_feed(self):
        return self._bit(2, 0x80)

    @property
    def front_slot(self):
        """True if a sheet is in the front slot: hopper empty AND feed sensor set [HW].

        The exit sensor bit (byte 3 bit 0) can stay set with no paper (seen after
        a halt), so this test does not use it."""
        return self.hopper_empty and self.paper_at_feed

    @property
    def paper_path(self):
        """"top" (paper in the hopper), "front" (paper in the front slot) or None (no paper).

        Valid only before a job: during a job the scanner pulls sheets into the path."""
        if not self.hopper_empty:
            return "top"
        return "front" if self.front_slot else None

    @property
    def button_short(self):
        return self._bit(4, 0x01)

    @property
    def button_long(self):
        return self._bit(4, 0x20)

    @property
    def host_id(self):
        """Registered host ID (16 hex digits), "" if the reply is short, None if not set."""
        if len(self.raw) < 0x28:
            return ""
        hid = self.raw[0x20:0x28]
        if hid in (b"\xff" * 8, bytes(8)):
            return None
        return hid.hex().upper()


@dataclass
class Sense:
    raw: bytes

    @property
    def key(self):
        return self.raw[2] & 0x0F if len(self.raw) > 2 else 0

    @property
    def eom(self):
        return len(self.raw) > 2 and bool(self.raw[2] & 0x40)

    @property
    def ili(self):
        return len(self.raw) > 2 and bool(self.raw[2] & 0x20)

    @property
    def asc(self):
        return self.raw[12] if len(self.raw) > 12 else 0

    @property
    def ascq(self):
        return self.raw[13] if len(self.raw) > 13 else 0

    @property
    def info(self):
        """Information field (bytes 3-6, BE32). In a mutual READ: the residue of the last chunk."""
        return int.from_bytes(self.raw[3:7], "big") if len(self.raw) >= 7 else 0

    @property
    def temporary_no_data(self):
        return (self.key, self.asc, self.ascq) == (3, 0x80, 0x13)

    def __str__(self):
        return "key=%d asc=%02x ascq=%02x eom=%d" % (self.key, self.asc, self.ascq, self.eom)


# --- Scan settings and results -------------------------------------------

def mm_to_length(mm):
    return int(round(mm / 25.4 * 1200))


@dataclass
class ScanSettings:
    dpi: int = 300
    color: str = "auto"              # auto, color, gray
    length: int = AUTO_LENGTH         # 1/1200 inch
    path: str = "top"                 # "top" (chute) or "front" (front slot, return path)
    long_page: bool = False           # long page mode: up to LONG_MAX_MM, mutual READs, top chute only
    double_feed: bool = True          # D4 byte 4: 0x88 = detection on, 0x00 = off [NV]

    def __post_init__(self):
        if self.path not in PATHS:
            raise ValueError("path must be top or front")
        if self.dpi not in DPI_VALUES:
            raise ValueError("dpi must be one of %s" % (DPI_VALUES,))
        if self.color not in ("auto", "color", "gray"):
            raise ValueError("color must be auto, color or gray")
        if not 0 < self.length <= AUTO_LENGTH:
            raise ValueError("length must be 1..%d (1/1200 inch)" % AUTO_LENGTH)
        if self.long_page:
            if self.path != "top":
                raise ValueError("long page mode needs the top chute")
            if self.dpi not in LONG_DPI_VALUES:
                raise ValueError("long page mode needs dpi %s" % (LONG_DPI_VALUES,))
            if self.length != AUTO_LENGTH:
                raise ValueError("long page mode uses the automatic window length")

    @property
    def max_lines(self):
        """The largest line count of one side: the E9 length in long page mode, else the window."""
        return (LONG_PREREAD_LENGTH if self.long_page else self.length) * self.dpi // 1200

    def e9_data(self):
        pre = FRONT_PREREAD_LENGTH if self.path == "front" else PREREAD_LENGTH
        if self.long_page:
            pre = LONG_PREREAD_LENGTH
        return struct.pack(">HHII", self.dpi, self.dpi, WINDOW_WIDTH, pre) + b"\x05\x00" + bytes(18)

    def d4_data(self):
        params = bytearray([0x00, 0x03, 0x01, 0x01, 0x88, 0x00, 0xC1, 0x80, 0x80, 0x80, 0x80, 0x80, 0x80, 0x00])
        params += bytes(17) + b"\x30"
        if self.path == "front":
            params[1] = FRONT_D4_PATH         # front slot [HW]
            params[4] = FRONT_D4_BYTE4
        if self.color != "auto":
            params[6] = 0x80             # fixed colour mode (not verified on hardware)
        if self.long_page:
            params[13] = LONG_D4_MUTUAL      # mutual transfer [NV]
        if not self.double_feed:
            params[4] = 0x00             # no double-feed detection (not verified on hardware)
        desc = bytearray(48)
        desc[1] = 0x10                   # JPEG
        struct.pack_into(">HH", desc, 2, self.dpi, self.dpi)
        desc[6] = 0x02 if self.color == "gray" else 0x05
        desc[7] = 0x82 if self.dpi == 300 else 0x81
        desc[8] = {600: 0x0A, 300: 0x0D}.get(self.dpi, 0x05)
        struct.pack_into(">II", desc, 10, WINDOW_WIDTH, self.length)
        desc[18] = 0x04
        desc[30] = 0x01
        return bytes(params) + bytes(desc)


@dataclass
class Side:
    sheet: int            # 0-based sheet number in the job
    side: str             # "front" or "back"
    page_id: int
    jpeg: bytes
    width: int = 0        # from the pixel size read
    lines: int = 0        # real line count from the pixel size read
    dpi: int = 300
    read: dict = field(default_factory=dict)   # long page mode: READ counts and the last chunk (raw.json)


@dataclass
class ScanResult:
    sides: List[Side] = field(default_factory=list)
    sheets: int = 0
    jam_warning: bool = False
    cancelled: bool = False
    warnings: List[str] = field(default_factory=list)


# --- Scanner client -------------------------------------------------------

class Scanner:
    """Client for one scanner. Port1 stays open for one job."""

    def __init__(self, ip, host_ip, host_mac, host_id, port1=PORT1, port2=PORT2,
                 trigger_port=TRIGGER_PORT, search_port=SEARCH_PORT, net=None,
                 timeout=30.0, data_timeout=60.0, reserve_timeout=30, poll=0.1,
                 frame_timeout=120.0, side_timeout=600.0, max_side_bytes=64 << 20):
        self.ip = ip
        self.host_ip = host_ip
        self.host_mac = host_mac
        self.host_id = host_id
        if len(host_mac) != 6 or len(host_id) != 8:
            raise ValueError("host_mac needs 6 bytes and host_id needs 8 bytes")
        self.port1 = port1
        self.port2 = port2
        self.trigger_port = trigger_port
        self.search_port = search_port
        self.net = net or SystemNet()
        self.timeout = timeout
        self.data_timeout = data_timeout
        self.reserve_timeout = reserve_timeout
        self.poll = poll
        self.frame_timeout = frame_timeout          # maximum time for one reply frame
        self.side_timeout = side_timeout            # maximum time for one side
        self.max_side_bytes = max_side_bytes        # maximum JPEG size of one side
        self.p1 = None
        self.job_state = "idle"                     # idle, active, ended, uncertain
        self.abort = None                           # callable: True stops a READ loop (Stopped)
        # Cancel with sheets left (D8, E0, wait, D6; PROTOCOL.md 8.6)
        self.flush_timeout = 120.0                  # longest wait for an empty paper path
        self.flush_poll = 0.5                       # seconds between two C2 reads in the wait
        self.flush_exit_wait = 5.0                  # the exit bit can stay set without paper [HW]
        self.stop_flush_wait = 5.0                  # the wait after Stopped (the stop deadline is near)

    # Connections
    def _connect(self, port):
        sk = self.net.tcp_connect((self.ip, port), self.timeout)
        try:
            sk.settimeout(self.timeout)
            greet = recv_exact(sk, 16)
        except Exception:
            sk.close()
            raise
        if greet[4:8] != KEY:
            sk.close()
            raise ProtocolError("bad greeting %s" % greet.hex(" "))
        return sk

    def close(self):
        if self.p1 is not None:
            try:
                self.p1.close()
            finally:
                self.p1 = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    # Port2
    def _port2(self, msg, replylen):
        sk = self._connect(self.port2)
        try:
            sk.sendall(msg)
            return recv_exact(sk, replylen)
        finally:
            sk.close()

    def reserve_message(self, mode=0):
        t = time.localtime()
        m = bytearray(128)
        struct.pack_into(">I", m, 0, 128)
        m[4:8] = KEY
        struct.pack_into(">I", m, 8, 0x11)
        m[16:22] = self.host_mac
        struct.pack_into(">HBB", m, 32, IFVER, max(0, min(255, int(self.reserve_timeout))), 0)
        struct.pack_into(">I", m, 36, mode)
        struct.pack_into(">I", m, 40, 0)
        m[44:48] = socket.inet_aton(self.host_ip)
        struct.pack_into(">I", m, 48, self.trigger_port)
        struct.pack_into(">HBBBBBB", m, 100, t.tm_year, t.tm_mon, t.tm_mday, t.tm_hour, t.tm_min, t.tm_sec, 0)
        m[108:116] = self.host_id
        struct.pack_into(">i", m, 116, t.tm_gmtoff)
        return bytes(m)

    def reserve(self, mode=0):
        rep = self._port2(self.reserve_message(mode), 20)
        if rep[4:8] != KEY:
            raise ProtocolError("bad reserve reply %s" % rep.hex(" "))
        res = struct.unpack_from(">i", rep, 8)[0]
        if res != 0:
            raise ReserveError(res)
        log.debug("reserve OK")
        return res

    def release(self, force=False):
        m = bytearray(32)
        struct.pack_into(">I", m, 0, 32)
        m[4:8] = KEY
        struct.pack_into(">I", m, 8, 0x12)
        m[16:22] = self.host_mac
        struct.pack_into(">I", m, 24, 1 if force else 0)
        rep = self._port2(bytes(m), 16)
        if rep[4:8] != KEY:
            raise ProtocolError("bad release reply %s" % rep.hex(" "))
        return struct.unpack_from(">i", rep, 8)[0]

    def keepalive(self, timeout=3.0, repeat=3, interval=0.5):
        return keepalive(self.ip, self.host_ip, self.host_mac, self.search_port, timeout, self.net, repeat, interval)

    # Port1
    def cmd(self, cdb, out=b"", inlen=0):
        """Send one CDB. Return (scsi status, data-in)."""
        if self.p1 is None:
            self.p1 = self._connect(self.port1)
        h = bytearray(64)
        struct.pack_into(">I", h, 0, 64 + len(out))
        h[4:8] = KEY
        struct.pack_into(">I", h, 8, 1)
        h[16:22] = self.host_mac
        h[24:28] = socket.inet_aton(self.host_ip)
        struct.pack_into(">III", h, 32, len(cdb), inlen, len(out))
        h[48:48 + len(cdb)] = bytes(cdb)
        deadline = time.monotonic() + self.frame_timeout
        try:
            self.p1.sendall(bytes(h) + bytes(out))
            rh = recv_exact(self.p1, 40, deadline)
            total, key, st, scsi = struct.unpack_from(">I4sii", rh, 0)
            if key != KEY or total < 40:
                raise ProtocolError("bad reply header %s" % rh.hex(" "))
            # The data-in is at most INLEN bytes (+ a small prefix, for example the page/sequence ID).
            if total - 40 > inlen + REPLY_SLACK:
                raise ProtocolError("command %02X: reply of %d bytes for %d requested" % (cdb[0], total - 40, inlen))
            data = recv_exact(self.p1, total - 40, deadline) if total > 40 else b""
        except (OSError, IxError):
            self.close()
            raise
        if st != 0:
            raise ProtocolError("command %02X: frame status %d" % (cdb[0], st))
        return scsi, data

    def request_sense(self):
        scsi, d = self.cmd([0x03, 0, 0, 0, 18, 0], inlen=18)
        if scsi != 0 or len(d) < 14:
            raise ProtocolError("REQUEST SENSE: status %d, %d bytes" % (scsi, len(d)))
        return Sense(d)

    def inquiry(self):
        return self.cmd([0x12, 0, 0, 0, 0x60, 0], inlen=0x60)[1]

    def hw_status(self, n=0x30):
        """GET HW STATUS (C2). Raise ProtocolError for a bad status or a reply shorter than 5 bytes:
        a missing sensor byte must not read as "no paper" or "no jam"."""
        scsi, d = self.cmd([0xC2, 0, 0, 0, 0, 0, 0, 0, n, 0], inlen=n)
        if scsi == 2:
            try:
                self.request_sense()                # clear the CHECK CONDITION
            except (OSError, IxError):
                pass
        if scsi != 0 or len(d) < 5:
            raise ProtocolError("C2: SCSI status %d, %d bytes" % (scsi, len(d)))
        return HwStatus(d)

    def _check(self, name, cdb, out=b"", inlen=0, busy_retries=100):
        for _ in range(busy_retries):
            scsi, data = self.cmd(cdb, out, inlen)
            if scsi == 8:
                time.sleep(0.05)
                continue
            if scsi == 0:
                return data
            sense = self.request_sense()
            raise sense_error(name, sense)
        raise ScanTimeout("%s: scanner busy" % name)

    # Scan job
    def _read_side(self, win, page):
        img = bytearray()
        seq = 0
        start = last = time.monotonic()

        def add(d):
            nonlocal last
            if len(d) >= 2 and d[0] != page & 0xFF:
                log.debug("READ window %02X: page ID %d in the reply, %d expected", win, d[0], page & 0xFF)
            if len(img) + len(d) - 2 > self.max_side_bytes:
                raise ProtocolError("side larger than %d bytes" % self.max_side_bytes)
            if len(d) > 2:                         # progress only when image bytes come
                img.extend(memoryview(d)[2:])
                last = time.monotonic()

        while True:
            if self.abort is not None and self.abort():
                raise Stopped("READ window %02X page %d: stopped by the caller" % (win, page))
            now = time.monotonic()
            if now - last > self.data_timeout:
                raise ScanTimeout("READ window %02X page %d: no data for %d s" % (win, page, self.data_timeout))
            if now - start > self.side_timeout:
                raise ScanTimeout("READ window %02X page %d: side not complete in %d s" % (win, page,
                                                                                          self.side_timeout))
            cdb = [0x28, 0, 0, 0, 0, win, 0x30, 0, 0, 0, page & 0xFF, seq & 0xFF]
            scsi, d = self.cmd(cdb, inlen=READ_SIZE)
            if scsi == 8:                          # busy: send the same READ again
                time.sleep(0.05)
                continue
            if scsi == 0:
                add(d)
                seq += 1
                continue
            if scsi != 2:
                raise ProtocolError("READ: SCSI status %d" % scsi)
            sense = self.request_sense()
            if sense.temporary_no_data:            # drop the data, wait, same READ
                time.sleep(self.poll)
                continue
            if sense.key == 0 and sense.eom:       # end of side; this reply has data
                add(d)
                return bytes(img)
            if sense.key == 0:                     # not an error: keep the data
                add(d)
                seq += 1
                continue
            raise sense_error("READ", sense)

    def _read_mutual(self, pages):
        """Read the two sides of one sheet with mutual READs (long page mode).

        PAGES: {window: page ID}. Return {window: (image bytearray, read info)}.
        ScanSnap Home, getImageDataForMutualTransfer 0x241d4 [C]: in each step one
        READ for window 0x00, then one READ for window 0x80, with one sequence ID
        for the two. Each READ asks for MUTUAL_READ_SIZE bytes. A side ends with
        CHECK CONDITION, sense key 0 and EOM (ILI is not examined). After that, only
        the other side gets READs. The last chunk of a side has (MUTUAL_READ_SIZE - 2)
        - (sense information) image bytes (0x24404-0x24478) [C]. If that cut removes
        the JPEG end marker, all data stays (the scanner value is not verified [NV]).
        Busy and "temporary no data" give the same READ again, as in _read_side.
        An error sense gets the attribute "partial": True if image data came.
        """
        bufs = {w: bytearray() for w in pages}
        info = {w: dict(mode="mutual", reads=0, chunks=0, bytes=0, eom_info=None, cut=0) for w in pages}
        done = set()
        seq = 0
        start = last = time.monotonic()
        body_max = MUTUAL_READ_SIZE - IMAGE_PREFIX
        size = [(MUTUAL_READ_SIZE >> 16) & 0xFF, (MUTUAL_READ_SIZE >> 8) & 0xFF, MUTUAL_READ_SIZE & 0xFF]

        def add(win, d, sense=None):
            nonlocal last
            page = pages[win]
            if len(d) >= IMAGE_PREFIX and d[0] != page & 0xFF:
                log.debug("READ window %02X: page ID %d in the reply, %d expected", win, d[0], page & 0xFF)
            body = memoryview(d)[IMAGE_PREFIX:] if len(d) > IMAGE_PREFIX else memoryview(b"")
            if sense is not None:
                info[win]["eom_info"] = sense.info
                valid = body_max - sense.info if sense.info <= body_max else 0
                if valid < len(body):
                    if bytes(body[-2:]) == b"\xff\xd9" and bytes(body[max(0, valid - 2):valid]) != b"\xff\xd9":
                        log.warning("READ window %02X page %d: the residue %d would cut the JPEG end; "
                                    "all %d bytes kept", win, page, sense.info, len(body))
                    else:
                        info[win]["cut"] = len(body) - valid
                        body = body[:valid]
            if len(bufs[win]) + len(body) > self.max_side_bytes:
                raise ProtocolError("side larger than %d bytes" % self.max_side_bytes)
            if len(body):
                bufs[win].extend(body)
                info[win]["chunks"] += 1
                last = time.monotonic()

        while len(done) < len(pages):
            for win in sorted(pages):
                if win in done:
                    continue
                page = pages[win]
                while True:
                    if self.abort is not None and self.abort():
                        raise Stopped("READ window %02X page %d: stopped by the caller" % (win, page))
                    now = time.monotonic()
                    if now - last > self.data_timeout:
                        raise ScanTimeout("mutual READ page %d: no data for %d s" % (page, self.data_timeout))
                    if now - start > self.side_timeout:
                        raise ScanTimeout("mutual READ page %d: sheet not complete in %d s" % (page,
                                                                                               self.side_timeout))
                    cdb = [0x28, 0, 0, 0, 0, win] + size + [0, page & 0xFF, seq & 0xFF]
                    scsi, d = self.cmd(cdb, inlen=MUTUAL_READ_SIZE)
                    info[win]["reads"] += 1
                    if scsi == 8:                      # busy: send the same READ again
                        time.sleep(0.05)
                        continue
                    if scsi == 0:
                        add(win, d)
                        break
                    if scsi != 2:
                        raise ProtocolError("READ: SCSI status %d" % scsi)
                    sense = self.request_sense()
                    if sense.temporary_no_data:        # drop the data, wait, same READ
                        time.sleep(self.poll)
                        continue
                    if sense.key == 0 and sense.eom:   # end of this side; this reply has data
                        add(win, d, sense)
                        done.add(win)
                        log.debug("READ window %02X page %d: end of side, %d bytes, %d READs (mutual), "
                                  "EOM information %d, %d bytes cut", win, page, len(bufs[win]),
                                  info[win]["reads"], sense.info, info[win]["cut"])
                        break
                    if sense.key == 0:                 # not an error: keep the data
                        add(win, d)
                        break
                    err = sense_error("READ", sense)
                    err.partial = bool(done) or any(len(b) for b in bufs.values())
                    raise err
            seq += 1
        for win in pages:
            info[win]["bytes"] = len(bufs[win])
        return {win: (bufs[win], info[win]) for win in pages}

    def _mutual_sheet(self, sheet, page, settings):
        """Long page mode: read one sheet (two sides) with mutual READs. Return the list of 2 Side."""
        pages = {0x00: page, 0x80: page + 1}
        data = self._read_mutual(pages)
        sides = []
        for side, win in (("front", 0x00), ("back", 0x80)):
            buf, rinfo = data.pop(win)
            # ScanSnap Home reads the pixel size one time, for the last window [C]. This
            # service reads it for each side, as in the normal flow [NV in long page mode].
            width, lines = self._pixel_size(win, pages[win])
            if lines:
                jpeg.fix_height_in_place(buf, lines, max_lines=settings.max_lines)
            rinfo.update(width=width, lines=lines)
            img, buf = bytes(buf), None              # one copy: a long side can be large
            sides.append(Side(sheet=sheet, side=side, page_id=pages[win] & 0xFF, jpeg=img, width=width,
                              lines=lines, dpi=settings.dpi, read=rinfo))
            img = None
        return sides

    def _pixel_size(self, win, page):
        """Return (width, real lines) of the last side, or (0, 0)."""
        scsi, ps = self.cmd([0x28, 0, 0x80, 0, 0, win, 0, 0, 0x20, 0, page & 0xFF, 0], inlen=0x20)
        if scsi == 2:
            self.request_sense()
            return 0, 0
        if len(ps) == 0x22:
            ps = ps[2:]
        if scsi != 0 or len(ps) < 16:
            return 0, 0
        width, _padded, _pw, lines = struct.unpack_from(">IIII", ps, 0)
        return width, lines

    def _cancel_and_flush(self, timeout=None, use_abort=True):
        """Cancel a job with sheets left: D8, then E0, then wait for an empty paper path.

        Measured on the iX1300 (PROTOCOL.md 8.6): after D8 the motor stops at once,
        and a READ gives sense 5/24/00. D8 and D6 alone leave paper stuck in the path.
        E0 after D8 makes the scanner feed ALL remaining sheets out without a scan.
        So there is no clean stop after N sheets. This function sends no READ after D8.
        It ignores the sense of E0, except "no paper" (nothing to feed out), a jam and an
        open cover (the paper cannot move). The wait stops when C2 shows an empty chute,
        no paper at the feed sensor and no paper at the exit sensor (or flush_exit_wait
        later: the exit bit can stay set), after TIMEOUT, or (with USE_ABORT) when
        self.abort() gives True. The caller sends D6.
        Return True if the scanner possibly fed out sheets without a scan.
        """
        timeout = self.flush_timeout if timeout is None else timeout
        try:
            scsi, _ = self.cmd([0xD8, 0, 0, 0, 0, 0])
            if scsi == 2:
                self.request_sense()
        except (OSError, IxError) as e:
            log.warning("D8 cancel failed: %s", e)
            return False
        try:
            scsi, _ = self.cmd([0xE0, 0, 0, 0, 0, 0])
            if scsi == 2:
                err = sense_error("E0 after D8", self.request_sense())
                if isinstance(err, NoPaper):
                    return False
                if isinstance(err, (PaperJam, CoverOpen)):
                    log.warning("cancel: %s; remove the paper by hand", err)
                    return True
                log.debug("cancel: %s (ignored)", err)
        except (OSError, IxError) as e:
            log.warning("E0 after D8 failed: %s", e)
            return True
        start = time.monotonic()
        clear_at = None
        while True:
            try:
                st = self.hw_status()
            except (OSError, IxError) as e:
                log.warning("cancel: C2 failed: %s", e)
                return True
            now = time.monotonic()
            if st.hopper_empty and not st.paper_at_feed:
                if not st.paper_at_exit:
                    log.debug("cancel: the paper path is empty after %.1f s", now - start)
                    return True
                if clear_at is None:
                    clear_at = now
                elif now - clear_at >= self.flush_exit_wait:
                    log.debug("cancel: the exit sensor stays set")
                    return True
            else:
                clear_at = None
            if now - start >= timeout or (use_abort and self.abort is not None and self.abort()):
                log.warning("cancel: the paper path is not empty after %.0f s; open the ADF top section "
                            "and remove the paper", now - start)
                return True
            time.sleep(self.flush_poll)

    def _flush_warning(self, result):
        msg = ("scan stopped after %d sheets: the other sheets in the scanner (if any) may have been "
               "ejected without a scan" % result.sheets)
        result.warnings.append(msg)
        log.warning(msg)

    def _end_check(self, result):
        """After "no paper": a set exit sensor bit (C2 byte 3 bit 0) means a probable jam.

        Over Wi-Fi a jam can end a page with a normal EOM [HW]. Use this test
        only here: during the job, the bit is also set by the next sheet.
        """
        try:
            st = self.hw_status()
        except (OSError, IxError) as e:
            msg = "after sheet %d: jam status not known (C2 failed: %s)" % (result.sheets, e)
            result.warnings.append(msg)
            log.warning(msg)
            return
        if st.paper_at_exit:
            result.jam_warning = True
            msg = ("after sheet %d: no paper in the hopper, but paper stays at the exit sensor; "
                   "probable paper jam" % result.sheets)
            result.warnings.append(msg)
            log.warning(msg)

    def scan(self, settings, on_sheet=None, cancel=None):
        """Scan all sheets in the hopper (duplex). Return a ScanResult.

        A sheet counts only when both sides ended with EOM. The sides of an
        incomplete sheet are dropped. ON_SHEET(list of 2 Side) gets each
        complete sheet at once; then result.sides stays empty (the caller keeps
        the data). A SenseError (and any other IxError) keeps the complete
        sheets in its "result" field. CANCEL() is examined before each sheet:
        if it gives True, the job stops with D8, E0 and D6. Then the scanner
        feeds the rest of the stack out without a scan (PROTOCOL.md 8.6).
        The caller must have a reserve.
        """
        result = ScanResult()
        if settings.long_page:
            log.info("long page mode: E9 length 0x%X, D4 byte 13 = 0x%02X, mutual READ of 0x%X bytes",
                     LONG_PREREAD_LENGTH, LONG_D4_MUTUAL, MUTUAL_READ_SIZE)
        self._check("D8", [0xD8, 0, 0, 0, 0, 0])
        self._check("E9", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], out=settings.e9_data())
        self._check("D4", [0xD4, 0, 0, 0, 0x50, 0], out=settings.d4_data())
        try:
            if self.hw_status().hopper_empty:
                log.debug("C2 shows an empty hopper before the job (E0 decides)")
        except ProtocolError as e:                  # only information here: E0 decides
            log.debug("C2 before the job: %s", e)
        page = 0
        # D5 is in the try block: if its reply is lost, the scanner can have a job. D6 then ends it.
        self.job_state = "uncertain"
        try:
            self._check("D5", [0xD5, 0, 0, 0, 0x08, 0x08], out=bytes(8), inlen=8)
            self.job_state = "active"
            while True:
                if cancel is not None and result.sheets and cancel():
                    log.warning("job stopped after %d sheets (service stop)", result.sheets)
                    result.warnings.append("scan stopped after %d sheets: service stop" % result.sheets)
                    result.cancelled = True
                    if self._cancel_and_flush():
                        self._flush_warning(result)
                    break
                # Always ask for the next sheet. The hopper bit in C2 is not a valid
                # end test: the scanner pulls the next sheets into the paper path
                # before we read them, and then C2 shows an empty hopper [HW].
                try:
                    self._check("E0", [0xE0, 0, 0, 0, 0, 0])
                except NoPaper:
                    if result.sheets == 0:
                        raise
                    self._end_check(result)
                    break
                sides = []
                try:
                    if settings.long_page:
                        sides = self._mutual_sheet(result.sheets, page, settings)
                    else:
                        for side, win, pid in (("front", 0x00, page), ("back", 0x80, page + 1)):
                            data = self._read_side(win, pid)
                            width, lines = self._pixel_size(win, pid)
                            if lines:
                                data = jpeg.fix_height(data, lines)
                            sides.append(Side(sheet=result.sheets, side=side, page_id=pid & 0xFF, jpeg=data,
                                              width=width, lines=lines, dpi=settings.dpi))
                            data = None
                except NoPaper as e:
                    if sides or getattr(e, "partial", False):   # no paper in the middle of a sheet: error
                        raise PaperLost(e.where, e.sense)
                    if result.sheets == 0:
                        raise
                    self._end_check(result)               # the first READ of the sheet: normal end
                    break
                except IxError:
                    if sides:
                        log.warning("sheet %d: incomplete sheet dropped", result.sheets + 1)
                    raise
                result.sheets += 1
                page += 2
                if on_sheet:
                    on_sheet(sides)
                else:
                    result.sides.extend(sides)
                sides = None
        except IxError as e:
            e.result = result
            if isinstance(e, Stopped) and self.job_state == "active":
                # D8 and D6 alone leave paper stuck in the path [HW]. The stop deadline is
                # near: wait at most stop_flush_wait s for the eject.
                if self._cancel_and_flush(timeout=min(self.flush_timeout, self.stop_flush_wait),
                                          use_abort=False):
                    self._flush_warning(result)
            raise
        except OSError as e:
            e.result = result
            raise
        finally:
            self._end_job()
        return result

    def _end_job(self):
        """Send D6. Set job_state to "ended", or to "uncertain" if D6 fails."""
        try:
            scsi, _ = self.cmd([0xD6, 0, 0, 0, 0, 0])
            if scsi == 2:
                sense = self.request_sense()
                if sense.key != 0:
                    raise sense_error("D6", sense)
            elif scsi != 0:
                raise ProtocolError("D6: SCSI status %d" % scsi)
            self.job_state = "ended"
        except (OSError, IxError) as e:
            self.job_state = "uncertain"
            log.warning("D6 end job failed: %s", e)
