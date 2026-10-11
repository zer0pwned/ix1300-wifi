"""Long page mode (length = "long"): documents up to 3,000 mm in the top chute.

Values from ScanSnap Home (SshResident, SsSvcPCWIFIFrmwk), PROTOCOL.md "Long page mode":
  - paper code 0x8003: E9 length 0x23680 (DoSetPreReadMode 0x10017d940), D4 params
    byte 13 = 0x80 (SetWindow 0x10017e090), byte 28 = 0 in the top chute, window
    length 0xA543 (DoSetWindowInfo 0x100156f00);
  - D4 byte 13 = 0x80 sets the mutual transfer (setParams 0x207f4): each image READ
    asks for 0x20000 bytes (TelRead 0x3a834);
  - one mutual READ loop gives both sides (getImageDataForMutualTransfer 0x241d4):
    READ window 0x00, then READ window 0x80, one sequence ID for both, front page
    c + 1, back page c + 2. A side ends with CHECK CONDITION, sense key 0 and EOM.
    The last chunk has (0x20000 - 2) - sense information bytes (0x24404-0x24414).
The scanner side of these values is not verified on the hardware [NV].
"""
import logging
import os
import struct
import tempfile
import time
import tracemalloc
import unittest

import support
from emulator import Emulator, Sheet, sense_bytes
from netsim import make_net
from test_daemon import RecordOutput, make_service, sync, wait_for
from test_front_slot import FRONT_PARAMS, TOP_PARAMS, e9_length
from test_protocol import scanner_for
from ix1300 import config as C
from ix1300 import jpeg
from ix1300 import pipeline as PL
from ix1300 import protocol as P

# The verified top chute values at 300 dpi, automatic colour and length (hardware, PROTOCOL.md).
NORMAL_E9 = bytes.fromhex("012C012C 00002880 00004350 0500") + bytes(18)
NORMAL_D4 = bytes.fromhex(
    "00030101 8800C180 80808080 80000000 00000000 00000000 00000000 00000030"
    "0010012C 012C0582 0D000000 28800000 A5430400 00000000 00000000 00000100"
    "00000000 00000000 00000000 00000000")
LONG_E9 = bytes.fromhex("012C012C 00002880 00023680 0500") + bytes(18)
LONG_PARAMS = bytes.fromhex("00030101 8800C180 80808080 80800000 00000000 00000000 00000000 00000030")


def image_reads(emu):
    """(window, length, page, sequence) of each image READ (data type 0)."""
    out = []
    for op, cdb in emu.log:
        if op == 0x28 and cdb[2] == 0x00:
            out.append((cdb[5], int.from_bytes(cdb[6:9], "big"), cdb[10], cdb[11]))
    return out


class LongSettingsTest(unittest.TestCase):
    def test_normal_values_byte_for_byte(self):
        s = P.ScanSettings(dpi=300)
        self.assertFalse(s.long_page)
        self.assertEqual(s.e9_data(), NORMAL_E9)
        self.assertEqual(s.d4_data(), NORMAL_D4)

    def test_long_e9_and_d4(self):
        s = P.ScanSettings(dpi=300, long_page=True)
        self.assertEqual(s.e9_data(), LONG_E9)
        d4 = s.d4_data()
        self.assertEqual(d4[:32], LONG_PARAMS)
        self.assertEqual(d4[13], 0x80)                    # mutual transfer
        self.assertEqual(d4[28], 0x00)                    # top chute: no byte 28
        self.assertEqual(d4[32:], NORMAL_D4[32:])         # same window: length 0xA543
        self.assertEqual(struct.unpack_from(">I", d4, 32 + 14)[0], 0xA543)
        # Only E9 +8 and D4 byte 13 change.
        diff = [i for i in range(0x50) if d4[i] != NORMAL_D4[i]]
        self.assertEqual(diff, [13])
        self.assertEqual([i for i in range(32) if LONG_E9[i] != NORMAL_E9[i]], [9, 10, 11])

    def test_long_other_dpi_and_colour(self):
        for dpi in (150, 200, 300):
            s = P.ScanSettings(dpi=dpi, color="gray", long_page=True)
            n = P.ScanSettings(dpi=dpi, color="gray")
            self.assertEqual(e9_length(s.e9_data()), 0x23680)
            self.assertEqual(s.d4_data()[:13] + s.d4_data()[14:], n.d4_data()[:13] + n.d4_data()[14:])

    def test_long_limits(self):
        with self.assertRaises(ValueError):
            P.ScanSettings(dpi=600, long_page=True)        # 3,000 mm at 600 dpi: more than 65,535 lines
        with self.assertRaises(ValueError):
            P.ScanSettings(long_page=True, path="front")   # the front slot takes no long paper
        with self.assertRaises(ValueError):
            P.ScanSettings(long_page=True, length=P.mm_to_length(300))   # the window stays 0xA543
        self.assertEqual(P.LONG_MAX_MM, 3000)
        self.assertGreaterEqual(P.LONG_PREREAD_LENGTH * 25.4 / 1200, P.LONG_MAX_MM)

    def test_sense_information(self):
        s = bytearray(sense_bytes(0, eom=True))
        s[3:7] = (0x1FFFE - 1000).to_bytes(4, "big")
        self.assertEqual(P.Sense(bytes(s)).info, 0x1FFFE - 1000)
        self.assertEqual(P.Sense(b"\x70\x00\x40").info, 0)

    def test_fix_height_can_grow_with_a_limit(self):
        import jpegsynth
        j = jpegsynth.synth(64, 400, 1, content_rows=400, density=0)
        self.assertEqual(jpeg.fix_height(j, 500), j)                      # default: only smaller
        g = jpeg.fix_height(j, 500, max_lines=600)
        self.assertEqual(jpeg.info(g), (64, 500, 1))
        self.assertEqual(jpeg.fix_height(j, 700, max_lines=600), j)       # more than the limit: no change
        self.assertEqual(jpeg.info(jpeg.fix_height(j, 100, max_lines=600)), (64, 100, 1))
        buf = bytearray(j)
        self.assertTrue(jpeg.fix_height_in_place(buf, 500, max_lines=600))
        self.assertEqual(bytes(buf), g)
        self.assertFalse(jpeg.fix_height_in_place(bytearray(j), 500))     # no limit: only smaller


class LongConfigTest(unittest.TestCase):
    def profile(self, **kw):
        text = "[profiles.default]\n[profiles.r]\n" + "".join("%s = %s\n" % (k, v) for k, v in kw.items())
        return C.load(text=text, env={}).profile("r")

    def test_long_value(self):
        p = self.profile(length='"long"')
        self.assertTrue(p.long_page)
        s = p.scan_settings("top")
        self.assertTrue(s.long_page)
        self.assertEqual(s.length, P.AUTO_LENGTH)
        self.assertFalse(self.profile(length='"auto"').long_page)
        self.assertFalse(self.profile(length="300").scan_settings().long_page)
        self.assertTrue(self.profile(length='"LONG"').long_page)

    def test_front_slot_falls_back_to_auto(self):
        p = self.profile(length='"long"')
        with self.assertLogs("ix1300.config", level="WARNING") as logs:
            s = p.scan_settings("front")
        self.assertFalse(s.long_page)
        self.assertEqual((s.path, s.length), ("front", P.AUTO_LENGTH))
        self.assertIn("front slot", logs.output[0])

    def test_bad_values(self):
        with self.assertRaises(C.ConfigError):
            self.profile(length='"longer"')
        with self.assertRaises(C.ConfigError):
            self.profile(length="3000")                    # millimetres stay 10..895
        with self.assertRaises(C.ConfigError) as cm:
            self.profile(length='"long"', dpi=600)
        self.assertIn("600", str(cm.exception))

    def test_environment_override(self):
        cfg = C.load(text="[profiles.default]\n[profiles.r]\ndpi = 300\n", env={"IX1300_PROFILES__R__LENGTH": "long"})
        self.assertTrue(cfg.profile("r").long_page)

    def test_example_file(self):
        cfg = C.load(os.path.join(support.ROOT, "config.example.toml"), env={})
        self.assertTrue(cfg.profile("receipt").long_page)
        self.assertEqual(cfg.profile("default").length, "auto")
        self.assertEqual(cfg.service["long_press_profile"], "receipt")

    def test_pipeline_decode_limit(self):
        cfg = C.load(text="", env={})
        self.assertEqual(cfg.pipeline["max_decode_mb"], 192)
        with self.assertRaises(C.ConfigError):
            C.load(text="[pipeline]\nmax_decode_mb = 0\n", env={})


class MutualReadTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.sc = scanner_for(self.emu, self.net)

    def tearDown(self):
        self.sc.close()
        self.emu.stop()

    def scan(self, *sheets, dpi=150, **kw):
        self.emu.load(*sheets)
        self.sc.reserve()
        try:
            return self.sc.scan(P.ScanSettings(dpi=dpi, long_page=True, **kw))
        finally:
            self.sc.close()
            self.sc.release()

    def expected(self, kind, mm, dpi=150, gray=False):
        """The JPEG of one side as the emulator makes it, with the real height."""
        pre = P.LONG_PREREAD_LENGTH * dpi // 1200
        lines = round(mm / 25.4 * dpi)
        return self.emu._jpeg(kind, P.WINDOW_WIDTH * dpi // 1200, pre, lines, gray, dpi), lines

    def test_settings_reach_the_scanner(self):
        self.scan(Sheet(front="text", back="text", length_mm=1200))
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x23680)
        self.assertEqual(self.emu.d4_log[-1][13], 0x80)
        self.assertTrue(self.emu.params["mutual"])

    def test_read_cdbs_and_order(self):
        self.emu.chunk_size = 50000
        self.scan(Sheet(front="text", back="blank", length_mm=1500))
        reads = image_reads(self.emu)
        self.assertTrue(all(length == 0x20000 for _w, length, _p, _s in reads))
        for op, cdb in self.emu.log:
            if op == 0x28 and cdb[2] == 0:
                self.assertEqual(cdb[:5] + cdb[9:10], bytes([0x28, 0, 0, 0, 0, 0]))
                self.assertEqual(cdb[6:9], b"\x02\x00\x00")
        # Window 0x00, then 0x80, one sequence ID for the two READs.
        # (A "temporary no data" reply gives the same READ again: compare without the copies.)
        steps = [r for i, r in enumerate(reads) if i == 0 or r != reads[i - 1]]
        self.assertEqual([(w, p, sq) for w, _l, p, sq in steps[:4]], [(0x00, 0, 0), (0x80, 1, 0), (0x00, 0, 1),
                                                                      (0x80, 1, 1)])
        fronts = [sq for w, _l, _p, sq in steps if w == 0x00]
        backs = [sq for w, _l, _p, sq in steps if w == 0x80]
        self.assertEqual(fronts, list(range(len(fronts))))
        self.assertEqual(backs, list(range(len(backs))))
        reads = steps
        # The blank back side ends first. Then only front READs come.
        self.assertLess(len(backs), len(fronts))
        last_back = max(i for i, r in enumerate(reads) if r[0] == 0x80)
        self.assertTrue(all(r[0] == 0x00 for r in reads[last_back + 1:]))
        # The pixel size for each side, after the two sides.
        ps = [(cdb[5], cdb[10]) for op, cdb in self.emu.log if op == 0x28 and cdb[2] == 0x80]
        self.assertEqual(ps, [(0x00, 0), (0x80, 1)])

    def test_split_front_and_back(self):
        r = self.scan(Sheet(front="text", back="blank", length_mm=2000), Sheet(front="blank", back="text",
                                                                               length_mm=900))
        self.assertEqual(r.sheets, 2)
        self.assertEqual([(s.side, s.page_id) for s in r.sides], [("front", 0), ("back", 1), ("front", 2),
                                                                  ("back", 3)])
        for side, (kind, mm) in zip(r.sides, (("text", 2000), ("blank", 2000), ("blank", 900), ("text", 900))):
            data, lines = self.expected(kind, mm)
            self.assertEqual(side.jpeg, jpeg.fix_height(data, lines))
            self.assertEqual((side.lines, jpeg.info(side.jpeg)[1]), (lines, lines))
            self.assertEqual(side.read["mode"], "mutual")
            self.assertEqual(side.read["cut"], 0)

    def test_last_chunk_padding_is_cut(self):
        # The last reply has the full 0x1FFFE bytes; the sense information gives the residue.
        self.emu.eom_pad = True
        r = self.scan(Sheet(front="text", back="text", length_mm=600))
        for side in r.sides:
            data, lines = self.expected("text", 600)
            self.assertEqual(side.jpeg, jpeg.fix_height(data, lines))
            self.assertGreater(side.read["cut"], 0)
            self.assertEqual(side.jpeg[-2:], b"\xff\xd9")

    def test_wrong_residue_keeps_the_end_of_image(self):
        # A residue that would cut the EOI marker: keep the data (the scanner value is not verified).
        self.emu.eom_info_error = 2
        r = self.scan(Sheet(front="text", back="text", length_mm=400))
        for side in r.sides:
            self.assertEqual(side.jpeg[-2:], b"\xff\xd9")
            self.assertEqual(side.read["cut"], 0)

    def test_sof_height_smaller_than_the_page(self):
        # If the scanner writes the window length (0xA543) into the SOF, the pixel size makes it larger.
        self.emu.long_sof = "window"
        r = self.scan(Sheet(front="text", back="blank", length_mm=2500))
        lines = round(2500 / 25.4 * 150)
        self.assertEqual([jpeg.info(s.jpeg)[1] for s in r.sides], [lines, lines])

    def test_3000_mm_at_300_dpi(self):
        self.emu.chunk_size = 0x1FFFE
        t0 = time.monotonic()
        r = self.scan(Sheet(front="receipt", back="blank", length_mm=3000), dpi=300)
        lines = round(3000 / 25.4 * 300)
        self.assertEqual(lines, 35433)
        self.assertEqual([jpeg.info(s.jpeg)[:2] for s in r.sides], [(2592, lines), (2592, lines)])
        self.assertLess(time.monotonic() - t0, 60)

    def test_temporary_no_data_and_busy(self):
        self.emu.temp_no_data = 2
        self.emu.busy = 1
        self.emu.chunk_size = 20000
        r = self.scan(Sheet(front="text", back="text", length_mm=1000))
        self.assertEqual(self.emu.stats["temp_no_data"], 4)
        for side in r.sides:
            self.assertNotIn(b"\xde\xad\xff\xd8", side.jpeg)
            data, lines = self.expected("text", 1000)
            self.assertEqual(side.jpeg, jpeg.fix_height(data, lines))

    def test_no_paper_after_first_sheet_ends_job(self):
        r = self.scan(Sheet(length_mm=1000))
        self.assertEqual((r.sheets, len(r.sides)), (1, 2))
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_paper_lost_in_back_side(self):
        with self.assertRaises(P.PaperLost) as cm:
            self.scan(Sheet(length_mm=1000), Sheet(length_mm=1000, paper_lost=True))
        self.assertEqual(cm.exception.result.sheets, 1)

    def test_jam_keeps_complete_sheets(self):
        with self.assertRaises(P.PaperJam) as cm:
            self.scan(Sheet(length_mm=1000), Sheet(length_mm=1000, jam_sense=True))
        self.assertEqual(cm.exception.result.sheets, 1)

    def test_side_size_limit(self):
        self.sc.max_side_bytes = 100000
        with self.assertRaises(P.ProtocolError):
            self.scan(Sheet(front="text", back="text", length_mm=1500))

    def test_normal_reads_unchanged(self):
        self.emu.load(Sheet(front="text", back="text", length_mm=300))
        self.sc.reserve()
        try:
            r = self.sc.scan(P.ScanSettings(dpi=150))
        finally:
            self.sc.close()
            self.sc.release()
        self.assertEqual(r.sheets, 1)
        reads = image_reads(self.emu)
        self.assertTrue(all(length == 0x300000 for _w, length, _p, _s in reads))
        wins = [w for w, _l, _p, _s in reads]
        self.assertEqual(wins, sorted(wins))                 # all front READs, then all back READs
        self.assertFalse(self.emu.params["mutual"])
        self.assertEqual(r.sides[0].read, {})


LONG_CONFIG = '''
[scanner]
ip = "127.0.0.1"
host_id = "%(hid)s"
host_ip = "127.0.0.1"
host_mac = "02:00:00:a1:b2:c3"
search_port = %(search)d
broadcast = "127.255.255.255"
trigger_port = 0
command_timeout = 10
data_timeout = 10
retry_min = 0.05
retry_max = 0.3

[service]
short_press_profile = "doc"
long_press_profile = "receipt"
work_dir = "%(work)s"
keep_raw = true

[pipeline]
rotate = 0

[profiles.doc]
dpi = 150

[profiles.receipt]
dpi = 300
duplex = false
color = "gray"
length = "long"
deskew = true
crop = true
receipt = "yes"
'''


class LongServiceTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = C.load(text=LONG_CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port,
                                                  work=self.tmp.name), env={})
        self.out = RecordOutput()
        self.svc = make_service(self.cfg, self.net, {"rec": self.out})
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None), "no reserve")

    def tearDown(self):
        self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def press(self, long=True):
        with self.assertLogs("ix1300", level="INFO") as logs:
            self.emu.press(long=long)
            self.assertTrue(sync(self.svc, self.emu, 120))
            self.svc.wait_idle()
        return [r.getMessage() for r in logs.records]

    def test_3000_mm_receipt_end_to_end(self):
        self.emu.chunk_size = 0x1FFFE
        self.emu.load(Sheet(front="blank", back="receipt", length_mm=3000))
        self.emu.prerender(self.cfg.profile("receipt").scan_settings("top"))
        tracemalloc.start()
        try:
            msgs = self.press()
            _, peak = tracemalloc.get_traced_memory()
        finally:
            tracemalloc.stop()
        self.assertTrue(any("long page mode" in m for m in msgs), msgs)
        self.assertEqual(len(self.out.docs), 1)
        doc = self.out.docs[0]
        self.assertEqual((doc.profile, doc.pages, doc.receipt), ("receipt", 1, True))
        self.assertEqual(doc.warnings, [])
        rec = doc.page_records[0]
        self.assertEqual(rec["side"], "back")
        self.assertEqual(rec["height"], 35433)               # 3,000 mm at 300 dpi
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x23680)
        self.assertEqual(self.emu.d4_log[-1][13], 0x80)
        self.assertTrue(all(length == 0x20000 for _w, length, _p, _s in image_reads(self.emu)))
        # raw.json has the scan mode and the READ data of each side.
        raw = os.path.join(self.tmp.name, "raw")
        import json
        with open(os.path.join(raw, os.listdir(raw)[0], "raw.json")) as fh:
            data = json.load(fh)
        self.assertEqual(data["profile_settings"]["length"], "long")
        self.assertTrue(data["long_page"])
        self.assertEqual(data["reads"][0]["back"]["mode"], "mutual")
        self.assertEqual(data["reads"][0]["back"]["lines"], 35433)
        # Python memory: a small multiple of the JPEG data (no decoded image in Python).
        side_bytes = sum(len(s["data"]) for s in self.emu._last_sides)
        if os.environ.get("IX1300_TEST_VERBOSE"):
            print("\n3000 mm receipt: JPEG %d bytes, Python peak %d bytes" % (side_bytes, peak))
        self.assertLess(peak, 8 * side_bytes + (8 << 20))

    def test_front_slot_uses_auto_length(self):
        self.emu.load_front(Sheet(front="text", back="text", length_mm=200))
        msgs = self.press()
        self.assertTrue(any("front slot" in m and "auto" in m for m in msgs), msgs)
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x4410)
        d4 = self.emu.d4_log[-1]
        self.assertEqual((d4[1], d4[4], d4[13]), (FRONT_PARAMS[1], FRONT_PARAMS[4], 0x00))
        self.assertTrue(all(length == 0x300000 for _w, length, _p, _s in image_reads(self.emu)))
        self.assertEqual(len(self.out.docs), 1)

    def test_short_press_normal_flow(self):
        self.emu.load(Sheet(front="text", back="text", length_mm=297))
        msgs = self.press(long=False)
        self.assertFalse(any("long page mode" in m for m in msgs))
        self.assertEqual(self.emu.e9_log[-1][8:12], b"\x00\x00\x43\x50")
        self.assertEqual(self.emu.d4_log[-1][:14], TOP_PARAMS)
        self.assertTrue(all(length == 0x300000 for _w, length, _p, _s in image_reads(self.emu)))
        self.assertEqual(self.out.docs[0].pages, 2)


class LongPipelineTest(unittest.TestCase):
    """The page work on a long page: limits for the full decode, crop with a small memory."""

    def page(self, w, h, nc=1, dpi=300):
        return PL.Page(PL.Blob(data=b""), w, h, nc, dpi)

    def test_long_page_test(self):
        self.assertFalse(PL.is_long_page(self.page(2592, 10571)))            # 895 mm: auto maximum
        self.assertTrue(PL.is_long_page(self.page(2592, 35433)))             # 3,000 mm
        self.assertTrue(PL.is_long_page(self.page(1296, 6000, dpi=150)))     # 1,016 mm

    def test_decode_estimate(self):
        # Pillow keeps "L" with 1 byte and "RGB" with 4 bytes for each pixel. Deskew needs about 3 copies.
        self.assertEqual(PL.decode_bytes(self.page(1000, 1000, 1)), 3 * 1000 * 1000)
        self.assertEqual(PL.decode_bytes(self.page(1000, 1000, 3)), 3 * 4 * 1000 * 1000)
        cfg = {"max_decode_mb": 192}
        self.assertTrue(PL.decode_allowed(self.page(945, 35433, 1), cfg))      # a cropped gray receipt
        self.assertFalse(PL.decode_allowed(self.page(2592, 35433, 1), cfg))    # the full window
        self.assertFalse(PL.decode_allowed(self.page(945, 35433, 3), cfg))     # a cropped colour receipt

    def test_pillow_pixel_limit(self):
        self.assertGreaterEqual(PL.PIL_MAX_PIXELS, 2592 * (P.LONG_PREREAD_LENGTH * 300 // 1200))
        self.assertGreaterEqual(PL.PIL_MAX_PIXELS, 5184 * (0xA543 * 600 // 1200))
        if PL.Image is not None:
            self.assertEqual(PL.Image.MAX_IMAGE_PIXELS, PL.PIL_MAX_PIXELS)

    @unittest.skipUnless(PL.djpeg_path() and PL.cjpeg_path(), "needs djpeg and cjpeg")
    def test_long_crop_streams_without_jpegtran(self):
        import subprocess
        w, h, x0, x1 = 1296, 17717, 400, 873                  # 3,000 mm at 150 dpi, 80 mm paper
        row = bytearray([215]) * w
        row[x0:x1] = b"\xfa" * (x1 - x0)
        ink = bytearray(row)
        ink[x0 + 40:x0 + 300] = b"\x20" * 260
        pgm = b"P5\n%d %d\n255\n" % (w, h) + b"".join(bytes(ink if (y // 10) % 3 == 0 else row)
                                                       for y in range(h))
        data = subprocess.run([PL.cjpeg_path(), "-quality", "85"], input=pgm, capture_output=True,
                              check=True).stdout
        page = PL.Page.from_jpeg(data, 150)
        calls = []
        old = PL.jpegtran_path
        PL.jpegtran_path = lambda: calls.append(1) or old()
        try:
            out = PL.deskew_and_crop(page, {"max_decode_mb": 192}, deskew=False, crop=True)
        finally:
            PL.jpegtran_path = old
        self.assertEqual(calls, [])                           # jpegtran keeps the full page in memory
        self.assertEqual(out.height, h)
        self.assertLess(out.width, 600)
        self.assertGreater(out.width, x1 - x0)
        self.assertEqual(out.info["crop_method"], "stream")

    def test_long_deskew_skipped_when_too_large(self):
        page = self.page(2592, 35433, 3)
        page.analysis = PL.Analysis(ink_ratio=0.02, paper_width_mm=219, paper_length_mm=3000,
                                    bbox=(), size=(648, 8858), skew=1.5, paper_level=240)
        with self.assertLogs("ix1300.pipeline", level="INFO") as logs:
            out = PL.deskew_and_crop(page, {"max_decode_mb": 192}, deskew=True, crop=False)
        self.assertIs(out, page)
        self.assertFalse(page.info["deskew_applied"])
        self.assertIn("deskew_skipped", page.info)
        self.assertTrue(any("deskew" in m for m in logs.output))


if __name__ == "__main__":
    unittest.main()
