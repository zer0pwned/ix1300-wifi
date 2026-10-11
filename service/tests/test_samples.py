"""Tests with receipt scans (300 dpi, front = blank side, back = print).

tests/data has the "synth" pair: a synthetic receipt (make_synth_receipt.py) with the
size and layout of a real iX1300 scan over Wi-Fi. The real receipts r1..r4 are not in
the repository (personal data). The tests use them only if ../../scans/receipts has
them; otherwise these cases are skipped.
The pixel tests need a JPEG decoder: Pillow or the djpeg program.
"""
import os
import re
import unittest

import support
from ix1300 import config as C, jpeg, pipeline as PL
from ix1300.protocol import ScanResult, Side

DATA = os.path.join(support.HERE, "data")
SCANS = os.path.join(os.path.dirname(support.ROOT), "scans")
MORE = os.path.join(SCANS, "receipts")
# Real receipts: length in mm, measured on the scanner: 175, 668, 415, 834 mm.
RECEIPTS = {"synth": DATA, "r1": MORE, "r2": MORE, "r3": MORE, "r4": MORE}


def load(name, side):
    fn = os.path.join(RECEIPTS[name], "%s_%s.jpg" % (name, "0_front" if side == "front" else "1_back"))
    if not os.path.exists(fn):
        raise unittest.SkipTest("sample %s not found" % fn)
    with open(fn, "rb") as fh:
        data = fh.read()
    return Side(sheet=0, side=side, page_id=0 if side == "front" else 1, jpeg=data, dpi=300)


def need_decoder():
    if not (PL.have_pillow() or PL.djpeg_path()):
        raise unittest.SkipTest("no JPEG decoder (Pillow or djpeg)")


class SampleTest(unittest.TestCase):
    cfg = dict(C.DEFAULTS["pipeline"], rotate=0)

    def test_headers(self):
        s = load("synth", "back")
        self.assertEqual(jpeg.info(s.jpeg), (2592, 2072, 3))
        self.assertEqual(jpeg.density(s.jpeg), (300, 300))

    def test_blank_detection(self):
        need_decoder()
        for name in sorted(RECEIPTS):
            try:
                front, back = PL.Page.from_side(load(name, "front")), PL.Page.from_side(load(name, "back"))
            except unittest.SkipTest:
                continue
            self.assertTrue(PL.is_blank(front, self.cfg), name)
            self.assertFalse(PL.is_blank(back, self.cfg), name)

    def test_size_method_cannot_see_blank_sides(self):
        # Color blank sides have about the same JPEG size as the printed sides.
        front = PL.Page.from_side(load("synth", "front"))
        self.assertGreater(front.bytes_per_pixel, 0.05)
        self.assertFalse(PL.is_blank(front, dict(self.cfg, blank_method="size")))

    def test_receipt_classification(self):
        need_decoder()
        pl = PL.Pipeline(self.cfg)
        prof = C._profile("p", {"duplex": False})
        for name in sorted(RECEIPTS):
            try:
                r = ScanResult(sheets=1, sides=[load(name, "front"), load(name, "back")])
            except unittest.SkipTest:
                continue
            pages = pl.pages(r, prof)
            self.assertEqual([p.side for p in pages], ["back"])
            a = PL.page_analysis(pages[0], self.cfg)
            self.assertLess(a.paper_width_mm, 100, name)         # receipt paper, not the 219 mm window
            self.assertTrue(pl.is_receipt(pages, prof), name)

    def test_duplex_document_and_pdf(self):
        pl = PL.Pipeline(self.cfg)
        r = ScanResult(sheets=1, sides=[load("synth", "front"), load("synth", "back")])
        doc = pl.process(r, C._profile("p", {"crop": False}))
        back = load("synth", "back").jpeg
        self.assertIn(back, doc.pdf)                             # the JPEG bytes are embedded unchanged
        self.assertIn(b"/MediaBox [0 0 622.08 497.28]", doc.pdf)  # 2592 x 2072 pixels at 300 dpi
        if PL.have_pillow() or PL.djpeg_path():
            self.assertEqual(doc.pages, 1)                       # the blank front is removed
            self.assertNotIn(load("synth", "front").jpeg, doc.pdf)


    def test_printed_side_up(self):
        # Hardware case: receipt printed side UP, simplex, paper_face "down": keep the printed side.
        need_decoder()
        front = load("synth", "back")                               # the print is on the front image now
        back = load("synth", "front")
        front.side, back.side = "front", "back"
        pl = PL.Pipeline(self.cfg)
        pages = pl.pages(ScanResult(sheets=1, sides=[front, back]), C._profile("p", {"duplex": False, "crop": False}))
        self.assertEqual([p.side for p in pages], ["front"])
        self.assertEqual(pages[0].jpeg, front.jpeg)

    def test_skew_of_real_receipts(self):
        need_decoder()
        for name in ("synth", "r3"):
            try:
                page = PL.Page.from_side(load(name, "back"))
            except unittest.SkipTest:
                continue
            g, w, h, dpi = PL.gray_image(page)
            a = PL.analyze(g, w, h, dpi)
            self.assertIsNotNone(a.skew, name)
            self.assertGreater(abs(a.skew), 1.0, name)           # these receipts are skewed
            ink = PL.ink_skew(g, w, h)
            self.assertAlmostEqual(ink, a.skew, delta=1.0, msg=name)    # print and edges agree
            fixed = PL.rotate_gray(g, w, h, a.skew)
            self.assertAlmostEqual(PL.analyze(fixed, w, h, dpi).skew, 0.0, delta=0.4, msg=name)

    def test_crop_receipt(self):
        need_decoder()
        if not (PL.have_pillow() or PL.jpegtran_path()):
            self.skipTest("crop needs Pillow or jpegtran")
        pl = PL.Pipeline(self.cfg)
        r = ScanResult(sheets=1, sides=[load("synth", "front"), load("synth", "back")])
        doc = pl.process(r, C._profile("p", {"duplex": False}))     # crop "auto": a receipt is cut
        self.assertTrue(doc.receipt)
        m = re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", doc.pdf)
        wmm, hmm = float(m.group(1)) * 25.4 / 72, float(m.group(2)) * 25.4 / 72
        self.assertLess(wmm, 110)                                # receipt width, not the 219 mm window
        self.assertGreater(hmm, 150)
        self.assertLess(len(doc.pdf), len(load("synth", "back").jpeg) * 0.7)
        doc2 = pl.process(r, C._profile("p", {"duplex": False, "crop": False}))
        self.assertIn(b"/MediaBox [0 0 622.08 497.28]", doc2.pdf)


    def test_crop_box_keeps_print_when_turned(self):
        # Real receipts turned by 8 degrees (pure Python turn of the 75 dpi gray copy):
        # no print outside the box, before and after the deskew.
        need_decoder()
        from test_pipeline import ink_outside
        for name in ("synth", "r3", "r2"):
            try:
                page = PL.Page.from_side(load(name, "back"))
            except unittest.SkipTest:
                continue
            g, w, h, dpi = PL.gray_image(page)
            turned = PL.rotate_gray(g, w, h, 8.0)
            a = PL.analyze(turned, w, h, dpi)
            self.assertEqual(ink_outside(turned, w, h, a.bbox), 0, name)
            self.assertIsNotNone(a.skew, name)
            fixed = PL.rotate_gray(turned, w, h, a.skew)
            b = PL.analyze(fixed, w, h, dpi)
            self.assertEqual(ink_outside(fixed, w, h, b.bbox), 0, name)
            first_ink_row = next(y for y in range(h) if min(fixed[y * w:(y + 1) * w]) < 110)
            self.assertLess(b.bbox[1], first_ink_row, name)


if __name__ == "__main__":
    unittest.main()
