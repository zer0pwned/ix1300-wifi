"""Service tests for the review findings: durable jobs, recovery, admission, stop, supervision."""
import errno
import io
import json
import os
import stat
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
import jpegsynth
from emulator import Emulator, Sheet
from httpfake import FakeServer
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, SheetGate, make_service, sync, wait_for
from test_protocol import scanner_for
from ix1300 import cli, config as C, daemon as D, jobs as J, protocol as P
from ix1300.protocol import Side


class FlakyOutput(RecordOutput):
    """Fails until OK is True."""

    def __init__(self):
        super().__init__()
        self.ok = False
        self.calls = 0
        self.last_error, self.last_permanent = None, False

    def deliver(self, doc):
        self.calls += 1
        if not self.ok:
            self.last_error = "receiver down"
            return False
        return super().deliver(doc)


def read_json(path):
    with open(path) as fh:
        return json.load(fh)


def sides(n, kind="text"):
    out = []
    for name in ("front", "back"):
        data = jpegsynth.synth(320, 440, 1, density=0.5, dpi=150, seed=n + 1)
        out.append(Side(sheet=n, side=name, page_id=0, jpeg=data, width=320, lines=440, dpi=150))
    return out


class JobTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.work),
                          env={})
        self.out = RecordOutput()
        self.svc = None

    def service(self, out=None):
        self.svc = make_service(self.cfg, self.net, {"rec": out or self.out})
        return self.svc

    def tearDown(self):
        if self.svc:
            self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def start(self, out=None):
        svc = self.service(out)
        svc.start()
        self.assertTrue(wait_for(lambda: svc.reserved and self.emu.owner is not None), "no reserve")
        return svc

    def press_and_wait(self, n=1, *sheets, **kw):
        if n > 1:                         # the main thread drops the presses that come during a job
            self.assertTrue(sync(self.svc, self.emu), "the scan thread does not wait for a press")
        if sheets:
            self.emu.load(*sheets)
        self.emu.press(**kw)
        self.assertTrue(wait_for(lambda: self.svc.jobs_done >= n, 30), "job not done")

    def ls(self, kind):
        try:
            return sorted(os.listdir(os.path.join(self.work, kind)))
        except OSError:
            return []

    # --- Findings 1 and 10 ---
    def test_processing_failure_keeps_sheets(self):
        svc = self.start()

        def boom(*a, **k):
            raise OSError(errno.ENOSPC, "No space left on device")
        svc.pipeline.finish = boom
        self.press_and_wait(1, Sheet(front="text"), Sheet(front="text"))
        self.assertEqual(self.ls("jobs"), [])
        (jid,) = self.ls("failed")
        d = os.path.join(self.work, "failed", jid)
        self.assertEqual(len([n for n in os.listdir(d) if n.endswith(".jpg")]), 4)
        m = read_json(os.path.join(d, "manifest.json"))
        self.assertIn("No space left", m["error"])
        self.assertEqual(stat.S_IMODE(os.stat(d).st_mode), 0o700)
        self.assertEqual(stat.S_IMODE(os.stat(os.path.join(d, "manifest.json")).st_mode), 0o600)
        self.assertEqual(self.out.docs, [])

    def test_output_failure_unsent_and_retry(self):
        self.cfg.pipeline["filename_format"] = "same.pdf"        # one name for all documents
        flaky = FlakyOutput()
        svc = self.start(flaky)
        self.press_and_wait(1, Sheet(front="text"))
        self.press_and_wait(2, Sheet(front="text"))
        ids = self.ls("unsent")
        self.assertEqual(len(ids), 2)                                 # no job replaces another
        d = os.path.join(self.work, "unsent", ids[0])
        self.assertEqual(sorted(n for n in os.listdir(d) if not n.startswith(".")), ["document.pdf", "manifest.json"])
        m = read_json(os.path.join(d, "manifest.json"))
        self.assertEqual(m["outputs"]["rec"]["ok"], False)
        self.assertEqual(m["outputs"]["rec"]["error"], "receiver down")
        self.assertEqual(m["document"]["filename"], "same.pdf")
        flaky.ok = True
        self.assertEqual(svc.retry_unsent(force=True), (2, 0))
        self.assertEqual(self.ls("unsent"), [])
        self.assertEqual(sorted(doc.job_id for doc in flaky.docs), ids)

    def test_automatic_retry_with_backoff(self):
        self.cfg.service["retry_interval"] = 1
        flaky = FlakyOutput()
        svc = self.start(flaky)
        self.press_and_wait(1, Sheet(front="text"))
        self.assertEqual(len(self.ls("unsent")), 1)
        n = flaky.calls
        self.assertEqual(svc.retry_unsent(), (0, 1))                  # attempt 1 (due at once)
        self.assertEqual(svc.retry_unsent(), (0, 1))                  # not due yet: no attempt
        self.assertEqual(flaky.calls, n + 1)
        flaky.ok = True
        svc.clock.advance(3600)          # the backoff time and the first wait of the retry thread end
        self.assertTrue(wait_for(lambda: self.ls("unsent") == [], 15))   # the worker tries again

    def test_recovery_after_restart(self):
        jd = J.JobDir.create(self.work, "doc")
        jd.add_sheet(sides(0))
        jd.m.update(state="scanned", deliver=True)
        jd.save()
        jd2 = J.JobDir.create(self.work, "doc")                     # stopped during the delivery
        jd2.add_sheet(sides(1))
        with open(jd2.pdf_path(), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        jd2.m.update(state="delivering", outputs={"rec": {"ok": False}},
                     document=dict(filename="a.pdf", title="A", profile="doc", pages=1, receipt=False, created=0))
        jd2.save()
        self.start()
        self.assertTrue(wait_for(lambda: len(self.out.docs) >= 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)
        self.assertIn("recovered after a service restart", doc.warnings)
        self.assertTrue(wait_for(lambda: self.ls("jobs") == []))
        self.svc.clock.advance(3600)                                  # the retry thread starts
        self.assertTrue(wait_for(lambda: self.ls("unsent") == []))  # the worker sent the second job too
        self.assertEqual(len(self.out.docs), 2)

    def test_cli_retry(self):
        jd = J.JobDir.create(self.work, "doc")
        with open(jd.pdf_path(), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        jd.m.update(state="unsent", outputs={"hook": {"ok": False, "permanent": True}},
                    document=dict(filename="a.pdf", title="A", profile="doc", pages=1, receipt=False, created=0))
        jd.save()
        jd.move(J.UNSENT)
        jf = J.JobDir.create(self.work, "doc")                      # a failed job: only sheets
        jf.add_sheet(sides(0))
        jf.m.update(state="failed", error="processing failed")
        jf.save()
        jf.move(J.FAILED)
        with FakeServer() as srv:
            cfgfile = os.path.join(self.work, "c.toml")
            with open(cfgfile, "w") as fh:
                fh.write('[scanner]\nhost_id = "%s"\n[service]\nwork_dir = "%s"\nshort_press_profile = "doc"\n'
                         '[outputs.hook]\ntype = "webhook"\nurl = "%s/up"\nretries = 0\n[profiles.doc]\n'
                         % (support.HOST_ID, self.work, srv.url))
            buf = io.StringIO()
            with redirect_stdout(buf):
                self.assertEqual(cli.main(["-c", cfgfile, "retry", "--list"]), 0)
            self.assertIn(jd.id, buf.getvalue())
            self.assertIn(jf.id, buf.getvalue())
            with redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main(["-c", cfgfile, "retry", "--failed"]), 0)
            heads = [r["headers"].get("X-Ix1300-Job") for r in srv.rec.requests]
        self.assertEqual(sorted(heads), sorted([jd.id, jf.id]))
        self.assertEqual((self.ls("unsent"), self.ls("failed")), ([], []))

    # --- Findings 8: admission before the scan ---
    def test_no_scan_without_disk_space(self):
        self.cfg.service["min_free_mb"] = 10 ** 9
        self.start()
        self.emu.load(Sheet())
        with self.assertLogs("ix1300.daemon", "ERROR") as logs:
            self.emu.press()
            self.assertTrue(wait_for(lambda: any("scan not started" in m for m in logs.output)))
        self.assertNotIn(0xD5, self.emu.opcodes())

    def test_no_scan_with_full_backlog(self):
        svc = self.start()
        svc.backlog = int(self.cfg.service["max_backlog"])
        self.emu.load(Sheet())
        with self.assertLogs("ix1300.daemon", "ERROR") as logs:
            self.emu.press()
            self.assertTrue(wait_for(lambda: any("max_backlog" in m for m in logs.output)))
            self.assertTrue(sync(svc, self.emu))                       # the press is handled
        self.assertNotIn(0xD5, self.emu.opcodes())
        svc.backlog = 0
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))

    # --- Findings 2 and 3 ---
    def test_failed_page_work_keeps_page(self):
        svc = self.start()
        prepare = svc.pipeline.prepare

        def bad(page, profile, store=None):
            if page.sheet == 0 and page.side == "front":
                raise ValueError("decoder broken")
            return prepare(page, profile, store)
        svc.pipeline.prepare = bad
        self.press_and_wait(1, Sheet(front="text", back="text"))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)                                 # the page is kept as scanned
        self.assertTrue(any("decoder broken" in w for w in doc.warnings))

    def test_unusable_side_is_reported(self):
        svc = self.start()
        orig = D.Page.from_side

        def bad(side, store=None):
            if side.side == "front":
                raise ValueError("data is not a JPEG")
            return orig(side, store)
        D.Page.from_side = staticmethod(bad)
        try:
            self.press_and_wait(1, Sheet(front="text", back="text"))
        finally:
            D.Page.from_side = orig
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 1)
        self.assertTrue(any("not usable" in w for w in doc.warnings))
        self.assertEqual(len(self.ls("failed")), 1)                    # originals stay for a check

    def test_unexpected_error_does_not_stop_the_scan_thread(self):
        svc = self.start()
        orig = D.Job.add_sheet
        calls = []

        def boom(job, sides):
            calls.append(1)
            if len(calls) == 1:
                raise RuntimeError("unexpected")
            return orig(job, sides)
        D.Job.add_sheet = boom
        try:
            self.press_and_wait(1, Sheet(front="text"))
            self.assertEqual(self.out.docs, [])
            self.assertTrue(svc.threads["main"].is_alive())
            self.press_and_wait(2, Sheet(front="text"))
        finally:
            D.Job.add_sheet = orig
        self.assertEqual(len(self.out.docs), 1)
        self.assertIsNone(svc.fatal)
        with self.assertLogs("ix1300.daemon", "CRITICAL"):
            svc._run_thread("test", lambda: 1 / 0)
        self.assertEqual(svc.fatal, "test")

    # --- Finding 7: stop during a scan ---
    def test_stop_during_scan(self):
        self.cfg.service["stop_scan_wait"] = 0
        svc = self.start()
        self.emu.load(*[Sheet(front="text", back="text") for _ in range(6)])
        with SheetGate(go=svc.stop_event) as gate:                     # the job waits after sheet 1 until stop
            self.emu.press()
            self.assertTrue(gate.reached.wait(30))
            svc.stop(timeout=60)
        ops = self.emu.opcodes()
        self.assertEqual(ops.count(0xD8), 2)                           # job start and cancel
        d8 = len(ops) - 1 - ops[::-1].index(0xD8)
        self.assertEqual(ops[d8 + 1], 0xE0)                            # E0 feeds out the rest [HW]
        self.assertNotIn(0x28, ops[d8:])                               # no READ after D8
        self.assertEqual(ops[-1], 0xD6)
        self.assertEqual(self.emu.hopper, [])
        self.assertGreater(self.emu.ejected, 0)
        self.assertTrue(any("ejected without a scan" in w for w in self.out.docs[0].warnings))
        self.assertEqual(self.emu.stats["release"], 1)
        self.assertEqual(len(self.out.docs), 1)
        self.assertTrue(any("service stop" in w for w in self.out.docs[0].warnings))
        self.assertEqual(self.ls("jobs"), [])
        self.assertFalse(svc.threads["main"].is_alive())

    # --- Finding 11: paper lost in the middle of a sheet ---
    def test_paper_lost_partial_off(self):
        self.cfg.service["deliver_partial"] = False
        self.start()
        self.press_and_wait(1, Sheet(front="text"), Sheet(front="text", paper_lost=True))
        self.assertEqual(self.out.docs, [])
        self.assertEqual(len(self.ls("failed")), 1)

    def test_paper_lost_partial_on(self):
        self.start()
        self.press_and_wait(1, Sheet(front="text"), Sheet(front="text", paper_lost=True))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)
        self.assertTrue(any("scan stopped after 1 sheets" in w for w in doc.warnings))

    # --- Finding 23: end of job ---
    def test_failed_end_job_reserves_again(self):
        self.start()
        self.emu.fail_d6 = True
        self.press_and_wait(1, Sheet(front="text"))
        self.emu.fail_d6 = False
        self.assertEqual(len(self.out.docs), 1)
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 2 and self.svc.reserved))
        self.assertGreaterEqual(self.emu.stats["release"], 1)

    def test_lost_start_job_reply_sends_end_job(self):
        sc = scanner_for(self.emu, self.net)
        self.emu.d5_close = True
        self.emu.load(Sheet())
        sc.reserve()
        try:
            with self.assertRaises((P.IxError, OSError)):
                sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320)))
        finally:
            sc.close()
            sc.release()
        ops = self.emu.opcodes()
        self.assertIn(0xD6, ops[ops.index(0xD5):])
        self.assertEqual(sc.job_state, "ended")

    # --- Findings 6, 16, 17 ---
    def test_trigger_source_filter(self):
        svc = self.start()
        other = P.Trigger(code=1, cond=bytes(4), seq=9, reason=0, src=("10.9.9.9", 5))
        ours = P.Trigger(code=1, cond=bytes(4), seq=9, reason=0, src=(svc.scanner.ip, 5))
        self.assertFalse(svc._from_scanner(other))
        self.assertTrue(svc._from_scanner(ours))

    def test_late_copy_after_routine_reserve(self):
        self.cfg.scanner["renew_after_job"] = True
        self.start()
        self.press_and_wait(1, Sheet(front="text"))
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.stats["reserve"] == 2))
        self.emu.load(Sheet(front="text"))
        self.emu.trigger_seq -= 1                                      # a late copy of the same event
        self.emu.press(copies=1)
        self.assertTrue(sync(self.svc, self.emu))                     # the copy is handled
        self.assertEqual(self.emu.opcodes().count(0xD5), 1)

    def test_keepalive_result_of_old_reserve(self):
        self.cfg.scanner["keepalive_interval"] = 0.05
        svc = self.service()
        entered, go, second = threading.Event(), threading.Event(), threading.Event()

        class Slow:
            ip = "127.0.0.1"

            calls = 0

            def keepalive(self, **kw):
                Slow.calls += 1
                if Slow.calls > 1:
                    second.set()                                       # the result of the first probe is done
                    return True                                        # the new reserve is good
                entered.set()
                go.wait(5)
                return False
        svc.scanner, svc.reserved = Slow(), True
        t = threading.Thread(target=svc._keepalive, daemon=True)
        t.start()
        self.assertTrue(entered.wait(5))
        svc._unreserve()                                               # released and reserved again
        with svc.lock:
            svc.reserved, svc.epoch = True, svc.epoch + 1
        go.set()
        self.assertTrue(second.wait(5))
        svc.stop_event.set()
        t.join(5)
        self.assertEqual(svc.ka_failures, 0)
        self.assertFalse(svc.lost.is_set())

    # --- keep_raw ---
    def test_keep_raw(self):
        self.cfg.service.update(keep_raw=True, keep_raw_max_jobs=1)
        self.start()
        self.press_and_wait(1, Sheet(front="blank", back="text"))
        self.press_and_wait(2, Sheet(front="blank", back="text"))
        (rid,) = self.ls("raw")
        d = os.path.join(self.work, "raw", rid)
        self.assertEqual(sorted(os.listdir(d)), ["raw.json", "sheet001-back.jpg", "sheet001-front.jpg"])
        info = read_json(os.path.join(d, "raw.json"))
        self.assertEqual(info["profile"], "doc")
        by = {p["side"]: p for p in info["pages"]}
        self.assertTrue(by["front"]["blank"])
        self.assertFalse(by["back"]["blank"])
        self.assertTrue(by["back"]["kept"])
        self.assertIn("ink_ratio", by["back"])


class CliScanTest(unittest.TestCase):
    """ix1300 scan: streamed pages, partial result after an error, exit code 5."""

    def test_partial_scan(self):
        net = make_net()
        emu = Emulator(net, temp_no_data=0).start()      # no READ poll: the CLI uses Scanner.poll = 0.1 s
        old = P.SystemNet
        P.SystemNet = lambda: net                     # the CLI uses the test network
        try:
            with tempfile.TemporaryDirectory() as tmp:
                cfgfile = os.path.join(tmp, "c.toml")
                with open(cfgfile, "w") as fh:
                    fh.write((CONFIG % dict(hid=support.HOST_ID, search=emu.search_port, work=tmp)).replace(
                        "trigger_port = 0", "trigger_port = 55265"))
                emu.load(Sheet(front="text"), Sheet(front="text", double_feed="back"))
                out = os.path.join(tmp, "x.pdf")
                with redirect_stdout(io.StringIO()), redirect_stderr(io.StringIO()):
                    code = cli.main(["-c", cfgfile, "scan", "--out", out])
                self.assertEqual(code, 5)
                with open(out, "rb") as fh:
                    self.assertIn(b"/Count 2", fh.read())            # sheet 1, both sides
                self.assertEqual(stat.S_IMODE(os.stat(out).st_mode), 0o600)
                self.assertEqual([n for n in os.listdir(tmp) if n.startswith("ix1300-scan-")], [])
        finally:
            P.SystemNet = old
            emu.stop()


if __name__ == "__main__":
    unittest.main()
