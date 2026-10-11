"""Service tests for the third review: a durable OCR PDF, the recovery of damaged jobs, a bounded stop."""
import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

import support
import jpegsynth
from emulator import Emulator
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, wait_for
from test_jobs import read_json, sides
from ix1300 import cli, config as C, jobs as J, pipeline as PL


class OcrDurableTest(unittest.TestCase):
    """Finding A: the OCR PDF is on the disk (fsync) before the originals go."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = self.tmp.name
        # A tesseract that writes out.pdf: the arguments are "list.txt out ... pdf".
        self.exe = os.path.join(self.dir, "fake-tesseract")
        with open(self.exe, "w") as fh:
            fh.write("#!/bin/sh\nprintf '%%PDF-1.4\\n%%%%EOF\\n' > \"$2.pdf\"\n")
        os.chmod(self.exe, 0o700)
        self.jobdir = os.path.join(self.dir, "job")
        os.mkdir(self.jobdir, 0o700)

    def tearDown(self):
        self.tmp.cleanup()

    def test_ocr_pdf_is_synced_before_use(self):
        page = PL.Page.from_jpeg(jpegsynth.synth(320, 440, 1, density=0.5, dpi=150, seed=1), 150)
        out = os.path.join(self.jobdir, J.PDF)
        synced = []
        real = os.fsync

        def fsync(fd):
            st = os.fstat(fd)
            synced.append((st.st_dev, st.st_ino))
            return real(fd)
        with mock.patch.dict(os.environ, {"IX1300_TESSERACT": self.exe}), mock.patch.object(PL.os, "fsync", fsync):
            self.assertEqual(PL.ocr_pdf([page], out_path=out), out)
        with open(out, "rb") as fh:
            self.assertTrue(fh.read().startswith(b"%PDF"))
        st, dst = os.stat(out), os.stat(self.jobdir)
        self.assertIn((st.st_dev, st.st_ino), synced, "the OCR PDF was not synced")
        self.assertIn((dst.st_dev, dst.st_ino), synced, "the job directory was not synced")
        self.assertEqual(stat.S_IMODE(st.st_mode), 0o600)
        self.assertEqual([n for n in os.listdir(self.jobdir) if n != J.PDF], [])   # no temporary file stays


class DamagedRecoveryTest(unittest.TestCase):
    """Finding B: one damaged job does not stop the recovery of the other jobs."""

    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.work),
                          env={})
        self.out = RecordOutput()
        self.svc = None

    def tearDown(self):
        if self.svc:
            self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def ls(self, kind):
        try:
            return sorted(os.listdir(os.path.join(self.work, kind)))
        except OSError:
            return []

    def test_missing_side_does_not_stop_the_recovery(self):
        bad = J.JobDir.create(self.work, "doc")
        bad.add_sheet(sides(0))
        bad.m.update(state="scanned", deliver=True)
        bad.save()
        os.remove(os.path.join(bad.path, bad.m["sheets"][0]["front"]["file"]))
        good = J.JobDir.create(self.work, "doc")
        good.add_sheet(sides(1))
        good.m.update(state="scanned", deliver=True)
        good.save()
        self.svc = make_service(self.cfg, self.net, {"rec": self.out})
        self.svc.start()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 2), "a job was not recovered")
        by_id = {d.job_id: d for d in self.out.docs}
        self.assertEqual(by_id[good.id].pages, 2)
        self.assertEqual(by_id[bad.id].pages, 1)                     # the side that is on the disk
        self.assertTrue(any("not readable" in w for w in by_id[bad.id].warnings))
        # The damaged job stays in failed with its remaining original; the intact job goes away.
        self.assertTrue(wait_for(lambda: self.ls("jobs") == [] and self.ls("failed") == [bad.id]))
        m = read_json(os.path.join(self.work, "failed", bad.id, J.MANIFEST))
        self.assertTrue(m["missing"])

    def test_bad_manifest_goes_to_failed_and_the_recovery_continues(self):
        bad = J.JobDir.create(self.work, "doc")
        bad.add_sheet(sides(0))
        bad.m.update(state="scanned", deliver=True, sheets=[{"front": {"dpi": 150}}])   # no file name
        bad.save()
        good = J.JobDir.create(self.work, "doc")
        good.add_sheet(sides(1))
        good.m.update(state="scanned", deliver=True)
        good.save()
        self.svc = make_service(self.cfg, self.net, {"rec": self.out})
        self.svc.start()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1), "the intact job was not recovered")
        self.assertEqual(self.out.docs[0].job_id, good.id)
        self.assertTrue(wait_for(lambda: self.ls("jobs") == [] and self.ls("failed") == [bad.id]))
        m = read_json(os.path.join(self.work, "failed", bad.id, J.MANIFEST))
        self.assertIn("recovery failed", m["error"])
        self.assertEqual(len([n for n in os.listdir(os.path.join(self.work, "failed", bad.id))
                              if n.endswith(".jpg")]), 2)            # the originals stay


STOP_SCRIPT = r"""
import os, sys, threading, time
sys.path.insert(0, sys.argv[1])
import support
from ix1300 import cli, daemon as D

started = threading.Event()

class Fake:
    def __init__(self, cfg):
        import concurrent.futures
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=1)
    def run_forever(self):
        self.executor.submit(lambda: (started.set(), time.sleep(30)))
        started.wait()
        self.executor.shutdown(wait=False, cancel_futures=True)
        return 0

cli.Service = Fake
cli._load = lambda args: type("Cfg", (), {"host_id": "x"})()
sys.exit(cli.main(["serve"]))
"""


class BoundedStopTest(unittest.TestCase):
    """Finding C: a page task that still runs does not keep the process alive after the stop."""

    def test_serve_exits_with_a_running_page_task(self):
        with tempfile.TemporaryDirectory() as tmp:
            script = os.path.join(tmp, "stop.py")
            with open(script, "w") as fh:
                fh.write(STOP_SCRIPT)
            t0 = time.monotonic()
            r = subprocess.run([sys.executable, script, support.HERE], capture_output=True, text=True, timeout=60)
            took = time.monotonic() - t0
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertLess(took, 15, "the process waited for the page task")

    def test_exit_code_without_running_threads(self):
        self.assertEqual(cli.exit_after_stop(3), 3)


if __name__ == "__main__":
    unittest.main()
