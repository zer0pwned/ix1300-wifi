#!/usr/bin/env python3
"""Put the iX1300 on a WPA2-PSK Wi-Fi network over USB (SETUP NET CMD ope 0x1006 and 0x1106).

Usage: setwifi.py CREDFILE [-v]   (line 1 = SSID, line 2 = passphrase or 64-hex PSK)

Run it as root on Linux, with the scanner on USB and turned on (cover open).
It needs libusb-1.0 and Python 3 only (no Docker). Refer to PROTOCOL.md section 3.
This tool is a thin wrapper: it uses service/ix1300/usb.py, the same code as
"ix1300 usb-wifi-setup".
"""
import logging
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "service"))

from ix1300 import usb  # noqa: E402


def main(argv=None):
    argv = sys.argv[1:] if argv is None else argv
    files = [a for a in argv if a != "-v"]
    if len(files) != 1:
        print(__doc__.strip().splitlines()[2], file=sys.stderr)
        return 2
    logging.basicConfig(level=logging.DEBUG if "-v" in argv else logging.WARNING)
    with open(files[0]) as fh:
        lines = fh.read().splitlines()
    if len(lines) < 2:
        print("error: the file needs the SSID on line 1 and the key on line 2", file=sys.stderr)
        return 2
    try:
        ips = usb.wifi_setup(lines[0], lines[1])
    except (usb.UsbError, ValueError) as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("scanner IP: %s" % (", ".join(ips) or "not known yet"))
    return 0 if ips else 1


if __name__ == "__main__":
    sys.exit(main())
