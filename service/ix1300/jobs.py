"""Job directories on disk: the scan is safe before the service works on it.

work_dir/jobs/<id>/     a job in work: manifest.json and the original sheet JPEGs
work_dir/unsent/<id>/   the PDF and manifest.json; one or more outputs failed
work_dir/failed/<id>/   the original sheets and manifest.json; no PDF, or pages failed
work_dir/raw/<id>/      a copy of the original sheets and raw.json (service.keep_raw)

The service writes the sheets (fsync) before it works on them. It removes a
job directory only after all outputs have the document. The job ID has the
time and a random part, so two jobs never have the same directory.

The manifest is not the only record of the sheets. A crash or a full disk can
stop the service after it wrote a side file and before it wrote the manifest.
reconcile() compares the directory with the manifest: it adds the complete
sheets that it finds, and it records each side without its other side as an
"orphan". retain_reason() then keeps the originals after the delivery.
"""
import contextlib
import json
import logging
import os
import re
import secrets
import shutil
import subprocess
import time

from . import jpeg
from .pipeline import DIR_MODE, Image, commit_file, djpeg_path, jpegtran_path, sync_dir, tesseract_path, write_file
from .protocol import Side

log = logging.getLogger("ix1300.jobs")

JOBS, UNSENT, FAILED, RAW = "jobs", "unsent", "failed", "raw"
MANIFEST = "manifest.json"
PDF = "document.pdf"
SIDE_FILE = re.compile(r"^sheet(\d{3,})-(front|back)\.jpg$")

try:
    import fcntl
except ImportError:                      # pragma: no cover - not on Linux or macOS
    fcntl = None


def new_job_id():
    return "%s-%s" % (time.strftime("%Y%m%d-%H%M%S"), secrets.token_hex(3))


def private_dir(path):
    """Make PATH (and its parents) with mode 0700 for the new directories."""
    os.makedirs(path, mode=DIR_MODE, exist_ok=True)
    return path


def free_mb(path):
    """Free space (MB) of the file system of PATH (or of its first parent that exists)."""
    while path and not os.path.exists(path):
        parent = os.path.dirname(path)
        if parent == path:
            break
        path = parent
    return shutil.disk_usage(path or ".").free / float(1 << 20)


def side_name(sheet, side):
    return "sheet%03d-%s.jpg" % (sheet + 1, side)


class JobDir:
    """One job directory and its manifest (a dict)."""

    def __init__(self, path, manifest):
        self.path = path
        self.m = manifest
        self._fd = None                   # lock file of claim()

    @property
    def id(self):
        return self.m["id"]

    @property
    def work_dir(self):
        return os.path.dirname(os.path.dirname(self.path))

    @classmethod
    def create(cls, work_dir, profile):
        """Make a new job directory in work_dir/jobs. Raise OSError if this is not possible."""
        jid = new_job_id()
        base = private_dir(os.path.join(work_dir, JOBS))
        path = os.path.join(base, jid)
        os.mkdir(path, DIR_MODE)
        job = cls(path, dict(id=jid, profile=profile, created=time.time(), state="scanning", sheets=[],
                             warnings=[], deliver=None, outputs={}, document=None, attempts=0, error=None))
        job.save()
        sync_dir(base)
        return job

    @staticmethod
    def _read(path):
        try:
            with open(os.path.join(path, MANIFEST), encoding="utf-8") as fh:
                m = json.load(fh)
        except (OSError, ValueError):
            return None
        return m if isinstance(m, dict) and m.get("id") else None

    @classmethod
    def open(cls, path):
        """Read a job directory. Return None if it has no usable manifest."""
        m = cls._read(path)
        return None if m is None else cls(path, m)

    def reload(self):
        """Read the manifest again (after claim()). False if the job is gone."""
        m = self._read(self.path)
        if m is None:
            return False
        self.m = m
        return True

    def save(self):
        commit_file(os.path.join(self.path, MANIFEST), [json.dumps(self.m, indent=1).encode()], sync=True)

    def add_sheet(self, sides):
        """Write the sides of one sheet (fsync), then the manifest. Set side.path. Return the file names.

        If the manifest write fails, the side files stay on disk; reconcile() finds them.
        """
        names = {}
        for s in sides:
            fn = side_name(s.sheet, s.side)
            write_file(os.path.join(self.path, fn), [s.jpeg], sync=True)
            s.path = os.path.join(self.path, fn)
            names[s.side] = dict(file=fn, dpi=s.dpi, width=s.width, lines=s.lines, page_id=s.page_id)
        self.m["sheets"].append(names)
        try:
            self.save()
        except BaseException:
            self.m["sheets"].pop()        # the manifest on disk does not have the sheet
            raise
        return names

    def iter_sides(self, indices=None):
        """Read the sides of the sheets, one sheet at a time. Yield lists of Side (with .path).

        INDICES: only these sheets (positions in the manifest).
        """
        for n, sheet in enumerate(self.m["sheets"]):
            if indices is not None and n not in indices:
                continue
            sides = []
            for name in ("front", "back"):
                d = sheet.get(name)
                if not d:
                    continue
                path = os.path.join(self.path, d["file"])
                with open(path, "rb") as fh:
                    data = fh.read()
                s = Side(sheet=n, side=name, page_id=d.get("page_id", 0), jpeg=data, width=d.get("width", 0),
                         lines=d.get("lines", 0), dpi=d.get("dpi", 300))
                s.path = path
                sides.append(s)
            yield sides

    def pdf_path(self):
        return os.path.join(self.path, PDF)

    def pending(self):
        """Names of the outputs that do not have the document yet."""
        return [n for n, st in self.m["outputs"].items() if not st.get("ok")]

    # --- Directory contents and the manifest ---
    def listed(self):
        """File names of the sides in the manifest (sheets and orphans)."""
        out = {d["file"] for sheet in self.m.get("sheets") or [] for d in sheet.values()}
        out.update(self.m.get("orphans") or [])
        return out

    def unlisted(self):
        """Side files in the directory that the manifest does not know."""
        try:
            names = os.listdir(self.path)
        except OSError:
            return []
        known = self.listed()
        return sorted(n for n in names if SIDE_FILE.match(n) and n not in known)

    def reconcile(self):
        """Add the side files that the manifest does not know. Return the positions of the new sheets.

        A sheet with both sides (two usable JPEG headers) goes into the sheet list.
        A single side goes into "orphans". The manifest is saved if it changed.
        """
        extra = self.unlisted()
        if not extra:
            return []
        found = {}
        for fn in extra:
            num, side = SIDE_FILE.match(fn).groups()
            found.setdefault(int(num), {})[side] = fn
        known = {int(SIDE_FILE.match(d["file"]).group(1)) for sheet in self.m["sheets"]
                 for d in sheet.values() if SIDE_FILE.match(d["file"])}
        added, orphans = [], list(self.m.get("orphans") or [])
        for num in sorted(found):
            files = found[num]
            entry = {}
            for side, fn in sorted(files.items()):
                try:
                    with open(os.path.join(self.path, fn), "rb") as fh:
                        data = fh.read()
                    w, h, _nc = jpeg.check(data)
                except (OSError, ValueError) as e:
                    log.warning("job %s: %s is not usable (%s): kept as an orphan", self.id, fn, e)
                    continue
                dpi = (jpeg.density(data) or (300, 300))[0]
                entry[side] = dict(file=fn, dpi=dpi, width=w, lines=h, page_id=0 if side == "front" else 1)
            if num in known or set(entry) != {"front", "back"}:
                orphans.extend(sorted(files.values()))
                continue
            added.append((num, entry))
        sheets = self.m["sheets"]
        for num, entry in added:
            sheets.append(entry)
        if added:
            sheets.sort(key=lambda s: min(int(SIDE_FILE.match(d["file"]).group(1)) for d in s.values()))
        new = [i for i, s in enumerate(sheets) if any(e is s for _n, e in added)]
        self.m["orphans"] = sorted(set(orphans))
        if added:
            self.m["recovered"] = sorted(set(self.m.get("recovered") or []) | {n for n, _e in added})
            self.m.setdefault("warnings", []).append(
                "sheets %s: found on disk, not in the manifest (crash or full disk): originals kept"
                % ", ".join(str(n) for n, _e in added))
        if self.m["orphans"]:
            log.warning("job %s: sides without their other side: %s", self.id, ", ".join(self.m["orphans"]))
        self.save()
        return new

    def retain_reason(self):
        """Text if the originals must stay after the delivery (else None)."""
        why = []
        if self.m.get("missing"):
            why.append("pages missing from the PDF: %s" % "; ".join(self.m["missing"]))
        if self.m.get("orphans"):
            why.append("sides without a complete sheet: %s" % ", ".join(self.m["orphans"]))
        if self.m.get("recovered"):
            why.append("sheets found on disk, not in the manifest: %s" %
                       ", ".join(str(n) for n in self.m["recovered"]))
        extra = self.unlisted()
        if extra:
            why.append("side files not in the manifest: %s" % ", ".join(extra))
        return "; ".join(why) or None

    def remove_sheets(self):
        for sheet in self.m["sheets"]:
            for d in sheet.values():
                try:
                    os.remove(os.path.join(self.path, d["file"]))
                except OSError:
                    pass

    def move(self, kind):
        """Move the directory to work_dir/KIND/<id> (same file system: a rename)."""
        base = private_dir(os.path.join(self.work_dir, kind))
        if os.path.dirname(self.path) == base:
            return self.path
        dest = os.path.join(base, self.id)
        if os.path.exists(dest):
            dest = "%s-%s" % (dest, secrets.token_hex(2))
        os.rename(self.path, dest)
        sync_dir(base)
        self.path = dest
        return dest

    def remove(self):
        shutil.rmtree(self.path, ignore_errors=True)

    # --- One owner for each job (service or "ix1300 retry") ---
    def claim(self):
        """Take the job lock and keep it until unclaim(). False if another owner has it."""
        if fcntl is None or self._fd is not None:
            return True
        try:
            fd = os.open(os.path.join(self.path, ".lock"), os.O_RDWR | os.O_CREAT | getattr(os, "O_NOFOLLOW", 0),
                         0o600)
        except OSError:
            return False                      # the directory moved or went away
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            os.close(fd)
            return False
        self._fd = fd
        return True

    def unclaim(self):
        if self._fd is not None:
            os.close(self._fd)
            self._fd = None

    @contextlib.contextmanager
    def lock(self):
        """claim() for a "with" block. Yields False if another owner has the job."""
        had = self._fd is not None
        ok = self.claim()
        try:
            yield ok
        finally:
            if ok and not had:
                self.unclaim()


def list_jobs(work_dir, kind):
    """JobDir objects in work_dir/KIND (oldest first). Directories without a manifest are skipped."""
    base = os.path.join(work_dir, kind)
    try:
        names = sorted(os.listdir(base))
    except OSError:
        return []
    out = []
    for n in names:
        path = os.path.join(base, n)
        if os.path.isdir(path):
            j = JobDir.open(path)
            if j is None:
                log.warning("%s: no usable manifest.json: ignored", path)
            else:
                out.append(j)
    return out


def tool_versions():
    """Decoders and OSD used for the measurements (raw.json)."""
    pil = None
    if Image is not None:
        import PIL
        pil = getattr(PIL, "__version__", "yes")
    out = dict(pillow=pil, djpeg=djpeg_path(), jpegtran=jpegtran_path(), tesseract=tesseract_path())
    if out["tesseract"]:
        try:
            r = subprocess.run([out["tesseract"], "--version"], capture_output=True, text=True, timeout=10)
            out["tesseract_version"] = ((r.stdout or r.stderr).strip().splitlines() or [""])[0]
        except (OSError, subprocess.TimeoutExpired):
            pass
    return out


def keep_raw(job, pages, doc, profile, pipeline_cfg, max_jobs):
    """Copy the original sheets of JOB and the measured values of PAGES into work_dir/raw/<id>.

    PAGES: all pages that went into the page selection. Each page record has
    "original" (the scanner JPEG and its analysis: the input of the blank test
    and of the side selection) and "delivered" (the page in the PDF after crop,
    deskew and rotation; null if the page is not in the PDF). DOC: the Document
    or None. Only the newest MAX_JOBS directories stay.
    """
    from .pipeline import page_record
    base = private_dir(os.path.join(job.work_dir, RAW))
    dest = os.path.join(base, job.id)
    os.mkdir(dest, DIR_MODE)
    for sheet in job.m["sheets"]:
        for d in sheet.values():
            shutil.copyfile(os.path.join(job.path, d["file"]), os.path.join(dest, d["file"]))
            os.chmod(os.path.join(dest, d["file"]), 0o600)
    final = {(r["sheet"], r["side"]): r for r in (doc.page_records if doc is not None else [])}
    info = []
    for p in pages:
        orig = p.info.get("original") or page_record(p)
        delivered = final.get((p.sheet + 1, p.side))
        info.append(dict(sheet=p.sheet + 1, side=p.side, blank=p.blank, kept=delivered is not None,
                         errors=p.errors, ink_ratio=orig.get("ink_ratio"), original=orig, delivered=delivered))
    data = dict(id=job.id, profile=profile.name, profile_settings=dict(
        dpi=profile.dpi, duplex=profile.duplex, paper_face=profile.paper_face, crop=profile.crop,
        deskew=profile.deskew, rotate=profile.rotate, receipt=profile.receipt, blank_skip=profile.blank_skip),
        pipeline=dict(pipeline_cfg), tools=tool_versions(), pages=info)
    commit_file(os.path.join(dest, "raw.json"), [json.dumps(data, indent=1, default=str).encode()])
    old = sorted(n for n in os.listdir(base) if os.path.isdir(os.path.join(base, n)))
    for n in old[:max(0, len(old) - max_jobs)]:
        shutil.rmtree(os.path.join(base, n), ignore_errors=True)
    return dest
