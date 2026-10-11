"""Tests for the findings of the second code review (part A).

Each test reproduces one finding. The tests with receipt scans use the synthetic
receipt in tests/data. Real scans in ../../scans are optional (skipped if missing).
"""
import errno
import io
import os
import re
import tempfile
import threading
import unittest
from contextlib import redirect_stderr, redirect_stdout

import support
from emulator import Emulator, Sheet
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, wait_for
from test_jobs import FlakyOutput, read_json
from test_samples import load, need_decoder
from ix1300 import cli, config as C, daemon as D, jobs as J, outputs as O, pipeline as PL, protocol as P
from ix1300.protocol import Side

SCANS = os.path.join(os.path.dirname(support.ROOT), "scans")
RAW_RECEIPT = os.path.join(SCANS, "raw", "20261010-122419-2b9efb")


def real_sides(name="synth"):
    """Both sides of a receipt scan (front = blank, back = print)."""
    return [load(name, "front"), load(name, "back")]


def raw_side(job_dir, sheet, side):
    fn = os.path.join(job_dir, "sheet%03d-%s.jpg" % (sheet, side))
    if not os.path.exists(fn):
        raise unittest.SkipTest("sample %s not found" % fn)
    with open(fn, "rb") as fh:
        data = fh.read()
    return Side(sheet=sheet - 1, side=side, page_id=0 if side == "front" else 1, jpeg=data, dpi=300)


class ServiceCase(unittest.TestCase):
    """A Service that is not started: the tests call recover() and process() directly."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=9, work=self.work), env={})
        self.out = RecordOutput()
        self.services = []

    def tearDown(self):
        for s in self.services:
            s.executor.shutdown(wait=True)
        self.tmp.cleanup()

    def service(self, out=None):
        svc = D.Service(self.cfg, net=make_net(), outputs={"rec": out or self.out})
        self.services.append(svc)
        return svc

    @staticmethod
    def drain(svc):
        while not svc.jobs.empty():
            svc.process(svc.jobs.get())
            svc.backlog -= 1

    def jpegs(self):
        """Names of the sheet JPEGs in the job directories (not in work_dir/raw)."""
        out = []
        for kind in (J.JOBS, J.UNSENT, J.FAILED):
            for root, _dirs, files in os.walk(os.path.join(self.work, kind)):
                out.extend(f for f in files if f.endswith(".jpg"))
        return sorted(out)


# --- A1: a crash or ENOSPC between the side files and the manifest ---------

class A1DurableSheetTest(ServiceCase):
    def test_a1_manifest_commit_fails_then_recover_keeps_jpegs(self):
        jd = J.JobDir.create(self.work, "doc")

        def enospc():
            raise OSError(errno.ENOSPC, "ENOSPC manifest commit")
        jd.save = enospc
        with self.assertRaises(OSError):
            jd.add_sheet(real_sides("synth"))
        self.assertEqual(self.jpegs(), ["sheet001-back.jpg", "sheet001-front.jpg"])
        svc = self.service()                                  # the next start
        svc.recover()
        self.drain(svc)
        self.assertEqual(self.jpegs(), ["sheet001-back.jpg", "sheet001-front.jpg"])
        self.assertEqual(len(self.out.docs), 1)               # the sheet is in a document
        self.assertTrue(any("not in the manifest" in w for w in self.out.docs[0].warnings))
        (fid,) = os.listdir(os.path.join(self.work, J.FAILED))  # and the originals stay for a check
        m = read_json(os.path.join(self.work, J.FAILED, fid, J.MANIFEST))
        self.assertEqual(len(m["sheets"]), 1)

    def test_a1_second_side_write_fails(self):
        jd = J.JobDir.create(self.work, "doc")
        real = J.write_file
        calls = []

        def write(path, chunks, *a, **k):
            calls.append(path)
            if len(calls) == 2:
                raise OSError(errno.ENOSPC, "No space left on device")
            return real(path, chunks, *a, **k)
        J.write_file = write
        try:
            with self.assertRaises(OSError):
                jd.add_sheet(real_sides("synth"))
        finally:
            J.write_file = real
        self.assertEqual(self.jpegs(), ["sheet001-front.jpg"])
        svc = self.service()
        svc.recover()
        self.drain(svc)
        self.assertEqual(self.jpegs(), ["sheet001-front.jpg"])   # quarantined, not removed
        (fid,) = os.listdir(os.path.join(self.work, J.FAILED))
        m = read_json(os.path.join(self.work, J.FAILED, fid, J.MANIFEST))
        self.assertEqual(m["orphans"], ["sheet001-front.jpg"])

    def test_a1_manifest_failure_during_the_scan(self):
        svc = self.service()
        job = D.Job(svc, self.cfg.profile("doc"))
        real_save = job.dir.save

        def enospc():
            raise OSError(errno.ENOSPC, "No space left on device")
        job.dir.save = enospc
        with self.assertRaises(OSError):
            job.add_sheet(real_sides("synth"))
        job.dir.save = real_save                              # space is free again
        job.end_scan(True)
        svc.process(job)
        self.assertEqual(self.jpegs(), ["sheet001-back.jpg", "sheet001-front.jpg"])

    def test_a1_unlisted_sheet_next_to_listed_sheets(self):
        jd = J.JobDir.create(self.work, "doc")
        jd.add_sheet(real_sides("synth"))
        sides = real_sides("synth")
        for s in sides:
            s.sheet = 1
        jd.save = lambda: (_ for _ in ()).throw(OSError(errno.ENOSPC, "full"))
        with self.assertRaises(OSError):
            jd.add_sheet(sides)
        svc = self.service()
        svc.recover()
        self.drain(svc)
        self.assertEqual(self.out.docs[0].pages, 2)            # sheet 2 is found again
        self.assertIn("sheet002-back.jpg", self.jpegs())


class A1FaultSweepTest(unittest.TestCase):
    """A fault (crash or ENOSPC) at each file step of a two-sheet job, then a restart.

    Each side file that was on disk at the fault must stay on disk, or be in a delivered PDF.
    """

    def test_a1_fault_at_each_step(self):
        from test_jobs import sides
        steps = 0
        k = 1
        while True:
            with tempfile.TemporaryDirectory() as work:
                cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=9, work=work), env={})
                calls = [0]
                real = {n: getattr(J, n) for n in ("write_file", "commit_file", "sync_dir")}

                def wrap(name):
                    def fn(*a, **kw):
                        calls[0] += 1
                        if calls[0] == k:
                            raise OSError(errno.ENOSPC, "fault at step %d (%s)" % (k, name))
                        return real[name](*a, **kw)
                    return fn
                for n in real:
                    setattr(J, n, wrap(n))
                fault = None
                try:
                    jd = J.JobDir.create(work, "doc")
                    for n in range(2):
                        jd.add_sheet(sides(n))
                    jd.m.update(state="scanned", deliver=True)
                    jd.save()
                except OSError as e:
                    fault = e
                finally:
                    for n, f in real.items():
                        setattr(J, n, f)
                if fault is None:
                    break                                 # no more steps
                steps += 1
                on_disk = {}
                for root, _d, files in os.walk(work):
                    for f in files:
                        if f.endswith(".jpg"):
                            with open(os.path.join(root, f), "rb") as fh:
                                on_disk[f] = fh.read()
                out = RecordOutput()
                svc = D.Service(cfg, net=make_net(), outputs={"rec": out})
                try:
                    svc.recover()
                    ServiceCase.drain(svc)
                finally:
                    svc.executor.shutdown(wait=True)
                left = set()
                for root, _d, files in os.walk(work):
                    left.update(f for f in files if f.endswith(".jpg"))
                for name, data in on_disk.items():
                    self.assertTrue(name in left or any(data in d.pdf_data for d in out.docs),
                                    "step %d (%s): %s lost" % (k, fault, name))
            k += 1
        self.assertGreaterEqual(steps, 8)


# --- A2: unknown analysis must not lose to a confirmed blank side ------------

class A2SimplexUnknownTest(ServiceCase):
    def test_a2_failed_back_is_not_replaced_by_blank_front(self):
        need_decoder()
        svc = self.service()
        prepare = svc.pipeline.prepare

        def bad(page, profile, store=None):
            if page.side == "back":
                raise ValueError("decoder broken")
            return prepare(page, profile, store)
        svc.pipeline.prepare = bad
        job = D.Job(svc, self.cfg.profile("receipt"))          # simplex
        f, b = real_sides("synth")
        job.add_sheet([f, b])
        job.end_scan(True)
        doc = svc.process(job)
        self.assertIn((0, "back"), doc.kept)                    # the printed side is in the PDF
        self.assertIn(b.jpeg, self.out.docs[0].pdf_data)
        self.assertTrue(any("decoder broken" in w for w in doc.warnings), doc.warnings)

    def unknown(self, side):
        page = PL.Page.from_side(side)
        page.blank = False
        page.errors.append("sheet 1 %s side: processing failed (x): page kept as scanned" % side.side)
        return page

    def test_a2_pipeline_unknown_side_is_kept(self):
        need_decoder()
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        prof = C._profile("p", {"duplex": False, "crop": False})
        f, b = real_sides("synth")
        front = pl.prepare(PL.Page.from_side(f), prof)
        self.assertTrue(front.blank)                              # confirmed blank: it can go
        doc = pl.finish([front, self.unknown(b)], prof)
        self.assertEqual(doc.kept, {(0, "back")})
        self.assertTrue(any("processing failed" in w for w in doc.warnings))
        # Print side measured, other side unknown: both stay (the unknown side can have print).
        back = pl.prepare(PL.Page.from_side(b), prof)
        doc = pl.finish([self.unknown(f), back], prof)
        self.assertEqual(doc.kept, {(0, "front"), (0, "back")})
        # A removed side also gives its warning to the document.
        front = pl.prepare(PL.Page.from_side(f), prof)
        front.errors.append("sheet 1 front side: rotation failed (y): page kept as scanned")
        doc = pl.finish([front, pl.prepare(PL.Page.from_side(b), prof)], prof)
        self.assertEqual(doc.kept, {(0, "back")})
        self.assertTrue(any("rotation failed" in w for w in doc.warnings))


# --- A3: originals of an incomplete PDF stay after a retry or a restart ------

class A3RetainTest(ServiceCase):
    def incomplete_job(self, svc):
        job = D.Job(svc, self.cfg.profile("doc"))
        job.add_sheet(real_sides("synth"))
        job.failures.append("sheet 2 front side: not usable (test)")
        job.end_scan(True)
        return job

    def test_a3_retry_keeps_originals(self):
        flaky = FlakyOutput()
        svc = self.service(flaky)
        svc.process(self.incomplete_job(svc))
        self.assertEqual(len(os.listdir(os.path.join(self.work, J.UNSENT))), 1)
        flaky.ok = True
        svc.retry_unsent(force=True)
        self.assertEqual(len(flaky.docs), 1)
        self.assertEqual(self.jpegs(), ["sheet001-back.jpg", "sheet001-front.jpg"])
        (fid,) = os.listdir(os.path.join(self.work, J.FAILED))
        m = read_json(os.path.join(self.work, J.FAILED, fid, J.MANIFEST))
        self.assertEqual(m["state"], "sent with missing pages")

    def test_a3_restart_during_delivery_keeps_originals(self):
        svc = self.service()

        def crash(*a, **k):
            raise RuntimeError("power cut during the upload")
        svc.send = crash
        with self.assertRaises(RuntimeError):
            svc.process(self.incomplete_job(svc))
        svc2 = self.service()
        svc2.recover()
        self.drain(svc2)
        self.assertEqual(self.jpegs(), ["sheet001-back.jpg", "sheet001-front.jpg"])


# --- A4: the CLI keeps the scan after a failure -------------------------------

class A4CliTest(unittest.TestCase):
    def run_cli(self, extra_cfg, sheets, args):
        net = make_net()
        emu = Emulator(net, temp_no_data=0).start()      # no READ poll: the CLI uses Scanner.poll = 0.1 s
        old = P.SystemNet
        P.SystemNet = lambda: net
        self.addCleanup(setattr, P, "SystemNet", old)
        self.addCleanup(emu.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.work = tmp.name
        cfgfile = os.path.join(tmp.name, "c.toml")
        text = (CONFIG % dict(hid=support.HOST_ID, search=emu.search_port, work=tmp.name)).replace(
            "trigger_port = 0", "trigger_port = 55265").replace("[service]\n", "[service]\n" + extra_cfg)
        with open(cfgfile, "w") as fh:
            fh.write(text)
        emu.load(*sheets)
        err = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(err):
            try:
                code = cli.main(["-c", cfgfile, "scan"] + [a.replace("{tmp}", tmp.name) for a in args])
            except Exception as e:                        # the old CLI let some errors go out
                code = e
        return code, err.getvalue()

    def kept(self):
        out = []
        for root, _d, files in os.walk(self.work):
            out.extend(f for f in files if f.endswith(".jpg"))
        return sorted(out)

    def test_a4_partial_off_keeps_sheets(self):
        code, err = self.run_cli("deliver_partial = false\n",
                                 [Sheet(front="text"), Sheet(front="text", double_feed="back")],
                                 ["--out", "{tmp}/x.pdf"])
        self.assertEqual(code, 5)
        self.assertEqual(self.kept(), ["sheet001-back.jpg", "sheet001-front.jpg"])
        self.assertIn(os.path.join(self.work, J.FAILED), err)
        self.assertFalse(os.path.exists(os.path.join(self.work, "x.pdf")))

    def test_a4_save_failure_keeps_sheets(self):
        code, err = self.run_cli("", [Sheet(front="text")], ["--out", "{tmp}/no/such/dir/x.pdf"])
        self.assertEqual(code, 4)
        self.assertEqual(self.kept(), ["sheet001-back.jpg", "sheet001-front.jpg"])

    def test_a4_success_leaves_nothing(self):
        code, err = self.run_cli("", [Sheet(front="text")], ["--out", "{tmp}/x.pdf"])
        self.assertEqual(code, 0, err)
        self.assertEqual(self.kept(), [])
        self.assertTrue(os.path.exists(os.path.join(self.work, "x.pdf")))
        for kind in (J.JOBS, J.UNSENT, J.FAILED):
            self.assertEqual(os.listdir(os.path.join(self.work, kind)) if os.path.isdir(
                os.path.join(self.work, kind)) else [], [])


# --- A5: physical paper width ---------------------------------------------

class A5WidthTest(unittest.TestCase):
    def width(self, side):
        page = PL.Page.from_side(side)
        g, w, h, dpi = PL.gray_image(page)
        return PL.analyze(g, w, h, dpi).paper_width_mm

    def check_sheet(self, front, back, label):
        wf, wb = self.width(front), self.width(back)
        for sd, v in (("front", wf), ("back", wb)):
            self.assertTrue(77.0 <= v <= 81.0, "%s %s: %.1f mm" % (label, sd, v))
        self.assertLess(abs(wf - wb), 1.5, "%s: front %.1f mm, back %.1f mm" % (label, wf, wb))

    def test_a5_receipt_samples(self):
        need_decoder()
        for name in ("synth", "r1", "r2", "r3", "r4"):
            try:
                sides = real_sides(name)
            except unittest.SkipTest:
                continue
            self.check_sheet(sides[0], sides[1], name)

    def test_a5_raw_receipt(self):
        need_decoder()
        self.check_sheet(raw_side(RAW_RECEIPT, 1, "front"), raw_side(RAW_RECEIPT, 1, "back"), "raw receipt")

    def test_a5_a7_raw_receipt_replay(self):
        # The latest raw receipt (duplex, paper_face up, print on the front): the crop is the
        # same as in the delivered PDF; the records keep the original and the delivered values apart.
        need_decoder()
        if not PL.jpegtran_path():
            self.skipTest("jpegtran not found")
        sides = [raw_side(RAW_RECEIPT, 1, "front"), raw_side(RAW_RECEIPT, 1, "back")]
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        prof = C._profile("default", {"paper_face": "up"})
        pages = [pl.prepare(PL.Page.from_side(s), prof) for s in PL.select_sides(P.ScanResult(sides=sides), prof)]
        doc = pl.finish(pages, prof)
        self.assertTrue(doc.receipt)
        (rec,) = doc.page_records
        self.assertEqual((rec["side"], rec["width"], rec["height"]), ("front", 1188, 9844))
        self.assertEqual(rec["crop_box"], [560, 0, 1748, 9844])
        self.assertAlmostEqual(rec["paper_mm"][0], 79, delta=1.5)
        orig = pages[0].info["original"]
        self.assertEqual((orig["width"], orig["height"], orig["paper_level"]), (2592, 9864, 229))
        pdf = os.path.join(SCANS, "service-out", "scan-20261010-122431.pdf")
        if os.path.exists(pdf):
            with open(pdf, "rb") as fh:
                delivered = fh.read()
            self.assertIn(pages[0].jpeg, delivered)                  # byte for byte the same crop

    def test_a5_print_density_does_not_change_the_width(self):
        g, w, h, dpi, (x0, x1) = synthetic_receipt(lines=False)
        g2, *_ = synthetic_receipt(lines=True)
        a, b = PL.analyze(g, w, h, dpi), PL.analyze(g2, w, h, dpi)
        want = (x1 - x0) * 25.4 / dpi
        self.assertAlmostEqual(a.paper_width_mm, want, delta=1.0)
        self.assertAlmostEqual(b.paper_width_mm, want, delta=1.0)
        self.assertGreater(b.ink_ratio, a.ink_ratio)


def synthetic_receipt(lines):
    """A 80 mm receipt at 75 dpi on a gray backing, with or without dense print."""
    w, h, dpi = 400, 600, 75
    x0, x1 = 80, 80 + int(round(80 / 25.4 * dpi))
    px = bytearray([214]) * (w * h)
    for y in range(30, h - 30):
        for x in range(x0, x1):
            v = 240
            if lines and (y % 6) != 5 and x0 + 5 <= x < x1 - 5:
                v = 20 if x % 3 else 170                        # dark print and gray shade
            px[y * w + x] = v
    return bytes(px), w, h, dpi, (x0, x1)


# --- A6: the memory budget follows the JPEG bytes ---------------------------

class A6BudgetTest(unittest.TestCase):
    def test_a6_close_drops_the_bytes_it_gives_back(self):
        budget = PL.Budget(1000)
        store = PL.PageStore(None, budget=budget)
        blob = store.put(b"x" * 100)
        self.assertEqual(budget.used, 100)
        store.close()
        self.assertEqual(budget.used, 0)
        self.assertIsNone(blob.data)                            # no live bytes that are not counted
        blob.discard()                                          # no second give back
        self.assertEqual(budget.used, 0)

    def test_a6_spilled_blob_stays_readable(self):
        with tempfile.TemporaryDirectory() as d:
            store = PL.PageStore(d, budget=PL.Budget(10))
            blob = store.put(b"y" * 100)
            store.close()
            self.assertEqual(blob.read(), b"y" * 100)


# --- A7: raw.json has original and delivered records --------------------------

class A7RawJsonTest(ServiceCase):
    def test_a7_simplex_crop_and_rotation(self):
        need_decoder()
        if not (PL.have_pillow() or PL.jpegtran_path()):
            self.skipTest("crop needs Pillow or jpegtran")
        self.cfg.service.update(keep_raw=True)
        prof = self.cfg.profile("receipt")
        prof.crop, prof.rotate = True, 180
        svc = self.service()
        job = D.Job(svc, prof)
        job.add_sheet(real_sides("synth"))
        job.end_scan(True)
        svc.process(job)
        pdf = self.out.docs[0].pdf_data
        img_w = int(re.search(rb"/Subtype /Image /Width (\d+) /Height (\d+)", pdf).group(1))
        self.assertIn(b"/Rotate 180", pdf)
        (rid,) = os.listdir(os.path.join(self.work, J.RAW))
        info = read_json(os.path.join(self.work, J.RAW, rid, "raw.json"))
        by = {p["side"]: p for p in info["pages"]}
        back = by["back"]
        final = back.get("delivered") or back                  # the record of the page in the PDF
        self.assertEqual(final["rotate"], 180)
        self.assertEqual(final["width"], img_w)
        self.assertLess(img_w, 2592)
        self.assertEqual(back["original"]["width"], 2592)
        self.assertEqual(back["original"]["height"], 2072)
        self.assertIsNotNone(back["original"]["ink_ratio"])
        self.assertIsNone(by["front"]["delivered"])
        self.assertIn("pipeline", info)
        self.assertIn("tools", info)

    def test_a7_deskew_without_pillow_is_recorded(self):
        need_decoder()
        if PL.have_pillow():
            self.skipTest("Pillow is installed")
        page = PL.Page.from_side(load("synth", "back"))
        PL.deskew_and_crop(page, dict(C.DEFAULTS["pipeline"]), deskew=True, crop=False)
        self.assertIsNotNone(page.info["skew"])
        self.assertIs(page.info["deskew_applied"], False)


# --- A8: one owner for each job ---------------------------------------------

class A8ClaimTest(ServiceCase):
    def failed_job(self):
        jf = J.JobDir.create(self.work, "doc")
        jf.add_sheet(real_sides("synth"))
        jf.m.update(state="failed", error="processing failed")
        jf.save()
        jf.move(J.FAILED)
        return jf

    def test_a8_two_recoveries_do_not_take_the_same_job(self):
        self.failed_job()
        svc1, svc2 = self.service(), self.service()
        self.assertEqual(svc1.recover(kinds=(J.FAILED,)), 1)
        self.assertEqual(svc2.recover(kinds=(J.FAILED,)), 0)       # svc1 owns the job
        self.drain(svc1)
        self.assertEqual(len(self.out.docs), 1)

    def test_a8_retry_reads_the_manifest_under_the_lock(self):
        flaky = FlakyOutput()
        flaky.ok = True
        jd = J.JobDir.create(self.work, "doc")
        with open(jd.pdf_path(), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        jd.m.update(state="unsent", outputs={"rec": {"ok": False, "attempts": 1}},
                    document=dict(filename="a.pdf", title="A", profile="doc", pages=1, receipt=False, created=0))
        jd.save()
        jd.move(J.UNSENT)
        stale = J.list_jobs(self.work, J.UNSENT)                       # read before the other process
        jd.m["outputs"]["rec"]["ok"] = True                             # the other process sent it
        jd.save()
        svc = self.service(flaky)
        old = D.list_jobs
        D.list_jobs = lambda work, kind: stale
        try:
            svc.retry_unsent(force=True)
        finally:
            D.list_jobs = old
        self.assertEqual(flaky.calls, 0)                                # no second upload


# --- A9: C2 and USB replies are checked ----------------------------------------

class A9ValidationTest(unittest.TestCase):
    def scanner(self, reply):
        sc = P.Scanner("127.0.0.1", "127.0.0.1", support.HOST_MAC, bytes.fromhex(support.HOST_ID), net=make_net())
        sc.cmd = lambda cdb, out=b"", inlen=0: reply
        return sc

    def test_a9_bad_c2_is_an_error(self):
        for reply in ((2, bytes(0x30)), (0, b""), (0, b"\x00\x00")):
            with self.assertRaises(P.IxError, msg=repr(reply)):
                self.scanner(reply).hw_status()
        self.assertFalse(self.scanner((0, bytes(0x30))).hw_status().hopper_empty)

    def test_a9_jam_status_unavailable(self):
        sc = self.scanner((0, b""))
        r = P.ScanResult(sheets=2)
        sc._end_check(r)
        self.assertTrue(any("jam status not known" in w for w in r.warnings), r.warnings)

    def test_a9_usb_short_complete_reply(self):
        from ix1300 import usb
        dev = usb.UsbScanner.__new__(usb.UsbScanner)
        reply = bytearray(0x18)
        reply[4:8] = (0x0007).to_bytes(4, "little")
        dev.send_diag = lambda payload: None
        dev.recv_diag = lambda n: bytes(reply)
        self.assertEqual(dev.netcmd(0x0007, 4), bytes(reply))
        dev.recv_diag = lambda n: bytes(reply[:12])                     # shorter than the 20-byte header
        with self.assertRaises(usb.UsbError):
            dev.netcmd(0x0007, 4)


# --- A10: a slow output does not stop the other outputs ------------------------

class SlowOutput(RecordOutput):
    """An output that waits in deliver() until RELEASE is set (or 10 s)."""

    def __init__(self):
        super().__init__()
        self.inside, self.release = threading.Event(), threading.Event()
        self.released = None

    def deliver(self, doc):
        self.inside.set()
        self.released = self.release.wait(10)
        return super().deliver(doc)


class A10SlowOutputTest(ServiceCase):
    def test_a10_outputs_work_in_parallel(self):
        slow, fast = SlowOutput(), RecordOutput()
        svc = D.Service(self.cfg, net=make_net(), outputs={"slow": slow, "fast": fast})
        self.services.append(svc)
        seen = {}
        orig = fast.deliver

        def mark(doc):
            # The slow output has the document and waits for this output: both work at the same time.
            seen["slow inside"] = slow.inside.wait(10)
            ok = orig(doc)
            slow.release.set()
            return ok
        fast.deliver = mark
        job = D.Job(svc, self.cfg.profile("doc"))
        job.add_sheet(real_sides("synth"))
        job.end_scan(True)
        svc.process(job)
        self.assertTrue(seen["slow inside"])                  # the fast output did not wait for the slow one
        self.assertTrue(slow.released)                        # the slow output did not wait for the fast one
        self.assertEqual((len(slow.docs), len(fast.docs)), (1, 1))

    def test_a10_slow_unsent_retry_does_not_stop_new_scans(self):
        net = make_net()
        emu = Emulator(net).start()
        self.addCleanup(emu.stop)
        cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=emu.search_port, work=self.work), env={})
        inside, release = threading.Event(), threading.Event()

        class Hanging(RecordOutput):
            def deliver(self, doc):
                inside.set()
                release.wait(10)
                self.last_error = "receiver down"
                return False
        jd = J.JobDir.create(self.work, "doc")
        with open(jd.pdf_path(), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        jd.m.update(state="unsent", outputs={"slow": {"ok": False}},
                    document=dict(filename="a.pdf", title="A", profile="doc", pages=1, receipt=False, created=0))
        jd.save()
        jd.move(J.UNSENT)
        fast = RecordOutput()
        svc = make_service(cfg, net, {"slow": Hanging(), "fast": fast})
        svc.retry_first = 0                                   # the retry thread starts at once
        cfg.profiles["doc"].outputs = ["fast"]
        try:
            svc.start()
            self.assertTrue(wait_for(lambda: svc.reserved and emu.owner is not None), "no reserve")
            self.assertTrue(inside.wait(10), "no new attempt")            # the old job hangs in its output
            emu.load(Sheet(front="text"))
            emu.press()
            self.assertTrue(wait_for(lambda: len(fast.docs) == 1, 8), "the new scan waits for the old job")
        finally:
            release.set()
            svc.stop()


# --- A11: configuration values are not changed silently ---------------------------

class A11ConfigTest(unittest.TestCase):
    HEAD = '[service]\nshort_press_profile = "p"\nlong_press_profile = ""\n'

    def bad(self, text, want):
        with self.assertRaises(C.ConfigError, msg=text) as cm:
            C.load(text=self.HEAD + text, env={})
        self.assertIn(want, str(cm.exception))

    def test_a11_timing_relations(self):
        self.HEAD = '[service]\nshort_press_profile = "p"\nlong_press_profile = ""\nstop_timeout = 10\n'
        self.bad("[profiles.p]\n", "stop_scan_wait")          # default stop_scan_wait = 15
        self.HEAD = '[service]\nshort_press_profile = "p"\nlong_press_profile = ""\n'
        self.bad("[profiles.p]\n[scanner]\nreserve_timeout = 30\nkeepalive_interval = 29\nkeepalive_wait = 20\n",
                 "keepalive_wait")

    def test_a11_unknown_section(self):
        self.bad("[profiles.p]\n[pipline]\nrotate = 45\n", "pipline")

    def test_a11_fractional_profile_numbers(self):
        self.bad("[profiles.p]\ndpi = 300.9\n", "profile p: dpi")
        self.bad("[profiles.p]\ndpi = \"300\"\n", "profile p: dpi")
        self.bad("[profiles.p]\ndpi = true\n", "profile p: dpi")
        self.bad("[profiles.p]\noutputs = \"abc\"\n", "profile p: outputs")
        cfg = C.load(text=self.HEAD + "[profiles.p]\ndpi = 300\n", env={})
        self.assertEqual(cfg.profile("p").dpi, 300)


# --- A12: the stop deadline also stops a READ that waits ----------------------------

class A12StopTest(unittest.TestCase):
    def test_a12_stop_during_first_read(self):
        net = make_net()
        emu = Emulator(net).start()
        self.addCleanup(emu.stop)
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=emu.search_port, work=tmp.name), env={})
        cfg.scanner.update(data_timeout=60, side_timeout=600)
        cfg.service["stop_scan_wait"] = 0
        svc = make_service(cfg, net, {"rec": RecordOutput()})
        svc.start()
        self.assertTrue(wait_for(lambda: svc.reserved and emu.owner is not None), "no reserve")
        emu.temp_no_data = 10 ** 6                    # the first READ never gets data
        emu.load(Sheet(front="text"))
        emu.press()
        self.assertTrue(wait_for(lambda: emu.stats["temp_no_data"] >= 3, 10))
        # stop(timeout=60) sets the READ deadline to 50 s after the stop (60 s - 10 s). The test
        # clock jumps over it; the real time of stop() stays far below data_timeout (60 s).
        stopper = threading.Thread(target=svc.stop, kwargs=dict(timeout=60), daemon=True)
        stopper.start()
        self.assertTrue(svc.stop_event.wait(10))
        self.assertFalse(svc._abort_read())                    # before the deadline the READ continues
        svc.clock.advance(50)
        stopper.join(30)
        self.assertFalse(stopper.is_alive())
        self.assertFalse(svc.threads["main"].is_alive())       # the scan stopped at the deadline
        self.assertIn(0xD6, emu.opcodes())                       # and ended the job


# --- A13: the directory output never replaces a file ------------------------------

class A13DirectoryTest(unittest.TestCase):
    def test_a13_no_link_fallback_does_not_replace(self):
        with tempfile.TemporaryDirectory() as d:
            out = O.DirectoryOutput("dir", {"path": d})
            doc = PL.Document("a.pdf", "A", "p", 1, False, pdf=b"%PDF-new")
            real_link, real_rename = os.link, os.rename

            def no_link(src, dst):
                raise OSError(errno.EPERM, "no hard links")

            def racy_rename(src, dst):                       # another writer is faster
                if not os.path.exists(dst):
                    with open(dst, "wb") as fh:
                        fh.write(b"%PDF-other")
                return real_rename(src, dst)

            def racy_open(path, flags, mode=0o777, **kw):    # the same race for an exclusive create
                if path.endswith("a.pdf") and not os.path.exists(path):
                    with open(path, "wb") as fh:
                        fh.write(b"%PDF-other")
                return real_open(path, flags, mode, **kw)
            real_open = os.open
            os.link, os.rename, os.open = no_link, racy_rename, racy_open
            try:
                fn = out.send(doc)
            finally:
                os.link, os.rename, os.open = real_link, real_rename, real_open
            with open(os.path.join(d, "a.pdf"), "rb") as fh:
                self.assertEqual(fh.read(), b"%PDF-other")          # the other file is not replaced
            self.assertNotEqual(fn, os.path.join(d, "a.pdf"))
            with open(fn, "rb") as fh:
                self.assertEqual(fh.read(), b"%PDF-new")
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".part")], [])


if __name__ == "__main__":
    unittest.main()
