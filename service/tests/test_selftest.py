"""The guided hardware self-test (ix1300 selftest), the protocol trace and the replay helper.

The self-test runs against the emulator. A scripted user gives the answers and
does the paper and button actions of each prompt (no real waits).
"""
import json
import os
import struct
import tarfile
import tempfile
import unittest

import support
import replay
from emulator import Emulator, Sheet
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, SCANNER_OPTIONS, make_service, wait_for
from ix1300 import config as C
from ix1300 import protocol as P
from ix1300 import selftest as ST
from ix1300 import trace as T
from ix1300.daemon import make_scanner

OVERRIDES = dict(dpi=150, length=320)       # small images: fast tests (the long profile keeps "long")


class ScriptedUI:
    """The user of the tests. SCRIPT: a list of (prompt key, action). An action is a text (the
    typed line), or a function (it loads paper or pushes the button; its text result, if any,
    is the typed line)."""

    def __init__(self, script):
        self.script = list(script)
        self.shown = []
        self.errors = []
        self.keys = []
        self.pending = None

    def _next(self, key):
        self.keys.append(key)
        if not self.script:
            self.errors.append("no script entry for %s" % key)
            return "q"
        want, action = self.script.pop(0)
        if want != key:
            self.errors.append("prompt %s, script has %s" % (key, want))
            return "q"
        if callable(action):
            r = action()
            return r if isinstance(r, str) else ""
        return action

    def show(self, text=""):
        self.shown.append(text)

    def ask(self, key, prompt, timeout=600.0):
        self.shown.append(prompt)
        return self._next(key)

    def begin_wait(self, key, prompt):
        self.shown.append(prompt)
        r = self._next(key)
        self.pending = r or None

    def poll(self):
        r, self.pending = self.pending, None
        return r


def text_sheets(n):
    return [Sheet(front="text", back="text") for _ in range(n)]


class Base(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.work),
                          env={})

    def tearDown(self):
        self.emu.hang_release.set()
        self.emu.stop()
        self.tmp.cleanup()

    def selftest(self, script, only=None):
        self.ui = ScriptedUI(script)
        st = ST.Selftest(self.cfg, self.ui, net=self.net, scanner_options=SCANNER_OPTIONS, only=only,
                         button_timeout=10.0, tick=0.01, reserve_wait=5.0, profile_overrides=OVERRIDES)
        return st

    def press(self, *sheets, long=False, front=None):
        def act():
            self.emu.load(*sheets)
            if front is not None:
                self.emu.load_front(front)
            self.emu.press(long=long)
        return act

    def load(self, *sheets, answer=""):
        def act():
            self.emu.load(*sheets)
            return answer
        return act

    def results(self, st):
        return {r["id"]: r for r in st.results}

    def report(self, st):
        with open(os.path.join(st.bundle, "report.json")) as fh:
            return json.load(fh)



class SelftestStepsTest(Base):
    def full_script(self):
        return [
            ("sensors", ""),
            ("power_off", ""),
            ("registration", ""),
            ("empty_press", self.press()),
            ("empty_press.rollers", "y"),
            ("continue.1", self.press(*text_sheets(2))),
            ("continue.2", self.press(*text_sheets(2))),
            ("front_slot", self.press(front=Sheet(front="text", back="text"))),
            ("front_slot.back", "y"),
            ("long_receipt.have", "y"),
            ("long_receipt", self.press(Sheet(front="text", back="text", length_mm=1200), long=True)),
            ("double_feed.have", "y"),
            ("double_feed.on", self.load(Sheet(front="text", back="text", double_feed=True))),
            ("double_feed.off", self.load(Sheet(front="text", back="text", double_feed=True))),
            ("stop.have", "y"),
            ("stop.load", self.load(*text_sheets(3))),
            ("stop.count", "3"),
        ]

    def test_all_steps_pass(self):
        self.emu.power_off_time[:] = bytes([0x10, 0x1E])
        st = self.selftest(self.full_script())
        code = st.run()
        res = self.results(st)
        self.assertEqual(self.ui.errors, [])
        self.assertEqual({k: r["status"] for k, r in res.items()}, {k: ST.PASS for k in res},
                         {k: (r["status"], r["why"], r.get("data", {}).get("traceback")) for k, r in res.items()})
        self.assertEqual(code, 0)
        # b: the power-off value is the old value again; one change and one restore.
        self.assertEqual(bytes(self.emu.power_off_time), bytes([0x10, 0x1E]))
        self.assertEqual([d for k, d in self.emu.e6_log if k == "set"], [bytes([0x11, 0x1E, 0, 0]),
                                                                           bytes([0x10, 0x1E, 0, 0])])
        # c: show only: no clear and no register.
        self.assertEqual([op for op, _ in self.emu.dc_log], [4, 1])
        self.assertEqual(self.emu.registered_id, bytes.fromhex(support.HOST_ID))
        # e: one document, two parts.
        doc = res["continue"]["data"]["documents"][0]
        self.assertEqual([(p["part"], p["sheets"]) for p in doc["parts"]], [(1, 2), (2, 2)])
        # g: long page mode (E9 length 0x23680, D4 byte 13 = 0x80).
        g = res["long_receipt"]["data"]
        self.assertEqual(bytes.fromhex(g["e9"][0])[8:12], struct.pack(">I", P.LONG_PREREAD_LENGTH))
        self.assertEqual(bytes.fromhex(g["d4"][0])[13], 0x80)
        # h: D4 byte 4: 88, then 00.
        h = res["double_feed"]["data"]
        self.assertEqual([bytes.fromhex(h[k]["d4"][0])[4] for k in ("on", "off")], [0x88, 0x00])
        self.assertEqual(h["on"]["sense"], [dict(op="READ", key=3, asc=0x80, ascq=7)])
        # i: the stop: D8, E0 and D6; the scanner fed out the sheets.
        i = res["stop"]["data"]
        self.assertEqual(i["sheets_out"], 3)
        self.assertTrue(i["path_empty"])
        self.assertGreaterEqual(self.emu.ejected, 2)
        # The scanner is free at the end.
        self.assertIsNone(self.emu.owner)
        # Bundle: report, a trace and raw images for each step, the archive.
        rep = self.report(st)
        self.assertEqual(rep["counts"], {"PASS": 9})
        with open(os.path.join(st.bundle, "report.txt")) as fh:
            self.assertIn("PASS     front_slot", fh.read())
        for r in rep["steps"]:
            self.assertIn(os.path.join(r["dir"], "trace.jsonl"), r["files"])
        recs = replay.load(os.path.join(st.bundle, res["front_slot"]["dir"], "trace.jsonl"))
        self.assertEqual(replay.assert_job(recs, replay.expected_job(1))[0], "D8")
        self.assertTrue(any(f.endswith(".jpg") for f in res["front_slot"]["files"]), res["front_slot"]["files"])
        self.assertTrue(any(f.endswith("document-01.pdf") for f in res["front_slot"]["files"]))
        self.assertTrue(os.path.exists(st.archive))
        with tarfile.open(st.archive) as tar:
            names = tar.getnames()
        self.assertIn(os.path.basename(st.bundle) + "/report.json", names)
        # Image data never goes into the trace: only the summary.
        for r in recs:
            if r.get("op") == "28" and r.get("dir") == "in" and "image" in r:
                self.assertNotIn("data", r)
                self.assertEqual(len(r["image"]["sha256"]), 64)

    def test_fail_keeps_going(self):
        self.emu.e6_check = True               # E6 gives CHECK CONDITION 5/20/00
        st = self.selftest([("sensors", ""), ("power_off", ""), ("empty_press", self.press()),
                            ("empty_press.rollers", "y")], only=["sensors", "power_off", "empty_press"])
        code = st.run()
        res = self.results(st)
        self.assertEqual(self.ui.errors, [])
        self.assertEqual(res["power_off"]["status"], ST.FAIL)
        self.assertIn("nothing written", res["power_off"]["why"])
        self.assertEqual(res["sensors"]["status"], ST.PASS)
        self.assertEqual(res["empty_press"]["status"], ST.PASS)
        self.assertEqual(code, 1)
        self.assertEqual([k for k, _d in self.emu.e6_log], ["get"])     # never a write

    def test_power_off_restored_after_bad_read_back(self):
        self.emu.e6_ignore_set = True          # the set does nothing: the read back differs
        st = self.selftest([("power_off", "")], only=["power_off"])
        st.run()
        r = self.results(st)["power_off"]
        self.assertEqual(r["status"], ST.FAIL)
        self.assertIn("restored", r["why"])
        self.assertEqual(bytes(self.emu.power_off_time), bytes([0x10, 0x1E]))

    def test_trial_code(self):
        self.assertEqual(ST.Selftest.trial_code(0x10), 0x11)
        self.assertEqual(ST.Selftest.trial_code(0x7E), 0x7D)          # the maximum: 15 min less
        self.assertEqual(ST.Selftest.trial_code(0x90), 0x10)          # off: on for the test
        self.assertEqual(ST.Selftest.trial_code(0x7F), 0x10)

    def test_empty_press_with_paper_fails(self):
        st = self.selftest([("empty_press", self.press(*text_sheets(1))), ("empty_press.rollers", "n")],
                           only=["empty_press"])
        st.run()
        r = self.results(st)["empty_press"]
        self.assertEqual(r["status"], ST.FAIL)
        self.assertIn("D5", r["data"]["ops"])

    def test_skip_and_quit(self):
        st = self.selftest([("sensors", "s"), ("power_off", "q")])
        code = st.run()
        res = self.results(st)
        self.assertEqual(res["sensors"]["status"], ST.SKIPPED)
        self.assertEqual(res["power_off"]["status"], ST.QUIT)
        self.assertEqual(res["stop"]["status"], ST.NOT_RUN)
        self.assertEqual(code, 2)
        self.assertEqual(self.emu.e6_log, [])
        self.assertIsNone(self.emu.owner)
        self.assertTrue(os.path.exists(st.archive))

    def test_optional_steps_skip_on_no(self):
        st = self.selftest([("long_receipt.have", "n"), ("double_feed.have", "n"), ("stop.have", "n")],
                           only=["long_receipt", "double_feed", "stop"])
        self.assertEqual(st.run(), 0)
        self.assertEqual({r["status"] for r in st.results}, {ST.SKIPPED})
        self.assertNotIn(0xD5, self.emu.opcodes())

    def test_button_timeout_fails(self):
        st = self.selftest([("empty_press", "")], only=["empty_press"])
        st.button_timeout = 0.2
        st.run()
        r = self.results(st)["empty_press"]
        self.assertEqual(r["status"], ST.FAIL)
        self.assertIn("no button event", r["why"])

    def test_long_mode_refused_falls_back(self):
        orig = self.emu._dispatch

        def dispatch(op, cdb, out, inlen):
            if op == 0xE9 and struct.unpack_from(">I", out, 8)[0] == P.LONG_PREREAD_LENGTH:
                return self.emu._check(5, 0x24, 0)
            return orig(op, cdb, out, inlen)
        self.emu._dispatch = dispatch
        receipt = Sheet(front="text", back="text", length_mm=700)
        st = self.selftest([("long_receipt.have", "y"), ("long_receipt", self.press(receipt, long=True)),
                            ("long_receipt.again", "")], only=["long_receipt"])
        st.run()
        r = self.results(st)["long_receipt"]
        self.assertEqual(self.ui.errors, [])
        self.assertEqual(r["status"], ST.FAIL)
        self.assertIn("long page mode refused (E9 5/24/00)", r["why"])
        self.assertEqual(len(r["data"]["fallback_documents"]), 1)     # the receipt is still in the chute

    def test_stop_on_a_new_connection(self):
        self.emu.hang_read = 0                 # the first READ gets no reply (a Wi-Fi stall)
        st = self.selftest([("stop.have", "y"), ("stop.load", self.load(*text_sheets(3))), ("stop.count", "3")],
                           only=["stop"])
        st.run()
        r = self.results(st)["stop"]
        self.assertEqual(r["status"], ST.PASS, (r["why"], r["data"].get("traceback")))
        self.assertTrue(r["data"]["new_connection"])
        self.assertEqual([o for o in r["data"]["after_stop"] if o in ("D8", "E0", "D6")][0], "D8")

    def test_ctrl_c_ends_cleanly(self):
        def interrupt():
            raise KeyboardInterrupt()
        st = self.selftest([("sensors", ""), ("power_off", interrupt)])
        code = st.run()
        self.assertEqual(code, 2)
        res = self.results(st)
        self.assertEqual(res["power_off"]["status"], ST.QUIT)
        self.assertIsNone(self.emu.owner)
        self.assertTrue(os.path.exists(st.archive))

    def test_sigint_during_a_job_uses_the_stop_path(self):
        st = self.selftest([], only=["sensors"])
        st.svc = make_service(st.cfg, self.net, {})
        st.in_job = True
        st._sigint(2, None)
        self.assertTrue(st.interrupted)
        self.assertTrue(st.svc._abort_read() and st.svc._cancel_scan())
        st.in_job = False
        with self.assertRaises(KeyboardInterrupt):
            st._sigint(2, None)
        st.svc.executor.shutdown()

    def test_refused_when_reserved_by_another_host(self):
        self.emu.occupied_by_other = True
        st = self.selftest([])
        code = st.run()
        self.assertEqual(code, 3)
        self.assertTrue(any("docker stop ix1300" in t for t in self.ui.shown), self.ui.shown)
        rep = self.report(st)
        self.assertIn("another host", rep["refused"])
        self.assertEqual(rep["counts"], {ST.NOT_RUN: 9})
        self.assertNotIn(0xC2, self.emu.opcodes())        # no step ran
        self.assertTrue(os.path.exists(st.archive))

    def test_unknown_step(self):
        with self.assertRaises(ValueError):
            ST.Selftest(self.cfg, ScriptedUI([]), only=["nothing"])


class TerminalUITest(unittest.TestCase):
    def test_lines_poll_and_end(self):
        import io
        out = io.StringIO()
        ui = ST.TerminalUI(inp=io.StringIO("Y\n s \n"), out=out)
        self.assertEqual(ui.ask("k", "question? "), "y")
        ui.begin_wait("w", "push the button")
        self.assertTrue(wait_for(lambda: not ui.lines.empty()))
        self.assertEqual(ui.poll(), "s")
        self.assertEqual(ui.ask("k", "again? "), "q")          # end of input: quit
        self.assertIn("question? ", out.getvalue())


class TraceTest(Base):
    def scanner(self, trace=None):
        sc = make_scanner(self.cfg, support.HOST_IP, support.HOST_MAC, None, self.net, trigger_port=55265,
                          trace=trace)
        sc.port1, sc.port2, sc.search_port = self.emu.port1, self.emu.port2, self.emu.search_port
        for k, v in SCANNER_OPTIONS.items():
            setattr(sc, k, v)
        return sc

    def test_trace_of_a_scan(self):
        path = os.path.join(self.work, "t", "trace.jsonl")
        tr = T.Trace(path, payload_bytes=8)
        sc = self.scanner(tr)
        sc.reserve()
        self.assertTrue(sc.keepalive(timeout=2.0))
        self.emu.load(*text_sheets(2))
        res = sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320)))
        sc.close()
        sc.release()
        tr.close()
        self.assertEqual(res.sheets, 2)
        recs = replay.load(path)
        self.assertEqual(replay.assert_job(recs, replay.expected_job(2))[-1], "D6")
        self.assertEqual(replay.check_emulator(recs, self.emu), len(self.emu.log))
        kinds = {(r["dir"], r["port"]) for r in recs}
        for k in (("out", "port2"), ("in", "port2"), ("out", "keepalive"), ("in", "keepalive"),
                  ("conn", "port1"), ("close", "port1")):
            self.assertIn(k, kinds)
        reserve = [r for r in recs if r["port"] == "port2" and r["dir"] == "in"][0]
        self.assertEqual((reserve["result"], reserve["kind"]), (0, "reserve mode 0"))
        # Payload limit: 8 bytes of hex; the D4 data (0x50 bytes) is cut.
        d4 = [r for r in recs if r.get("op") == "D4" and r["dir"] == "out"][0]
        self.assertEqual((d4["len"], len(d4["data"]), d4["trunc"]), (0x50, 16, True))
        # Sense data is decoded; images have a summary only.
        self.assertTrue(any(r.get("sense", {}).get("eom") for r in recs))
        imgs = [r for r in recs if "image" in r]
        self.assertTrue(imgs)
        self.assertTrue(all("data" not in r for r in imgs))
        for r in recs:
            for k in ("t", "wall", "n", "dir", "port"):
                self.assertIn(k, r)

    def test_trace_off(self):
        sc = self.scanner()
        self.assertIsNone(sc.trace)
        sc.reserve()
        self.emu.load(*text_sheets(1))
        self.assertEqual(sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320))).sheets, 1)
        sc.close()
        sc.release()
        self.assertIsNone(T.open_trace(self.cfg))
        self.assertEqual(os.listdir(self.work), [])

    def test_service_trace_dir(self):
        self.cfg.service["trace_dir"] = os.path.join(self.work, "trace")
        out = RecordOutput()
        svc = make_service(self.cfg, self.net, {"rec": out})
        svc.start()
        try:
            self.assertTrue(wait_for(lambda: svc.reserved and self.emu.owner is not None))
            self.emu.load(*text_sheets(1))
            self.emu.press()
            self.assertTrue(wait_for(lambda: len(out.docs) == 1))
        finally:
            svc.stop()
        files = os.listdir(self.cfg.service["trace_dir"])
        self.assertEqual(len(files), 1)
        recs = replay.load(os.path.join(self.cfg.service["trace_dir"], files[0]))
        trg = [r for r in recs if r["port"] == "trigger"]
        self.assertTrue(trg and trg[0]["code"] == 1 and trg[0]["seq"] >= 1)
        self.assertTrue(any(r["port"] == "search" and r["dir"] == "in" for r in recs))
        self.assertEqual(len(replay.jobs(recs)), 1)

    def test_rotation(self):
        path = os.path.join(self.work, "r.jsonl")
        tr = T.Trace(path, max_bytes=400)
        for i in range(20):
            tr.note("line %d" % i)
        tr.close()
        self.assertTrue(os.path.exists(path + ".1"))
        self.assertLessEqual(os.path.getsize(path), 400)


class ReplayTest(Base):
    def test_synthetic_trace(self):
        recs = replay.synthetic_job(sheets=2)
        self.assertEqual(replay.assert_job(recs, replay.expected_job(2)),
                         ["D8", "E9", "D4", "C2", "D5", "E0", "READ+", "PSIZE", "READ+", "PSIZE",
                          "E0", "READ+", "PSIZE", "READ+", "PSIZE", "E0", "C2", "D6"])
        with self.assertRaises(AssertionError):
            replay.assert_job(recs, replay.expected_job(1))
        with self.assertRaises(AssertionError):
            replay.assert_job(recs, replay.expected_job(2), index=1)
        self.assertEqual(ST.sense_events(recs), [])                  # "no paper" after E0: the normal end
        recs[-5]["sense"] = dict(key=3, asc=0x80, ascq=7, eom=False, ili=False, info=0)
        self.assertEqual(ST.sense_events(recs), [dict(op="E0", key=3, asc=0x80, ascq=7)])

    def test_synthetic_trace_file_round_trip(self):
        path = os.path.join(self.work, "s.jsonl")
        tr = T.Trace(path)
        for r in replay.synthetic_job(sheets=1):
            fields = {k: v for k, v in r.items() if k not in ("t", "wall", "n", "dir", "port")}
            tr.write(r["dir"], r["port"], **fields)
        tr.close()
        self.assertEqual(replay.jobs(replay.load(path)), replay.jobs(replay.synthetic_job(sheets=1)))

    def test_replay_commands_on_the_emulator(self):
        recs = replay.synthetic_job(sheets=0)
        sc = make_scanner(self.cfg, support.HOST_IP, support.HOST_MAC, None, self.net, trigger_port=55265)
        sc.port1, sc.port2 = self.emu.port1, self.emu.port2
        sc.reserve()
        try:
            # Without paper, the emulator gives the same status for each command of this trace.
            self.assertEqual(replay.replay_commands(recs, sc), [])
        finally:
            sc.close()
            sc.release()


if __name__ == "__main__":
    unittest.main()
