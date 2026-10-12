"""Unit tests for the review findings: input limits, validation, outputs, storage, crop."""
import json
import logging
import os
import socket
import stat
import struct
import tempfile
import threading
import time
import unittest

import support
import jpegsynth
from httpfake import FakeServer, parse_multipart
from ix1300 import config as C, jpeg, outputs as O, pipeline as PL, protocol as P
from ix1300.protocol import ScanResult, Side


def side(sheet, name, kind="text", w=640, h=880, comps=1, dpi=100):
    density = {"blank": 0.0}.get(kind, 0.5)
    data = jpegsynth.synth(w, h, comps, density=density, dpi=dpi, seed=sheet + 1)
    return Side(sheet=sheet, side=name, page_id=0, jpeg=data, width=w, lines=h, dpi=dpi)


# --- Finding 4: PGM parser -------------------------------------------------

class PgmTest(unittest.TestCase):
    def test_bad_input_does_not_hang(self):
        for data in (b"", b"P5", b"P5\n3 2", b"P5\n# comment without end", b"P5\n0 2\n255\n",
                     b"P5\n3 2\n65535\n" + bytes(12), b"P5\n3 2\n255\n" + bytes(5), b"P6\n3 2\n255\n" + bytes(18),
                     b"P5\n3x 2\n255\n" + bytes(6)):
            t0 = time.monotonic()
            with self.assertRaises(ValueError, msg=data):
                PL._parse_pgm(data)
            self.assertLess(time.monotonic() - t0, 1.0)

    def test_good(self):
        px, w, h = PL._parse_pgm(b"P5 3 2 255\n" + bytes(range(6)))
        self.assertEqual((w, h, px), (3, 2, bytes(range(6))))


# --- Findings 3 and 22: JPEG header checks ----------------------------------

class JpegHeaderTest(unittest.TestCase):
    def test_truncated(self):
        j = jpegsynth.synth(64, 80, 1, density=0)
        sof = j.index(b"\xff\xc0")
        for cut in (sof + 3, sof + 6, 12, 20):
            d = j[:cut]
            self.assertIsNone(jpeg.info(d), cut)          # no exception
            self.assertEqual(jpeg.fix_height(d, 10), d)
            jpeg.density(d)
        with self.assertRaises(ValueError):
            PL.Page.from_jpeg(j[:sof + 6], 300)

    def test_bad_dimensions(self):
        j = bytearray(jpegsynth.synth(64, 80, 1, density=0))
        i = j.index(b"\xff\xc0")
        struct.pack_into(">H", j, i + 7, 0)              # width 0
        with self.assertRaises(ValueError):
            jpeg.check(bytes(j))
        with self.assertRaises(ValueError):
            PL.Page.from_jpeg(bytes(j), 300)


# --- Finding 5: limits for frames, sides and HTTP replies -------------------

class FakeSock:
    """Port1 socket: gives DATA in pieces of STEP bytes, DELAY s apart."""

    def __init__(self, data, step=1 << 20, delay=0.0):
        self.data, self.step, self.delay, self.timeout, self.sent = data, step, delay, None, b""

    def sendall(self, b):
        self.sent += b

    def recv(self, n):
        if self.delay:
            time.sleep(self.delay)
        chunk, self.data = self.data[:min(n, self.step)], self.data[min(n, self.step):]
        return chunk

    def settimeout(self, t):
        self.timeout = t

    def gettimeout(self):
        return self.timeout

    def close(self):
        pass


def reply(data=b"", scsi=0, total=None):
    return struct.pack(">I", 40 + len(data) if total is None else total) + P.KEY + struct.pack(">ii", 0, scsi) + \
        bytes(24) + data


def scanner(**kw):
    return P.Scanner("10.0.0.2", "10.0.0.1", support.HOST_MAC, bytes.fromhex(support.HOST_ID), **kw)


class LimitTest(unittest.TestCase):
    def test_reply_longer_than_requested(self):
        sc = scanner()
        sc.p1 = FakeSock(reply(total=40 + (100 << 20)))
        with self.assertRaises(P.ProtocolError):
            sc.cmd([0x03, 0, 0, 0, 18, 0], inlen=18)
        self.assertIsNone(sc.p1)                              # the connection is closed

    def test_slow_frame_has_a_deadline(self):
        sc = scanner(frame_timeout=0.1)
        sc.p1 = FakeSock(reply(bytes(18)), step=1, delay=0.02)   # each byte in time, but the frame is slow
        t0 = time.monotonic()
        with self.assertRaises(P.ScanTimeout):
            sc.cmd([0x03, 0, 0, 0, 18, 0], inlen=18)
        self.assertLess(time.monotonic() - t0, 2.0)

    def test_empty_success_is_no_progress(self):
        sc = scanner(data_timeout=0.1)
        sc.cmd = lambda cdb, out=b"", inlen=0: (0, b"\x00\x00")
        with self.assertRaises(P.ScanTimeout):
            sc._read_side(0, 0)

    def test_side_size_limit(self):
        sc = scanner(max_side_bytes=2500)
        sc.cmd = lambda cdb, out=b"", inlen=0: (0, b"\x00\x00" + bytes(1000))
        with self.assertRaises(P.ProtocolError):
            sc._read_side(0, 0)

    def test_side_time_limit(self):
        sc = scanner(side_timeout=0.1)
        sc.cmd = lambda cdb, out=b"", inlen=0: (time.sleep(0.01), (0, b"\x00\x00" + bytes(10)))[1]
        with self.assertRaises(P.ScanTimeout):
            sc._read_side(0, 0)

    def test_short_sense_and_bad_release(self):
        sc = scanner()
        sc.p1 = FakeSock(reply(bytes(4)))
        with self.assertRaises(P.ProtocolError):
            sc.request_sense()
        sc._port2 = lambda msg, n: bytes(16)
        with self.assertRaises(P.ProtocolError):
            sc.release()

    def test_http_reply_is_bounded(self):
        with FakeServer() as srv:
            srv.rec.body = b"x" * (1 << 20)
            status, body = O.http_post(srv.url, b"abc", {})
        self.assertEqual(status, 200)
        self.assertEqual(len(body), O.MAX_RESPONSE)


# --- Finding 6: triggers, keepalive and discovery ---------------------------

def trigger(code=1, seq=5, size=48):
    p = bytearray(max(size, 18))
    struct.pack_into(">I", p, 0, 0x30)
    p[4:8] = P.KEY
    struct.pack_into(">I", p, 8, code)
    p[16] = seq
    return bytes(p[:size]) if size >= 18 else bytes(p[:size])


class UdpFake:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []

    def udp(self, bind=None, broadcast=False):
        return self

    def sendto(self, data, addr):
        self.sent.append(addr)

    def settimeout(self, t):
        self.t = t

    def recvfrom(self, n):
        if not self.replies:
            time.sleep(min(self.t or 0.01, 0.05))
            raise socket.timeout()
        return self.replies.pop(0)

    def close(self):
        pass


def search_reply(ip="192.0.2.212", p1=53218, p2=53219):
    r = bytearray(144)
    r[0:4] = P.KEY
    struct.pack_into(">HHHHI", r, 4, 0, 0, 0x10, 0x30, 0)
    r[16:20] = socket.inet_aton(ip)
    struct.pack_into(">II", r, 20, p1, p2)
    r[28:34] = bytes.fromhex("1cbcec001234")
    r[104:120] = b"ScanSnap iX1300 "
    return bytes(r)


class PacketCheckTest(unittest.TestCase):
    def test_trigger_size_and_code(self):
        self.assertIsNotNone(P.parse_trigger(trigger()))
        self.assertIsNone(P.parse_trigger(trigger(size=18)))
        self.assertIsNone(P.parse_trigger(trigger(size=64)))
        self.assertIsNone(P.parse_trigger(trigger(code=0x99)))

    def test_discovery_ports(self):
        self.assertIsNone(P.DeviceInfo.parse(search_reply(p1=0)))
        self.assertIsNone(P.DeviceInfo.parse(search_reply(p2=70000)))
        self.assertIsNotNone(P.DeviceInfo.parse(search_reply()))

    def test_discovery_sender_must_agree(self):
        net = UdpFake([(search_reply("192.0.2.212"), ("192.0.2.99", 52217)),
                       (search_reply("192.0.2.212"), ("192.0.2.212", 52217))])
        devs = P.discover("192.0.2.2", support.HOST_MAC, timeout=0.1, net=net, interval=0.05)
        self.assertEqual([d.src[0] for d in devs], ["192.0.2.212"])
        net = UdpFake([(search_reply("192.0.2.212"), ("192.0.2.99", 52217))])
        self.assertEqual(P.discover("192.0.2.2", support.HOST_MAC, timeout=0.1, net=net, interval=0.05), [])

    def test_keepalive_reply_from_other_host(self):
        ok = P.KEY + b"\x80\x01" + bytes(6)
        net = UdpFake([(ok, ("192.0.2.99", 52217))])
        self.assertFalse(P.keepalive("192.0.2.212", "192.0.2.2", support.HOST_MAC, timeout=0.1, net=net))
        net = UdpFake([(ok, ("192.0.2.212", 52217))])
        self.assertTrue(P.keepalive("192.0.2.212", "192.0.2.2", support.HOST_MAC, timeout=0.2, net=net))


# --- Finding 16: duplicate triggers ------------------------------------------

class DedupTest(unittest.TestCase):
    def test_reordered_copies(self):
        d = P.TriggerDedup()
        src = ("10.0.0.2", 1)
        got = [s for s in (254, 255, 254, 1, 255, 1, 2) if d.is_new(P.parse_trigger(trigger(seq=s), src))]
        self.assertEqual(got, [254, 255, 1, 2])


# --- Findings 12 and 13: configuration ----------------------------------------

class ConfigCheckTest(unittest.TestCase):
    def bad(self, text, env=None):
        with self.assertRaises(C.ConfigError, msg=text):
            C.load(text=text, env=env or {})

    def test_values(self):
        self.bad('[scanner]\nkeepalive_interval = -1\n')
        self.bad('[scanner]\nkeepalive_interval = 40\nreserve_timeout = 30\n')
        self.bad('[scanner]\nsearch_port = 70000\n')
        self.bad('[scanner]\nip = "192.0.2"\n')
        self.bad('[scanner]\nhost_mac = "zz"\n')
        self.bad('[scanner]\nkeepalive_intervall = 5\n')              # misspelled key
        self.bad('[pipeline]\nrotate = 45\n')
        self.bad('[pipeline]\nworkers = 1.5\n')
        self.bad('[pipeline]\nmax_memory_mb = nan\n')
        self.bad('[profiles.default]\nrotate = 45\n')
        self.bad('[service]\nlog_level = "LOUD"\n')
        self.bad('[outputs.p]\ntype = "paperless"\nurl = "paperless:8000"\n')
        self.bad('[outputs.w]\ntype = "webhook"\nurl = "http://h/x"\nheaders = { A = "x\\ny" }\n')
        self.bad('[outputs.w]\ntype = "webhook"\nurl = "http://h/x"\nretries = -1\n')
        self.bad('[outputs.w]\ntype = "webhook"\nurl = "http://h/x"\nurll = "x"\n')
        self.bad('[outputs.d]\ntype = "directory"\nfile_mode = "999"\n')

    def test_typed_booleans(self):
        cfg = C.load(text='[scanner]\nrenew_after_job = "false"\n[pipeline]\nrotate = "180"\n', env={})
        self.assertIs(cfg.scanner["renew_after_job"], False)
        self.assertEqual(cfg.pipeline["rotate"], 180)

    def test_file_and_syntax_errors(self):
        with self.assertRaises(C.ConfigError):
            C.load("/nonexistent/ix1300.toml", env={})
        self.bad("[scanner\n")
        self.bad('{"scanner": ')
        self.bad('[1, 2]')

    def test_env_keeps_secrets_as_text(self):
        env = {"IX1300_OUTPUTS__PL__TOKEN": "123e40", "IX1300_OUTPUTS__PL__TYPE": "paperless",
               "IX1300_OUTPUTS__PL__URL": "http://pl:8000", "IX1300_SCANNER__HOST_ID": "000123456789ABCD",
               "IX1300_OUTPUTS__PL__TAGS": "[1, 2]", "IX1300_OUTPUTS__PL__RETRIES": "0"}
        cfg = C.load(text="", env=env)
        self.assertEqual(cfg.outputs["pl"]["token"], "123e40")
        self.assertEqual(cfg.outputs["pl"]["tags"], [1, 2])
        self.assertEqual(cfg.outputs["pl"]["retries"], 0)
        for tok in ("null", "007", "true"):
            cfg = C.load(text="", env=dict(env, IX1300_OUTPUTS__PL__TOKEN=tok))
            self.assertEqual(cfg.outputs["pl"]["token"], tok)
        with self.assertRaises(C.ConfigError):
            C.load(text="", env={"IX1300_SCANNER__RETRY_MIN": "soon"})

    def test_env_file(self):
        with tempfile.NamedTemporaryFile("w", delete=False) as fh:
            fh.write("s3cret\n")
        try:
            cfg = C.load(text='[outputs.pl]\ntype = "paperless"\nurl = "http://pl"\n',
                         env={"IX1300_OUTPUTS__PL__TOKEN_FILE": fh.name})
            self.assertEqual(cfg.outputs["pl"]["token"], "s3cret")
        finally:
            os.remove(fh.name)


# --- Findings 14, 19, 21: outputs ------------------------------------------------

def doc(filename="scan-1.pdf"):
    d = PL.Document(filename, "Scan 1", "default", 1, False, created=0, pdf=b"%PDF-1.4\n%%EOF\n")
    d.job_id = "20261010-120000-abc123"
    return d


class OutputSafetyTest(unittest.TestCase):
    def test_token_not_in_logs(self):
        token = "tok-SECRET-123"
        with FakeServer(status=500) as srv:
            srv.rec.body = ("error: bad token " + token).encode()
            out = O.PaperlessOutput("pl", {"url": srv.url, "token": token, "retries": 0})
            with self.assertLogs("ix1300.outputs", "INFO") as logs:
                self.assertFalse(out.deliver(doc()))
        self.assertNotIn(token, "\n".join(logs.output))
        bad = O.PaperlessOutput("pl", {"url": "http://x", "token": token + "\nX", "retries": 0})
        with self.assertLogs("ix1300.outputs", "INFO") as logs:
            self.assertFalse(bad.deliver(doc()))
        self.assertNotIn(token, "\n".join(logs.output))
        self.assertTrue(bad.last_permanent)

    def test_permanent_error_no_retry(self):
        with FakeServer(status=401) as srv:
            out = O.WebhookOutput("hook", {"url": srv.url, "retries": 3, "retry_delay": 0})
            self.assertFalse(out.deliver(doc()))
            self.assertEqual(len(srv.rec.requests), 1)
            self.assertTrue(out.last_permanent)
            self.assertEqual(srv.rec.requests[0]["headers"]["X-Ix1300-Job"], "20261010-120000-abc123")
        with FakeServer(status=503) as srv:
            out = O.WebhookOutput("hook", {"url": srv.url, "retries": 2, "retry_delay": 0})
            self.assertFalse(out.deliver(doc()))
            self.assertEqual(len(srv.rec.requests), 3)
            self.assertFalse(out.last_permanent)

    def test_multipart_quoting(self):
        with FakeServer() as srv:
            out = O.WebhookOutput("hook", {"url": srv.url, "fields": {'we"ird': "a"}, "standard_fields": True})
            self.assertTrue(out.deliver(doc('report"xé.pdf')))
            parts = parse_multipart(srv.rec.requests[0])
        names = [p[0] for p in parts]
        self.assertIn("we%22ird", names)
        self.assertIn("job_id", names)
        f = [p for p in parts if p[0] == "file"][0]
        self.assertEqual(f[1], "report%22xé.pdf")
        with self.assertRaises(O.OutputError):
            O.multipart([("a\x01", "v")], [])

    def test_directory_modes_and_no_replace(self):
        with tempfile.TemporaryDirectory() as tmp:
            d = os.path.join(tmp, "out")
            out = O.DirectoryOutput("dir", {"path": d, "file_mode": "0640"})
            a, b = out.send(doc("../../x.pdf")), out.send(doc("../../x.pdf"))
            self.assertEqual(os.path.dirname(a), d)
            self.assertNotEqual(a, b)
            self.assertEqual(stat.S_IMODE(os.stat(a).st_mode), 0o640)
            self.assertEqual(stat.S_IMODE(os.stat(d).st_mode) & 0o077, 0)
            self.assertEqual([n for n in os.listdir(d) if n.endswith(".part")], [])


# --- Findings 8, 9, 15, 20, 24: storage and pipeline ------------------------------

class StorageTest(unittest.TestCase):
    def test_shared_budget(self):
        with tempfile.TemporaryDirectory() as tmp:
            budget = PL.Budget(100)
            a = PL.PageStore(os.path.join(tmp), budget=budget)
            b = PL.PageStore(os.path.join(tmp), budget=budget)
            self.assertIsNotNone(a.put(bytes(80)).data)
            self.assertIsNone(b.put(bytes(80)).data)          # the other job used the budget
            a.close()
            self.assertIsNotNone(b.put(bytes(80)).data)

    def test_private_modes_and_filename(self):
        with tempfile.TemporaryDirectory() as tmp:
            pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0, filename_format="../%Y/%m/x\x01.pdf"))
            d = pl.process(ScanResult(sheets=1, sides=[side(0, "back")]), C._profile("p", {}), out_dir=tmp)
            self.assertNotIn("/", d.filename)
            self.assertTrue(d.filename.endswith(".pdf"))
            path = os.path.join(tmp, d.filename)
            self.assertTrue(os.path.exists(path))
            self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
            saved = d.save(os.path.join(tmp, "copy.pdf"))
            self.assertEqual(stat.S_IMODE(os.stat(saved).st_mode), 0o600)

    def test_simplex_transformed_pages_spill(self):
        if not (PL.have_pillow() or PL.djpeg_path()) or not (PL.have_pillow() or PL.jpegtran_path()):
            self.skipTest("needs a decoder and jpegtran or Pillow")
        from test_samples import load
        with tempfile.TemporaryDirectory() as tmp:
            store = PL.PageStore(tmp, limit_bytes=0)
            pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
            prof = C._profile("p", {"duplex": False, "crop": True})
            r = ScanResult(sheets=1, sides=[load("synth", "front"), load("synth", "back")])
            pages = [pl.prepare(PL.Page.from_side(s, store), prof, store) for s in PL.select_sides(r, prof)]
            kept = pl.keep(pages, prof, store)
            self.assertEqual(len(kept), 1)
            self.assertIn("crop_box", kept[0].info)
            self.assertIsNone(kept[0].jpeg_blob.data)          # the cropped page is in a file
            self.assertEqual(store.in_memory, 0)

    def test_failed_step_keeps_page(self):
        pl = PL.Pipeline(dict(C.DEFAULTS["pipeline"], rotate=0))
        old = PL.deskew_and_crop
        PL.deskew_and_crop = lambda *a, **k: 1 / 0
        try:
            page = pl.prepare(PL.Page.from_side(side(0, "back")), C._profile("p", {"crop": True}))
        finally:
            PL.deskew_and_crop = old
        self.assertTrue(page.oriented)
        self.assertTrue(any("deskew/crop failed" in e for e in page.errors))
        d = pl.finish([page], C._profile("p", {"crop": True}))
        self.assertTrue(any("deskew/crop failed" in w for w in d.warnings))

    def test_rotation_without_pillow(self):
        old = PL.Image
        PL.Image = None
        try:
            page = PL.Page.from_side(side(0, "back", w=640, h=880))
            page.rotate = 90
            if PL.jpegtran_path():
                turned = PL.rotate_pixels(page)
                self.assertEqual((turned.width, turned.height, turned.rotate), (880, 640, 0))
            os.environ["IX1300_JPEGTRAN"] = "ix1300-no-jpegtran"
            try:
                self.assertIsNone(PL.rotate_pixels(page))       # OCR then uses the normal PDF
            finally:
                del os.environ["IX1300_JPEGTRAN"]
        finally:
            PL.Image = old


# --- Crop: a bright scanner backing (hardware report, 835 mm receipt) -------------

def receipt_on_bright_backing(seed=1):
    """75 dpi, 219 mm window. Receipt 80 mm wide at 5..85 mm, paper 240 with print.
    Backing: 215 at the left, bright and noisy (mean 224, std 3) at the right."""
    import random
    rnd = random.Random(seed)
    w, h, dpi = 647, 700, 75
    x0, x1 = int(5 / 25.4 * dpi), int(85 / 25.4 * dpi)
    px = bytearray(w * h)
    for y in range(h):
        row = y * w
        for x in range(w):
            if x0 <= x < x1 and 20 <= y < h - 20:
                v = 240 + rnd.randint(-3, 3)
                if (y - 20) % 14 in (5, 6) and x0 + 8 <= x < x1 - 8 - (y % 23):
                    v = 30
            elif x < x0:
                v = 215 + rnd.randint(-2, 2)
            else:
                v = int(round(rnd.gauss(224, 3)))
            px[row + x] = max(0, min(255, v))
    return bytes(px), w, h, dpi, (x0, x1)


class BrightBackingTest(unittest.TestCase):
    def test_box_stays_on_the_paper(self):
        from test_pipeline import ink_outside
        g, w, h, dpi, (x0, x1) = receipt_on_bright_backing()
        a = PL.analyze(g, w, h, dpi)
        bx0, by0, bx1, by1 = a.bbox
        self.assertLessEqual((bx1 - bx0) * 25.4 / dpi, 90)
        self.assertLessEqual(bx0, x0)
        self.assertGreaterEqual(bx1, x1)
        self.assertEqual(ink_outside(g, w, h, a.bbox), 0)
        self.assertGreater(a.paper_level, 225)

    def test_real_receipts_width(self):
        if not (PL.have_pillow() or PL.djpeg_path()):
            self.skipTest("no JPEG decoder")
        from test_samples import load
        for name in ("synth", "r1", "r2", "r3", "r4"):
            for sd in ("front", "back"):
                try:
                    page = PL.Page.from_side(load(name, sd))
                except unittest.SkipTest:
                    continue
                g, w, h, dpi = PL.gray_image(page)
                a = PL.analyze(g, w, h, dpi)
                width = (a.bbox[2] - a.bbox[0]) * 25.4 / dpi
                self.assertTrue(75 <= width <= 100, "%s %s: %.1f mm" % (name, sd, width))


if __name__ == "__main__":
    unittest.main()
