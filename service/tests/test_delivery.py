"""Second review, findings A10 and A12.

A10: each output has its own queue and thread. A slow or failing output delays
only itself: the document worker and the next scans continue. The manifest has
the state of each output (attempts, next due time), so a restart continues.

A12: a stop interrupts a READ that waits for its reply (a short socket tick
examines the stop), then the service does D8, E0, D6 and the release.
"""
import os
import socket
import tempfile
import threading
import time
import unittest

import support
from emulator import Emulator, Sheet
from netsim import make_net
from test_daemon import CONFIG, RecordOutput, make_service, sync, wait_for
from test_jobs import read_json, sides
from ix1300 import config as C, daemon as D, jobs as J, protocol as P


class Gate(RecordOutput):
    """An output that waits in deliver() until RELEASE is set."""

    def __init__(self):
        super().__init__()
        self.inside, self.release = threading.Event(), threading.Event()
        self.calls = 0
        self.result = True                # False: the attempt fails after the release
        self.last_error, self.last_permanent = None, False

    def deliver(self, doc):
        self.calls += 1
        self.inside.set()
        self.release.wait(30)
        if not self.result:
            self.last_error = "receiver down"
            return False
        return super().deliver(doc)


class Failing(RecordOutput):
    """Fails until OK is True. RETRIES: attempts after the first in one delivery round."""

    def __init__(self, retries=0, retry_delay=100.0, permanent=False):
        super().__init__()
        self.retries, self.retry_delay = retries, retry_delay
        self.permanent = permanent
        self.ok = False
        self.calls = 0
        self.last_error, self.last_permanent = None, False

    def deliver(self, doc):
        self.calls += 1
        if not self.ok:
            self.last_error, self.last_permanent = "HTTP 503" if not self.permanent else "HTTP 401", self.permanent
            return False
        self.last_error, self.last_permanent = None, False
        return super().deliver(doc)


class DeliveryCase(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.work = self.tmp.name
        self.emu = None
        self.svcs = []

    def config(self, search=9):
        return C.load(text=CONFIG % dict(hid=support.HOST_ID, search=search, work=self.work), env={})

    def service(self, cfg, outputs):
        svc = make_service(cfg, self.net, outputs)
        svc.wall = lambda: time.time() + svc.clock.offset        # the wall clock follows the test clock
        svc.random = lambda: 0.0                                  # the shortest jitter: deterministic
        self.svcs.append(svc)
        self.addCleanup(svc.stop, 5)
        return svc

    def emulator(self):
        self.emu = Emulator(self.net).start()
        self.addCleanup(self.emu.stop)
        return self.emu

    def queue_job(self, svc, n=0):
        job = D.Job(svc, svc.cfg.profile("doc"))
        job.add_sheet(sides(n))
        job.end_scan(True)
        svc._queue(job)
        return job.dir.id

    def manifest(self, kind, jid):
        return read_json(os.path.join(self.work, kind, jid, J.MANIFEST))

    def ls(self, kind):
        try:
            return sorted(os.listdir(os.path.join(self.work, kind)))
        except OSError:
            return []


class A10DeliveryTest(DeliveryCase):
    def test_a10_slow_output_does_not_block_the_next_document(self):
        emu = self.emulator()
        cfg = self.config(emu.search_port)
        cfg.service["max_backlog"] = 1
        slow, fast = Gate(), RecordOutput()
        self.addCleanup(slow.release.set)
        svc = self.service(cfg, {"slow": slow, "fast": fast})
        svc.start()
        self.assertTrue(wait_for(lambda: svc.reserved and emu.owner is not None), "no reserve")
        emu.load(Sheet(front="text"))
        emu.press()
        self.assertTrue(slow.inside.wait(15), "no attempt of the slow output")
        self.assertTrue(wait_for(lambda: len(fast.docs) == 1 and svc.backlog == 0, 10),
                        "the document worker waits for the slow output")
        self.assertTrue(sync(svc, emu), "the scan thread does not wait for a press")
        emu.load(Sheet(front="text"))
        emu.press()
        # The second scan starts (max_backlog = 1) and its PDF goes to the fast output at once.
        self.assertTrue(wait_for(lambda: len(fast.docs) == 2, 15), "the next document waits for the slow output")
        self.assertEqual(slow.calls, 1)                           # one document at a time for each output
        slow.release.set()
        self.assertTrue(wait_for(lambda: len(slow.docs) == 2 and svc.jobs_done == 2, 15))
        self.assertTrue(wait_for(lambda: self.ls(J.JOBS) == [] and self.ls(J.UNSENT) == []))

    def test_a10_retry_state_in_the_manifest(self):
        cfg = self.config()
        bad = Failing(retries=2, retry_delay=100.0)
        good = RecordOutput()
        svc = self.service(cfg, {"bad": bad, "good": good})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: bad.calls == 1 and len(good.docs) == 1, 15), "first attempts")
        # Each output saves its own state; wait for both before the checks.
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["bad"].get("due")
                                 and self.manifest(J.JOBS, jid)["outputs"]["good"].get("ok")), "no due time")
        m = self.manifest(J.JOBS, jid)
        st = m["outputs"]["bad"]
        self.assertEqual(m["state"], "delivering")
        self.assertEqual((st["ok"], st["attempts"], st["error"]), (False, 1, "HTTP 503"))
        self.assertTrue(m["outputs"]["good"]["ok"])
        # Backoff with jitter: 100 s x 2^0 x (0.5 + 0.5 x random()); random() = 0 here.
        self.assertAlmostEqual(st["due"] - svc.wall(), 50.0, delta=5)
        self.assertTrue(wait_for(lambda: svc.backlog == 0), "the worker is not free")
        self.assertFalse(wait_for(lambda: bad.calls > 1, 0.2))    # not due yet
        svc.clock.advance(51)
        self.assertTrue(wait_for(lambda: bad.calls == 2, 10), "no second attempt")
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["bad"]["attempts"] == 2),
                        "attempt 2 not in the manifest")
        st = self.manifest(J.JOBS, jid)["outputs"]["bad"]
        self.assertAlmostEqual(st["due"] - svc.wall(), 100.0, delta=5)     # 100 s x 2^1 x 0.5
        bad.ok = True
        svc.clock.advance(101)
        self.assertTrue(wait_for(lambda: len(bad.docs) == 1, 10), "no third attempt")
        self.assertTrue(wait_for(lambda: self.ls(J.JOBS) == [] and self.ls(J.UNSENT) == []), "job not removed")
        self.assertEqual(len(good.docs), 1)                       # the good output got the document one time

    def test_a10_jitter_range(self):
        cfg = self.config()
        svc = D.Service(cfg, net=make_net(), outputs={})
        self.addCleanup(svc.executor.shutdown)
        out = Failing(retry_delay=10.0)
        for r, low, high in ((0.0, 5.0, 5.0), (1.0, 10.0, 10.0), (0.5, 7.5, 7.5)):
            svc.random = lambda r=r: r
            self.assertAlmostEqual(svc._output_wait(out, 1), low)
            self.assertAlmostEqual(svc._output_wait(out, 2), 2 * high)

    def test_a10_exhausted_round_goes_to_unsent(self):
        cfg = self.config()
        bad = Failing(retries=1, retry_delay=10.0)
        svc = self.service(cfg, {"bad": bad})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: bad.calls == 1, 15))
        svc.clock.advance(11)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 10))
        self.assertTrue(wait_for(lambda: svc.jobs_done == 1))
        m = self.manifest(J.UNSENT, jid)
        self.assertEqual((m["state"], m["outputs"]["bad"]["attempts"]), ("unsent", 2))

    def test_a10_permanent_error_is_not_retried(self):
        cfg = self.config()
        bad = Failing(retries=3, retry_delay=1.0, permanent=True)
        svc = self.service(cfg, {"bad": bad})
        svc.retry_first = 0
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 15))
        svc.clock.advance(3600)
        self.assertFalse(wait_for(lambda: bad.calls > 1, 0.3))
        st = self.manifest(J.UNSENT, jid)["outputs"]["bad"]
        self.assertEqual((st["ok"], st["permanent"], st["attempts"]), (False, True, 1))
        self.assertNotIn("due", st)

    def delivering_job(self, kind, due, attempts=1, rnd=1):
        jd = J.JobDir.create(self.work, "doc")
        with open(jd.pdf_path(), "wb") as fh:
            fh.write(b"%PDF-1.4\n%%EOF\n")
        jd.m.update(state="delivering" if kind == J.JOBS else "unsent", attempts=attempts if kind == J.UNSENT else 0,
                    outputs={"rec": {"ok": False, "attempts": attempts, "round": rnd, "due": due,
                                     "error": "HTTP 503"}},
                    document=dict(filename="a.pdf", title="A", profile="doc", pages=1, receipt=False, created=0))
        jd.save()
        jd.move(kind)
        return jd.id

    def test_a10_restart_resumes_the_due_time(self):
        cfg = self.config()
        out = Failing(retries=2)
        out.ok = True
        jid = self.delivering_job(J.JOBS, time.time() + 100)
        svc = self.service(cfg, {"rec": out})
        svc.start()
        self.assertFalse(wait_for(lambda: out.calls > 0, 0.3))    # the saved due time is in the future
        self.assertEqual(self.ls(J.JOBS), [jid])                  # still a delivery, not unsent
        svc.clock.advance(101)
        self.assertTrue(wait_for(lambda: len(out.docs) == 1, 10))
        self.assertEqual(out.docs[0].job_id, jid)
        self.assertTrue(wait_for(lambda: self.ls(J.JOBS) == [] and self.ls(J.UNSENT) == []))

    def test_a10_restart_resumes_the_unsent_due_time(self):
        cfg = self.config()
        out = Failing()
        out.ok = True
        jid = self.delivering_job(J.UNSENT, time.time() + 100, rnd=0)
        svc = self.service(cfg, {"rec": out})
        svc.retry_first = 0                                       # the retry thread examines unsent at once
        svc.start()
        self.assertFalse(wait_for(lambda: out.calls > 0, 0.3))
        svc.clock.advance(101)
        self.assertTrue(wait_for(lambda: len(out.docs) == 1, 10))
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == []))
        self.assertEqual(out.docs[0].job_id, jid)

    def test_a10_unsent_retry_has_backoff_per_output(self):
        cfg = self.config()
        cfg.service["retry_interval"] = 60
        bad = Failing()
        svc = self.service(cfg, {"rec": bad})
        svc.retry_first = 0
        jid = self.delivering_job(J.UNSENT, 0, rnd=0)
        svc.start()
        self.assertTrue(wait_for(lambda: bad.calls == 1, 10))                    # due at once
        self.assertTrue(wait_for(lambda: self.manifest(J.UNSENT, jid)["attempts"] == 2 and
                                 self.manifest(J.UNSENT, jid)["outputs"]["rec"].get("due", 0) > svc.wall()))
        st = self.manifest(J.UNSENT, jid)["outputs"]["rec"]
        self.assertAlmostEqual(st["due"] - svc.wall(), 60.0, delta=5)       # 60 s x 2^1 x 0.5
        self.assertFalse(wait_for(lambda: bad.calls > 1, 0.3))
        bad.ok = True
        svc.clock.advance(61)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [], 10))

    def test_a10_stop_keeps_the_delivery_for_the_next_start(self):
        cfg = self.config()
        slow, fast = Gate(), RecordOutput()
        self.addCleanup(slow.release.set)
        svc = self.service(cfg, {"slow": slow, "fast": fast})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(slow.inside.wait(15))
        self.assertTrue(wait_for(lambda: self.manifest(J.JOBS, jid)["outputs"]["fast"]["ok"]))
        t0 = time.monotonic()
        svc.stop(timeout=1)
        self.assertLess(time.monotonic() - t0, 5)
        slow.result = False                                       # the receiver did not get it
        slow.release.set()
        self.assertEqual(self.ls(J.JOBS), [jid])
        slow2, fast2 = RecordOutput(), RecordOutput()
        svc2 = self.service(cfg, {"slow": slow2, "fast": fast2})
        # The earlier service had the job lock until its thread ended.
        self.assertTrue(wait_for(lambda: not svc.threads["out-slow"].is_alive(), 10))
        svc2.start()
        self.assertTrue(wait_for(lambda: len(slow2.docs) == 1, 10))
        self.assertEqual(fast2.docs, [])                         # the fast output has it already
        self.assertTrue(wait_for(lambda: self.ls(J.JOBS) == [] and self.ls(J.UNSENT) == []))


class A12StopReadTest(DeliveryCase):
    def test_a12_recv_tick_examines_the_stop(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(30)
        stop = threading.Event()
        threading.Timer(0.1, stop.set).start()
        t0 = time.monotonic()
        with self.assertRaises(P.Stopped):
            P.recv_exact(a, 40, time.monotonic() + 120, stop=stop.is_set, tick=0.02)
        self.assertLess(time.monotonic() - t0, 2)
        self.assertEqual(a.gettimeout(), 30)

    def test_a12_recv_tick_keeps_the_socket_timeout(self):
        a, b = socket.socketpair()
        self.addCleanup(a.close)
        self.addCleanup(b.close)
        a.settimeout(0.2)
        b.sendall(b"x" * 10)
        with self.assertRaises(socket.timeout):              # no data for 0.2 s: as without the tick
            P.recv_exact(a, 40, time.monotonic() + 120, stop=lambda: False, tick=0.02)
        b.sendall(b"y" * 40)
        self.assertEqual(P.recv_exact(a, 40, None, stop=lambda: False, tick=0.02), b"y" * 40)

    def test_a12_stop_interrupts_a_blocking_read(self):
        emu = self.emulator()
        self.addCleanup(emu.hang_release.set)
        cfg = self.config(emu.search_port)
        cfg.scanner.update(command_timeout=60, data_timeout=60, frame_timeout=600, side_timeout=600)
        cfg.service["stop_scan_wait"] = 0
        out = RecordOutput()
        svc = self.service(cfg, {"rec": out})
        svc.start()
        self.assertTrue(wait_for(lambda: svc.reserved and emu.owner is not None), "no reserve")
        emu.hang_read = 2                                   # the first READ of sheet 2 gets no reply
        emu.load(Sheet(front="text"), Sheet(front="text"), Sheet(front="text"))
        emu.press()
        self.assertTrue(wait_for(lambda: emu.stats.get("hung") == 1, 15), "no hanging READ")
        stopper = threading.Thread(target=svc.stop, kwargs=dict(timeout=60), daemon=True)
        stopper.start()
        self.assertTrue(svc.stop_event.wait(10))
        svc.clock.advance(50)                                # the READ deadline of the stop
        stopper.join(10)
        self.assertFalse(stopper.is_alive(), "the stop waits for the READ reply")
        self.assertFalse(svc.threads["main"].is_alive())
        ops = emu.opcodes()
        tail = ops[ops.index(0xD8, 3):]                      # the cancel after the hanging READ
        self.assertIn(0xE0, tail)
        self.assertIn(0xD6, tail)
        self.assertGreaterEqual(emu.stats["release"], 1)
        # The complete sheet 1 stays: sent, or on disk for the next start.
        kept = [m for k in (J.JOBS, J.UNSENT) for m in (self.manifest(k, j) for j in self.ls(k))]
        self.assertTrue(len(out.docs) == 1 or any(len(m["sheets"]) == 1 for m in kept), (out.docs, kept))


if __name__ == "__main__":
    unittest.main()
