# ix1300 service

**The scan service for the ScanSnap iX1300. Push the Scan button, and get a PDF.**

This folder has the Docker service of the project. The service connects to the
scanner over Wi-Fi and waits for the Scan button. When you push the button, it
scans the paper, makes a clean PDF, and sends the PDF to Paperless-ngx, to a
folder, or to a webhook.

Start with the [project README](../README.md). It gives a short setup and the
daily use. This file gives the full details for the user and the administrator:
each command, each setting, the outputs, the hardware self-test, and the
remedies for problems. For the messages on the USB cable and on the network, refer to
[PROTOCOL.md](../PROTOCOL.md). For the open work, refer to [TODO.md](../TODO.md).

## Contents

1. [How the service works](#1-how-the-service-works)
2. [Install](#2-install)
3. [Configure](#3-configure)
4. [Daily use](#4-daily-use)
5. [Profiles](#5-profiles)
6. [Outputs](#6-outputs)
7. [Jobs, recovery and retries](#7-jobs-recovery-and-retries)
8. [Commands](#8-commands)
9. [Small computers and memory](#9-small-computers-and-memory)
10. [Security](#10-security)
11. [Problems and remedies](#11-problems-and-remedies)
12. [Hardware self-test](#12-hardware-self-test)
13. [Protocol trace](#13-protocol-trace)
14. [Development and tests](#14-development-and-tests)
15. [What is tested on the scanner](#15-what-is-tested-on-the-scanner)
16. [Configuration reference](#16-configuration-reference)

---

## 1. How the service works

The scanner works with one computer at a time. The service asks the scanner to
"reserve" itself for this computer, and it keeps the reserve all the time. The
scanner then sends a small network message to the service when you push the
Scan button.

When a button message comes, the service does these steps:

1. It selects a profile. A short press and a long press can use different
   profiles (for example, "documents" and "receipts").
2. It examines the paper sensors. With no paper, it does nothing.
3. It scans all the sheets in the scanner into one job.
4. It writes each sheet to disk at once, so that an error or a restart does not
   lose the scan.
5. It makes the PDF: it removes blank pages, turns pages that are upside down,
   and cuts receipts to the paper.
6. It sends the PDF to the outputs of the profile. It removes the job from the
   disk only after all outputs have the PDF.

If the scanner is off, the service waits. When you open the cover, the scanner
turns on, and the service finds it again in a few seconds.

**Important:** the scanner always feeds the full stack in one job. It cannot
stop after a number of sheets. Load only the sheets that you want to scan.

---

## 2. Install

You do the USB steps only one time. After that, the scanner works over Wi-Fi.

### 2.1 What you need

- A Linux computer with Docker, in the same network as the scanner.
- A USB cable for the first setup.
- A host ID: a number of 16 hex digits that the scanner accepts. Make a random
  one with this command:

  ```
  python3 -c "import secrets; print(secrets.token_hex(8).upper())"
  ```

### 2.2 Prepare the folders and the configuration

```
cd service
mkdir -p config data
cp config.example.toml config/config.toml
sudo chown 1000:1000 data && chmod 700 data
docker compose build
```

Write the host ID into `config/config.toml` as `host_id` in the `[scanner]`
section.

The service runs as the user `ix1300` (UID 1000, GID 1000), not as root. The
`data` folder must belong to this user. For a different user, set `IX1300_UID`
and `IX1300_GID` in the file `.env`, and build the image again.

### 2.3 Connect the scanner to Wi-Fi (USB, one time)

Connect the scanner with the USB cable, and open the cover to turn it on. Stop
`saned` and all other scan programs on this computer first. Only the USB
commands need root rights.

```
docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-wifi-setup --ssid YourNetworkName
```

The command asks for the Wi-Fi password. It writes the network into the
scanner, sets the scanner to "client" mode, and waits until the scanner has an
IP address.

- To give the password from a file, use `--key-file FILE` (the password on
  line 1). You can also set the variable `IX1300_WIFI_KEY`.
- The network must use a WPA or WPA2 password (PSK). The default is
  `--auth WPA2 --cipher AES`. Other values: `--auth WPA` or `MIX`, and
  `--cipher TKIP` or `AUTO`.

In your router, give the scanner a fixed IP address (a DHCP reservation). Write
this address into `config/config.toml` as `ip`, for example `ip = "192.168.1.50"`.

### 2.4 Register the host ID (USB, one time)

The scanner keeps one registered host ID. It accepts a reserve only from a
computer with this ID.

```
docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-register-host --host-id 0123456789ABCDEF
```

- Without `--host-id`, the command uses `scanner.host_id` from the
  configuration.
- `usb-register-host --show` shows the registered ID and changes nothing. This
  option also needs `--host-id` or `scanner.host_id`.

After this step, disconnect the USB cable.

**Caution:** do not send scan commands over USB while the service uses the
scanner over Wi-Fi. After a USB scan command, the scanner refuses the network
reserve until you close and open the cover.

### 2.5 Share the scanner with a Mac that has ScanSnap Home

The scanner keeps only one host ID. If ScanSnap Home on a Mac also uses the
scanner, use the ID of the Mac in `scanner.host_id`. Then you do not need step
2.4, and the two computers do not take the scanner from each other.

The Mac ID is the `HostID` value in the file
`~/Library/Application Support/PFU/ScanSnap/PfuSsHostID.plist` on the Mac.

### 2.6 Register the host ID over Wi-Fi (without USB)

You can also register the host ID over Wi-Fi. The read part of this command is
tested on the scanner. The write part is not tested yet.

The scanner must be on and in the network. Stop the service first, because the
scanner accepts one reserve only.

```
docker compose run --rm ix1300 wifi-register-host --show
docker compose run --rm ix1300 wifi-register-host --host-id 0123456789ABCDEF
```

- `--show` shows the ID of the last computer that used the scanner, the
  registered ID, and the registration data of the scanner. It changes nothing.
- Without `--host-id`, the command uses `scanner.host_id`.
- The command asks for a confirmation. Type `yes`. Without a terminal, add
  `--yes`.
- The command shows each step, and it does each step one time. If the scanner
  refuses the registration, use `usb-register-host`.

**Caution:** a registration replaces the ID of all other computers. ScanSnap
Home on another computer then stops to work with the scanner. At its next
connection, ScanSnap Home registers its own ID again, and this service then
loses the scanner.

`scanner.auto_register = true` makes the service do this registration by
itself when the scanner refuses its host ID. The service does it at most one
time in each hour, and it logs a warning. The default is `false`. Do not set it
to `true` on two computers: they then take the scanner from each other.

### 2.7 Start the service

```
docker compose up -d
docker compose logs -f
```

When the log shows `reserved scanner`, the service is ready. Push the Scan
button to make a test.

What `docker-compose.yml` sets:

- `network_mode: host`. The scanner sends its replies and button messages to
  the IP address of the host. The trigger port (UDP 55265) must be open in the
  host firewall.
- `restart: unless-stopped`. If a part of the service stops because of an
  error, the service stops with exit code 1, and Docker starts it again.
- `stop_grace_period: 90s`. This gives the service time to end a scan and
  release the scanner (refer to 4.8).
- `mem_limit`, default 768 MB. Change it with `IX1300_MEM_LIMIT=400m` in
  `.env` (refer to section 9).
- `OMP_THREAD_LIMIT=1`. Tesseract uses one thread.
- `/etc/localtime` from the host, so that the PDF titles use the local time.

### 2.8 Image options (build arguments)

You can make the image smaller. Set these values in `docker-compose.yml` under
`build: args:`.

| Argument | Default | Effect |
|---|---|---|
| `WITH_TESSERACT` | `1` | `0`: no tesseract. Then no page rotation and no OCR. |
| `TESSERACT_LANGS` | empty | More OCR languages, for example `"deu fra"`. |
| `WITH_PILLOW` | `1` | `0`: no Pillow. Then no deskew. |
| `UID`, `GID` | `1000` | The user of the service (the owner of `data`). |

Without Pillow, the service uses the programs `djpeg` and `jpegtran` for the
blank test and the crop.

---

## 3. Configure

The configuration is the file `config/config.toml`. The file
`config.example.toml` has all the important settings, with comments. Section
16 of this file lists every setting.

### 3.1 The rules

- The file is TOML. A JSON file is also possible.
- The service examines all values before it starts. It stops with
  `config error` for an unknown section or key (for example `[pipline]`), for
  a wrong type, or for a value out of range.
- A setting that is not in the file gets its default value.

### 3.2 Change a setting with an environment variable

Each setting has an environment variable: `IX1300_<SECTION>__<KEY>`. Use two
underscores between the parts. Examples:

```
IX1300_SCANNER__IP=192.168.1.50
IX1300_PROFILES__DEFAULT__DPI=600
IX1300_OUTPUTS__PAPERLESS__TOKEN=0123abcd
```

The type of the setting decides how the service reads the text. Numbers and
`true`/`false` are read as values. Lists and tables are read as JSON, for
example `[1,2]`. Tokens, URLs, addresses and header values stay text, exactly
as you give them.

To read a value from a file (for example a Docker secret), add `_FILE` to the
name:

```
IX1300_OUTPUTS__PAPERLESS__TOKEN_FILE=/run/secrets/paperless_token
```

The variable `IX1300_CONFIG` gives the path of the configuration file. In the
container, it is `/config/config.toml`.

### 3.3 The minimum configuration

You must set these values:

- `scanner.host_id`: the registered host ID.
- `scanner.ip`: the fixed IP address of the scanner (recommended).
- One or more outputs (refer to section 6).

---

## 4. Daily use

### 4.1 Load the paper

- **Top chute:** a stack of sheets, or one long receipt.
- **Front slot:** one card, photo, or thick sheet at a time. The sheet comes
  back out at the front.

### 4.2 Push the Scan button

| What you do | Profile that the service uses |
|---|---|
| Short press: push and release | `service.short_press_profile` |
| Long press: hold until the button flashes blue (about 3 s), then release | `service.long_press_profile` (empty: the short press profile) |
| A sheet in the front slot (chute empty), then a short or long press | `service.front_slot_profile` (empty: the profile of the press) |

The example configuration uses the `default` profile for a short press and the
`receipt` profile for a long press.

With no paper in the chute and no sheet in the front slot, a press starts no
job. The log then shows `no paper in the chute or the front slot`. A long press
works only with paper in the chute.

### 4.3 Continue scanning (more loads into one PDF)

This function is the same as "Continue scanning" in ScanSnap Home. It is
tested on the scanner. It is off by default.

Set `service.continue_window` to a number of seconds, for example `30`. After
each scan, the document then waits for this time. Load more paper, and push the
same button gesture again before the time ends. The service scans the new load
and adds its pages to the end of the document. The time then starts again.

Example log line:

```
continue scanning: part 2 added (3 sheets); delivery in 30 s
```

The rules:

- "The same gesture" means the same profile. A press with the other profile
  first sends the document that waits, and then starts a new document.
- A press with no paper changes nothing. The time does not start again.
- The end of the time, or a stop of the service, sends the document.
- A scan error in a part (jam, double feed, lost sheet) sends the document at
  once. The warning gives the part, for example
  `part 2: scan stopped after 1 sheets: ...`.
- All parts use the profile of the first part.
- A front slot sheet can also be a part, for example a card between two
  stacks.
- After a restart (also after a crash), the service sends the document with
  all the parts that it has.

### 4.4 The front slot

The front slot is tested on the scanner. Before each job, the service reads the
paper sensors one time. Paper in the chute gives a normal job. An empty chute
and a sheet in the front slot give a front slot job. The service scans the one
sheet (both sides), and the sheet comes back out at the front.

The log shows the paper path, for example
`scan button (short press): profile default, front slot`. The webhook and the
job records show `paper_path = "front"`.

The front slot takes one sheet at a time and no long paper. A receipt that is
longer than an A4 sheet can jam.

### 4.5 Long receipts

The normal mode scans a page of up to about 895 mm (89.5 cm). The long page
mode scans up to 3,000 mm (the limit of the scanner). To use it, set
`length = "long"` in a profile. The example configuration sets it in the
`receipt` profile, so a long press scans in long page mode.

The long page mode is tested on the scanner: an 83 cm receipt went through in
one piece. A receipt longer than about 89.5 cm is not tested yet.

The rules:

- Top chute only. A sheet in the front slot gets the normal length, and the
  log shows a warning.
- The resolution must be 150, 200 or 300 dpi. The service does not accept
  600 dpi with `length = "long"`.
- Set the paper guides against the edges of the receipt. Hold the end of a long
  receipt loosely so that it goes in straight. A skewed receipt can jam.
- A long receipt needs much memory for the deskew. On a small computer, refer
  to section 9.

### 4.6 Double feed

The scanner can find two sheets that go through together (a double feed). The
detection is on by default (`double_feed = true` in each profile). When it
finds a double feed, the scan stops, and the log shows
`double feed: fan the paper and load it again`.

Set `double_feed = false` in a profile for envelopes, or for sheets with a
label. This setting is not tested on the scanner yet.

### 4.7 One side or two sides

The scanner always reads both sides. With `duplex = false`, the service keeps
only the side with more print. Thus the paper can face up or down. If the
service cannot measure the print of a side, it keeps that side, with a
warning.

### 4.8 Stop the service during a scan

At `docker compose stop`, the service stops to accept button presses. A scan
that runs can continue for `stop_scan_wait` seconds (default 15). Then the
service stops the scan. This stop is tested on the scanner.

**Caution:** when the service stops a scan, the scanner feeds the other
sheets out **without a scan**. The service keeps the complete sheets and sends
them in a PDF, with the warning `the other sheets in the scanner (if any) may
have been ejected without a scan`. Scan the other sheets again.

The service waits until the paper path is empty (at most 120 s, and not after
the stop deadline). If the stop takes longer than `stop_timeout` (default
60 s), the job stays on disk, and the next start continues it. Keep
`stop_timeout` less than `stop_grace_period` in `docker-compose.yml`.

### 4.9 Auto power-off

The scanner turns itself off after some time with no job. Open the cover to
turn it on again. The service finds it again in a few seconds.

You can read or change this time:

- With the command `settings` (refer to section 8).
- With the setting `scanner.power_off_minutes`. The service then sets the time
  after each reserve, but only when the value in the scanner is different.

The values are `0` (never turn off), or 15 to 1890 minutes in steps of 15. The
factory value is 240 minutes. An empty value (the default) makes the service
leave the scanner value as it is.

The read and the write are tested: the scanner keeps the value. A scanner that
never got a value shows `not set (factory value 240 min)`. The effect of a new
time on the real power-off is not tested yet.

Only a scan job starts the idle time again. The keepalive messages of the
service do not.

---

## 5. Profiles

A profile is a set of scan and PDF settings. Each profile is a table
`[profiles.NAME]`. The example configuration has two profiles:

- `default` (short press): normal documents, both sides, 300 dpi, normal
  length.
- `receipt` (long press): one side, long page mode, deskew, crop to the paper,
  and the document is always a receipt.

### 5.1 Profile settings

| Key | Values | Default |
|---|---|---|
| `dpi` | 150, 200, 300, 600 | 300 |
| `color` | `auto`, `color`, `gray` | `auto` |
| `duplex` | `true`: both sides. `false`: the side with more print | `true` |
| `paper_face` | Page order only. `down`: back side first. `up`: front side first | `down` |
| `length` | `auto` (up to 895 mm), `long` (up to 3,000 mm), or 10 to 895 (mm) | `auto` |
| `blank_skip` | Remove blank sides | `true` |
| `crop` | Cut the page to the paper: `auto` (receipts only), `true`, `false` | `auto` |
| `rotate` | `auto`, 0, 90, 180, 270 (empty: `pipeline.rotate`) | empty |
| `deskew` | Straighten the page (empty: `pipeline.deskew`) | empty |
| `ocr` | Add a text layer (empty: `pipeline.ocr`) | empty |
| `receipt` | `auto` (measure), `yes`, `no` | `auto` |
| `double_feed` | Double-feed detection | `true` |
| `title` | Title format for this profile (empty: `pipeline.title_format`) | empty |
| `outputs` | Names of outputs. Empty list: all outputs | `[]` |

Note: the example configuration sets `paper_face = "up"` in the `default`
profile. If the pages of a two-sided document are in the wrong order, change
this value.

### 5.2 Separate outputs for each profile

Each profile can send its documents to its own outputs. You can have more than
one output of the same type, each with its own name. In this example, a short
press sends documents to Paperless with tag 1. A long press sends receipts to
Paperless with tag 7 and document type 3, and also to an expense webhook.

```toml
[service]
short_press_profile = "default"      # short press
long_press_profile = "receipt"       # long press: hold until the button flashes blue

[outputs.paperless_docs]
type = "paperless"
url = "http://paperless:8000"
token = "change-me"                  # or IX1300_OUTPUTS__PAPERLESS_DOCS__TOKEN
tags = [1]

[outputs.paperless_receipts]
type = "paperless"
url = "http://paperless:8000"
token = "change-me"                  # or IX1300_OUTPUTS__PAPERLESS_RECEIPTS__TOKEN
tags = [7]
document_type = 3

[outputs.expense_webhook]
type = "webhook"
url = "https://expenses.example/api/receipts"

[profiles.default]
outputs = ["paperless_docs"]

[profiles.receipt]
duplex = false
length = "long"
crop = true
receipt = "yes"
outputs = ["paperless_receipts", "expense_webhook"]
```

A profile with an empty `outputs` list sends to all outputs. A name in
`outputs` without an `[outputs.NAME]` table is a configuration error. Two
outputs can also go to two different Paperless servers.

### 5.3 What the service does to the pages

- **Blank pages:** the service measures the dark pixels on a small gray copy of
  each side. A side with less than `blank_ink_ratio` (0.4 %) of dark pixels is
  blank. Light print from the other side does not count. If all sides look
  blank, the service keeps the side with the most print of each sheet.
- **Rotation:** with `rotate = "auto"`, tesseract finds the orientation of the
  page. The PDF page gets the correct rotation. The image data does not change.
- **Deskew:** the service measures the angle from the paper edges, or from the
  lines of print. Then Pillow turns the page. This needs Pillow and a new JPEG
  encode.
- **Crop:** the service cuts the page to the paper, with a margin of 2 mm.
  Print near an edge stays in. A cropped receipt PDF is about 30 to 40 %
  smaller.
- **Receipts:** a page is a receipt when the paper length divided by the paper
  width is more than `receipt_ratio` (2.0). The service measures the paper
  itself, not the scan window.
- **OCR:** `ocr = true` makes a PDF with a text layer that you can search. If
  the service cannot turn a page for OCR, it makes the PDF without a text
  layer.
- **PDF:** without OCR, the PDF contains the JPEG data from the scanner,
  without a new encode.

If a step fails on a page (for example the deskew), the service uses the page
as scanned and adds a warning.

---

## 6. Outputs

An output is a destination for the PDF. Each output is a table
`[outputs.NAME]` with a `type`. There are three types.

### 6.1 Local folder (`type = "directory"`)

```toml
[outputs.local]
type = "directory"
path = "/data/out"          # data/out on the host
# file_mode = "0640"        # default "0600"
# dir_mode = "0750"         # default "0700"
```

The service writes the PDF into `path`. It never replaces a file: a second file
with the same name gets the name `name-1.pdf`. New files get `file_mode`, and
new folders get `dir_mode`. Use `file_mode = "0640"` if other users must read
the files.

On a network share without hard links, the service makes the file and then
copies the PDF into it. Only a crash during the copy can leave a short file.
The job then stays on disk, and the service sends it again.

### 6.2 Webhook (`type = "webhook"`)

```toml
[outputs.webhook]
type = "webhook"
url = "https://example.com/scan"
field = "file"                                   # the name of the file field
headers = { Authorization = "Bearer change-me" }
fields = { source = "ix1300" }
```

The service sends a multipart POST request to `url`. The PDF is in the field
`field`. The request also has these fields:

| Field | Value |
|---|---|
| `title` | The title of the document |
| `profile` | The name of the profile |
| `pages` | The number of pages |
| `receipt` | `true` or `false` |
| `created` | The scan time |
| `job_id` | The job ID |
| `paper_path` | `top` (chute) or `front` (front slot) |
| `parts` | Continue scanning, two or more parts only (JSON) |
| `warnings` | Only if the scan has warnings (JSON list) |

The values in `fields` are added to the request. `headers` adds HTTP headers,
for example a token.

### 6.3 Paperless-ngx (`type = "paperless"`)

```toml
[outputs.paperless]
type = "paperless"
url = "http://paperless:8000"
token = ""                  # or IX1300_OUTPUTS__PAPERLESS__TOKEN
tags = [1]                  # tag IDs for all documents
receipt_tags = [7]          # tag IDs added for receipts
correspondent = 0           # 0: not set
document_type = 0
receipt_document_type = 0
```

The service uploads the PDF to `<url>/api/documents/post_document/` with the
API token of Paperless. Without a `token`, the service uses the environment
variable `PAPERLESS_TOKEN`.

- Tags, correspondent and document type are IDs from Paperless. Use 0 for
  "not set".
- For a receipt, the service adds `receipt_tags`. It uses
  `receipt_correspondent` and `receipt_document_type` if they are set.
- If the scan has a warning (for example a probable jam), the title gets
  `warning_title_suffix` (default ` [scan warning]`). Set `""` to stop this.
- "Sent" means that Paperless accepted the upload. Paperless makes the
  document some seconds later.

### 6.4 Retries

Each output tries again when a send fails. These settings are the same for all
output types:

| Key | Default | Description |
|---|---|---|
| `retries` | 2 | New attempts after a failure |
| `retry_delay` | 5 | Seconds before the first new attempt (then 2 x, 4 x, ...) |
| `timeout` | 60 (webhook), 120 (Paperless) | Seconds for each network operation |
| `max_time` | 600 | Seconds for one full attempt: connection, upload and reply. A name look-up (DNS) can take longer |
| `verify_tls` | `true` | Examine the HTTPS certificate |

Each wait is between 50 % and 100 % of the calculated value, so that many
retries do not occur at the same time.

An HTTP status 4xx (but not 408 or 429) is a permanent error. The service then
makes no new attempt until you correct the configuration.

### 6.5 Queues

Each output has its own queue, and sends one document at a time. A slow or
failed output delays only its own documents. The other outputs send at once.

After the retries of an output, the PDF goes into `work_dir/unsent`. The
service tries again later (refer to section 7).

The service sends each document **at least once**. If a receiver keeps a
document but its reply is lost, the service sends the document again. Each
request has the job ID (the header `X-Ix1300-Job`, and the webhook field
`job_id`), so that a receiver can find copies. Paperless can find duplicates by
their checksum.

The logs do not show tokens, header values, or the reply text. For Paperless,
the log and the job record keep only the task ID of the reply. For a webhook,
the job record keeps the first 200 characters of the reply. The service
removes the tokens and header values from the full reply before it cuts the
text.

---

## 7. Jobs, recovery and retries

The service keeps each job on disk until all outputs have the PDF. Thus an
error, a receiver that is down, or a restart does not lose a scan. The job
folders are in `work_dir` (default `/data`, which is `data` on the host):

| Folder | What it is |
|---|---|
| `jobs/<job ID>` | A job in work. After a crash, the next start makes the PDF again and sends it. |
| `unsent/<job ID>` | The PDF, after one or more outputs failed. The service tries again. |
| `failed/<job ID>` | The original sheets of a job without a correct PDF, or a job that lost pages. |
| `raw/<job ID>` | A copy of the scanner images, only with `keep_raw = true`. |

The job ID has the time and a random part, for example
`20261010-142501-3fa9c2`. Thus no job replaces another job.

### 7.1 Automatic retries

The service tries the failed outputs of a job in `unsent` again. The first wait
is `retry_interval` (60 s). Each wait is then two times longer, up to
`retry_max_interval` (1 hour). After `retry_max_attempts` (20) attempts, only
the command `ix1300 retry` sends the job. The service keeps the time of the
next attempt on disk, so a restart does not start the count again.

### 7.2 Manual retries

```
docker compose run --rm ix1300 retry --list      # show the unsent and failed jobs
docker compose run --rm ix1300 retry             # send all jobs in unsent now
docker compose run --rm ix1300 retry --failed    # make the PDF again for failed jobs, then send
```

`retry` sends to all failed outputs, also after a permanent error. It gives
exit code 4 if jobs stay unsent.

### 7.3 Errors during a scan

A sheet counts only when the scanner sends both sides completely. If an error
stops the scan (a double feed, a jam, or a sheet that is lost on the way), the
service never uses the incomplete sheet.

- `deliver_partial = true` (default): the service sends the complete sheets
  before the error, with a warning.
- `deliver_partial = false`: the service sends nothing. The sheets stay in
  `work_dir/failed`.

If a page cannot be used at all, the document gets a warning, and the original
sheets stay in `work_dir/failed` after the delivery.

### 7.4 Limits before a scan

The scanner feeds the full stack after the start, so the service decides before
the scan:

- If `work_dir` has less than `min_free_mb` (200 MB) free, the service does not
  start the scan.
- If `max_backlog` (3) jobs wait for the PDF work or for their first
  delivery, the service does not start the scan. Thus a slow output stops
  new scans before too many documents wait in its queue.

The paper then stays in the chute. Correct the cause, and push the button
again. A scan that started always continues to the end.

The jobs in `work_dir/unsent` do not count for `max_backlog`. At most
`max_backlog` of them are in the output queues at the same time. The service
puts the other jobs into the queues later.

### 7.5 Damaged jobs

A crash or a full disk can stop the service after it wrote a sheet and before
it wrote the job record. At the next start, the service compares the folder
with the record:

- A sheet with both sides goes into the PDF. Its originals stay in
  `work_dir/failed` after the delivery.
- One side without the other side stays in the folder. The service does not
  remove it. Examine it, and then remove the job folder yourself.

Remove old folders in `failed` yourself when you do not need them.

Each job has a lock file (`.lock`). Thus the service and `ix1300 retry` never
work on the same job at the same time.

---

## 8. Commands

All commands run in the container:

```
docker compose run --rm ix1300 COMMAND [OPTIONS]
```

**Important:** the scanner accepts one computer only. Stop the service
(`docker stop ix1300`) before you use `scan`, `settings`, `selftest`, or
`wifi-register-host`. Start it again with `docker start ix1300`.

| Command | What it does |
|---|---|
| `serve` | Run the service (the default command of the container) |
| `discover` | Find scanners in the network |
| `status` | Show the scanner, the registered host ID, and the paper |
| `scan` | Scan one time and write a PDF |
| `settings` | Read or set the auto power-off time |
| `retry` | Send the unsent jobs again (section 7.2) |
| `selftest` | The guided hardware self-test (section 12) |
| `usb-wifi-setup` | Connect the scanner to Wi-Fi over USB (section 2.3) |
| `usb-register-host` | Register the host ID over USB (section 2.4) |
| `wifi-register-host` | Register the host ID over Wi-Fi (section 2.6) |

General options: `-c FILE` (the configuration file), `-v` (debug log), and
`--version`.

### 8.1 discover and status

```
docker compose run --rm ix1300 discover
docker compose run --rm ix1300 discover --ip 192.168.1.50 --timeout 5
docker compose run --rm ix1300 status
```

`discover` shows each scanner that answers. `status` also shows if the
registered host ID agrees with the configuration, and where the paper is.

### 8.2 scan

```
docker compose run --rm -v $PWD/data:/out ix1300 scan --profile default --out /out/test.pdf
```

| Option | Effect |
|---|---|
| `--profile NAME` | The profile (default: `service.short_press_profile`) |
| `--out FILE` | Write the PDF into this file |
| `--send` | Also send the PDF to the outputs of the profile |
| `--raw DIR` | Also write the JPEG images of each side into this folder |

`scan` uses the same job folders and rules as the service. If the PDF or an
output fails, the job stays in `work_dir/failed` or `work_dir/unsent`, and the
command shows the folder. Use `retry` to continue.

Exit codes: 0 OK, 2 no reserve, 3 no sheet, 4 an output failed, 5 an error
stopped the scan (the PDF has the complete sheets before the error).

### 8.3 settings

```
docker compose run --rm ix1300 settings --show
docker compose run --rm ix1300 settings --power-off 240
```

`--show` reads the auto power-off time. `--power-off MINUTES` sets it and reads
it back. Use `0` for "never turn off", or 15 to 1890 in steps of 15.

### 8.4 Debug log

Add `-v` before the command for a debug log:

```
docker compose run --rm ix1300 -v status
```

---

## 9. Small computers and memory

The default values are measured on a Raspberry Pi 5 with 8 GB of memory. On a
Raspberry Pi Zero 2 W (512 MB), OCR and deskew used all the memory: both
decode the full page. A long receipt at 300 dpi is about 80 MB as pixels.

On a computer with less than 1 GB, use these values, and examine the memory
use with `docker stats` and your longest documents:

```toml
[pipeline]
workers = 1
max_memory_mb = 16
deskew = false
ocr = false

[service]
max_backlog = 1

[profiles.receipt]
deskew = false
```

Also set `IX1300_MEM_LIMIT` in `.env` to a value below the free memory. Without
OCR and deskew, the service decodes only small gray copies of the pages.

How the memory settings work:

- `max_memory_mb` (64) is the JPEG data of all jobs in memory. More data goes
  into files in `work_dir`.
- `workers` (1) is the number of threads for the page work.
- `max_decode_mb` (192) is the largest memory for the deskew of a long page.
  A long gray receipt needs about 96 MB, the same receipt in colour about
  383 MB. If a deskew needs more, the service does not straighten the page.
- Tesseract uses one thread and has a time limit (`tesseract_timeout`).

---

## 10. Security

- The scanner protocol has no password and no encryption. Each device in the
  network can send button messages and read the scan data. Use the service
  only in a network that you trust, or put the scanner and the host in their
  own network (VLAN) with a firewall.
- The service accepts button messages only from the address of the scanner.
  This is a filter, not an authentication.
- Set `scanner.host_ip` so that the trigger port listens on one address only.
  Open the trigger port (UDP 55265) only for the scanner.
- The service runs without root rights. Its files have mode 0600 and its
  folders mode 0700. Scans are personal data: keep `data` private.
- Use `keep_raw` only for tests. The copies contain the full documents.
- Put tokens into a file with mode 0600 (`..._TOKEN_FILE`) or into the
  environment. Do not put them into a configuration file that other users can
  read.

---

## 11. Problems and remedies

Each item gives the log text (or the symptom), the cause, and the remedy.

**`host ID ... is not registered`**
Another computer (for example with ScanSnap Home) registered its own ID.
Register the ID again (section 2.4 or 2.6), or use the ID of that computer
(section 2.5).

**`scanner busy with a USB host or a stale job; close and open the cover`**
The scanner is in a USB job, or an old job did not end. Close the cover and
open it again. Do not use the scanner over USB at the same time.

**`scanner occupied by another host`**
Another computer has the reserve. The service tries again after 2 to 10 s
(`scanner.retry_min` and `retry_max`).

**`double feed: fan the paper and load it again`**
The scanner found two sheets together. The service sends nothing for that
sheet. Fan the stack, load it again, and push the button.

**`probable paper jam`**
Over Wi-Fi, a jam can end a page without an error, so the page is shorter than
the paper. After the scan, a sensor still showed paper. The service sends the
pages with this warning. Remove the paper, and scan again. If the service
cannot read the sensors, the warning is `jam status not known`: examine the
scanner.

**`may have been ejected without a scan`**
The service stopped during a scan (refer to 4.8). Scan the other sheets
again.

**A long receipt jams or comes out skewed**
Set the paper guides against the edges of the receipt.

**The service does not find the scanner after an auto power-off**
Open the cover. Set `scanner.ip`, and make sure that UDP replies from port
52217 come to the host. The log shows `still waiting for the scanner` every 10
minutes.

**No reaction to the Scan button**
The log must show `reserved scanner`. Open the trigger port (UDP 55265) in the
host firewall.

**`no scanner found`**
Open the cover to turn the scanner on. Set `scanner.ip`: a busy scanner does not
answer all broadcast messages.

**The host computer becomes slow or stops**
Use the small computer values of section 9.

**Blank pages stay in the PDF**
Make sure that Pillow or `djpeg` is available (the log shows it at the start).
Adjust `blank_ink_ratio`.

**Pages are upside down**
Use the default image (with tesseract), or set `rotate = 180` in the profile.

**`scan not started: only N MB free`**
Make space in `work_dir` (for example old jobs in `failed`), or change
`min_free_mb`. Push the button again.

**`scan not started: N jobs wait for processing or delivery`**
The PDF work or the outputs are slow. Wait, and push the button again. Change
`max_backlog` if necessary.

**`trigger packet from ... ignored`**
A message came from an address that is not the scanner. Examine `scanner.ip`,
or find the device that sends to the trigger port.

**`PDF kept in .../unsent/...`**
An output failed. The service tries again. Examine the receiver, then use
`ix1300 retry`.

**`Permission denied` for `/data`**
The `data` folder must belong to UID 1000 (or to `IX1300_UID`).

---

## 12. Hardware self-test

The self-test is one guided session at the scanner, of about 15 minutes. It
examines the functions that need real paper and the Scan button. It keeps all
data for a later analysis, so a failure does not need a second session.

Run the self-test after an update of the service, or when you want to test a
function of section 15 on your scanner.

### 12.1 What you need

- The scanner on, in the network, with the host ID of the service.
- 5 plain sheets (A4 or Letter). Use plain sheets: the results contain the
  images.
- Optional: a receipt longer than 90 cm.
- Optional: 2 sheets that you tape together with an overlap of a few
  centimetres (a double feed).

### 12.2 Run it

```
docker compose build                      # only after an update of the code
docker stop ix1300
docker compose run --rm ix1300 selftest
docker start ix1300
```

If the service still has the scanner, the self-test stops with
`Stop the service first: docker stop ix1300`. It changes nothing on the
scanner.

At each step, the terminal shows what paper to load and which button gesture to
use. Then the step runs, and the terminal shows `PASS`, `FAIL` or `SKIPPED`
with a short reason. The next step starts also after a failure.

- Type `s` and Enter to skip a step. Type `q` and Enter to stop the session.
- Push Ctrl-C to stop. During a scan, the first Ctrl-C ends the job, and a
  second Ctrl-C stops at once. The self-test then releases the scanner and
  keeps the data.
- `--steps front_slot,stop` runs only these steps.
- `--button-timeout 120` sets the time for each button press (seconds).
- Exit codes: 0 all steps `PASS` or `SKIPPED`, 1 a `FAIL`, 2 stopped by the
  user, 3 another computer has the scanner.

### 12.3 The steps

| Step | Paper | What you do | What the step examines |
|---|---|---|---|
| `sensors` | none | Enter | Discovery, reserve, paper sensors, registered host ID |
| `power_off` | none | Enter | Auto power-off: read, change, read back, set the old value again |
| `registration` | none | Enter | The registration data over Wi-Fi. It registers nothing |
| `empty_press` | none | Short press | No job with no paper |
| `continue` | 2 + 2 sheets | Short press, load 2 more, push again | Continue scanning: one PDF, 8 pages in order |
| `front_slot` | 1 sheet in the front slot | Short press | A front slot job with two sides |
| `long_receipt` | a receipt longer than 90 cm (optional) | Long press | Long page mode |
| `double_feed` | 2 taped sheets (optional) | Enter, two times | Detection on, then off |
| `stop` | 3 sheets (optional) | Enter | A service stop during the scan |

Some steps ask you a question, for example if the sheet came back out at the
front. If the scanner refuses the long page mode, the `long_receipt` step
records the reason, scans again with the normal length, and shows `FAIL`.

The self-test uses the code of the service, so a `PASS` shows that the service
works. It uses its own profiles (300 dpi, all pages kept, no rotation, no OCR).
It sends no document to your outputs.

### 12.4 The result

The self-test writes all data into `work_dir/selftest/<time>/`, and packs it
into `work_dir/selftest/<time>.tar.gz`. With the Docker setup, the file is in
`data/selftest/` on the host. Send this one file for the analysis.

| File | Contents |
|---|---|
| `report.txt` | One line for each step: the result and the reason |
| `report.json` | All results, the measured values, the warnings, the scanner data |
| `selftest.log` | The full debug log |
| `preflight/trace.jsonl` | The protocol trace of the discovery and the first reserve |
| `steps/NN-<step>/trace.jsonl` | The protocol trace of each step |
| `steps/NN-<step>/document-NN.pdf` | The PDF of each document |
| `steps/NN-<step>/work/raw/<job>/` | The scanner images and `raw.json` |

---

## 13. Protocol trace

The protocol trace records each message to and from the scanner. It helps to
find the cause of a problem. It is off by default.

To start it, set a folder:

```toml
[service]
trace_dir = "/data/trace"
```

- Each start of the service makes a new file
  `service-<time>-<pid>.jsonl`. The command `scan` writes
  `scan-<time>-<pid>.jsonl`.
- Each line is one message, as JSON.
- Long data is cut to `trace_payload_bytes` (256).
- Image data is never in the trace. The trace shows only its length, its
  checksum, and its first and last 32 bytes.
- A file larger than `trace_max_mb` (50) becomes `<name>.1`, and a new file
  starts.
- When a file starts, the service removes the oldest trace files in the
  folder until all trace files together are not larger than `trace_total_mb`
  (500). Only the files `*.jsonl` and `*.jsonl.1` count. Other files in the
  folder stay. The new file can then grow to `trace_max_mb` before the next
  check.

The script `tests/replay.py` reads a trace. It shows the command sequence of
each job, and it can compare a trace with the emulator.

### 13.1 Line format

All lines have these keys:

| Key | Value |
|---|---|
| `n` | Line number in the file |
| `t`, `wall` | Monotonic time and clock time, in seconds |
| `dir` | `out` (to the scanner), `in` (from the scanner), `conn`, `close`, `err`, `note` |
| `port` | `port1`, `port2`, `search`, `keepalive`, `trigger` (empty for a note) |

A Port1 command has two lines with the same `x`:

- The `out` line has `op`, `cdb`, `in_len`, `len`, and `data`.
- The `in` line has `status`, `scsi`, `len`, and `data`. For REQUEST SENSE, it
  also has `sense`. For an image READ, it has `image` (`len`, `sha256`,
  `head`, `tail`) and no `data`.

`trunc: true` shows data that is cut. Port2 lines have `kind` and, for the
reply, `result`. Trigger lines have `code`, `seq`, and `long`.

### 13.2 Raw images (`keep_raw`)

`service.keep_raw = true` keeps a copy of the scanner images of each job in
`work_dir/raw/<job ID>`. The file `raw.json` gives the profile, the paper path,
and for each side the blank decision and the measured values: before the work
(`original`) and in the PDF (`delivered`). Only the newest `keep_raw_max_jobs`
(20) folders stay. Use this only for tests: the copies contain the full
documents.

---

## 14. Development and tests

The tests use only the Python standard library. They use an emulator of the
scanner (`tests/emulator.py`), so they need no scanner and no network.

```
python3 -m unittest discover -s service/tests
```

- `IX1300_TEST_NET=real` or `mem` selects the test network. With `real`, the
  emulator uses 127.0.0.1. If the system does not let the tests open a port,
  they use a network in memory.
- `IX1300_TEST_LOG=DEBUG` shows the logs.
- `tests/data` has one synthetic receipt, made by
  `tests/make_synth_receipt.py`. The tests also use real scans in
  `../scans/receipts` and `../scans/raw` if these folders exist. Else, these
  tests are skipped.

---

## 15. What is tested on the scanner

**Tested on the scanner:**

- Wi-Fi setup and host ID registration over USB.
- The reserve, the keepalive, and the Scan button (short press and long press).
- Scans from the top chute in colour mode `auto` at 300 dpi, normal length.
- The front slot.
- Continue scanning (several loads into one PDF).
- Long page mode for receipts: an 83 cm receipt went through in one piece.
- Auto power-off: read and write. The scanner keeps the value. The value
  `00 00` means "never set" (factory value 240 minutes).
- The service stop during a scan.
- The registration read over Wi-Fi (`wifi-register-host --show`).

**Not tested on the scanner yet:**

- Receipts longer than about 89.5 cm.
- Double-feed detection on and off.
- The registration write over Wi-Fi (`wifi-register-host` and
  `scanner.auto_register`).
- The effect of a new auto power-off time on the real power-off.
- Other colour modes (`color`, `gray`), other resolutions (150, 200, 600 dpi),
  and a fixed `length` in mm.
- OCR with real scans.
- Some rare error paths of a job, and the trigger codes other than the Scan
  button.
- The memory use on a Raspberry Pi Zero after the latest changes.
- TLS (the service does not use TLS).
- The PDF pages contain some extra bytes after the image data. Ghostscript
  shows these pages correctly. Other PDF viewers are not tested.

For the plan of these tests, refer to [TODO.md](../TODO.md). Most of them are
steps of the self-test (section 12).

---

## 16. Configuration reference

The values in the tables are the defaults. Most users change only the settings
in sections 3.3 and 5. The comments in `config.example.toml` give more help.

### 16.1 `[scanner]`

| Key | Default | Description |
|---|---|---|
| `ip` | empty | IP address of the scanner. Empty: find it with a broadcast |
| `broadcast` | `255.255.255.255` | Broadcast address for the search |
| `name` | empty | With a broadcast: use only the scanner with this name or serial number |
| `host_id` | empty | The registered host ID (16 hex digits). Necessary |
| `host_ip` | empty | IP address of this host. Empty: find it. If set, the trigger port listens only here |
| `host_mac` | empty | MAC address of this host. Empty: find it |
| `search_port` | 52217 | UDP port of the scanner search |
| `trigger_port` | 55265 | UDP port for the button messages |
| `discovery_timeout` | 3 | Seconds to wait for search replies |
| `keepalive_interval` | 15 | Seconds between keepalive messages. Less than `reserve_timeout` |
| `keepalive_repeat` | 3 | Messages in each interval (the scanner Wi-Fi saves power) |
| `keepalive_wait` | 3 | Seconds to wait for the reply in each interval |
| `keepalive_failures` | 3 | Intervals without a reply before the service reserves again |
| `reserve_timeout` | 30 | Seconds (1 to 255) that the scanner keeps the reserve without keepalive |
| `power_off_hold` | 20 | Seconds without a reserve attempt after the scanner turns off |
| `waiting_log_interval` | 600 | Seconds between `still waiting for the scanner` log lines |
| `retry_min`, `retry_max` | 2, 10 | Seconds between reserve attempts (the time doubles up to `retry_max`) |
| `renew_after_job` | `false` | `true`: release and reserve again after each job |
| `yield_on_request` | `true` | Release the scanner for 60 s when another computer asks for it |
| `auto_register` | `false` | Register the host ID over Wi-Fi when the scanner refuses it (section 2.6) |
| `power_off_minutes` | empty | Auto power-off time (section 4.9). Empty: no change |
| `command_timeout` | 30 | Seconds for one command reply |
| `data_timeout` | 60 | Seconds without image data before a scan error |
| `frame_timeout` | 120 | Maximum seconds for one reply |
| `side_timeout` | 600 | Maximum seconds for one side (for a long page: both sides) |
| `max_side_mb` | 64 | Maximum size of one side, in MB |

### 16.2 `[service]`

| Key | Default | Description |
|---|---|---|
| `short_press_profile` | `default` | Profile for a short press |
| `long_press_profile` | empty | Profile for a long press. Empty: the short press profile. Example file: `receipt` |
| `front_slot_profile` | empty | Profile for a front slot job. Empty: the profile of the press |
| `continue_window` | 0 | Seconds for continue scanning (0 = off, maximum 3600) |
| `check_paper` | `true` | Start no job when there is no paper |
| `work_dir` | `/data` | Folder for the jobs |
| `log_level` | `INFO` | `DEBUG`, `INFO`, `WARNING` or `ERROR` |
| `deliver_partial` | `true` | Send the complete sheets before a scan error |
| `min_free_mb` | 200 | No scan with less free space in `work_dir` (MB) |
| `max_backlog` | 3 | No scan when this number of jobs waits for the PDF work or the first delivery |
| `stop_timeout` | 60 | Seconds for a clean stop. Less than `stop_grace_period` |
| `stop_scan_wait` | 15 | Seconds that a scan can continue at a stop. Less than `stop_timeout` |
| `retry_interval` | 60 | First wait (s) before a new attempt for unsent jobs |
| `retry_max_interval` | 3600 | Longest wait (s) between attempts |
| `retry_max_attempts` | 20 | Attempts before only `ix1300 retry` sends the job |
| `keep_raw` | `false` | Keep a copy of the scanner images (section 13.2) |
| `keep_raw_max_jobs` | 20 | Number of `raw` folders to keep |
| `trace_dir` | empty | Folder for the protocol trace. Empty: off |
| `trace_payload_bytes` | 256 | Longest data in a trace line (bytes) |
| `trace_max_mb` | 50 | Size of a trace file before a new file starts |
| `trace_total_mb` | 500 | Size of all trace files in `trace_dir`. The oldest files go first |

### 16.3 `[pipeline]`

| Key | Default | Description |
|---|---|---|
| `blank_method` | `auto` | `pixels` (decode a gray copy), `size` (JPEG size only), `auto` |
| `blank_ink_ratio` | 0.004 | A side with less dark pixels than this part is blank |
| `ink_level` | 110 | Gray levels below this value are print |
| `paper_level` | 225 | Gray levels above this value are paper |
| `blank_bytes_per_pixel` | 0.01 | For `size`: JPEG bytes for each pixel of a blank side |
| `receipt_ratio` | 2.0 | Length divided by width above this value: a receipt |
| `rotate` | `auto` | `auto` (tesseract), 0, 90, 180, 270 |
| `osd_min_confidence` | 2.0 | Lowest tesseract confidence for a rotation |
| `deskew` | `false` | Straighten the pages (needs Pillow) |
| `ocr` | `false` | Add a text layer with tesseract |
| `ocr_lang` | `eng` | OCR language(s) |
| `title_format` | `Scan {profile} %Y-%m-%d %H:%M:%S` | Document title (`strftime`, `{profile}` = profile name) |
| `filename_format` | `scan-%Y%m%d-%H%M%S.pdf` | File name of the PDF |
| `jpeg_quality` | 90 | JPEG quality when the service encodes a page again |
| `workers` | 1 | Threads for the page work (1 to 16) |
| `max_memory_mb` | 64 | JPEG data in memory (section 9) |
| `max_decode_mb` | 192 | Largest memory for the deskew of a long page |
| `tesseract_timeout` | 60 | Seconds for each tesseract run |

### 16.4 `[profiles.NAME]`

Refer to section 5.1.

### 16.5 `[outputs.NAME]`

| Type | Keys |
|---|---|
| All | `type`, `retries`, `retry_delay`, `timeout`, `max_time`, `verify_tls` (section 6.4) |
| `directory` | `path`, `file_mode`, `dir_mode` (section 6.1) |
| `webhook` | `url`, `field`, `headers`, `fields` (section 6.2) |
| `paperless` | `url`, `token`, `tags`, `receipt_tags`, `correspondent`, `receipt_correspondent`, `document_type`, `receipt_document_type`, `warning_title_suffix` (section 6.3) |

---

## Technical notes

These notes are for developers. The full protocol is in
[PROTOCOL.md](../PROTOCOL.md).

- The service finds the scanner with a UDP search on port 52217 (a broadcast,
  and a unicast to `scanner.ip`). It tries a reserve only after the scanner
  answers.
- The scanner sends each button event 3 times with one sequence number. The
  service uses only the first. After a job, it drops the button messages that
  came during the job. It accepts only 48-byte messages with a known code.
- After each sheet, the service asks for the next sheet. The job ends when the
  scanner answers "no paper". The paper sensors do not show the end, because
  the scanner pulls the next sheets in early.
- The stop of a scan uses the commands D8 (cancel), E0 and D6 (end of job).
  E0 after D8 makes the scanner feed the other sheets out without a scan.
  D8 and D6 alone leave paper in the paper path.
- The service threads are: scan (main), keepalive, worker (PDF), retry, and one
  thread for each output.
- The auto power-off time is the command E6. The double-feed setting is in the
  D4 data.
