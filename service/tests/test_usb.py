"""USB setup tests with a fake libusb and a fake diagnostic transport (no hardware)."""
import struct
import unittest
from unittest import mock

import support  # noqa: F401
from ix1300 import usb


class FakeLib:
    """The libusb_* calls of UsbScanner. Records the calls."""

    def __init__(self, init=0, open_ok=True, claim=0):
        self.init, self.open_ok, self.claim = init, open_ok, claim
        self.calls = []

    def libusb_init(self, ref):
        self.calls.append("init")
        return self.init

    def libusb_exit(self, ctx):
        self.calls.append("exit")

    def libusb_open_device_with_vid_pid(self, ctx, vid, pid):
        self.calls.append("open")
        return 1234 if self.open_ok else None

    def libusb_set_auto_detach_kernel_driver(self, h, on):
        return 0

    def libusb_claim_interface(self, h, i):
        self.calls.append("claim")
        return self.claim

    def libusb_release_interface(self, h, i):
        self.calls.append("release")

    def libusb_close(self, h):
        self.calls.append("close")


class InitTest(unittest.TestCase):
    def make(self, lib):
        with mock.patch.object(usb, "_lib", return_value=lib):
            return usb.UsbScanner()

    def test_open_fails(self):
        lib = FakeLib(open_ok=False)
        with self.assertRaises(usb.UsbError):
            self.make(lib)
        self.assertEqual(lib.calls, ["init", "open", "exit"])

    def test_claim_fails(self):
        lib = FakeLib(claim=-6)
        with self.assertRaises(usb.UsbError):
            self.make(lib)
        self.assertEqual(lib.calls, ["init", "open", "claim", "close", "exit"])

    def test_init_fails(self):
        lib = FakeLib(init=-1)
        with self.assertRaises(usb.UsbError):
            self.make(lib)
        self.assertEqual(lib.calls, ["init"])

    def test_close_twice(self):
        lib = FakeLib()
        dev = self.make(lib)
        dev.close()
        dev.close()
        self.assertEqual(lib.calls, ["init", "open", "claim", "release", "close", "exit"])


class FakeDev(usb.UsbScanner):
    """UsbScanner with a fake diagnostic transport. REPLIES: list of RECEIVE DIAGNOSTIC replies."""

    def __init__(self, replies=(), diag_replies=None):
        self.h = None
        self.ctx = None
        self.replies = list(replies)
        self.diag_replies = dict(diag_replies or {})
        self.sent = []
        self.recv_count = 0

    def send_diag(self, payload):
        self.sent.append(payload[:16].decode().strip())

    def recv_diag(self, n):
        self.recv_count += 1
        return self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]

    def diag(self, tag, extra=b"", rlen=0):
        self.sent.append(tag)
        return self.diag_replies.get(tag, b"\0" * rlen)

    def host_id(self):
        return bytes(8)

    def __exit__(self, *exc):
        pass


def reply(flag, ope, res=0, n=0x20):
    r = bytearray(n)
    r[0] = flag
    struct.pack_into("<II", r, 4, ope, res)
    return bytes(r)


@mock.patch("time.sleep", lambda s: None)
class NetCmdTest(unittest.TestCase):
    def test_busy_then_done(self):
        dev = FakeDev([reply(1, 0x103), reply(1, 0x103), reply(0, 0x103, 7)])
        x = dev.netcmd(0x103, 4)
        self.assertEqual(usb.result(x), 7)
        self.assertEqual(dev.sent, ["SETUP NET CMD"])           # the command goes one time only
        self.assertEqual(dev.recv_count, 3)

    def test_busy_timeout(self):
        dev = FakeDev([reply(1, 0x103)])
        with self.assertRaises(usb.UsbTimeout):
            dev.netcmd(0x103, 4, tmo=0.05, poll=0.01)
        self.assertEqual(dev.sent, ["SETUP NET CMD"])

    def test_short_reply(self):
        for r in (b"", b"\0" * 8):
            with self.assertRaises(usb.UsbError):
                FakeDev([r]).netcmd(0x103, 4)
        with self.assertRaises(usb.UsbError):
            usb.result(b"\0" * 11)

    def test_wrong_echo_is_logged(self):
        with self.assertLogs("ix1300.usb", "WARNING"):
            x = FakeDev([reply(0, 0x999)]).netcmd(0x103, 4)
        self.assertEqual(usb.result(x), 0)


@mock.patch("time.sleep", lambda s: None)
class SetupTest(unittest.TestCase):
    def run_with(self, dev, fn, *args):
        with mock.patch.object(usb, "UsbScanner", return_value=dev):
            return fn(*args, log=lambda *a: None)

    def test_wifi_short_reply_sends_end(self):
        dev = FakeDev([b"\0" * 4])
        with self.assertRaises(usb.UsbError):
            self.run_with(dev, usb.wifi_setup, "net", "key")
        self.assertEqual(dev.sent[-1], "SETUP NET END")

    def test_wifi_wrong_key(self):
        dev = FakeDev([reply(0, 0x1006, 0x1000)])
        with self.assertRaises(usb.UsbError) as cm:
            self.run_with(dev, usb.wifi_setup, "net", "key")
        self.assertIn("wrong key", str(cm.exception))
        self.assertEqual(dev.sent[-1], "SETUP NET END")

    def test_wifi_busy_timeout_sends_end(self):
        dev = FakeDev([reply(1, 0x1006)])
        orig = FakeDev.netcmd
        dev.netcmd = lambda ope, size, data=b"", tmo=30.0: orig(dev, ope, size, data, tmo=0.02, poll=0.01)
        with self.assertRaises(usb.UsbTimeout):
            self.run_with(dev, usb.wifi_setup, "net", "key")
        self.assertEqual(dev.sent[-1], "SETUP NET END")

    def test_wifi_ok(self):
        ip = bytearray(reply(0, 0x7, n=0x230 + 0x14))
        ip[0x1BC:0x1BC + 14] = b"192.0.2.212"
        dev = FakeDev([reply(0, 0x1006), reply(0, 0x1106), bytes(ip)])
        self.assertEqual(self.run_with(dev, usb.wifi_setup, "net", "key"), ["192.0.2.212"])
        self.assertEqual(dev.sent[0], "SETUP NET START")
        self.assertEqual(dev.sent[-1], "SETUP NET END")

    def test_register_empty_reply_sends_end(self):
        dev = FakeDev(diag_replies={"REG HOSTID DATA": b""})
        with self.assertRaises(usb.UsbError):
            self.run_with(dev, usb.register_host_id, bytes.fromhex("0123456789ABCDEF"))
        self.assertEqual(dev.sent[-1], "SETUP NET END")

    def test_net_start_refused(self):
        dev = FakeDev(diag_replies={"SETUP NET START": b"\x82"})
        with self.assertRaises(usb.UsbError):
            self.run_with(dev, usb.register_host_id, bytes.fromhex("0123456789ABCDEF"))
        dev = FakeDev(diag_replies={"SETUP NET START": b""})
        with self.assertRaises(usb.UsbError):
            self.run_with(dev, usb.register_host_id, bytes.fromhex("0123456789ABCDEF"))


if __name__ == "__main__":
    unittest.main()
