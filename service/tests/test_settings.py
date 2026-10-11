"""Scanner settings: auto power-off time (E6) and double-feed detection (D4 byte 4).

The scanner values are not verified on the hardware [NV]. These tests use the emulator.
"""
import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
from emulator import Emulator, Sheet
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, wait_for
from test_protocol import scanner_for
from ix1300 import cli, config as C, protocol as P
from ix1300 import settings as S

# The D4 and E9 data of the default profile (300 dpi, auto color, auto length) before this change.
GOLDEN_D4 = bytes.fromhex(
    "000301018800c1808080808080000000000000000000000000000000000000300010012c012c05820d00000028800000a543"
    "040000000000000000000000010000000000000000000000000000000000")
GOLDEN_E9 = bytes.fromhex("012c012c00002880000043500500000000000000000000000000000000000000")


def load(extra_scanner="", profile="", env=None):
    text = "[scanner]\n%s\n[profiles.default]\n%s\n" % (extra_scanner, profile)
    return C.load(text=text, env=env or {})


class PowerOffConfigTest(unittest.TestCase):
    def test_unset_is_default(self):
        self.assertIsNone(load().scanner["power_off_minutes"])
        self.assertIsNone(load('power_off_minutes = ""').scanner["power_off_minutes"])

    def test_good_values(self):
        for v in (0, 15, 240, 255, 1890):
            self.assertEqual(load("power_off_minutes = %d" % v).scanner["power_off_minutes"], v)

    def test_bad_values(self):
        for v in ("14", "16", "1891", "1905", "-15", "240.5", "true", '"abc"', "3"):
            with self.assertRaises(C.ConfigError, msg=v):
                load("power_off_minutes = %s" % v)

    def test_env(self):
        cfg = load(env={"IX1300_SCANNER__POWER_OFF_MINUTES": "120"})
        self.assertEqual(cfg.scanner["power_off_minutes"], 120)
        cfg = load("power_off_minutes = 120", env={"IX1300_SCANNER__POWER_OFF_MINUTES": ""})
        self.assertIsNone(cfg.scanner["power_off_minutes"])
        with self.assertRaises(C.ConfigError):
            load(env={"IX1300_SCANNER__POWER_OFF_MINUTES": "ten"})


class PowerOffCodeTest(unittest.TestCase):
    def test_code(self):
        self.assertEqual(S.power_off_code(240, 0x08), 0x10)
        self.assertEqual(S.power_off_code(15, 0x10), 0x01)
        self.assertEqual(S.power_off_code(1890, 0x10), 0x7E)
        self.assertEqual(S.power_off_code(255, 0x90), 0x11)       # set a time: auto power-off on again
        self.assertEqual(S.power_off_code(0, 0x10), 0x90)         # off: keep the time bits
        self.assertEqual(S.power_off_code(0, 0x90), 0x90)

    def test_describe(self):
        self.assertEqual(S.power_off_minutes(0x10), 240)
        self.assertEqual(S.power_off_minutes(0x90), 0)
        self.assertEqual(S.power_off_minutes(0x7F), 3)
        self.assertIsNone(S.power_off_minutes(0x00))
        self.assertIn("240", S.describe_power_off(0x10, 0x1E))
        self.assertIn("off", S.describe_power_off(0x90, 0x1E))


class PowerOffScannerTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.sc = scanner_for(self.emu, self.net)
        self.sc.reserve()

    def tearDown(self):
        self.sc.close()
        self.emu.stop()

    def test_cdb_layout(self):
        self.assertEqual(S.read_power_off(self.sc), (0x10, 0x1E))
        S.write_power_off(self.sc, 0x08, 0x1E)
        cdbs = [cdb for op, cdb in self.emu.log if op == 0xE6]
        self.assertEqual(cdbs, [bytes.fromhex("E6 00 01 00 00 00 00 04"), bytes.fromhex("E6 00 00 00 00 04 00 00")])
        self.assertEqual(self.emu.e6_log, [("get", b""), ("set", bytes.fromhex("08 1E 00 00"))])
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("08 1E"))

    def test_set_when_different(self):
        r = S.sync_power_off(self.sc, 255)
        self.assertEqual((r.before, r.after, r.written), ((0x10, 0x1E), (0x11, 0x1E), True))
        self.assertEqual([k for k, _ in self.emu.e6_log], ["get", "set", "get"])
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("11 1E"))

    def test_keep_byte1_as_read(self):
        self.emu.power_off_time[1] = 0x2A
        S.sync_power_off(self.sc, 120)
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("08 2A"))

    def test_no_write_when_equal(self):
        r = S.sync_power_off(self.sc, 240)
        self.assertFalse(r.written)
        self.assertEqual([k for k, _ in self.emu.e6_log], ["get"])

    def test_off(self):
        S.sync_power_off(self.sc, 0)
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("90 1E"))

    def test_verify_mismatch(self):
        self.emu.e6_ignore_set = True
        with self.assertRaises(S.SettingsError):
            S.sync_power_off(self.sc, 255)
        with self.assertLogs("ix1300", logging.WARNING) as cm:
            self.assertFalse(S.apply_power_off(self.sc, 255))
        self.assertTrue(any("power-off" in m for m in cm.output))

    def test_not_set(self):
        # [HW] a scanner that never got a value gives 00 00 (the factory time, 240 minutes)
        self.emu.power_off_time[0:2] = [0x00, 0x00]
        self.assertIn("not set", S.describe_power_off(0, 0))
        self.assertTrue(S.apply_power_off(self.sc, 255))
        self.assertEqual(self.emu.e6_log, [("get", b""), ("set", bytes.fromhex("11 00 00 00")), ("get", b"")])

    def test_check_condition(self):
        self.emu.e6_check = True
        with self.assertLogs("ix1300", logging.WARNING):
            self.assertFalse(S.apply_power_off(self.sc, 255))
        self.assertEqual(self.sc.hw_status().hopper_empty, True)   # the connection still works

    def test_unset_no_traffic(self):
        self.assertIsNone(S.apply_power_off(self.sc, None))
        self.assertEqual(self.emu.e6_log, [])


class PowerOffServiceTest(unittest.TestCase):
    def run_service(self, extra, **emu_attrs):
        net = make_net()
        self.emu = Emulator(net)
        for k, v in emu_attrs.items():
            setattr(self.emu, k, v)
        self.emu.start()
        self.addCleanup(self.emu.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        text = (CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=tmp.name)).replace(
            "[scanner]\n", "[scanner]\n" + extra)
        cfg = C.load(text=text, env={})
        self.out = RecordOutput()
        self.svc = make_service(cfg, net, {"rec": self.out})
        self.addCleanup(self.svc.stop)
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None), "no reserve")

    def scan_one(self):
        self.emu.load(Sheet(front="text", back="text"))
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1), "no document")

    def test_unset_no_e6(self):
        self.run_service("")
        self.scan_one()
        self.assertNotIn(0xE6, self.emu.opcodes())

    def test_set_after_reserve(self):
        self.run_service("power_off_minutes = 255\n")
        self.assertTrue(wait_for(lambda: len(self.emu.e6_log) == 3), "no E6 read-back")
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("11 1E"))
        self.assertEqual([k for k, _ in self.emu.e6_log], ["get", "set", "get"])
        self.scan_one()

    def test_mismatch_does_not_block(self):
        with self.assertLogs("ix1300", logging.WARNING) as cm:
            self.run_service("power_off_minutes = 255\n", e6_ignore_set=True)
            self.scan_one()
        self.assertTrue(any("power-off" in m for m in cm.output))
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("10 1E"))

    def test_error_does_not_block(self):
        with self.assertLogs("ix1300", logging.WARNING) as cm:
            self.run_service("power_off_minutes = 255\n", e6_check=True)
            self.scan_one()
        self.assertTrue(any("power-off" in m for m in cm.output))


class PowerOffCliTest(unittest.TestCase):
    def run_cli(self, args):
        net = make_net()
        self.emu = Emulator(net).start()
        self.addCleanup(self.emu.stop)
        old = P.SystemNet
        P.SystemNet = lambda: net
        self.addCleanup(setattr, P, "SystemNet", old)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfgfile = os.path.join(tmp.name, "c.toml")
        with open(cfgfile, "w") as fh:
            fh.write((CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=tmp.name)).replace(
                "trigger_port = 0", "trigger_port = 55265"))
        out, err = io.StringIO(), io.StringIO()
        with redirect_stdout(out), redirect_stderr(err):
            code = cli.main(["-c", cfgfile, "settings"] + args)
        return code, out.getvalue(), err.getvalue()

    def test_show(self):
        code, out, _ = self.run_cli(["--show"])
        self.assertEqual(code, 0)
        self.assertIn("240", out)
        self.assertEqual([k for k, _ in self.emu.e6_log], ["get"])
        self.assertIsNone(self.emu.owner)             # released

    def test_set(self):
        code, out, _ = self.run_cli(["--power-off", "255"])
        self.assertEqual(code, 0)
        self.assertIn("255", out)
        self.assertEqual(bytes(self.emu.power_off_time), bytes.fromhex("11 1E"))

    def test_bad_value(self):
        code, _, err = self.run_cli(["--power-off", "100"])
        self.assertEqual(code, 2)
        self.assertEqual(self.emu.e6_log, [])


class DoubleFeedTest(unittest.TestCase):
    def test_golden_default_unchanged(self):
        self.assertEqual(P.ScanSettings().d4_data(), GOLDEN_D4)
        self.assertEqual(P.ScanSettings().e9_data(), GOLDEN_E9)
        prof = load().profile("default")
        self.assertTrue(prof.double_feed)
        self.assertEqual(prof.scan_settings().d4_data(), GOLDEN_D4)
        self.assertEqual(prof.scan_settings().e9_data(), GOLDEN_E9)

    def test_off_clears_byte4_only(self):
        d = load(profile="double_feed = false").profile("default").scan_settings().d4_data()
        self.assertEqual(d[4], 0x00)
        self.assertEqual(d[:4] + d[5:], GOLDEN_D4[:4] + GOLDEN_D4[5:])
        self.assertEqual(load(profile="double_feed = true").profile("default").scan_settings().d4_data(), GOLDEN_D4)

    def test_bad_value(self):
        with self.assertRaises(C.ConfigError):
            load(profile='double_feed = "maybe"')

    def test_env(self):
        cfg = load(env={"IX1300_PROFILES__DEFAULT__DOUBLE_FEED": "false"})
        self.assertFalse(cfg.profile("default").double_feed)

    def scan(self, double_feed):
        net = make_net()
        emu = Emulator(net).start()
        sc = scanner_for(emu, net)
        try:
            emu.load(Sheet(front="text", double_feed=True))
            sc.reserve()
            try:
                return emu, sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(400), double_feed=double_feed))
            except P.DoubleFeed:
                return emu, None
            finally:
                sc.close()
                sc.release()
        finally:
            emu.stop()

    def test_emulator(self):
        emu, r = self.scan(True)
        self.assertEqual(emu.d4_byte4, [0x88])
        self.assertIsNone(r)
        emu, r = self.scan(False)
        self.assertEqual(emu.d4_byte4, [0x00])
        self.assertEqual(r.sheets, 1)


if __name__ == "__main__":
    unittest.main()
