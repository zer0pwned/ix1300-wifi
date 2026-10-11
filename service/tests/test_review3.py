"""Tests for the findings of the third code review.

A: the documents in the output queues count for max_backlog, and unsent jobs
   go into the output queues only while the queues have space.
B: the attempt deadline (max_time) also applies to the connection setup.
C: the service removes secrets from the full reply before it cuts the text.
"""
import http.client
import logging
import socket
import time
import unittest

import support  # noqa: F401
import test_delivery
from test_daemon import wait_for
from test_delivery import DeliveryCase, Failing, Gate
from ix1300 import jobs as J, outputs as O
from ix1300.pipeline import Document

SECRET = "SECRETVALUE123"


class AdmissionTest(DeliveryCase):
    delivering_job = test_delivery.A10DeliveryTest.delivering_job

    def test_queued_deliveries_count_for_max_backlog(self):
        cfg = self.config()
        cfg.service["max_backlog"] = 3
        slow = Gate()
        self.addCleanup(slow.release.set)
        svc = self.service(cfg, {"slow": slow})
        svc.start()
        for n in range(3):
            self.queue_job(svc, n)
        self.assertTrue(wait_for(lambda: svc.backlog == 0 and len(svc.deliveries) == 3, 15))
        self.assertFalse(svc._admit())                    # 3 documents wait for the slow output
        slow.release.set()
        self.assertTrue(wait_for(lambda: svc.jobs_done == 3 and not svc.deliveries, 15))
        self.assertTrue(svc._admit())

    def test_unsent_jobs_go_into_the_queues_lazily(self):
        cfg = self.config()
        cfg.service["max_backlog"] = 3
        slow = Gate()
        self.addCleanup(slow.release.set)
        ids = [self.delivering_job(J.UNSENT, 0, rnd=0) for _ in range(12)]
        svc = self.service(cfg, {"rec": slow})
        svc.retry_first = 0
        svc.retry_period = 0.01
        svc.start()
        self.assertTrue(slow.inside.wait(15))
        time.sleep(0.2)                                   # many examinations of work_dir/unsent
        self.assertLessEqual(len(svc.deliveries), 3)      # at most max_backlog job locks
        slow.release.set()
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [], 20), "not all unsent jobs sent")
        self.assertEqual(sorted(d.job_id for d in slow.docs), sorted(ids))


class SlowConnection(http.client.HTTPConnection):
    """connect() takes DELAY seconds (as a slow DNS or TCP connect), then the peer reads nothing."""
    delay = 0.5

    def connect(self):
        self.connect_timeout = self.timeout
        time.sleep(self.delay)
        self.sock, self.peer = socket.socketpair()
        self.sock.settimeout(self.timeout)


class DeadlineTest(unittest.TestCase):
    def post(self, max_time):
        conns = []

        def factory(scheme, host, port, timeout, ctx):
            conns.append(SlowConnection(host, port, timeout=timeout))
            return conns[-1]
        t0 = time.monotonic()
        with self.assertRaises(O.OutputError) as cm:
            O.http_post("http://slow.invalid/", b"abc", {}, timeout=5.0, max_time=max_time, factory=factory)
        conn = conns[0]
        conn.peer.setblocking(False)
        try:
            data = conn.peer.recv(1000)
        except BlockingIOError:
            data = b""
        conn.peer.close()
        return time.monotonic() - t0, str(cm.exception), conn, data

    def test_deadline_applies_to_the_connection_setup(self):
        elapsed, error, conn, data = self.post(0.2)
        self.assertLess(elapsed, 2.0)
        self.assertIn("no complete reply", error)
        self.assertLessEqual(conn.connect_timeout, 0.2)   # the connect gets only the time that is left
        self.assertEqual(data, b"")                       # no request after the deadline

    def test_deadline_applies_to_the_reply(self):
        elapsed, error, conn, data = self.post(1.0)       # the connect ends before the deadline
        self.assertLess(elapsed, 3.0)
        self.assertIn("no complete reply", error)
        self.assertTrue(data.startswith(b"POST / HTTP/1.1"))


def doc():
    return Document("scan-1.pdf", "Scan 1", "default", 1, False, created=0, warnings=[], pdf=b"%PDF-1.4\n%%EOF\n")


class EchoHook(O.WebhookOutput):
    """A webhook whose receiver sends the secret header back after 195 bytes."""

    def send(self, doc):
        return b"x" * 195 + SECRET.encode()


class EchoPaperless(O.PaperlessOutput):
    def __init__(self, reply):
        super().__init__("pl", {"url": "http://paperless.invalid", "token": SECRET})
        self.reply = reply

    def _post(self, url, body, headers, default_timeout):
        return 200, self.reply


class RedactTest(DeliveryCase):
    def test_manifest_reply_has_no_secret_prefix(self):
        cfg = self.config()
        hook = EchoHook("hook", {"url": "http://hook.invalid/", "headers": {"X-Key": SECRET}})
        bad = Failing(permanent=True)                    # the job stays in unsent with its manifest
        svc = self.service(cfg, {"hook": hook, "bad": bad})
        svc.start()
        jid = self.queue_job(svc)
        self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 15))
        reply = self.manifest(J.UNSENT, jid)["outputs"]["hook"]["reply"]
        self.assertTrue(reply.startswith("xxx"))
        self.assertNotIn(SECRET[:4], reply)

    def test_paperless_log_has_only_the_task_id(self):
        out = EchoPaperless(b'{"detail": "' + b"x" * 64 + SECRET.encode() + b'"}')
        with self.assertLogs("ix1300.outputs", logging.INFO) as cm:
            self.assertTrue(out.attempt(doc()))
        self.assertNotIn(SECRET[:4], "\n".join(cm.output))
        self.assertEqual(O.reply_text(out, out.last_reply), "(no task ID in the reply)")
        out = EchoPaperless(b'"6f1c0f8e-1b2c-4d5e-8f90-0a1b2c3d4e5f"')
        with self.assertLogs("ix1300.outputs", logging.INFO) as cm:
            self.assertTrue(out.attempt(doc()))
        self.assertIn("paperless task 6f1c0f8e-1b2c-4d5e-8f90-0a1b2c3d4e5f", "\n".join(cm.output))
        self.assertEqual(O.reply_text(out, out.last_reply), "6f1c0f8e-1b2c-4d5e-8f90-0a1b2c3d4e5f")


if __name__ == "__main__":
    unittest.main()
