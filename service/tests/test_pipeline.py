import os
import re
import unittest

import support
import jpegsynth
from ix1300 import config as C, pipeline as PL
from ix1300.protocol import ScanResult, Side


def side(sheet, name, kind="text", w=640, h=880, comps=1, dpi=100):
    density = {"blank": 0.0, "faint": 0.002}.get(kind, 0.5)
    data = jpegsynth.synth(w, h, comps, density=density, dpi=dpi, seed=sheet + 1)
    return Side(sheet=sheet, side=name, page_id=0, jpeg=data, width=w, lines=h, dpi=dpi)


def profile(**kw):
    return C._profile("p", kw)


def parse_pdf(pdf):
    """Check the xref table. Return a dict object number -> object bytes."""
    m = re.search(rb"startxref\n(\d+)\n%%EOF\n$", pdf)
    assert m, "no startxref"
    xref = int(m.group(1))
    assert pdf[xref:xref + 4] == b"xref"
    lines = pdf[xref:].split(b"\n")
    first, count = map(int, lines[1].split())
    objs = {}
    for i in range(1, count):
        entry = lines[2 + i]
        off = int(entry[:10])
        assert entry.endswith(b" n "), entry
        assert pdf[off:].startswith(b"%d 0 obj\n" % i), (i, pdf[off:off + 20])
        end = pdf.index(b"\nendobj\n", off)
        objs[i] = pdf[off:end]
    assert re.search(rb"trailer\n<< /Size %d /Root 1 0 R" % count, pdf)
    return objs


class PdfTest(unittest.TestCase):
    def test_structure(self):
        pages = [PL.Page.from_side(side(0, "back", dpi=100)),
                 PL.Page.from_side(side(1, "back", w=320, h=1600, comps=3, dpi=200))]
        pages[1].rotate = 180
        pdf = PL.write_pdf(pages, title="Test (1) é", created=0)
        self.assertTrue(pdf.startswith(b"%PDF-1.4\n"))
        objs = parse_pdf(pdf)
        self.assertIn(b"/Count 2", objs[2])
        self.assertIn(b"/Title <FEFF", objs[3])
        # page 1: 640 x 880 pixels at 100 dpi = 460.8 x 633.6 pt
        self.assertIn(b"/MediaBox [0 0 460.8 633.6]", objs[4])
        self.assertNotIn(b"/Rotate", objs[4])
        self.assertIn(b"/MediaBox [0 0 115.2 576]", objs[7])
        self.assertIn(b"/Rotate 180", objs[7])
        for num, pg in ((6, pages[0]), (9, pages[1])):
            body = objs[num]
            self.assertIn(b"/Filter /DCTDecode", body)
            self.assertIn(b"/Length %d" % len(pg.jpeg), body)
            start = body.index(b"stream\n") + 7
            self.assertEqual(body[start:start + len(pg.jpeg)], pg.jpeg)       # no new encode
        self.assertIn(b"/ColorSpace /DeviceGray", objs[6])
        self.assertIn(b"/ColorSpace /DeviceRGB", objs[9])
        content = objs[5]
        self.assertIn(b"q 460.8 0 0 633.6 0 0 cm /Im0 Do Q", content)

    def test_empty(self):
        with self.assertRaises(ValueError):
            PL.write_pdf([])


class BlankTest(unittest.TestCase):
    cfg = dict(C.DEFAULTS["pipeline"])

    def test_pixel_method(self):
        if not (PL.have_pillow() or PL.djpeg_path()):
            self.skipTest("no JPEG decoder (Pillow or djpeg)")
        for comps in (1, 3):
            blank = PL.Page.from_side(side(0, "front", "blank", comps=comps))
            text = PL.Page.from_side(side(0, "back", "text", comps=comps))
            self.assertTrue(PL.is_blank(blank, self.cfg), comps)
            self.assertFalse(PL.is_blank(text, self.cfg), comps)

    def test_size_method(self):
        cfg = dict(self.cfg, blank_method="size", blank_bytes_per_pixel=0.035)
        for comps in (1, 3):
            self.assertTrue(PL.is_blank(PL.Page.from_side(side(0, "front", "blank", comps=comps)), cfg))
            self.assertFalse(PL.is_blank(PL.Page.from_side(side(0, "back", "text", comps=comps)), cfg))

    def test_fallback_without_decoder(self):
        old = PL.Image, os.environ.get("IX1300_DJPEG")
        PL.Image = None
        os.environ["IX1300_DJPEG"] = "ix1300-no-djpeg"
        try:
            page = PL.Page.from_side(side(0, "back", "text"))
            self.assertIsNone(PL.page_analysis(page, self.cfg))
            self.assertFalse(PL.is_blank(page, self.cfg))           # size method
        finally:
            PL.Image = old[0]
            if old[1] is None:
                del os.environ["IX1300_DJPEG"]
            else:
                os.environ["IX1300_DJPEG"] = old[1]

    def test_analyze(self):
        # 100 x 400 pixels at 50 dpi: gray backing, paper column 40 px wide, 5 % print.
        w, h = 100, 400
        px = bytearray([215]) * (w * h)
        for y in range(50, 350):
            for x in range(30, 70):
                px[y * w + x] = 240
            if y % 20 == 0:
                for x in range(35, 65):
                    px[y * w + x] = 30
        a = PL.analyze(bytes(px), w, h, 50)
        self.assertAlmostEqual(a.paper_width_mm, 40 * 25.4 / 50, places=3)
        self.assertAlmostEqual(a.paper_length_mm, 300 * 25.4 / 50, places=3)
        self.assertAlmostEqual(a.ink_ratio, 15 * 30 / (300 * 40.0), places=4)

    def test_pgm(self):
        px, w, h = PL._parse_pgm(b"P5\n# c\n3 2\n255\n" + bytes(range(6)))
        self.assertEqual((w, h, px), (3, 2, bytes(range(6))))


class SelectionTest(unittest.TestCase):
    def result(self):
        r = ScanResult(sheets=2)
        r.sides = [side(0, "front", "blank"), side(0, "back"), side(1, "front"), side(1, "back")]
        return r

    def test_order_face_down(self):
        got = [(s.sheet, s.side) for s in PL.select_sides(self.result(), profile(duplex=True))]
        self.assertEqual(got, [(0, "back"), (0, "front"), (1, "back"), (1, "front")])

    def test_order(self):
        got = [(s.sheet, s.side) for s in PL.select_sides(self.result(), profile(duplex=False))]
        self.assertEqual(got, [(0, "back"), (0, "front"), (1, "back"), (1, "front")])
        got = [s.side for s in PL.select_sides(self.result(), profile(paper_face="up"))][:2]
        self.assertEqual(got, ["front", "back"])

    def need_decoder(self):
        if not (PL.have_pillow() or PL.djpeg_path()):
            self.skipTest("no JPEG decoder")

    def test_simplex_keeps_side_with_print(self):
        # Hardware case: a receipt loaded printed side UP with paper_face "down".
        self.need_decoder()
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        r = ScanResult(sheets=2, sides=[side(0, "front", "text"), side(0, "back", "blank"),
                                        side(1, "front", "blank"), side(1, "back", "text")])
        for face in ("down", "up"):
            pages = pl.pages(r, profile(duplex=False, paper_face=face))
            self.assertEqual([(p.sheet, p.side) for p in pages], [(0, "front"), (1, "back")], face)

    def test_simplex_without_decoder_uses_order(self):
        old = PL.Image, os.environ.get("IX1300_DJPEG")
        PL.Image = None
        os.environ["IX1300_DJPEG"] = "ix1300-no-djpeg"
        try:
            pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0, blank_method="size"))
            r = ScanResult(sheets=1, sides=[side(0, "front", "text"), side(0, "back", "blank")])
            self.assertEqual([p.side for p in pl.pages(r, profile(duplex=False))], ["back"])
            self.assertEqual([p.side for p in pl.pages(r, profile(duplex=False, paper_face="up"))], ["front"])
        finally:
            PL.Image = old[0]
            if old[1] is None:
                del os.environ["IX1300_DJPEG"]
            else:
                os.environ["IX1300_DJPEG"] = old[1]

    def test_all_blank_fallback_uses_ink(self):
        self.need_decoder()
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        r = ScanResult(sheets=1, sides=[side(0, "front", "faint"), side(0, "back", "blank")])
        for prof in (profile(), profile(duplex=False)):
            pages = pl.pages(r, prof)
            self.assertEqual([p.side for p in pages], ["front"])
            self.assertTrue(0 < pages[0].analysis.ink_ratio < 0.004)

    def test_pipeline_blank_skip(self):
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        pages = pl.pages(self.result(), profile())
        self.assertEqual([(p.sheet, p.side) for p in pages], [(0, "back"), (1, "back"), (1, "front")])
        pages = pl.pages(self.result(), profile(blank_skip=False))
        self.assertEqual(len(pages), 4)

    def test_all_blank_keeps_main_side(self):
        r = ScanResult(sheets=1, sides=[side(0, "front", "blank"), side(0, "back", "blank")])
        pages = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0)).pages(r, profile())
        self.assertEqual([p.side for p in pages], ["back"])

    def test_forced_rotation(self):
        r = ScanResult(sheets=1, sides=[side(0, "back")])
        pages = PL.Pipeline(dict(C.DEFAULTS["pipeline"])).pages(r, profile(rotate=180))
        self.assertEqual(pages[0].rotate, 180)
        pages = PL.Pipeline(dict(C.DEFAULTS["pipeline"])).pages(r, profile())
        self.assertEqual(pages[0].rotate, 0)          # auto without tesseract: no change


class ReceiptTest(unittest.TestCase):
    def test_ratio(self):
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        a4 = ScanResult(sheets=1, sides=[side(0, "back", w=640, h=905)])
        long = ScanResult(sheets=1, sides=[side(0, "back", w=640, h=2400)])
        self.assertFalse(pl.process(a4, profile()).receipt)
        doc = pl.process(long, profile())
        self.assertTrue(doc.receipt)
        self.assertEqual(doc.pages, 1)
        self.assertFalse(pl.process(long, profile(receipt="no")).receipt)
        self.assertTrue(pl.process(a4, profile(receipt="yes")).receipt)

    def test_title(self):
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0, title_format="{profile} %Y"))
        doc = pl.process(ScanResult(sheets=1, sides=[side(0, "back")]), profile(), now=0)
        self.assertRegex(doc.title, r"^p 19(69|70)$")
        self.assertTrue(doc.filename.endswith(".pdf"))



class StoreTest(unittest.TestCase):
    def test_spill_and_stream(self):
        import tempfile
        with tempfile.TemporaryDirectory() as tmp:
            store = PL.PageStore(tmp, limit_bytes=150000)
            r = ScanResult(sheets=3, sides=[side(i, n) for i in range(3) for n in ("front", "back")])
            pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
            pages = pl.pages(r, profile(blank_skip=False), store)
            self.assertGreater(store.spilled, 0)
            self.assertLessEqual(store.in_memory, 150000)
            doc = pl.finish(pages, profile(blank_skip=False), out_dir=tmp)
            self.assertTrue(os.path.exists(os.path.join(tmp, doc.filename)))
            pdf = doc.pdf
            objs = parse_pdf(pdf)
            self.assertIn(b"/Count 6", objs[2])
            for p in pages:
                self.assertIn(p.jpeg, pdf)
            self.assertEqual(doc.size, len(pdf))
            self.assertEqual(b"".join(doc.chunks()), pdf)

    def test_finish_without_pages(self):
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        self.assertIsNone(pl.finish([], profile()))
        self.assertIsNone(pl.process(ScanResult(), profile()))



def paper_image(w=300, h=500, angle=0.0, x0=90, x1=210, y0=40, y1=460):
    """Gray image: backing 215, paper 240 with lines of print, turned by ANGLE (Pillow direction)."""
    px = bytearray([215]) * (w * h)
    for y in range(y0, y1):
        for x in range(x0, x1):
            px[y * w + x] = 240
        if (y - y0) % 12 in (4, 5) and y < y1 - 10:
            for x in range(x0 + 10, x1 - 10 - (y % 37)):
                px[y * w + x] = 30
    return PL.rotate_gray(bytes(px), w, h, angle) if angle else bytes(px)


class SkewTest(unittest.TestCase):
    """Deskew direction: the measured angle makes the page straight with Image.rotate(angle)."""

    def test_rotate_gray_direction(self):
        # A positive angle turns counter-clockwise: the top end of a vertical line moves to the left.
        w = h = 41
        px = bytearray([0]) * (w * h)
        for y in range(h):
            px[y * w + 19:y * w + 22] = b"\xff\xff\xff"
        out = PL.rotate_gray(bytes(px), w, h, 30)
        top = [x for x in range(w) if out[2 * w + x] == 255]
        bottom = [x for x in range(w) if out[(h - 3) * w + x] == 255]
        self.assertLess(top[0], 19)
        self.assertGreater(bottom[-1], 21)

    def test_edges_and_print(self):
        for turn in (4.0, -4.0, 2.5):
            g = paper_image(angle=turn)
            a = PL.analyze(g, 300, 500, 75)
            self.assertAlmostEqual(a.skew, -turn, delta=0.3, msg=turn)          # edges
            self.assertAlmostEqual(PL.ink_skew(g, 300, 500), -turn, delta=0.5, msg=turn)   # print
            fixed = PL.rotate_gray(g, 300, 500, a.skew)
            self.assertAlmostEqual(PL.analyze(fixed, 300, 500, 75).skew, 0.0, delta=0.3, msg=turn)

    def test_straight(self):
        a = PL.analyze(paper_image(), 300, 500, 75)
        self.assertAlmostEqual(a.skew, 0.0, delta=0.1)
        self.assertEqual(a.bbox, (90, 40, 210, 460))

    def test_edge_at_border_not_used(self):
        # Paper as wide as the image: no edges, so no edge angle.
        g = paper_image(x0=0, x1=300)
        self.assertIsNone(PL.analyze(g, 300, 500, 75).skew)


class StoreTest2(unittest.TestCase):
    def test_crop_box(self):
        a = PL.Analysis(0.01, 40, 140, bbox=(30, 10, 70, 150), size=(100, 160))
        pg = PL.Page(PL.Blob(data=b""), 400, 640, 3, 300)
        x0, y0, x1, y1 = PL._crop_box(a, pg, margin_mm=0)
        self.assertEqual((x0, y0, x1, y1), (120, 40, 280, 600))
        full = PL.Analysis(0.01, 40, 140, bbox=(0, 0, 100, 160), size=(100, 160))
        self.assertIsNone(PL._crop_box(full, pg))



def ink_outside(gray, w, h, box, ink_level=110):
    """Number of ink pixels outside BOX."""
    mask = gray.translate(bytes(1 if v < ink_level else 0 for v in range(256)))
    x0, y0, x1, y1 = box
    inside = sum(mask.count(1, y * w + x0, y * w + x1) for y in range(y0, y1))
    return mask.count(1) - inside


class CropBoxTest(unittest.TestCase):
    def torn_receipt(self):
        """75 dpi: paper x 100..199, slanted and torn top edge (y 40..73), print 1 mm below its lowest point."""
        w, h = 300, 500
        px = bytearray([215]) * (w * h)
        lowest = 0
        for x in range(100, 200):
            top = 40 + (x - 100) * 3 // 10 + (3 if x % 7 < 3 else 0)
            lowest = max(lowest, top)
            for y in range(top, 460):
                px[y * w + x] = 240
        first = lowest + 3                                   # 1 mm at 75 dpi
        for y in range(first, first + 3):
            for x in range(105, 195):
                px[y * w + x] = 30
        for y in range(first + 20, 440, 15):
            for x in range(110, 190):
                px[y * w + x] = 30
        return bytes(px), w, h, first

    def test_torn_top_edge(self):
        g, w, h, first = self.torn_receipt()
        a = PL.analyze(g, w, h, 75)
        x0, y0, x1, y1 = a.bbox
        self.assertLessEqual(y0, 40)                         # outer bound: the highest paper point
        self.assertLess(y0, first)
        self.assertEqual((x0, x1), (100, 200))
        self.assertGreaterEqual(y1, 460)
        self.assertEqual(ink_outside(g, w, h, a.bbox), 0)
        # Full size (300 dpi = 4 x): the crop box with margin keeps the first print row.
        page = PL.Page(PL.Blob(data=b""), w * 4, h * 4, 3, 300)
        box = PL._crop_box(a, page)
        self.assertLess(box[1], first * 4 - 20)

    def test_thin_ink_at_edge(self):
        # A thin stroke above the paper top (fewer pixels than a paper row): the box grows over it.
        g, w, h, first = self.torn_receipt()
        px = bytearray(g)
        for y in range(30, 45):
            px[y * w + 101] = 20
        a = PL.analyze(bytes(px), w, h, 75)
        self.assertLessEqual(a.bbox[1], 30)
        self.assertEqual(ink_outside(bytes(px), w, h, a.bbox), 0)

    def test_skewed_synthetic(self):
        for turn in (8.0, -8.0):
            g = paper_image(angle=turn)
            a = PL.analyze(g, 300, 500, 75)
            self.assertEqual(ink_outside(g, 300, 500, a.bbox), 0, turn)
            fixed = PL.rotate_gray(g, 300, 500, a.skew)
            b = PL.analyze(fixed, 300, 500, 75)
            self.assertEqual(ink_outside(fixed, 300, 500, b.bbox), 0, turn)


if __name__ == "__main__":
    unittest.main()
