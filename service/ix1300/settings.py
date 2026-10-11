"""Scanner settings over Wi-Fi: the auto power-off time (E6) [NV].

Port1 CDB `E6 00 MM 00 00 DO 00 DI` with 4 data bytes `CC BB 00 00`:
MM = 0 sets the value (4 bytes out), MM = 1 gets the value (4 bytes in).
CC bits 0-6 = time in 15-minute units (15..1890 minutes), bit 7 = auto
power-off off. 0x7F is a special 3-minute value. BB is probably the warning
lead time (0x1E). The service keeps BB as the scanner gives it.

The double-feed setting is in the D4 data (protocol.ScanSettings.double_feed).
"""
import logging
from dataclasses import dataclass

from .protocol import IxError

log = logging.getLogger("ix1300")

POWER_OFF_STEP = 15
POWER_OFF_MAX = 1890                 # code 0x7E
POWER_OFF_DISABLED = 0x80            # bit 7 of CC
POWER_OFF_SPECIAL = 0x7F             # 3 minutes (a vendor test value)
POWER_OFF_DEFAULT_CODE = 0x10        # 240 minutes (factory value)


class SettingsError(IxError):
    """An unexpected value from the scanner, or a read-back that does not agree."""


def parse_power_off(value, name="scanner.power_off_minutes"):
    """Return None (do not change the scanner), 0 (off) or minutes (15..1890, steps of 15).
    Raise ValueError for other values."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    if isinstance(value, bool):
        raise ValueError("%s: minutes expected, got %r" % (name, value))
    if isinstance(value, str):
        try:
            value = int(value.strip())
        except ValueError:
            raise ValueError("%s: minutes expected, got %r" % (name, value))
    if isinstance(value, float) and value.is_integer():
        value = int(value)
    if not isinstance(value, int) or not (value == 0 or (POWER_OFF_STEP <= value <= POWER_OFF_MAX
                                                          and value % POWER_OFF_STEP == 0)):
        raise ValueError("%s: 0 (off) or %d..%d in steps of %d expected, got %r"
                         % (name, POWER_OFF_STEP, POWER_OFF_MAX, POWER_OFF_STEP, value))
    return value


def power_off_minutes(code):
    """The minutes of CODE: 0 = auto power-off off. None for a code without a time."""
    if code & POWER_OFF_DISABLED:
        return 0
    if code == POWER_OFF_SPECIAL:
        return 3
    return code * POWER_OFF_STEP if code else None


def describe_power_off(code, b1):
    m = power_off_minutes(code)
    if m == 0:
        text = "off"
    elif m is None:
        text = "unknown"
    else:
        text = "%d min" % m
    return "%s (bytes %02X %02X)" % (text, code, b1)


def power_off_code(minutes, current):
    """The code CC for MINUTES (0 = off). Off keeps the time bits of CURRENT (as ScanSnap Home)."""
    if minutes == 0:
        t = current & 0x7F
        if t in (0, POWER_OFF_SPECIAL):
            t = POWER_OFF_DEFAULT_CODE
        return POWER_OFF_DISABLED | t
    return minutes // POWER_OFF_STEP


def read_power_off(scanner):
    """E6 get. Return (CC, BB). The caller must have a reserve."""
    data = scanner._check("E6", [0xE6, 0, 1, 0, 0, 0, 0, 4], inlen=4)
    if len(data) < 2:
        raise SettingsError("E6 get: reply of %d bytes" % len(data))
    return data[0], data[1]


def write_power_off(scanner, code, b1):
    """E6 set with the data bytes CC BB 00 00. The caller must have a reserve."""
    scanner._check("E6", [0xE6, 0, 0, 0, 0, 4, 0, 0], out=bytes([code, b1, 0, 0]))


@dataclass
class PowerOffResult:
    before: tuple
    after: tuple
    written: bool


def sync_power_off(scanner, minutes):
    """Read the value, write it only if it is different, then read it back.
    Raise IxError or OSError for an error, SettingsError for an unexpected value."""
    before = read_power_off(scanner)
    log.info("auto power-off on the scanner: %s", describe_power_off(*before))
    if power_off_minutes(before[0]) is None:
        raise SettingsError("E6 get: unexpected value %s" % describe_power_off(*before))
    code = power_off_code(minutes, before[0])
    if code == before[0]:
        return PowerOffResult(before, before, False)
    write_power_off(scanner, code, before[1])
    after = read_power_off(scanner)
    if after != (code, before[1]):
        raise SettingsError("auto power-off: wrote %s, read back %s" % (describe_power_off(code, before[1]),
                                                                         describe_power_off(*after)))
    log.info("auto power-off set to %s", describe_power_off(*after))
    return PowerOffResult(before, after, True)


def apply_power_off(scanner, minutes):
    """Service hook after a reserve. MINUTES None: no scanner traffic, return None.
    Else return True if the scanner has the value. An error gives a WARNING and False:
    it never stops a scan."""
    if minutes is None:
        return None
    frame_timeout = scanner.frame_timeout
    scanner.frame_timeout = min(frame_timeout, scanner.timeout)   # a short wait: the button must work
    try:
        sync_power_off(scanner, minutes)
        return True
    except (OSError, IxError) as e:
        log.warning("auto power-off setting not applied: %s", e)
    except Exception:                       # never block the scan service
        log.warning("auto power-off setting not applied", exc_info=True)
    finally:
        scanner.frame_timeout = frame_timeout
        try:
            scanner.close()                 # the scan job opens Port1 again
        except OSError:
            pass
    return False
