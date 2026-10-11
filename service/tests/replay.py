"""Read a protocol trace (ix1300/trace.py) and compare it with the client or the emulator.

  load(path)              the records of a JSON Lines trace file
  port1(records)          the Port1 "out" records
  ops(records)            short names: READ, PSIZE, SENSE, D5, ...
  jobs(records)           the commands of each scan job (D8 ... D6)
  job_shape(ops)          one job with the repeats folded: READ+ for each side
  assert_job(records, expected, index=0)
  check_emulator(records, emu)    the trace and the emulator log have the same CDBs
  replay_commands(records, scanner, skip=("28",))
                          send the recorded Port1 commands again (for example to the
                          emulator) and compare the SCSI status of each reply
  synthetic_job(sheets)   a trace of a normal top chute job (for the tests)
"""
import json

import support  # noqa: F401  (path setup)
from ix1300.protocol import ScanSettings
from ix1300.trace import Trace, op_name


def load(path):
    out = []
    with open(path, encoding="utf-8") as fh:
        for n, line in enumerate(fh, 1):
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError as e:
                raise ValueError("%s line %d: %s" % (path, n, e))
    return out


def port1(records):
    return [r for r in records if r.get("port") == "port1" and r.get("dir") == "out"]


def ops(records):
    return [op_name(r) for r in port1(records)]


def jobs(records):
    """The command names of each job: from the D8 before a D5 to the D6 after it."""
    names = ops(records)
    out, i = [], 0
    while i < len(names):
        if names[i] != "D5":
            i += 1
            continue
        start = i
        while start > 0 and names[start - 1] != "D8":
            start -= 1
        start = max(0, start - 1)
        end = i
        while end < len(names) and names[end] != "D6":
            end += 1
        out.append(names[start:end + 1])
        i = end + 1
    return out


def job_shape(names):
    """Fold the repeats: SENSE goes away, and a run of READ becomes one "READ+"."""
    out = []
    for n in names:
        if n == "SENSE":
            continue
        if n == "READ":
            n = "READ+"
            if out and out[-1] == "READ+":
                continue
        out.append(n)
    return out


def expected_job(sheets, prolog=("D8", "E9", "D4", "C2", "D5"), end_check=True):
    """The folded command sequence of a normal top chute job with SHEETS sheets."""
    seq = list(prolog)
    for _ in range(sheets):
        seq += ["E0", "READ+", "PSIZE", "READ+", "PSIZE"]
    seq.append("E0")                          # "no paper"
    if end_check:
        seq.append("C2")                      # the jam check after the last sheet
    seq.append("D6")
    return seq


def assert_job(records, expected, index=0):
    got = jobs(records)
    if index >= len(got):
        raise AssertionError("trace has %d jobs, job %d expected" % (len(got), index))
    shape = job_shape(got[index])
    if shape != list(expected):
        raise AssertionError("job %d: %s, expected %s" % (index, " ".join(shape), " ".join(expected)))
    return shape


def check_emulator(records, emu):
    """The CDBs in the trace are the CDBs that the emulator received, in the same order."""
    traced = [bytes.fromhex(r["cdb"]) for r in port1(records)]
    seen = [bytes(c) for _op, c in emu.log]
    if traced != seen:
        for i, (a, b) in enumerate(zip(traced, seen)):
            if a != b:
                raise AssertionError("command %d: trace %s, emulator %s" % (i, a.hex(), b.hex()))
        raise AssertionError("trace has %d commands, the emulator %d" % (len(traced), len(seen)))
    return len(traced)


def replay_commands(records, scanner, skip=("28",)):
    """Send the Port1 commands of RECORDS again with SCANNER.cmd. Return a list of
    (op, recorded scsi, new scsi) for each command whose status is different.
    Commands with an op in SKIP (default: READ) and commands with truncated data are not sent."""
    replies = {r.get("x"): r for r in records if r.get("port") == "port1" and r.get("dir") == "in"}
    diff = []
    for r in port1(records):
        if r.get("op") in skip or r.get("trunc"):
            continue
        cdb = bytes.fromhex(r["cdb"])
        out = bytes.fromhex(r.get("data", ""))
        scsi, _data = scanner.cmd(cdb, out, r.get("in_len", 0))
        old = replies.get(r.get("x"), {}).get("scsi")
        if old != scsi:
            diff.append((r.get("op"), old, scsi))
    return diff


def synthetic_job(sheets=1, reads_per_side=2):
    """A trace (in-memory records) of a normal top chute job, as the client writes it."""
    t = Trace(None, keep=True)

    def cmd(cdb, out=b"", inlen=0, scsi=0, data=b"", sense=None):
        x = t.next_x()
        cdb = bytes(cdb)
        t.write("out", "port1", x=x, op="%02X" % cdb[0], cdb=cdb.hex(), in_len=inlen, **t.add_data({}, out))
        rec = dict(x=x, op="%02X" % cdb[0], status=0, scsi=scsi)
        t.add_data(rec, data, image=cdb[0] == 0x28 and cdb[2] == 0 and len(data) > 2)
        if sense:
            rec["sense"] = sense
        t.write("in", "port1", **rec)

    t.write("conn", "port1", to="192.0.2.10:53218", ok=True)
    cmd([0xD8, 0, 0, 0, 0, 0])
    st = ScanSettings()
    cmd([0xE9, 0, 0, 0, 0, 0, 0, 0x20, 0, 0], out=st.e9_data())
    cmd([0xD4, 0, 0, 0, 0x50, 0], out=st.d4_data())
    cmd([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30, data=bytes(0x30))
    cmd([0xD5, 0, 0, 0, 8, 8], out=bytes(8), inlen=8, data=bytes(8))
    page = 0
    for _ in range(sheets):
        cmd([0xE0, 0, 0, 0, 0, 0])
        for win, pid in ((0, page), (0x80, page + 1)):
            for seq in range(reads_per_side):
                last = seq == reads_per_side - 1
                cdb = [0x28, 0, 0, 0, 0, win, 0x30, 0, 0, 0, pid, seq]
                cmd(cdb, inlen=0x300000, scsi=2 if last else 0, data=bytes([pid, seq]) + b"\xff\xd8" + bytes(100))
                if last:
                    cmd([0x03, 0, 0, 0, 18, 0], inlen=18, data=bytes(18),
                        sense=dict(key=0, asc=0, ascq=0, eom=True, ili=True, info=0))
            cmd([0x28, 0, 0x80, 0, 0, win, 0, 0, 0x20, 0, pid, 0], inlen=0x20, data=bytes(0x20))
        page += 2
    cmd([0xE0, 0, 0, 0, 0, 0], scsi=2)
    cmd([0x03, 0, 0, 0, 18, 0], inlen=18, data=bytes(18), sense=dict(key=3, asc=0x80, ascq=3, eom=False,
                                                                      ili=False, info=0))
    cmd([0xC2, 0, 0, 0, 0, 0, 0, 0, 0x30, 0], inlen=0x30, data=bytes(0x30))
    cmd([0xD6, 0, 0, 0, 0, 0])
    return t.records
