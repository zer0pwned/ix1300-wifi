"""Protocol trace: one JSON line for each message to or from the scanner.

The trace is optional (service.trace_dir, default "" = off). When it is off, the
protocol code keeps None in its "trace" attribute and does no trace work.

Each line is a JSON object with these keys:

  n       sequence number in the file (1, 2, ...)
  t       time.monotonic() in seconds
  wall    time.time() in seconds
  dir     "out" (host to scanner), "in" (scanner to host), "conn" (TCP connect),
          "close" (Port1 close), "err" (an exception in an exchange), "note" (a marker)
  port    "port1", "port2", "search", "keepalive", "trigger", or "" for a note
  ...     other keys for each kind (refer to service/README.md, "Hardware self-test")

Port1 exchanges have two lines with the same "x" value: "out" (cdb, op, out_len,
in_len, data) and "in" (status, scsi, len, data; for REQUEST SENSE also "sense").
The payload "data" is hex, at most payload_bytes bytes ("trunc": true if cut).
Image data (READ with data type 0) is never written: the line has "image" with
the length, the SHA-256 and the first and last 32 bytes.
"""
import hashlib
import json
import os
import threading
import time

DEFAULT_PAYLOAD_BYTES = 256
IMAGE_EDGE = 32


def is_image_read(cdb):
    """True for a READ (0x28) with data type 0 (image data)."""
    return len(cdb) > 2 and cdb[0] == 0x28 and cdb[2] == 0


def image_summary(data):
    data = bytes(data or b"")
    return dict(len=len(data), sha256=hashlib.sha256(data).hexdigest(),
                head=data[:IMAGE_EDGE].hex(), tail=data[-IMAGE_EDGE:].hex() if data else "")


class Trace:
    """A JSON Lines writer. Thread safe. PATH None: keep the records in memory only.

    KEEP: also keep all records in memory (self.records), for the self-test checks.
    MAX_BYTES: when the file is larger, it becomes PATH + ".1" (one old file) and a
    new file starts. 0 = no limit.
    TOTAL_BYTES: when a file starts, the oldest trace files in its directory go away
    until all trace files together are not larger (refer to prune()). 0 = no limit.
    """

    def __init__(self, path=None, payload_bytes=DEFAULT_PAYLOAD_BYTES, keep=False, max_bytes=0, total_bytes=0):
        self.payload_bytes = max(0, int(payload_bytes))
        self.keep = keep
        self.max_bytes = int(max_bytes or 0)
        self.total_bytes = int(total_bytes or 0)
        self.records = []
        self.hook = None             # a function: called with each new record (the self-test)
        self.lock = threading.Lock()
        self.n = 0
        self.x = 0
        self.fh = None
        self.path = None
        self.size = 0
        if path:
            self.switch(path)

    # --- Files ---
    def switch(self, path):
        """Close the current file and write the next records into PATH (append)."""
        with self.lock:
            self._close()
            d = os.path.dirname(path)
            if d:
                os.makedirs(d, exist_ok=True)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            self.fh = os.fdopen(fd, "a", encoding="utf-8")
            self.path = path
            self.size = self.fh.tell()
            self.n = 0
            self._prune()

    def _close(self):
        if self.fh is not None:
            try:
                self.fh.close()
            finally:
                self.fh = None

    def close(self):
        with self.lock:
            self._close()

    def _rotate(self):
        self._close()
        try:
            os.replace(self.path, self.path + ".1")
        except OSError:
            pass
        fd = os.open(self.path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        self.fh = os.fdopen(fd, "a", encoding="utf-8")
        self.size = 0
        self._prune()

    def _prune(self):
        if self.total_bytes:
            prune(os.path.dirname(self.path) or ".", self.total_bytes, keep=self.path)

    # --- Records ---
    def next_x(self):
        with self.lock:
            self.x += 1
            return self.x

    def payload(self, data):
        """Hex of DATA (at most payload_bytes bytes) and the "trunc" flag."""
        data = bytes(data or b"")
        cut = data[:self.payload_bytes]
        return cut.hex(), len(cut) < len(data)

    def add_data(self, rec, data, image=False):
        """Put DATA into REC: "len", and "image" (a summary) or "data" (hex, truncated)."""
        data = data or b""
        rec["len"] = len(data)
        if image:
            rec["image"] = image_summary(data)
        elif data:
            rec["data"], trunc = self.payload(data)
            if trunc:
                rec["trunc"] = True
        return rec

    def write(self, dir, port, **fields):
        rec = dict(t=round(time.monotonic(), 6), wall=round(time.time(), 6), dir=dir, port=port)
        rec.update(fields)
        with self.lock:
            self.n += 1
            rec["n"] = self.n
            if self.keep:
                self.records.append(rec)
            if self.fh is not None:
                line = json.dumps(rec, separators=(",", ":"), default=str) + "\n"
                try:
                    if self.max_bytes and self.size + len(line) > self.max_bytes:
                        self._rotate()
                    self.fh.write(line)
                    self.fh.flush()
                    self.size += len(line)
                except (OSError, ValueError):
                    pass                       # a trace problem never stops a scan
        hook = self.hook
        if hook is not None:
            hook(rec)
        return rec

    def note(self, text, **fields):
        return self.write("note", "", text=text, **fields)

    def mark(self):
        """The index of the next kept record (for since())."""
        with self.lock:
            return len(self.records)

    def since(self, mark):
        with self.lock:
            return list(self.records[mark:])


def is_trace_file(name):
    """True for the names of the trace files: <name>.jsonl and <name>.jsonl.1."""
    return name.endswith(".jsonl") or name.endswith(".jsonl.1")


def prune(d, total_bytes, keep=None):
    """Remove the oldest trace files in directory D until the trace files together have at
    most TOTAL_BYTES. Other files and the file KEEP stay. A trace problem never stops a scan."""
    files = []
    try:
        for name in os.listdir(d):
            fn = os.path.join(d, name)
            if not is_trace_file(name) or (keep and os.path.abspath(fn) == os.path.abspath(keep)):
                continue
            try:
                st = os.stat(fn)
            except OSError:
                continue
            files.append((st.st_mtime, name, st.st_size, fn))
        total = sum(f[2] for f in files)
        if keep:
            total += os.path.getsize(keep) if os.path.exists(keep) else 0
    except OSError:
        return
    for _mtime, _name, size, fn in sorted(files):
        if total <= total_bytes:
            break
        try:
            os.remove(fn)
            total -= size
        except OSError:
            pass


def open_trace(cfg, name="service"):
    """A Trace into service.trace_dir/<NAME>-<time>.jsonl, or None if service.trace_dir is empty."""
    sv = cfg.service
    d = (sv.get("trace_dir") or "").strip()
    if not d:
        return None
    os.makedirs(d, mode=0o700, exist_ok=True)
    path = os.path.join(d, "%s-%s-%d.jsonl" % (name, time.strftime("%Y%m%d-%H%M%S"), os.getpid()))
    return Trace(path, payload_bytes=int(sv.get("trace_payload_bytes", DEFAULT_PAYLOAD_BYTES)),
                 max_bytes=int(float(sv.get("trace_max_mb", 50)) * (1 << 20)),
                 total_bytes=int(float(sv.get("trace_total_mb", 500)) * (1 << 20)))


# --- Summaries for the tests and the self-test -------------------------------

def op_name(rec):
    """A short name of a Port1 "out" record: READ, PSIZE (READ type 0x80), SENSE, or the opcode."""
    cdb = bytes.fromhex(rec.get("cdb", ""))
    if not cdb:
        return "?"
    if cdb[0] == 0x28:
        return "READ" if cdb[2] == 0 else "PSIZE"
    if cdb[0] == 0x03:
        return "SENSE"
    return "%02X" % cdb[0]
