"""Scan service: keep a reserve, wait for the scan button, scan, make a PDF, send it.

Threads:
  main       reserve, trigger packets, scan jobs (one at a time). It owns the scanner;
  keepalive  UDP keepalive while we have a reserve;
  worker     pipeline and outputs (the scanner is free again during this work);
  retry      new attempts for the jobs in work_dir/unsent.
If one of these threads stops because of an error, run_forever() gives exit code 1,
so that Docker starts the container again.

Each job has a directory on disk (jobs.JobDir). The sheets go to disk before the
service works on them, so a failure or a restart does not lose a scan.
"""
import concurrent.futures
import errno
import logging
import os
import queue
import signal
import socket
import threading
import time

from . import outputs as outputs_mod
from . import settings as settings_mod
from .config import ConfigError
from .jobs import FAILED, JOBS, PDF, UNSENT, JobDir, free_mb, keep_raw, list_jobs
from .net import SystemNet, local_ip_for, mac_for_ip, parse_mac
from .pipeline import Budget, Document, Page, Pipeline, have_pillow, side_order, tesseract_path
from .protocol import (PORT1, PORT2, TRG_FORCE_RELEASE, TRG_POWER_OFF, TRG_POWER_ON, TRG_REQUEST_RELEASE,
                       TRG_SCAN, IxError, NoPaper, PaperLost, ReserveError, Scanner, SenseError, TriggerDedup,
                       discover, parse_trigger)

log = logging.getLogger("ix1300.daemon")

REQUEST_RELEASE_HOLD = 60.0


def host_address(cfg):
    """Return (host IP, host MAC bytes) from the configuration or by detection."""
    sc = cfg.scanner
    ip = sc.get("host_ip") or local_ip_for(sc.get("ip") or None)
    mac = parse_mac(sc["host_mac"]) if sc.get("host_mac") else mac_for_ip(ip)
    return ip, mac


def locate(cfg, host_ip, host_mac, net=None):
    """Find the scanner with a search probe. Return a DeviceInfo, or None.

    The probe goes as broadcast and, with scanner.ip set, also as unicast
    (3 times each). A reply also refreshes the ARP entry of the scanner on the
    host, and shows that the scanner is on.
    """
    sc = cfg.scanner
    ip = sc.get("ip") or None
    devs = discover(host_ip, host_mac, target=None, port=int(sc["search_port"]),
                    timeout=float(sc["discovery_timeout"]), net=net,
                    broadcast=sc.get("broadcast") or "255.255.255.255", unicast=ip)
    want = (sc.get("name") or "").strip()
    for d in devs:
        if "iX1300" not in d.product:
            log.debug("ignore %s (%s)", d.name, d.product)
            continue
        if ip and d.ip != ip:
            continue
        if want and want not in (d.name, d.serial):
            continue
        return d
    return None


def short_error(e):
    """A short text for network errors (no stack trace)."""
    if isinstance(e, socket.timeout):
        return "timed out"
    if isinstance(e, OSError) and e.errno in (errno.EHOSTUNREACH, errno.ENETUNREACH, errno.ECONNREFUSED,
                                              errno.ECONNRESET, errno.ETIMEDOUT, errno.EHOSTDOWN):
        return os.strerror(e.errno).lower()
    if isinstance(e, SenseError):
        return SenseError.describe(e.sense)
    return str(e) or e.__class__.__name__


BUSY_HINT = "scanner busy with a USB host or a stale job; close and open the cover"


def occupied_text(scanner, net=None):
    """Explain a reserve result -4 with a unicast search (device status +38)."""
    try:
        devs = discover(scanner.host_ip, scanner.host_mac, target=scanner.ip, port=scanner.search_port,
                        timeout=1.0, net=net)
    except OSError:
        devs = []
    if devs and devs[0].devstatus == 2:
        return BUSY_HINT
    if devs and devs[0].owner_ip != "0.0.0.0":
        return "scanner occupied by another host (%s, host ID %s); retry later" % (devs[0].owner_ip,
                                                                                  devs[0].owner_id)
    return "scanner occupied by another host; retry later"


def make_scanner(cfg, host_ip, host_mac, dev=None, net=None, trigger_port=None):
    sc = cfg.scanner
    if cfg.host_id is None:
        raise IxError("scanner.host_id is not set (see README: usb-register-host)")
    ip = dev.ip if dev else sc.get("ip")
    if not ip:
        raise IxError("no scanner found")
    if dev and dev.tls:
        log.warning("the scanner asks for TLS ports %d/%d; TLS is not implemented", dev.port1, dev.port2)
    return Scanner(ip, host_ip, host_mac, cfg.host_id,
                   port1=dev.port1 if dev else PORT1, port2=dev.port2 if dev else PORT2,
                   trigger_port=trigger_port or int(sc["trigger_port"]), search_port=int(sc["search_port"]),
                   net=net, timeout=float(sc["command_timeout"]), data_timeout=float(sc["data_timeout"]),
                   reserve_timeout=int(sc["reserve_timeout"]), frame_timeout=float(sc["frame_timeout"]),
                   side_timeout=float(sc["side_timeout"]), max_side_bytes=int(sc["max_side_mb"]) << 20)


class Service:
    # Internal waits in seconds. The tests set shorter values; these are the values for the service.
    trigger_poll = 0.5                     # longest wait for a trigger packet before the next state check
    error_pause = 1.0                      # pause of the scan thread after an unexpected error
    retry_first = 1.0                      # the first examination of work_dir/unsent after the start
    retry_period = 5.0                     # then one examination in this period
    retry_poll = 1.0                       # the retry thread examines the time in this period

    def __init__(self, cfg, net=None, outputs=None, pipeline=None, clock=None, scanner_options=None):
        """CLOCK: a function that gives the time in seconds (default time.monotonic). The service
        uses it for the hold times, the retry times and the stop times. SCANNER_OPTIONS: attributes
        that the service sets on each new Scanner (for example poll and flush_poll in the tests)."""
        self.cfg = cfg
        self.net = net or SystemNet()
        self.clock = clock or time.monotonic
        self.scanner_options = dict(scanner_options or {})
        self.stop_event = threading.Event()
        self.outputs = outputs if outputs is not None else outputs_mod.build(cfg.outputs)
        for o in self.outputs.values():
            if hasattr(o, "stop"):
                o.stop = self.stop_event         # no long waits between attempts at stop
        self.pipeline = pipeline or Pipeline(cfg.pipeline)
        self.work_dir = cfg.service.get("work_dir") or "/data"
        self.budget = Budget(int(float(cfg.pipeline.get("max_memory_mb", 64)) * (1 << 20)))
        self.lock = threading.Lock()
        self.epoch = 0                     # +1 at each reserve and release (keepalive results)
        self.lost = threading.Event()
        self.lost_epoch = None
        self.reserved = False
        self.scanner = None
        self.trigger_sock = None
        self.trigger_port = None
        self.dedup = TriggerDedup()
        self.dedup_reset = True            # forget the sequence numbers at the next reserve
        self.jobs = queue.Queue()
        self.backlog = 0                   # jobs that wait for the worker or that it works on
        self.jobs_done = 0                 # jobs that the worker completed (sent, unsent or failed)
        self.threads = {}
        self.ka_failures = 0
        self.hold_until = 0.0
        self.last_error = None
        self.wait_attempts = 0
        self.wait_logged = 0.0
        self.ignored_sources = set()
        self.documents = []                # data of the last documents (no PDF data), newest last
        self.scans = 0
        self.stop_at = None
        self.stopped = False
        self.fatal = None
        self.retry_next = {}               # job ID -> self.clock() of the next attempt
        self.output_locks = {}             # output name -> Lock: one document at a time for each output
        self.stop_deadline = None          # self.clock(): a READ that still waits stops (Scanner.abort)
        workers = max(1, int(cfg.pipeline.get("workers", 1)))
        self.executor = concurrent.futures.ThreadPoolExecutor(max_workers=workers, thread_name_prefix="ix1300-page")

    # --- Life cycle ---
    def start(self):
        sc = self.cfg.scanner
        log.info("ix1300 service: Pillow %s, tesseract %s", "yes" if have_pillow() else "no",
                 tesseract_path() or "no")
        # Bind to the host address if it is set; else to all interfaces.
        self.trigger_sock = self.net.udp(bind=(sc.get("host_ip") or "", int(sc["trigger_port"])))
        self.trigger_port = self.trigger_sock.getsockname()[1]
        log.info("trigger port %d", self.trigger_port)
        try:
            self.recover()
        except Exception:
            log.exception("recovery of old jobs failed")
        for name, fn in (("main", self._main), ("keepalive", self._keepalive), ("worker", self._worker),
                         ("retry", self._retry_loop)):
            t = threading.Thread(target=self._run_thread, args=(name, fn), name="ix1300-" + name, daemon=True)
            t.start()
            self.threads[name] = t

    def _run_thread(self, name, fn):
        try:
            fn()
        except BaseException:
            log.critical("thread %s stopped because of an error", name, exc_info=True)
            self.fatal = name
            return
        if not self.stop_event.is_set():
            log.critical("thread %s stopped", name)
            self.fatal = name

    def stop(self, timeout=None):
        """Stop in order: no new triggers, end the scan (D8, E0, D6 after stop_scan_wait), release
        the scanner (main thread), then let the worker finish the queued jobs. After TIMEOUT
        the jobs stay on disk and the next start continues them."""
        if self.stopped:
            return
        self.stopped = True
        timeout = float(self.cfg.service.get("stop_timeout", 60)) if timeout is None else timeout
        deadline = time.monotonic() + timeout
        self.stop_at = self.clock()
        # A READ that still waits for data stops at this time (Scanner.abort), so that the
        # scan thread can send D8, E0 and D6 and release the scanner before the deadline.
        self.stop_deadline = self.stop_at + timeout - min(10.0, timeout / 2.0)
        self.stop_event.set()
        for name in ("main", "keepalive", "retry"):
            t = self.threads.get(name)
            if t is not None:
                t.join(max(0.1, deadline - time.monotonic()))
        main = self.threads.get("main")
        if main is not None and main.is_alive():
            # No end marker for the worker: the scan thread can still queue its job.
            log.warning("the scan did not stop in time: the job stays in %s for the next start",
                        os.path.join(self.work_dir, JOBS))
        worker = self.threads.get("worker")
        if worker is not None and not (main is not None and main.is_alive()):
            self.jobs.put(None)
            worker.join(max(0.1, deadline - time.monotonic()))
        if worker is not None and worker.is_alive():
            log.warning("document work not complete: the next start continues the jobs in %s",
                        os.path.join(self.work_dir, JOBS))
        self.executor.shutdown(wait=False, cancel_futures=True)
        if self.trigger_sock:
            self.trigger_sock.close()

    def wait_idle(self):
        self.jobs.join()

    def run_forever(self):
        """Run until SIGTERM or SIGINT. Return the exit code (1 if a thread stopped)."""
        self.start()
        stop = threading.Event()
        for sig in (signal.SIGTERM, signal.SIGINT):
            signal.signal(sig, lambda *a: stop.set())
        code = 0
        while not stop.wait(1.0):
            dead = [n for n, t in self.threads.items() if not t.is_alive()]
            if dead or self.fatal:
                log.critical("thread %s stopped: the service stops (exit code 1)", self.fatal or dead[0])
                code = 1
                break
        log.info("stop")
        self.stop()
        return code

    # --- Reserve ---
    def _note(self, level, msg, *args):
        """Log a reserve problem one time. Log "still waiting" at INFO each waiting_log_interval."""
        text = msg % args
        now = self.clock()
        if text != self.last_error:          # a new state: log it, and start to count again
            log.log(level, text)
            self.last_error = text
            self.wait_attempts = 1
            self.wait_logged = now
            return
        self.wait_attempts += 1
        if now - self.wait_logged >= float(self.cfg.scanner.get("waiting_log_interval", 600)):
            log.info("still waiting for the scanner (%d attempts, last error: %s)", self.wait_attempts, text)
            self.wait_logged = now

    def _try_reserve(self):
        if self.clock() < self.hold_until:
            return False
        try:
            host_ip, host_mac = host_address(self.cfg)
            dev = locate(self.cfg, host_ip, host_mac, self.net)
            if dev is None:
                self._note(logging.INFO, "the scanner does not answer the search (off or asleep): waiting. "
                           "Open the scanner cover to turn it on")
                return False
            self.scanner = make_scanner(self.cfg, host_ip, host_mac, dev, self.net, self.trigger_port)
            for name, value in self.scanner_options.items():
                setattr(self.scanner, name, value)
            self.scanner.reserve()
        except ReserveError as e:
            if e.result == -4:
                self._note(logging.WARNING, "reserve: %s", occupied_text(self.scanner, self.net))
            elif e.result == -6:
                self._note(logging.ERROR, "reserve: host ID %s is not registered on the scanner. "
                           "Another computer (ScanSnap Home) registered its ID. Do usb-register-host again, "
                           "or use the ID of that computer", self.cfg.scanner["host_id"])
            else:
                self._note(logging.ERROR, "%s", e)
            return False
        except (OSError, IxError) as e:
            self._note(logging.INFO, "scanner not available: %s", short_error(e))
            return False
        with self.lock:
            self.reserved = True
            self.epoch += 1
            self.ka_failures = 0
            self.lost.clear()
        if self.dedup_reset:                 # only after a possible restart of the scanner
            self.dedup.reset()
            self.dedup_reset = False
        self.last_error = None
        log.info("reserved scanner %s", self.scanner.ip)
        settings_mod.apply_power_off(self.scanner, self.cfg.scanner.get("power_off_minutes"))
        return True

    def _unreserve(self, reset_dedup=False):
        with self.lock:
            self.reserved = False
            self.epoch += 1
        if reset_dedup:
            self.dedup_reset = True

    def _release(self):
        if self.scanner is None:
            return
        try:
            self.scanner.close()
            res = self.scanner.release()
            if res != 0:
                log.warning("release: result %d", res)
        except (OSError, IxError) as e:
            log.debug("release: %s", e)
        self._unreserve()

    # --- Threads ---
    def _from_scanner(self, trg):
        """True if TRG comes from our scanner. The protocol has no authentication: this is a filter only."""
        want = self.scanner.ip if self.scanner is not None else (self.cfg.scanner.get("ip") or None)
        src = trg.src[0] if trg.src else None
        if want and src == want:
            return True
        if src not in self.ignored_sources and len(self.ignored_sources) < 100:
            self.ignored_sources.add(src)
            log.warning("trigger packet from %s ignored (scanner: %s)", src, want or "not known yet")
        return False

    def _recv_trigger(self, timeout):
        self.trigger_sock.settimeout(timeout)
        try:
            data, src = self.trigger_sock.recvfrom(256)
        except socket.timeout:
            return None
        except OSError as e:
            if not self.stop_event.is_set():
                log.warning("trigger socket: %s", e)
                time.sleep(timeout)
            return None
        trg = parse_trigger(data, src)
        if trg is None:
            log.debug("UDP packet from %s (%d bytes) is not a trigger", src[0] if src else "?", len(data))
            return None
        return trg if self._from_scanner(trg) else None

    def _main(self):
        try:
            while not self.stop_event.is_set():
                try:
                    self._main_step()
                except Exception:
                    # A failing job or packet must not stop the scan thread.
                    log.exception("unexpected error in the scan thread: reserve again")
                    self._unreserve()
                    self.stop_event.wait(self.error_pause)
        finally:
            if self.reserved and self.scanner is not None:
                self._release()
                log.info("released the scanner")

    def _main_step(self):
        sc = self.cfg.scanner
        if not self.reserved:
            if self._try_reserve():
                self.delay = float(sc["retry_min"])
                return
            # Wait, but react to a POWER ON trigger at once.
            delay = getattr(self, "delay", float(sc["retry_min"]))
            end = self.clock() + delay
            self.delay = min(delay * 2, float(sc["retry_max"]))
            while not self.stop_event.is_set() and self.clock() < end:
                trg = self._recv_trigger(min(self.trigger_poll, max(0.01, end - self.clock())))
                if trg and trg.code == TRG_POWER_ON and self.dedup.is_new(trg):
                    log.info("scanner power on")
                    self._power_on(trg)
                    self.hold_until = 0.0
                    self.delay = float(sc["retry_min"])
                    break
            return
        trg = self._recv_trigger(self.trigger_poll)
        if self.lost.is_set():
            self.lost.clear()
            if self.lost_epoch == self.epoch:
                log.warning("keepalive failed: reserve again")
                self._unreserve(reset_dedup=True)
                return
        if trg is not None and not self.stop_event.is_set():
            self._on_trigger(trg)

    def _power_on(self, trg):
        """The scanner starts again: its sequence numbers start at 1. Keep this event, so its copies are dropped."""
        self.dedup.reset()
        self.dedup.is_new(trg)
        self.dedup_reset = False

    def _on_trigger(self, trg):
        if not self.dedup.is_new(trg):
            return
        log.debug("trigger code 0x%02x seq %d cond %s", trg.code, trg.seq, trg.cond.hex(" "))
        if trg.code == TRG_SCAN:
            svc = self.cfg.service
            name = svc["short_press_profile"]
            if trg.long_press and svc.get("long_press_profile"):
                name = svc["long_press_profile"]
            log.info("scan button (%s press): profile %s", "long" if trg.long_press else "short", name)
            self.run_job(name)
            self._drain_triggers()
        elif trg.code == TRG_POWER_ON:
            log.info("scanner power on: reserve again")
            self._power_on(trg)
            self._unreserve()
        elif trg.code == TRG_POWER_OFF:
            hold = float(self.cfg.scanner.get("power_off_hold", 20))
            log.info("scanner power off: no reserve for %.0f s (or until POWER ON)", hold)
            self._unreserve(reset_dedup=True)
            self.hold_until = self.clock() + hold
        elif trg.code == TRG_FORCE_RELEASE:
            log.info("scanner force release")
            self._unreserve()
        elif trg.code == TRG_REQUEST_RELEASE:
            if self.cfg.scanner.get("yield_on_request", True):
                log.info("another host asks for the scanner: release for %d s", REQUEST_RELEASE_HOLD)
                self._release()
                self.hold_until = self.clock() + REQUEST_RELEASE_HOLD
        else:
            log.debug("trigger code 0x%02x ignored", trg.code)

    def _drain_triggers(self):
        """After a job: drop the scan triggers that came during the job. Handle the others."""
        keep = []
        while True:
            trg = self._recv_trigger(0.001)
            if trg is None:
                break
            if trg.code == TRG_SCAN:
                self.dedup.is_new(trg)
                log.debug("scan trigger seq %d during the job: dropped", trg.seq)
            else:
                keep.append(trg)
        for trg in keep:
            self._on_trigger(trg)

    def _admit(self):
        """True if a new scan can start. The scanner feeds the full stack after the start,
        so the service must decide before the job (PROTOCOL.md 8.5)."""
        sv = self.cfg.service
        try:
            free = free_mb(self.work_dir)
        except OSError as e:
            log.error("scan not started: work_dir %s is not usable (%s)", self.work_dir, e)
            return False
        if free < float(sv["min_free_mb"]):
            log.error("scan not started: only %d MB free in %s (service.min_free_mb = %d). "
                      "Make space, then push the button again", free, self.work_dir, sv["min_free_mb"])
            return False
        if self.backlog >= int(sv["max_backlog"]):
            log.error("scan not started: %d jobs wait for processing (service.max_backlog = %d). "
                      "Wait, then push the button again", self.backlog, sv["max_backlog"])
            return False
        return True

    def _cancel_scan(self):
        """At stop: let the scan continue for stop_scan_wait seconds, then stop it (D8, E0, D6).

        The scanner then feeds the rest of the stack out without a scan [HW, PROTOCOL.md 8.6]."""
        return (self.stop_event.is_set() and self.stop_at is not None and
                self.clock() >= self.stop_at + float(self.cfg.service.get("stop_scan_wait", 15)))

    def _abort_read(self):
        """At stop: True after stop_deadline. A READ that still waits then stops (the sheet is lost)."""
        return (self.stop_event.is_set() and self.stop_deadline is not None and
                self.clock() >= self.stop_deadline)

    def run_job(self, profile_name):
        """Scan with one profile. The caller must have a reserve.

        Each complete sheet goes to disk and to the page workers at once
        (Job.add_sheet). Only complete sheets go into the document. If an error
        stops the scan before the first complete sheet, no document is made.
        """
        if not self._admit():
            return None
        # Start no job with an empty chute. An empty job puts the scanner into its
        # "after the last sheet" state: the next 3 s hold then returns the stacker
        # and sends no event [HW]. The hopper bit is valid before the first sheet.
        if self.cfg.service.get("check_paper", True):
            try:
                empty = self.scanner.hw_status().hopper_empty
            except (OSError, IxError) as e:
                log.debug("paper check: %s", short_error(e))
                empty = False
            if empty:
                log.warning("scan button (%s): no paper in the chute; no job started", profile_name)
                return None
        try:
            profile = self.cfg.profile(profile_name)
            job = Job(self, profile)
        except (OSError, ConfigError) as e:
            log.error("scan not started: %s", e)
            return None
        result, error = None, None
        t0 = time.monotonic()
        self.scanner.abort = self._abort_read
        try:
            result = self.scanner.scan(profile.scan_settings(), on_sheet=job.add_sheet, cancel=self._cancel_scan)
        except PaperLost as e:
            result, error = e.result, e
            log.error("scan stopped after %d complete sheets: paper lost in the middle of a sheet", job.sheets)
        except NoPaper as e:
            result, error = e.result, e
            log.warning("no paper in the scanner" if not job.sheets else "scan stopped: no paper")
        except SenseError as e:
            result, error = e.result, e
            log.error("scan stopped after %d complete sheets: %s", job.sheets, SenseError.describe(e.sense))
        except (OSError, IxError) as e:
            result, error = getattr(e, "result", None), e
            log.error("scan failed after %d complete sheets: %s", job.sheets, short_error(e))
        except Exception as e:
            error = e
            log.exception("scan failed after %d complete sheets", job.sheets)
        finally:
            try:
                self.scanner.close()
            except OSError:
                pass
            if self.scanner.job_state == "uncertain":
                log.warning("the scanner did not confirm the end of the job: release and reserve again")
                self._release()
            elif self.cfg.scanner.get("renew_after_job", False):
                self._release()
                self._try_reserve()
        normal = error is None or (isinstance(error, NoPaper) and not isinstance(error, PaperLost))
        deliver = job.sheets > 0 and (normal or bool(self.cfg.service.get("deliver_partial", True)))
        if not normal and job.sheets:
            job.warnings.append("scan stopped after %d sheets: %s" % (job.sheets, short_error(error)))
            if not deliver:
                log.warning("deliver_partial = false: %d complete sheets not sent (kept in %s)", job.sheets,
                            os.path.join(self.work_dir, FAILED))
        if result is not None:
            job.warnings.extend(w for w in result.warnings if w not in job.warnings)
        if deliver:
            self.scans += 1
            log.info("scan done: %d sheets in %.1f s%s", job.sheets, time.monotonic() - t0,
                     " (probable paper jam)" if result is not None and result.jam_warning else "")
        job.end_scan(deliver)
        self._queue(job)
        return result

    def _queue(self, job):
        with self.lock:
            self.backlog += 1
        self.jobs.put(job)

    def _keepalive(self):
        interval = float(self.cfg.scanner["keepalive_interval"])
        while not self.stop_event.wait(interval):
            self.keepalive_once()

    def keepalive_once(self):
        """One keepalive probe (the keepalive thread calls it each keepalive_interval).

        Return True or False (reply or no reply), or None if the result is not valid
        (no reserve, or a release or a new reserve during the probe).
        """
        sc = self.cfg.scanner
        interval = float(sc["keepalive_interval"])
        with self.lock:
            scanner, epoch, reserved = self.scanner, self.epoch, self.reserved
        if not reserved or scanner is None:
            return None
        try:
            ok = scanner.keepalive(timeout=min(float(sc["keepalive_wait"]), interval * 0.8),
                                   repeat=int(sc["keepalive_repeat"]), interval=0.5)
        except OSError as e:
            log.debug("keepalive: %s", short_error(e))
            ok = False
        with self.lock:
            if epoch != self.epoch:              # released or reserved again during the probe: not valid
                return None
            if ok:
                self.ka_failures = 0
                return True
            self.ka_failures += 1
            n = self.ka_failures
        log.warning("keepalive: no reply (%d)", n)
        if n >= int(sc["keepalive_failures"]):
            self.lost_epoch = epoch
            self.lost.set()
        return False

    # --- Documents and outputs (worker thread) ---
    def _retry_loop(self):
        """New attempts for the jobs in work_dir/unsent. A thread of its own: a slow output
        does not stop the PDF work of new scans."""
        next_retry = self.clock() + self.retry_first
        while not self.stop_event.wait(self.retry_poll):
            if self.clock() >= next_retry:
                next_retry = self.clock() + self.retry_period
                try:
                    self.retry_unsent()
                except Exception:
                    log.exception("new attempt for unsent jobs failed")

    def _worker(self):
        while True:
            try:
                item = self.jobs.get(timeout=1.0)
            except queue.Empty:
                continue
            if item is None:
                self.jobs.task_done()
                return
            try:
                self.process(item)
            except Exception:
                log.exception("document processing failed")
            finally:
                with self.lock:
                    self.backlog -= 1
                    self.jobs_done += 1
                self.jobs.task_done()

    def process(self, job, save_to=None, send=True):
        """Make the PDF of JOB and send it. Remove the job directory only if all outputs have it
        and no original must stay (JobDir.retain_reason). Then give the job lock back.

        SAVE_TO (CLI): also write the PDF to this file ("" = the document file name).
        SEND=False (CLI without --send): no outputs; the saved file is the delivery.
        """
        try:
            return self._process(job, save_to, send)
        finally:
            job.dir.unclaim()

    def _adopt(self, job):
        """Find the side files that the manifest does not know (crash or full disk) and add their pages."""
        new = job.dir.reconcile()
        for sides in job.dir.iter_sides(set(new)):
            job.sheets += 1
            job.submit(sides)
        for w in job.dir.m.get("warnings") or []:
            if w not in job.warnings:
                job.warnings.append(w)

    def _process(self, job, save_to, send):
        jd = job.dir
        doc, pages = None, []
        sv = self.cfg.service
        try:
            self._adopt(job)
            pages = job.pages()
            warnings = list(job.warnings) + [w for w in job.failures if w not in job.warnings]
            deliver = job.deliver and (not job.failures or bool(sv.get("deliver_partial", True)))
            if deliver and pages:
                doc = self.pipeline.finish(pages, job.profile, warnings, out_dir=jd.path, store=job.store,
                                           pdf_name=PDF)
        except Exception as e:
            log.exception("document processing failed: the sheets stay in %s", os.path.join(self.work_dir, FAILED))
            jd.m["error"] = "processing failed: %s" % (str(e) or e.__class__.__name__)
            self._settle_failed(jd)
            return None
        finally:
            if sv.get("keep_raw") and jd.m["sheets"]:
                try:
                    keep_raw(jd, pages, doc, job.profile, self.cfg.pipeline, int(sv.get("keep_raw_max_jobs", 20)))
                except Exception as e:
                    log.warning("keep_raw failed: %s", e)
            job.store.close()                    # the JPEG data goes, then its memory budget
        jd.m["missing"] = list(job.failures)     # also after a restart: the originals stay (_settle)
        if doc is None:
            reason = jd.retain_reason()
            if not jd.m["sheets"] and not reason:
                jd.remove()
            elif job.deliver and not job.failures and not reason:
                log.warning("no page left after blank page removal: nothing sent")
                jd.remove()
            else:
                why = []
                if jd.m["sheets"]:
                    why.append("not sent: incomplete job and deliver_partial = false" if pages
                               else "not sent: no usable page")
                jd.m["error"] = "; ".join(why + [reason or ""]).strip("; ")
                self._settle_failed(jd)
            return None
        doc.job_id = jd.id
        jd.m["document"] = dict(filename=doc.filename, title=doc.title, profile=doc.profile, pages=doc.pages,
                                receipt=doc.receipt, created=doc.created, warnings=doc.warnings, size=doc.size)
        names = list(job.profile.outputs or self.outputs.keys()) if send else []
        jd.m["outputs"] = {n: {"ok": False, "attempts": 0} for n in names}
        jd.m["state"] = "delivering"
        if save_to is not None:
            try:
                jd.m["saved"] = doc.save(save_to or doc.filename)
            except OSError as e:
                jd.m["save_error"] = "cannot write %s: %s" % (save_to or doc.filename, e.strerror or e)
                log.error("%s", jd.m["save_error"])
        jd.save()
        self.send(jd, doc)
        self.documents.append(dict(filename=doc.filename, pages=doc.pages, receipt=doc.receipt, size=doc.size,
                                   status={n: st["ok"] for n, st in jd.m["outputs"].items()}))
        del self.documents[:-20]
        self._settle(jd)
        return doc

    def _send_one(self, jd, doc, name, mutex):
        st = dict(jd.m["outputs"].get(name) or {"ok": False, "attempts": 0})
        out = self.outputs.get(name)
        st["attempts"] = st.get("attempts", 0) + 1
        if out is None:
            st.update(ok=False, error="no output with this name in the configuration", permanent=True)
        else:
            with self.output_locks.setdefault(name, threading.Lock()):     # one document at a time for each output
                try:
                    ok = bool(out.deliver(doc))
                except Exception as e:                  # an output must not stop the other outputs
                    log.exception("output %s failed", name)
                    out.last_error, out.last_permanent = str(e) or e.__class__.__name__, False
                    ok = False
                st["ok"] = ok
                if ok:
                    st.pop("error", None)
                    st.pop("permanent", None)
                    reply = getattr(out, "last_reply", None)
                    if isinstance(reply, bytes) and reply and hasattr(out, "redact"):
                        st["reply"] = out.redact(reply[:200].decode("utf-8", "replace").strip())
                else:
                    st["error"] = getattr(out, "last_error", None) or "failed"
                    st["permanent"] = bool(getattr(out, "last_permanent", False))
        with mutex:
            jd.m["outputs"][name] = st
            jd.save()

    def send(self, jd, doc, names=None):
        """Send DOC to the pending outputs of JD (or NAMES). Save the status of each output in the manifest.

        Each output has its own thread: a slow or failing output does not delay the others.
        """
        names = list(names or jd.pending())
        mutex = threading.Lock()
        if len(names) <= 1:
            for name in names:
                self._send_one(jd, doc, name, mutex)
            return
        threads = [threading.Thread(target=self._send_one, args=(jd, doc, n, mutex), name="ix1300-out-" + n,
                                    daemon=True) for n in names]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _settle(self, jd):
        """After the delivery: remove the job, or keep it in unsent (or in failed, if originals must stay).

        The same rules apply after the first delivery, a new attempt and a restart.
        """
        reason = jd.retain_reason()
        if jd.m.get("save_error"):               # CLI: the PDF file was not written
            jd.m["error"] = "; ".join(r for r in (jd.m["save_error"], reason) if r)
            self._settle_failed(jd)
            return
        if (jd.m["outputs"] or jd.m.get("saved")) and not jd.pending():
            if reason:
                jd.m["state"] = "sent with missing pages" if jd.m.get("missing") else "sent, originals kept"
                jd.m["error"] = reason
                self._settle_failed(jd)
            else:
                jd.remove()
            return
        if not jd.m["outputs"]:
            jd.m["error"] = "no outputs in the configuration"
        if not reason:
            jd.remove_sheets()                  # the PDF has the pages
        jd.m["state"] = "unsent"
        jd.save()
        path = jd.move(UNSENT)
        log.warning("PDF kept in %s (outputs not done: %s). The service tries again; or use \"ix1300 retry\"",
                    path, ", ".join(jd.pending()) or "none")

    def _settle_failed(self, jd):
        if not str(jd.m.get("state", "")).startswith("sent"):
            jd.m["state"] = "failed"
        jd.save()
        path = jd.move(FAILED)
        log.error("job %s kept in %s (%s). Use \"ix1300 retry --failed\" to make the PDF again",
                  jd.id, path, jd.m.get("error") or jd.m["state"])

    def document_for(self, jd):
        m = jd.m["document"]
        doc = Document(m["filename"], m["title"], m["profile"], m["pages"], m["receipt"], m["created"],
                       m.get("warnings") or [], path=jd.pdf_path())
        doc.job_id = jd.id
        return doc

    def retry_unsent(self, force=False):
        """Send the jobs in work_dir/unsent again (only the outputs that failed).

        Without FORCE: only jobs that are due (backoff), not after retry_max_attempts,
        and not the outputs with a permanent error (for example HTTP 401).
        Return (number of jobs sent, number of jobs that are still unsent).
        """
        sv = self.cfg.service
        done = left = 0
        for jd in list_jobs(self.work_dir, UNSENT):
            if self.stop_event.is_set():
                break
            if not jd.claim():                   # another owner (service or "ix1300 retry") has the job
                left += 1
                continue
            try:
                if not jd.reload():              # read again under the lock: the other owner can have sent it
                    continue
                r = self._retry_one(jd, force)
            finally:
                jd.unclaim()
            done += r
            left += 1 - r
        return done, left

    def _retry_one(self, jd, force):
        """One attempt for the pending outputs of JD (the caller has the job lock). Return 1 if all are done."""
        sv = self.cfg.service
        attempts = int(jd.m.get("attempts", 0))
        names = jd.pending() if force else [n for n in jd.pending() if not jd.m["outputs"][n].get("permanent")]
        if jd.pending():
            if not force and (not names or attempts >= int(sv["retry_max_attempts"]) or
                              self.clock() < self.retry_next.get(jd.id, 0)):
                return 0
            if not os.path.exists(jd.pdf_path()) or not jd.m.get("document"):
                log.warning("%s: no PDF: use \"ix1300 retry --failed\"", jd.path)
                return 0
            jd.m["attempts"] = attempts + 1
            log.info("job %s: new attempt %d for %s", jd.id, attempts + 1, ", ".join(names))
            self.send(jd, self.document_for(jd), names)
        if jd.m["outputs"] and not jd.pending():
            log.info("job %s: all outputs done", jd.id)
            self.retry_next.pop(jd.id, None)
            self._settle(jd)                     # the same rules as after the first delivery
            return 1
        wait = min(float(sv["retry_max_interval"]), float(sv["retry_interval"]) * 2 ** attempts)
        self.retry_next[jd.id] = self.clock() + wait
        return 0

    def recover(self, kinds=(JOBS,)):
        """Continue the jobs that a stop or a crash left in work_dir/jobs (or in KINDS).

        Each job is claimed (job lock) first and its manifest is read again. The
        side files that the manifest does not know are added (JobDir.reconcile).
        A job with a PDF goes to unsent (its originals stay if some must stay).
        A job with sheets gets a new PDF from its sheets; it keeps the lock until
        process() is done. Return the number of jobs.
        """
        n = 0
        for kind in kinds:
            for jd in list_jobs(self.work_dir, kind):
                if not jd.claim():
                    continue
                queued = False
                try:
                    if not jd.reload():
                        continue
                    jd.reconcile()
                    if jd.m.get("state") == "delivering" and os.path.exists(jd.pdf_path()) and jd.m.get("document"):
                        log.warning("job %s: the delivery was not complete", jd.id)
                        self._settle(jd)
                        n += 1
                        continue
                    if not jd.m.get("sheets"):
                        reason = jd.retain_reason()
                        if reason:
                            jd.m["error"] = "no complete sheet: " + reason
                            self._settle_failed(jd)
                        else:
                            jd.remove()
                        continue
                    self._queue(self.job_from_dir(jd, kind))
                    queued = True
                    n += 1
                finally:
                    if not queued:
                        jd.unclaim()
        if n:
            log.warning("%d jobs from an earlier run: work continues", n)
        return n

    def job_from_dir(self, jd, kind=JOBS):
        """A Job with the pages from the sheet files of JD."""
        try:
            profile = self.cfg.profile(jd.m.get("profile"))
        except ConfigError:
            profile = self.cfg.profile(self.cfg.service["short_press_profile"])
        job = Job(self, profile, jd)
        complete = jd.m.get("state") != "scanning"
        job.warnings = list(jd.m.get("warnings") or [])
        if kind == JOBS:
            job.warnings.append("recovered after a service restart")
            if not complete:
                job.warnings.append("the scan was not complete (the service stopped during the scan)")
        sv = self.cfg.service
        job.deliver = bool(jd.m.get("deliver")) if complete else bool(sv.get("deliver_partial", True))
        if kind == FAILED:
            job.deliver = True                   # the user asks for the document
            jd.m.pop("recovered", None)          # the user examined the found sheets: normal sheets now
        jd.m["error"] = None
        for k in ("saved", "save_error"):        # CLI values of the earlier run
            jd.m.pop(k, None)
        for sides in jd.iter_sides():
            job.sheets += 1
            job.submit(sides)
        return job


class Job:
    """One button press: the sheets on disk and the prepared pages."""

    def __init__(self, svc, profile, jobdir=None):
        self.svc = svc
        self.profile = profile
        self.sheets = 0
        self.futures = []                 # (sheet, side, file, dpi, future)
        self.failures = []                # texts: pages that are not in the document
        self.warnings = []
        self.deliver = False
        self.dir = jobdir or JobDir.create(svc.work_dir, profile.name)
        self.dir.claim()                  # this service owns the job until process() is done
        self.store = svc.pipeline.store_for(self.dir.path, svc.budget)

    def add_sheet(self, sides):
        """Called by the scan for each complete sheet. Write it to disk, then queue its pages.

        If the write fails (for example a full disk), the error stops the scan. The side
        files that are on disk stay; process() finds them (JobDir.reconcile).
        """
        self.dir.add_sheet(sides)         # first on disk (fsync): an error later cannot lose it
        self.sheets += 1
        self.submit(sides)

    def submit(self, sides):
        by_name = {s.side: s for s in sides}
        for name in side_order(self.profile):
            s = by_name.get(name)
            if s is None:
                continue
            try:
                page = Page.from_side(s, self.store)
            except Exception as e:
                msg = "sheet %d %s side: not usable (%s)" % (s.sheet + 1, name, e)
                log.error(msg)
                self.failures.append(msg)
                continue
            fut = self.svc.executor.submit(self.svc.pipeline.prepare, page, self.profile, self.store)
            self.futures.append((s.sheet, name, getattr(s, "path", None), s.dpi, fut))

    def end_scan(self, deliver):
        self.deliver = deliver
        self.dir.m.update(state="scanned", deliver=deliver, warnings=self.warnings)
        try:
            self.dir.save()
        except OSError as e:
            log.error("job %s: cannot write the manifest: %s", self.dir.id, e)

    def pages(self):
        """The prepared pages. If the work on a page failed, use the page as scanned."""
        out = []
        for sheet, side, path, dpi, f in self.futures:
            try:
                out.append(f.result())
                continue
            except Exception as e:
                msg = "sheet %d %s side: processing failed (%s)" % (sheet + 1, side, str(e) or e.__class__.__name__)
                log.error(msg, exc_info=True)
            try:
                with open(path, "rb") as fh:
                    page = Page.from_jpeg(fh.read(), dpi, sheet, side, self.store, path)
                page.blank, page.oriented = False, True
                page.errors.append(msg + ": page kept as scanned")
                out.append(page)
            except Exception:
                self.failures.append(msg)
        self.futures = []
        return out
