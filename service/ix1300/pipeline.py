"""Image pipeline: page selection, blank removal, rotation, deskew, PDF, OCR.

The core uses the standard library only. Pillow and tesseract are optional.
Without them, these functions use a fallback or do nothing:
  - blank detection and receipt size: a small gray copy (75 dpi) from Pillow
    or from the djpeg program. Without both: JPEG size per pixel (only flat pages);
  - rotation: tesseract OSD on a gray copy at 150 dpi (needs tesseract);
  - deskew: Pillow only (full decode of one page);
  - OCR: tesseract only. Without tesseract, the PDF has no text layer.
The PDF writer embeds the scanner JPEG bytes directly (DCTDecode, no new encode).

Memory: the service works on one page at a time. It keeps only JPEG bytes,
in memory up to pipeline.max_memory_mb for all jobs together (one Budget), and
in files after that. It writes the PDF into a file.
"""
import collections
import io
import logging
import math
import os
import re
import shutil
import subprocess
import secrets
import tempfile
import threading
import time
from dataclasses import dataclass, field
from typing import List, Optional

from . import jpeg

log = logging.getLogger("ix1300.pipeline")

try:                                     # optional
    from PIL import Image                # type: ignore
except ImportError:                      # pragma: no cover - depends on the system
    Image = None

# Pillow refuses a large image as a "decompression bomb": a warning above MAX_IMAGE_PIXELS
# (default 89,478,485) and an error above 2 x MAX_IMAGE_PIXELS. The largest pages of the
# scanner: 600 dpi x 895 mm = 5184 x 21142 = 109.6 M pixels; long page mode, 300 dpi x
# 3,070 mm (E9 length 0x23680) = 2592 x 36256 = 94.0 M pixels. This explicit limit accepts
# them without a warning and refuses images with more than 240 M pixels.
PIL_MAX_PIXELS = 120_000_000
if Image is not None:
    Image.MAX_IMAGE_PIXELS = PIL_MAX_PIXELS

# A page longer than this is a long page (only length = "long" gives one). Its page work
# uses the memory limits of deskew_and_crop (pipeline.max_decode_mb).
LONG_PAGE_MM = 900.0


def have_pillow():
    return Image is not None


def tesseract_path():
    return shutil.which(os.environ.get("IX1300_TESSERACT", "tesseract"))


def _tess_env():
    env = dict(os.environ)
    env.setdefault("OMP_THREAD_LIMIT", "1")      # one thread: small computers (Pi Zero)
    return env


# --- JPEG storage ---------------------------------------------------------

FILE_MODE = 0o600
DIR_MODE = 0o700


def write_file(path, chunks, mode=FILE_MODE, sync=False):
    """Write CHUNKS into a new file PATH (no symlink, private mode). Optionally fsync."""
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(path, flags, mode)
    try:
        with os.fdopen(fd, "wb") as fh:
            for b in chunks:
                fh.write(b)
            if sync:
                fh.flush()
                os.fsync(fh.fileno())
    except BaseException:
        try:
            os.remove(path)
        except OSError:
            pass
        raise
    return path


def commit_file(path, chunks, mode=FILE_MODE, sync=False):
    """Write a temporary file with a random name, then rename it to PATH (atomic)."""
    tmp = "%s.%s.part" % (path, secrets.token_hex(4))
    write_file(tmp, chunks, mode, sync)
    try:
        os.replace(tmp, path)
    except BaseException:
        os.remove(tmp)
        raise
    if sync:
        sync_dir(os.path.dirname(path) or ".")
    return path


def sync_dir(path):
    """fsync a directory (the names in it). Not all systems allow it."""
    try:
        fd = os.open(path, os.O_RDONLY)
    except OSError:
        return
    try:
        os.fsync(fd)
    except OSError:
        pass
    finally:
        os.close(fd)


_UNSAFE = re.compile(r'[\x00-\x1f\x7f/\\:*?"<>|]+')


def safe_filename(name, default="scan.pdf"):
    """Return NAME as one file name: no directory parts, no control characters, ends with .pdf."""
    name = _UNSAFE.sub("-", str(name)).strip(" .-")
    if not name:
        name = default
    if not name.lower().endswith(".pdf"):
        name += ".pdf"
    return name[-200:]


class Budget:
    """Memory limit for JPEG data, shared by all jobs of the service."""

    def __init__(self, limit_bytes):
        self.limit = limit_bytes
        self.used = 0
        self.lock = threading.Lock()

    def take(self, n):
        with self.lock:
            if self.used + n > self.limit:
                return False
            self.used += n
            return True

    def give(self, n):
        with self.lock:
            self.used = max(0, self.used - n)


class Blob:
    """JPEG or PDF data in memory or in a file. KEEP: discard() does not remove the file."""

    def __init__(self, data=None, path=None, store=None, keep=False):
        self.data = data
        self.path = path
        self.store = store
        self.keep = keep
        self.size = len(data) if data is not None else (os.path.getsize(path) if path else 0)

    def read(self):
        if self.data is not None:
            return self.data
        with open(self.path, "rb") as fh:
            return fh.read()

    def chunks(self, size=1 << 20):
        if self.data is not None:
            yield self.data
            return
        with open(self.path, "rb") as fh:
            while True:
                b = fh.read(size)
                if not b:
                    return
                yield b

    def discard(self):
        if self.store is not None:
            self.store.release(self)
        if self.path and not self.keep and os.path.exists(self.path):
            os.remove(self.path)
        self.data = None


class PageStore:
    """Keep JPEG bytes in memory up to the BUDGET. Put more data into files in DIRECTORY.

    BUDGET is shared by all jobs (Budget). Without BUDGET, the store has its own
    budget of LIMIT_BYTES.
    """

    def __init__(self, directory=None, limit_bytes=64 << 20, budget=None):
        self.directory = directory
        self.budget = budget or Budget(limit_bytes)
        self.in_memory = 0
        self.spilled = 0
        self.count = 0
        self.live = set()                          # blobs in memory that the budget counts
        self.lock = threading.Lock()

    @property
    def limit(self):
        return self.budget.limit

    def put(self, data, path=None):
        """Return a Blob for DATA. PATH: a file that already has DATA (used if DATA must go to disk)."""
        with self.lock:
            self.count += 1
            n = self.count
        if self.budget.take(len(data)):
            blob = Blob(data=data, store=self)
            with self.lock:
                self.in_memory += len(data)
                self.live.add(blob)
            return blob
        if self.directory is None:                 # no directory: memory only (not counted)
            return Blob(data=data)
        self.spilled += 1
        if path and os.path.exists(path):
            return Blob(path=path, keep=True)
        return Blob(path=write_file(os.path.join(self.directory, "side%04d.jpg" % n), [data]))

    def release(self, blob):
        n = 0
        with self.lock:
            if blob in self.live:
                self.live.discard(blob)
                n = len(blob.data)
                self.in_memory -= n
        if n:
            self.budget.give(n)
        blob.store = None

    def close(self):
        """Drop the data of all blobs that are still in memory, then give back their memory.

        Call it when the job does not read its pages again (after the PDF and raw.json).
        A blob in a file stays readable.
        """
        with self.lock:
            blobs, self.live = self.live, set()
            n, self.in_memory = self.in_memory, 0
        for b in blobs:
            b.data = None
            b.store = None
        self.budget.give(n)


@dataclass
class Page:
    jpeg_blob: Blob
    width: int
    height: int
    components: int
    dpi: int
    sheet: int = 0
    side: str = "back"
    rotate: int = 0                      # clockwise degrees for the viewer (PDF /Rotate)
    blank: Optional[bool] = None
    analysis: Optional["Analysis"] = None
    oriented: bool = False               # deskew, crop and rotation done
    errors: List[str] = field(default_factory=list)      # failed steps (the page is kept as scanned)
    info: dict = field(default_factory=dict)             # measured values (service.keep_raw)

    @classmethod
    def from_jpeg(cls, data, dpi, sheet=0, side="back", store=None, path=None):
        try:
            w, h, nc = jpeg.check(data)
        except ValueError as e:
            raise ValueError("sheet %d %s: %s" % (sheet + 1, side, e))
        blob = store.put(data, path) if store is not None else Blob(data=data)
        return cls(jpeg_blob=blob, width=w, height=h, components=nc, dpi=dpi, sheet=sheet, side=side)

    @classmethod
    def from_side(cls, side, store=None):
        return cls.from_jpeg(side.jpeg, side.dpi, side.sheet, side.side, store, getattr(side, "path", None))

    @property
    def jpeg(self):
        return self.jpeg_blob.read()

    @property
    def jpeg_size(self):
        return self.jpeg_blob.size

    @property
    def bytes_per_pixel(self):
        return self.jpeg_size / float(max(1, self.width * self.height))

    @property
    def size_mm(self):
        return self.width * 25.4 / self.dpi, self.height * 25.4 / self.dpi


class Document:
    """The PDF (bytes or a file) and its data for the outputs."""

    def __init__(self, filename, title, profile, pages, receipt, created=None, warnings=(),
                 pdf=None, path=None):
        self.filename = filename
        self.title = title
        self.profile = profile
        self.pages = pages
        self.receipt = receipt
        self.created = time.time() if created is None else created
        self.warnings = list(warnings)
        self.blob = Blob(data=pdf) if pdf is not None else Blob(path=path)
        self.job_id = None                # stable ID for the receivers (at-least-once delivery)
        self.paper_path = "top"           # "top" (chute) or "front" (front slot)
        self.kept = set()                 # (sheet, side) of the pages in the PDF
        self.page_records = []            # size, crop and rotation of the pages in the PDF (raw.json)

    @property
    def pdf(self):
        return self.blob.read()

    @property
    def size(self):
        return self.blob.size

    def chunks(self):
        return self.blob.chunks()

    def save(self, path, mode=FILE_MODE):
        """Write the PDF into PATH (atomic replace, private file mode). Return PATH."""
        return commit_file(path, self.chunks(), mode, sync=True)


# --- Page selection -------------------------------------------------------

def side_order(profile):
    """Return the two sides in reading order.

    paper_face only sets the order. With "up" (the default: the stack face up,
    first page on top), the front side comes first. With "down", the back side
    comes first. Which side a simplex
    profile keeps depends on the print (ink ratio), not on paper_face.
    """
    return ("back", "front") if profile.paper_face == "down" else ("front", "back")


def select_sides(result, profile):
    """Put the sides of all sheets in reading order (both sides of each sheet)."""
    by_sheet = {}
    for s in result.sides:
        by_sheet.setdefault(s.sheet, {})[s.side] = s
    out = []
    for sheet in sorted(by_sheet):
        sides = by_sheet[sheet]
        out.extend(sides[n] for n in side_order(profile) if n in sides)
    return out


# --- Gray image analysis -------------------------------------------------
#
# Real scans of receipts: the scanner backing is gray
# (about 212..217), the paper is whiter (more than 225), and print is dark
# (less than 110). A blank side can show the print of the other side as a
# light gray ("show-through"). JPEG size does not show a blank side: color
# blank sides have about the same bytes per pixel as printed sides.

ANALYSIS_DPI = 75
DJPEG_OK = (0, 2)            # 2 = warnings only (the scanner JPEGs with a fixed height give a warning)


def djpeg_path():
    return shutil.which(os.environ.get("IX1300_DJPEG", "djpeg"))


def cjpeg_path():
    return shutil.which(os.environ.get("IX1300_CJPEG", "cjpeg"))


_WS = b" \t\r\n\x0b\x0c"


def _parse_pgm(data):
    """Parse a binary PGM (P5). Return (pixels, width, height). Raise ValueError for bad data."""
    fields, i, n = [], 0, len(data)
    while len(fields) < 4:
        while i < n and data[i] in _WS:
            i += 1
        if i >= n:
            raise ValueError("short PGM header")
        if data[i:i + 1] == b"#":
            j = data.find(b"\n", i)
            if j < 0:
                raise ValueError("PGM comment not closed")
            i = j
            continue
        j = i
        while j < n and data[j] not in _WS:
            j += 1
        if j >= n or j - i > 10:
            raise ValueError("bad PGM header")
        fields.append(data[i:j])
        i = j
    if fields[0] != b"P5" or not all(f.isdigit() for f in fields[1:]):
        raise ValueError("not a binary PGM")
    w, h, maxval = int(fields[1]), int(fields[2]), int(fields[3])
    if not (0 < w <= 65535 and 0 < h <= 65535 and 0 < maxval <= 255):
        raise ValueError("not an 8-bit PGM (%dx%d, %d)" % (w, h, maxval))
    px = data[i + 1:i + 1 + w * h]
    if len(px) != w * h:
        raise ValueError("short PGM")
    return px, w, h


def gray_image(page):
    """Return (pixels, width, height, dpi) of a small gray copy (about 75 dpi), or None.

    Uses Pillow, else the djpeg program (libjpeg). Without both: None.
    """
    scale = max(1, min(8, page.dpi // ANALYSIS_DPI))
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(page.jpeg))
            img.draft("L", (max(1, page.width // scale), max(1, page.height // scale)))
            img = img.convert("L")
            return img.tobytes(), img.size[0], img.size[1], page.dpi * img.size[0] / float(page.width)
        except Exception as e:
            log.debug("Pillow decode failed: %s", e)
    exe = djpeg_path()
    if exe:
        try:
            r = subprocess.run([exe, "-grayscale", "-scale", "1/%d" % scale, "-pnm"], input=page.jpeg,
                               capture_output=True, timeout=60)
            if r.returncode not in DJPEG_OK:
                raise ValueError("djpeg exit status %d" % r.returncode)
            px, w, h = _parse_pgm(r.stdout)
            return px, w, h, page.dpi * w / float(page.width)
        except (OSError, ValueError, IndexError, subprocess.TimeoutExpired) as e:
            log.debug("djpeg decode failed: %s", e)
    return None


@dataclass
class Analysis:
    ink_ratio: float          # dark pixels / (paper + dark pixels)
    paper_width_mm: float     # physical paper width: median distance between the outer paper edges
    paper_length_mm: float    # first to last row with paper
    bbox: tuple = ()          # paper box (x0, y0, x1, y1) in pixels of the gray copy
    size: tuple = ()          # (width, height) of the gray copy
    skew: Optional[float] = None   # correction angle from the paper edges (degrees, Pillow direction)
    paper_level: int = 0      # gray level used for "paper" in this image


# Angle convention (all functions): a positive angle turns the image counter-
# clockwise on the screen, the same as Pillow Image.rotate(angle). The skew
# value is the angle that makes the page straight.

MAX_SKEW = 15.0


def _fit(points):
    """Least squares x = c + s * y with one outlier pass. Return (s, rms) or None."""
    for _ in range(2):
        n = len(points)
        if n < 10:
            return None
        sy = sum(y for y, _ in points)
        sx = sum(x for _, x in points)
        syy = sum(y * y for y, _ in points)
        sxy = sum(x * y for y, x in points)
        den = n * syy - sy * sy
        if den == 0:
            return None
        sl = (n * sxy - sy * sx) / float(den)
        c = (sx - sl * sy) / float(n)
        res = [(x - c - sl * y) for y, x in points]
        rms = math.sqrt(sum(r * r for r in res) / n)
        points = [pt for pt, r in zip(points, res) if abs(r) <= max(1.5, 2 * rms)]
    return sl, rms


def edge_skew(pmask, w, h, y0, y1):
    """Correction angle from the left and right paper edges, or None.

    PMASK has 1 for paper pixels. An edge that touches the image border is not used.
    """
    span = y1 - y0
    if span < 20:
        return None
    run = b"\x01\x01\x01"                      # 3 paper pixels: not a single bright noise pixel
    left, right = [], []
    for y in range(y0 + span // 10, y1 - span // 10):
        a = pmask.find(run, y * w, (y + 1) * w)
        if a < 0:
            continue
        b = pmask.rfind(run, y * w, (y + 1) * w) + 2
        xl, xr = a - y * w, b - y * w
        if xl > 1:
            left.append((y, xl))
        if xr < w - 2:
            right.append((y, xr))
    need = (span * 8 // 10) // 2
    fits = [f for f in (_fit(left) if len(left) >= need else None,
                        _fit(right) if len(right) >= need else None) if f is not None]
    if not fits:
        return None
    if len(fits) == 2 and abs(fits[0][0] - fits[1][0]) < 0.03:
        slope = (fits[0][0] + fits[1][0]) / 2.0
    else:
        slope = min(fits, key=lambda f: f[1])[0]
    angle = -math.degrees(math.atan(slope))
    return angle if abs(angle) <= MAX_SKEW else None


def ink_skew(gray, w, h, ink_level=110, max_angle=10.0, step=0.25, max_points=30000):
    """Correction angle from the lines of print (projection profile), or None."""
    tab = bytes(1 if v < ink_level else 0 for v in range(256))
    mask = gray.translate(tab)
    total = mask.count(1)
    if total < 50:
        return None
    every = max(1, total // max_points)
    pts = []
    i, k = mask.find(1), 0
    while i >= 0:
        if k % every == 0:
            pts.append((i % w, i // w))
        k += 1
        i = mask.find(1, i + 1)
    best, best_score = None, -1
    a = -max_angle
    while a <= max_angle + 1e-9:
        t = math.tan(math.radians(a))
        hist = {}
        for x, y in pts:
            key = int(y - x * t)
            hist[key] = hist.get(key, 0) + 1
        score = sum(c * c for c in hist.values())
        if score > best_score:
            best, best_score = a, score
        a += step
    return best


def rotate_gray(gray, w, h, angle, fill=215):
    """Turn a gray image by ANGLE degrees (Pillow direction), same size, nearest pixel."""
    r = math.radians(angle)
    c, s = math.cos(r), math.sin(r)
    cx, cy = (w - 1) / 2.0, (h - 1) / 2.0
    out = bytearray([fill]) * (w * h)
    for y in range(h):
        dy = y - cy
        for x in range(w):
            dx = x - cx
            sx = int(round(cx + dx * c - dy * s))
            sy = int(round(cy + dx * s + dy * c))
            if 0 <= sx < w and 0 <= sy < h:
                out[y * w + x] = gray[sy * w + sx]
    return bytes(out)


def paper_box(pmask, imask, w, h, rows, ys, dpi, max_grow_mm=5.0):
    """Outer paper box (x0, y0, x1, y1) that does not cut through print.

    1. Start at the core (rows and columns with much paper). Then grow while a
       row still has some paper (torn or slanted receipt ends stay in), and
       while a column has at least 10 % paper (a bright backing stays out).
    2. Grow each side to the last line with ink in the next MAX_GROW_MM, so the
       box never cuts through a line of print.
    """
    top = max(rows)
    cols = [pmask[x::w].count(1) for x in range(w)]
    ctop = max(cols)
    xs = [x for x in range(w) if cols[x] >= 0.25 * ctop]
    y0, y1, x0, x1 = ys[0], ys[-1], xs[0], xs[-1]
    rmin, cmin = max(3, int(0.02 * top)), max(3, int(0.02 * ctop))
    while y0 > 0 and rows[y0 - 1] >= rmin:
        y0 -= 1
    while y1 < h - 1 and rows[y1 + 1] >= rmin:
        y1 += 1

    # Columns: grow only into columns where at least 10 % of the box rows are paper. A bright
    # scanner backing can have some "paper" pixels in each column [HW]; 3 pixels are not sufficient.
    def col_paper(x):
        return pmask[y0 * w + x:y1 * w + x + 1:w].count(1)

    cneed = max(cmin, int(0.10 * (y1 - y0 + 1)))
    while x0 > 0 and col_paper(x0 - 1) >= cneed:
        x0 -= 1
    while x1 < w - 1 and col_paper(x1 + 1) >= cneed:
        x1 += 1

    def row_ink(y):
        return imask.find(1, y * w + x0, y * w + x1 + 1) >= 0

    def col_ink(x):
        return 1 in imask[y0 * w + x:y1 * w + x + 1:w]

    # Ink near an edge (at most MAX_GROW_MM out): grow the box to the last line with ink.
    # A slanted corner can have a gap of backing between the box edge and its print.
    limit = max(1, int(max_grow_mm / 25.4 * dpi))
    grown = {"t": 0, "b": 0, "l": 0, "r": 0}
    changed = True
    while changed:
        changed = False
        for d in "tblr":
            left = limit - grown[d]
            if left <= 0:
                continue
            if d == "t":
                far = [y for y in range(max(0, y0 - left), y0) if row_ink(y)]
                n = y0 - far[0] if far else 0
                y0 -= n
            elif d == "b":
                far = [y for y in range(y1 + 1, min(h, y1 + 1 + left)) if row_ink(y)]
                n = far[-1] - y1 if far else 0
                y1 += n
            elif d == "l":
                far = [x for x in range(max(0, x0 - left), x0) if col_ink(x)]
                n = x0 - far[0] if far else 0
                x0 -= n
            else:
                far = [x for x in range(x1 + 1, min(w, x1 + 1 + left)) if col_ink(x)]
                n = far[-1] - x1 if far else 0
                x1 += n
            if n:
                grown[d] += n
                changed = True
    return x0, y0, x1 + 1, y1 + 1


def paper_threshold(gray, ink_level=110, paper_level=225):
    """Gray level above which a pixel is paper, for this image.

    The scanner backing is not equally bright on all scanners and sides (206 to
    226 [HW]). The paper makes a peak in the histogram above PAPER_LEVEL + 5.
    The threshold is the lowest point of the histogram in the 20 levels below
    that peak (between backing and paper), but never less than PAPER_LEVEL.
    """
    counts = collections.Counter(gray)         # one pass over the image (not 256 passes)
    hist = [counts[v] for v in range(256)]
    sm = [sum(hist[max(0, v - 2):v + 3]) for v in range(256)]
    lo = min(250, paper_level + 6)             # a bright backing can make a peak at 224..226 [HW]
    if sum(hist[lo:]) < 0.01 * max(1, len(gray)):
        return paper_level                     # no clear paper: keep the configured level
    peak = max(range(lo, 255), key=lambda v: sm[v])
    if peak == lo:
        return paper_level                     # the histogram falls from LO: the paper peak is lower
    a, b = max(ink_level, peak - 20), peak - 3
    if b <= a:
        return paper_level
    valley = min(range(a, b + 1), key=lambda v: (sm[v], -v))
    return max(paper_level, min(valley, peak - 3))


def outer_width(pmask, w, box, y0, y1, skew=None):
    """Physical paper width in pixels: the median distance between the outer paper edges of a row.

    Only the middle 80 % of the rows from Y0 to Y1 and the columns of BOX count.
    The pixels between the two edges count as paper (print, shade and wrinkles
    make holes in the paper mask, not a narrower paper). With SKEW (degrees),
    the horizontal distance becomes the width across the paper. Return None if
    no row has paper edges.
    """
    x0, _by0, x1, _by1 = box
    span = y1 - y0
    run = b"\x01\x01\x01"                      # 3 paper pixels: not a single bright noise pixel
    widths = []
    for y in range(y0 + span // 10, y1 - span // 10 + 1):
        a = pmask.find(run, y * w + x0, y * w + x1)
        if a < 0:
            continue
        b = pmask.rfind(run, y * w + x0, y * w + x1) + 3
        widths.append(b - a)
    if not widths:
        return None
    widths.sort()
    width = widths[len(widths) // 2]
    if skew is not None:
        width *= math.cos(math.radians(skew))
    return width


def analyze(gray, w, h, dpi, ink_level=110, paper_level=225):
    """Measure the print, the paper size, the paper box and the edge skew in a gray image.

    Two masks: the intensity mask (paper and print pixels) gives the ink ratio
    for the blank test. The paper geometry (box, width) uses the outer edges of
    the paper, so print and shade inside the paper do not make it narrower.
    """
    paper_level = paper_threshold(gray, ink_level, paper_level)
    ink_tab = bytes(1 if v < ink_level else 0 for v in range(256))
    paper_tab = bytes(1 if (v > paper_level or v < ink_level) else 0 for v in range(256))
    imask = gray.translate(ink_tab)
    ink = imask.count(1)
    pmask = gray.translate(paper_tab)
    rows = [pmask.count(1, y * w, (y + 1) * w) for y in range(h)]
    paper = sum(rows)
    top = max(rows) if rows else 0
    ys = [y for y in range(h) if top and rows[y] >= 0.25 * top]
    if not ys:
        return Analysis(0.0, 0.0, 0.0, (), (w, h), None, paper_level)
    box = paper_box(pmask, imask, w, h, rows, ys, dpi)
    skew = edge_skew(pmask, w, h, ys[0], ys[-1])
    width = outer_width(pmask, w, box, ys[0], ys[-1], skew)
    if width is None:
        width = sorted(rows[y] for y in ys)[len(ys) // 2]
    return Analysis(ink_ratio=ink / float(max(1, paper)),
                    paper_width_mm=width * 25.4 / dpi,
                    paper_length_mm=(ys[-1] - ys[0] + 1) * 25.4 / dpi,
                    bbox=box, size=(w, h), skew=skew, paper_level=paper_level)


def page_record(page):
    """Size and measured values of PAGE (as it is now) for raw.json."""
    a = page.analysis
    return dict(width=page.width, height=page.height, dpi=page.dpi,
                ink_ratio=None if a is None else round(a.ink_ratio, 5),
                paper_level=None if a is None else a.paper_level,
                paper_mm=None if a is None else [round(a.paper_width_mm, 1), round(a.paper_length_mm, 1)],
                paper_box_gray=None if a is None else list(a.bbox), gray_size=None if a is None else list(a.size),
                edge_skew=None if a is None or a.skew is None else round(a.skew, 2))


def page_analysis(page, cfg):
    """Return the Analysis of PAGE (kept on the page), or None without a decoder."""
    if page.analysis is None:
        g = gray_image(page)
        if g is None:
            return None
        page.analysis = analyze(g[0], g[1], g[2], g[3], int(cfg.get("ink_level", 110)),
                                int(cfg.get("paper_level", 225)))
    return page.analysis


_warned = set()


def is_blank(page, cfg):
    """True if the page has no print.

    blank_method: "auto" (pixels if a decoder is available, else size), "pixels", "size".
    """
    method = str(cfg.get("blank_method", "auto")).lower()
    if method in ("auto", "pixels"):
        a = page_analysis(page, cfg)
        if a is not None:
            return a.ink_ratio < float(cfg.get("blank_ink_ratio", 0.004))
        if method == "pixels" or "nodecoder" not in _warned:
            _warned.add("nodecoder")
            log.warning("no JPEG decoder (Pillow or djpeg): blank detection uses the JPEG size only")
    limit = float(cfg.get("blank_bytes_per_pixel", 0.01))
    if page.components == 1:
        limit *= 0.6
    return page.bytes_per_pixel < limit


# --- Rotation and deskew --------------------------------------------------

_ROT = re.compile(r"Rotate:\s*(\d+)")
_CONF = re.compile(r"Orientation confidence:\s*([\d.]+)")
OSD_DPI = 150


def _gray_file(page, directory, target_dpi=OSD_DPI):
    """Write a gray copy of PAGE at about TARGET_DPI. Return (file name, dpi)."""
    scale = max(1, min(8, page.dpi // target_dpi))
    if Image is not None:
        try:
            img = Image.open(io.BytesIO(page.jpeg))
            img.draft("L", (max(1, page.width // scale), max(1, page.height // scale)))
            img = img.convert("L")
            img.save(os.path.join(directory, "page.pgm"))
            return "page.pgm", round(page.dpi * img.size[0] / float(page.width))
        except Exception as e:
            log.debug("Pillow gray copy failed: %s", e)
    exe = djpeg_path()
    if exe:
        try:
            with open(os.path.join(directory, "page.pgm"), "wb") as fh:
                r = subprocess.run([exe, "-grayscale", "-scale", "1/%d" % scale, "-pnm"], input=page.jpeg,
                                   stdout=fh, stderr=subprocess.PIPE, timeout=60)
            if r.returncode in DJPEG_OK:
                return "page.pgm", page.dpi // scale
        except (OSError, subprocess.TimeoutExpired) as e:
            log.debug("djpeg gray copy failed: %s", e)
    with open(os.path.join(directory, "page.jpg"), "wb") as fh:
        fh.write(page.jpeg)
    return "page.jpg", page.dpi


def detect_rotation(page, min_conf=2.0, timeout=60.0):
    """Return the clockwise rotation (0, 90, 180, 270) from tesseract OSD, or 0."""
    exe = tesseract_path()
    if not exe:
        return 0
    with tempfile.TemporaryDirectory() as tmp:
        # Relative names: Leptonica on macOS changes paths that start with /tmp.
        name, dpi = _gray_file(page, tmp)
        try:
            r = subprocess.run([exe, name, "stdout", "--psm", "0", "--dpi", str(dpi)],
                               capture_output=True, text=True, timeout=timeout, cwd=tmp, env=_tess_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("tesseract OSD failed: %s", e)
            return 0
    text = r.stdout + r.stderr
    m, c = _ROT.search(text), _CONF.search(text)
    if not m:
        log.debug("OSD gave no result: %s", text.strip()[:200])
        return 0
    rot, conf = int(m.group(1)) % 360, float(c.group(1)) if c else 0.0
    if conf < min_conf:
        log.debug("OSD rotation %d with low confidence %.2f: ignored", rot, conf)
        return 0
    return rot


def _encode(img, page, quality):
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=quality, dpi=(page.dpi, page.dpi))
    return buf.getvalue()


def jpegtran_path():
    return shutil.which(os.environ.get("IX1300_JPEGTRAN", "jpegtran"))


def skew_angle(page, cfg):
    """Correction angle of PAGE: paper edges first, then the lines of print. None if not known."""
    a = page_analysis(page, cfg)
    if a is not None and a.skew is not None:
        return a.skew
    g = gray_image(page)
    if g is None:
        return None
    return ink_skew(g[0], g[1], g[2], int(cfg.get("ink_level", 110)))


def _crop_box(a, page, margin_mm=2.0):
    """Paper box of analysis A in full-size pixels, with a margin. None if no box."""
    if a is None or not a.bbox:
        return None
    gw, gh = a.size
    fx, fy = page.width / float(gw), page.height / float(gh)
    m = margin_mm / 25.4 * page.dpi
    x0, y0, x1, y1 = a.bbox
    box = (max(0, int(x0 * fx - m)), max(0, int(y0 * fy - m)),
           min(page.width, int(math.ceil(x1 * fx + m))), min(page.height, int(math.ceil(y1 * fy + m))))
    if box[2] - box[0] < 16 or box[3] - box[1] < 16:
        return None
    if box == (0, 0, page.width, page.height):
        return None
    return box


def _new_page(page, data, store):
    new = Page.from_jpeg(data, page.dpi, page.sheet, page.side, store)
    new.rotate, new.blank = page.rotate, page.blank
    new.errors, new.info = page.errors, page.info
    page.jpeg_blob.discard()
    return new


def is_long_page(page):
    """True if PAGE is longer than the automatic length maximum (long page mode)."""
    return page.height * 25.4 / max(1, page.dpi) > LONG_PAGE_MM


def decode_bytes(page):
    """Estimated Pillow memory for a deskew of PAGE: the decoded image, the turned
    image and one more copy. Pillow keeps "L" in 1 byte, "RGB" in 4 bytes per pixel."""
    return 3 * page.width * page.height * (1 if page.components == 1 else 4)


def decode_allowed(page, cfg):
    """True if a full Pillow decode of PAGE stays in pipeline.max_decode_mb."""
    return decode_bytes(page) <= float(cfg.get("max_decode_mb", 192)) * (1 << 20)


def _pillow_deskew(page, cfg, angle, crop, store, quality):
    """Turn PAGE by ANGLE with Pillow (and cut it to the paper with CROP). Return the new page."""
    img = Image.open(io.BytesIO(page.jpeg))
    fill = 215 if img.mode == "L" else (215, 215, 215)       # the color of the scanner backing
    bicubic = getattr(getattr(Image, "Resampling", Image), "BICUBIC")
    out = img.rotate(angle, resample=bicubic, expand=True, fillcolor=fill)
    del img
    if crop:
        small = out.convert("L")
        scale = max(1, page.dpi // ANALYSIS_DPI)
        small = small.reduce(scale) if scale > 1 else small
        a = analyze(small.tobytes(), small.size[0], small.size[1], page.dpi / float(scale),
                    int(cfg.get("ink_level", 110)), int(cfg.get("paper_level", 225)))
        del small
        tmp = Page(Blob(data=b""), out.size[0], out.size[1], 3, page.dpi)
        box = _crop_box(a, tmp)
        if box:
            out = out.crop(box)
            page.info["crop_box"] = list(box)
    log.info("deskew sheet %d %s: %.2f degrees%s", page.sheet + 1, page.side, angle,
             ", cropped" if crop else "")
    new = _new_page(page, _encode(out, page, quality), store)
    new.info["deskew_applied"] = True
    return new


def _stream_crop(page, box, quality, timeout=300):
    """Cut PAGE to BOX with "djpeg -crop | cjpeg": one new JPEG encode, a few MB of memory.

    libjpeg-turbo djpeg moves the left edge to an iMCU step (8 or 16 pixels). Return the
    JPEG bytes, or None if the programs are missing or fail.
    """
    dj, cj = djpeg_path(), cjpeg_path()
    if not (dj and cj):
        return None
    x0, y0, x1, y1 = box
    with tempfile.TemporaryDirectory() as tmp:
        src = page.jpeg_blob.path
        if not src:
            src = os.path.join(tmp, "page.jpg")
            with open(src, "wb") as fh:
                for chunk in page.jpeg_blob.chunks():
                    fh.write(chunk)
        p1 = p2 = None
        try:
            p1 = subprocess.Popen([dj, "-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0), "-pnm", src],
                                  stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
            p2 = subprocess.Popen([cj, "-quality", str(quality)], stdin=p1.stdout, stdout=subprocess.PIPE,
                                  stderr=subprocess.PIPE)
            p1.stdout.close()
            out, err = p2.communicate(timeout=timeout)
            p1.wait(timeout=timeout)
        except (OSError, subprocess.TimeoutExpired) as e:
            for p in (p1, p2):
                if p is not None and p.poll() is None:
                    p.kill()
                    p.wait()
            log.warning("stream crop failed: %s", e)
            return None
    if p1.returncode not in DJPEG_OK or p2.returncode != 0 or jpeg.info(out) is None:
        log.warning("stream crop failed: djpeg %s, cjpeg %s: %s", p1.returncode, p2.returncode,
                    err.decode("utf-8", "replace").strip()[:200])
        return None
    return out


def _long_page(page, cfg, deskew, crop, store, min_angle, quality):
    """deskew_and_crop for a long page, with a limit for the memory.

    A full Pillow decode of a 3,000 mm page needs much memory (2592 x 35433 pixels:
    92 MB gray, 367 MB colour for each copy), and jpegtran keeps all coefficients
    of the page in memory (measured: 256 MB gray, 383 MB colour). So: first cut the
    page to the paper with djpeg | cjpeg (a few MB), then deskew the narrower page
    with Pillow only if decode_bytes() stays in pipeline.max_decode_mb.
    """
    if crop:
        box = _crop_box(page_analysis(page, cfg), page)
        if box is not None:
            data, method = _stream_crop(page, box, quality), "stream"
            if data is None and decode_allowed(page, cfg):
                data, method = _small_crop(page, box, quality), "small"
            if data is None:
                log.info("crop of a long page needs djpeg and cjpeg (or a smaller page): skipped")
            else:
                log.info("crop sheet %d %s (long page, %s): %dx%d -> %s", page.sheet + 1, page.side, method,
                         page.width, page.height, "x".join(map(str, (jpeg.info(data) or (0, 0))[:2])))
                page.info["crop_box"] = list(box)
                page.info["crop_method"] = method
                page = _new_page(page, data, store)
    if not deskew:
        return page
    angle = skew_angle(page, cfg)
    page.info["skew"] = None if angle is None else round(angle, 2)
    page.info["deskew_applied"] = False
    if angle is None or abs(angle) < min_angle:
        return page
    if not decode_allowed(page, cfg):
        mb = decode_bytes(page) / float(1 << 20)
        page.info["deskew_skipped"] = "about %d MB, more than pipeline.max_decode_mb" % round(mb)
        log.info("deskew sheet %d %s: skipped, the long page (%dx%d) needs about %d MB "
                 "(pipeline.max_decode_mb = %s)", page.sheet + 1, page.side, page.width, page.height,
                 round(mb), cfg.get("max_decode_mb", 192))
        return page
    if Image is None:
        log.info("deskew needs Pillow: skipped (%.2f degrees)", angle)
        return page
    return _pillow_deskew(page, cfg, angle, crop, store, quality)


def _small_crop(page, box, quality):
    """Crop with jpegtran (lossless) or Pillow. For pages that decode_allowed() accepts."""
    exe = jpegtran_path()
    x0, y0, x1, y1 = box
    if exe:
        try:
            r = subprocess.run([exe, "-copy", "all", "-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0)],
                               input=page.jpeg, capture_output=True, timeout=60)
            if r.returncode == 0 and jpeg.info(r.stdout) is not None:
                return r.stdout
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("jpegtran crop failed: %s", e)
    if Image is not None:
        return _encode(Image.open(io.BytesIO(page.jpeg)).crop(box), page, quality)
    return None


def deskew_and_crop(page, cfg, deskew=True, crop=False, store=None, min_angle=0.3):
    """Straighten the page and/or cut it to the paper box. Return the new page (or PAGE).

    Deskew needs Pillow (one new JPEG encode). Crop without deskew uses Pillow,
    else jpegtran (no new encode; the box edges go to 8 or 16 pixel steps).
    A long page (length = "long") has its own steps with memory limits (_long_page).
    """
    quality = int(cfg.get("jpeg_quality", 90))
    if is_long_page(page):
        return _long_page(page, cfg, deskew, crop, store, min_angle, quality)
    angle = skew_angle(page, cfg) if deskew else None
    page.info["skew"] = None if angle is None else round(angle, 2)     # measured, not always applied
    page.info["deskew_applied"] = False
    if angle is not None and abs(angle) >= min_angle:
        if Image is None:
            log.info("deskew needs Pillow: skipped (%.2f degrees)", angle)
        else:
            return _pillow_deskew(page, cfg, angle, crop, store, quality)
    if not crop:
        return page
    box = _crop_box(page_analysis(page, cfg), page)
    if box is None:
        return page
    x0, y0, x1, y1 = box
    page.info["crop_box"] = list(box)
    data = None
    exe = jpegtran_path()
    if exe:                                   # lossless, small memory: first choice
        try:
            r = subprocess.run([exe, "-copy", "all", "-crop", "%dx%d+%d+%d" % (x1 - x0, y1 - y0, x0, y0)],
                               input=page.jpeg, capture_output=True, timeout=60)
            if r.returncode == 0 and jpeg.info(r.stdout) is not None:
                data = r.stdout
            else:
                log.warning("jpegtran crop failed: %s", r.stderr.decode("utf-8", "replace").strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("jpegtran crop failed: %s", e)
    if data is None and Image is not None:
        data = _encode(Image.open(io.BytesIO(page.jpeg)).crop(box), page, quality)
    if data is None:
        log.info("crop needs jpegtran or Pillow: skipped")
        return page
    log.info("crop sheet %d %s: %dx%d -> %dx%d", page.sheet + 1, page.side, page.width, page.height,
             x1 - x0, y1 - y0)
    return _new_page(page, data, store)


def rotate_pixels(page, quality=90):
    """Turn the pixels and set rotate to 0. For OCR input. Return the new page, or None if not possible.

    Uses jpegtran (no new encode; up to 15 edge pixels can go) or Pillow.
    """
    if page.rotate == 0:
        return page
    exe = jpegtran_path()
    if exe:
        try:
            r = subprocess.run([exe, "-copy", "none", "-trim", "-rotate", str(page.rotate)], input=page.jpeg,
                               capture_output=True, timeout=60)
            if r.returncode == 0 and jpeg.info(r.stdout) is not None:
                return Page.from_jpeg(r.stdout, page.dpi, page.sheet, page.side)
            log.warning("jpegtran rotate failed: %s", r.stderr.decode("utf-8", "replace").strip()[:200])
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("jpegtran rotate failed: %s", e)
    if Image is None:
        return None
    img = Image.open(io.BytesIO(page.jpeg))
    tr = getattr(Image, "Transpose", Image)
    op = {90: tr.ROTATE_270, 180: tr.ROTATE_180, 270: tr.ROTATE_90}[page.rotate]
    out = img.transpose(op)
    del img
    return Page.from_jpeg(_encode(out, page, quality), page.dpi, page.sheet, page.side)


# --- PDF writer -----------------------------------------------------------

def _pdf_text(s):
    try:
        b = s.encode("latin-1")
        if all(32 <= c < 127 for c in b):
            return b"(" + b.replace(b"\\", b"\\\\").replace(b"(", b"\\(").replace(b")", b"\\)") + b")"
    except UnicodeEncodeError:
        pass
    return b"<FEFF" + s.encode("utf-16-be").hex().upper().encode() + b">"


def _num(v):
    return ("%.3f" % v).rstrip("0").rstrip(".").encode()


class _Counter:
    def __init__(self, fh):
        self.fh = fh
        self.pos = 0

    def write(self, b):
        self.fh.write(b)
        self.pos += len(b)


def write_pdf(pages, title="", producer="ix1300", created=None, fh=None):
    """Make a PDF. Each page shows one JPEG at its physical size (from the DPI).

    Without FH: return the PDF bytes. With FH: write into FH (one JPEG at a time
    in memory) and return the size.
    """
    if not pages:
        raise ValueError("no pages")
    buf = io.BytesIO() if fh is None else None
    out = _Counter(fh if fh is not None else buf)
    out.write(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = {}

    def obj(num, body, stream=None):
        offsets[num] = out.pos
        out.write(b"%d 0 obj\n" % num + body)
        if stream is not None:
            out.write(b"\nstream\n")
            for b in stream:
                out.write(b)
            out.write(b"\nendstream")
        out.write(b"\nendobj\n")

    n = len(pages)
    page_nums = [4 + 3 * i for i in range(n)]
    t = time.localtime(created or time.time())
    obj(1, b"<< /Type /Catalog /Pages 2 0 R >>")
    obj(2, b"<< /Type /Pages /Kids [" + b" ".join(b"%d 0 R" % p for p in page_nums) + b"] /Count %d >>" % n)
    info = b"<< /Producer " + _pdf_text(producer) + b" /CreationDate (D:" + time.strftime("%Y%m%d%H%M%S", t).encode() + b")"
    if title:
        info += b" /Title " + _pdf_text(title)
    obj(3, info + b" >>")
    for i, pg in enumerate(pages):
        pn, cn, im = page_nums[i], page_nums[i] + 1, page_nums[i] + 2
        w = pg.width * 72.0 / pg.dpi
        h = pg.height * 72.0 / pg.dpi
        page = (b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 " + _num(w) + b" " + _num(h) + b"]"
                b" /Resources << /XObject << /Im0 %d 0 R >> >> /Contents %d 0 R" % (im, cn))
        if pg.rotate:
            page += b" /Rotate %d" % pg.rotate
        obj(pn, page + b" >>")
        content = b"q " + _num(w) + b" 0 0 " + _num(h) + b" 0 0 cm /Im0 Do Q"
        obj(cn, b"<< /Length %d >>" % len(content), [content])
        cs = {1: b"/DeviceGray", 3: b"/DeviceRGB", 4: b"/DeviceCMYK"}.get(pg.components, b"/DeviceRGB")
        extra = b""
        if pg.components == 4 and b"Adobe" in next(pg.jpeg_blob.chunks(), b"")[:200]:
            extra = b" /Decode [1 0 1 0 1 0 1 0]"
        obj(im, b"<< /Type /XObject /Subtype /Image /Width %d /Height %d /ColorSpace " % (pg.width, pg.height) +
            cs + b" /BitsPerComponent 8 /Filter /DCTDecode" + extra + b" /Length %d >>" % pg.jpeg_size,
            pg.jpeg_blob.chunks())
    size = 4 + 3 * n
    xref = out.pos
    out.write(b"xref\n0 %d\n0000000000 65535 f \n" % size)
    out.write(b"".join(b"%010d 00000 n \n" % offsets[num] for num in range(1, size)))
    out.write(b"trailer\n<< /Size %d /Root 1 0 R /Info 3 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (size, xref))
    return buf.getvalue() if buf is not None else out.pos


def ocr_pdf(pages, lang="eng", title="", timeout=600, out_path=None):
    """Make a searchable PDF with tesseract (one page at a time, one thread).

    Return the PDF bytes (or OUT_PATH when it is set), or None if tesseract is
    not available or fails.
    """
    exe = tesseract_path()
    if not exe:
        return None
    dpis = {p.dpi for p in pages}
    with tempfile.TemporaryDirectory() as tmp:
        # Relative names: Leptonica on macOS changes paths that start with /tmp.
        names = []
        for i, p in enumerate(pages):
            fn = "p%03d.jpg" % i
            turned = rotate_pixels(p)
            if turned is None:              # the OCR PDF would lose the rotation: use the normal PDF
                log.warning("OCR: cannot turn page %d (no jpegtran or Pillow): PDF without text layer", i + 1)
                return None
            write_file(os.path.join(tmp, fn), turned.jpeg_blob.chunks())
            names.append(fn)
        with open(os.path.join(tmp, "list.txt"), "w") as fh:
            fh.write("\n".join(names) + "\n")
        base = os.path.join(tmp, "out")
        cmd = [exe, "list.txt", "out", "-l", lang]
        if len(dpis) == 1:
            cmd += ["--dpi", str(dpis.pop())]
        if title:
            cmd += ["-c", "document_title=%s" % title]
        cmd += ["pdf"]
        try:
            r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout, cwd=tmp, env=_tess_env())
        except (OSError, subprocess.TimeoutExpired) as e:
            log.warning("OCR failed: %s", e)
            return None
        if r.returncode != 0 or not os.path.exists(base + ".pdf"):
            log.warning("OCR failed (%d): %s", r.returncode, r.stderr.strip()[:300])
            return None
        if out_path:
            # The temporary directory can be on a different file system. Copy the PDF into a
            # private file next to OUT_PATH, fsync it, rename it, and fsync the directory.
            # Only then can the caller remove the originals.
            with open(base + ".pdf", "rb") as fh:
                commit_file(out_path, iter(lambda: fh.read(1 << 20), b""), sync=True)
            return out_path
        with open(base + ".pdf", "rb") as fh:
            return fh.read()


# --- Pipeline -------------------------------------------------------------

def _opt(profile_value, default):
    return default if profile_value is None else profile_value


class Pipeline:
    """Use sheet_pages() and prepare() for each sheet, then finish() for the document."""

    def __init__(self, cfg):
        self.cfg = cfg                    # the [pipeline] table

    def store_for(self, directory, budget=None):
        return PageStore(directory, int(float(self.cfg.get("max_memory_mb", 64)) * (1 << 20)), budget)

    def sheet_pages(self, sides, profile, store=None):
        """Make the Page objects of one complete sheet (both sides, reading order)."""
        by_name = {s.side: s for s in sides}
        return [Page.from_side(by_name[n], store) for n in side_order(profile) if n in by_name]

    def prepare(self, page, profile, store=None):
        """Work on one page during the scan: blank test and measurement.

        Duplex profiles also turn and crop a page with print at once. Simplex
        profiles do that later (finish), for the side with more print only.
        If a step fails, the page stays as scanned (with a warning in page.errors).
        """
        try:
            page.blank = is_blank(page, self.cfg) if profile.blank_skip or not profile.duplex else False
            if not page.blank:
                page_analysis(page, self.cfg)
        except Exception as e:                       # keep the page: a lost page is worse than a blank one
            self._failed(page, "analysis", e)
            page.blank = False
            page.analysis = None                     # not known: keep() never drops this page
        page.info["original"] = page_record(page)    # the input of the blank test and side selection
        if not page.blank and profile.duplex:
            page = self._orient(page, profile, store)
        return page

    @staticmethod
    def _failed(page, step, e):
        msg = "sheet %d %s: %s failed (%s): page kept as scanned" % (page.sheet + 1, page.side, step, e)
        log.warning(msg)
        page.errors.append(msg)

    def _crop_wanted(self, page, profile):
        crop = profile.crop
        if crop in (True, False):
            return crop
        if profile.receipt == "yes":
            return True
        a = page_analysis(page, self.cfg)
        return bool(a and a.paper_width_mm > 20 and
                    a.paper_length_mm / a.paper_width_mm > float(self.cfg.get("receipt_ratio", 2.0)))

    def _orient(self, page, profile, store=None):
        """Deskew, crop and rotation of one page (one time)."""
        if page.oriented:
            return page
        try:
            desk = bool(_opt(profile.deskew, self.cfg.get("deskew", False)))
            crop = self._crop_wanted(page, profile)
            if desk or crop:
                page = deskew_and_crop(page, self.cfg, deskew=desk, crop=crop, store=store)
        except Exception as e:
            self._failed(page, "deskew/crop", e)
        rot = _opt(profile.rotate, self.cfg.get("rotate", "auto"))
        if str(rot).lower() == "auto":
            if tesseract_path():
                try:
                    page.rotate = detect_rotation(page, float(self.cfg.get("osd_min_confidence", 2.0)),
                                                  float(self.cfg.get("tesseract_timeout", 60)))
                except Exception as e:
                    self._failed(page, "rotation", e)
        else:
            page.rotate = int(rot) % 360
        page.oriented = True
        return page

    @staticmethod
    def _ink(page):
        return page.analysis.ink_ratio if page.analysis is not None else -1.0

    @staticmethod
    def unknown(page):
        """True if a step failed before the page had a measurement (the page content is not known)."""
        return page.analysis is None and bool(page.errors)

    def keep(self, pages, profile, store=None):
        """Choose the pages for the PDF.

        Simplex: for each sheet, the side with the higher ink ratio (paper_face
        order if equal). With profile.side "up" or "down": that side, also when it
        looks blank (with a warning). Duplex: all sides that are not blank. If all chosen
        pages are blank, each sheet keeps its side with the most ink.
        A page without a measurement because a step failed (unknown()) is always
        kept: an unknown page never loses to a blank side.
        """
        if not pages:
            return []
        sheets = {}
        for p in pages:
            sheets.setdefault(p.sheet, []).append(p)
        best = {n: max(ps, key=self._ink) for n, ps in sheets.items()}      # max() keeps the first if equal
        fixed = getattr(profile, "side", "auto") in ("up", "down")
        if not profile.duplex and fixed:
            # The side that faced up (or down) in the scanner: the front image is the side
            # that faces up [HW]. No ink check: for example a receipt with print on its back.
            want = "front" if profile.side == "up" else "back"
            kept = []
            for n in sorted(sheets):
                p = next((q for q in sheets[n] if q.side == want), best[n])
                if p.blank:
                    msg = ("sheet %d: the %s side looks blank: check the load direction"
                           % (n + 1, "face-up" if profile.side == "up" else "face-down"))
                    log.warning(msg)
                    p.errors.append(msg)
                kept.append(p)
        elif not profile.duplex:
            kept = []
            for n in sorted(sheets):
                ps = sheets[n]
                if any(self.unknown(p) for p in ps):
                    log.warning("sheet %d: a side has no measurement: it stays in the PDF with each side "
                                "that is not blank", n + 1)
                    kept.extend(p for p in ps if self.unknown(p) or not p.blank)
                else:
                    kept.append(best[n])
            if profile.blank_skip and any(not p.blank for p in kept):
                kept = [p for p in kept if not p.blank]
        elif profile.blank_skip:
            kept = [p for p in pages if not p.blank]
        else:
            kept = list(pages)
        if not kept:
            kept = [best[n] for n in sorted(sheets)]
            log.warning("all pages look blank; keep the side with the most print of each sheet")
        if len(kept) != len(pages):
            log.info("pages kept: %d of %d", len(kept), len(pages))
        keep_ids = {id(p) for p in kept}
        for p in pages:
            if id(p) not in keep_ids:
                p.jpeg_blob.discard()
        return [self._orient(p, profile, store) for p in kept]

    def pages(self, result, profile, store=None):
        """Return the list of Page objects that go into the PDF (all sheets of RESULT)."""
        pages = [self.prepare(Page.from_side(s, store), profile, store) for s in select_sides(result, profile)]
        return self.keep(pages, profile, store)

    def is_receipt(self, pages, profile):
        if profile.receipt != "auto":
            return profile.receipt == "yes"
        ratio = float(self.cfg.get("receipt_ratio", 2.0))
        for p in pages:
            a = page_analysis(p, self.cfg)
            if a is not None and a.paper_width_mm > 20:
                w, h = a.paper_width_mm, a.paper_length_mm      # paper size, not the scan window
            else:
                w, h = p.size_mm
            if p.rotate in (90, 270):
                w, h = h, w
            if w > 0 and h / w > ratio:
                return True
        return False

    def finish(self, pages, profile, warnings=(), now=None, out_dir=None, store=None, pdf_name=None):
        """Make the Document from prepared pages. Return None if no page is left.

        With OUT_DIR, the PDF goes into a file in OUT_DIR (name PDF_NAME, else the
        document file name). Else it stays in memory. STORE gets the pages that
        deskew and crop make (simplex profiles do that here).
        """
        now = time.time() if now is None else now
        warnings = list(warnings)
        for p in pages:                              # all pages, also the pages that keep() removes
            warnings.extend(w for w in p.errors if w not in warnings)
        pages = self.keep(pages, profile, store)
        if not pages:
            return None
        for p in pages:                              # and the failed steps of deskew, crop and rotation
            warnings.extend(w for w in p.errors if w not in warnings)
        t = time.localtime(now)
        title = time.strftime((profile.title or self.cfg.get("title_format", "Scan %Y-%m-%d %H:%M:%S"))
                              .replace("{profile}", profile.name), t)
        filename = safe_filename(time.strftime(self.cfg.get("filename_format", "scan-%Y%m%d-%H%M%S.pdf")
                                               .replace("{profile}", profile.name), t))
        receipt = self.is_receipt(pages, profile)
        out_path = os.path.join(out_dir, pdf_name or filename) if out_dir else None
        pdf = None
        if _opt(profile.ocr, self.cfg.get("ocr", False)):
            pdf = ocr_pdf(pages, self.cfg.get("ocr_lang", "eng"), title,
                          float(self.cfg.get("tesseract_timeout", 60)) * max(1, len(pages)), out_path)
            if pdf is None:
                log.warning("OCR not available: PDF without text layer")
        if pdf is None:
            if out_path:
                tmp = out_path + ".part"
                fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | getattr(os, "O_NOFOLLOW", 0), FILE_MODE)
                with os.fdopen(fd, "wb") as fh:
                    write_pdf(pages, title=title, created=now, fh=fh)
                    fh.flush()
                    os.fsync(fh.fileno())
                os.replace(tmp, out_path)
                sync_dir(out_dir)                    # the new name is on the disk too
                pdf = out_path
            else:
                pdf = write_pdf(pages, title=title, created=now)
        if out_path:
            doc = Document(filename, title, profile.name, len(pages), receipt, now, warnings, path=out_path)
        else:
            doc = Document(filename, title, profile.name, len(pages), receipt, now, warnings, pdf=pdf)
        doc.kept = {(p.sheet, p.side) for p in pages}
        doc.page_records = [dict(page_record(p), sheet=p.sheet + 1, side=p.side, rotate=p.rotate,
                                 crop_box=p.info.get("crop_box"), skew=p.info.get("skew"),
                                 deskew_applied=bool(p.info.get("deskew_applied"))) for p in pages]
        log.info("document %s: %d pages, %d bytes%s", filename, doc.pages, doc.size, ", receipt" if receipt else "")
        return doc

    def process(self, result, profile, now=None, out_dir=None, store=None):
        """All steps for a finished ScanResult. Return a Document, or None if no page is left."""
        pages = [self.prepare(Page.from_side(s, store), profile, store) for s in select_sides(result, profile)]
        return self.finish(pages, profile, result.warnings, now, out_dir, store)
