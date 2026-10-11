# SANE backend `ix1300`: ScanSnap iX1300 on Wi-Fi

This backend lets the standard SANE programs (scanimage, scanadf, scanbd,
gscan2pdf, sane-scan-pdf) scan from a PFU (Fujitsu) ScanSnap iX1300 on a
Wi-Fi network. The backend uses TCP and UDP. For USB, use the `fujitsu`
backend.

## Files

| File | Content |
|---|---|
| `ix1300.c`, `ix1300.h` | The backend source |
| `ix1300.conf.in` | The configuration file template |
| `sane-ix1300.man` | The manual page, sane-ix1300(5) |
| `ix1300.desc` | The SANE description file (`doc/descriptions/`) |
| `sane-backends-ix1300.patch` | The patch for sane-backends 1.3.1 (`git diff`) |
| `install-on-debian.sh` | The build and install script for Debian |
| `Makefile` | A standalone build (it needs a configured sane-backends tree) |
| `tests/` | The test harness: stub SANE headers, a fake scanner, and the tests (see "Tests") |

## Install on Debian

```
sudo HOSTID=0123456789ABCDEF ./install-on-debian.sh
```

The script installs the build packages, gets the sane-backends 1.3.1
source, applies the patch, and builds only the `ix1300` backend. Then it
installs these items:

- `/usr/lib/<multiarch>/sane/libsane-ix1300.so.1`
- `/etc/sane.d/ix1300.conf` (only if the file is not there)
- the `ix1300` line in `/etc/sane.d/dll.conf`
- `/usr/share/man/man5/sane-ix1300.5`

## Configuration

Edit `/etc/sane.d/ix1300.conf`:

```
net auto                  # find scanners with a UDP broadcast
#net 192.0.2.212       # or give the address of the scanner
hostid 0123456789ABCDEF   # the host ID that the scanner has registered
```

The scanner keeps one registered host ID. If the ID is not correct, the
scanner refuses the reserve (result -6), and `scanimage` shows "Access to
resource has been denied". ScanSnap Home on a different computer can
register its own ID. Then you must register your ID again over USB.

## Use

```
scanimage -L
scanimage -d ix1300:192.0.2.212 -A
scanimage -d ix1300:192.0.2.212 --source "ADF Back" --mode Gray \
          --resolution 300 --format=png -o page.png
scanimage -d ix1300:192.0.2.212 --source "ADF Duplex" --batch=p%03d.pnm
scanimage -d ix1300:192.0.2.212 --source "ADF Duplex" --batch=p%03d.pnm \
          --batch-count=4
scanimage -d ix1300:192.0.2.212 --source "Card Duplex" --batch=card%d.pnm
```

Load only the sheets that you want to scan. The scanner feeds the whole
stack in one job (see "Paper feed").

### Which side is the front?

The backend uses the side names of the scanner. Window 0 is "front".
Usually you load the paper with the printed side down. Then the print is
on the **back** side. For one-sided documents, use `--source "ADF Back"`.
With `ADF Duplex`, the backend gives two frames for each sheet: first the
front, then the back.

### Front slot (Card sources)

The `Card Front`, `Card Back` and `Card Duplex` sources scan from the
front slot (return path). The sides are the same as for the ADF sources
(window 0 is the front). The names are the same as in the fujitsu
backend.

- Put **one sheet** into the front slot. The slot takes one sheet at a
  time, and the job ends after that sheet. The sheet comes back out at
  the front.
- **No long paper.** The front slot does not take paper longer than an
  A4 sheet: a long receipt jams.
- With a Card source, `sane_start` reads C2 first. With no sheet in the
  front slot, it gives SANE_STATUS_NO_DOCS and starts no job.
- With an ADF source and a sheet only in the front slot, `sane_start`
  also gives SANE_STATUS_NO_DOCS. The debug output (`SANE_DEBUG_IX1300=1`)
  then shows "paper is in the front slot; select a Card source".
- `page-loaded` follows the source: the chute for ADF, the front slot
  for Card.

### Options

| Option | Values | Note |
|---|---|---|
| `--source` | ADF Front, ADF Back, ADF Duplex, Card Front, Card Back, Card Duplex | See above. Card: the front slot, one sheet |
| `--mode` | Color, Gray, Lineart | Lineart is a software threshold |
| `--resolution` | 150, 200, 300, 600 | |
| `--page-width`, `--page-height` | mm | The maximum height gives the automatic length (895.5 mm) |
| `-l -t -x -y` | mm | The backend cuts the area from the image |
| `--threshold` | 0..255 | Lineart only |
| `--ald` | yes (default), no | Removes the empty area after the paper |
| `--halt-on-cancel` | yes, no (default) | Cancel: stop the paper in the paper path, not feed it out (see below) |
| `--compression` | None (default), JPEG | JPEG gives SANE_FRAME_JPEG; most frontends cannot use it |
| `--scan`, `--long-press`, `--page-loaded` | read only | Sensors for scanbd |

With `--ald=yes`, the backend does not know the height before
`sane_start`. Then the parameters show lines = -1. After `sane_start`, the
height is correct, so `--format=png` also works.

### Paper feed

The scanner feeds **all** sheets in the chute in one job. It does not wait
for the host, and there is no clean stop after a number of sheets
(PROTOCOL.md 8.6). So:

- **Load only the sheets that you want to scan.**
- To scan the stack, use `--batch` and let the backend read until the
  chute is empty. This is the normal end: E0 gives "no paper" (sense
  3/80/03), and the backend sends END JOB (D6) only.
- The frontend sets the page limit, not the backend. Use
  `scanimage --batch-count=N`. With duplex, each sheet gives 2 pages.
  When the frontend stops before the chute is empty (`--batch-count`, or
  `scanimage` without `--batch`), it calls `sane_cancel`. The cancel has
  the result below.

Cancel with sheets left in the job (`sane_cancel`, `sane_close`, or
Ctrl-C during a job):

| `--halt-on-cancel` | Commands | Result on the iX1300 |
|---|---|---|
| no (default) | CANCEL READ (D8), E0, wait, END JOB (D6) | **The scanner feeds out the rest of the stack without a scan.** The backend waits until C2 shows an empty chute and a clear paper path (at most 120 s), then sends D6. The paper path is then empty |
| yes | D8, D6 | The motor stops at once. D6 does not eject paper. A sheet that the scanner already pulled in can stop in the paper path (5-sheet test: near the exit, and the next sheet at the pick), or it can still come out (2-sheet test). The rest of the stack stays in the chute. If paper stays in the path, open the ADF top section and remove it before the next scan |

This agrees with the `fujitsu` backend: a cancel ejects the paper in the
path, and the option `halt-on-cancel` halts the feed. After D8, a READ
gives sense 5/24/00, so the backend sends no READ. The backend ignores
the sense of E0 after D8, but it stops the wait at once for "no paper", a
paper jam, or an open cover.

After an error (for example a paper jam or a timeout), the backend sends
D8 and D6 only, because the paper path can have a jam. Remove the paper
by hand. If the scanner gives BUSY for D8 or D6, the backend sends the
command again after 0.25 s (at most 20 times): BUSY means that the
scanner did not do the command. The backend reports a failure of D8 or D6
in the debug output (level 1).

Uncertain job: if the reply of D5 or D6 is lost, or if the backend makes
a new reserve while a job was active, the backend does not know if the
scanner still has the job. The next `sane_start` then does a bounded
recovery on the new connection before the next D5:

1. D8 and D6 (no E0: it could feed out the paper of the new job). For
   this job, an error sense for D6 means "no job". If D8 and D6 fail,
   `sane_start` tries one more time, then gives the error. The job stays
   uncertain, and the next `sane_start` starts the recovery again.
2. The C2 check in the job start: if paper is at the feed sensor (ADF
   source, or paper in the chute), paper of the old job is still in the
   paper path. The backend does not send D5 (D5 would feed this paper into
   the new job) and gives `SANE_STATUS_JAMMED`. Open the ADF top section,
   remove the paper, and start again. The check stays active until C2
   shows a clear feed sensor. For a Card source with an empty chute, the
   check cannot see a difference between old paper and a sheet in the
   front slot.

The backend never sends E0, READ, D5 or D6 again after a lost reply.

### Status values

| Status | Meaning |
|---|---|
| `SANE_STATUS_NO_DOCS` | The chute is empty: E0, or the first READ of a new sheet (before image data), gave sense 3/80/03 |
| `SANE_STATUS_JAMMED` | Paper jam, double feed, or the sheet was lost: sense 3/80/03 on the back side or after image data. Also: paper of an uncertain old job is in the paper path (see above) |
| `SANE_STATUS_IO_ERROR` | No new image data for 60 s, a side that is not complete in 600 s, a side larger than 64 MiB, a bad reply, or JPEG data that is not complete or not valid |
| `SANE_STATUS_DEVICE_BUSY` | An option change during a job (call `sane_cancel` first), or a different call uses the device |
| `SANE_STATUS_CANCELLED` | `sane_cancel` stopped the scan, or `sane_read` after `sane_cancel` |

A frame never gets lines that are not in the JPEG data. If the JPEG data
of a side is not complete, `sane_read` gives the good lines and then
`SANE_STATUS_IO_ERROR`.

## How the backend works

1. `sane_get_devices` sends a 32-byte UDP probe to port 52217. It accepts
   144-byte replies with the product name "ScanSnap iX1300".
   The backend accepts a reply only from the address in the reply.
2. `sane_open` sends a reserve to Port2 (TCP 53219), connects to Port1
   (TCP 53218), and sends INQUIRY. A thread sends a keepalive every 15 s.
   If the thread does not start, `sane_open` fails.
   - Result -4: `SANE_STATUS_DEVICE_BUSY` (a different host has the scanner).
   - Result -6: `SANE_STATUS_ACCESS_DENIED` (the host ID is not registered).
3. The first `sane_start` of a job sends D8, E9, D4, C2 and D5. If C2 shows
   no paper, `sane_start` gives `SANE_STATUS_NO_DOCS`.
4. Each sheet: E0, then READ of the front and the back until the end of
   each side (sense key 0 with EOM). The backend sends the same READ again
   after "busy" and after "temporary no data" (sense 3/80/13). Sense key 0
   without EOM does not end the side. With `--ald`, the pixel size READ
   gives the real line count, and the backend writes it into the JPEG SOF
   marker.
5. The next `sane_start` sends E0 again. When E0 or the first READ of the
   new sheet gives sense 3/80/03 (no paper), `sane_start` sends D6 and
   gives `SANE_STATUS_NO_DOCS`. The backend does not use the C2 hopper bit
   after the first sheet, also not after a timeout. The scanner pulls the
   next sheets into the paper path early, so the bit shows "empty" while
   sheets are still in the scanner.
6. A cancel in a job sends D8, E0 (the scanner feeds out the rest of the
   stack), waits for a clear paper path, and sends D6. With
   `--halt-on-cancel`, it sends D8 and D6 only. `sane_cancel` only sets a
   flag; the next call does this work (see "Cancel" below). `sane_close`
   does the same, then sends the release.
7. A full reply must come in the `timeout` time (30 s). The backend sends a
   command again on a new connection only if this is safe: INQUIRY, C2,
   D8, E9 and D4. It does not send E0, READ, D5 or D6 again. If the reply
   of D5 or D6 is lost, the next `sane_start` does the recovery of an
   uncertain job (D8, D6, C2 check).

Reserve: if the scanner does not answer a keepalive (3 packets in 3 s), or
if it sends a power or release event, the next `sane_start` sends a
keepalive again. If there is no reply, the backend searches the scanner
again and makes a new reserve. This is necessary after the scanner turns
itself off (after about 4 hours) and you turn it on again. If a job was
active, the job is then uncertain, and the backend does the recovery
(D8, D6, C2 check) before the next D5.

Cancel: `sane_cancel` can come from a signal handler or a different thread.
It only sets a flag (an atomic `sig_atomic_t`). It does no network I/O, no
memory operation and no debug output, so it is safe in a signal handler.

- If `sane_start` runs, a wait for a scanner reply stops in 0.1 s (also in
  the middle of one reply). The backend closes Port1, because the reply is
  not complete. Then `sane_start` ends the job on a new connection and
  gives `SANE_STATUS_CANCELLED`.
- If `sane_read` runs, it stops after its current line, ends the job and
  gives `SANE_STATUS_CANCELLED`.
- If no call runs, the next `sane_read` (it gives `SANE_STATUS_CANCELLED`),
  `sane_start` (it then starts a new job), option change or `sane_close`
  ends the job. Until then, the scanner keeps the job. A frontend that
  calls `sane_cancel` and then stops without `sane_close` leaves the job
  open in the scanner until the reserve ends.

A cancel does not stop the end of the job itself (D8, E0, the wait, D6).

The scan button: while the device is open, the backend reads C2 byte 4
(bit 0 short press, bit 5 long press) and the UDP trigger packets that the
scanner sends to the trigger port of the reserve. The backend accepts only
48-byte trigger packets from the address of the scanner, with a known
code, and ignores a sequence number that it saw in the last 16 events.
This is a filter on the LAN, not an authentication.

Options: while a job is active, or while a side of a sheet waits for its
frame, an option change gives `SANE_STATUS_DEVICE_BUSY`. Call
`sane_cancel` first.

Debug output: `SANE_DEBUG_IX1300=5` (events and sense data), `15` (each
command), `30` (hex dumps).

## Tests

`tests/` has a test harness. It compiles `ix1300.c` with stub SANE
headers and a fake scanner (no network, no scanner, AddressSanitizer):

```
make -C tests            # the tests on ix1300.c
make -C tests test-real  # also decode scanner JPEGs through the backend
make -C tests orig       # the same tests on an older copy in tests/orig/
```

`test-real` uses the synthetic JPEGs in `../service/tests/data` and the
real scans in `../scans/raw` if that directory exists. `tests/orig/` is
not in the repository. Without it, `make orig` does nothing.

## Limits

- The backend has no TLS. It cannot use a scanner in "RED" or "SEC" mode.
- The backend does not register a host ID.
- The front slot (Card sources) takes one sheet at a time and no paper
  longer than an A4 sheet.
- The maximum page height is 895.5 mm. Long paper mode is not available.
- The scanner selects the color automatically. The backend converts the
  image to the mode that you selected.
- A paper jam on Wi-Fi does not always give an error. The page then ends at
  the jam point. The backend has no exit sensor check at the end of the
  job.
- There is no clean stop after N sheets. A cancel feeds out the rest of
  the stack without a scan, or (`--halt-on-cancel`) leaves the paper in
  the paper path. Load only the sheets that you want to scan.
