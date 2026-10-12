"""Outputs: HTTP webhook, Paperless-ngx and a local directory.

Delivery is "at least once": if the receiver keeps a document but its reply
does not come back, the service sends the document again later. Each request
has the job ID (header X-Ix1300-Job, webhook field "job_id"), so a receiver
can find copies.

In the service, each output has its own OutputQueue (a thread and a list of
due times): a slow or failing output delays only its own documents.
"""
import hashlib
import heapq
import http.client
import json
import logging
import math
import os
import random
import re
import socket
import ssl
import stat
import threading
import time
import uuid
from urllib.parse import urlsplit

from .pipeline import DIR_MODE, FILE_MODE, safe_filename, sync_dir, write_file

log = logging.getLogger("ix1300.outputs")

# Tests can set this to a function (scheme, host, port, timeout, context) -> HTTPConnection.
# An output can also have its own factory (Output.connection_factory).
CONNECTION_FACTORY = None

MAX_RESPONSE = 64 << 10          # bytes of an HTTP reply that we read


class OutputError(Exception):
    """A failed delivery. PERMANENT: a new attempt with the same configuration fails again."""

    def __init__(self, msg, permanent=False):
        super().__init__(msg)
        self.permanent = permanent


class Body:
    """A request body made of bytes and documents. The documents are read in chunks."""

    def __init__(self, parts):
        self.parts = parts
        self.length = sum(len(p) if isinstance(p, bytes) else p.size for p in parts)

    def __iter__(self):
        for p in self.parts:
            if isinstance(p, bytes):
                yield p
            else:
                for chunk in p.chunks():
                    yield chunk


_CTRL = re.compile(r"[\x00-\x1f\x7f]")
_TASK_ID = re.compile(r"[0-9A-Za-z-]{1,64}\Z")
_TOKEN = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")      # an HTTP header name (RFC 9110 token)
_VISIBLE = re.compile(r"[\x21-\x7e]{1,200}\Z")              # an idempotency key: 1 to 200 visible ASCII


def _quote(value):
    """A quoted-string parameter for Content-Disposition (HTML form rules: %22, %0D, %0A)."""
    v = str(value)
    if _CTRL.search(v.replace("\r", "").replace("\n", "")):
        raise OutputError("control character in a form field name or file name", permanent=True)
    return '"%s"' % v.replace("\\", "\\\\").replace('"', "%22").replace("\r", "%0D").replace("\n", "%0A")


def multipart(fields, files, key=None):
    """Make a multipart/form-data body.

    fields: list of (name, value). files: list of (name, filename, content type, data),
    where data is bytes or an object with "size" and "chunks()" (a Document).
    KEY (for example the job ID) gives a fixed boundary: each attempt for the same
    document then sends the same bytes (receivers that compare idempotent requests).
    Without KEY, the boundary is random.
    Return (Body, content type header).
    """
    if key:
        boundary = "ix1300-" + hashlib.sha256(str(key).encode("utf-8")).hexdigest()[:32]
    else:
        boundary = "ix1300-" + uuid.uuid4().hex
    parts = []
    head = bytearray()
    for name, value in fields:
        head += ("--%s\r\nContent-Disposition: form-data; name=%s\r\n\r\n" % (boundary, _quote(name))).encode()
        head += str(value).encode("utf-8") + b"\r\n"
    for name, filename, ctype, data in files:
        head += ("--%s\r\nContent-Disposition: form-data; name=%s; filename=%s\r\n"
                 "Content-Type: %s\r\n\r\n" % (boundary, _quote(name), _quote(filename), ctype)).encode("utf-8")
        parts.append(bytes(head))
        parts.append(data)
        head = bytearray(b"\r\n")
    head += ("--%s--\r\n" % boundary).encode()
    parts.append(bytes(head))
    return Body(parts), "multipart/form-data; boundary=%s" % boundary


def safe_url(url):
    """URL for logs: no user name, password or query."""
    try:
        u = urlsplit(url)
        host = u.hostname or ""
        if u.port:
            host += ":%d" % u.port
        return "%s://%s%s%s" % (u.scheme, host, u.path, "?..." if u.query else "")
    except ValueError:
        return "(bad URL)"


def _abort(conn):
    sk = getattr(conn, "sock", None)
    if sk is not None:
        try:
            sk.shutdown(socket.SHUT_RDWR)
        except OSError:
            pass


class _Late(Exception):
    """The attempt deadline (max_time) is past."""


def _until(body, deadline):
    """The chunks of BODY. Stop before a chunk after DEADLINE."""
    for chunk in body:
        if time.monotonic() >= deadline:
            raise _Late()
        yield chunk


def http_post(url, body, headers, timeout=60.0, verify_tls=True, max_time=None, factory=None):
    """POST BODY (bytes or Body) to URL. Return (status, the first MAX_RESPONSE bytes of the reply).

    TIMEOUT applies to each socket operation. MAX_TIME stops the full attempt: the
    connection (DNS, TCP and TLS) gets only the time that is left, and no data goes
    out after the deadline. A timer stops the transfer and the reply at the deadline.
    """
    u = urlsplit(url)
    if u.scheme not in ("http", "https") or not u.hostname:
        raise OutputError("URL must start with http:// or https:// and have a host", permanent=True)
    port = u.port or (443 if u.scheme == "https" else 80)
    ctx = None
    if u.scheme == "https":
        ctx = ssl.create_default_context()
        if not verify_tls:
            ctx.check_hostname = False
            ctx.verify_mode = ssl.CERT_NONE
    deadline = time.monotonic() + max_time if max_time else None
    first = timeout if deadline is None else max(0.001, min(timeout, max_time))
    factory = factory or CONNECTION_FACTORY
    if factory is not None:
        conn = factory(u.scheme, u.hostname, port, first, ctx)
    elif u.scheme == "https":
        conn = http.client.HTTPSConnection(u.hostname, port, timeout=first, context=ctx)
    else:
        conn = http.client.HTTPConnection(u.hostname, port, timeout=first)
    path = (u.path or "/") + ("?" + u.query if u.query else "")
    length = body.length if isinstance(body, Body) else len(body)
    hdrs = {"Content-Length": str(length), "User-Agent": "ix1300-service"}
    hdrs.update(headers)
    late = OutputError("no complete reply in %g s" % max_time) if max_time else None
    timer = None
    try:
        # The connection has the time that is left (a socket timeout). Python cannot stop
        # a DNS look-up; then the check after connect() stops the attempt.
        conn.connect()
        if deadline is not None:
            left = deadline - time.monotonic()
            if left <= 0:
                raise late
            if conn.sock is not None:
                conn.sock.settimeout(timeout)
            timer = threading.Timer(left, _abort, [conn])
            timer.daemon = True
            timer.start()
        data = iter(body) if isinstance(body, Body) else body
        if deadline is not None:
            data = _until(data if isinstance(body, Body) else [body], deadline)
        conn.request("POST", path, body=data, headers=hdrs)
        resp = conn.getresponse()
        return resp.status, resp.read(MAX_RESPONSE)
    except _Late:
        raise late
    except OSError as e:
        if deadline is not None and time.monotonic() >= deadline:
            raise late
        raise OutputError(e.__class__.__name__ + (": " + os.strerror(e.errno) if e.errno else ""))
    except http.client.HTTPException as e:
        if deadline is not None and time.monotonic() >= deadline:
            raise late
        raise OutputError("HTTP error: %s" % e.__class__.__name__)
    finally:
        if timer is not None:
            timer.cancel()
        conn.close()


def _status_error(status, detail=""):
    """OutputError for a status that is not 2xx. 4xx (not 408/429) is permanent.
    DETAIL: a short text of the reply, with no secrets (Output.error_text)."""
    return OutputError("HTTP %d" % status + (": " + detail if detail else ""),
                       permanent=400 <= status < 500 and status not in (408, 429))


def _ids(v):
    if v in (None, "", 0):
        return []
    if isinstance(v, (list, tuple)):
        return [int(x) for x in v]
    if isinstance(v, str):
        return [int(x) for x in v.replace(",", " ").split()]
    return [int(v)]


def _num(cfg, key, default, low=0.0):
    v = cfg.get(key, default)
    if isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < low:
        raise ValueError("%s must be a number >= %s" % (key, low))


def _text_map(cfg, key):
    m = cfg.get(key) or {}
    if not isinstance(m, dict):
        raise ValueError("%s must be a table" % key)
    for k, v in m.items():
        if _CTRL.search(str(k)) or _CTRL.search(str(v)) or not str(k).strip():
            raise ValueError("%s: bad name or value for %r (control character)" % (key, str(k)))


def _url(cfg, required=True):
    url = cfg.get("url") or ""
    if not url and not required:
        return
    try:
        u = urlsplit(str(url))
        ok = u.scheme in ("http", "https") and bool(u.hostname)
        u.port
    except ValueError:
        ok = False
    if not ok:
        raise ValueError("url must be http://host... or https://host...")


class Output:
    KEYS = {"type", "retries", "retry_delay", "timeout", "max_time", "verify_tls"}

    def __init__(self, name, cfg, connection_factory=None):
        self.name = name
        self.cfg = cfg
        self.retries = int(cfg.get("retries", 2))
        self.retry_delay = float(cfg.get("retry_delay", 5.0))
        self.connection_factory = connection_factory
        self.stop = None                  # threading.Event: no more waits between attempts
        self.last_error = None
        self.last_permanent = False
        self.last_reply = None
        self.last_file = None             # a dict (path, size, ino): the file that the last send wrote

    @classmethod
    def validate(cls, cfg):
        """Raise ValueError if the output configuration is not usable."""
        unknown = set(cfg) - cls.KEYS
        if unknown:
            raise ValueError("unknown keys %s" % sorted(unknown))
        _num(cfg, "retries", 2)
        if int(cfg.get("retries", 2)) != cfg.get("retries", 2):
            raise ValueError("retries must be an integer")
        _num(cfg, "retry_delay", 5.0)
        _num(cfg, "timeout", 60, 0.1)
        _num(cfg, "max_time", 600, 1)
        if not isinstance(cfg.get("verify_tls", True), bool):
            raise ValueError("verify_tls must be true or false")

    def secrets(self):
        """Text that must not go into the logs."""
        return []

    def redact(self, text, limit=300):
        """TEXT with no secrets, at most LIMIT characters. The cut comes after the replacement,
        so that no part of a secret stays at the end."""
        text = _CTRL.sub(" ", str(text))
        for s in self.secrets():
            if s and len(str(s)) >= 3:
                text = text.replace(str(s), "***")
        return text[:limit]

    def reply_text(self, reply):
        """The reply of the receiver for the manifest: no secrets, at most 200 characters."""
        return self.redact(bytes(reply).decode("utf-8", "replace").strip(), 200)

    def error_text(self, reply):
        """A short part of a failed reply for the log and the manifest: no secrets, at most
        200 characters. The secrets go out of the full text before the cut (redact)."""
        return self.redact(bytes(reply or b"").decode("utf-8", "replace").strip(), 200).strip()

    def _post(self, url, body, headers, default_timeout):
        return http_post(url, body, headers, float(self.cfg.get("timeout", default_timeout)),
                         bool(self.cfg.get("verify_tls", True)), float(self.cfg.get("max_time", 600)),
                         self.connection_factory)

    def send(self, doc):
        raise NotImplementedError

    def attempt(self, doc):
        """One send. Return True on success. Else last_error and last_permanent tell why."""
        self.last_error, self.last_permanent, self.last_file = None, False, None
        try:
            self.last_reply = self.send(doc)
            log.info("output %s: sent %s", self.name, doc.filename)
            return True
        except Exception as e:
            self.last_error = self.redact(e) or e.__class__.__name__
            self.last_permanent = bool(getattr(e, "permanent", False))
            return False

    def retry_wait(self, n, rand=random.random):
        """Seconds before the next attempt after N failed attempts (N >= 1) of one delivery round.

        Exponential backoff with jitter: retry_delay x 2^(N-1) x (0.5 ... 1.0).
        """
        return retry_wait(self.retry_delay, n, rand)

    def deliver(self, doc):
        """Send with retries (this thread waits). Return True on success. Else last_error and
        last_permanent tell why. The service uses attempt() and OutputQueue (no wait in a thread)."""
        for n in range(1, self.retries + 2):
            if self.attempt(doc):
                return True
            log.warning("output %s: attempt %d failed: %s%s", self.name, n, self.last_error,
                        " (no new attempt: change the configuration)" if self.last_permanent else "")
            if self.last_permanent or n > self.retries:
                break
            delay = self.retry_wait(n)
            if self.stop is not None:
                if self.stop.wait(delay):
                    break
            else:
                time.sleep(delay)
        return False


def retry_wait(delay, n, rand=random.random):
    """DELAY x 2^(N-1) x (0.5 ... 1.0): exponential backoff with jitter (RAND gives 0 ... 1)."""
    base = float(delay) * 2 ** max(0, n - 1)
    return base * (0.5 + 0.5 * rand())


def reply_text(out, reply):
    """The text of REPLY (bytes) of OUT for the manifest, or None if OUT cannot remove its secrets."""
    fn = getattr(out, "reply_text", None)
    if fn is not None:
        return fn(reply)
    if hasattr(out, "redact"):
        return out.redact(bytes(reply).decode("utf-8", "replace").strip())[:200]
    return None


def attempt(out, doc):
    """One attempt of OUT (an Output, or any object with deliver(doc)). Return True on success."""
    fn = getattr(out, "attempt", None)
    return bool(fn(doc) if fn is not None else out.deliver(doc))


class OutputQueue:
    """The documents for one output, each with its due time. One thread (run()) sends them,
    one at a time, in the order of the due times (the order of the queue for the same time).

    HANDLER(key) makes one attempt; it can put the key again with a later due time.
    CLOCK gives the time (the due times use it). The thread examines the queue at least
    each POLL seconds. After close(), run() sends the documents that are due, then returns.
    """

    def __init__(self, name, handler, clock=time.monotonic, poll=1.0):
        self.name = name
        self.handler = handler
        self.clock = clock
        self.poll = poll
        self.cond = threading.Condition()
        self.heap = []
        self.keys = set()
        self.seq = 0
        self.closing = False
        self.busy = None                      # the key of the attempt in work

    def put(self, key, due):
        with self.cond:
            if key in self.keys:
                return
            self.keys.add(key)
            self.seq += 1
            heapq.heappush(self.heap, (due, self.seq, key))
            self.cond.notify_all()

    def __len__(self):
        with self.cond:
            return len(self.heap) + (self.busy is not None)

    def close(self):
        with self.cond:
            self.closing = True
            self.cond.notify_all()

    def _next(self):
        with self.cond:
            while True:
                now = self.clock()
                if self.heap and self.heap[0][0] <= now:
                    _due, _seq, key = heapq.heappop(self.heap)
                    self.keys.discard(key)
                    self.busy = key
                    return key
                if self.closing:
                    return None
                self.cond.wait(self.poll if not self.heap else max(0.0, min(self.poll, self.heap[0][0] - now)))

    def run(self):
        while True:
            key = self._next()
            if key is None:
                return
            try:
                self.handler(key)
            except Exception:
                log.exception("output %s: delivery failed", self.name)
            finally:
                with self.cond:
                    self.busy = None


def _job_headers(doc):
    jid = getattr(doc, "job_id", None)
    return {"X-Ix1300-Job": jid} if jid else {}


def _sha256(doc):
    """The lowercase hex SHA-256 of the PDF of DOC. It reads the same chunks as the request body."""
    h = hashlib.sha256()
    for chunk in doc.chunks():
        h.update(chunk)
    return h.hexdigest()


class WebhookOutput(Output):
    """Multipart POST with the PDF and some form fields.

    idempotency_header (for example "Idempotency-Key") adds a key to each request. The key
    is the same for each attempt of a document: "sha256" (the hex SHA-256 of the PDF) or
    "job_id" (the job ID).

    standard_fields = false sends only the PDF and the configured fields (for receivers
    that refuse unknown form fields, or that limit the number of parts).
    """
    KEYS = Output.KEYS | {"url", "field", "headers", "fields", "idempotency_header", "idempotency_value",
                          "standard_fields"}
    IDEMPOTENCY_VALUES = ("sha256", "job_id")
    RESERVED = ("content-type", "content-length", "host", "transfer-encoding", "user-agent", "x-ix1300-job")

    @classmethod
    def validate(cls, cfg):
        super().validate(cfg)
        _url(cfg)
        _text_map(cfg, "headers")
        _text_map(cfg, "fields")
        if _CTRL.search(str(cfg.get("field", "file"))):
            raise ValueError("field: control character")
        if not isinstance(cfg.get("standard_fields", False), bool):
            raise ValueError("standard_fields must be true or false")
        name = cfg.get("idempotency_header", "")
        if not isinstance(name, str):
            raise ValueError("idempotency_header must be a text")
        if cfg.get("idempotency_value", "sha256") not in cls.IDEMPOTENCY_VALUES:
            raise ValueError("idempotency_value must be %s" % " or ".join('"%s"' % v for v in cls.IDEMPOTENCY_VALUES))
        if not name:
            return
        if not _TOKEN.match(name):
            raise ValueError("idempotency_header: %r is not a header name (letters, digits and !#$%%&'*+-.^_`|~)"
                             % name)
        if name.lower() in cls.RESERVED:
            raise ValueError("idempotency_header: the service sets the header %s itself" % name)
        if any(str(k).strip().lower() == name.lower() for k in (cfg.get("headers") or {})):
            raise ValueError("idempotency_header: the header %s is also in headers: remove one of the two" % name)

    def idempotency_key(self, doc):
        """The value of the idempotency header for DOC. The same document gives the same value."""
        if self.cfg.get("idempotency_value", "sha256") == "job_id":
            jid = getattr(doc, "job_id", None)
            if not isinstance(jid, str) or not _VISIBLE.match(jid):
                raise OutputError("idempotency_value = \"job_id\": the job ID is empty, longer than 200 "
                                  "characters or has a character that is not visible ASCII", permanent=True)
            return jid
        return _sha256(doc)

    def secrets(self):
        u = urlsplit(self.cfg.get("url") or "")
        return [str(v) for v in (self.cfg.get("headers") or {}).values()] + [u.password, u.username, u.query]

    def send(self, doc):
        url = self.cfg.get("url")
        if not url:
            raise OutputError("webhook url not set", permanent=True)
        fields = []
        if self.cfg.get("standard_fields", False):
            fields = [("title", doc.title), ("profile", doc.profile), ("pages", doc.pages),
                      ("receipt", "true" if doc.receipt else "false"),
                      ("created", time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(doc.created)))]
            if getattr(doc, "job_id", None):
                fields.append(("job_id", doc.job_id))
            fields.append(("paper_path", getattr(doc, "paper_path", None) or "top"))
            if getattr(doc, "parts", None):          # continue scanning: more than one part
                fields.append(("parts", json.dumps(doc.parts)))
            if doc.warnings:
                fields.append(("warnings", json.dumps(doc.warnings)))
        for k, v in (self.cfg.get("fields") or {}).items():
            fields.append((k, v))
        body, ctype = multipart(fields, [(self.cfg.get("field", "file"), doc.filename, "application/pdf", doc)],
                                key=getattr(doc, "job_id", None))
        headers = {"Content-Type": ctype}
        headers.update(_job_headers(doc))
        headers.update({str(k): str(v) for k, v in (self.cfg.get("headers") or {}).items()})
        if self.cfg.get("idempotency_header"):
            headers[self.cfg["idempotency_header"]] = self.idempotency_key(doc)
        status, resp = self._post(url, body, headers, 60)
        if not 200 <= status < 300:
            raise _status_error(status, self.error_text(resp))
        return resp


class PaperlessOutput(Output):
    """Paperless-ngx: POST /api/documents/post_document/ with token authentication.

    Success means "Paperless accepted the upload" (it returns a task ID). The
    document is made later by the Paperless consumer.
    """
    KEYS = Output.KEYS | {"url", "token", "tags", "receipt_tags", "correspondent", "receipt_correspondent",
                          "document_type", "receipt_document_type", "warning_title_suffix"}

    @classmethod
    def validate(cls, cfg):
        super().validate(cfg)
        _url(cfg)
        if _CTRL.search(str(cfg.get("token") or "")):
            raise ValueError("token: control character (for example a line break)")
        for k in ("tags", "receipt_tags", "correspondent", "receipt_correspondent", "document_type",
                  "receipt_document_type"):
            try:
                _ids(cfg.get(k))
            except (TypeError, ValueError):
                raise ValueError("%s must be IDs (integers)" % k)

    def token(self):
        return self.cfg.get("token") or os.environ.get("PAPERLESS_TOKEN", "")

    def reply_text(self, reply):
        """Only the task ID of the reply (a JSON string, or "task_id" in an object)."""
        try:
            v = json.loads(bytes(reply).decode("utf-8"))
        except ValueError:
            v = None
        if isinstance(v, dict):
            v = v.get("task_id")
        if isinstance(v, str) and _TASK_ID.match(v):
            return v
        return "(no task ID in the reply)"

    def secrets(self):
        u = urlsplit(self.cfg.get("url") or "")
        return [self.token(), u.password, u.username, u.query]

    def send(self, doc):
        base = (self.cfg.get("url") or "").rstrip("/")
        token = self.token()
        if not base or not token:
            raise OutputError("paperless url or token not set", permanent=True)
        if _CTRL.search(token):
            raise OutputError("paperless token has a control character", permanent=True)
        title = doc.title
        suffix = self.cfg.get("warning_title_suffix", " [scan warning]")
        if doc.warnings and suffix:
            title = (title + suffix)[:128]              # the title shows the warning in the document list
        fields = [("title", title),
                  ("created", time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(doc.created)))]
        tags = _ids(self.cfg.get("tags"))
        if doc.receipt:
            tags += [t for t in _ids(self.cfg.get("receipt_tags")) if t not in tags]
        for t in tags:
            fields.append(("tags", t))
        corr = self.cfg.get("receipt_correspondent") if doc.receipt else None
        corr = corr or self.cfg.get("correspondent")
        if corr:
            fields.append(("correspondent", int(corr)))
        dtype = self.cfg.get("receipt_document_type") if doc.receipt else None
        dtype = dtype or self.cfg.get("document_type")
        if dtype:
            fields.append(("document_type", int(dtype)))
        body, ctype = multipart(fields, [("document", doc.filename, "application/pdf", doc)])
        headers = {"Content-Type": ctype, "Authorization": "Token %s" % token, "Accept": "application/json"}
        headers.update(_job_headers(doc))
        status, resp = self._post(base + "/api/documents/post_document/", body, headers, 120)
        if not 200 <= status < 300:
            raise _status_error(status, self.error_text(resp))
        log.info("paperless task %s", self.reply_text(resp))
        return resp


class DirectoryOutput(Output):
    """Write the PDF into a directory. An existing file is never replaced.

    keep = "always": the file stays. keep = "until_sent": the service deletes the file
    when all other outputs of the job have the document (Service._remove_copies).
    """
    KEYS = Output.KEYS | {"path", "file_mode", "dir_mode", "keep"}
    KEEP = ("always", "until_sent")

    def __init__(self, name, cfg, connection_factory=None):
        super().__init__(name, cfg, connection_factory)
        self.keep = cfg.get("keep", "always")

    @classmethod
    def validate(cls, cfg):
        super().validate(cfg)
        for k in ("file_mode", "dir_mode"):
            if k in cfg:
                _mode(cfg[k])
        if cfg.get("keep", "always") not in cls.KEEP:
            raise ValueError("keep must be %s" % " or ".join('"%s"' % k for k in cls.KEEP))

    def folder(self):
        return self.cfg.get("path") or "/data/out"

    def remove_copy(self, rec, job_id=None):
        """Delete the file of REC (a last_file dict from the manifest). Return True if it is deleted.

        The file must be in the folder of this output (not in a subfolder) and have the same
        inode and size as at the write. Else it stays. A file that is not there is not an error.
        """
        path = rec.get("path") if isinstance(rec, dict) else None
        if not isinstance(path, str) or not path:
            return False
        name = os.path.basename(path)
        if (not name or name.startswith(".")
                or os.path.realpath(os.path.dirname(path)) != os.path.realpath(self.folder())):
            log.warning("output %s: job %s: %s is not in %s: the file stays", self.name, job_id, path,
                        self.folder())
            return False
        try:
            st = os.lstat(path)
        except FileNotFoundError:
            log.info("output %s: job %s: %s is not there (moved or deleted)", self.name, job_id, path)
            return False
        except OSError as e:
            log.warning("output %s: job %s: cannot examine %s: %s", self.name, job_id, path, e.strerror or e)
            return False
        if not stat.S_ISREG(st.st_mode) or st.st_ino != rec.get("ino") or st.st_size != rec.get("size"):
            log.warning("output %s: job %s: %s is not the file of the job (changed): the file stays",
                        self.name, job_id, path)
            return False
        try:
            os.remove(path)
            sync_dir(os.path.dirname(path))
        except FileNotFoundError:
            return False
        except OSError as e:
            log.warning("output %s: job %s: cannot delete %s: %s", self.name, job_id, path, e.strerror or e)
            return False
        log.info("output %s: job %s: all other outputs have the document: %s deleted", self.name, job_id, path)
        return True

    def send(self, doc):
        path = self.folder()
        os.makedirs(path, mode=_mode(self.cfg.get("dir_mode", DIR_MODE)), exist_ok=True)
        mode = _mode(self.cfg.get("file_mode", FILE_MODE))
        name = safe_filename(doc.filename)
        base, ext = os.path.splitext(name)
        tmp = write_file(os.path.join(path, ".%s.%s.part" % (base, uuid.uuid4().hex[:8])), doc.chunks(),
                         mode, sync=True)
        try:
            for n in range(1000):
                fn = os.path.join(path, name if n == 0 else "%s-%d%s" % (base, n, ext))
                try:
                    os.link(tmp, fn)                  # fails if FN exists: no file is replaced
                except FileExistsError:
                    continue
                except OSError:                       # no hard links (for example some network shares)
                    # Exclusive create (O_EXCL), then copy: a file of another writer is never
                    # replaced. A failed copy removes FN; only a crash during the copy can leave
                    # a short FN (the job stays in work_dir until the output reports success).
                    try:
                        write_file(fn, _file_chunks(tmp), mode, sync=True)
                    except FileExistsError:
                        continue
                sync_dir(path)
                try:
                    st = os.stat(fn)
                    self.last_file = {"path": fn, "size": st.st_size, "ino": st.st_ino}
                except OSError:                       # no record: the file then stays
                    self.last_file = None
                return fn
            raise OutputError("no free file name for %s in %s" % (name, path))
        finally:
            if os.path.exists(tmp):
                os.remove(tmp)


def _file_chunks(path, size=1 << 20):
    with open(path, "rb") as fh:
        while True:
            b = fh.read(size)
            if not b:
                return
            yield b


def _mode(v):
    """File mode from an integer (0o640) or a text ("0640")."""
    m = int(v, 8) if isinstance(v, str) else int(v)
    if not 0 <= m <= 0o777:
        raise ValueError("bad file mode %r" % v)
    return m


TYPES = {"webhook": WebhookOutput, "paperless": PaperlessOutput, "directory": DirectoryOutput}


def validate(name, cfg):
    """Check one [outputs.NAME] table. Raise ValueError with a clear message."""
    if not isinstance(cfg, dict) or cfg.get("type") not in TYPES:
        raise ValueError("type must be %s" % ", ".join(sorted(TYPES)))
    TYPES[cfg["type"]].validate(cfg)


def build(outputs_cfg, stop=None):
    out = {name: TYPES[c["type"]](name, c) for name, c in outputs_cfg.items()}
    for o in out.values():
        o.stop = stop
    return out


def deliver(doc, outputs, names=None, spool_dir=None):
    """Send DOC to the named outputs (all if NAMES is empty).

    If an output fails (or no output exists), the PDF goes into SPOOL_DIR/unsent
    with a new name (no file is replaced). Return a dict name -> bool.
    """
    names = list(names or outputs.keys())
    status = {}
    for n in names:
        status[n] = outputs[n].deliver(doc)
    if spool_dir and (not names or not all(status.values())):
        d = os.path.join(spool_dir, "unsent")
        fn = DirectoryOutput("unsent", {"path": d}).send(doc)
        log.warning("PDF kept in %s (failed or no outputs: %s)", fn,
                    ", ".join(n for n, ok in status.items() if not ok) or "none")
    return status
