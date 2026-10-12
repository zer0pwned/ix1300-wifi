"""Front slot (return path) scans: detection, E9/D4 values, one-sheet job, profile, metadata.

Hardware facts (PROTOCOL.md, front slot): before the job C2 shows byte 2 = 0x80
and byte 3 = 0x81 (hopper empty, exit sensor). The job uses E9 length 0x4410
and D4 params byte 1 = 0x06, byte 4 = 0x00. After one sheet E0 gives "no paper".
"""
import json
import os
import struct
import tempfile
import unittest

import support
from emulator import Emulator, Sheet
from httpfake import FakeServer, parse_multipart
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, sync, wait_for
from test_protocol import scanner_for
from ix1300 import config as C
from ix1300 import jobs as J
from ix1300 import outputs as O
from ix1300 import protocol as P
from ix1300.pipeline import Document

TOP_PARAMS = bytes.fromhex("00 03 01 01 88 00 C1 80 80 80 80 80 80 00")
FRONT_PARAMS = bytes.fromhex("00 06 01 01 00 00 C1 80 80 80 80 80 80 00")


def e9_length(data):
    return struct.unpack_from(">I", data, 8)[0]


class FrontSettingsTest(unittest.TestCase):
    def test_front_slot_e9_d4_values(self):
        top, front = P.ScanSettings(dpi=300), P.ScanSettings(dpi=300, path="front")
        self.assertEqual(e9_length(front.e9_data()), 0x4410)
        self.assertEqual(front.d4_data()[:14], FRONT_PARAMS)
        # Only these three values change.
        self.assertEqual(front.e9_data()[:8] + front.e9_data()[12:], top.e9_data()[:8] + top.e9_data()[12:])
        self.assertEqual(front.d4_data()[14:], top.d4_data()[14:])
        with self.assertRaises(ValueError):
            P.ScanSettings(path="side")

    def test_top_chute_values_unchanged(self):
        s = P.ScanSettings(dpi=300)
        self.assertEqual(s.path, "top")
        self.assertEqual(e9_length(s.e9_data()), 0x4350)
        self.assertEqual(s.d4_data()[:14], TOP_PARAMS)
        self.assertEqual(C.Profile(name="x").scan_settings().path, "top")
        self.assertEqual(C.Profile(name="x").scan_settings(path="front").path, "front")

    def test_paper_path_from_c2(self):
        def st(b2, b3):
            return P.HwStatus(bytes([0, 0, b2, b3, 0]))
        self.assertEqual(st(0x80, 0x00).paper_path, "top")         # paper in the hopper
        self.assertEqual(st(0x80, 0x81).paper_path, "front")       # measured: front slot sheet
        self.assertEqual(st(0x80, 0x80).paper_path, "front")       # hopper empty + feed sensor
        self.assertIsNone(st(0x00, 0x81).paper_path)               # stale exit bit only: no paper
        self.assertIsNone(st(0x00, 0x80).paper_path)               # clear path
        self.assertTrue(st(0x80, 0x81).front_slot)
        self.assertFalse(st(0x00, 0x81).front_slot)


class FrontScannerTest(unittest.TestCase):
    """Scanner.scan with the emulator: the front-slot values go to the scanner, one sheet ends the job."""

    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.sc = scanner_for(self.emu, self.net)

    def tearDown(self):
        self.sc.close()
        self.emu.stop()

    def test_c2_shows_front_slot_sheet(self):
        self.emu.load_front(Sheet())
        st = self.sc.hw_status()
        self.assertEqual((st.raw[2], st.raw[3]), (0x80, 0x81))
        self.assertEqual(st.paper_path, "front")

    def test_one_sheet_job(self):
        self.emu.load_front(Sheet(front="text", back="text"))
        self.sc.reserve()
        try:
            r = self.sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(400), path="front"))
        finally:
            self.sc.close()
            self.sc.release()
        self.assertEqual(r.sheets, 1)
        self.assertEqual([(s.side, s.page_id) for s in r.sides], [("front", 0), ("back", 1)])
        self.assertEqual(self.emu.opcodes().count(0xE0), 2)      # the second E0 gets "no paper"
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x4410)
        self.assertEqual(self.emu.d4_log[-1][:14], FRONT_PARAMS)
        self.assertFalse(r.jam_warning)
        self.assertEqual(self.emu.front, [])
        st = self.sc.hw_status()
        self.assertEqual((st.raw[2], st.raw[3]), (0x00, 0x80))  # the path is clear after the job


class FrontServiceTest(unittest.TestCase):
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

    def start(self, out=None):
        self.svc = make_service(self.cfg, self.net, {"rec": out or self.out})
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None), "no reserve")

    def press(self, long=False):
        with self.assertLogs("ix1300.daemon", level="INFO") as logs:
            self.emu.press(long=long)
            self.assertTrue(sync(self.svc, self.emu))
        self.svc.wait_idle()
        return [r.getMessage() for r in logs.records]

    def test_front_slot_job(self):
        self.cfg.service.update(keep_raw=True)
        self.start()
        self.emu.load_front(Sheet(front="text", back="text"))
        msgs = self.press()
        self.assertIn("scan button (short press): profile doc, front slot", msgs)
        self.assertEqual(len(self.out.docs), 1)
        doc = self.out.docs[0]
        self.assertEqual((doc.profile, doc.pages, doc.paper_path), ("doc", 2, "front"))
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x4410)
        self.assertEqual(self.emu.d4_log[-1][:14], FRONT_PARAMS)
        self.assertEqual(self.emu.opcodes().count(0xD5), 1)
        self.assertEqual(self.emu.opcodes().count(0xE0), 2)      # one sheet, then "no paper"
        (rid,) = os.listdir(os.path.join(self.work, "raw"))
        with open(os.path.join(self.work, "raw", rid, "raw.json")) as fh:
            self.assertEqual(json.load(fh)["paper_path"], "front")

    def test_front_slot_manifest_and_retry_document(self):
        class Fail(RecordOutput):
            def deliver(self, doc):
                self.last_error, self.last_permanent = "down", False
                return False
        self.start(out=Fail())
        self.emu.load_front(Sheet(front="text", back="text"))
        self.press()
        (jid,) = os.listdir(os.path.join(self.work, J.UNSENT))
        jd = J.JobDir.open(os.path.join(self.work, J.UNSENT, jid))
        self.assertEqual(jd.m["paper_path"], "front")
        self.assertEqual(jd.m["document"]["paper_path"], "front")
        self.assertEqual(self.svc.document_for(jd).paper_path, "front")

    def test_front_slot_profile(self):
        self.cfg.service["front_slot_profile"] = "receipt"
        self.start()
        self.emu.load_front(Sheet(front="blank", back="text"))
        msgs = self.press()
        self.assertIn("scan button (short press): profile receipt, front slot", msgs)
        self.assertEqual(self.out.docs[0].profile, "receipt")
        self.assertEqual(self.out.docs[0].paper_path, "front")
        # A top chute job still uses the button profile.
        self.emu.load(Sheet(front="blank", back="text"))
        msgs = self.press()
        self.assertIn("scan button (short press): profile doc, top chute", msgs)
        self.assertEqual(self.out.docs[1].profile, "doc")

    def test_stale_exit_bit_starts_no_job(self):
        # Seen after a halt [HW]: byte 2 = 0x00, byte 3 = 0x81 with no paper.
        self.start()
        self.emu.exit_sensor = True
        msgs = self.press()
        self.assertIn(0xC2, self.emu.opcodes())
        self.assertNotIn(0xD5, self.emu.opcodes())
        self.assertNotIn(0xE0, self.emu.opcodes())
        self.assertTrue(any("no paper in the chute or the front slot; no job started" in m for m in msgs), msgs)
        self.assertEqual(self.out.docs, [])

    def test_top_chute_unchanged(self):
        self.start()
        self.emu.load(Sheet(front="text", back="text"))
        msgs = self.press()
        self.assertIn("scan button (short press): profile doc, top chute", msgs)
        self.assertEqual(e9_length(self.emu.e9_log[-1]), 0x4350)
        self.assertEqual(self.emu.d4_log[-1][:14], TOP_PARAMS)
        self.assertEqual(self.out.docs[0].paper_path, "top")


class FrontConfigTest(unittest.TestCase):
    def test_front_slot_profile_default(self):
        self.assertEqual(C.load(env={}).service["front_slot_profile"], "")

    def test_front_slot_profile_must_exist(self):
        with self.assertRaises(C.ConfigError) as cm:
            C.load(text='[service]\nfront_slot_profile = "card"\n', env={})
        self.assertIn("front_slot_profile", str(cm.exception))
        cfg = C.load(text='[service]\nfront_slot_profile = "card"\n[profiles.default]\n[profiles.card]\ndpi = 600\n',
                     env={})
        self.assertEqual(cfg.service["front_slot_profile"], "card")


class FrontWebhookTest(unittest.TestCase):
    def test_paper_path_field(self):
        d = Document("scan-1.pdf", "Scan 1", "default", 2, False, created=0, pdf=b"%PDF-1.4\n%%EOF\n")
        self.assertEqual(d.paper_path, "top")
        d.paper_path = "front"
        with FakeServer() as srv:
            self.assertTrue(O.WebhookOutput("hook", {"url": srv.url, "standard_fields": True}).deliver(d))
            parts = {p[0]: p for p in parse_multipart(srv.rec.requests[0])}
        self.assertEqual(parts["paper_path"][3], b"front")


if __name__ == "__main__":
    unittest.main()
