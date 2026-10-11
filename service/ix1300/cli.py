"""Command line: ix1300 discover | status | scan | settings | serve | retry | selftest | usb-register-host |
wifi-register-host | usb-wifi-setup."""
import argparse
import getpass
import logging
import os
import sys
import threading
import time

from . import __version__
from . import config as config_mod
from . import outputs as outputs_mod
from . import settings as settings_mod
from . import trace as trace_mod
from .daemon import Job, Service, host_address, locate, make_scanner, occupied_text, short_error
from .jobs import FAILED, UNSENT, list_jobs
from .pipeline import have_pillow, tesseract_path, write_file
from .protocol import IxError, NoPaper, PaperLost, ReserveError, discover

log = logging.getLogger("ix1300")


def _load(args):
    return config_mod.load(args.config)


def cmd_discover(args):
    cfg = _load(args)
    host_ip, host_mac = host_address(cfg)
    sc = cfg.scanner
    devs = discover(host_ip, host_mac, target=args.ip or None, port=int(sc["search_port"]),
                    timeout=args.timeout, broadcast=sc.get("broadcast") or "255.255.255.255",
                    unicast=sc.get("ip") or None)
    if not devs:
        print("no scanner found")
        return 1
    for d in devs:
        print("%-15s %-24s %s serial %s mac %s ports %d/%d owner %s %s" % (
            d.ip, d.name, d.product, d.serial, d.mac, d.port1, d.port2,
            d.owner_id, "(in use)" if d.owner_ip != "0.0.0.0" else ""))
    return 0


def cmd_status(args):
    cfg = _load(args)
    host_ip, host_mac = host_address(cfg)
    print("host        %s %s" % (host_ip, host_mac.hex(":")))
    print("host ID     %s" % (cfg.scanner["host_id"] or "(not set)"))
    print("Pillow      %s" % ("yes" if have_pillow() else "no"))
    print("tesseract   %s" % (tesseract_path() or "no"))
    dev = locate(cfg, host_ip, host_mac)
    if dev is None:
        print("scanner     not found")
        return 1
    print("scanner     %s %s (%s) serial %s" % (dev.ip, dev.name, dev.product, dev.serial))
    print("ports       %d/%d%s" % (dev.port1, dev.port2, " TLS" if dev.tls else ""))
    print("last owner  %s %s" % (dev.owner_id, dev.owner_ip))
    sc = make_scanner(cfg, host_ip, host_mac, dev)
    try:
        inq = sc.inquiry()
        print("inquiry     %s" % inq[8:36].decode("ascii", "replace"))
        st = sc.hw_status()
        reg = st.host_id
        print("registered  %s%s" % (reg or "none", "" if reg is None or reg == cfg.scanner["host_id"]
                                    else "  <- not our host ID: reserve gives -6"))
        print("paper       %s" % {"top": "paper in the hopper", "front": "paper in the front slot"}.get(
            st.paper_path, "no paper in the hopper or the front slot"))
    except IxError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    finally:
        sc.close()
    return 0


def cmd_scan(args):
    """Scan one time. Exit codes: 0 OK, 2 reserve, 3 no sheet, 4 output failed, 5 incomplete scan
    (the PDF has the complete sheets before the error).

    The scan uses the job directories of the service (work_dir, jobs.JobDir): the
    sheets go to disk first. After a failure, the sheets stay in work_dir/failed
    (or the PDF in work_dir/unsent); "ix1300 retry" continues the job.
    """
    cfg = _load(args)
    prof = cfg.profile(args.profile or cfg.service["short_press_profile"])
    host_ip, host_mac = host_address(cfg)
    trace = trace_mod.open_trace(cfg, "scan")
    try:
        dev = locate(cfg, host_ip, host_mac, trace=trace)
        sc = make_scanner(cfg, host_ip, host_mac, dev, trace=trace)
        return _scan(cfg, args, prof, sc)
    finally:
        if trace is not None:
            trace.close()
            print("protocol trace: %s" % trace.path, file=sys.stderr)


def _scan(cfg, args, prof, sc):
    try:
        sc.reserve()
    except ReserveError as e:
        print("error: %s" % (occupied_text(sc) if e.result == -4 else e), file=sys.stderr)
        return 2
    settings_mod.apply_power_off(sc, cfg.scanner.get("power_off_minutes"))
    svc = job = None
    result, error = None, None
    try:                                          # from here: always release the scanner
        svc = Service(cfg, outputs=outputs_mod.build(cfg.outputs) if args.send else {})
        try:
            job = Job(svc, prof)
        except OSError as e:
            print("error: work_dir %s is not usable: %s (set service.work_dir)" % (svc.work_dir, e),
                  file=sys.stderr)
            return 1
        print("job %s in %s" % (job.dir.id, job.dir.path))
        if args.raw:
            os.makedirs(args.raw, exist_ok=True)

        def on_sheet(sides):
            job.add_sheet(sides)                  # first on disk (fsync)
            for s in sides:
                if args.raw:
                    write_file(os.path.join(args.raw, "sheet%02d_%s.jpg" % (s.sheet + 1, s.side)), [s.jpeg])

        try:
            path = "top"
            try:
                path = sc.hw_status().paper_path or "top"     # no paper: E0 decides (exit code 3)
            except (OSError, IxError) as e:
                log.debug("paper check: %s", e)
            if path == "front":
                print("front slot: one sheet")
                job.dir.m["paper_path"] = "front"
            settings = prof.scan_settings(path)
            if settings.long_page:
                print("long page mode (up to 3,000 mm)")
                job.dir.m["long_page"] = True
            result = sc.scan(settings, on_sheet=on_sheet)
        except Exception as e:                    # also a failed side file: the job keeps what is on disk
            result, error = getattr(e, "result", None), e
            if not isinstance(e, (IxError, OSError)):
                log.exception("scan failed")
    finally:
        sc.close()
        try:
            sc.release()
        except Exception as e:
            log.warning("release failed: %s", e)
        if job is None and svc is not None:
            svc.executor.shutdown(wait=True)
    if job is None:
        return 1
    try:
        return _finish_scan(cfg, svc, job, result, error, args)
    finally:
        svc.executor.shutdown(wait=True)


def _finish_scan(cfg, svc, job, result, error, args):
    """Make the PDF of a CLI scan with the service rules (Service.process). Return the exit code."""
    normal = error is None or (isinstance(error, NoPaper) and not isinstance(error, PaperLost))
    partial = bool(cfg.service.get("deliver_partial", True))
    if result is not None:
        job.warnings.extend(w for w in result.warnings if w not in job.warnings)
    if error is not None and not (normal and job.sheets):
        print("error: %s" % short_error(error), file=sys.stderr)
    if not normal and job.sheets:
        job.warnings.append("scan stopped after %d sheets: %s" % (job.sheets, short_error(error)))
        print("deliver_partial = false: no PDF" if not partial else
              "only the %d complete sheets go into the PDF" % job.sheets, file=sys.stderr)
    for w in job.warnings:
        print("warning: %s" % w, file=sys.stderr)
    job.end_scan(job.sheets > 0 and (normal or partial))
    doc = svc.process(job, save_to=args.out or "", send=args.send)
    jd = job.dir
    kept = os.path.exists(jd.path)
    if doc is not None and jd.m.get("saved"):
        print("%s: %d pages%s" % (jd.m["saved"], doc.pages, ", receipt" if doc.receipt else ""))
    for name, st in jd.m.get("outputs", {}).items():
        print("output %s: %s" % (name, "OK" if st.get("ok") else "FAILED"))
    if kept:
        print("job kept in %s (%s). Use \"ix1300 retry\" (or \"ix1300 retry --failed\")"
              % (jd.path, jd.m.get("error") or jd.m.get("state")), file=sys.stderr)
    if doc is None:
        if not job.sheets and not jd.m.get("sheets"):
            if error is None:
                print("error: no sheet", file=sys.stderr)
            return 3
        if normal and not kept:
            print("error: no page left after blank page removal", file=sys.stderr)
            return 3
        return 5 if not normal else 4
    if jd.m.get("save_error") or any(not st.get("ok") for st in jd.m.get("outputs", {}).values()):
        return 4
    return 0 if normal and not job.failures else 5


def cmd_settings(args):
    """Read (--show) or set (--power-off MINUTES) the auto power-off time of the scanner [NV].
    Exit codes: 0 OK, 1 scanner error, 2 bad value or no reserve."""
    cfg = _load(args)
    minutes = None
    if args.power_off is not None:
        try:
            minutes = settings_mod.parse_power_off(args.power_off, "--power-off")
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
    host_ip, host_mac = host_address(cfg)
    dev = locate(cfg, host_ip, host_mac)
    sc = make_scanner(cfg, host_ip, host_mac, dev)
    try:
        sc.reserve()
    except ReserveError as e:
        print("error: %s" % (occupied_text(sc) if e.result == -4 else e), file=sys.stderr)
        return 2
    try:
        if minutes is None:
            print("auto power-off  %s" % settings_mod.describe_power_off(*settings_mod.read_power_off(sc)))
        else:
            r = settings_mod.sync_power_off(sc, minutes)
            print("auto power-off  %s%s" % (settings_mod.describe_power_off(*r.after),
                                            "" if r.written else " (no change)"))
    except (OSError, IxError) as e:
        print("error: %s" % short_error(e), file=sys.stderr)
        return 1
    finally:
        sc.close()
        try:
            sc.release()
        except Exception as e:
            log.warning("release failed: %s", e)
    return 0


def cmd_selftest(args):
    """Guided hardware self-test. Exit codes: 0 all PASS or SKIPPED, 1 a FAIL or an error,
    2 stopped by the user, 3 the scanner is in use by another host."""
    from . import selftest
    cfg = _load(args)
    if cfg.host_id is None:
        print("error: scanner.host_id is not set", file=sys.stderr)
        return 2
    if not sys.stdin.isatty() and not args.no_tty_check:
        print("error: the self-test needs a terminal (docker compose run --rm ix1300 selftest)", file=sys.stderr)
        return 2
    only = [s.strip() for s in (args.steps or "").split(",") if s.strip()] or None
    try:
        st = selftest.Selftest(cfg, selftest.TerminalUI(), only=only, button_timeout=args.button_timeout,
                               install_signals=True, quiet_console=not args.verbose)
    except ValueError as e:
        print("error: %s" % e, file=sys.stderr)
        return 2
    return st.run()


def cmd_serve(args):
    cfg = _load(args)
    if cfg.host_id is None:
        print("error: scanner.host_id is not set", file=sys.stderr)
        return 2
    return exit_after_stop(Service(cfg).run_forever())


def exit_after_stop(code):
    """Exit at once if threads of the stopped service still run. Else return CODE.

    At its exit, Python waits for the page threads (ThreadPoolExecutor), and a
    page task can run for tesseract_timeout or longer. Service.stop() keeps to
    stop_timeout and leaves each job on disk (manifest with fsync), and the next
    start continues it. Thus os._exit() loses no job. In the container, this
    process has PID 1: its exit also stops the tesseract and djpeg processes.
    """
    end = time.monotonic() + 1.0                  # idle page threads end in a short time
    busy = []
    for t in threading.enumerate():
        if t is not threading.current_thread() and not t.daemon:
            t.join(max(0.0, end - time.monotonic()))
            if t.is_alive():
                busy.append(t.name)
    if not busy:
        return code
    log.warning("threads still run after the stop (%s): the next start continues their jobs", ", ".join(busy))
    logging.shutdown()
    for fh in (sys.stdout, sys.stderr):
        try:
            fh.flush()
        except (OSError, ValueError):
            pass
    os._exit(code)


def cmd_retry(args):
    """Send the jobs in work_dir/unsent again. With --failed, first make the PDF again from the
    sheets in work_dir/failed. --list only shows the jobs."""
    cfg = _load(args)
    work = cfg.service["work_dir"]
    if args.list:
        for kind in (UNSENT, FAILED):
            for jd in list_jobs(work, kind):
                outs = ", ".join("%s %s" % (n, "OK" if st.get("ok") else "FAILED (%s)" % st.get("error"))
                                 for n, st in jd.m.get("outputs", {}).items())
                print("%-7s %s %s sheets %d %s%s" % (kind, jd.id, jd.m.get("profile"), len(jd.m.get("sheets", [])),
                                                    outs, "  error: %s" % jd.m["error"] if jd.m.get("error") else ""))
        return 0
    svc = Service(cfg)
    try:
        if args.failed:
            n = svc.recover(kinds=(FAILED,))
            print("%d failed jobs: PDF made again" % n)
            while not svc.jobs.empty():
                svc.process(svc.jobs.get())
                svc.backlog -= 1
        done, left = svc.retry_unsent(force=True)
    finally:
        svc.executor.shutdown(wait=True)
    print("%d jobs sent, %d jobs still unsent" % (done, left))
    return 0 if left == 0 else 4


def cmd_usb_register(args):
    from . import usb
    if args.show:                        # read only: no host ID and no configuration needed
        try:
            with usb.UsbScanner() as dev:
                print("registered host ID: %s" % dev.host_id().hex().upper())
        except usb.UsbError as e:
            print("error: %s" % e, file=sys.stderr)
            return 1
        return 0
    hid = args.host_id
    if not hid:
        hid = _load(args).scanner["host_id"]
    if not hid:
        print("error: give --host-id or set scanner.host_id", file=sys.stderr)
        return 2
    try:
        usb.register_host_id(bytes.fromhex(hid))
    except usb.UsbError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("OK. Use host_id = \"%s\" in the configuration." % hid.upper())
    return 0


def _confirm(prompt):
    """Ask the user on a terminal. Without a terminal: no."""
    if not sys.stdin.isatty():
        return False
    print(prompt, end="", flush=True)
    answer = sys.stdin.readline()
    return answer.strip().lower() in ("y", "yes")


def cmd_wifi_register(args):
    """Register a host ID over Wi-Fi [NV]. Exit codes: 0 OK, 1 error, 2 refused or bad input."""
    from . import register
    cfg = _load(args)
    text = args.host_id or cfg.scanner["host_id"]
    hid = None
    if text:
        try:
            hid = register.parse_host_id(text)
        except ValueError as e:
            print("error: %s" % e, file=sys.stderr)
            return 2
    elif not args.show:
        print("error: give --host-id or set scanner.host_id", file=sys.stderr)
        return 2
    if not args.show:
        print(register.CAUTION, file=sys.stderr)
        if not args.yes and not _confirm("Register host ID %s on the scanner? Type yes: " % hid.hex().upper()):
            print("not registered: give --yes, or type yes at the prompt", file=sys.stderr)
            return 2
    host_ip, host_mac = host_address(cfg)
    dev = locate(cfg, host_ip, host_mac)
    if dev is None:
        print("error: no scanner found", file=sys.stderr)
        return 1
    sc = make_scanner(cfg, host_ip, host_mac, dev, host_id=hid or bytes(8))

    def say(msg):
        print(msg, flush=True)
        log.debug("wifi-register-host: %s", msg)
    try:
        if args.show:
            register.show(sc, dev, read_dc=hid is not None, say=say)
            return 0
        register.register(sc, hid, say=say)
    except (OSError, IxError) as e:
        print("error: %s" % short_error(e), file=sys.stderr)
        return 1
    finally:
        sc.close()
    print("OK. Use host_id = \"%s\" in the configuration." % hid.hex().upper())
    return 0


def cmd_usb_wifi(args):
    from . import usb
    key = None
    if args.key_file:
        with open(args.key_file) as fh:
            key = fh.read().splitlines()[0]
    else:
        key = os.environ.get("IX1300_WIFI_KEY") or getpass.getpass("Wi-Fi key: ")
    try:
        ips = usb.wifi_setup(args.ssid, key, args.auth, args.cipher)
    except usb.UsbError as e:
        print("error: %s" % e, file=sys.stderr)
        return 1
    print("scanner IP: %s" % (", ".join(ips) or "not known yet"))
    return 0 if ips else 1


def main(argv=None):
    p = argparse.ArgumentParser(prog="ix1300", description="ScanSnap iX1300 Wi-Fi scan service")
    p.add_argument("-c", "--config", default=os.environ.get("IX1300_CONFIG"), help="TOML or JSON file")
    p.add_argument("-v", "--verbose", action="store_true")
    p.add_argument("--version", action="version", version=__version__)
    sub = p.add_subparsers(dest="cmd")

    s = sub.add_parser("discover", help="find scanners on the network")
    s.add_argument("--ip", help="send to this address only")
    s.add_argument("--timeout", type=float, default=3.0)
    s.set_defaults(fn=cmd_discover)

    s = sub.add_parser("status", help="show the scanner state and the registered host ID")
    s.set_defaults(fn=cmd_status)

    s = sub.add_parser("scan", help="scan one time and write a PDF")
    s.add_argument("--profile")
    s.add_argument("--out", help="PDF file name")
    s.add_argument("--raw", help="also write the JPEG sides into this directory")
    s.add_argument("--send", action="store_true", help="also send the PDF to the outputs of the profile")
    s.set_defaults(fn=cmd_scan)

    s = sub.add_parser("settings", help="read or set the auto power-off time of the scanner")
    g = s.add_mutually_exclusive_group(required=True)
    g.add_argument("--show", action="store_true", help="read and show the auto power-off time")
    g.add_argument("--power-off", metavar="MINUTES",
                   help="set the auto power-off time: 0 = off, else 15..1890 in steps of 15")
    s.set_defaults(fn=cmd_settings)

    s = sub.add_parser("selftest", help="guided hardware self-test (stop the service first)")
    s.add_argument("--steps", help="only these steps, for example front_slot,stop (default: all)")
    s.add_argument("--button-timeout", type=float, default=120.0, help="seconds to wait for each press")
    s.add_argument("--no-tty-check", action="store_true", help=argparse.SUPPRESS)
    s.set_defaults(fn=cmd_selftest)

    s = sub.add_parser("serve", help="run the scan button service")
    s.set_defaults(fn=cmd_serve)

    s = sub.add_parser("retry", help="send the unsent jobs again (work_dir/unsent)")
    s.add_argument("--failed", action="store_true", help="also make the PDF again for the jobs in work_dir/failed")
    s.add_argument("--list", action="store_true", help="only show the unsent and failed jobs")
    s.set_defaults(fn=cmd_retry)

    s = sub.add_parser("usb-register-host", help="write our host ID into the scanner (USB, one time)")
    s.add_argument("--host-id", help="16 hex digits (default: scanner.host_id)")
    s.add_argument("--show", action="store_true", help="only show the registered host ID")
    s.set_defaults(fn=cmd_usb_register)

    s = sub.add_parser("wifi-register-host", help="register our host ID over Wi-Fi (not verified on the scanner)")
    s.add_argument("--host-id", help="16 hex digits (default: scanner.host_id)")
    s.add_argument("--show", action="store_true", help="only show the registered host ID (no change)")
    s.add_argument("--yes", action="store_true", help="no question: replace the ID of the other computer")
    s.set_defaults(fn=cmd_wifi_register)

    s = sub.add_parser("usb-wifi-setup", help="connect the scanner to Wi-Fi (USB, one time)")
    s.add_argument("--ssid", required=True)
    s.add_argument("--key-file", help="file with the Wi-Fi key on line 1 (else IX1300_WIFI_KEY or a prompt)")
    s.add_argument("--auth", default="WPA2", choices=["WPA2", "WPA", "MIX"])
    s.add_argument("--cipher", default="AES", choices=["AES", "TKIP", "AUTO"])
    s.set_defaults(fn=cmd_usb_wifi)

    args = p.parse_args(argv)
    level = "DEBUG" if args.verbose else None
    if level is None:
        try:
            level = config_mod.load(args.config).service.get("log_level", "INFO")
        except Exception:
            level = "INFO"
    logging.basicConfig(level=getattr(logging, str(level).upper(), logging.INFO),
                        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s")
    if not getattr(args, "fn", None):
        p.print_help()
        return 2
    try:
        return args.fn(args)
    except config_mod.ConfigError as e:
        print("config error: %s" % e, file=sys.stderr)
        return 2
