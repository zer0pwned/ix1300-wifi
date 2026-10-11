#!/usr/bin/env python3
"""Put the iX1300 on a WPA2-PSK Wi-Fi network over USB (SETUP NET CMD ope 0x1006 and 0x1106).

Usage: setwifi.py CREDFILE [-v]   (line 1 = SSID, line 2 = passphrase or 64-hex PSK)

Run it as root on Linux, with the scanner on USB and turned on (cover open).
It needs libusb-1.0 and ixusb.py (same directory). Refer to PROTOCOL.md section 3.
The service has the same function: "ix1300 usb-wifi-setup".
"""
import struct, sys, time, ixusb

KEY = b"pFusCANsNapFiPfu"


def pfu_encode(s):
    """PFU digit encoding: "%03d" % (byte + 11 + key[i % 16])."""
    return "".join("%03d" % (b + 11 + KEY[i % 16]) for i, b in enumerate(s.encode())).encode()


def pfu_decode(s):
    s = s.split(b"\0")[0].decode()
    return bytes(int(s[i:i + 3]) - 11 - KEY[(i // 3) % 16] for i in range(0, len(s), 3)).decode()


def manual_struct(ssid, psk, sec="WPA2", enc="AES"):
    """Ope 0x1006 data (248 bytes)."""
    b = bytearray(0xF8)
    b[0x00:0x00 + len(ssid)] = ssid.encode()[:32]
    b[0x24:0x24 + len(sec)] = sec.encode()
    b[0x2C:0x2C + len(enc)] = enc.encode()
    k = pfu_encode(psk)
    assert len(k) <= 192
    b[0x34:0x34 + len(k)] = k
    return bytes(b)


def main():
    ssid, psk = open(sys.argv[1]).read().splitlines()[:2]
    assert pfu_decode(pfu_encode(psk)) == psk
    d = ixusb.Dev()
    d.verbose = "-v" in sys.argv
    try:
        print("start", d.diag("SETUP NET START", rlen=1).hex())
        x = d.netcmd(0x1006, 0xF8, manual_struct(ssid, psk), tmo=60)
        print("set wifi: flag=%02x res=%08x reply=%s" % (x[0], struct.unpack_from("<I", x, 8)[0], x[:0x30].hex(" ")))
        if struct.unpack_from("<I", x, 8)[0] != 0:
            print("set wifi failed; boot mode not changed")
            return
        x = d.netcmd(0x1106, 4, b"\0\0\0\0")
        print("set boot mode 0 (client): res=%08x" % struct.unpack_from("<I", x, 8)[0])
        x = d.netcmd(0x103, 4)
        print("boot mode now:", struct.unpack_from("<I", x, 0x14)[0])
        t0 = time.time()
        while time.time() - t0 < 90:
            x = d.netcmd(0x7, 0x230)
            ips = [s.decode() for s in x[0x1BC:0x200].split(b"\0") if s]
            print("%5.1fs ope7 res=%08x mode=%s ips=%s" % (time.time() - t0, struct.unpack_from("<I", x, 8)[0],
                  x[0x158:0x160].split(b"\0")[0].decode(errors="replace"), ips), flush=True)
            if any(i != "0.0.0.0" for i in ips):
                break
            time.sleep(5)
    finally:
        print("end", d.diag("SETUP NET END", rlen=1).hex())
        d.close()


if __name__ == "__main__":
    main()
