import os
import tempfile
import unittest

import support  # noqa: F401
from httpfake import FakeServer, parse_multipart
from ix1300 import outputs as O
from ix1300.pipeline import Document

PDF = b"%PDF-1.4\n% test\n%%EOF\n"


def doc(receipt=False):
    return Document("scan-1.pdf", "Scan 1", "default", 2, receipt, created=0,
                    warnings=["probable paper jam"], pdf=PDF)


class WebhookTest(unittest.TestCase):
    def test_upload(self):
        with FakeServer() as srv:
            out = O.WebhookOutput("hook", {"url": srv.url + "/hook?x=1", "field": "upload",
                                           "headers": {"X-Api-Key": "k1"}, "fields": {"source": "ix1300"}})
            self.assertTrue(out.deliver(doc()))
            req = srv.rec.requests[0]
        self.assertEqual(req["path"], "/hook?x=1")
        self.assertEqual(req["headers"]["X-Api-Key"], "k1")
        parts = {p[0]: p for p in parse_multipart(req)}
        self.assertEqual(parts["upload"][1:], ("scan-1.pdf", "application/pdf", PDF))
        self.assertEqual(parts["title"][3], b"Scan 1")
        self.assertEqual(parts["pages"][3], b"2")
        self.assertEqual(parts["receipt"][3], b"false")
        self.assertEqual(parts["source"][3], b"ix1300")
        self.assertIn(b"jam", parts["warnings"][3])

    def test_upload_from_file(self):
        with FakeServer() as srv, tempfile.TemporaryDirectory() as tmp:
            fn = os.path.join(tmp, "big.pdf")
            data = PDF + os.urandom(3 << 20)
            with open(fn, "wb") as fh:
                fh.write(data)
            d = Document("big.pdf", "Big", "default", 1, False, 0, path=fn)
            self.assertTrue(O.WebhookOutput("hook", {"url": srv.url}).deliver(d))
            parts = {p[0]: p for p in parse_multipart(srv.rec.requests[0])}
        self.assertEqual(parts["file"][3], data)

    def test_http_error_and_spool(self):
        with FakeServer(status=500) as srv, tempfile.TemporaryDirectory() as tmp:
            out = O.WebhookOutput("hook", {"url": srv.url, "retries": 1, "retry_delay": 0})
            status = O.deliver(doc(), {"hook": out}, spool_dir=tmp)
            self.assertEqual(status, {"hook": False})
            self.assertEqual(len(srv.rec.requests), 2)
            with open(os.path.join(tmp, "unsent", "scan-1.pdf"), "rb") as fh:
                self.assertEqual(fh.read(), PDF)


class PaperlessTest(unittest.TestCase):
    cfg = {"type": "paperless", "token": "abc123", "tags": [1, 2], "receipt_tags": [9, 2],
           "correspondent": 4, "document_type": 5, "receipt_document_type": 7}

    def send(self, receipt):
        with FakeServer() as srv:
            out = O.PaperlessOutput("pl", dict(self.cfg, url=srv.url + "/"))
            self.assertTrue(out.deliver(doc(receipt)))
            return srv.rec.requests[0]

    def test_format(self):
        req = self.send(False)
        self.assertEqual(req["path"], "/api/documents/post_document/")
        self.assertEqual(req["headers"]["Authorization"], "Token abc123")
        parts = parse_multipart(req)
        names = [p[0] for p in parts]
        self.assertEqual(names.count("tags"), 2)
        values = {}
        for name, fn, ct, data in parts:
            values.setdefault(name, []).append(data)
        self.assertEqual(values["tags"], [b"1", b"2"])
        self.assertEqual(values["title"], [b"Scan 1 [scan warning]"])     # the document has a warning
        self.assertEqual(values["correspondent"], [b"4"])
        self.assertEqual(values["document_type"], [b"5"])
        doc_part = [p for p in parts if p[0] == "document"][0]
        self.assertEqual(doc_part[1:], ("scan-1.pdf", "application/pdf", PDF))

    def test_receipt(self):
        parts = parse_multipart(self.send(True))
        tags = [p[3] for p in parts if p[0] == "tags"]
        self.assertEqual(tags, [b"1", b"2", b"9"])
        self.assertEqual([p[3] for p in parts if p[0] == "document_type"], [b"7"])

    def test_title_suffix_off(self):
        with FakeServer() as srv:
            out = O.PaperlessOutput("pl", dict(self.cfg, url=srv.url, warning_title_suffix=""))
            self.assertTrue(out.deliver(doc()))
            parts = parse_multipart(srv.rec.requests[0])
        self.assertEqual([p[3] for p in parts if p[0] == "title"], [b"Scan 1"])

    def test_missing_token(self):
        out = O.PaperlessOutput("pl", {"url": "http://x", "retries": 0})
        with self.assertRaises(O.OutputError):
            out.send(doc())


class DirectoryTest(unittest.TestCase):
    def test_write(self):
        with tempfile.TemporaryDirectory() as tmp:
            out = O.DirectoryOutput("dir", {"path": tmp})
            a = out.send(doc())
            b = out.send(doc())
            self.assertNotEqual(a, b)
            self.assertEqual(sorted(os.listdir(tmp)), ["scan-1-1.pdf", "scan-1.pdf"])


if __name__ == "__main__":
    unittest.main()
