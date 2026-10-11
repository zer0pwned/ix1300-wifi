# SANE backend for the ScanSnap iX1300 on Wi-Fi

This folder has a SANE backend with the name `ix1300`. With it, the normal
Linux scan programs can use a ScanSnap iX1300 over Wi-Fi. Examples of these
programs are `scanimage`, `scanadf`, `gscan2pdf`, `sane-scan-pdf` and
`scanbd`.

For a scanner on a USB cable, you do not need this backend. Use the
`fujitsu` backend that comes with SANE.

---

## Do you need the backend?

The project has two ways to use the scanner. Select the one that agrees
with how you work.

- **The Docker service** (see the [main README](../README.md)). You push the
  Scan button, and a PDF goes to Paperless-ngx, a folder, or a webhook. You
  do not use a computer screen. Most users want this.
- **This SANE backend.** You start each scan from a scan program on your
  computer, and you get image files. This is for users who already have a
  SANE workflow, or who want `gscan2pdf` or a similar program.

> **Use one at a time.** The scanner works with only one program at a time.
> Do not run the Docker service and a SANE program at the same time. Stop
> the service (`docker compose stop`) before you use the backend.

---

## Before you start

You need these items:

- A ScanSnap iX1300 that is on your Wi-Fi network.
- The host ID that the scanner knows. This is a number of 16 hex digits.
- A Debian or Raspberry Pi OS computer in the same network.

The scanner accepts commands only from a computer with the correct host ID.
The backend cannot put the scanner on Wi-Fi, and it cannot register a host
ID. Do these two steps one time with a USB cable. Step 2 of the
[main README](../README.md) shows how.

---

## Install

Go to this folder and run the install script as root. Put your host ID in
the command:

```
cd sane-backend
sudo HOSTID=0123456789ABCDEF ./install-on-debian.sh
```

The script does these steps:

1. It installs the build packages with `apt`.
2. It downloads the sane-backends 1.3.1 source into `sane-backend/sane-backends`.
3. It adds the `ix1300` backend to that source.
4. It builds only the `ix1300` backend.
5. It installs the library, the configuration file, the `dll.conf` line and
   the manual page.

The script writes `/etc/sane.d/ix1300.conf` only if that file does not exist.
It writes the host ID only into a new file. If the file already exists, add
the host ID yourself (see "Configure").

The script has two more settings. Set `NO_APT=1` if the build packages are
already on the computer. Give a folder as the first argument if you already
have a sane-backends 1.3.1 source tree.

When the script finishes, find the scanner:

```
scanimage -L
```

The output shows the device name of the scanner, for example
`ix1300:192.168.1.50`. The part after `ix1300:` is the IP address of the
scanner.

The manual page has the full reference: `man sane-ix1300`.

---

## Configure

The configuration file is `/etc/sane.d/ix1300.conf`. Usually you must
change only two lines:

```
net auto
hostid 0123456789ABCDEF
```

`net auto` finds the scanner with a broadcast on the local network. If the
broadcast does not get to the scanner, write its address instead:

```
net 192.168.1.50
```

You can write more than one `net` line. If you do not set `hostid`, the
backend uses the MAC address of the computer and two zero bytes. The
scanner usually refuses this ID.

The file has more settings. Usually you do not change them:

| Setting | Default | What it does |
|---|---|---|
| `iface` | the route to the scanner | The network interface to use, for example `wlan0` |
| `timeout` | 30 | The longest time in seconds for one reply from the scanner |
| `reserve-timeout` | 30 | The scanner becomes free this many seconds (plus 10) after the last keepalive |
| `keepalive` | 15 | The time in seconds between two keepalive packets |
| `discovery-timeout` | 3000 | The time in milliseconds that `net auto` waits for replies |
| `trigger-port` | 0 | The UDP port for Scan button packets. 0 lets the system select a port |

---

## Scan

All examples use the address 192.168.1.50. Use the address that
`scanimage -L` shows.

### Load the paper

> **Load only the sheets that you want to scan.** The scanner always feeds
> the whole stack in one job. It cannot stop after a number of sheets.

The side of the sheet that faces up in the top chute is the **front**. The
side that faces down is the **back**. Select the source that agrees with
the side that has the print:

- Print faces up: use `--source "ADF Front"` (the default).
- Print faces down: use `--source "ADF Back"`.
- Print on both sides: use `--source "ADF Duplex"`. You get two images for
  each sheet, first the front, then the back.

The backend does not turn the images. Some pages can come out upside down.

### scanimage

Scan one page into a PNG file:

```
scanimage -d ix1300:192.168.1.50 --source "ADF Front" --mode Gray \
          --resolution 300 --format=png -o page.png
```

Scan the full stack, both sides, into one file for each page:

```
scanimage -d ix1300:192.168.1.50 --source "ADF Duplex" --batch=page%03d.pnm
```

With `--batch`, the backend reads until the chute is empty. Two sheets in
duplex give four files.

Scan only the first two sheets, both sides:

```
scanimage -d ix1300:192.168.1.50 --source "ADF Duplex" --batch=page%03d.pnm \
          --batch-count=4
```

The scanner still feeds the sheets after the limit. They come out with no
scan (see "Stop a scan").

### scanadf

```
scanadf -d ix1300:192.168.1.50 --source "ADF Duplex" -o page-%04d.pnm
```

### sane-scan-pdf

`sane-scan-pdf` is a separate script that makes one PDF from a stack. It
calls the SANE programs, so it can use this backend. Give it the device
name and the duplex option:

```
scan -x ix1300:192.168.1.50 -d -o document.pdf
```

The options of `sane-scan-pdf` can change. Examine `scan --help` for your
version.

### The front slot

The front slot takes one card, photo, or thick sheet at a time. Use the
`Card` sources for it:

```
scanimage -d ix1300:192.168.1.50 --source "Card Duplex" --batch=card%d.pnm
```

- Put **one** sheet into the slot. The job ends after that sheet, and the
  sheet comes back out at the front.
- Do not put long paper into the front slot. Paper longer than an A4 sheet
  jams. Use the top chute for receipts.
- `Card Front`, `Card Back` and `Card Duplex` use the same sides as the
  `ADF` sources.
- If the front slot is empty, the scan stops with the message "Document
  feeder out of documents".

If a sheet is in the front slot but you select an `ADF` source, you also
get "out of documents". Select a `Card` source.

---

## The options

`scanimage -d ix1300:192.168.1.50 -A` shows all options and their values.
This list explains them.

**`--source`**: `ADF Front` (default), `ADF Back`, `ADF Duplex`,
`Card Front`, `Card Back` or `Card Duplex`. ADF is the top chute. Card is
the front slot.

**`--mode`**: `Color` (default), `Gray` or `Lineart`. The scanner selects
the color itself. The backend changes the image to the mode that you select.
For `Lineart`, the backend makes black and white from the gray image.

**`--threshold`**: 0 to 255, default 128. This option works only in
`Lineart` mode. A pixel that is darker than this value becomes black.

**`--resolution`**: 150, 200, 300 (default) or 600 dpi.

**`--page-width`, `--page-height`**: the paper size in mm. The maximum
width is about 219.5 mm. The maximum height is 895.5 mm. The default is
the maximum. With the maximum height, the scanner finds the length of the
paper itself.

**`-l`, `-t`, `-x`, `-y`**: the scan area on the page, in mm. The backend
cuts this area from the full image.

**`--ald`**: `yes` (default) or `no`. With `yes`, the backend removes the
empty area after the end of the paper. The backend then knows the page
height only after the scan starts. Some programs show a height of -1 before
the scan. This is normal.

**`--compression`**: `None` (default) or `JPEG`. With `JPEG`, the backend
gives the JPEG data of the scanner without a change. Most scan programs
cannot use this data. Keep `None`. This option is not available in
`Lineart` mode.

**`--halt-on-cancel`**: `no` (default) or `yes`. This option sets what
happens to the paper when you stop a scan. See "Stop a scan".

**`--scan`, `--long-press`, `--page-loaded`**: sensors that you can only
read. `scan` becomes yes one time after a short press of the Scan button.
`long-press` becomes yes one time after a long press (about 3 seconds).
`page-loaded` shows if paper is ready: in the top chute for an `ADF`
source, or in the front slot for a `Card` source. A button daemon such as
`scanbd` can read these sensors and start a scan.

The backend refuses an option change while a scan job is open. Stop the
job first.

---

## Stop a scan

The scanner does not wait for the computer. When a job starts, it feeds the
whole stack. A stop before the end of the stack has one of these two
results:

- **`--halt-on-cancel=no` (default).** The scanner feeds out all the
  remaining sheets with no scan. The backend waits until the paper path is
  empty (at most 120 seconds). After that, the scanner is ready for the next
  job.
- **`--halt-on-cancel=yes`.** The motor stops at once. A sheet can stay
  in the paper path. The other sheets stay in the chute. Open the top
  section of the scanner and remove the paper before the next scan.

A stop occurs when you use `--batch-count`, when you use `scanimage`
without `--batch` and the stack has more than one sheet, or when you push
Ctrl-C.

After an error, for example a paper jam, the backend stops the job and
does not eject the paper. Open the scanner and remove the paper by hand.

---

## Limits

- **No long page mode.** The maximum page length is 895.5 mm. The Docker
  service has a long page mode for receipts up to about 3 m. The backend
  does not have it.
- **No clean stop after a number of sheets.** See "Stop a scan".
- **Front slot:** one sheet at a time, and no paper longer than an A4
  sheet.
- **No encryption.** The backend has no TLS. It cannot use a scanner in a
  mode that needs TLS. Use it only in a network that you trust.
- **Paper jams.** On Wi-Fi, the scanner does not always report a paper
  jam. The page then ends at the point of the jam.
- **No host ID registration.** Register the host ID over USB (see the
  [main README](../README.md)).

---

## Test status

We did these tests on a real iX1300 over Wi-Fi with this backend:

- Duplex scans of a stack from the top chute: 2 sheets gave 4 pages, and
  3 sheets gave 6 pages.
- A stop with sheets left in the chute. The scanner fed out the rest of the
  stack.
- A stop with `--halt-on-cancel=yes`.
- The recovery after the scanner turned itself off and on again.

We did not do these tests with the backend yet:

- The front slot sources (`Card Front`, `Card Back`, `Card Duplex`).
- A stop with Ctrl-C while a page comes in.

[`../TODO.md`](../TODO.md) has the list of all open items.

---

## If you have a problem

To see more data, set the debug level before the scan command:

```
SANE_DEBUG_IX1300=5 scanimage -L
```

Level 1 shows errors, 5 shows important events, 15 shows each command,
and 30 shows all data bytes.

**`scanimage -L` finds no scanner.**
Make sure that the scanner is on: open the cover. Make sure that the
computer and the scanner are in the same network. If the broadcast does not
work, write `net <address>` in the configuration file.

**"Access to resource has been denied".**
The scanner does not know your host ID. Make sure that `hostid` in
`/etc/sane.d/ix1300.conf` is the ID that you registered. ScanSnap Home on a
different computer can register its own ID. Then register your ID again
over USB.

**"Device busy".**
A different program or computer uses the scanner. Stop the Docker service
and close ScanSnap Home. The scanner also gives this message when its USB
cable is connected. Disconnect the cable.

**"Document feeder out of documents" at the start.**
The chute is empty, or the paper is in the wrong place. For the front
slot, use a `Card` source.

**"Document feeder jammed".**
Paper is in the paper path. Open the top section of the scanner and remove
the paper. Then start again. The backend also gives this message when paper
from a previous job that did not end correctly stays in the scanner.

**"Error during device I/O".**
The scanner did not send data in time, or the data was not correct. Look
at the debug output with level 5.

**The scanner turned itself off.**
Open and close the cover to turn it on again. The backend finds the
scanner again at the next scan.

---

## For developers

### Files

- `ix1300.c`, `ix1300.h`: the backend source.
- `ix1300.conf.in`: the template of the configuration file.
- `sane-ix1300.man`: the manual page, sane-ix1300(5).
- `ix1300.desc`: the SANE description file.
- `sane-backends-ix1300.patch`: a `git diff` that adds the backend to
  sane-backends 1.3.1.
- `install-on-debian.sh`: the build and install script.
- `Makefile`: a standalone build.
- `tests/`: the test harness.

### Build

The usual way to build is `install-on-debian.sh`. It applies the patch to
the sane-backends 1.3.1 source and then copies the current `ix1300.c`,
`ix1300.h`, configuration template, manual page and description file over
the patched files. Thus the build always uses the files in this folder.

To use the patch with your own sane-backends tree, apply it at the tag
1.3.1:

```
cd sane-backends
git checkout 1.3.1
git apply /path/to/sane-backend/sane-backends-ix1300.patch
./autogen.sh
./configure BACKENDS="ix1300"
make
```

The backend needs libjpeg.

The `Makefile` in this folder builds the library from a sane-backends tree
that `configure` has already prepared:

```
make SANE_SRC=/path/to/sane-backends
sudo make install SANE_SRC=/path/to/sane-backends
```

### Tests

The test harness compiles `ix1300.c` with small copies of the SANE headers
and a simulated scanner. It needs a C compiler and the libjpeg headers. It
does not need sane-backends, a network, or a scanner. It uses
AddressSanitizer.

```
make -C sane-backend/tests            # run the tests
make -C sane-backend/tests test-real  # also decode scanner JPEG files
```

`test-real` uses the JPEG files in `service/tests/data`, and the real scans
in `scans/raw` if that folder exists. Set `JPEG_PREFIX` if the libjpeg
headers are not in `/opt/homebrew` or `/usr`.

The target `orig` runs the same tests on an older copy of the backend in
`tests/orig/`. That folder is not in the repository. Without it, the target
does nothing.

### How the backend talks to the scanner

This section gives a short overview. [`../PROTOCOL.md`](../PROTOCOL.md) has
all the details.

- **Discovery.** The backend sends a UDP probe to port 52217. It accepts a
  reply only from the address that the reply contains.
- **Reserve.** `sane_open` reserves the scanner on TCP port 53219 and sends
  commands on TCP port 53218. A thread sends a keepalive every 15 seconds.
  The reserve result -4 gives "Device busy". The results -6 and -7 (wrong
  host ID) give "Access to resource has been denied". The result -9 (USB
  mode) gives "Device busy".
- **Scan job.** The first `sane_start` sends D8, E9, D4, C2 and D5. For each
  sheet, the backend sends E0 and then reads each side until the end of
  the side. The job ends when E0 gives "no paper" (sense 3/80/03). Then the
  backend sends D6.
- **Stop.** The default stop sends D8, then E0, waits for an empty paper
  path, and sends D6. With `--halt-on-cancel`, it sends only D8 and D6.
  `sane_cancel` only sets a flag, so a signal handler can call it. The
  next backend call does the network work.
- **Lost replies.** The backend sends a command again only if this is safe
  (INQUIRY, C2, D8, E9, D4). It never sends E0, READ, D5 or D6 again. If a
  reply to D5 or D6 is lost, the next `sane_start` sends D8 and D6 and then
  reads C2. If paper of the old job is still in the paper path, it gives
  "Document feeder jammed" and does not start a new job.
- **Scan button.** While the device is open, the backend reads the button
  from C2 and from the trigger packets of the scanner. It accepts trigger
  packets only from the address of the scanner. This is a filter, not a
  security function.
- **Status values.** `SANE_STATUS_IO_ERROR` also comes when no new image data
  arrives for 60 seconds, when a side is not complete after 600 seconds,
  or when a side is larger than 64 MiB.

### License

The backend is under the GPL version 2 or later, with the SANE exception.
See [`LICENSE`](LICENSE) and [`COPYING`](COPYING).
