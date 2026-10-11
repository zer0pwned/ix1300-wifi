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


if __name__ == "__main__":
    unittest.main()
