"""Guided hardware self-test: "ix1300 selftest".

One session at the scanner (about 15 minutes) examines the functions that no
hardware test confirmed yet, and keeps the data for an analysis later:

  a  sensors       discovery, registered host ID (show only), reserve, C2
  b  power_off     auto power-off time: read, change, read back, restore, verify
  c  registration  Wi-Fi registration data: mode-2 reserve, DC op 4 and op 1 (no change)
  d  empty_press   a short press with no paper: no job
  e  continue      2 sheets, then 2 more sheets with "continue scanning": one document
  f  front_slot    one sheet in the front slot: a front slot job, two sides
  g  long_receipt  a receipt longer than 90 cm, long press: long page mode (optional)
  h  double_feed   two taped sheets: detection on, then off (optional)
  i  stop          the service stop during the first READ (optional, 3 sheets)

The self-test uses the code of the service: daemon.Service (reserve, keepalive,
trigger packets, run_job, continue scanning, the PDF), protocol.Scanner, settings
and register. It uses its own profiles (300 dpi, all pages kept, no rotation) and
sends the documents only into the bundle, not to the outputs of the configuration.

Each step writes its protocol trace, its log lines, the raw images and the PDF into
work_dir/selftest/<time>/steps/<step>/. At the end, report.json and report.txt
go into the bundle, and the bundle goes into <bundle>.tar.gz.
"""
import copy
import json
import logging
import os
import queue
import signal
import sys
import tarfile
import threading
import time
import traceback

from . import __version__
from . import register as register_mod
from . import settings as settings_mod
from .config import Config
from .daemon import BUSY_HINT, Service, host_address, locate, make_scanner, occupied_text, short_error
from .protocol import TRG_SCAN, IxError, ReserveError
from .trace import Trace, op_name

log = logging.getLogger("ix1300.selftest")

PASS, FAIL, SKIPPED, QUIT, NOT_RUN = "PASS", "FAIL", "SKIPPED", "QUIT", "NOT RUN"

PROFILE = "selftest"
PROFILE_LONG = "selftest-long"
PROFILE_DF_OFF = "selftest-df-off"
_BASE_PROFILE = dict(dpi=300, color="auto", duplex=True, blank_skip=False, rotate=0, deskew=False, ocr=False,
                     crop=False, receipt="no", outputs=[])
PROFILES = {
    PROFILE: dict(_BASE_PROFILE),
    PROFILE_LONG: dict(_BASE_PROFILE, length="long"),
    PROFILE_DF_OFF: dict(_BASE_PROFILE, double_feed=False),
}
STOP_HINT = "Stop the service first: docker stop ix1300"
LONG_MM = 900.0                         # a page longer than this needs the long page mode
REJECT_SENSE = ((5, 0x24), (5, 0x26))   # invalid field in the CDB or in the data


class Skip(Exception):
    """The user typed s (or the step does not apply)."""


class Quit(Exception):
    """The user typed q."""


class Refused(Exception):
    """The self-test cannot start: another host has the scanner."""


class SetupError(Exception):
    """The self-test cannot start (no scanner, no host ID, ...)."""


# --- User interface ---------------------------------------------------------

class TerminalUI:
    """Text in the terminal. One reader thread puts the input lines into a queue,
    so that a step can wait for a button event and for a typed line together."""

    def __init__(self, inp=None, out=None):
        self.inp = inp or sys.stdin
        self.out = out or sys.stdout
        self.lines = queue.Queue()
        self.reader = None

    def _start(self):
        if self.reader is None:
            self.reader = threading.Thread(target=self._read, name="ix1300-selftest-input", daemon=True)
            self.reader.start()

    def _read(self):
        while True:
            try:
                line = self.inp.readline()
            except (OSError, ValueError):
                line = ""
            if not line:
                self.lines.put(None)             # end of input: the same as q
                return
            self.lines.put(line.strip().lower())

    def show(self, text=""):
        print(text, file=self.out, flush=True)

    def ask(self, key, prompt, timeout=600.0):
        """Show PROMPT and return the next line (lower case). End of input or TIMEOUT: "q"."""
        self._start()
        print(prompt, end="", file=self.out, flush=True)
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            try:
                line = self.lines.get(timeout=0.2)     # short waits: Ctrl-C comes through
            except queue.Empty:
                continue
            return "q" if line is None else line
        self.show("")
        self.show("No answer in %d s." % timeout)
        return "q"

    def begin_wait(self, key, prompt):
        self._start()
        self.show(prompt)

    def poll(self):
        """A typed line, or None."""
        try:
            line = self.lines.get_nowait()
        except queue.Empty:
            return None
        return "q" if line is None else line


class CaptureOutput:
    """The output of the self-test: it keeps the data of each document and writes the PDF into DIR."""

    def __init__(self):
        self.docs = []
        self.dir = None

    def deliver(self, doc):
        rec = dict(filename=doc.filename, profile=doc.profile, pages=doc.pages, receipt=doc.receipt,
                   paper_path=getattr(doc, "paper_path", "top"), parts=getattr(doc, "parts", None),
                   warnings=list(doc.warnings), size=doc.size, page_records=list(doc.page_records))
        if self.dir:
            name = "document-%02d.pdf" % (len(self.docs) + 1)
            doc.save(os.path.join(self.dir, name))
            rec["pdf"] = name
        self.docs.append(rec)
        return True

    def take(self):
        docs, self.docs = self.docs, []
        return docs


class LogCapture(logging.Handler):
    """Keep the log lines (WARNING and higher) of one step for the report."""

    def __init__(self):
        super().__init__(logging.WARNING)
        self.lines = []

    def emit(self, record):
        try:
            self.lines.append("%s %s: %s" % (record.levelname, record.name, record.getMessage()))
        except Exception:
            pass


# --- Trace helpers ------------------------------------------------------------

def port1_ops(records):
    """The Port1 commands in RECORDS, as short names (READ, PSIZE, SENSE, D5, ...)."""
    return [op_name(r) for r in records if r.get("port") == "port1" and r.get("dir") == "out"]


def sense_events(records):
    """The sense data that is not a normal end: not EOM, not "temporary no data", and not
    "no paper" after E0 (the normal end of a job).

    Return a list of dict(op, key, asc, ascq): OP is the command before the REQUEST SENSE."""
    out, last = [], None
    for r in records:
        if r.get("port") != "port1":
            continue
        if r.get("dir") == "out":
            name = op_name(r)
            if name != "SENSE":
                last = name
        elif r.get("dir") == "in" and r.get("sense"):
            s = r["sense"]
            code = (s["key"], s["asc"], s["ascq"])
            if s["key"] == 0 or code == (3, 0x80, 0x13) or (code == (3, 0x80, 0x03) and last == "E0"):
                continue
            out.append(dict(op=last, key=s["key"], asc=s["asc"], ascq=s["ascq"]))
    return out


def out_data(records, op):
    """The data-out bytes of each Port1 command OP ("E9", "D4", ...)."""
    return [bytes.fromhex(r.get("data", "")) for r in records
            if r.get("port") == "port1" and r.get("dir") == "out" and r.get("op") == op]


def fmt_sense(s):
    return "%s %d/%02X/%02X" % (s["op"], s["key"], s["asc"], s["ascq"])


def page_mm(rec):
    dpi = rec.get("dpi") or 300
    return round((rec.get("height") or 0) / float(dpi) * 25.4, 1)


def selftest_profiles(overrides=None):
    """The self-test profiles. OVERRIDES (the tests): values for all profiles (not "length" of the long one)."""
    out = copy.deepcopy(PROFILES)
    for name, prof in out.items():
        for k, v in (overrides or {}).items():
            if not (name == PROFILE_LONG and k == "length"):
                prof[k] = v
    return out


def selftest_config(cfg, work_dir, overrides=None):
    """A copy of CFG for the self-test: its profiles, no power-off change, no trace_dir, WORK_DIR."""
    data = copy.deepcopy(cfg.data)
    sv = data["service"]
    sv.update(work_dir=work_dir, keep_raw=True, keep_raw_max_jobs=100000, continue_window=0.0,
              check_paper=True, deliver_partial=True, trace_dir="",
              short_press_profile=PROFILE, long_press_profile=PROFILE_LONG, front_slot_profile="")
    data["scanner"]["power_off_minutes"] = None
    data["scanner"]["auto_register"] = False
    data.setdefault("profiles", {}).update(selftest_profiles(overrides))
    return Config(data)


# --- The self-test ------------------------------------------------------------

class Step:
    def __init__(self, sid, title, fn, needs):
        self.id, self.title, self.fn, self.needs = sid, title, fn, needs


class Selftest:
    """Run the steps. UI: TerminalUI (or a test script). NET, CLOCK, SCANNER_OPTIONS: as for Service."""

    def __init__(self, cfg, ui, net=None, clock=None, scanner_options=None, bundle_root=None, only=None,
                 button_timeout=120.0, tick=0.2, reserve_wait=30.0, install_signals=False, profile_overrides=None,
                 quiet_console=True):
        self.user_cfg = cfg
        self.ui = ui
        self.net = net
        self.clock = clock
        self.scanner_options = dict(scanner_options or {})
        self.button_timeout = float(button_timeout)
        self.tick = float(tick)
        self.reserve_wait = float(reserve_wait)
        self.install_signals = install_signals
        self.quiet_console = quiet_console      # the terminal shows the instructions; the log goes to the bundle
        root = bundle_root or os.path.join(cfg.service.get("work_dir") or "/data", "selftest")
        self.started = time.time()
        self.bundle = os.path.join(root, time.strftime("%Y%m%d-%H%M%S", time.localtime(self.started)))
        self.archive = self.bundle + ".tar.gz"
        self.profiles = selftest_profiles(profile_overrides)
        self.cfg = selftest_config(cfg, os.path.join(self.bundle, "work"), profile_overrides)
        sv = cfg.service
        self.trace = Trace(None, payload_bytes=int(sv.get("trace_payload_bytes", 256)), keep=True)
        self.capture = CaptureOutput()
        self.svc = None
        self.dev = None
        self.host = None
        self.preflight = {}
        self.results = []
        self.quit = False
        self.refused = None
        self.in_job = False
        self.interrupted = False
        self.ka_stop = threading.Event()
        self.ka_thread = None
        self.log_handler = None
        all_steps = [
            Step("sensors", "Sensors and status", self.step_sensors, "no paper"),
            Step("power_off", "Auto power-off time", self.step_power_off, "no paper"),
            Step("registration", "Wi-Fi registration data (show only)", self.step_registration, "no paper"),
            Step("empty_press", "Button press with no paper", self.step_empty_press, "no paper"),
            Step("continue", "Top chute and continue scanning", self.step_continue, "4 sheets"),
            Step("front_slot", "Front slot", self.step_front_slot, "1 sheet"),
            Step("long_receipt", "Long receipt (long page mode)", self.step_long_receipt,
                 "a receipt longer than 90 cm (optional)"),
            Step("double_feed", "Double-feed detection on and off", self.step_double_feed,
                 "2 sheets taped with an overlap (optional)"),
            Step("stop", "Service stop during a scan", self.step_stop, "3 sheets (optional)"),
        ]
        known = {s.id for s in all_steps}
        if only:
            bad = [s for s in only if s not in known]
            if bad:
                raise ValueError("unknown steps %s (known: %s)" % (", ".join(bad), ", ".join(sorted(known))))
        self.steps = [s for s in all_steps if not only or s.id in only]

    # --- Questions ---
    def _answer(self, line):
        line = (line or "").strip().lower()
        if line in ("q", "quit"):
            raise Quit()
        if line in ("s", "skip"):
            raise Skip("skipped by the user")
        return line

    def enter(self, key, text):
        self.ui.show(text)
        self._answer(self.ui.ask(key, "Press Enter to continue (s = skip this step, q = quit): "))

    def yes_no(self, key, text):
        while True:
            a = self._answer(self.ui.ask(key, text + " (y/n, s = skip, q = quit): "))
            if a in ("y", "yes"):
                return True
            if a in ("n", "no"):
                return False
            self.ui.show("Type y or n.")

    def number(self, key, text):
        while True:
            a = self._answer(self.ui.ask(key, text + " (a number, Enter = not known): "))
            if a == "":
                return None
            if a.isdigit():
                return int(a)
            self.ui.show("Type a number.")

    def button(self, key, text, timeout=None, held=False):
        """Wait for a scan button event. Return the Trigger, or None after TIMEOUT.

        HELD: continue scanning: the document that waits goes to delivery when its time ends.
        """
        svc = self.svc
        while svc._recv_trigger(0.001) is not None:     # old packets (for example copies of a press)
            pass
        timeout = self.button_timeout if timeout is None else timeout
        self.ui.begin_wait(key, text + "\n(Waiting for the Scan button. Type s + Enter to skip, q + Enter to quit.)")
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            line = self.ui.poll()
            if line is not None:
                self._answer(line)
            if held:
                svc._continue_tick()
            self._check_reserve()
            trg = svc._recv_trigger(self.tick)
            if trg is None or not svc.dedup.is_new(trg):
                continue
            if trg.code == TRG_SCAN:
                return trg
            self.trace.note("trigger code 0x%02X during the wait" % trg.code)
            log.warning("trigger code 0x%02X during the wait (not the scan button)", trg.code)
        return None

    # --- Scanner and service ---
    def _open(self):
        os.makedirs(self.bundle, mode=0o700, exist_ok=True)
        self.capture.dir = self.bundle
        svc = self.svc = Service(self.cfg, net=self.net, outputs={"selftest": self.capture}, clock=self.clock,
                                 scanner_options=self.scanner_options)
        svc.trace = self.trace
        self.trace.switch(os.path.join(self.bundle, "preflight", "trace.jsonl"))
        sc = self.cfg.scanner
        if self.cfg.host_id is None:
            raise SetupError("scanner.host_id is not set")
        host_ip, host_mac = host_address(self.cfg)
        self.host = dict(ip=host_ip, mac=host_mac.hex(":"))
        svc.trigger_sock = svc.net.udp(bind=(sc.get("host_ip") or "", int(sc["trigger_port"])))
        svc.trigger_port = svc.trigger_sock.getsockname()[1]
        self.dev = locate(self.cfg, host_ip, host_mac, svc.net, trace=self.trace)
        if self.dev is None:
            raise SetupError("no scanner found. Open the scanner cover to turn it on, and set scanner.ip")
        d = self.dev
        self.preflight["discovery"] = dict(ip=d.ip, name=d.name, product=d.product, serial=d.serial, mac=d.mac,
                                           port1=d.port1, port2=d.port2, owner_ip=d.owner_ip, owner_id=d.owner_id,
                                           devstatus=d.devstatus, tls=d.tls, status1=d.status1, status2=d.status2)
        scanner = make_scanner(self.cfg, host_ip, host_mac, d, svc.net, svc.trigger_port, trace=self.trace)
        for name, value in self.scanner_options.items():
            setattr(scanner, name, value)
        svc.scanner = scanner
        try:
            scanner.reserve()
        except ReserveError as e:
            self.preflight["reserve"] = e.result
            if e.result == -4:
                why = occupied_text(scanner, svc.net)
                if why == BUSY_HINT:
                    raise Refused("The scanner is busy (%s)." % why)
                raise Refused("The scanner is in use by another host: %s.\n%s, then start the self-test again."
                              % (why, STOP_HINT))
            raise SetupError("reserve failed: %s" % e)
        self.preflight["reserve"] = 0
        with svc.lock:
            svc.reserved = True
            svc.epoch += 1
            svc.ka_failures = 0
            svc.lost.clear()
        svc.dedup.reset()
        svc.dedup_reset = False
        self.ka_thread = threading.Thread(target=self._keepalive, name="ix1300-selftest-keepalive", daemon=True)
        self.ka_thread.start()

    def _keepalive(self):
        interval = float(self.cfg.scanner["keepalive_interval"])
        while not self.ka_stop.wait(interval):
            try:
                self.svc.keepalive_once()
            except Exception:
                log.exception("keepalive")

    def _check_reserve(self):
        svc = self.svc
        if svc.lost.is_set():
            svc.lost.clear()
            if svc.lost_epoch == svc.epoch:
                log.warning("keepalive failed: reserve again")
                svc._unreserve(reset_dedup=True)

    def ensure_reserved(self):
        """Reserve again (the service code) if the reserve is lost. Raise SetupError after reserve_wait."""
        self._check_reserve()
        svc = self.svc
        end = time.monotonic() + self.reserve_wait
        while not svc.reserved:
            if svc._try_reserve():
                break
            if time.monotonic() >= end:
                raise SetupError("no reserve in %d s (last error: %s)" % (self.reserve_wait, svc.last_error))
            time.sleep(self.tick)

    def run_job(self, profile, press="short"):
        """One job with the service code (Service.run_job), then the PDF. Return (records, documents)."""
        svc = self.svc
        mark = self.trace.mark()
        self.in_job = True
        try:
            svc.run_job(profile, press=press)
        finally:
            self.in_job = False
        svc._drain_triggers()
        docs = self.process_queue()
        if self.interrupted:
            raise KeyboardInterrupt()
        return self.trace.since(mark), docs

    def process_queue(self):
        svc = self.svc
        while True:
            try:
                job = svc.jobs.get_nowait()
            except queue.Empty:
                break
            try:
                svc.process(job)
            except Exception:
                log.exception("document processing failed")
            finally:
                with svc.lock:
                    svc.backlog -= 1
                svc.jobs.task_done()
        return self.capture.take()

    def _trigger_stop(self):
        """The service stop path: a READ that waits stops (Scanner.abort), the job ends with D8, E0, D6."""
        svc = self.svc
        now = svc.clock()
        svc.stop_at = now - float(self.cfg.service.get("stop_scan_wait", 15)) - 1.0
        svc.stop_deadline = now
        svc.stop_event.set()

    def _clear_stop(self):
        svc = self.svc
        svc.stop_event.clear()
        svc.stop_at = None
        svc.stop_deadline = None

    def _close(self):
        svc = self.svc
        self.ka_stop.set()
        if svc is None:
            return
        try:
            if svc.held is not None:
                svc._deliver_held("self-test end")
                self.process_queue()
        except Exception:
            log.exception("delivery of the waiting document")
        sc = svc.scanner
        if sc is not None and sc.job_state in ("active", "uncertain"):
            # Ctrl-C during a job: end it (D8, D6), with a short limit for each reply.
            sc._cmd_limit = sc.stop_cmd_timeout
            for cdb in ([0xD8, 0, 0, 0, 0, 0], [0xD6, 0, 0, 0, 0, 0]):
                try:
                    sc.cmd(cdb)
                except (OSError, IxError) as e:
                    log.warning("end of the job: %02X failed: %s", cdb[0], e)
            sc._cmd_limit = None
            sc.job_state = "ended"
        if svc.reserved and sc is not None:
            svc._release()
        if self.ka_thread is not None:
            self.ka_thread.join(2.0)
        if svc.trigger_sock is not None:
            svc.trigger_sock.close()
        svc.executor.shutdown(wait=True)

    # --- Signals ---
    def _sigint(self, signum, frame):
        if self.in_job and not self.interrupted:
            self.interrupted = True
            self._trigger_stop()
            self.ui.show("\nCtrl-C: the scan stops now (D8, E0, D6). Push Ctrl-C again to end at once.")
            return
        raise KeyboardInterrupt()

    # --- Run ---
    def run(self):
        """Run all steps. Return the exit code: 0 all PASS or SKIPPED, 1 a FAIL or an error,
        2 stopped by the user, 3 refused (the scanner is in use by another host)."""
        os.makedirs(self.bundle, mode=0o700, exist_ok=True)
        self.log_handler = logging.FileHandler(os.path.join(self.bundle, "selftest.log"), encoding="utf-8")
        self.log_handler.setLevel(logging.DEBUG)
        self.log_handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s"))
        logging.getLogger().addHandler(self.log_handler)
        pkg = logging.getLogger("ix1300")
        old_level = pkg.level
        pkg.setLevel(logging.DEBUG)
        root = logging.getLogger()
        quiet = {}
        if self.quiet_console:
            for h in root.handlers:
                if h is not self.log_handler:
                    quiet[h] = h.level
                    h.setLevel(max(logging.ERROR, h.level, root.level))
        old_sig = None
        if self.install_signals:
            old_sig = signal.signal(signal.SIGINT, self._sigint)
        try:
            self._intro()
            try:
                self._open()
            except Refused as e:
                self.refused = str(e)
                self.ui.show("")
                self.ui.show(str(e))
                return 3
            except (SetupError, OSError, IxError) as e:
                self.refused = "setup: %s" % (short_error(e) if not isinstance(e, SetupError) else e)
                self.ui.show("Cannot start: %s" % self.refused)
                return 1
            for n, step in enumerate(self.steps):
                if self.quit:
                    self.results.append(dict(id=step.id, title=step.title, status=NOT_RUN, why="", data={}))
                    continue
                self._run_step(n + 1, step)
        except KeyboardInterrupt:
            self.quit = True
            self.ui.show("\nStopped (Ctrl-C).")
        finally:
            try:
                self._close()
            except Exception:
                log.exception("close")
            if old_sig is not None:
                signal.signal(signal.SIGINT, old_sig)
            done = {r["id"] for r in self.results}
            for step in self.steps:
                if step.id not in done:
                    self.results.append(dict(id=step.id, title=step.title, status=NOT_RUN, why="", data={}))
            try:
                self._write_report()
                self._pack()
            finally:
                logging.getLogger().removeHandler(self.log_handler)
                self.log_handler.close()
                pkg.setLevel(old_level)
                for h, level in quiet.items():
                    h.setLevel(level)
                self.trace.close()
            self._outro()
        if any(r["status"] == FAIL for r in self.results):
            return 1
        if self.quit:
            return 2
        return 0

    def _intro(self):
        ui = self.ui
        ui.show("ix1300 hardware self-test (%d steps, about 15 minutes)" % len(self.steps))
        ui.show("You need: 5 plain sheets; optional: a receipt longer than 90 cm, and 2 sheets taped with an "
                "overlap.")
        ui.show("At each step, do what the text says. Type s + Enter to skip a step, q + Enter to quit.")
        ui.show("The data goes into %s" % self.bundle)
        ui.show("")

    def _outro(self):
        ui = self.ui
        ui.show("")
        ui.show("Results:")
        for r in self.results:
            ui.show("  %-8s %-14s %s" % (r["status"], r["id"], r.get("why", "")))
        if os.path.exists(self.archive):
            ui.show("")
            ui.show("Bundle: %s" % self.archive)
            ui.show("Send this file for the analysis.")

    def _run_step(self, n, step):
        ui, svc = self.ui, self.svc
        step_dir = os.path.join(self.bundle, "steps", "%02d-%s" % (n, step.id))
        os.makedirs(step_dir, mode=0o700, exist_ok=True)
        self.trace.switch(os.path.join(step_dir, "trace.jsonl"))
        self.trace.note("step start", step=step.id)
        svc.work_dir = os.path.join(step_dir, "work")
        self.capture.dir = step_dir
        rec = dict(id=step.id, title=step.title, status=None, why="", data={}, dir=os.path.relpath(step_dir,
                                                                                                 self.bundle))
        cap = LogCapture()
        logging.getLogger("ix1300").addHandler(cap)
        t0 = time.monotonic()
        ui.show("")
        ui.show("=== Step %d of %d: %s (%s) ===" % (n, len(self.steps), step.title, step.needs))
        try:
            self.ensure_reserved()
            status, why = step.fn(rec["data"])
        except Skip as e:
            status, why = SKIPPED, str(e) or "skipped"
        except Quit:
            status, why = QUIT, "the user typed q"
            self.quit = True
        except KeyboardInterrupt:
            rec.update(status=QUIT, why="Ctrl-C")
            self._finish_step(rec, cap, t0, step_dir)
            raise
        except (SetupError, OSError, IxError) as e:
            status, why = FAIL, "error: %s" % (e if isinstance(e, SetupError) else short_error(e))
            rec["data"]["traceback"] = traceback.format_exc()
        except Exception as e:
            status, why = FAIL, "internal error: %s" % (str(e) or e.__class__.__name__)
            rec["data"]["traceback"] = traceback.format_exc()
            log.exception("step %s", step.id)
        rec.update(status=status, why=why)
        self._finish_step(rec, cap, t0, step_dir)
        ui.show("%s: %s" % (status, why))

    def _finish_step(self, rec, cap, t0, step_dir):
        logging.getLogger("ix1300").removeHandler(cap)
        rec["log"] = cap.lines
        rec["seconds"] = round(time.monotonic() - t0, 1)
        self.trace.note("step end", step=rec["id"], status=rec["status"])
        files = []
        for base, _dirs, names in os.walk(step_dir):
            for name in names:
                files.append(os.path.relpath(os.path.join(base, name), self.bundle))
        rec["files"] = sorted(files)
        self.results.append(rec)

    # --- Report ---
    def _write_report(self):
        counts = {}
        for r in self.results:
            counts[r["status"]] = counts.get(r["status"], 0) + 1
        sc, sv = self.user_cfg.scanner, self.user_cfg.service
        report = dict(tool="ix1300 selftest", version=__version__, started=self.started, ended=time.time(),
                      started_text=time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(self.started)),
                      host=self.host, preflight=self.preflight, refused=self.refused, counts=counts,
                      config=dict(scanner_ip=sc.get("ip"), host_id=sc.get("host_id"), trigger_port=sc.get("trigger_port"),
                                  keepalive_interval=sc.get("keepalive_interval"),
                                  continue_window=sv.get("continue_window"), profiles=self.profiles),
                      trace_format="JSON lines, refer to ix1300/trace.py", steps=self.results)
        os.makedirs(self.bundle, mode=0o700, exist_ok=True)
        with open(os.path.join(self.bundle, "report.json"), "w", encoding="utf-8") as fh:
            json.dump(report, fh, indent=1, default=str)
        lines = ["ix1300 self-test %s, version %s" % (report["started_text"], __version__)]
        if self.dev is not None:
            lines.append("scanner %s %s serial %s" % (self.dev.ip, self.dev.name, self.dev.serial))
        if self.refused:
            lines.append("not started: %s" % self.refused)
        lines.append("")
        for r in self.results:
            lines.append("%-8s %-14s %s" % (r["status"], r["id"], r.get("why", "")))
        lines.append("")
        lines.append(", ".join("%s %d" % kv for kv in sorted(counts.items())))
        with open(os.path.join(self.bundle, "report.txt"), "w", encoding="utf-8") as fh:
            fh.write("\n".join(lines) + "\n")

    def _pack(self):
        tmp = self.archive + ".part"
        with tarfile.open(tmp, "w:gz") as tar:
            tar.add(self.bundle, arcname=os.path.basename(self.bundle))
        os.chmod(tmp, 0o600)
        os.replace(tmp, self.archive)

    # --- Steps ---------------------------------------------------------------
    def step_sensors(self, data):
        self.enter("sensors", "Open the scanner cover (the scanner is on). Remove all paper.")
        sc = self.svc.scanner
        data["discovery"] = self.preflight.get("discovery")
        data["reserve"] = self.preflight.get("reserve")
        inq = sc.inquiry()
        data["inquiry"] = inq[8:36].decode("ascii", "replace")
        st = sc.hw_status(0x30)
        data["c2"] = st.raw.hex()
        data["c2_bits"] = dict(hopper_empty=st.hopper_empty, paper_at_feed=st.paper_at_feed,
                               paper_at_exit=st.paper_at_exit, button_short=st.button_short,
                               button_long=st.button_long, paper_path=st.paper_path)
        reg = st.host_id
        data["registered_id"] = reg
        ours = self.cfg.scanner["host_id"]
        self.ui.show("Scanner: %s (%s), %s" % (self.dev.name, self.dev.ip, data["inquiry"]))
        self.ui.show("Registered host ID: %s (the configuration has %s)" % (reg or "none", ours))
        if len(st.raw) < 0x28:
            return FAIL, "C2 reply has only %d bytes (0x30 expected)" % len(st.raw)
        return PASS, "discovery, reserve, INQUIRY and C2 OK; registered ID %s%s" % (
            reg or "none", "" if reg == ours else " (not the configured ID)")

    @staticmethod
    def trial_code(code):
        """A different, valid auto power-off code for the test (the time + 15 min, or - 15 min at the maximum)."""
        t = code & 0x7F
        if code & settings_mod.POWER_OFF_DISABLED:
            return t if 1 <= t <= 0x7E else settings_mod.POWER_OFF_DEFAULT_CODE    # turn it on for the test
        if t in (0, settings_mod.POWER_OFF_SPECIAL):
            return settings_mod.POWER_OFF_DEFAULT_CODE
        return t - 1 if t >= 0x7E else t + 1

    def step_power_off(self, data):
        self.enter("power_off", "The test reads the auto power-off time, changes it, and then sets the old "
                                "value again. No paper is necessary.")
        sc = self.svc.scanner
        try:
            before = settings_mod.read_power_off(sc)
        except IxError as e:
            data["read_error"] = str(e)
            return FAIL, "E6 get failed (nothing written): %s" % e
        data["before"] = list(before)
        data["before_text"] = settings_mod.describe_power_off(*before)
        test = self.trial_code(before[0])
        data["test"] = [test, before[1]]
        after = restored = None
        problem = None
        try:
            settings_mod.write_power_off(sc, test, before[1])
            after = settings_mod.read_power_off(sc)
            data["after"] = list(after)
        except (OSError, IxError) as e:
            problem = "E6 set or read back failed: %s" % short_error(e)
        finally:
            try:
                settings_mod.write_power_off(sc, before[0], before[1])
                restored = settings_mod.read_power_off(sc)
                data["restored"] = list(restored)
            except (OSError, IxError) as e:
                data["restore_error"] = str(e)
        if restored != before:
            minutes = settings_mod.power_off_minutes(before[0]) or 0
            self.ui.show("CAUTION: the old value is maybe not set again. Later, run: "
                         "ix1300 settings --power-off %d" % minutes)
            return FAIL, "restore not verified: wrote %s, read %s" % (
                data["before_text"], settings_mod.describe_power_off(*restored) if restored else "nothing")
        if problem:
            return FAIL, problem + " (old value restored)"
        if after != (test, before[1]):
            return FAIL, "wrote %s, read back %s (old value restored)" % (
                settings_mod.describe_power_off(test, before[1]), settings_mod.describe_power_off(*after))
        return PASS, "%s -> %s -> %s (restored)" % (data["before_text"],
                                                    settings_mod.describe_power_off(*after),
                                                    settings_mod.describe_power_off(*restored))

    def step_registration(self, data):
        self.enter("registration", "The test reads the registration data. It changes nothing. No paper is "
                                   "necessary.")
        svc = self.svc
        lines = data.setdefault("lines", [])

        def say(msg):
            lines.append(msg)
            self.ui.show("  " + msg)
        svc._release()                         # the mode-2 reserve needs a free scanner
        try:
            dev = locate(self.cfg, svc.scanner.host_ip, svc.scanner.host_mac, svc.net, trace=self.trace)
            register_mod.show(svc.scanner, dev, read_dc=True, say=say)
        finally:
            svc.scanner.close()
            svc._unreserve()
            self.ensure_reserved()
        text = "\n".join(lines)
        if "mode-2 reserve:" in text:
            return FAIL, "the scanner refused the mode-2 reserve (data recorded)"
        if "DC read failed" in text:
            return FAIL, "mode-2 reserve OK, but DC op 4/op 1 failed (data recorded)"
        dc = [l for l in lines if l.startswith("DC op")]
        return PASS, "mode-2 reserve OK; " + "; ".join(dc)

    def step_empty_press(self, data):
        trg = self.button("empty_press", "Remove all paper from the chute and the front slot.\n"
                                         "Then push the Scan button one time (short press).")
        if trg is None:
            return FAIL, "no button event in %d s" % self.button_timeout
        data["press"] = "long" if trg.long_press else "short"
        records, docs = self.run_job(PROFILE, data["press"])
        ops = port1_ops(records)
        data["ops"] = ops
        data["documents"] = docs
        still = self.yes_no("empty_press.rollers", "Did the rollers stay still?")
        data["rollers_still"] = still
        if "D5" in ops or docs:
            return FAIL, "a job started with no paper (%s)" % " ".join(ops)
        if not still:
            return FAIL, "no job started, but the user saw the rollers move"
        return PASS, "no job started (%s)" % " ".join(ops)

    def step_continue(self, data):
        svc = self.svc
        window = float(self.user_cfg.service.get("continue_window") or 0) or 60.0
        data["window"] = window
        trg = self.button("continue.1", "Load 2 plain sheets into the top chute.\n"
                                        "Then push the Scan button (short press).")
        if trg is None:
            return FAIL, "no button event in %d s" % self.button_timeout
        svc.cfg.service["continue_window"] = window
        docs = []
        try:
            records, d = self.run_job(PROFILE, "long" if trg.long_press else "short")
            docs += d
            data["part1_ops"] = port1_ops(records)
            data["part1_sense"] = sense_events(records)
            if svc.held is None:
                return FAIL, "part 1 did not wait for more pages (%s)" % (
                    ", ".join(fmt_sense(s) for s in data["part1_sense"]) or "no scan")
            trg = self.button("continue.2", "Load 2 more sheets into the top chute.\n"
                                            "Then push the Scan button again within %d s (short press)." % window,
                              timeout=window + 5, held=True)
            if trg is None or svc.held is None:
                return FAIL, "no second press within %d s" % window
            records, d = self.run_job(PROFILE, "long" if trg.long_press else "short")
            docs += d
            data["part2_ops"] = port1_ops(records)
            data["part2_sense"] = sense_events(records)
            if svc.held is not None:
                svc._deliver_held("self-test")
            docs += self.process_queue()
        finally:
            svc.cfg.service["continue_window"] = 0.0
            if svc.held is not None:
                svc._deliver_held("self-test")
                docs += self.process_queue()
        data["documents"] = docs
        if len(docs) != 1:
            return FAIL, "%d documents (1 expected)" % len(docs)
        doc = docs[0]
        parts = [(p["part"], p["sheets"], p["paper_path"]) for p in (doc["parts"] or [])]
        sheets = [r["sheet"] for r in doc["page_records"]]
        if parts != [(1, 2, "top"), (2, 2, "top")]:
            return FAIL, "parts %s (2 + 2 sheets from the top chute expected)" % parts
        if doc["pages"] != 8 or sheets != [1, 1, 2, 2, 3, 3, 4, 4]:
            return FAIL, "%d pages, sheet order %s (8 pages, sheets 1 to 4 expected)" % (doc["pages"], sheets)
        return PASS, "one document: 2 parts, 4 sheets, 8 pages in order"

    def step_front_slot(self, data):
        trg = self.button("front_slot", "Make sure that the top chute is empty.\n"
                                        "Put ONE sheet into the front slot, then push the Scan button (short press).")
        if trg is None:
            return FAIL, "no button event in %d s" % self.button_timeout
        records, docs = self.run_job(PROFILE, "long" if trg.long_press else "short")
        data["ops"] = port1_ops(records)
        data["sense"] = sense_events(records)
        data["documents"] = docs
        e9 = out_data(records, "E9")
        d4 = out_data(records, "D4")
        data["e9"] = [x.hex() for x in e9]
        data["d4"] = [x.hex() for x in d4]
        back = self.yes_no("front_slot.back", "Did the sheet come back out at the front?")
        data["sheet_back_at_front"] = back
        if len(docs) != 1:
            return FAIL, "no document (%s)" % (", ".join(fmt_sense(s) for s in data["sense"]) or "no scan")
        doc = docs[0]
        problems = []
        if doc["paper_path"] != "front":
            problems.append("paper path %s" % doc["paper_path"])
        if doc["pages"] != 2:
            problems.append("%d pages (2 expected)" % doc["pages"])
        if not e9 or e9[0][8:12] != bytes.fromhex("00004410"):
            problems.append("E9 pre-read length is not 0x4410")
        if not d4 or d4[0][1] != 0x06:
            problems.append("D4 byte 1 is not 06")
        if not back:
            problems.append("the sheet did not come back at the front")
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, "front slot job: 1 sheet, 2 sides, sheet back at the front"

    def step_long_receipt(self, data):
        if not self.yes_no("long_receipt.have", "Do you have a receipt longer than 90 cm?"):
            raise Skip("no long receipt")
        trg = self.button("long_receipt", "Load the receipt into the top chute. Keep the paper guides against "
                                          "its edges, and guide the paper with your hand.\n"
                                          "Hold the Scan button until it flashes blue, then release it (long press).")
        if trg is None:
            return FAIL, "no button event in %d s" % self.button_timeout
        data["press"] = "long" if trg.long_press else "short"
        records, docs = self.run_job(PROFILE_LONG, data["press"])
        data["ops"] = port1_ops(records)
        data["sense"] = sense = sense_events(records)
        data["documents"] = docs
        data["e9"] = [x.hex() for x in out_data(records, "E9")]
        data["d4"] = [x.hex() for x in out_data(records, "D4")]
        note = "" if trg.long_press else " (a short press came, not a long press)"
        reject = [s for s in sense if (s["key"], s["asc"]) in REJECT_SENSE and s["op"] in ("E9", "D4", "READ")]
        if reject:
            data["rejection"] = reject
            started = "D5" in data["ops"]
            self.enter("long_receipt.again",
                       "The scanner refused the long page mode. The test scans the receipt again with the "
                       "automatic length (up to 895 mm).\n" +
                       ("Load the receipt into the top chute again." if started else
                        "The receipt must still be in the top chute (load it again if not)."))
            records, docs2 = self.run_job(PROFILE, "long")
            data["fallback_ops"] = port1_ops(records)
            data["fallback_sense"] = sense_events(records)
            data["fallback_documents"] = docs2
            got = ", ".join("%.0f mm" % page_mm(r) for d in docs2 for r in d["page_records"]) or "no page"
            return FAIL, "long page mode refused (%s); automatic length: %s%s" % (
                ", ".join(fmt_sense(s) for s in reject), got, note)
        if not docs:
            return FAIL, "no document (%s)%s" % (", ".join(fmt_sense(s) for s in sense) or "no scan", note)
        lengths = [page_mm(r) for r in docs[0]["page_records"]]
        data["lengths_mm"] = lengths
        longest = max(lengths or [0])
        if longest <= LONG_MM:
            return FAIL, "the longest page has only %.0f mm (more than %.0f mm expected)%s" % (longest, LONG_MM,
                                                                                               note)
        if sense:
            return FAIL, "page of %.0f mm, but sense %s%s" % (longest, ", ".join(fmt_sense(s) for s in sense), note)
        return PASS, "long page mode: %d pages, longest %.0f mm%s" % (len(lengths), longest, note)

    def step_double_feed(self, data):
        if not self.yes_no("double_feed.have", "Do you have 2 sheets taped together with an overlap?"):
            raise Skip("no taped sheets")
        self.enter("double_feed.on", "Load the taped sheets into the top chute. The next scan has the "
                                     "double-feed detection ON.")
        records, docs = self.run_job(PROFILE, "short")
        on = data["on"] = dict(ops=port1_ops(records), sense=sense_events(records), documents=docs,
                               d4=[x.hex() for x in out_data(records, "D4")])
        self.enter("double_feed.off", "Take the taped sheets out of the scanner (pull them out gently if they "
                                      "stay in the paper path).\nLoad them again into the top chute. The next "
                                      "scan has the double-feed detection OFF.")
        records, docs = self.run_job(PROFILE_DF_OFF, "short")
        off = data["off"] = dict(ops=port1_ops(records), sense=sense_events(records), documents=docs,
                                 d4=[x.hex() for x in out_data(records, "D4")])
        problems = []
        if not any((s["key"], s["asc"], s["ascq"]) == (3, 0x80, 0x07) for s in on["sense"]):
            problems.append("detection on: no double-feed sense (%s)" % (
                ", ".join(fmt_sense(s) for s in on["sense"]) or "no error"))
        if not on["d4"] or bytes.fromhex(on["d4"][0])[4] != 0x88:
            problems.append("detection on: D4 byte 4 is not 88")
        if off["sense"]:
            problems.append("detection off: sense %s" % ", ".join(fmt_sense(s) for s in off["sense"]))
        if not off["d4"] or bytes.fromhex(off["d4"][0])[4] != 0x00:
            problems.append("detection off: D4 byte 4 is not 00")
        if not off["documents"]:
            problems.append("detection off: no document")
        if problems:
            return FAIL, "; ".join(problems)
        return PASS, "on: double feed 3/80/07; off: no error, %d pages" % off["documents"][0]["pages"]

    def step_stop(self, data):
        if not self.yes_no("stop.have", "Test the service stop during a scan? It needs 3 sheets."):
            raise Skip("not wanted")
        self.enter("stop.load", "Load 3 plain sheets into the top chute. The test starts a scan and stops it "
                                "during the first sheet.\nThe scanner then feeds the other sheets out "
                                "without a scan.")
        svc = self.svc
        sc = svc.scanner
        state = dict(at=None)

        def hook(rec):
            if (state["at"] is None and rec.get("dir") == "out" and rec.get("port") == "port1"
                    and op_name(rec) == "READ"):
                state["at"] = rec["n"]
                self._trigger_stop()
        poll = sc.abort_poll
        sc.abort_poll = min(poll, 0.001)     # the stop comes while the READ waits for its reply
        self.trace.hook = hook
        try:
            records, docs = self.run_job(PROFILE, "short")
        finally:
            self.trace.hook = None
            sc.abort_poll = poll
            self._clear_stop()
        data["ops"] = port1_ops(records)
        data["documents"] = docs
        if state["at"] is None:
            return FAIL, "no READ: the scan did not start (%s)" % " ".join(data["ops"])
        after = [r for r in records if r["n"] > state["at"]] if records else []
        # The records since the stop: the commands of the cancel and their replies.
        seq, new_conn, replies = [], False, {}
        pending = {}
        for r in after:
            if r.get("port") != "port1":
                continue
            if r["dir"] == "conn" and not seq:
                new_conn = True
            elif r["dir"] == "out":
                seq.append(op_name(r))
                pending[r["x"]] = op_name(r)
            elif r["dir"] == "in" and r.get("x") in pending:
                replies.setdefault(pending[r["x"]], []).append(dict(scsi=r.get("scsi"), sense=r.get("sense")))
        data.update(after_stop=seq, new_connection=new_conn, replies=replies)
        # The paper path after the job, on a new Port1 connection.
        sc = svc.scanner
        sc.close()
        st = sc.hw_status()
        data["c2_after"] = st.raw.hex()
        empty = st.hopper_empty and not st.paper_at_feed
        data["path_empty"] = empty
        out = self.number("stop.count", "How many sheets came out of the scanner?")
        data["sheets_out"] = out
        order = [o for o in seq if o in ("D8", "E0", "D6")]
        ok_order = order[:1] == ["D8"] and "E0" in order and order[-1:] == ["D6"]
        how = "new connection" if new_conn else "same connection"
        if not ok_order:
            return FAIL, "after the stop: %s (D8, E0, D6 expected)" % " ".join(seq)
        if not empty:
            return FAIL, "D8, E0, D6 sent (%s), but C2 shows paper in the path (%s)" % (how, data["c2_after"])
        return PASS, "D8, E0, D6 (%s); paper path empty; %s sheets out" % (
            how, "?" if out is None else out)
