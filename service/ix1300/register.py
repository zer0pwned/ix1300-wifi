"""Host ID registration over Wi-Fi [NV].

The scanner keeps one registered host ID (8 bytes). A normal reserve (mode 0)
with another ID gets -6. ScanSnap Home registers its ID over Wi-Fi with this
sequence (re/hostid-registration.md 3.2). It is not verified on the scanner:

  1. Read the registered ID: GET HW STATUS (C2, 0x30 bytes) at 0x20..0x27.
     C2 works without a reserve.
  2. Reserve with mode 2 ("Wi-Fi tool") and the NEW host ID.
  3. Port1 DC op 3 (clear), only if the scanner has an ID (as ScanSnap Home).
  4. Port1 DC op 0 (register): BE64 host ID + 8 zero bytes.
  5. Release.
  6. Verify: C2 again, then a normal reserve (mode 0) and a release.

CDB of DC (HostIDOperation): DC 00 OP 00 00 WL 00 RL (WL = data-out length,
RL = data-in length). Data-in byte 0 is the result: OK if (byte 0 & 0x7F) == 0.
Each step runs one time. There is no retry loop.
"""
import logging

from .protocol import IxError, ReserveError

log = logging.getLogger("ix1300.register")

MODE_SCAN = 0
MODE_TOOL = 2                    # the "Wi-Fi tool" reserve of ScanSnap Home

OP_REGISTER = 0
OP_GET_DATA = 1
OP_CLEAR = 3
OP_GET_LEN = 4

CAUTION = ("Caution: the scanner keeps ONE registered host ID. This replaces the ID of any other computer. "
           "ScanSnap Home on the other computer stops to work with the scanner until it registers again "
           "(it does that at its next connection, and then this host gets -6 again).")


class RegisterError(IxError):
    """The registration failed."""


def parse_host_id(text):
    """16 hex digits (spaces are ignored) -> 8 bytes. Raise ValueError."""
    t = str(text or "").replace(" ", "")
    try:
        b = bytes.fromhex(t)
    except ValueError:
        b = b""
    if len(b) != 8:
        raise ValueError("host ID must be 16 hex digits, not %r" % text)
    return b


def fmt(host_id):
    return "none" if host_id is None else host_id.hex().upper()


def dc_cdb(op, outlen, inlen):
    return bytes([0xDC, 0, op, 0, 0, outlen, 0, inlen])


def hostid_op(scanner, op, data=b"", inlen=2):
    """Send one DC command. Return the data-in bytes. Raise RegisterError for a bad SCSI status."""
    scsi, reply = scanner.cmd(dc_cdb(op, len(data), inlen), data, inlen)
    if scsi != 0:
        if scsi == 2:
            try:
                scanner.request_sense()          # clear the CHECK CONDITION
            except (OSError, IxError):
                pass
        raise RegisterError("DC op %d: SCSI status %d" % (op, scsi))
    return reply


def check_result(op, reply):
    if not reply:
        raise RegisterError("DC op %d: empty reply" % op)
    if reply[0] & 0x7F:
        raise RegisterError("DC op %d: result 0x%02X (reply %s)" % (op, reply[0], reply.hex(" ")))


def dc_data_id(data):
    """Get the host ID from the DC op 1 data, or None.

    [HW] The data is 24 bytes: byte 0 = 01, then 7 zero bytes, the ID at
    bytes 8..15, and 8 zero bytes. Op 4 gives 00 18 00 00 (the data length).
    """
    if len(data) < 16 or data[0] != 1:
        return None
    return bytes(data[8:16])


def registered_id(scanner):
    """The registered host ID (8 bytes) from C2, or None if no ID is registered."""
    try:
        hid = scanner.hw_status(0x30).host_id
    finally:
        scanner.close()                          # the next commands use a new Port1 connection
    if hid == "":
        raise RegisterError("C2 reply too short: the host ID is not in the reply")
    return None if hid is None else bytes.fromhex(hid)


def _release(scanner, say):
    scanner.close()
    try:
        res = scanner.release()
        say("release: result %d" % res)
    except (OSError, IxError) as e:
        say("release failed: %s" % e)


def show(scanner, dev=None, read_dc=True, say=log.info):
    """Show the registered host ID. Change nothing. Return the ID from C2 (or None).

    DEV: a DeviceInfo (discovery reply +124: host ID of the last owner).
    READ_DC: also try a mode-2 reserve with scanner.host_id and read DC op 4 and op 1.
    """
    if dev is not None:
        say("discovery: last owner %s, host ID %s" % (dev.owner_ip, dev.owner_id))
    reg = registered_id(scanner)
    say("registered host ID (C2): %s" % fmt(reg))
    if not read_dc:
        return reg
    try:
        scanner.reserve(MODE_TOOL)
    except ReserveError as e:
        say("mode-2 reserve: %s. DC op 4 and op 1 not read" % e)
        return reg
    try:
        say("DC op 4 (length): %s" % hostid_op(scanner, OP_GET_LEN, inlen=4).hex(" "))
        data = hostid_op(scanner, OP_GET_DATA, bytes(2), inlen=0x18)
        say("DC op 1 (data): %s" % data.hex(" "))
        say("registered host ID (DC op 1): %s" % fmt(dc_data_id(data)))
    except (OSError, IxError) as e:
        say("DC read failed: %s" % e)
    finally:
        _release(scanner, say)
    return reg


def register(scanner, host_id, say=log.info, verify_reserve=True):
    """Register HOST_ID (8 bytes) over Wi-Fi. Return the old ID (or None). Raise RegisterError.

    The caller must not have a reserve. With VERIFY_RESERVE, a normal reserve and a
    release follow the C2 check (the daemon does its own reserve).
    """
    if len(host_id) != 8:
        raise ValueError("host ID needs 8 bytes")
    scanner.host_id = host_id
    old = registered_id(scanner)
    say("step 1: registered host ID now: %s" % fmt(old))
    if old == host_id:
        say("the scanner has this host ID already: no change")
        return old
    say("step 2: reserve with mode 2 and host ID %s" % fmt(host_id))
    try:
        scanner.reserve(MODE_TOOL)
    except ReserveError as e:
        raise RegisterError("the scanner refused the mode-2 reserve: %s. Register over USB "
                            "(usb-register-host)" % e)
    cleared = False
    try:
        if old is not None:
            say("step 3: clear the old host ID %s (DC op 3)" % fmt(old))
            check_result(OP_CLEAR, hostid_op(scanner, OP_CLEAR))
            cleared = True
        else:
            say("step 3: no host ID registered: no clear")
        say("step 4: register host ID %s (DC op 0)" % fmt(host_id))
        try:
            check_result(OP_REGISTER, hostid_op(scanner, OP_REGISTER, host_id + bytes(8)))
        except (OSError, IxError) as e:
            if cleared:
                raise RegisterError("%s. Caution: the old host ID was cleared; the scanner can have no "
                                    "registered ID now" % e)
            raise
    finally:
        say("step 5: release")
        _release(scanner, say)
    new = registered_id(scanner)
    say("step 6: verify: registered host ID after: %s" % fmt(new))
    if new != host_id:
        raise RegisterError("verify failed: the scanner reports %s, not %s" % (fmt(new), fmt(host_id)))
    if verify_reserve:
        say("step 7: verify: normal reserve (mode 0)")
        try:
            scanner.reserve(MODE_SCAN)
        except ReserveError as e:
            raise RegisterError("verify failed: %s" % e)
        _release(scanner, say)
        say("verify OK")
    return old
