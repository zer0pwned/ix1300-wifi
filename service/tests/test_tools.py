"""Tests of the standalone USB tools in tools/ (thin wrappers around ix1300.usb, no hardware)."""
import contextlib
import io
import os
import sys
import tempfile
import unittest
from unittest import mock

import support  # noqa: F401
from ix1300 import usb
from test_usb import FakeDev, reply

TOOLS = os.path.join(os.path.dirname(support.ROOT), "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)
import ixusb  # noqa: E402
import setwifi  # noqa: E402


@mock.patch("time.sleep", lambda s: None)
class SetWifiTest(unittest.TestCase):
    def run_tool(self, dev, ssid="net"):
        with tempfile.TemporaryDirectory() as tmp:
            path = os.path.join(tmp, "cred")
            with open(path, "w") as fh:
                fh.write("%s\nkey12345\n" % ssid)
            err = io.StringIO()
            with mock.patch.object(usb, "UsbScanner", return_value=dev), \
                    contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(err):
                return setwifi.main([path]), err.getvalue()

    def test_tools_use_the_service_module(self):
        self.assertIs(setwifi.usb, usb)
        self.assertTrue(issubclass(ixusb.Dev, usb.UsbScanner))

    def test_busy_timeout_does_not_set_boot_mode(self):
        dev = FakeDev([reply(1, 0x1006)])
        orig = FakeDev.netcmd
        dev.netcmd = lambda ope, size, data=b"", tmo=30.0: orig(dev, ope, size, data, tmo=0.02, poll=0.01)
        rc, err = self.run_tool(dev)
        self.assertEqual(rc, 1)
        self.assertIn("still busy", err)
        self.assertEqual(dev.sent, ["SETUP NET START", "SETUP NET CMD", "SETUP NET END"])

    def test_ok(self):
        ip = bytearray(reply(0, 0x7, n=0x230 + 0x14))
        ip[0x1BC:0x1BC + 11] = b"192.0.2.212"
        dev = FakeDev([reply(0, 0x1006), reply(0, 0x1106), bytes(ip)])
        self.assertEqual(self.run_tool(dev, ssid="Café")[0], 0)

    def test_utf8_ssid_keeps_248_bytes(self):
        data = usb.wifi_data("Café", "key12345")
        self.assertEqual(len(data), 0xF8)
        self.assertEqual(data[:5], "Café".encode())


if __name__ == "__main__":
    unittest.main()
