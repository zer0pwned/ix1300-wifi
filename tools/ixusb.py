#!/usr/bin/env python3
"""Raw USB access to the ScanSnap iX1300 (04c5:162c) through libusb-1.0 (ctypes).

The tool sends the 31-byte 0x43 command block, the data phase, and reads the
13-byte 'S' status block. Refer to PROTOCOL.md sections 2 and 3.
This tool is a thin wrapper: the USB transport, SETUP NET CMD and the host ID
registration come from service/ix1300/usb.py (Python 3 only, no Docker).

Usage (as root on Linux, scanner on USB and turned on): ixusb.py [-v] TEST ...
  tur, inq, vpd, hw, sense        simple SCSI commands
  hostid                          show the registered host ID
  reghostid:16HEX                 register a host ID
  ncmd:OPE:SIZE[:HEXDATA]         one SETUP NET CMD (for example ncmd:0x7:0x230)
  diag:TAG:RLEN                   send a 16-byte tag, read RLEN bytes
  raw:HEXOUT:RLEN                 SEND DIAGNOSTIC with HEXOUT, then read RLEN
  watch:SECONDS                   show the changes of GET HW STATUS
  tur17, inq17                    the CDB at offset 17 (this gives no data)
The service has the same setup functions: "ix1300 usb-register-host".
"""
import logging
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service"))

from ix1300 import usb  # noqa: E402


def hexs(b, n=64):
    s = b[:n].hex(" ")
    return s + (" ..." if len(b) > n else "")


class Dev(usb.UsbScanner):
    """UsbScanner with a command that returns the SCSI status and does not raise for it."""

    verbose = False

    def xcmd(self, cdb, out=b"", inlen=0, offset=19):
        """Send one command with the CDB at OFFSET. Return (data, status byte)."""
        blk = bytearray(31)
        blk[0] = 0x43
        blk[offset:offset + len(cdb)] = bytes(cdb)
        if self.verbose:
            print(" >cmd", hexs(bytes(cdb)), "out=%d in=%d" % (len(out), inlen))
        self._write(blk)
        if out:
            self._write(out)
        data = self._read(inlen) if inlen else b""
        st = self._read(13)
        if len(st) != 13 or st[0] != 0x53:
            raise usb.UsbError("bad status block %s" % hexs(st))
        if self.verbose:
            print(" <st", hexs(st), "data", len(data), hexs(data, 32))
        return data, st[9] & 0x3E

    def sense(self):
        return self.xcmd([0x03, 0, 0, 0, 18, 0], inlen=18)[0]

    @staticmethod
    def decode_sense(d):
        if len(d) < 14:
            return "short sense " + hexs(d)
        return "key=%x asc=%02x ascq=%02x eom=%d ili=%d info=%s" % (
            d[2] & 0x0F, d[12], d[13], (d[2] >> 6) & 1, (d[2] >> 5) & 1, d[3:7].hex())

    def checked(self, cdb, out=b"", inlen=0):
        d, s = self.xcmd(cdb, out, inlen)
        if s:
            raise usb.UsbError("status %02x sense %s" % (s, self.decode_sense(self.sense())))
        return d

    def hw_status(self):
        return self.checked([0xC2, 0, 0, 0, 0, 0, 0, 0, 12, 0], inlen=12)


def run(d, t):
    if t == "tur":
        print("status", d.xcmd([0, 0, 0, 0, 0, 0])[1])
    elif t == "tur17":
        print("status (cdb at offset 17)", d.xcmd([0, 0, 0, 0, 0, 0], offset=17)[1])
    elif t == "inq17":
        data, s = d.xcmd([0x12, 0, 0, 0, 96, 0], inlen=96, offset=17)
        print("status", s, hexs(data))
    elif t == "inq":
        x = d.checked([0x12, 0, 0, 0, 96, 0], inlen=96)
        print(hexs(x, 96))
        print(repr(x[8:48]))
    elif t == "vpd":
        print(hexs(d.checked([0x12, 1, 0xF0, 0, 204, 0], inlen=204), 204))
    elif t == "hw":
        print(hexs(d.hw_status()))
    elif t == "sense":
        print(d.decode_sense(d.sense()))
    elif t.startswith("raw:"):
        _, hx, rlen = t.split(":")
        d.send_diag(bytes.fromhex(hx))
        if int(rlen, 0):
            x = d.recv_diag(int(rlen, 0))
            print(hexs(x, 1024))
            print(repr(x))
    elif t.startswith("ncmd:"):
        f = t.split(":")
        ope, size = int(f[1], 0), int(f[2], 0)
        data = bytes.fromhex(f[3]) if len(f) > 3 and f[3] else b""
        x = d.netcmd(ope, size, data)
        print("flag=%02x result=%08x" % (x[0], usb.result(x)))
        print(hexs(x, 4096))
        print(repr(x[0x14:]))
    elif t.startswith("watch:"):
        secs = float(t.split(":")[1])
        last = None
        t0 = time.monotonic()
        while time.monotonic() - t0 < secs:
            x = d.hw_status()
            if x != last:
                print("%6.2f" % (time.monotonic() - t0), hexs(x))
                last = x
            time.sleep(0.2)
    elif t == "hostid":
        print("registered host id:", d.host_id().hex())
    elif t.startswith("reghostid:"):
        # register_host_id opens its own handle: release this one first.
        hid = bytes.fromhex(t.split(":")[1])
        d.close()
        try:
            usb.register_host_id(hid)
        finally:
            d.__init__()
    elif t.startswith("diag:"):
        _, tag, rlen = t.split(":")
        x = d.diag(tag, rlen=int(rlen, 0))
        print(hexs(x, 256))
        print(repr(x))
    else:
        print("unknown test")


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    verbose = "-v" in argv
    logging.basicConfig(level=logging.DEBUG if verbose else logging.WARNING)
    try:
        d = Dev()
    except usb.UsbError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    d.verbose = verbose
    failed = False
    try:
        for t in argv:
            if t == "-v":
                continue
            print("==", t)
            try:
                run(d, t)
            except (usb.UsbError, ValueError, struct.error) as e:
                print("ERROR", e)
                failed = True
    finally:
        d.close()
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
