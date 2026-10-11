import socket
import struct
import time
import unittest

import support
from emulator import Emulator, Sheet
from netsim import MemNet, make_net
from ix1300 import jpeg, protocol as P


def scanner_for(emu, net, **kw):
    return P.Scanner("127.0.0.1", support.HOST_IP, support.HOST_MAC, bytes.fromhex(support.HOST_ID),
                     port1=emu.port1, port2=emu.port2, search_port=emu.search_port, trigger_port=55265,
                     net=net, timeout=5, data_timeout=5, poll=0.01, **kw)


class PacketTest(unittest.TestCase):
    def test_search_packet_layout(self):
        pkt = P.search_packet("192.0.2.161", bytes.fromhex("b827eb000001"))
        self.assertEqual(len(pkt), 32)
        self.assertEqual(pkt[:4], b"\x56\x45\x4e\x53")
        self.assertEqual(struct.unpack_from(">I", pkt, 4)[0], 0)
        self.assertEqual(pkt[8:12], socket.inet_aton("192.0.2.161"))
        self.assertEqual(pkt[12:20], bytes.fromhex("b827eb0000010000"))
        self.assertEqual(struct.unpack_from(">H", pkt, 24)[0], 0x0010)
        ka = P.search_packet("192.0.2.161", bytes(6), keepalive=True)
        self.assertEqual(struct.unpack_from(">I", ka, 4)[0], 1)

    def test_parse_search_reply(self):
        r = bytearray(144)
        r[0:4] = P.KEY
        struct.pack_into(">HHHHI", r, 4, 0, 0, 0x10, 0x30, 0)
        r[16:20] = socket.inet_aton("192.0.2.212")
        struct.pack_into(">II", r, 20, 53218, 53219)
        r[28:34] = bytes.fromhex("1cbcec001234")
        r[40:57] = b"iX1300-ABCDE12345"
        r[104:120] = b"ScanSnap iX1300 "
        r[124:132] = bytes.fromhex("0123456789ABCDEF")
        r[132:142] = b"ABCDE12345"
        d = P.DeviceInfo.parse(bytes(r), ("192.0.2.212", 52217))
        self.assertEqual((d.ip, d.port1, d.port2), ("192.0.2.212", 53218, 53219))
        self.assertEqual(d.mac, "1c:bc:ec:00:12:34")
        self.assertEqual(d.name, "iX1300-ABCDE12345")
        self.assertEqual(d.product, "ScanSnap iX1300")
        self.assertEqual(d.serial, "ABCDE12345")
        self.assertEqual(d.owner_id, "0123456789ABCDEF")
        self.assertFalse(d.tls)
        self.assertIsNone(P.DeviceInfo.parse(bytes(r[:100])))
        r[6:8] = b"\xff\xff"
        self.assertIsNone(P.DeviceInfo.parse(bytes(r)))

    def test_reserve_message(self):
        s = P.Scanner("10.0.0.2", "10.0.0.1", bytes.fromhex("020000000001"), bytes.fromhex(support.HOST_ID),
                      trigger_port=55265)
        m = s.reserve_message()
        self.assertEqual(len(m), 128)
        self.assertEqual(struct.unpack_from(">I", m, 8)[0], 0x11)
        self.assertEqual(m[16:24], bytes.fromhex("0200000000010000"))
        self.assertEqual(struct.unpack_from(">HB", m, 32), (0x10, 30))
        self.assertEqual(struct.unpack_from(">I", m, 48)[0], 55265)
        self.assertEqual(m[108:116].hex().upper(), support.HOST_ID)

    def test_settings_d4(self):
        d = P.ScanSettings(dpi=300).d4_data()
        self.assertEqual(len(d), 0x50)
        self.assertEqual(d[:14], bytes.fromhex("00 03 01 01 88 00 C1 80 80 80 80 80 80 00"))
        self.assertEqual(d[31], 0x30)
        desc = d[32:]
        self.assertEqual(desc[:9], bytes.fromhex("00 10 01 2C 01 2C 05 82 0D"))
        self.assertEqual(struct.unpack_from(">II", desc, 10), (0x2880, 0xA543))
        self.assertEqual((desc[18], desc[30]), (4, 1))
        d6 = P.ScanSettings(dpi=600, color="gray", length=P.mm_to_length(300)).d4_data()[32:]
        self.assertEqual((d6[6], d6[7], d6[8]), (0x02, 0x81, 0x0A))
        self.assertEqual(P.ScanSettings(dpi=150).d4_data()[32 + 8], 0x05)
        e9 = P.ScanSettings(dpi=200).e9_data()
        self.assertEqual(e9, struct.pack(">HHII", 200, 200, 0x2880, 0x4350) + b"\x05\x00" + bytes(18))
        with self.assertRaises(ValueError):
            P.ScanSettings(dpi=250)


class TriggerTest(unittest.TestCase):
    def pkt(self, code=1, cond=b"\0\0\x01\0", seq=5):
        p = bytearray(48)
        struct.pack_into(">I", p, 0, 0x30)
        p[4:8] = P.KEY
        struct.pack_into(">I", p, 8, code)
        p[12:16] = cond
        p[16] = seq
        return bytes(p)

    def test_parse(self):
        t = P.parse_trigger(self.pkt(), ("10.0.0.2", 52217))
        self.assertEqual((t.code, t.seq, t.long_press), (1, 5, False))
        t = P.parse_trigger(self.pkt(cond=b"\0\0\x20\0"))
        self.assertTrue(t.long_press)
        self.assertIsNone(P.parse_trigger(b"\0" * 48))

    def test_dedup(self):
        d = P.TriggerDedup()
        t = P.parse_trigger(self.pkt(seq=7), ("10.0.0.2", 1))
        self.assertTrue(d.is_new(t))
        self.assertFalse(d.is_new(t))
        self.assertFalse(d.is_new(t, now=1e9))               # no time limit: a late copy is still a copy
        self.assertTrue(d.is_new(P.parse_trigger(self.pkt(seq=8), ("10.0.0.2", 1))))
        self.assertTrue(d.is_new(P.parse_trigger(self.pkt(code=0x30, seq=8), ("10.0.0.2", 1))))  # other code

    def test_dedup_wraparound_and_reset(self):
        d = P.TriggerDedup()
        src = ("10.0.0.2", 1)
        self.assertTrue(d.is_new(P.parse_trigger(self.pkt(seq=255), src)))
        self.assertTrue(d.is_new(P.parse_trigger(self.pkt(seq=1), src)))     # 255 -> 1
        self.assertFalse(d.is_new(P.parse_trigger(self.pkt(seq=1), src)))
        d.reset()                                            # power on: the scanner starts at 1 again
        self.assertTrue(d.is_new(P.parse_trigger(self.pkt(seq=1), src)))


class JpegTest(unittest.TestCase):
    def test_fix_height(self):
        import jpegsynth
        j = jpegsynth.synth(64, 400, 1, content_rows=100, density=0)
        self.assertEqual(jpeg.info(j), (64, 400, 1))
        f = jpeg.fix_height(j, 100)
        self.assertEqual(jpeg.info(f), (64, 100, 1))
        self.assertEqual(len(f), len(j))
        self.assertEqual(jpeg.fix_height(j, 500), j)        # only smaller
        self.assertEqual(jpeg.fix_height(j, 0), j)
        self.assertEqual(jpeg.density(j), (300, 300))


class EmulatorTest(unittest.TestCase):
    def setUp(self):
        self.net = make_net()
        self.emu = Emulator(self.net).start()
        self.sc = scanner_for(self.emu, self.net)

    def tearDown(self):
        self.sc.close()
        self.emu.stop()

    def test_discover(self):
        devs = P.discover(support.HOST_IP, support.HOST_MAC, target="127.0.0.1", port=self.emu.search_port,
                          timeout=2, net=self.net)
        self.assertEqual(len(devs), 1)
        d = devs[0]
        self.assertEqual((d.port1, d.port2, d.product), (self.emu.port1, self.emu.port2, "ScanSnap iX1300"))
        self.assertEqual(d.serial, "EMU0000001")

    def test_discover_busy_scanner_repeats(self):
        self.emu.drop_search = 2                             # a busy scanner drops probes
        devs = P.discover(support.HOST_IP, support.HOST_MAC, target="127.0.0.1", port=self.emu.search_port,
                          timeout=2, net=self.net, interval=0.05)
        self.assertEqual(len(devs), 1)

    def test_discover_broadcast_with_unicast(self):
        if not isinstance(self.net, MemNet):
            self.skipTest("needs the in-memory network (no real broadcast)")
        # The broadcast address goes nowhere; the unicast probe finds the scanner.
        devs = P.discover(support.HOST_IP, support.HOST_MAC, port=self.emu.search_port, timeout=1.5,
                          net=self.net, broadcast="10.255.255.255", unicast="127.0.0.1", interval=0.05)
        self.assertEqual([d.serial for d in devs], ["EMU0000001"])
        none = P.discover(support.HOST_IP, support.HOST_MAC, port=self.emu.search_port, timeout=0.1,
                          net=self.net, broadcast="10.255.255.255", interval=0.05)
        self.assertEqual(none, [])

    def test_reserve_release_keepalive(self):
        self.assertFalse(self.sc.keepalive(timeout=0.1))       # no reserve: no reply
        self.assertEqual(self.sc.reserve(), 0)
        self.assertTrue(self.sc.keepalive(timeout=2))
        self.assertEqual(self.sc.release(), 0)
        self.assertFalse(self.sc.keepalive(timeout=0.1))

    def test_keepalive_repeats(self):
        self.sc.reserve()
        self.emu.drop_keepalive = 2                           # power save: 2 packets lost
        self.assertTrue(self.sc.keepalive(timeout=2.0, repeat=3, interval=0.05))
        self.emu.drop_keepalive = 3
        self.assertFalse(self.sc.keepalive(timeout=0.15, repeat=3, interval=0.05))
        self.sc.release()

    def test_reserve_unregistered(self):
        bad = P.Scanner("127.0.0.1", support.HOST_IP, support.HOST_MAC, bytes(8), port1=self.emu.port1,
                        port2=self.emu.port2, net=self.net, timeout=5)
        with self.assertRaises(P.ReserveError) as cm:
            bad.reserve()
        self.assertEqual(cm.exception.result, -6)

    def test_reserve_occupied(self):
        self.emu.occupied_by_other = True
        with self.assertRaises(P.ReserveError) as cm:
            self.sc.reserve()
        self.assertEqual(cm.exception.result, -4)

    def test_commands_without_reserve(self):
        self.assertIn(b"iX1300", self.sc.inquiry())
        st = self.sc.hw_status()
        self.assertTrue(st.hopper_empty)
        self.assertEqual(st.host_id, support.HOST_ID)
        with self.assertRaises((P.ProtocolError, OSError)):
            self.sc.request_sense()                          # needs a reserve: connection closed

    def scan(self, *sheets, **kw):
        self.emu.load(*sheets)
        self.sc.reserve()
        on_sheet = kw.pop("on_sheet", None)
        try:
            return self.sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(kw.pop("window_mm", 400)), **kw),
                                on_sheet=on_sheet)
        finally:
            self.sc.close()
            self.sc.release()

    def test_scan_single_sheet(self):
        r = self.scan(Sheet(front="blank", back="text", length_mm=297))
        self.assertEqual(r.sheets, 1)
        self.assertEqual([(s.side, s.page_id) for s in r.sides], [("front", 0), ("back", 1)])
        lines = round(297 / 25.4 * 150)
        for s in r.sides:
            self.assertEqual(jpeg.info(s.jpeg)[:2], (1296, lines))     # height fixed from the padded window
            self.assertEqual(s.lines, lines)
        self.assertGreaterEqual(self.emu.stats["temp_no_data"], 2)       # one per side, then the same READ again
        self.assertFalse(r.jam_warning)
        ops = self.emu.opcodes()
        self.assertEqual(ops[:6], [0xD8, 0xE9, 0xD4, 0xC2, 0xD5, 0xE0])
        self.assertEqual(ops[-1], 0xD6)
        self.assertEqual(self.emu.stats["release"], 1)

    def test_scan_multi_page(self):
        r = self.scan(Sheet(), Sheet(front="text"), Sheet(length_mm=150))
        self.assertEqual(r.sheets, 3)
        self.assertEqual([s.page_id for s in r.sides], [0, 1, 2, 3, 4, 5])
        self.assertEqual(self.emu.opcodes().count(0xE0), 4)           # the last E0 gets "no paper"
        self.assertEqual(jpeg.info(r.sides[-1].jpeg)[1], round(150 / 25.4 * 150))

    def test_prefetch_hopper_bit_does_not_end_job(self):
        # Hardware bug: after sheet 1 the hopper bit shows empty, but 2 sheets are in the paper path.
        r = self.scan(Sheet(), Sheet(front="text"), Sheet())
        self.assertEqual(r.sheets, 3)
        self.assertEqual(len(r.sides), 6)
        self.assertFalse(r.jam_warning)
        self.assertEqual(r.warnings, [])
        self.assertEqual(self.emu.opcodes().count(0xE0), 4)           # the 4th E0 gets "no paper"
        self.assertEqual(self.emu.hopper, [])

    def test_hopper_bit_empty_while_sheets_left(self):
        # The emulator model: C2 shows an empty hopper while sheets are still in the path.
        self.emu.load(Sheet(), Sheet(), Sheet())
        self.sc.reserve()
        try:
            self.sc._check("D8", [0xD8, 0, 0, 0, 0, 0])
            self.sc._check("E9", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], out=P.ScanSettings(dpi=150).e9_data())
            self.sc._check("D4", [0xD4, 0, 0, 0, 0x50, 0], out=P.ScanSettings(dpi=150).d4_data())
            self.sc._check("D5", [0xD5, 0, 0, 0, 0x08, 0x08], out=bytes(8), inlen=8)
            self.sc._check("E0", [0xE0, 0, 0, 0, 0, 0])
            st = self.sc.hw_status()
            self.assertTrue(st.hopper_empty)
            self.assertTrue(st.paper_at_exit)
            self.assertEqual(len(self.emu.hopper), 2)
            self.sc.cmd([0xD6, 0, 0, 0, 0, 0])
        finally:
            self.sc.close()
            self.sc.release()

    def test_temporary_no_data_and_busy(self):
        self.emu.temp_no_data = 3
        self.emu.busy = 2
        self.emu.chunk_size = 5000                   # many READ chunks for each side
        r = self.scan(Sheet(front="text"))
        self.assertEqual(self.emu.stats["temp_no_data"], 6)
        self.assertEqual(self.emu.stats["busy"], 4)
        for s in r.sides:
            self.assertEqual(s.jpeg[:2], b"\xff\xd8")
            self.assertEqual(s.jpeg[-2:], b"\xff\xd9")
            self.assertNotIn(b"\xde\xad\xff\xd8", s.jpeg)

    def test_pixel_size_without_prefix(self):
        self.emu.psize_prefix = False
        r = self.scan(Sheet(length_mm=200))
        self.assertEqual(jpeg.info(r.sides[1].jpeg)[1], round(200 / 25.4 * 150))

    def test_no_paper(self):
        with self.assertRaises(P.NoPaper):
            self.scan()
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_wifi_jam_warning(self):
        r = self.scan(Sheet(length_mm=380, jam_mm=250), Sheet())
        self.assertEqual(r.sheets, 1)
        self.assertTrue(r.jam_warning)
        self.assertEqual(jpeg.info(r.sides[1].jpeg)[1], round(250 / 25.4 * 150))

    def test_jam_sense_keeps_pages(self):
        self.emu.chunk_size = 5000
        with self.assertRaises(P.PaperJam) as cm:
            self.scan(Sheet(), Sheet(jam_sense=True))
        res = cm.exception.result
        self.assertEqual(res.sheets, 1)
        self.assertEqual(len(res.sides), 2)              # sheet 1 only; the incomplete sheet 2 is dropped
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_double_feed(self):
        with self.assertRaises(P.DoubleFeed) as cm:
            self.scan(Sheet(double_feed=True))
        self.assertIn("fan the paper", str(cm.exception))

    def test_double_feed_after_front_eom(self):
        # The front side ends with EOM, then the back side reports the double feed.
        with self.assertRaises(P.DoubleFeed) as cm:
            self.scan(Sheet(front="text", double_feed="back"))
        self.assertEqual((cm.exception.result.sheets, cm.exception.result.sides), (0, []))
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_double_feed_on_second_sheet(self):
        with self.assertRaises(P.DoubleFeed) as cm:
            self.scan(Sheet(), Sheet(double_feed="back"))
        res = cm.exception.result
        self.assertEqual(res.sheets, 1)
        self.assertEqual([(s.sheet, s.side) for s in res.sides], [(0, "front"), (0, "back")])

    def test_paper_lost_in_front_side_after_data(self):
        # Image bytes of the next front side came, then "no paper": the sheet is not complete.
        self.emu.chunk_size = 80
        with self.assertRaises(P.PaperLost) as cm:
            self.scan(Sheet(), Sheet(front="text", paper_lost="front_data"))
        res = cm.exception.result
        self.assertEqual(res.sheets, 1)
        self.assertEqual([(s.sheet, s.side) for s in res.sides], [(0, "front"), (0, "back")])
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_no_paper_at_start_of_front_side_is_normal_end(self):
        # "No paper" with no image bytes at the start of a front side: the normal end of the stack.
        r = self.scan(Sheet(), Sheet(front="text", paper_lost="front"))
        self.assertEqual((r.sheets, len(r.sides)), (1, 2))
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)

    def test_on_sheet_callback(self):
        got = []
        r = self.scan(Sheet(), Sheet(), on_sheet=got.append)
        self.assertEqual(r.sheets, 2)
        self.assertEqual(r.sides, [])                       # the callback keeps the data
        self.assertEqual([[s.side for s in sh] for sh in got], [["front", "back"]] * 2)

    # --- Cancel with sheets left in the job (PROTOCOL.md 8.6) ---
    def test_scan_settings_have_no_max_sheets(self):
        with self.assertRaises(TypeError):
            P.ScanSettings(max_sheets=1)

    def test_emulator_d8_model(self):
        # The measured behaviour: after D8 a READ gives 5/24/00, and E0 feeds out ALL remaining sheets.
        self.emu.load(*[Sheet() for _ in range(5)])
        self.sc.reserve()
        try:
            self.sc._check("E9", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], out=P.ScanSettings(dpi=150).e9_data())
            self.sc._check("D4", [0xD4, 0, 0, 0, 0x50, 0], out=P.ScanSettings(dpi=150).d4_data())
            self.sc._check("D5", [0xD5, 0, 0, 0, 0x08, 0x08], out=bytes(8), inlen=8)
            self.sc._check("E0", [0xE0, 0, 0, 0, 0, 0])
            self.sc._check("D8", [0xD8, 0, 0, 0, 0, 0])
            with self.assertRaises(P.SenseError) as cm:
                self.sc._read_side(0x00, 0)
            self.assertEqual((cm.exception.sense.key, cm.exception.sense.asc, cm.exception.sense.ascq),
                             (5, 0x24, 0x00))
            self.sc._check("E0", [0xE0, 0, 0, 0, 0, 0])
            self.assertEqual((self.emu.hopper, self.emu.ejected), ([], 5))
            self.sc.cmd([0xD6, 0, 0, 0, 0, 0])
        finally:
            self.sc.close()
            self.sc.release()

    def test_emulator_d8_without_e0_leaves_paper(self):
        self.emu.load(*[Sheet() for _ in range(5)])
        self.sc.reserve()
        try:
            self.sc._check("E9", [0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], out=P.ScanSettings(dpi=150).e9_data())
            self.sc._check("D4", [0xD4, 0, 0, 0, 0x50, 0], out=P.ScanSettings(dpi=150).d4_data())
            self.sc._check("D5", [0xD5, 0, 0, 0, 0x08, 0x08], out=bytes(8), inlen=8)
            self.sc._check("E0", [0xE0, 0, 0, 0, 0, 0])
            self.sc._check("D8", [0xD8, 0, 0, 0, 0, 0])
            self.sc.cmd([0xD6, 0, 0, 0, 0, 0])
            st = self.sc.hw_status()
            self.assertFalse(st.hopper_empty)                 # D6 does not eject [HW]
            self.assertTrue(st.paper_at_exit and st.paper_at_feed)
            self.assertEqual((len(self.emu.hopper), self.emu.ejected), (4, 0))
        finally:
            self.sc.close()
            self.sc.release()

    def cancel_scan(self, n, cancel_after=1, **kw):
        """Scan N sheets; cancel() gives True after CANCEL_AFTER complete sheets."""
        self.emu.load(*[Sheet() for _ in range(n)])
        self.sc.flush_poll = 0.02
        got = []
        self.sc.reserve()
        try:
            with self.assertLogs("ix1300.protocol", "WARNING") as logs:
                r = self.sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320)), on_sheet=got.append,
                                 cancel=lambda: len(got) >= cancel_after, **kw)
        finally:
            self.sc.close()
            self.sc.release()
        return r, got, "\n".join(logs.output)

    def assert_flush(self, ops):
        d8 = len(ops) - 1 - ops[::-1].index(0xD8)
        self.assertGreater(d8, 0)
        self.assertEqual(ops[d8 + 1], 0xE0)                  # E0 at once after D8
        self.assertNotIn(0x28, ops[d8:])                     # no READ after D8 (5/24/00)
        self.assertEqual(ops[-1], 0xD6)
        self.assertIn(0xC2, ops[d8 + 2:-1])                  # the wait reads the sensors
        self.assertEqual(ops.count(0xD8), 2)                 # job start and cancel

    def test_cancel_flushes_rest_of_stack(self):
        r, got, logs = self.cancel_scan(5)
        self.assertEqual((r.sheets, len(got), r.cancelled), (1, 1, True))
        self.assert_flush(self.emu.opcodes())
        self.assertEqual((self.emu.hopper, self.emu.ejected), ([], 4))
        self.assertTrue(self.emu.path_clear())               # D6 came after the eject
        self.assertIn("ejected without a scan", logs)
        self.assertTrue(any("ejected without a scan" in w for w in r.warnings))

    def test_cancel_flush_ignores_e0_sense(self):
        self.emu.e0_cancel_sense = True                      # E0 after D8: CHECK 5/24/00
        r, got, _ = self.cancel_scan(4, cancel_after=2)
        self.assertEqual((r.sheets, r.cancelled), (2, True))
        self.assert_flush(self.emu.opcodes())
        self.assertEqual((self.emu.hopper, self.emu.ejected), ([], 2))

    def test_cancel_flush_wait_limit(self):
        self.emu.eject_time = 1000.0                         # the path never becomes clear
        self.sc.flush_timeout = 0.1
        t0 = time.monotonic()
        r, _, logs = self.cancel_scan(3)
        self.assertLess(time.monotonic() - t0, 10)
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)
        self.assertIn("not empty", logs)

    def test_cancel_with_nothing_left(self):
        self.emu.load(Sheet(), Sheet())
        got = []
        self.sc.flush_poll = 0.02
        self.sc.reserve()
        try:
            r = self.sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320)), on_sheet=got.append,
                             cancel=lambda: len(got) >= 2)
        finally:
            self.sc.close()
            self.sc.release()
        self.assertEqual((r.sheets, self.emu.ejected), (2, 0))
        self.assertEqual(self.emu.opcodes()[-1], 0xD6)
        self.assertFalse(any("ejected" in w for w in r.warnings))

    def test_stopped_read_flushes(self):
        # Scanner.abort (stop deadline) in the READ of sheet 2: D8, E0, wait, D6. Sheet 1 stays.
        self.emu.load(*[Sheet() for _ in range(5)])
        self.emu.chunk_size = 5000
        self.sc.flush_poll = 0.02
        got = []
        self.sc.abort = lambda: len(got) >= 1 and self.emu.cur is not None and \
            self.emu.cur["sides"][0x00]["pos"] > 0
        self.sc.reserve()
        try:
            with self.assertRaises(P.Stopped) as cm:
                self.sc.scan(P.ScanSettings(dpi=150, length=P.mm_to_length(320)), on_sheet=got.append)
        finally:
            self.sc.close()
            self.sc.release()
        self.assertEqual((cm.exception.result.sheets, len(got)), (1, 1))
        ops = self.emu.opcodes()
        d8 = len(ops) - 1 - ops[::-1].index(0xD8)
        self.assertEqual((ops[d8 + 1], ops[-1]), (0xE0, 0xD6))
        self.assertEqual((self.emu.hopper, self.emu.ejected), ([], 4))     # sheet 2 (half read) and 3..5

    def test_long_receipt_auto_length(self):
        self.emu.chunk_size = 200000
        self.emu.load(Sheet(front="blank", back="text", length_mm=825))
        self.sc.reserve()
        try:
            r = self.sc.scan(P.ScanSettings(dpi=150))
        finally:
            self.sc.close()
            self.sc.release()
        w, h, _ = jpeg.info(r.sides[1].jpeg)
        self.assertEqual(h, round(825 / 25.4 * 150))
        self.assertLess(h, 0xA543 * 150 // 1200)


if __name__ == "__main__":
    unittest.main()
