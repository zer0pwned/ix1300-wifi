# ix1300: scan service for the ScanSnap iX1300 on Linux

This service replaces ScanSnap Home for one task. You put paper in the scanner and push the Scan button. The service scans over Wi-Fi, makes a PDF, and sends the PDF to Paperless-ngx, to an HTTP webhook, or to a directory.

The service runs in Docker on a Linux host in the same network as the scanner.

## 1. How it works

1. The service sends a search probe (UDP 52217, broadcast, and unicast to `scanner.ip` if set). It tries a reserve only after the scanner answers. The reply also refreshes the ARP entry of the scanner on the host. While the scanner is off, the service logs one line, then "still waiting for the scanner" each 10 minutes.
2. It reserves the scanner with the registered host ID. It keeps the reserve with keepalive packets each 15 s (up to 3 packets in each interval, because the scanner Wi-Fi uses power save). After 3 intervals without a reply, it reserves again. After the POWER OFF trigger (auto power-off after some hours), it waits 20 s before the next reserve attempt.
3. The scanner sends a UDP packet to the service when you push the Scan button. A short press and a long press can use different profiles (refer to "Button gestures" in section 4.1). The scanner sends each event 3 times with one sequence number: the service uses only the first. After a job, it drops the scan button packets that came during the job. The service accepts only 48-byte packets with a known code from the reserved scanner.
4. Before the scan, the service checks the free disk space (`min_free_mb`) and the number of waiting jobs (`max_backlog`). If one is not correct, it logs an error and does not start the scan. The paper stays in the chute. The service scans all sheets in the paper chute (duplex), one job for each button press. The scanner feeds the full stack in one job, and there is no clean stop after a number of sheets. **Load only the sheets that you want to scan.** After each sheet, the service asks for the next sheet (E0). The job ends only when the scanner answers "no paper". The hopper bit in C2 does not show the end: the scanner pulls the next sheets into the paper path early, and then C2 shows an empty hopper. The reserve stays for the next job.
5. A sheet counts only when both sides end correctly (EOM). The service writes each complete sheet to disk (`work_dir/jobs/<job ID>`), and then sends it to the page workers, while the scan continues: blank test, size measurement, rotation.
6. At the end of the job, the service writes the PDF into the job directory and sends it to the outputs. It removes the job directory only after all outputs have the PDF. Refer to section 5.

If an error (double feed, jam, paper lost in the middle of a sheet) stops the scan, the service never uses the incomplete sheet. If no sheet is complete, it sends nothing. With `deliver_partial = true`, it sends the complete sheets before the error, with a warning. With `false`, it keeps them in `work_dir/failed`.

## 2. One-time setup over USB

Do these steps one time. You need a Linux computer with the scanner on USB. The scanner must be on (open the cover).
The commands need `libusb-1.0` (in the Docker image), and root rights for the USB device (`--user root --privileged`). Only these one-time commands need root. Stop `saned` and other scanner programs first.

### 2.1 Connect the scanner to Wi-Fi

```
docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-wifi-setup --ssid MyNetwork
```

The command asks for the Wi-Fi key. You can also give `--key-file FILE` (key on line 1) or the variable `IX1300_WIFI_KEY`.
The command writes the network, sets the "client" boot mode, and waits until the scanner has an IP address. Only WPA/WPA2-PSK is supported.
Give the scanner a fixed IP address in your router (DHCP reservation).

### 2.2 Register the host ID

The scanner keeps one registered host ID (8 bytes). A reserve with another ID gets the result -6.

```
docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-register-host --host-id 0123456789ABCDEF
```

- Without `--host-id`, the command uses `scanner.host_id` from the configuration.
- `usb-register-host --show` shows the registered ID only.
- You can use any 16 hex digits. Use the same value in `scanner.host_id`. To make a random ID: `python3 -c "import secrets; print(secrets.token_hex(8).upper())"`.
- To share the scanner with a Mac that has ScanSnap Home, use the Mac ID. It is the `HostID` value in `~/Library/Application Support/PFU/ScanSnap/PfuSsHostID.plist`. Then you do not need this step, and ScanSnap Home does not register again.

#### Registration over Wi-Fi (not verified on the scanner)

You can also register the host ID over Wi-Fi, without USB. The scanner must be on and on the network. Stop the service first (the scanner accepts one reserve only).

```
docker compose run --rm ix1300 wifi-register-host --show
docker compose run --rm ix1300 wifi-register-host --host-id 0123456789ABCDEF
```

- `--show` shows the host ID of the last owner (discovery reply), the registered ID (C2), and the DC data (if a mode-2 reserve works). It changes nothing.
- Without `--host-id`, the command uses `scanner.host_id` from the configuration.
- The command asks for a confirmation. Type `yes`. Without a terminal, give `--yes`.
- Sequence: read the registered ID (C2), reserve with mode 2 and the new ID, clear the old ID (only if the scanner has an ID), register the new ID, release, then verify with C2 and a normal reserve. The command shows each step. It does each step one time.
- The sequence comes from the vendor software. It was not tested on the scanner. If the scanner refuses the mode-2 reserve, use `usb-register-host`.

`scanner.auto_register = true` makes the service do the same registration when a reserve gives -6, at most once an hour. The service logs a WARNING. The default is `false`.

**Caution:** the scanner keeps one registered host ID. A registration (over USB or over Wi-Fi) replaces the ID of any other computer. ScanSnap Home on that computer stops to work with the scanner until it registers again. ScanSnap Home registers again at its next connection, and then this host gets -6 again. With `auto_register = true` on both sides, the two computers take the scanner from each other (at most once an hour from this side). To share the scanner, use the ID of the other computer (see above).

Caution: do not send scan commands over USB while the service uses the scanner over Wi-Fi. After a USB scan command, the network reserve gives -4 until you close and open the cover.

## 3. Run the service

```
mkdir -p config data
cp config.example.toml config/config.toml     # then change the values
sudo chown 1000:1000 data && chmod 700 data
docker compose up -d --build
docker compose logs -f
```

The service runs as the user `ix1300` (UID and GID 1000), not as root. The `data` directory must belong to this user. For a different owner, set `IX1300_UID` and `IX1300_GID` in `.env` and build again. The service makes its files with mode 0600 and its directories with mode 0700.

`docker-compose.yml` sets a memory limit (`mem_limit`, default 768m; change it with `IX1300_MEM_LIMIT=400m` in `.env`), `OMP_THREAD_LIMIT=1`, and `stop_grace_period: 90s`. At `docker compose stop`, the service stops to accept button presses. A scan continues for `stop_scan_wait` seconds (15), then the service stops it: D8 (cancel read), E0, and D6 (end job). **Caution: E0 after D8 makes the scanner feed the rest of the stack out without a scan** (measured on the scanner). The service waits until the paper sensors show an empty paper path (at most 120 s, and not after the stop deadline), then sends D6. It keeps the complete sheets, logs "the other sheets in the scanner (if any) may have been ejected without a scan", and adds this warning to the document. D8 and D6 alone are not a better stop: the motor stops at once, and paper stays stuck in the paper path. The service releases the scanner, and makes the PDF of the complete sheets. If a READ still waits for data 10 s before the end of `stop_timeout` (60 s), the service stops the READ, sends D8 and E0, waits at most 5 s, sends D6, and releases the scanner. The sheet of that READ is not in the PDF. If the work takes longer than `stop_timeout`, the job stays on disk, and the next start continues it. Caution: a READ that waits in one network operation stops only after that operation (`frame_timeout`).

If a thread of the service stops because of an error, the service stops with exit code 1, and Docker starts it again (`restart: unless-stopped`). The threads are: scan (main), keepalive, worker (PDF and first delivery), and retry (new attempts for `work_dir/unsent`).

The container uses `network_mode: host`. The scanner sends UDP replies and button packets to the host IP address. The trigger port (default UDP 55265) must be open in the host firewall.

Useful commands:

```
docker compose run --rm ix1300 discover               # find scanners
docker compose run --rm ix1300 status                 # state, registered host ID, paper
docker compose run --rm -v $PWD/data:/out ix1300 scan --profile default --out /out/test.pdf
```

`scan --send` also sends the PDF to the outputs of the profile. `scan --raw DIR` also writes the JPEG sides. `scan` gives the exit code 5 if an error stopped the scan; the PDF then has the complete sheets before the error. `scan` uses the same job directories as the service (`service.work_dir`, section 5). If the PDF, the file or an output fails, or if `deliver_partial = false` stops the job, the sheets stay in `work_dir/failed` (or the PDF in `work_dir/unsent`). `scan` shows the directory. Use `retry` or `retry --failed` to continue.
Stop the service before you use `scan`: the scanner accepts one host only.

`settings --show` reads the auto power-off time from the scanner. `settings --power-off MINUTES` sets it (`0` = off, else 15 to 1890 in steps of 15) and reads it back. Stop the service before you use `settings`.

```
docker compose run --rm ix1300 settings --show
docker compose run --rm ix1300 settings --power-off 240
```

`retry` sends the jobs in `work_dir/unsent` again (all failed outputs, also after a permanent error). `retry --failed` first makes the PDF again from the sheets in `work_dir/failed`. `retry --list` shows the jobs.

```
docker compose run --rm ix1300 retry --list
docker compose run --rm ix1300 retry --failed
```

Build arguments (Dockerfile): `WITH_TESSERACT=0` makes a smaller image without rotation and OCR. `TESSERACT_LANGS="deu fra"` adds OCR languages. `WITH_PILLOW=0` removes Pillow (no deskew).

## 4. Configuration

The file is TOML (`config.example.toml` has all values and comments). A JSON file is also possible. The service checks all values before it starts. It stops with "config error" for an unknown section or key (for example `[pipline]`), a wrong type, a fraction where an integer is necessary (`dpi = 300.9`), or a value out of range.
Environment variables change single values: `IX1300_<SECTION>__<KEY>`, for example `IX1300_SCANNER__IP=192.0.2.212` or `IX1300_OUTPUTS__PAPERLESS__TOKEN=abc`. The type of the setting decides how the service reads the value: numbers, `true`/`false`, lists (`[1,2]`) and tables as JSON. Tokens, URLs, addresses and header values stay text exactly as given (also `123e40` or `null`). `IX1300_<SECTION>__<KEY>_FILE=/path` reads the value from a file, for example a Docker secret.

`scanner.power_off_minutes` sets the auto power-off time of the scanner [NV]. Empty (default): the service does not change the scanner value. `0`: the scanner does not turn off. `15` to `1890` in steps of 15 minutes: the scanner turns off after this time without a job (factory value: 240). After each reserve, the service reads the value and writes it only when it is different. Then it reads the value again to make sure. If an error occurs, the service logs a warning and continues. Only a scan job starts the idle time again. The keepalive packets do not.

Limits for bad or slow data from the network (section `[scanner]`): `frame_timeout` (120 s for one reply), `side_timeout` (600 s for one side), `max_side_mb` (64 MB for one side), `data_timeout` (60 s without image data). A reply can be at most the requested length plus 64 bytes.

`service.keep_raw = true` keeps a copy of the original sheets of each job (after the height fix, before all other work) in `work_dir/raw/<job ID>`, with `raw.json`: the profile and pipeline settings, the paper path (`top` or `front`), the decoder and OSD programs, and for each side the blank decision and two records. `original` has the size and the measured values of the scanner JPEG (the input of the blank test and the side choice). `delivered` has the page in the PDF: size after crop and deskew, measured values of the cut page (if measured), crop box, measured skew, `deskew_applied`, and the final rotation. `delivered` is `null` for a side that is not in the PDF. Only the newest `keep_raw_max_jobs` (20) directories stay. Caution: the copies contain the full documents.

### 4.1 Profiles

| Key | Values | Default |
|---|---|---|
| `dpi` | 150, 200, 300, 600 | 300 |
| `color` | `auto`, `color`, `gray` | `auto` |
| `duplex` | `true`: both sides. `false`: one side for each sheet, the side with more print | `true` |
| `paper_face` | order only. `down`: back side first (printed side down in the chute, normal). `up`: front side first | `down` |
| `crop` | cut the page to the paper: `auto` (receipts only), `true`, `false` | `auto` |
| `blank_skip` | remove blank sides | `true` |
| `length` | `auto` (up to 895 mm), `long` (long page mode, up to 3,000 mm, top chute only) or a length in mm (10 to 895) | `auto` |
| `rotate` | `auto`, 0, 90, 180, 270 (empty: `pipeline.rotate`) | empty |
| `deskew`, `ocr` | `true`/`false` (empty: the pipeline value) | empty |
| `receipt` | `auto`, `yes`, `no` | `auto` |
| `double_feed` | double-feed detection: `true` (on), `false` (off, for example for envelopes or sheets with a label) [NV] | `true` |
| `outputs` | names of outputs. Empty list: all outputs | `[]` |

`service.short_press_profile` and `service.long_press_profile` select the profile for each press. `service.front_slot_profile` (empty by default) selects the profile for a front slot job.

A button press always scans the whole stack: the scanner feeds all sheets in the chute in one job. There is no page limit. Load only the sheets that you want to scan.

**Button gestures:**

| Gesture | Profile |
|---|---|
| Short press (push and release) | `service.short_press_profile` |
| Long press: hold the Scan button until it flashes blue (about 3 s), then release | `service.long_press_profile` (empty: the short press profile) |
| Front slot: put one sheet into the front slot (chute empty), then a short or long press | `service.front_slot_profile` (empty: the profile of the press) |

**Front slot (return path):** before each job, the service reads the paper sensors (C2) one time. Paper in the chute gives a normal job. An empty chute and paper at the feed sensor gives a front slot job: the service scans the one sheet (both sides), and the sheet comes back out at the front. The log shows the paper path (for example `scan button (short press): profile default, front slot`). The front slot takes one sheet at a time, and no long paper: a receipt longer than an A4 sheet jams. The job manifest, `raw.json`, the webhook (field `paper_path`) and the document data in the manifest record the paper path: `top` or `front`.

**Long receipts (long page mode):** `length = "long"` scans documents up to 3,000 mm (the limit of the scanner) from the top chute. `config.example.toml` sets it in the `receipt` profile. So hold the Scan button until it flashes blue (long press): the receipt profile then scans in long page mode. The short press (`default` profile) keeps `length = "auto"` (up to 895 mm). Rules and limits:

- Top chute only. If the sheet is in the front slot, the job uses the automatic length (up to 895 mm), and the log shows a warning. The front slot does not take long paper.
- `dpi` must be 150, 200 or 300. The service does not accept `length = "long"` with 600 dpi: a JPEG cannot keep the 70,866 lines of a 3,000 mm page at 600 dpi.
- The scanner sends both sides of the sheet together (mutual transfer, READs of 128 KB). The log shows `long page mode: E9 length 0x23680, D4 byte 13 = 0x80, mutual READ of 0x20000 bytes` at the start of the job.
- One side of 3,000 mm at 300 dpi has 35,433 lines (2,592 pixels wide). Its JPEG is about 1 to 3 MB for a receipt, and up to about 35 MB for a full colour page. `scanner.max_side_mb` (64) is sufficient. `scanner.side_timeout` (600 s) is the limit for the two sides of one sheet.
- Guide the long paper with your hand, and keep the paper guides against the receipt edges. A skewed long receipt can jam.
- The job manifest and `raw.json` record the mode (`long_page`, `profile_settings.length`) and, for each side, the READ counts (`reads`).
- The long page mode is not verified on the scanner. Refer to section 9 and PROTOCOL.md, "Long page mode".

With no paper in the chute and no sheet in the front slot, a press starts no job (`service.check_paper = true`). The scanner sends one event for each press. A long press works only with paper in the chute.

The scanner always reads both sides. For a simplex profile, the service keeps the side with more print (ink ratio), so the paper can face up or down. `paper_face` is only the order of the pages (and the choice when the service cannot decode the JPEG). If the measurement of a side fails, the service does not know its print: that side stays in the PDF (with a warning), also when the other side is blank.

### 4.2 Pipeline

- **Blank sides:** the service decodes a small gray copy (Pillow, or the `djpeg` program). A side is blank if its dark pixels are less than `blank_ink_ratio` (0.4 %) of its paper pixels. Light show-through from the other side is not "dark". Without a decoder, only flat pages are found (JPEG size). If all sides look blank, the service keeps the side with the most print of each sheet.
- **Rotation:** with `rotate = "auto"`, tesseract finds the page orientation. The PDF page gets a `/Rotate` value. The JPEG data does not change.
- **Deskew:** the service measures the angle on a 75 dpi gray copy: from the paper edges (paper brighter than the gray backing), else from the lines of print. Pillow turns the page (one new JPEG encode). Without Pillow, no deskew.
- **Crop:** cuts the page to the outer paper bounds (torn or slanted receipt ends stay in). The gray level of the scanner backing is not the same on all sides (206 to 226). So the service finds the paper level for each image: the lowest point of the gray histogram below the paper peak (at least `paper_level`). The box grows only into columns where at least 10 % of the pixels are paper. If print is near an edge (up to 5 mm out), the box grows to include it. Then the service adds a 2 mm margin. After a deskew, it measures the box on the turned page. After a deskew, Pillow cuts the turned page. Else `jpegtran` cuts it without a new encode (the edges move to 8 or 16 pixel steps), or Pillow if jpegtran is not available. A cropped receipt PDF is about 30 to 40 % smaller.
- **OCR:** `ocr = true` makes a searchable PDF with tesseract (`ocr_lang`). For a rotated page, the service turns the pixels with `jpegtran` (or Pillow). If it cannot turn a page, it makes the normal PDF without a text layer.
- **PDF:** without OCR, the service embeds the scanner JPEG bytes directly (no new encode). The page size comes from the DPI.
- **Memory:** the service keeps only the JPEG bytes, one page decode at a time, at 75 dpi (blank test, paper size) and 150 dpi gray (rotation). `max_memory_mb` (default 64) is the JPEG data of all jobs together in memory (also the pages after crop and deskew). More data stays in files in `service.work_dir`. After the PDF, the service drops the JPEG data of the job, then gives back its memory. The PDF goes into a file and the outputs read it in parts. `workers` (default 1) is the number of threads for the page work. tesseract runs with one thread (`OMP_THREAD_LIMIT=1`) and a time limit (`tesseract_timeout`). Refer to section 4.5.
- **Long pages** (longer than 900 mm, only in long page mode): a full decode of a 3,000 mm page needs much memory (2,592 x 35,433 pixels: 88 MB gray, 350 MB colour for each Pillow copy), and `jpegtran` keeps the full page in memory (measured: 256 MB gray, 383 MB colour). So the service first cuts a long page to the paper with `djpeg -crop | cjpeg` (one new JPEG encode, about 2 MB of memory). Then it straightens the narrower page with Pillow only if the estimate (3 copies of the decoded page) is not more than `max_decode_mb` (192 MB). A 3,000 mm gray receipt, 80 mm wide, needs about 96 MB. The same receipt in colour needs about 383 MB: the service does not straighten it, and `raw.json` shows `deskew_skipped`. The service sets the Pillow limit `MAX_IMAGE_PIXELS` to 120,000,000 pixels: the largest scanner page (600 dpi, 895 mm) has 110 million pixels, a 3,000 mm page at 300 dpi has 92 million. Pillow rejects images with more than 240 million pixels.
- **Receipts:** a document is a receipt if paper length / paper width > `receipt_ratio` (2.0) on a page. The service measures the paper on the gray backing, not the 219 mm scan window. The paper width is the distance between the outer left and right paper edges (median of the rows, corrected for the skew). Print, shade and wrinkles in the paper do not make the paper narrower. The blank test uses the paper and print pixels only. Example: the 80 mm receipts in `scans/receipts` measure 78.6 to 79.2 mm on both sides. Without a decoder, the service uses the image size.

### 4.3 Outputs

**Paperless-ngx** (`type = "paperless"`): `POST <url>/api/documents/post_document/` with `Authorization: Token <token>`. If the scan has a warning (for example a probable jam), the title gets `warning_title_suffix` (default " [scan warning]"). Fields: `document`, `title`, `created`, `tags` (`tags`, plus `receipt_tags` for receipts), `correspondent`, `document_type` (or `receipt_correspondent`, `receipt_document_type` for receipts). Use 0 for "not set".

**Webhook** (`type = "webhook"`): multipart POST to `url`. The PDF is in the field `field` (default `file`). Other fields: `title`, `profile`, `pages`, `receipt`, `created`, `job_id`, `paper_path` (`top` for the chute, `front` for the front slot), `warnings`, and the values in `fields`. `headers` adds HTTP headers (for example a token).

**Directory** (`type = "directory"`): writes the PDF into `path`. It never replaces a file: the second file gets the name `name-1.pdf`. On a file system without hard links (some network shares), the service makes the file with an exclusive create and then copies the PDF into it. Only a crash during this copy can leave a short file; the job then stays in `work_dir` and the service sends it again. New files get `file_mode` (default `"0600"`), new directories `dir_mode` (default `"0700"`). Use for example `file_mode = "0640"` if other users must read the files.

Each output tries again `retries` times (`retry_delay` s, then 2 x, 3 x ...). An HTTP status 4xx (not 408 and 429) is a permanent error: no new attempt until you change the configuration. `timeout` applies to each network operation, `max_time` (600 s) to the full attempt. The service reads only the first 64 KB of a reply. The logs do not show tokens, header values or the reply text.

The outputs of a document work at the same time (one thread for each output). Thus a slow output does not delay the other outputs. Each output sends one document at a time. The new attempts for `work_dir/unsent` have their own thread. Caution: the worker thread makes the next PDF only after all outputs of the current document are done (at most `retries` attempts of `max_time` each).

### 4.4 Separate endpoints for each profile

Each profile can send its documents to its own outputs. You can have more than one output of the same type: each output has its own name. In this example, a short press sends documents to Paperless with tag 1. A long press sends receipts to Paperless with tag 7 and document type 3, and also to an expense webhook.

```toml
[service]
short_press_profile = "default"      # short press
long_press_profile = "receipt"       # long press: hold until the Scan button flashes blue

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

[profiles.default]                   # short press
outputs = ["paperless_docs"]

[profiles.receipt]                   # long press: hold until the Scan button flashes blue
duplex = false
crop = true
receipt = "yes"
outputs = ["paperless_receipts", "expense_webhook"]
```

A profile with an empty `outputs` list sends to all outputs. A name in `outputs` that has no `[outputs.NAME]` table is a configuration error. The outputs can also point to two different Paperless servers (each output has its own `url` and `token`).

### 4.5 Small computers

The values of this service were measured on a Raspberry Pi 5 with 8 GB only. A Raspberry Pi Zero 2 W (512 MB) ran out of memory with OCR and deskew: both decode the full page (a long receipt at 300 dpi is about 80 MB as pixels). Use this preset on a computer with less than 1 GB, and examine the memory use (`docker stats`) with your longest documents:

```
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

Also set `IX1300_MEM_LIMIT` below the free RAM. Without OCR and deskew, the service decodes only small gray copies (75 and 150 dpi), and tesseract OSD (rotation) uses one thread.

Long page mode on a small computer: the scan keeps about 2 x the JPEG size of the longest side in Python memory (measured with the emulator: 4.3 MB for a 3,000 mm receipt, 27 MB for a dense 3,000 mm colour page). The crop of a long page uses `djpeg` and `cjpeg` (a few MB). For deskew of long pages, set `pipeline.max_decode_mb` below the free RAM, or `deskew = false` in the receipt profile.

## 5. Jobs, recovery and delivery

The service keeps each job on disk, so that an error, a full disk at the receiver, or a restart does not lose a scan:

| Directory | Contents | What happens |
|---|---|---|
| `work_dir/jobs/<job ID>` | `manifest.json` and the original sheet JPEGs (written with fsync before the page work) | In work. After a stop or a crash, the next start makes the PDF again from the sheets and sends it. |
| `work_dir/unsent/<job ID>` | `document.pdf` and `manifest.json` with the status of each output | One or more outputs failed. The service tries the failed outputs again: first after `retry_interval` (60 s), then twice as long each time, up to `retry_max_interval` (1 h), for `retry_max_attempts` (20) attempts. Also at each start. `ix1300 retry` sends them at once. |
| `work_dir/failed/<job ID>` | the original sheets and `manifest.json` (fields `state` and `error`) | The PDF failed, `deliver_partial = false` stopped an incomplete job, or the originals must stay after the delivery (`state` "sent with missing pages" or "sent, originals kept"). `ix1300 retry --failed` makes the PDF again and sends it. |
| `work_dir/raw/<job ID>` | a copy of the sheets and `raw.json` | Only with `keep_raw = true`. |

The job ID has the time and a random part (`20261010-142501-3fa9c2`), so no job replaces another one. Remove old directories in `failed` by hand.

Delivery is **at least once**. If a receiver keeps a document but its reply is lost (for example a timeout), the service sends the document again later. Paperless-ngx then can show the document two times (Paperless can find duplicates by checksum). Each request has the job ID (header `X-Ix1300-Job`, and for the webhook the field `job_id`), so a receiver can find copies. For Paperless, "sent" means that Paperless accepted the upload (it gives a task ID, kept in the manifest). Paperless makes the document later.

If a page cannot be processed (for example a deskew error), the service uses the page as scanned and adds a warning. If a page cannot be used at all, the document has a warning, and the original sheets stay in `work_dir/failed` after the delivery. The manifest keeps this state (`missing`), so the originals also stay after a new attempt or a restart.

The manifest is not the only record of the sheets. A crash or a full disk can stop the service after it wrote a side file and before it wrote the manifest. Before the service removes or continues a job, it compares the directory with the manifest:

- A sheet with both side files goes into the job (the PDF has it). Its originals stay in `work_dir/failed` after the delivery (`recovered`).
- A side file without its other side goes into `orphans` in the manifest. The service does not remove it. Examine it, then remove the job directory by hand.

Each job has a lock (`.lock`). The service and `ix1300 retry` take the lock before they read the manifest, and keep it until the job is sent, moved or removed. Thus two processes never work on the same job.

## 6. Security

- The scanner protocol has no authentication and no encryption (TLS is not implemented). Each device in the network can send button packets and read the scan data on the network. Use the service only in a trusted network, or put the scanner and the host in their own network (VLAN) with a firewall.
- The service accepts button packets only from the reserved scanner address (48 bytes, a known code), and keepalive and search replies only from the scanner address. This is a filter, not an authentication: a device can send packets with a false source address.
- Set `scanner.host_ip` to bind the trigger port to one address. Open the trigger port (UDP 55265) only for the scanner.
- The service runs as a user without root rights. Its files have mode 0600 and its directories mode 0700. Scans are personal data: keep `data` private, and use `keep_raw` only for tests.
- Put tokens into a file with mode 0600 (`IX1300_OUTPUTS__PAPERLESS__TOKEN_FILE`) or into the environment, not into a configuration file that other users can read.

## 7. Troubleshooting

| Problem | Cause and remedy |
|---|---|
| Log: "host ID ... is not registered" (-6) | Another computer (ScanSnap Home) registered its own ID. Do step 2.2 again (USB, or `wifi-register-host`), or use the ID of that computer. `scanner.auto_register = true` registers the ID over Wi-Fi automatically (read the caution in 2.2). |
| Log: "scanner busy with a USB host or a stale job; close and open the cover" | The reserve gives -4 and the scanner shows device status 2. Close the cover and open it again. Do not use the scanner over USB at the same time. |
| Log: "scanner occupied by another host" (-4) | Another computer has the reserve. The service tries again (2 s to 10 s, `scanner.retry_min` and `retry_max`). |
| Log: "double feed: fan the paper and load it again" | The scanner found two sheets together. The service sends nothing for that sheet. Fan the stack, load it again and push Scan. |
| Log: "probable paper jam" | Over Wi-Fi, a jam can end a page without an error. The page is then shorter than the paper. After the last sheet ("no paper"), the service reads C2. If the exit sensor still shows paper, it adds this warning and sends the pages. Remove the paper and scan again. If C2 fails or gives a short reply, the warning is "jam status not known": examine the scanner. |
| Log: "may have been ejected without a scan" | The service stopped during a scan (`docker compose stop`). The scanner fed the rest of the stack out without a scan. The PDF has the complete sheets before the stop. Scan the other sheets again. |
| Long receipt jams or comes out skewed | Move the paper guides against the receipt edges. A skewed receipt can jam. |
| The scanner was off (auto power-off) and the service does not reserve it again | The service needs a reply to its search probe. Set `scanner.ip`, and check that UDP 52217 replies reach the host. The log shows "still waiting for the scanner" each 10 minutes. |
| No reaction to the Scan button | The service must have the reserve (log "reserved scanner"). Open the trigger port (UDP 55265) in the firewall. |
| "no scanner found" | Open the cover to turn the scanner on. Set `scanner.ip`: a busy scanner does not answer all broadcast probes. The service sends each probe 3 times, 0.5 s apart, and also a unicast probe when `scanner.ip` is set. |
| The host computer becomes slow or stops (swap) | Use `workers = 1`, a smaller `max_memory_mb`, `deskew = false`, `ocr = false`, and a `mem_limit`. |
| Blank pages stay in the PDF | Check that Pillow or `djpeg` is available (log line at start). Adjust `blank_ink_ratio`. |
| Pages upside down | Install tesseract (default image) or set `rotate = 180` in the profile. |
| Log: "scan not started: only N MB free" | Make space in `work_dir` (old jobs in `failed`), or change `min_free_mb`. Push the button again. |
| Log: "scan not started: N jobs wait for processing" | The outputs or the PDF work are slow. Wait, then push the button again. Change `max_backlog` if necessary. |
| Log: "trigger packet from ... ignored" | A packet came from an address that is not the scanner. Check `scanner.ip`, or find the device that sends to the trigger port. |
| Log: "PDF kept in .../unsent/..." | An output failed. The service tries again. Check the receiver, then use `ix1300 retry`. |
| "Permission denied" for `/data` | The `data` directory must belong to UID 1000 (or `IX1300_UID`). |

Use `-v` for debug logs: `docker compose run --rm ix1300 -v status`.

## 8. Tests

The tests use the Python standard library only. They use an emulator of the scanner (`tests/emulator.py`) on 127.0.0.1. If the system does not let the tests open a port, they use an in-memory network.

```
python3 -m unittest discover -s service/tests
```

`IX1300_TEST_NET=real` or `mem` selects the network. `IX1300_TEST_LOG=DEBUG` shows the logs.
`tests/data` has one synthetic receipt (front and back, 300 dpi), made by `tests/make_synth_receipt.py`. The tests also use real scans in `../scans/receipts` and `../scans/raw` if these directories exist. Otherwise, the tests for real scans are skipped.

## 9. Not verified on the scanner

- `color = "color"` and `color = "gray"` (only `auto` was tested). 150, 200 and 600 dpi.
- `length` in mm. The long page mode (`length = "long"`): all of PROTOCOL.md, "Long page mode" (the E9 and D4 values, the mutual READs, the residue of the last chunk, the JPEG height of a long page, the pixel size after a mutual read). The long page crop (`djpeg -crop | cjpeg`) was tested on the Mac with libjpeg-turbo 3.2 only. The Pillow memory figures for long pages are estimates (Pillow was not available on the test computer).
- The stop during a scan (D8, E0, wait for an empty paper path, D6) was tested with the emulator only. The scanner behaviour after D8 was measured (PROTOCOL.md 8.6), but not the complete service sequence: the E0 reply after D8, the C2 sensor values during the eject, and the time to the empty path.
- Error paths of a job: D6 after a lost D5 reply, a release and a new reserve after a failed D6.
- The button packet is always exactly 48 bytes, and the scanner sends it from its own IP address.
- Memory use on the Pi Zero after these changes (the test measures Python memory with the emulator: 6 MB peak for a 22-side, 13.8 MB job).
- Host ID registration over Wi-Fi (`wifi-register-host`, `scanner.auto_register`): the mode-2 reserve with an unregistered ID, the DC commands, and their replies come from the vendor software only. The emulator decides some details (for example: DC op 0 needs a clear first when an ID is registered).
- Port1 and Port2 over TLS (the scanner shows TLS ports in some modes). The service does not implement TLS.
- The auto power-off setting (`scanner.power_off_minutes`, `settings` command, PROTOCOL.md 8.9) and `double_feed = false` (PROTOCOL.md 8.2). The command layouts come from the vendor software, not from a hardware test.
- The trigger codes other than the scan button (power on 0x30, power off, release requests).
- The JPEG files with the fixed height still contain the padded image data. libjpeg shows the warning "extraneous bytes before marker 0xd9". Ghostscript shows these pages correctly (tested with the sample receipts). Other PDF viewers were not tested.
