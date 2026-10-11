#!/usr/bin/env python3
"""Raw USB access to the ScanSnap iX1300 (04c5:162c) through libusb-1.0 (ctypes).

The tool sends the 31-byte 0x43 command block, the data phase, and reads the
13-byte 'S' status block. Refer to PROTOCOL.md sections 2 and 3.

Usage (as root on Linux, scanner on USB and turned on): ixusb.py [-v] TEST ...
  tur, inq, vpd, hw, sense        simple SCSI commands
  hostid                          show the registered host ID
  reghostid:16HEX                 register a host ID
  ncmd:OPE:SIZE[:HEXDATA][:pad]   one SETUP NET CMD (for example ncmd:0x7:0x230)
  diag:TAG:RLEN                   send a 16-byte tag, read RLEN bytes
  raw:HEXOUT:RLEN                 SEND DIAGNOSTIC with HEXOUT, then read RLEN
  watch:SECONDS                   show the changes of GET HW STATUS
  tur17, inq17                    the CDB at offset 17 (this gives no data)
The service has the same setup functions: "ix1300 usb-register-host".
"""
import ctypes as C
import struct
import sys
import time

VID, PID = 0x04C5, 0x162C
EP_OUT, EP_IN = 0x02, 0x81

lib = C.CDLL("libusb-1.0.so.0")
lib.libusb_open_device_with_vid_pid.restype = C.c_void_p
lib.libusb_open_device_with_vid_pid.argtypes = [C.c_void_p, C.c_uint16, C.c_uint16]
lib.libusb_bulk_transfer.argtypes = [C.c_void_p, C.c_ubyte, C.c_void_p, C.c_int,
                                     C.POINTER(C.c_int), C.c_uint]
lib.libusb_claim_interface.argtypes = [C.c_void_p, C.c_int]
lib.libusb_release_interface.argtypes = [C.c_void_p, C.c_int]
lib.libusb_close.argtypes = [C.c_void_p]
lib.libusb_clear_halt.argtypes = [C.c_void_p, C.c_ubyte]
lib.libusb_reset_device.argtypes = [C.c_void_p]


class ScsiError(Exception):
    pass


def hexs(b, n=64):
    s = b[:n].hex(" ")
    return s + (" ..." if len(b) > n else "")


_CTX = None


def _ctx():
    global _CTX
    if _CTX is None:
        _CTX = C.c_void_p()
        lib.libusb_init(C.byref(_CTX))
    return _CTX


class Dev:
    def __init__(self, pid=PID):
        self.ctx = _ctx()
        self.h = lib.libusb_open_device_with_vid_pid(self.ctx, VID, pid)
        if not self.h:
            raise SystemExit("device %04x:%04x not found" % (VID, pid))
        r = lib.libusb_claim_interface(self.h, 0)
        if r:
            raise SystemExit("claim_interface failed %d" % r)
        self.verbose = False

    def close(self):
        lib.libusb_release_interface(self.h, 0)
        lib.libusb_close(self.h)

    def _bulk(self, ep, buf, n, tmo):
        done = C.c_int(0)
        r = lib.libusb_bulk_transfer(self.h, ep, buf, n, C.byref(done), tmo)
        return r, done.value

    def write(self, data, tmo=10000):
        buf = C.create_string_buffer(bytes(data), len(data))
        r, n = self._bulk(EP_OUT, buf, len(data), tmo)
        if r or n != len(data):
            raise IOError("bulk out r=%d n=%d/%d" % (r, n, len(data)))

    def read(self, n, tmo=10000):
        buf = C.create_string_buffer(n)
        r, got = self._bulk(EP_IN, buf, n, tmo)
        if r and not got:
            raise IOError("bulk in r=%d" % r)
        return buf.raw[:got]

    def cmd(self, cdb, out=b"", inlen=0, offset=19, tmo=10000, raw_status=False):
        """Send one command. Return (data, status_byte)."""
        blk = bytearray(31)
        blk[0] = 0x43
        blk[offset:offset + len(cdb)] = bytes(cdb)
        if self.verbose:
            print(" >cmd", hexs(bytes(cdb)), "out=%d in=%d" % (len(out), inlen))
        self.write(blk, tmo)
        data = b""
        if out:
            self.write(out, tmo)
        if inlen:
            data = self.read(inlen, tmo)
            # A short data block can be followed by the status block.
        st = self.read(13, tmo)
        if len(st) != 13 or st[0] != 0x53:
            # Data was shorter than the request; this block can be status.
            if inlen and len(data) == 13 and data[0] == 0x53 and len(st) != 13:
                raise IOError("protocol: status in data phase %s" % hexs(data))
            raise IOError("bad status block %s" % hexs(st))
        status = st[9] & 0x3E
        if self.verbose:
            print(" <st", hexs(st), "data", len(data), hexs(data, 32))
        return data, status

    # --- SCSI dialect ---
    def tur(self, **kw):
        return self.cmd([0, 0, 0, 0, 0, 0], **kw)[1]

    def sense(self):
        d, s = self.cmd([0x03, 0, 0, 0, 18, 0], inlen=18)
        return d

    @staticmethod
    def decode_sense(d):
        if len(d) < 14:
            return "short sense " + hexs(d)
        return "key=%x asc=%02x ascq=%02x eom=%d ili=%d info=%s" % (
            d[2] & 0x0F, d[12], d[13], (d[2] >> 6) & 1, (d[2] >> 5) & 1, d[3:7].hex())

    def checked(self, cdb, out=b"", inlen=0, **kw):
        d, s = self.cmd(cdb, out, inlen, **kw)
        if s:
            sd = self.sense()
            raise ScsiError("status %02x sense %s" % (s, self.decode_sense(sd)))
        return d

    def inquiry(self, vpd=False, n=96):
        cdb = [0x12, 1 if vpd else 0, 0xF0 if vpd else 0, 0, n, 0]
        return self.checked(cdb, inlen=n)

    def hw_status(self):
        return self.checked([0xC2, 0, 0, 0, 0, 0, 0, 0, 12, 0], inlen=12)

    def send_diag(self, payload):
        n = len(payload)
        return self.checked([0x1D, 0, 0, n >> 8, n & 0xFF, 0], out=payload)

    def recv_diag(self, n):
        return self.checked([0x1C, 0, 0, n >> 8, n & 0xFF, 0], inlen=n)

    def netcmd(self, ope, size, data=b"", pad=False, tag="SETUP NET CMD   ", tmo=30.0):
        """SETUP NET CMD: send once, then poll RECEIVE DIAGNOSTIC until byte0 != 1."""
        body = data if not pad else data.ljust(size, b"\0")
        p = tag.encode()[:16] + struct.pack(">HH", size + 0x10, 0) + \
            struct.pack("<IIII", ope, 0, 0, size) + body
        self.send_diag(p)
        t0 = time.time(); n = 0
        while True:
            x = self.recv_diag(size + 0x14)
            n += 1
            if x[0] != 0x01 or time.time() - t0 > tmo:
                break
            time.sleep(0.5)
        if self.verbose:
            print(" netcmd polls=%d %.1fs" % (n, time.time() - t0))
        return x

    def diag(self, tag, extra=b"", rlen=0):
        """Send one 16-byte ASCII tag (with optional extra bytes), then read rlen."""
        t = tag.encode().ljust(16, b" ")[:16]
        self.send_diag(t + extra)
        if rlen:
            return self.recv_diag(rlen)
        return b""


def main():
    d = Dev()
    d.verbose = "-v" in sys.argv
    try:
        for t in sys.argv[1:]:
            if t == "-v":
                continue
            print("==", t)
            try:
                if t == "tur":
                    print("status", d.tur())
                elif t == "tur17":
                    print("status (cdb at offset 17)", d.tur(offset=17))
                elif t == "inq17":
                    data, s = d.cmd([0x12, 0, 0, 0, 96, 0], inlen=96, offset=17)
                    print("status", s, hexs(data))
                elif t == "inq":
                    x = d.inquiry()
                    print(hexs(x, 96)); print(repr(x[8:48]))
                elif t == "vpd":
                    x = d.inquiry(True, 204)
                    print(hexs(x, 204))
                elif t == "hw":
                    x = d.hw_status()
                    print(hexs(x))
                elif t == "sense":
                    print(d.decode_sense(d.sense()))
                elif t.startswith("raw:"):
                    # raw:HEXOUT:rlen  send hex payload, then read rlen
                    _, hx, rlen = t.split(":")
                    d.send_diag(bytes.fromhex(hx))
                    if int(rlen, 0):
                        x = d.recv_diag(int(rlen, 0))
                        print(hexs(x, 1024)); print(repr(x))
                elif t.startswith("ncmd:"):
                    # ncmd:OPE:size[:hexdata][:pad]
                    f = t.split(":")
                    ope, size = int(f[1], 0), int(f[2], 0)
                    data = bytes.fromhex(f[3]) if len(f) > 3 and f[3] else b""
                    x = d.netcmd(ope, size, data, pad=len(f) > 4)
                    print("flag=%02x result=%08x" % (x[0], struct.unpack_from("<I", x, 8)[0]))
                    print(hexs(x, 4096)); print(repr(x[0x14:]))
                elif t.startswith("watch:"):
                    secs = float(t.split(":")[1]); last = None; t0 = time.time()
                    while time.time() - t0 < secs:
                        x = d.hw_status()
                        if x != last:
                            print("%6.2f" % (time.time() - t0), hexs(x)); last = x
                        time.sleep(0.2)
                elif t == "hostid":
                    x = d.checked([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30)
                    print(hexs(x, 48)); print("registered host id:", x[0x20:0x28].hex())
                elif t.startswith("reghostid:"):
                    # reghostid:16HEX  (SETUP NET START, CLR if needed, REG, SETUP NET END)
                    hid = bytes.fromhex(t.split(":")[1]); assert len(hid) == 8
                    old = d.checked([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30)[0x20:0x28]
                    print("old id", old.hex())
                    print("start", d.diag("SETUP NET START", rlen=1).hex())
                    try:
                        if old != bytes(8):
                            print("clear", d.diag("CLR HOSTID ADATA", rlen=2).hex())
                        print("reg", d.diag("REG HOSTID DATA", hid + bytes(8), rlen=2).hex())
                    finally:
                        print("end", d.diag("SETUP NET END", rlen=1).hex())
                    new = d.checked([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30)[0x20:0x28]
                    print("new id", new.hex(), "OK" if new == hid else "MISMATCH")
                elif t.startswith("diag:"):
                    # diag:TAG:rlen
                    _, tag, rlen = t.split(":")
                    x = d.diag(tag, rlen=int(rlen, 0))
                    print(hexs(x, 256)); print(repr(x))
                else:
                    print("unknown test")
            except (ScsiError, IOError) as e:
                print("ERROR", e)
    finally:
        d.close()


if __name__ == "__main__":
    main()
