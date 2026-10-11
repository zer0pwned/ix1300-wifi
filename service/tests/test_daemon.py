import os
import tempfile
import threading
import time
import tracemalloc
import unittest

import support
from emulator import Emulator, Sheet
from netsim import make_net
from ix1300 import config as C
from ix1300 import daemon as D
from ix1300.daemon import Service
from ix1300 import protocol as P
from ix1300.protocol import KEY


def wait_for(cond, timeout=10.0):
    """Wait for a final state. The timeout is only a limit for a failure, not a part of the test."""
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.005)
    return False


class Clock:
    """A test clock for Service: the real time plus the jumps of advance()."""

    def __init__(self):
        self.offset = 0.0

    def __call__(self):
        return time.monotonic() + self.offset

    def advance(self, seconds):
        self.offset += seconds


class Watch:
    """Record the calls of a method of OBJ when they are complete. A test waits on it with no sleep."""

    def __init__(self, obj, name):
        self.cond = threading.Condition()
        self.calls = []
        orig = getattr(obj, name)

        def call(*args, **kw):
            try:
                return orig(*args, **kw)
            finally:
                with self.cond:
                    self.calls.append(args)
                    self.cond.notify_all()
        setattr(obj, name, call)

    def wait(self, pred, timeout=30.0):
        """Wait until PRED(list of the argument tuples) is true. Return False after TIMEOUT."""
        with self.cond:
            return self.cond.wait_for(lambda: pred(self.calls), timeout)


SCANNER_OPTIONS = dict(poll=0.01, flush_poll=0.02)      # short Scanner waits for the emulator
SYNC = P.TRG_STOP                                        # a trigger code that the service ignores (debug log)


def make_service(cfg, net, outputs, **kw):
    """A Service with short internal waits, a test Clock and a Watch on the trigger handler.

    The retry thread examines work_dir/unsent only after svc.clock.advance(3600) (or with
    retry_first = 0 before start), so that it cannot race with the calls of a test.
    """
    svc = Service(cfg, net=net, outputs=outputs, clock=Clock(), scanner_options=SCANNER_OPTIONS, **kw)
    svc.trigger_poll = svc.error_pause = svc.retry_poll = 0.01
    svc.retry_first = 3600.0
    svc.handled = Watch(svc, "_on_trigger")
    return svc


def sync(svc, emu, timeout=30.0):
    """Send a trigger that the service ignores, and wait until the scan thread handled it.

    The scan thread handles the packets in their order. So then it handled all
    earlier packets: a job that they started is complete and in the worker queue,
    and the scan thread waits for the next trigger (a new press is not dropped).
    """
    seq = emu.send_trigger(SYNC, copies=1)
    if not seq:
        return False
    return svc.handled.wait(lambda calls: any(a[0].code == SYNC and a[0].seq == seq for a in calls), timeout)


class SheetGate:
    """Stop the scan thread after the first complete sheet of a job until GO is set.

    Then a test can do something during a job, also on a fast computer.
    """

    def __init__(self, go=None):
        self.reached = threading.Event()
        self.go = go or threading.Event()

    def __enter__(self):
        self.orig = orig = D.Job.add_sheet
        gate = self

        def add_sheet(job, sides):
            orig(job, sides)
            if not gate.reached.is_set():
                gate.reached.set()
                gate.go.wait(30)
        D.Job.add_sheet = add_sheet
        return self

    def __exit__(self, *exc):
        D.Job.add_sheet = self.orig
        self.go.set()


class RecordOutput:
    def __init__(self, keep_data=True):
        self.docs = []
        self.keep_data = keep_data

    def deliver(self, doc):
        # The job directory goes away after the delivery: keep the data (or only its size).
        if self.keep_data:
            doc.pdf_data = doc.pdf
        else:
            doc.pdf_head, doc.pdf_size = None, 0
            for chunk in doc.chunks():
                doc.pdf_head = doc.pdf_head or chunk[:8]
                doc.pdf_size += len(chunk)
        self.docs.append(doc)
        return True


CONFIG = '''
[scanner]
ip = "127.0.0.1"
host_id = "%(hid)s"
host_ip = "127.0.0.1"
host_mac = "02:00:00:a1:b2:c3"
search_port = %(search)d
broadcast = "127.255.255.255"
trigger_port = 0
power_off_hold = 0.5
waiting_log_interval = 0.3
discovery_timeout = 1.0
keepalive_interval = 20
keepalive_failures = 3
command_timeout = 5
data_timeout = 5
retry_min = 0.05
retry_max = 0.3

[service]
short_press_profile = "doc"
long_press_profile = "receipt"
work_dir = "%(work)s"

[pipeline]
rotate = 0

[profiles.doc]
dpi = 150
length = 320

[profiles.receipt]
dpi = 150
duplex = false
color = "gray"
'''


class ServiceTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.tmp = tempfile.TemporaryDirectory()
        self.cfg = C.load(text=CONFIG % dict(hid=support.HOST_ID, search=self.emu.search_port, work=self.tmp.name),
                          env={})
        self.out = RecordOutput()
        self.svc = make_service(self.cfg, self.net, {"rec": self.out})

    def tearDown(self):
        self.svc.stop()
        self.emu.stop()
        self.tmp.cleanup()

    def start(self):
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None), "no reserve")

    def test_short_press_with_duplicates(self):
        self.start()
        self.emu.load(Sheet(front="blank", back="text"), Sheet(front="text", back="text"))
        self.emu.press(long=False, copies=3)
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        self.assertTrue(sync(self.svc, self.emu))                # all copies handled
        self.svc.wait_idle()
        self.assertEqual(len(self.out.docs), 1)                  # copies of one event: one job
        self.assertEqual(self.emu.opcodes().count(0xD5), 1)
        doc = self.out.docs[0]
        self.assertEqual(doc.profile, "doc")
        self.assertEqual(doc.pages, 3)                           # one blank side removed
        self.assertTrue(doc.pdf_data.startswith(b"%PDF"))
        self.assertFalse(doc.receipt)
        # One reserve stays for the next jobs (default renew_after_job = false).
        self.assertTrue(self.svc.reserved and self.emu.owner is not None)
        self.assertEqual((self.emu.stats["reserve"], self.emu.stats["release"]), (1, 0))

    def test_many_jobs_one_reserve(self):
        self.start()
        for i, mm in enumerate((175, 668, 415, 834)):           # the receipts of the hardware test
            self.assertTrue(sync(self.svc, self.emu))            # the scan thread waits for a press
            self.emu.load(Sheet(front="blank", back="text", length_mm=mm))
            self.emu.press(long=True)
            self.assertTrue(wait_for(lambda: len(self.out.docs) == i + 1), mm)
        self.assertEqual((self.emu.stats["reserve"], self.emu.stats["release"]), (1, 0))
        self.assertEqual(self.emu.opcodes().count(0xD6), 4)
        self.assertEqual([d.pages for d in self.out.docs], [1, 1, 1, 1])

    def test_renew_after_job(self):
        self.cfg.scanner["renew_after_job"] = True
        self.start()
        self.emu.load(Sheet())
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None))
        self.assertEqual((self.emu.stats["reserve"], self.emu.stats["release"]), (2, 1))

    def test_busy_hint(self):
        self.emu.devstatus = 2
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 1 and self.svc.last_error))
        self.assertIn("close and open the cover", self.svc.last_error)
        self.emu.devstatus = 0
        self.assertTrue(wait_for(lambda: self.svc.reserved))

    def test_three_sheets_with_prefetch(self):
        # Hardware bug: only sheet 1 was saved, with a false jam warning.
        self.start()
        self.emu.load(Sheet(front="blank", back="text"), Sheet(front="blank", back="text"),
                      Sheet(front="blank", back="text"))
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 3)
        self.assertEqual(doc.warnings, [])
        self.assertNotIn(0xC2, self.emu.opcodes()[self.emu.opcodes().index(0xE0):-2])   # no C2 between sheets

    def test_press_copies_during_long_job(self):
        # Hardware case: copies of one press waited in the socket during the job,
        # and one copy started a second job after the job.
        self.start()
        self.emu.load(Sheet(front="text"), Sheet(front="text"), Sheet(front="text"))
        with SheetGate() as gate:                                # the job waits after sheet 1
            copies = self.emu.press(copies=3, interval=0.05, background=True)
            self.assertTrue(gate.reached.wait(30))
            copies.join(10)                                      # copies arrive during the job
            self.emu.press(copies=1)                             # another press during the job: dropped
            gate.go.set()
            self.assertTrue(wait_for(lambda: len(self.out.docs) == 1, 30))
        self.assertTrue(sync(self.svc, self.emu))                # all presses handled
        self.svc.wait_idle()
        self.assertEqual(self.emu.opcodes().count(0xD5), 1)      # exactly one job
        self.assertEqual(len(self.out.docs), 1)
        self.assertEqual(self.out.docs[0].pages, 6)
        # A new press after the job works.
        self.emu.load(Sheet())
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 2))

    def test_long_press_profile(self):
        self.start()
        self.emu.load(Sheet(front="text", back="text", length_mm=700))
        self.emu.press(long=True)
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.profile, "receipt")
        self.assertEqual(doc.pages, 1)                           # simplex: back side only
        self.assertTrue(doc.receipt)
        self.assertTrue(self.emu.params["gray"])

    def test_two_presses(self):
        self.start()
        self.emu.load(Sheet())
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None))
        self.assertTrue(sync(self.svc, self.emu))
        self.emu.load(Sheet())
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 2))

    def test_power_on_reserves_again(self):
        self.start()
        n = self.emu.stats["reserve"]
        self.emu.power_cycle()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] > n and self.emu.owner is not None))

    def fail_fast(self):
        """One keepalive packet in each probe, and a short wait: a lost reply fails at once."""
        self.cfg.scanner.update(keepalive_wait=0.05, keepalive_repeat=1)

    def test_keepalive_loss_reserves_again(self):
        # The test calls the keepalive probes (keepalive_once): no wait for keepalive_interval.
        self.start()
        n = self.emu.stats["reserve"]
        self.assertTrue(self.svc.keepalive_once())
        self.assertEqual(self.emu.stats["keepalive"], 1)
        with self.emu.lock:
            self.emu.owner = None                                # the scanner forgets us, no trigger
        self.fail_fast()
        self.assertEqual([self.svc.keepalive_once() for _ in range(3)], [False] * 3)   # limit 3
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] > n and self.emu.owner is not None))

    def test_power_off_then_on(self):
        # Hardware case: auto power-off, then the user turns the scanner on again (no POWER ON trigger).
        self.start()
        attempts = Watch(self.svc, "_try_reserve")
        self.cfg.scanner.update(discovery_timeout=0.1, retry_max=0.05)   # attempts while off end soon
        self.emu.power_off()
        self.assertTrue(self.svc.handled.wait(lambda calls: any(a[0].code == 0x10 for a in calls)))
        self.assertFalse(self.svc.reserved)
        self.svc.clock.advance(self.cfg.scanner["power_off_hold"])
        self.assertTrue(wait_for(lambda: self.emu.stats["probe_off"] >= 4))      # probes while off
        self.assertEqual(self.emu.stats["tcp_off"], 0)        # no TCP reserve without a search reply
        self.cfg.scanner["discovery_timeout"] = 5.0          # an attempt after power on gets the reply
        n = len(attempts.calls)
        self.emu.power_on()
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None))
        # The reserve comes in one retry period: the attempt at power on, or the next attempt
        # after a wait of retry_max or less.
        self.assertLessEqual(len(attempts.calls) - n, 2)
        self.assertLessEqual(self.svc.delay, self.cfg.scanner["retry_max"])

    def test_power_off_hold(self):
        self.start()
        attempts = Watch(self.svc, "_try_reserve")
        n = self.emu.stats["reserve"]
        self.emu.send_trigger(0x10)                           # POWER OFF, but the scanner still answers
        self.assertTrue(attempts.wait(lambda calls: len(calls) >= 3))     # attempts in the hold
        self.assertEqual(self.emu.stats["reserve"], n)        # no reserve in power_off_hold
        self.assertFalse(self.svc.reserved)
        self.svc.clock.advance(self.cfg.scanner["power_off_hold"])        # the hold ends
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.stats["reserve"] > n))

    def test_waiting_log(self):
        self.emu.off = True
        self.cfg.scanner["discovery_timeout"] = 0.1          # an attempt ends soon
        attempts = Watch(self.svc, "_try_reserve")
        with self.assertLogs("ix1300.daemon", "INFO") as logs:
            self.svc.start()
            self.assertTrue(attempts.wait(lambda calls: len(calls) >= 1))
            self.svc.clock.advance(self.cfg.scanner["waiting_log_interval"])
            self.assertTrue(wait_for(lambda: any("still waiting for the scanner" in m for m in logs.output)))
        first = [m for m in logs.output if "does not answer the search" in m and "still waiting" not in m]
        self.assertEqual(len(first), 1)                       # the same message is not repeated
        self.assertFalse(self.svc.reserved)

    def test_keepalive_tolerance(self):
        # The test calls the keepalive probes (keepalive_once): no wait for keepalive_interval.
        self.start()
        self.assertTrue(self.svc.keepalive_once())
        self.fail_fast()
        self.emu.drop_keepalive = 2                           # 2 intervals without a reply (limit 3)
        self.assertEqual([self.svc.keepalive_once() for _ in range(2)], [False, False])
        self.assertEqual(self.svc.ka_failures, 2)
        self.cfg.scanner["keepalive_wait"] = 5.0              # the reply comes
        self.assertTrue(self.svc.keepalive_once())
        self.assertEqual(self.emu.stats["keepalive_dropped"], 2)
        self.assertEqual(self.svc.ka_failures, 0)
        self.assertFalse(self.svc.lost.is_set())
        self.assertTrue(sync(self.svc, self.emu))
        self.assertEqual(self.emu.stats["reserve"], 1)
        self.assertTrue(self.svc.reserved)

    def test_short_error(self):
        import errno
        import socket
        from ix1300.daemon import short_error
        self.assertEqual(short_error(OSError(errno.EHOSTUNREACH, "x")), "no route to host")
        self.assertEqual(short_error(socket.timeout("timed out")), "timed out")
        self.assertEqual(short_error(ConnectionRefusedError(errno.ECONNREFUSED, "x")), "connection refused")

    def test_occupied_then_free(self):
        self.emu.occupied_by_other = True
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 2))
        self.assertFalse(self.svc.reserved)
        self.emu.occupied_by_other = False
        self.assertTrue(wait_for(lambda: self.svc.reserved and self.emu.owner is not None))

    def test_unregistered_host(self):
        self.emu.registered_id = bytes(8)
        self.svc.start()
        self.assertTrue(wait_for(lambda: self.emu.stats["reserve"] >= 2))
        self.assertFalse(self.svc.reserved)
        self.assertIn("not registered", self.svc.last_error)

    def test_jam_partial_delivery(self):
        self.start()
        self.emu.chunk_size = 5000
        self.emu.load(Sheet(front="text"), Sheet(jam_sense=True))
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)                           # sheet 1 only; sheet 2 front is blank
        self.assertTrue(any("jam" in w for w in doc.warnings))

    def test_double_feed_first_sheet(self):
        # Hardware case: double feed in the first sheet. Nothing may be sent.
        self.start()
        self.emu.load(Sheet(front="text", back="text", double_feed="back"))
        with self.assertLogs("ix1300.daemon", "ERROR") as logs:
            self.emu.press()
            self.assertTrue(sync(self.svc, self.emu))            # the job is in the worker queue
            self.svc.wait_idle()
        self.assertIn(0xD6, self.emu.opcodes())
        self.assertTrue(any("double feed: fan the paper and load it again" in m for m in logs.output))
        self.assertEqual(self.out.docs, [])
        self.assertEqual(os.listdir(os.path.join(self.tmp.name, "jobs")), [])     # job files removed

    def test_double_feed_partial(self):
        self.start()
        self.emu.load(Sheet(front="text"), Sheet(front="text", double_feed="back"))
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)                           # the complete sheet only
        self.assertTrue(any("double feed" in w for w in doc.warnings))

    def test_double_feed_partial_off(self):
        self.cfg.service["deliver_partial"] = False
        self.start()
        self.emu.load(Sheet(front="text"), Sheet(double_feed="back"))
        self.emu.press()
        self.assertTrue(sync(self.svc, self.emu))                # the job is in the worker queue
        self.svc.wait_idle()
        self.assertIn(0xD6, self.emu.opcodes())
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "failed"))), 1)   # the sheet is kept
        self.assertEqual(self.out.docs, [])

    def test_paper_lost_in_front_partial(self):
        # Image bytes of the second front side came, then "no paper": a warning, sheet 1 only.
        self.start()
        self.emu.chunk_size = 5000
        self.emu.load(Sheet(front="text"), Sheet(front="text", paper_lost="front_data"))
        self.emu.press()
        self.assertTrue(wait_for(lambda: len(self.out.docs) == 1))
        doc = self.out.docs[0]
        self.assertEqual(doc.pages, 2)
        self.assertTrue(any("no paper" in w for w in doc.warnings))

    def test_paper_lost_in_front_partial_off(self):
        self.cfg.service["deliver_partial"] = False
        self.start()
        self.emu.chunk_size = 5000
        self.emu.load(Sheet(front="text"), Sheet(front="text", paper_lost="front_data"))
        self.emu.press()
        self.assertTrue(sync(self.svc, self.emu))                # the job is in the worker queue
        self.svc.wait_idle()
        self.assertEqual(len(os.listdir(os.path.join(self.tmp.name, "failed"))), 1)   # the sheet is kept
        self.assertEqual(self.out.docs, [])

    def test_no_paper(self):
        # Empty chute: the service reads C2 and starts no job (no D5, no E0, no D6).
        self.start()
        with self.assertLogs("ix1300.daemon", level="WARNING") as logs:
            self.emu.press()
            self.assertTrue(sync(self.svc, self.emu))
        self.assertIn(0xC2, self.emu.opcodes())
        self.assertNotIn(0xD5, self.emu.opcodes())
        self.assertNotIn(0xE0, self.emu.opcodes())
        self.assertTrue(any("no job started" in m for m in logs.output))
        self.assertEqual(self.out.docs, [])

    def test_no_paper_check_off(self):
        # check_paper = false: the old behaviour (a job that E0 ends with "no paper").
        self.cfg.service["check_paper"] = False
        self.start()
        self.emu.press()
        self.assertTrue(sync(self.svc, self.emu))
        self.svc.wait_idle()
        self.assertIn(0xD6, self.emu.opcodes())
        self.assertEqual(self.out.docs, [])



class MemoryTest(unittest.TestCase):
    """22 sides at 300 dpi: the service must work page by page with small memory."""

    def test_22_sides(self):
        net = make_net()
        emu = Emulator(net, chunk_size=400000, temp_no_data=0).start()
        tmp = tempfile.TemporaryDirectory()
        text = (CONFIG % dict(hid=support.HOST_ID, search=emu.search_port, work=tmp.name)).replace(
            "[pipeline]\nrotate = 0\n", "[pipeline]\nrotate = 0\nmax_memory_mb = 1\n").replace(
            "[profiles.doc]\ndpi = 150", "[profiles.doc]\ndpi = 300")
        cfg = C.load(text=text, env={})
        out = RecordOutput(keep_data=False)
        svc = make_service(cfg, net, {"rec": out})
        calls = []
        base = [0]
        prepared = threading.Event()
        prepare = svc.pipeline.prepare

        def spy(page, profile, store=None):
            calls.append(emu.opcodes().count(0xD6) > base[0])  # True: the scan had ended
            if base[0]:
                prepared.set()
            return prepare(page, profile, store)

        svc.pipeline.prepare = spy
        try:
            emu.load(Sheet(front="blank", back="text", length_mm=300))
            # Make the emulator JPEGs now: in the E0 command they can need more than command_timeout.
            emu.prerender(cfg.profile("doc").scan_settings())
            svc.start()
            self.assertTrue(wait_for(lambda: svc.reserved and emu.owner is not None))
            emu.press()                                          # warm-up
            self.assertTrue(wait_for(lambda: len(out.docs) == 1, 60))
            self.assertTrue(sync(svc, emu))
            svc.wait_idle()
            del calls[:]
            out.docs.clear()
            base[0] = emu.opcodes().count(0xD6)
            emu.load(*[Sheet(front="blank", back="text", length_mm=300) for _ in range(11)])
            tracemalloc.start()
            with SheetGate(go=prepared):                         # the scan continues after the first page work
                emu.press()
                self.assertTrue(wait_for(lambda: len(out.docs) == 1, 120))
            svc.wait_idle()
            _, peak = tracemalloc.get_traced_memory()
            tracemalloc.stop()
        finally:
            svc.stop()
            emu.stop()
        doc = out.docs[0]
        self.assertEqual(doc.pages, 11)                          # blank fronts removed
        self.assertEqual(doc.pdf_head[:5], b"%PDF-")
        total = doc.pdf_size
        self.assertGreater(total, 8 << 20)
        if os.environ.get("IX1300_TEST_VERBOSE"):
            print("\n22 sides: PDF %d bytes, Python peak %d bytes" % (total, peak))
        self.assertLess(peak, total / 2, "peak %d bytes for a PDF of %d bytes" % (peak, total))
        self.assertEqual(len(calls), 22)
        self.assertIn(False, calls)                              # pages done while the scan ran
        tmp.cleanup()


if __name__ == "__main__":
    unittest.main()
