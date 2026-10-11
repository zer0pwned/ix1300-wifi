"""One-time USB setup of the iX1300: host ID registration and Wi-Fi setup.

Uses libusb-1.0 through ctypes (Linux). The library is optional: only these
commands need it. The scanner must be on USB and turned on (cover open).
Refer to PROTOCOL.md sections 3 and 3.6.
"""
import ctypes
import ctypes.util
import logging
import struct
import time

from .protocol import pfu_encode

VID = 0x04C5
PIDS = (0x162C,)
EP_OUT, EP_IN = 0x02, 0x81


log = logging.getLogger("ix1300.usb")


class UsbError(Exception):
    pass


class UsbTimeout(UsbError):
    """The scanner stays busy (reply byte 0 = 0x01) after the time limit."""


def _lib():
    name = ctypes.util.find_library("usb-1.0") or "libusb-1.0.so.0"
    try:
        lib = ctypes.CDLL(name)
    except OSError as e:
        raise UsbError("libusb-1.0 not found (%s). Install libusb-1.0-0." % e)
    lib.libusb_init.argtypes = [ctypes.POINTER(ctypes.c_void_p)]
    lib.libusb_exit.argtypes = [ctypes.c_void_p]
    lib.libusb_exit.restype = None
    lib.libusb_open_device_with_vid_pid.restype = ctypes.c_void_p
    lib.libusb_open_device_with_vid_pid.argtypes = [ctypes.c_void_p, ctypes.c_uint16, ctypes.c_uint16]
    lib.libusb_bulk_transfer.argtypes = [ctypes.c_void_p, ctypes.c_ubyte, ctypes.c_void_p, ctypes.c_int,
                                         ctypes.POINTER(ctypes.c_int), ctypes.c_uint]
    lib.libusb_claim_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_release_interface.argtypes = [ctypes.c_void_p, ctypes.c_int]
    lib.libusb_close.argtypes = [ctypes.c_void_p]
    lib.libusb_set_auto_detach_kernel_driver.argtypes = [ctypes.c_void_p, ctypes.c_int]
    return lib


class UsbScanner:
    """SCSI commands in the 31-byte 0x43 block (CDB at offset 19), 13-byte 'S' status."""

    def __init__(self):
        self.lib = _lib()
        self.ctx = ctypes.c_void_p()
        self.h = None
        self.claimed = False
        if self.lib.libusb_init(ctypes.byref(self.ctx)):
            self.ctx = None
            raise UsbError("libusb_init failed")
        try:
            for pid in PIDS:
                self.h = self.lib.libusb_open_device_with_vid_pid(self.ctx, VID, pid)
                if self.h:
                    break
            if not self.h:
                raise UsbError("scanner %04x:%04x not found on USB (open the cover to turn it on)" % (VID, PIDS[0]))
            self.lib.libusb_set_auto_detach_kernel_driver(self.h, 1)
            r = self.lib.libusb_claim_interface(self.h, 0)
            if r:
                raise UsbError("claim interface failed (%d). Stop saned or other users of the scanner." % r)
            self.claimed = True
        except BaseException:
            self.close()
            raise

    def close(self):
        """Release the interface, the handle and the context (reverse order). A second call does nothing."""
        if self.h:
            if self.claimed:
                self.lib.libusb_release_interface(self.h, 0)
                self.claimed = False
            self.lib.libusb_close(self.h)
            self.h = None
        if self.ctx is not None:
            self.lib.libusb_exit(self.ctx)
            self.ctx = None

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.close()

    def _bulk(self, ep, buf, n, tmo):
        done = ctypes.c_int(0)
        r = self.lib.libusb_bulk_transfer(self.h, ep, buf, n, ctypes.byref(done), tmo)
        return r, done.value

    def _write(self, data, tmo=10000):
        buf = ctypes.create_string_buffer(bytes(data), len(data))
        r, n = self._bulk(EP_OUT, buf, len(data), tmo)
        if r or n != len(data):
            raise UsbError("bulk out failed r=%d n=%d/%d" % (r, n, len(data)))

    def _read(self, n, tmo=10000):
        buf = ctypes.create_string_buffer(n)
        r, got = self._bulk(EP_IN, buf, n, tmo)
        if r and not got:
            raise UsbError("bulk in failed r=%d" % r)
        return buf.raw[:got]

    def cmd(self, cdb, out=b"", inlen=0):
        blk = bytearray(31)
        blk[0] = 0x43
        blk[19:19 + len(cdb)] = bytes(cdb)
        self._write(blk)
        if out:
            self._write(out)
        data = self._read(inlen) if inlen else b""
        st = self._read(13)
        if len(st) != 13 or st[0] != 0x53:
            raise UsbError("bad status block %s" % st.hex(" "))
        status = st[9] & 0x3E
        if status:
            raise UsbError("command %02X: status %02x" % (cdb[0], status))
        return data

    def send_diag(self, payload):
        n = len(payload)
        self.cmd([0x1D, 0, 0, n >> 8, n & 0xFF, 0], out=payload)

    def recv_diag(self, n):
        return self.cmd([0x1C, 0, 0, n >> 8, n & 0xFF, 0], inlen=n)

    def diag(self, tag, extra=b"", rlen=0):
        self.send_diag(tag.encode().ljust(16, b" ")[:16] + extra)
        return self.recv_diag(rlen) if rlen else b""

    def netcmd(self, ope, size, data=b"", tmo=30.0, poll=0.5):
        """SETUP NET CMD: send one time, then read the reply until byte 0 is not 1 (busy) [HW].

        Do not send the command again. Return the reply. Raise UsbTimeout if
        the scanner is still busy after TMO s, and UsbError for a short reply.
        """
        body = data.ljust(size, b"\0") if data else b""
        self.send_diag(b"SETUP NET CMD   " + struct.pack(">HH", size + 0x10, 0) +
                       struct.pack("<IIII", ope, 0, 0, size) + body)
        end = time.monotonic() + tmo
        while True:
            x = self.recv_diag(size + 0x14)
            if len(x) < 12:
                raise UsbError("ope 0x%x: short reply (%d bytes)" % (ope, len(x)))
            if x[0] != 0x01:
                # A complete reply has the 20-byte header and the ope echo at +4 [HW].
                if len(x) < 0x14:
                    raise UsbError("ope 0x%x: short reply (%d bytes)" % (ope, len(x)))
                echo = struct.unpack_from("<I", x, 4)[0]
                if echo != ope:                     # not seen on the scanner: log only (no hardware rule yet)
                    log.warning("ope 0x%x: reply has ope 0x%x", ope, echo)
                return x
            if time.monotonic() >= end:
                raise UsbTimeout("ope 0x%x: scanner still busy after %.0f s" % (ope, tmo))
            time.sleep(poll)

    def host_id(self):
        """Return the registered host ID (8 bytes) from GET HW STATUS (0x30 bytes)."""
        return self.cmd([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30)[0x20:0x28]


def result(reply):
    """Return the LE32 result at +8 of a SETUP NET CMD reply. UsbError if the reply is short."""
    if len(reply) < 12:
        raise UsbError("short reply (%d bytes)" % len(reply))
    return struct.unpack_from("<I", reply, 8)[0]


def _ok2(dev, tag, extra=b""):
    """Send a tag with a 2-byte reply. UsbError if the reply is empty or byte 0 & 0x7F is not 0."""
    r = dev.diag(tag, extra, rlen=2)
    if not r or r[0] & 0x7F:
        raise UsbError("%s failed: %s" % (tag, r.hex() or "no reply"))


def _net_start(dev):
    r = dev.diag("SETUP NET START", rlen=1)
    if r[:1] != b"\0":
        raise UsbError("SETUP NET START refused (%s). Turn the scanner off and on." % (r.hex() or "no reply"))


def register_host_id(host_id, log=print):
    """Write HOST_ID (8 bytes) as the registered host. Return the ID that the scanner reports after."""
    if len(host_id) != 8:
        raise ValueError("host ID needs 8 bytes")
    with UsbScanner() as dev:
        old = dev.host_id()
        log("registered host ID now: %s" % old.hex().upper())
        if old == host_id:
            log("no change necessary")
            return old
        _net_start(dev)
        try:
            if old not in (bytes(8), b"\xff" * 8):
                _ok2(dev, "CLR HOSTID ADATA")
            _ok2(dev, "REG HOSTID DATA", host_id + bytes(8))
        finally:
            dev.diag("SETUP NET END", rlen=1)
        new = dev.host_id()
        log("registered host ID after: %s" % new.hex().upper())
        if new != host_id:
            raise UsbError("the scanner reports %s, not %s" % (new.hex(), host_id.hex()))
        return new


def wifi_data(ssid, key, auth="WPA2", cipher="AES"):
    """Ope 0x1006 data (248 bytes)."""
    b = bytearray(0xF8)
    s = ssid.encode()
    if len(s) > 32:
        raise ValueError("SSID is longer than 32 bytes")
    b[0:len(s)] = s
    b[0x24:0x24 + len(auth)] = auth.encode()
    b[0x2C:0x2C + len(cipher)] = cipher.encode()
    k = pfu_encode(key)
    if len(k) > 192:
        raise ValueError("key is too long")
    b[0x34:0x34 + len(k)] = k
    return bytes(b)


def wifi_setup(ssid, key, auth="WPA2", cipher="AES", wait=90, log=print):
    """Connect the scanner to a WPA/WPA2-PSK network and save client boot mode. Return the IP list."""
    with UsbScanner() as dev:
        _net_start(dev)
        ips = []
        try:
            x = dev.netcmd(0x1006, 0xF8, wifi_data(ssid, key, auth, cipher), tmo=60)
            res = result(x)
            if res:
                raise UsbError("Wi-Fi setting failed: result 0x%x%s" % (res, " (wrong key)" if res == 0x1000 else ""))
            x = dev.netcmd(0x1106, 4, b"\0\0\0\0")
            res = result(x)
            if res:
                raise UsbError("boot mode setting failed: result 0x%x" % res)
            t0 = time.monotonic()
            while time.monotonic() - t0 < wait:
                x = dev.netcmd(0x7, 0x230)
                ips = [s.decode("ascii", "replace") for s in x[0x1BC:0x200].split(b"\0") if s]
                ips = [i for i in ips if i and i != "0.0.0.0"]
                log("%5.1f s: IP %s" % (time.monotonic() - t0, ", ".join(ips) or "none"))
                if ips:
                    break
                time.sleep(5)
        finally:
            dev.diag("SETUP NET END", rlen=1)
        return ips
