"""Webhook idempotency key and the reply text of a failed attempt.

1: idempotency_header adds a key to each upload. The key is the same for each
   attempt of a document: the SHA-256 of the PDF, or the job ID.
2: after an HTTP error, the log line and the manifest show a short part of the
   reply. The service removes the secrets from the full reply before the cut.
"""
import hashlib
import logging
import os
import tempfile
import threading
import unittest

import support  # noqa: F401
from httpfake import FakeServer, parse_multipart
from test_daemon import wait_for
from test_delivery import DeliveryCase
from ix1300 import config as C, jobs as J, outputs as O
from ix1300.pipeline import Document

PDF = b"%PDF-1.4\n% idempotency test\n%%EOF\n"
SECRET = "SECRETVALUE123"
JOB = "20261011-120000-abcd"


def doc(pdf=PDF, path=None, job_id=JOB):
    d = Document("scan-1.pdf", "Scan 1", "default", 1, False, created=0, pdf=None if path else pdf, path=path)
    d.job_id = job_id
    return d


def hook(url, **cfg):
    return O.WebhookOutput("hook", dict(url=url, **cfg))


class IdempotencyKeyTest(unittest.TestCase):
    def test_sha256_of_the_pdf_in_the_header(self):
        with FakeServer() as srv:
            self.assertTrue(hook(srv.url, idempotency_header="Idempotency-Key").attempt(doc()))
            req = srv.rec.requests[0]
        self.assertEqual(req["headers"]["Idempotency-Key"], hashlib.sha256(PDF).hexdigest())
        parts = {p[0]: p for p in parse_multipart(req)}
        self.assertEqual(parts["file"][3], PDF)          # the key is the hash of the sent bytes

    def test_same_key_on_a_retry_after_a_failure(self):
        with FakeServer(status=503) as srv, tempfile.TemporaryDirectory() as tmp:
            fn = os.path.join(tmp, "doc.pdf")
            data = PDF + os.urandom(3 << 20)              # more than one chunk on disk
            with open(fn, "wb") as fh:
                fh.write(data)
            out = hook(srv.url, idempotency_header="Idempotency-Key")
            d = doc(path=fn)
            self.assertFalse(out.attempt(d))
            self.assertFalse(out.last_permanent)
            srv.rec.status = 200
            self.assertTrue(out.attempt(d))
            keys = [r["headers"]["Idempotency-Key"] for r in srv.rec.requests]
        self.assertEqual(len(keys), 2)
        self.assertEqual(keys[0], keys[1])
        self.assertEqual(keys[0], hashlib.sha256(data).hexdigest())

    def test_job_id_value(self):
        with FakeServer() as srv:
            out = hook(srv.url, idempotency_header="Idempotency-Key", idempotency_value="job_id")
            self.assertTrue(out.attempt(doc()))
            self.assertEqual(srv.rec.requests[0]["headers"]["Idempotency-Key"], JOB)

    def test_bad_job_id_is_a_permanent_error(self):
        out = hook("http://hook.invalid/", idempotency_header="Idempotency-Key", idempotency_value="job_id")
        for jid in (None, "", "a b", "x" * 201, "café"):
            self.assertFalse(out.attempt(doc(job_id=jid)))
            self.assertTrue(out.last_permanent)
            self.assertIn("job ID", out.last_error)

    def test_off_by_default(self):
        with FakeServer() as srv:
            self.assertTrue(hook(srv.url).attempt(doc()))
            names = [k.lower() for k in srv.rec.requests[0]["headers"]]
        self.assertNotIn("idempotency-key", names)

    def test_standard_fields_off_sends_only_the_pdf_and_fields(self):
        with FakeServer() as srv:
            self.assertTrue(hook(srv.url, standard_fields=False, fields={"source": "scanner"}).attempt(doc()))
            names = [p[0] for p in parse_multipart(srv.rec.requests[0])]
            headers = {k.lower() for k in srv.rec.requests[0]["headers"]}
        self.assertEqual(names, ["source", "file"])        # the same parts as the curl example of Reflow
        self.assertIn("x-ix1300-job", headers)

    def test_standard_fields_off_by_default(self):
        with FakeServer() as srv:
            self.assertTrue(hook(srv.url).attempt(doc()))
            self.assertTrue(hook(srv.url, standard_fields=True).attempt(doc()))
            default = [p[0] for p in parse_multipart(srv.rec.requests[0])]
            on = [p[0] for p in parse_multipart(srv.rec.requests[1])]
        self.assertEqual(default, ["file"])
        self.assertIn("title", on)
        self.assertIn("job_id", on)
        with self.assertRaises(ValueError):
            O.validate("hook", {"type": "webhook", "url": "http://hook.invalid/", "standard_fields": "no"})
        cfg = C.load(text='[outputs.hook]\ntype = "webhook"\nurl = "http://hook.invalid/"\n'
                          '[profiles.default]\noutputs = ["hook"]\n',
                     env={"IX1300_OUTPUTS__HOOK__STANDARD_FIELDS": "true"})
        self.assertIs(cfg.data["outputs"]["hook"]["standard_fields"], True)

    def test_bad_configuration_is_rejected(self):
        good = {"type": "webhook", "url": "http://hook.invalid/"}
        O.validate("hook", dict(good, idempotency_header="Idempotency-Key", idempotency_value="job_id"))
        O.validate("hook", dict(good, idempotency_header=""))
        bad = [dict(idempotency_header="Idempotency Key"),            # a space
               dict(idempotency_header="Idempotency-Key:"),
               dict(idempotency_header="Idemé"),
               dict(idempotency_header="Content-Type"),
               dict(idempotency_header=5),
               dict(idempotency_header="Idempotency-Key", idempotency_value="md5"),
               dict(idempotency_value="SHA256"),
               dict(idempotency_header="Idempotency-Key", headers={"idempotency-key": "x"})]
        for extra in bad:
            with self.assertRaises(ValueError, msg=extra):
                O.validate("hook", dict(good, **extra))

    def test_environment_override(self):
        text = '[profiles.default]\nlength = 297\n[outputs.hook]\ntype = "webhook"\nurl = "http://hook.invalid/"\n'
        cfg = C.load(text=text, env={"IX1300_OUTPUTS__HOOK__IDEMPOTENCY_HEADER": "Idempotency-Key",
                                     "IX1300_OUTPUTS__HOOK__IDEMPOTENCY_VALUE": "job_id"})
        self.assertEqual(cfg.outputs["hook"]["idempotency_header"], "Idempotency-Key")
        self.assertEqual(cfg.outputs["hook"]["idempotency_value"], "job_id")
        with self.assertRaises(C.ConfigError):
            C.load(text=text, env={"IX1300_OUTPUTS__HOOK__IDEMPOTENCY_VALUE": "md5"})


class Refusing(O.WebhookOutput):
    """A webhook whose receiver refuses the upload with REPLY (no network)."""

    def __init__(self, reply, status=400, **cfg):
        super().__init__("hook", dict({"url": "http://hook.invalid/", "headers": {"X-Key": SECRET}}, **cfg))
        self.reply, self.status = reply, status

    def _post(self, url, body, headers, default_timeout):
        return self.status, self.reply


class ErrorReplyTest(DeliveryCase):
    def test_reply_text_in_the_log(self):
        with FakeServer(status=400) as srv:
            srv.rec.body = b'{"error": "idempotency_key_required"}'
            out = hook(srv.url, retries=0)
            with self.assertLogs("ix1300.outputs", logging.WARNING) as cm:
                self.assertFalse(out.deliver(doc()))
        self.assertTrue(out.last_permanent)
        self.assertEqual(out.last_error, 'HTTP 400: {"error": "idempotency_key_required"}')
        self.assertIn("idempotency_key_required", "\n".join(cm.output))

    def test_secret_is_removed_before_the_cut(self):
        out = Refusing(b"x" * 195 + SECRET.encode() + b" more text")
        self.assertFalse(out.attempt(doc()))
        self.assertTrue(out.last_error.startswith("HTTP 400: xxx"))
        self.assertNotIn(SECRET[:4], out.last_error)
        self.assertLessEqual(len(out.last_error), len("HTTP 400: ") + 200)

    def test_paperless_reply_text_has_no_token(self):
        class Refused(O.PaperlessOutput):
            def _post(self, url, body, headers, default_timeout):
                return 401, b'{"detail": "Invalid token ' + SECRET.encode() + b'."}'

        out = Refused("pl", {"url": "http://paperless.invalid", "token": SECRET})
        self.assertFalse(out.attempt(doc()))
        self.assertEqual(out.last_error, 'HTTP 401: {"detail": "Invalid token ***."}')
        self.assertTrue(out.last_permanent)

    def test_reply_text_in_the_manifest_and_the_log(self):
        out = Refusing(b'{"error": "idempotency_key_reuse", "key": "' + SECRET.encode() + b'"}')
        svc = self.service(self.config(), {"hook": out})
        with self.assertLogs("ix1300", logging.WARNING) as cm:
            svc.start()
            jid = self.queue_job(svc)
            self.assertTrue(wait_for(lambda: self.ls(J.UNSENT) == [jid], 15))
        st = self.manifest(J.UNSENT, jid)["outputs"]["hook"]
        self.assertEqual(st["error"], 'HTTP 400: {"error": "idempotency_key_reuse", "key": "***"}')
        self.assertTrue(st["permanent"])
        text = "\n".join(cm.output)
        self.assertIn("idempotency_key_reuse", text)
        self.assertNotIn(SECRET[:4], text)

    def test_reply_text_in_the_manifest_of_the_direct_send(self):
        out = Refusing(b"receiver down " + SECRET.encode(), status=503, retries=0)
        svc = self.service(self.config(), {"hook": out})

        class JobDir:
            m = {"outputs": {}}

            def save(self):
                pass

        jd = JobDir()
        with self.assertLogs("ix1300.outputs", logging.WARNING) as cm:
            svc._send_one(jd, doc(), "hook", threading.Lock())
        st = jd.m["outputs"]["hook"]
        self.assertEqual(st["error"], "HTTP 503: receiver down ***")
        self.assertFalse(st["permanent"])
        self.assertIn("receiver down ***", "\n".join(cm.output))


if __name__ == "__main__":
    unittest.main()
