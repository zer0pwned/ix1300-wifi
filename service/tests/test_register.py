"""Host ID registration over Wi-Fi: ix1300 wifi-register-host and scanner.auto_register.

The sequence comes from ScanSnap Home (re/hostid-registration.md 3.2). It is
not verified on the scanner [NV]; the emulator implements the same rules.
"""
import io
import logging
import os
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
from emulator import NO_ID, REGISTERED_ID, Emulator
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, wait_for
from ix1300 import cli, config as C, protocol as P, register

NEW_ID = bytes.fromhex("FEDCBA9876543210")
OTHER_ID = bytes.fromhex("1111222233334444")     # the ID of another computer (ScanSnap Home)


class Tty(io.StringIO):
    """A terminal on stdin with the answer of the user."""

    def isatty(self):
        return True


class CliTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net, temp_no_data=0).start()
        self.addCleanup(self.emu.stop)
        old = P.SystemNet
        P.SystemNet = lambda: self.net
        self.addCleanup(setattr, P, "SystemNet", old)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.cfgfile = os.path.join(tmp.name, "c.toml")
        with open(self.cfgfile, "w") as fh:
            fh.write((CONFIG % dict(hid=NEW_ID.hex().upper(), search=self.emu.search_port, work=tmp.name)).replace(
                "trigger_port = 0", "trigger_port = 55265"))

    def run_cli(self, *args, stdin=None):
        out, err = io.StringIO(), io.StringIO()
        old = cli.sys.stdin
        cli.sys.stdin = stdin if stdin is not None else io.StringIO("")
        try:
            with redirect_stdout(out), redirect_stderr(err):
                code = cli.main(["-c", self.cfgfile, "wifi-register-host"] + list(args))
        finally:
            cli.sys.stdin = old
        return code, out.getvalue(), err.getvalue()

    def dc_ops(self):
        return [op for op, _ in self.emu.dc_log]

    def test_show_changes_nothing(self):
        self.emu.registered_id = OTHER_ID
        code, out, err = self.run_cli("--show")
        self.assertEqual(code, 0, err)
        self.assertIn(OTHER_ID.hex().upper(), out)
        self.assertEqual(self.emu.registered_id, OTHER_ID)
        self.assertEqual(self.dc_ops(), [4, 1])                  # read only
        self.assertIsNone(self.emu.owner)                       # released

    def test_show_reads_id_from_dc_data(self):
        # the DC op 1 reply that the real scanner gave (2026-10-11)
        real = bytes.fromhex("01" + "00" * 7 + "18219D62ECE014DD" + "00" * 8)
        self.emu.dc_override[1] = real
        code, out, err = self.run_cli("--show")
        self.assertEqual(code, 0, err)
        self.assertIn("registered host ID (DC op 1): 18219D62ECE014DD", out)
        self.assertEqual(register.dc_data_id(bytes(24)), None)

    def test_show_without_mode2(self):
        self.emu.registered_id = OTHER_ID
        self.emu.accept_mode2 = False
        code, out, err = self.run_cli("--show")
        self.assertEqual(code, 0, err)
        self.assertIn(OTHER_ID.hex().upper(), out)
        self.assertEqual(self.dc_ops(), [])

    def test_register_with_yes(self):
        self.emu.registered_id = OTHER_ID
        code, out, err = self.run_cli("--yes")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.emu.registered_id, NEW_ID)
        self.assertEqual(self.dc_ops(), [3, 0])                  # clear, then register (driver order)
        self.assertEqual(self.emu.dc_log[1][1], NEW_ID + bytes(8))
        self.assertEqual(self.emu.stats["reserve"], 2)          # mode 2, then the mode-0 verify
        self.assertEqual(self.emu.stats["release"], 2)
        self.assertIsNone(self.emu.owner)
        self.assertIn("other computer", err + out)              # the caution
        self.assertIn("verify", out.lower())

    def test_register_without_old_id_does_not_clear(self):
        self.emu.registered_id = NO_ID
        code, out, err = self.run_cli("--yes", "--host-id", "aabbccddeeff0011")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.emu.registered_id, bytes.fromhex("AABBCCDDEEFF0011"))
        self.assertEqual(self.dc_ops(), [0])

    def test_same_id_no_change(self):
        self.emu.registered_id = NEW_ID
        code, out, err = self.run_cli("--yes")
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.dc_ops(), [])
        self.assertIn("no change", out)

    def test_refuse_without_yes(self):
        self.emu.registered_id = OTHER_ID
        code, out, err = self.run_cli()                          # stdin is not a terminal
        self.assertEqual(code, 2)
        self.assertIn("--yes", err)
        self.assertEqual(self.emu.registered_id, OTHER_ID)
        self.assertEqual(self.emu.stats["reserve"], 0)
        self.assertEqual(self.dc_ops(), [])

    def test_interactive_confirmation(self):
        self.emu.registered_id = OTHER_ID
        code, out, err = self.run_cli(stdin=Tty("no\n"))
        self.assertEqual(code, 2)
        self.assertEqual(self.emu.registered_id, OTHER_ID)
        code, out, err = self.run_cli(stdin=Tty("yes\n"))
        self.assertEqual(code, 0, err + out)
        self.assertEqual(self.emu.registered_id, NEW_ID)

    def test_verify_fails(self):
        self.emu.registered_id = NO_ID
        self.emu.dc_ignore_register = True                      # result 0, but no new ID
        code, out, err = self.run_cli("--yes")
        self.assertEqual(code, 1)
        self.assertIn("not " + NEW_ID.hex().upper(), err)
        self.assertEqual(self.dc_ops(), [0])                    # no loop
        self.assertIsNone(self.emu.owner)

    def test_unknown_dc_reply(self):
        self.emu.registered_id = NO_ID
        self.emu.dc_override = {0: b"\x05\x00"}
        code, out, err = self.run_cli("--yes")
        self.assertEqual(code, 1)
        self.assertIn("0x05", err)
        self.assertEqual(self.dc_ops(), [0])                    # one attempt
        self.assertEqual(self.emu.stats["reserve"], 1)          # no verify reserve
        self.assertIsNone(self.emu.owner)                       # released after the error

    def test_mode2_refused(self):
        self.emu.registered_id = OTHER_ID
        self.emu.accept_mode2 = False
        code, out, err = self.run_cli("--yes")
        self.assertEqual(code, 1)
        self.assertIn("-6", err)
        self.assertIn("usb-register-host", err)
        self.assertEqual(self.dc_ops(), [])
        self.assertEqual(self.emu.stats["reserve"], 1)

    def test_bad_host_id(self):
        code, out, err = self.run_cli("--yes", "--host-id", "1234")
        self.assertEqual(code, 2)
        self.assertEqual(self.emu.stats["reserve"], 0)


class AutoRegisterTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.emu.registered_id = OTHER_ID
        self.tmp = tempfile.TemporaryDirectory()
        self.text = CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.tmp.name)
        self.svc = None

    def tearDown(self):
        if self.svc is not None:
            self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def service(self, auto):
        text = self.text.replace("retry_max = 0.3\n", "retry_max = 0.3\nauto_register = %s\n" % auto)
        cfg = C.load(text=text, env={})
        self.svc = make_service(cfg, self.net, {"rec": RecordOutput()})
        return self.svc

    def test_config_default_off(self):
        cfg = C.load(text=self.text, env={})
        self.assertIs(cfg.scanner["auto_register"], False)
        with self.assertRaises(C.ConfigError):
            C.load(text=self.text.replace("retry_max = 0.3\n", "retry_max = 0.3\nauto_register = \"x\"\n"), env={})

    def test_off_keeps_error(self):
        svc = self.service("false")
        svc.start()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 3))
        self.assertFalse(svc.reserved)
        self.assertIn("not registered", svc.last_error)
        self.assertEqual(self.emu.dc_log, [])
        self.assertEqual(self.emu.registered_id, OTHER_ID)

    def test_on_registers_once(self):
        svc = self.service("true")
        with self.assertLogs("ix1300", level="WARNING") as logs:
            svc.start()
            self.assertTrue(wait_for(lambda: svc.reserved and self.emu.owner is not None))
        self.assertEqual(self.emu.registered_id, bytes.fromhex(support.HOST_ID))
        self.assertEqual([op for op, _ in self.emu.dc_log], [3, 0])
        self.assertEqual(self.emu.owner["mode"], 0)
        self.assertTrue(any("auto_register" in r.getMessage() and r.levelno == logging.WARNING
                            for r in logs.records))

    def test_rate_limit(self):
        self.emu.dc_override = {0: b"\x05\x00"}                 # the registration fails
        svc = self.service("true")
        svc.start()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 6))
        self.assertEqual([op for op, _ in self.emu.dc_log], [3, 0])   # one attempt only
        self.assertFalse(svc.reserved)
        self.emu.dc_override = {}
        n = self.emu.stats["reserve"]
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= n + 3))
        self.assertEqual(len(self.emu.dc_log), 2)               # still in the hold time
        svc.clock.advance(3601)
        self.assertTrue(wait_for(lambda: svc.reserved))
        self.assertEqual([op for op, _ in self.emu.dc_log], [3, 0, 0])   # the old ID was cleared before
        self.assertEqual(self.emu.registered_id, bytes.fromhex(support.HOST_ID))


if __name__ == "__main__":
    unittest.main()
