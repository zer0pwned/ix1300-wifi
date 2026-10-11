"""Fake ScanSnap iX1300 for the tests (127.0.0.1 or an in-memory network).

It implements: discovery and keepalive (UDP), reserve and release (Port2),
Port1 frames for 12, C2, D8, E9, D4, D5, E0, D6, 03 and 28 (image and pixel
size), "temporary no data", busy, EOM, padded JPEG height, a multi-sheet
hopper, jams and button trigger packets.

Host ID registration over Wi-Fi [NV] (re/hostid-registration.md 3): one
registered host ID; a reserve with another ID gives -6, but a reserve with
mode 2 ("Wi-Fi tool") accepts each ID (accept_mode2). With a mode-2 reserve,
Port1 DC (HostIDOperation) works: op 0 register, op 1 get data, op 3 clear,
op 4 get length. The emulator decides these points (not measured): op 0 with
a registered ID gives result 0x01 (the host must clear first, as ScanSnap
Home does); DC without a mode-2 reserve gives result 0x18; an unknown op
gives result 0x02.

D8 in a job (measured, PROTOCOL.md 8.6): the motor stops at once, and a
READ gives sense 5/24/00. E0 after D8 feeds ALL remaining sheets out
without a scan (C2 shows paper for eject_time seconds per sheet). D6 after
D8 without E0 does not eject: the paper stays in the path and the chute.
"""
import socket
import struct
import threading
import time
from dataclasses import dataclass
from typing import Optional

import support  # noqa: F401  (path setup)
import jpegsynth
from ix1300.protocol import KEY, WINDOW_WIDTH

REGISTERED_ID = bytes.fromhex("0123456789ABCDEF")
NO_ID = b"\xff" * 8                   # C2 0x20..0x27 when no host ID is registered
EXEMPT = (0x12, 0xC2, 0xE7, 0xD9, 0xED, 0xE2, 0xE3, 0xE4, 0xE5)


@dataclass
class Sheet:
    front: str = "blank"          # "blank" or "text"
    back: str = "text"
    length_mm: float = 297.0
    jam_mm: Optional[float] = None   # Wi-Fi jam: the side ends early with a normal EOM
    jam_sense: bool = False          # jam with sense 3/80/01 in the back side
    double_feed: object = False      # sense 3/80/07: True or "front" (front side), "back" (after front EOM)
    paper_lost: bool = False         # sense 3/80/03 in the back side (paper gone in the middle of a sheet)


def _recv_exact(sk, n):
    buf = bytearray()
    while len(buf) < n:
        chunk = sk.recv(n - len(buf))
        if not chunk:
            raise ConnectionError("closed")
        buf += chunk
    return bytes(buf)


def sense_bytes(key, asc=0, ascq=0, eom=False, ili=False):
    s = bytearray(18)
    s[0] = 0xF0
    s[2] = key | (0x40 if eom else 0) | (0x20 if ili else 0)
    s[7] = 10
    s[12] = asc
    s[13] = ascq
    return bytes(s)


class Emulator:
    _cache = {}                        # generated JPEGs, shared by all instances
    def __init__(self, net, search_port=0, port1=0, port2=0, registered_id=REGISTERED_ID,
                 name="iX1300-EMU0000001", serial="EMU0000001", mac="1c:bc:ec:00:00:01",
                 chunk_size=60000, temp_no_data=1, busy=0, psize_prefix=True, trigger_interval=0.02):
        self.net = net
        self.host = getattr(net, "host", "127.0.0.1")
        self.ports = (search_port, port1, port2)
        self.registered_id = registered_id
        self.name, self.serial = name, serial
        self.mac = bytes.fromhex(mac.replace(":", ""))
        self.chunk_size = chunk_size
        self.temp_no_data = temp_no_data
        self.busy = busy
        self.psize_prefix = psize_prefix
        self.trigger_interval = trigger_interval
        self.lock = threading.RLock()
        self.stop_event = threading.Event()
        self.threads = []
        self.hopper = []
        self.owner = None              # dict: mac, host_id, peer, trigger_port, timeout
        self.occupied_by_other = False
        self.devstatus = 0             # search reply +38 (2 = busy with USB or a stale job)
        self.drop_search = 0           # drop this number of search requests (busy scanner)
        self.drop_keepalive = 0        # drop this number of keepalive requests (Wi-Fi power save)
        self.off = False               # powered off: no UDP replies, TCP connections closed
        self.cancelled = False         # D8 in a job: the motor stopped [HW]
        self.eject_time = 0.05         # seconds for each sheet that E0 after D8 feeds out
        self.eject_until = 0.0         # time.monotonic(): the end of the eject
        self.e0_cancel_sense = False   # E0 after D8: CHECK 5/24/00 (else GOOD; not measured)
        self.ejected = 0               # sheets fed out without a scan
        self.stuck = False             # paper stays in the path after D8 + D6
        self.c2_log = []               # (hopper bit "empty", sheets left) of each C2 reply
        self.prefetch = 2              # sheets in the paper path during a job (hopper bit shows empty)
        self.trigger_seq = 0
        self.exit_sensor = False
        self.button = 0
        self.log = []                  # (opcode, extra) of each Port1 command
        self.stats = {"temp_no_data": 0, "busy": 0, "reserve": 0, "release": 0, "keepalive": 0, "reads": 0,
                      "probe_off": 0, "tcp_off": 0, "keepalive_dropped": 0}
        self.fail_d6 = False           # D6 answers CHECK CONDITION (sense key 5)
        self.d5_close = False          # D5 starts the job, but the reply is lost (connection closed)
        self.job = None
        self.params = None
        self.sense = sense_bytes(0)
        self.cur = None                # current sheet state
        self.accept_mode2 = True       # a mode-2 reserve accepts an unregistered host ID [NV]
        self.dc_override = {}          # DC op -> data-in bytes (a test reply)
        self.dc_ignore_register = False  # DC op 0 gives result 0, but the ID does not change
        self.dc_log = []               # (op, data-out) of each DC command

    # --- life cycle ---
    def start(self):
        self.udp = self.net.udp(bind=(self.host, self.ports[0]))
        self.l1 = self.net.tcp_listen((self.host, self.ports[1]))
        self.l2 = self.net.tcp_listen((self.host, self.ports[2]))
        self.search_port = self.udp.getsockname()[1]
        self.port1 = self.l1.getsockname()[1]
        self.port2 = self.l2.getsockname()[1]
        for fn, arg in ((self._udp_loop, None), (self._accept_loop, (self.l1, self._port1)),
                        (self._accept_loop, (self.l2, self._port2))):
            t = threading.Thread(target=fn, args=() if arg is None else arg, daemon=True)
            t.start()
            self.threads.append(t)
        return self

    def stop(self):
        self.stop_event.set()
        for s in (self.udp, self.l1, self.l2):
            try:
                s.close()
            except OSError:
                pass

    def __enter__(self):
        return self.start()

    def __exit__(self, *exc):
        self.stop()

    # --- test controls ---
    def load(self, *sheets):
        with self.lock:
            self.hopper.extend(sheets)
            self.exit_sensor = False

    def press(self, long=False, copies=3, code=1, interval=None, background=False):
        """Send a button event (COPIES packets with one sequence number, INTERVAL s apart)."""
        cond = bytes([0, 0, 0x20 if long else 0x01, 0])
        if background:
            t = threading.Thread(target=self.send_trigger, args=(code, cond, copies, interval), daemon=True)
            t.start()
            return t
        return self.send_trigger(code, cond, copies, interval)

    def send_trigger(self, code, cond=bytes(4), copies=3, interval=None, to=None):
        """Send a trigger packet to the owner (or to the owner data TO). Return its sequence number,
        or False if there is no owner."""
        with self.lock:
            owner = to or self.owner
            if owner is None:
                return False
            peer, port = owner["peer"], owner["trigger_port"]
            self.trigger_seq = self.trigger_seq % 255 + 1
            seq = self.trigger_seq
        pkt = bytearray(48)
        struct.pack_into(">I", pkt, 0, 0x30)
        pkt[4:8] = KEY
        struct.pack_into(">I", pkt, 8, code)
        pkt[12:16] = cond
        pkt[16] = seq
        sk = self.net.udp()
        try:
            for i in range(copies):
                sk.sendto(bytes(pkt), (peer, port))
                if i + 1 < copies:
                    time.sleep(self.trigger_interval if interval is None else interval)
        finally:
            sk.close()
        return seq

    def power_cycle(self):
        """Lose the reserve (as a power off and on), then send POWER ON to the last owner."""
        with self.lock:
            last = self.owner
            self.owner = None
            self.job = None
        if last:
            self.send_trigger(0x30, to=last)   # POWER ON goes to the last connection data

    def power_off(self):
        """Auto power-off: stop to answer, and send POWER OFF to the last owner."""
        with self.lock:
            last = self.owner
            self.owner = None
            self.job = None
            self.off = True
        if last:
            self.send_trigger(0x10, to=last)

    def power_on(self):
        """The user opens the cover. No trigger: no host has a reserve."""
        self.off = False

    def opcodes(self):
        return [op for op, _ in self.log]

    # --- UDP ---
    def _udp_loop(self):
        self.udp.settimeout(0.2)
        while not self.stop_event.is_set():
            try:
                data, src = self.udp.recvfrom(2048)
            except socket.timeout:
                continue
            except OSError:
                return
            if len(data) < 32 or data[:4] != KEY:
                continue
            if self.off:
                self.stats["probe_off"] += 1
                continue
            flag = struct.unpack_from(">I", data, 4)[0]
            mac64 = data[12:20]
            if flag == 0:
                if self.drop_search > 0:
                    self.drop_search -= 1
                    continue
                if 0x10 <= struct.unpack_from(">H", data, 24)[0] <= 0x1F:
                    self.udp.sendto(self._search_reply(mac64), src)
            elif flag == 1:
                if self.drop_keepalive > 0:
                    self.drop_keepalive -= 1
                    self.stats["keepalive_dropped"] += 1
                    continue
                owner = self.owner             # no lock: Port1 can hold it for a long time
                ok = owner is not None and owner["mac64"] == mac64
                if ok:
                    self.stats["keepalive"] += 1
                    self.udp.sendto(KEY + b"\x80\x01" + bytes(6), src)

    def _search_reply(self, mac64):
        r = bytearray(144)
        r[0:4] = KEY
        with self.lock:
            owner = self.owner
        struct.pack_into(">HHHHI", r, 4, 1 if owner and owner["mac64"] == mac64 else 0, 0, 0x10, 0x30, 0)
        r[16:20] = socket.inet_aton(self.host)
        struct.pack_into(">II", r, 20, self.port1, self.port2)
        r[28:34] = self.mac
        struct.pack_into(">H", r, 38, self.devstatus)
        r[40:40 + len(self.name)] = self.name.encode()
        r[104:120] = b"ScanSnap iX1300 "
        if owner:
            r[120:124] = socket.inet_aton(owner["peer"])
            r[124:132] = owner["host_id"]
        r[132:132 + len(self.serial)] = self.serial.encode()
        return bytes(r)

    # --- TCP ---
    def _accept_loop(self, lst, handler):
        lst.settimeout(0.2)
        while not self.stop_event.is_set():
            try:
                conn, peer = lst.accept()
            except socket.timeout:
                continue
            except OSError:
                return
            threading.Thread(target=self._serve, args=(conn, peer, handler), daemon=True).start()

    def _serve(self, conn, peer, handler):
        if self.off:
            self.stats["tcp_off"] += 1
            conn.close()
            return
        try:
            conn.settimeout(5)
            conn.sendall(struct.pack(">I", 16) + KEY + bytes(8))
            while not self.stop_event.is_set():
                try:
                    hdr = _recv_exact(conn, 4)
                except socket.timeout:
                    continue
                n = struct.unpack(">I", hdr)[0]
                msg = hdr + _recv_exact(conn, n - 4)
                if msg[4:8] != KEY:
                    return
                reply = handler(msg, peer)
                if reply is None:
                    return
                conn.sendall(reply)
        except (ConnectionError, OSError):
            pass
        finally:
            conn.close()

    # --- Port2 ---
    def _port2(self, m, peer):
        code = struct.unpack_from(">I", m, 8)[0]
        if code == 0x11:
            self.stats["reserve"] += 1
            res = self._reserve(m, peer)
            return struct.pack(">I", 20) + KEY + struct.pack(">iH", res, 0x10 if res != -1 else 0) + bytes(6)
        if code == 0x12:
            self.stats["release"] += 1
            with self.lock:
                if self.owner and self.owner["mac64"] == m[16:24]:
                    self.owner = None
                    self.job = None
                    res = 0
                else:
                    res = -1
            return struct.pack(">I", 16) + KEY + struct.pack(">i", res) + bytes(4)
        return None

    def _reserve(self, m, peer):
        if len(m) != 128 or m[120] not in (0, 1):
            return -1
        port = struct.unpack_from(">I", m, 48)[0]
        mac64 = m[16:24]
        if not 1 <= port <= 65535 or mac64 == bytes(8):
            return -1
        if not 0x10 <= struct.unpack_from(">H", m, 32)[0] <= 0x1F:
            return -5
        mode = struct.unpack_from(">I", m, 36)[0]
        if m[108:116] != self.registered_id and not (mode == 2 and self.accept_mode2):
            return -6
        with self.lock:
            if self.occupied_by_other or self.devstatus == 2 or (self.owner and self.owner["mac64"] != mac64):
                return -4
            self.owner = dict(mac64=mac64, host_id=m[108:116], peer=peer[0], trigger_port=port, timeout=m[34],
                              mode=mode)
        return 0

    # --- Port1 ---
    def _port1(self, m, peer):
        if len(m) < 64 or struct.unpack_from(">I", m, 8)[0] != 1:
            return self._reply(-1, 0)
        clen, inlen, outlen = struct.unpack_from(">III", m, 32)
        cdb = m[48:48 + clen]
        out = m[64:64 + outlen]
        op = cdb[0]
        with self.lock:
            owner_ok = self.owner is not None and self.owner["mac64"] == m[16:24]
        if op not in EXEMPT and not owner_ok:
            return None                        # the scanner closes the connection
        self.log.append((op, bytes(cdb)))
        with self.lock:
            return self._dispatch(op, cdb, out, inlen)

    def _reply(self, st, scsi, data=b""):
        return struct.pack(">I", 40 + len(data)) + KEY + struct.pack(">ii", st, scsi) + bytes(24) + data

    def _check(self, key, asc=0, ascq=0, eom=False, ili=False, data=b""):
        self.sense = sense_bytes(key, asc, ascq, eom, ili)
        return self._reply(0, 2, data)

    def _dispatch(self, op, cdb, out, inlen):
        if op == 0x12:
            inq = bytearray(96)
            inq[0] = 0x06
            inq[8:36] = b"FUJITSU ScanSnap iX1300 0J00"
            return self._reply(0, 0, bytes(inq[:inlen]))
        if op == 0xC2:
            st = bytearray(0x30)
            # Prefetch model [HW]: during a job the scanner pulls up to PREFETCH
            # sheets into the paper path. Then the hopper sensor shows "empty"
            # and the exit sensor shows paper, but these sheets still come.
            in_path = min(len(self.hopper), self.prefetch) if self.job is not None else 0
            if time.monotonic() < self.eject_until:
                st[2] |= 0x80                # E0 after D8: the sheets go out
                st[3] |= 0x01
            elif (self.cancelled or self.stuck) and self.hopper:
                st[2] |= 0x80                # D8 only: the paper stops in the path [HW]
                st[3] |= 0x01
            elif len(self.hopper) > in_path:
                st[2] |= 0x80
            else:
                st[3] |= 0x80
            if self.exit_sensor or in_path:
                st[3] |= 0x01
            self.c2_log.append((bool(st[3] & 0x80), len(self.hopper)))
            st[4] = self.button
            st[0x20:0x28] = self.registered_id
            return self._reply(0, 0, bytes(st[:inlen]))
        if op == 0x03:
            s, self.sense = self.sense, sense_bytes(0)
            return self._reply(0, 0, s[:inlen])
        if op == 0xD8:
            if self.job is not None:
                self.cancelled = True
            return self._reply(0, 0)
        if op == 0xE9:
            return self._reply(0, 0)
        if op == 0xD4:
            if len(out) != 0x50 or out[31] != 0x30:
                return self._check(5, 0x26, 0)
            self.params = self._params(out)
            return self._reply(0, 0)
        if op == 0xD5:
            self.job = dict(page=0)
            self.cancelled = False
            self.stuck = False
            if self.d5_close:
                return None
            return self._reply(0, 0, bytes(8)[:inlen])
        if op == 0xD6:
            if self.cancelled and self.hopper:
                self.stuck = True            # D6 does not eject after D8 [HW]
            self.cancelled = False
            self.job = None
            self.cur = None
            if self.fail_d6:
                return self._check(5, 0x2C, 0)
            return self._reply(0, 0)
        if op == 0xE0:
            if self.job is None or self.params is None:
                return self._check(5, 0x2C, 0)
            if self.cancelled:
                return self._flush()
            if not self.hopper:
                return self._check(3, 0x80, 0x03)
            sheet = self.hopper.pop(0)
            self.cur = self._prepare(sheet)
            return self._reply(0, 0)
        if op == 0x28:
            return self._read(cdb, inlen)
        if op == 0xDC:
            return self._hostid_op(cdb, out, inlen)
        return self._reply(0, 0)

    def has_id(self):
        return self.registered_id not in (NO_ID, bytes(8))

    def _hostid_op(self, cdb, out, inlen):
        """DC HostIDOperation: CDB DC 00 OP 00 00 WL 00 RL. Data-in byte 0 = result."""
        sub = cdb[2]
        self.dc_log.append((sub, bytes(out)))
        if sub in self.dc_override:
            return self._reply(0, 0, self.dc_override[sub][:inlen])
        res, data = 0, b""
        if self.owner.get("mode") != 2:
            res = 0x18
        elif sub == 0:
            if len(out) != 16 or out[8:] != bytes(8):
                res = 0x01
            elif self.has_id():
                res = 0x01
            elif not self.dc_ignore_register:
                self.registered_id = bytes(out[:8])
        elif sub == 3:
            self.registered_id = NO_ID
        elif sub == 4:
            data = struct.pack(">HH", 0x18 if self.has_id() else 0, 0)
        elif sub == 1:
            data = (self.registered_id if self.has_id() else bytes(8)) + bytes(16)
        else:
            res = 0x02
        if sub in (4, 1) and res == 0:
            return self._reply(0, 0, data[:inlen])
        return self._reply(0, 0, (bytes([res]) + bytes(max(0, inlen - 1)))[:inlen])

    def _flush(self):
        """E0 after D8: feed out the sheet in the path and all sheets in the chute, no scan."""
        n = len(self.hopper)
        if self.cur is not None and not all(sd["done"] for sd in self.cur["sides"].values()):
            n += 1
        self.hopper.clear()
        self.cur = None
        if not n:
            return self._check(3, 0x80, 0x03)
        self.ejected += n
        self.eject_until = time.monotonic() + self.eject_time * n
        if self.e0_cancel_sense:
            return self._check(5, 0x24, 0x00)
        return self._reply(0, 0)

    def path_clear(self):
        """True when no sheet is on its way out (the eject after D8 + E0 has ended)."""
        return time.monotonic() >= self.eject_until and not self.stuck

    @staticmethod
    def _params(d4):
        """The scan parameters in the data of a D4 command."""
        d = d4[32:]
        return dict(dpi=struct.unpack_from(">H", d, 2)[0], gray=d[6] == 0x02,
                    length=struct.unpack_from(">I", d, 14)[0], auto=d4[6] == 0xC1)

    def prerender(self, settings, *sheets):
        """Make the JPEGs of SHEETS (default: the sheets in the hopper) for the ScanSettings SETTINGS now.

        Else the emulator makes them in the E0 command. A slow test computer can then
        need more time than the command timeout of the client.
        """
        params = self._params(settings.d4_data())
        for sheet in sheets or list(self.hopper):
            self._prepare(sheet, params)

    def _jpeg(self, kind, width, height, lines, gray, dpi):
        key = (kind, width, height, lines, gray, dpi)
        if key not in self._cache:
            comps = 1 if gray or kind == "blank" else 3
            self._cache[key] = jpegsynth.synth(width, height, comps, content_rows=lines,
                                               density=0.0 if kind == "blank" else 0.5, dpi=dpi)
        return self._cache[key]

    def _prepare(self, sheet, params=None):
        p = params or self.params
        dpi = p["dpi"]
        width = WINDOW_WIDTH * dpi // 1200
        padded = p["length"] * dpi // 1200
        lines = min(padded, int(round(sheet.length_mm / 25.4 * dpi)))
        if sheet.jam_mm is not None:
            lines = min(lines, int(round(sheet.jam_mm / 25.4 * dpi)))
        sides = {}
        for win, kind in ((0x00, sheet.front), (0x80, sheet.back)):
            data = self._jpeg(kind, width, padded, lines, p["gray"], dpi)
            sides[win] = dict(data=data, pos=0, temp=self.temp_no_data, busy=self.busy, done=False)
        return dict(sheet=sheet, width=width, padded=padded, lines=lines, sides=sides)

    def _read(self, cdb, inlen):
        dtype, win, page, seq = cdb[2], cdb[5], cdb[10], cdb[11]
        cur = self.cur
        if self.cancelled:
            return self._check(5, 0x24, 0x00)    # READ after D8 [HW]
        if cur is None:
            return self._check(5, 0x2C, 0)
        if dtype == 0x80:
            ps = struct.pack(">IIIIHHII", cur["width"], cur["padded"], cur["width"], cur["lines"],
                             self.params["dpi"], self.params["dpi"], cur["width"], cur["lines"] + 7)
            ps = ps[:0x20].ljust(0x20, b"\0")
            if self.psize_prefix:
                ps = bytes([page, 0]) + ps
            return self._reply(0, 0, ps)
        if dtype != 0:
            return self._check(5, 0x24, 0)
        self.stats["reads"] += 1
        side = cur["sides"][win & 0x80]
        sheet = cur["sheet"]
        if side["busy"] > 0:
            side["busy"] -= 1
            self.stats["busy"] += 1
            return self._reply(0, 8)
        if side["temp"] > 0:
            side["temp"] -= 1
            self.stats["temp_no_data"] += 1
            return self._check(3, 0x80, 0x13, data=bytes([page, seq]) + b"\xde\xad")   # data must be dropped
        df = sheet.double_feed
        if df and ((win == 0x00 and df in (True, "front")) or (win == 0x80 and df == "back")):
            self.hopper.clear()
            return self._check(3, 0x80, 0x07)
        if win == 0x80 and sheet.paper_lost:
            self.hopper.clear()
            return self._check(3, 0x80, 0x03)
        if win == 0x80 and sheet.jam_sense and side["pos"] > 0:
            self.hopper.clear()
            self.exit_sensor = True
            return self._check(3, 0x80, 0x01)
        data = side["data"]
        n = min(self.chunk_size, max(1, inlen - 2))
        chunk = data[side["pos"]:side["pos"] + n]
        side["pos"] += len(chunk)
        payload = bytes([page, seq]) + chunk
        if side["pos"] >= len(data):
            side["done"] = True
            if win == 0x80 and sheet.jam_mm is not None:
                self.hopper.clear()
                self.exit_sensor = True
            return self._check(0, eom=True, ili=True, data=payload)
        return self._reply(0, 0, payload)
