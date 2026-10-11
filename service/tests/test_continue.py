"""Continue scanning (service.continue_window): more loads of paper into one document.

After a job, the document waits continue_window seconds. A press with the same
profile in this time scans more sheets into the same document, and the time
starts again. A press with another profile, the end of the time, or a stop
delivers the document. The tests use the test Clock (no real waits).
"""
import json
import os
import tempfile
import unittest

import support
from emulator import Emulator, Sheet
from httpfake import FakeServer, parse_multipart
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, sync, wait_for
from ix1300 import config as C
from ix1300 import jobs as J
from ix1300 import outputs as O
from ix1300.pipeline import Document

WINDOW = 30.0


def text_sheets(n):
    return [Sheet(front="text", back="text") for _ in range(n)]


class ContinueTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.work = self.tmp.name
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.work),
                          env={})
        self.cfg.service["continue_window"] = WINDOW
        self.out = RecordOutput()
        self.svc = None

    def tearDown(self):
        if self.svc:
            self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def start(self):
        self.svc = make_service(self.cfg, self.net, {"rec": self.out})
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None), "no reserve")

    def press(self, long=False):
        """One press. Return the log messages. Then the scan thread waits for the next trigger."""
        with self.assertLogs("ix1300.daemon", level="INFO") as logs:
            self.emu.press(long=long)
            self.assertTrue(sync(self.svc, self.emu))
        return [r.getMessage() for r in logs.records]

    def expire(self):
        """Let the window end. The scan thread then delivers the waiting document."""
        self.svc.clock.advance(WINDOW + 1)
        self.assertTrue(wait_for(lambda: self.svc.held is None), "the window did not end")
        self.svc.wait_idle()

    def jobs_in(self, kind):
        try:
            return os.listdir(os.path.join(self.work, kind))
        except OSError:
            return []

    # --- Tests ---
    def test_window_off_unchanged(self):
        self.cfg.service["continue_window"] = 0
        self.start()
        for i in range(2):
            self.emu.load(*text_sheets(1))
            msgs = self.press()
            self.svc.wait_idle()
            self.assertEqual(len(self.out.docs), i + 1)
            self.assertIsNone(self.svc.held)
            self.assertFalse(any("continue scanning" in m for m in msgs), msgs)
        self.assertEqual([d.pages for d in self.out.docs], [2, 2])
        self.assertIsNone(self.out.docs[0].parts)

    def test_two_parts_one_pdf(self):
        self.start()
        self.emu.load(*text_sheets(2))
        msgs = self.press()
        self.assertIn("continue scanning: part 1 added (2 sheets); delivery in 30 s", msgs)
        self.svc.wait_idle()
        self.assertEqual(self.out.docs, [])                      # the document waits
        self.assertEqual(len(self.jobs_in(J.JOBS)), 1)
        self.svc.clock.advance(WINDOW - 5)                       # inside the window
        self.emu.load(*text_sheets(1))
        msgs = self.press()
        self.assertIn("continue scanning: part 2 added (1 sheets); delivery in 30 s", msgs)
        self.svc.clock.advance(WINDOW - 5)                       # the window started again: still waiting
        self.assertTrue(sync(self.svc, self.emu))
        self.svc.wait_idle()
        self.assertEqual(self.out.docs, [])
        self.expire()
        self.assertEqual(len(self.out.docs), 1)
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 6)
        self.assertEqual([r["sheet"] for r in doc.page_records], [1, 1, 2, 2, 3, 3])   # scan order
        self.assertEqual([(p["part"], p["sheets"], p["paper_path"]) for p in doc.parts],
                         [(1, 2, "top"), (2, 1, "top")])
        self.assertEqual(self.emu.opcodes().count(0xD5), 2)      # two scan jobs on the scanner
        self.assertEqual(self.jobs_in(J.JOBS), [])

    def test_window_expires(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.expire()
        self.assertEqual([d.pages for d in self.out.docs], [2])
        # The next press starts a new document.
        self.emu.load(*text_sheets(1))
        self.press()
        self.assertIsNotNone(self.svc.held)
        self.expire()
        self.assertEqual([d.pages for d in self.out.docs], [2, 2])

    def test_other_profile_delivers_first(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.emu.load(Sheet(front="blank", back="text"))
        self.press(long=True)                                    # profile receipt
        self.svc.wait_idle()
        self.assertEqual([(d.profile, d.pages) for d in self.out.docs], [("doc", 2)])
        self.assertEqual(self.svc.held.job.profile.name, "receipt")
        self.expire()
        self.assertEqual([(d.profile, d.pages) for d in self.out.docs], [("doc", 2), ("receipt", 1)])

    def test_empty_chute_press_changes_nothing(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        deadline = self.svc.held.deadline
        msgs = self.press()                                      # no paper
        self.assertTrue(any("no job started" in m for m in msgs), msgs)
        self.assertEqual(self.svc.held.deadline, deadline)       # the window did not start again
        self.assertEqual(self.emu.opcodes().count(0xD5), 1)
        self.expire()
        self.assertEqual([d.pages for d in self.out.docs], [2])
        self.assertIsNone(self.out.docs[0].parts)                # one part: no "parts" data

    def test_front_slot_part(self):
        self.start()
        self.emu.load(*text_sheets(2))
        self.press()
        self.emu.load_front(Sheet(front="text", back="text"))
        msgs = self.press()
        self.assertIn("continue scanning: part 2 added (1 sheets); delivery in 30 s", msgs)
        self.emu.load_front(Sheet(front="text", back="blank"))
        self.press()
        self.expire()
        self.assertEqual(len(self.out.docs), 1)
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 7)                           # one blank side removed
        self.assertEqual([(p["sheets"], p["paper_path"]) for p in doc.parts],
                         [(2, "top"), (1, "front"), (1, "front")])
        self.assertEqual(doc.paper_path, "top")                  # the path of the first part

    def test_restart_during_window(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.emu.load(*text_sheets(1))
        self.press()
        # A crash: the service stops without the delivery. The job stays in work_dir/jobs.
        held = self.svc.held
        self.svc.held = None
        held.job.dir.unclaim()
        self.svc.stop()
        self.assertEqual(self.out.docs, [])
        (jid,) = self.jobs_in(J.JOBS)
        jd = J.JobDir.open(os.path.join(self.work, J.JOBS, jid))
        self.assertEqual(jd.m["state"], "scanned")
        self.assertEqual([p["sheets"] for p in jd.m["parts"]], [1, 1])
        self.assertEqual(len(jd.m["sheets"]), 2)
        # The next start delivers the document with the pages of the two parts.
        self.start()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 4)
        self.assertEqual(len(doc.parts), 2)
        self.assertIn("recovered after a service restart", doc.warnings)

    def test_stop_delivers_the_waiting_document(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.svc.stop()
        self.assertEqual([d.pages for d in self.out.docs], [2])
        self.assertEqual(self.jobs_in(J.JOBS), [])

    def test_scan_error_in_a_part_delivers_at_once(self):
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.emu.chunk_size = 5000
        self.emu.load(Sheet(front="text", back="text"), Sheet(jam_sense=True))
        self.press()
        self.svc.wait_idle()
        self.assertIsNone(self.svc.held)                         # no wait after an error
        self.assertEqual(len(self.out.docs), 1)
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 4)                           # part 1 and the complete sheet of part 2
        self.assertTrue(any(w.startswith("part 2: scan stopped after 1 sheets") for w in doc.warnings),
                        doc.warnings)

    def test_scan_error_in_a_part_without_partial_delivery(self):
        self.cfg.service["deliver_partial"] = False
        self.start()
        self.emu.load(*text_sheets(1))
        self.press()
        self.emu.load(Sheet(front="text"), Sheet(double_feed="back"))
        self.press()
        self.svc.wait_idle()
        self.assertEqual(self.out.docs, [])
        self.assertEqual(len(self.jobs_in(J.FAILED)), 1)        # all sheets are kept


class NextSheetTest(unittest.TestCase):
    def test_after_all_side_files(self):
        with tempfile.TemporaryDirectory() as work:
            jd = J.JobDir.create(work, "doc")
            self.assertEqual(jd.next_sheet(), 0)
            jd.m["sheets"].append(dict(front=dict(file="sheet001-front.jpg"), back=dict(file="sheet001-back.jpg")))
            self.assertEqual(jd.next_sheet(), 1)
            # A side file that the manifest does not know (crash or full disk) also counts.
            with open(os.path.join(jd.path, "sheet003-front.jpg"), "wb") as fh:
                fh.write(b"x")
            self.assertEqual(jd.next_sheet(), 3)


class ContinueConfigTest(unittest.TestCase):
    def test_default_off(self):
        self.assertEqual(C.load(env={}).service["continue_window"], 0)

    def test_range(self):
        for bad in ("-1", "3601", '"x"'):
            with self.assertRaises(C.ConfigError):
                C.load(text="[service]\ncontinue_window = %s\n[profiles.default]\n" % bad, env={})
        cfg = C.load(text="[service]\ncontinue_window = 12.5\n[profiles.default]\n", env={})
        self.assertEqual(cfg.service["continue_window"], 12.5)


class ContinueWebhookTest(unittest.TestCase):
    def test_parts_field(self):
        d = Document("scan-1.pdf", "Scan 1", "default", 2, False, created=0, pdf=b"%PDF-1.4\n%%EOF\n")
        with FakeServer() as srv:
            self.assertTrue(O.WebhookOutput("hook", {"url": srv.url}).deliver(d))
            d.parts = [dict(part=1, sheets=1, paper_path="top"), dict(part=2, sheets=1, paper_path="front")]
            self.assertTrue(O.WebhookOutput("hook", {"url": srv.url}).deliver(d))
            first = {p[0] for p in parse_multipart(srv.rec.requests[0])}
            second = {p[0]: p for p in parse_multipart(srv.rec.requests[1])}
        self.assertNotIn("parts", first)
        self.assertEqual(json.loads(second["parts"][3]), d.parts)


if __name__ == "__main__":
    unittest.main()
