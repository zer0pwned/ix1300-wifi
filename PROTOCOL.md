# ScanSnap iX1300: USB setup and Wi-Fi scan protocol

This document describes the protocol that the service (`service/`) and the
SANE backend (`sane-backend/`) use. It is a description of the messages on
the USB cable and on the network. It does not describe the vendor software.

Labels:

- **[HW]**: we tested this item on a real iX1300 (firmware 0J00).
- **[NV]**: not verified on the scanner. The implementation uses this item,
  but no hardware test confirmed it.

All examples use the documentation address ranges (192.0.2.0/24) and false
example values (host ID `0123456789ABCDEF`).

## 1. The scanner

| Item | Value |
|---|---|
| USB ID | `04c5:162c` [HW] |
| INQUIRY | `FUJITSU ScanSnap iX1300 0J00` (vendor, product, firmware version) [HW] |
| Device name | `iX1300-<serial number>` [HW] |
| Power | Open the cover (top paper chute) to turn the scanner on. Close the cover to turn it off. The USB device then disconnects [HW] |
| Paper paths | Top chute (U-turn, stack of sheets, long paper) and front slot (return path, one sheet) [HW] |

## 2. USB transport [HW]

The scanner uses two bulk endpoints: `0x02` (out) and `0x81` (in).

Each command has three phases:

1. The host sends a 31-byte command block. Byte 0 is `0x43`. The SCSI CDB
   starts at offset 19. The other bytes are 0.
2. Data phase (optional): the host sends the data-out bytes, or reads the
   data-in bytes.
3. The host reads a 13-byte status block. Byte 0 is `0x53` ("S"). The
   SCSI status is `byte 9 & 0x3E`. 0 is "good".

Over USB, the stock SANE `fujitsu` backend scans correctly [HW]. Thus the
scan commands over USB are not part of this document. The commands below
are necessary only for the one-time setup.

| Command | CDB | Use |
|---|---|---|
| SEND DIAGNOSTIC | `1D 00 00 LL LL 00` (BE16 length) | Sends a setup message |
| RECEIVE DIAGNOSTIC | `1C 00 00 LL LL 00` (BE16 length) | Reads the reply of a setup message |
| GET HW STATUS | `C2 00 00 00 00 00 00 00 NN 00` | Sensors, buttons, host ID (section 2.1) |

Caution: do not send scan commands (for example OBJECT POSITION `31`) over
USB while a host uses the scanner over Wi-Fi. After such a command, the
network reserve gives -4 (occupied), and the discovery reply shows device
status 2. To clear this state, close the cover and open it again [HW].

### 2.1 GET HW STATUS (C2) [HW]

The reply has the same layout over USB and over the network (Port1).

| Byte, bit | Meaning |
|---|---|
| 2, bit 7 | Paper at the feed sensor (also set for the front slot) |
| 3, bit 7 | Chute empty (1 = no paper in the top chute) |
| 3, bit 0 | Paper at the exit sensor (also the front slot) |
| 4, bit 0 | Scan button, short press |
| 4, bit 5 | Scan button, long press |
| 0x20..0x27 | Registered host ID (8 bytes; `FF..FF` = no ID). Only in a reply of 0x30 bytes |

Paper path before a job [HW] (measured over Wi-Fi with one A4 sheet in the
front slot): byte 2 = `80`, byte 3 = `81` (chute empty, exit sensor set).
After the front slot job, the path is clear: byte 2 = `00`, byte 3 = `80`.
The exit bit (byte 3 bit 0) can stay set with no paper (seen after a halt).
Thus do not use the exit bit alone. Before a job:

- Byte 3 bit 7 = 0: paper in the top chute.
- Byte 3 bit 7 = 1 and byte 2 bit 7 = 1: a sheet in the front slot.
- Else: no paper (also with byte 2 = `00`, byte 3 = `81`).

These rules apply only before the first sheet. During a job the scanner
pulls sheets into the paper path (section 8.5).

The button bits show over USB. Over the network without a reserve, the
button bits did not change (60 s test) [HW]. Use the trigger packets
(section 6) for the button.

## 3. Setup messages over USB [HW]

### 3.1 Session

A setup session starts with `SETUP NET START` and ends with `SETUP NET END`.
Each tag is 16 ASCII bytes (padded with spaces). The host sends the tag with
SEND DIAGNOSTIC, and reads a 1-byte reply with RECEIVE DIAGNOSTIC.

- Reply `0x00`: OK.
- Reply `0x82` to `SETUP NET START`: the scanner refused the session. A
  power cycle clears this state.

### 3.2 SETUP NET CMD frame

1. Send SEND DIAGNOSTIC with this payload:

   | Offset | Size | Value |
   |---|---|---|
   | 0x00 | 16 | `"SETUP NET CMD   "` |
   | 0x10 | 2 | BE16: dataSize + 0x10 |
   | 0x12 | 2 | 0 |
   | 0x14 | 4 | LE32: ope code |
   | 0x18 | 4 | 0 |
   | 0x1C | 4 | LE32: offset (0) |
   | 0x20 | 4 | LE32: dataSize |
   | 0x24 | n | data |

2. Send RECEIVE DIAGNOSTIC for dataSize + 0x14 bytes.
3. If byte 0 of the reply is `0x01` (busy), wait 0.5 s. Then do step 2
   again. Do not send step 1 again.
4. The reply: byte 0 = flag, LE32 ope echo at +4, LE32 result at +8, data
   at +0x14.

Note: the ope code is little-endian at offset 0x14. Other byte orders and
offsets give no valid reply.

### 3.3 Ope codes

| Ope | dataSize | Use | Status |
|---|---|---|---|
| 0x0007 | 0x230 | Device details: name, version, MAC, Wi-Fi mode, link, IP addresses (text at reply +0x1BC), mask, gateway | [HW] |
| 0x000A | 0x10 | Functional version (text, for example `03.04`) | [HW] |
| 0x000B | 0xF4C | Saved Wi-Fi networks | [HW] |
| 0x000C | 0x28C | System settings. The reply has the encoded reserve password | [HW] |
| 0x0103 | 4 | Read the boot Wi-Fi mode (0 = client, 1 = scanner access point) | [HW] |
| 0x0104 | 4 | Read the current Wi-Fi mode | [HW] |
| 0x1006 | 0xF8 | Write one Wi-Fi network (WPA/WPA2-PSK) and connect | [HW] |
| 0x1106 | 4 | Write the boot Wi-Fi mode and save it | [HW] |

Results: 0 = OK. 0x10 = wrong size or wrong ope. 0x1000 = wrong key (the
WPA handshake failed) [HW]. Ope 0x1108 gives 0x10 over USB [HW].

### 3.4 Ope 0x1006 data (248 bytes) [HW]

| Offset | Size | Field |
|---|---|---|
| 0x00 | 33 | SSID, NUL padded (32 bytes maximum) |
| 0x24 | 8 | Authentication: `WPA2`, `WPA`, `MIX`, `OPEN`, `SHARED` |
| 0x2C | 8 | Cipher: `AES`, `TKIP`, `AUTO`, `WEP`, `NONE` |
| 0x34 | 193 | Key, encoded (see below) |

Key encoding: for each byte `b` at index `i` of the key, write the 3 decimal
digits of `b + 11 + K[i % 16]`, with `K = "pFusCANsNapFiPfu"`. Example: the
key `1234` gives `172131179178`.

A key of 64 hex digits is a raw PSK. A shorter key is a passphrase.
The scanner saves the settings also when the connection fails.

Only `WPA2`/`AES` was tested on the scanner [HW]. The other values are [NV].

### 3.5 Wi-Fi setup procedure [HW]

1. `SETUP NET START`.
2. Ope 0x1006 with the SSID, `WPA2`, `AES`, and the key. Expect result 0.
3. Ope 0x1106 with the data `00 00 00 00` (boot mode = client). Expect
   result 0.
4. Optional: ope 0x0103. Expect 0.
5. Read ope 0x0007 until the IP address is not `0.0.0.0`.
6. `SETUP NET END`.

Without step 3, the scanner connects, but after a power cycle it starts its
own access point again (the factory boot mode is 1). The Wi-Fi LED is then
yellow. With step 3, the LED is blue after a power cycle, and the scanner
connects to the network again.

### 3.6 Host ID registration [HW]

The scanner keeps one registered host ID (8 bytes). A reserve over the
network with another ID gets the result -6 (section 5.3). Each computer can
use a random 64-bit value. The MAC address is not necessary.

To make a new ID (16 hex digits):

```
python3 -c "import secrets; print(secrets.token_hex(8).upper())"
```

- Read the ID: GET HW STATUS with 0x30 bytes
  (`C2 00 00 00 00 00 00 00 30 00`). The ID is at bytes 0x20..0x27.
  `FF..FF` = no ID.
- Write the ID over USB:
  1. `SETUP NET START`.
  2. `CLR HOSTID ADATA` (16-byte tag, 2-byte reply). Send it only if an ID
     is registered.
  3. `REG HOSTID DATA` + the 8-byte ID + 8 zero bytes (2-byte reply).
  4. `SETUP NET END`.

  A 2-byte reply is OK if `byte 0 & 0x7F` is 0.

Caution: the vendor software on another computer registers its own ID when
it connects. Then the reserve of your host gets -6 again. You can also use
the ID of that computer, if you know it.

#### Registration over Wi-Fi [NV]

This sequence comes from the vendor software (ScanSnap Home). It was not
tested on the scanner. The service implements it (`wifi-register-host`,
`scanner.auto_register`).

1. Read the registered ID with GET HW STATUS (C2 with 0x30 bytes). C2
   works without a reserve (section 7.3).
2. Reserve (section 5.3) with mode (+36) = 2 and the new host ID at +108.
   The vendor software uses mode 2 with an ID that is not registered yet.
   It is not known if the scanner accepts it.
3. Port1 `DC 00 03 00 00 00 00 02` (clear, no data-out, 2 bytes in). The
   vendor software sends it only if the scanner has an ID.
4. Port1 `DC 00 00 00 00 10 00 02` (register) with 16 bytes data-out: the
   8-byte ID + 8 zero bytes. 2 bytes in.
5. Release (section 5.4).
6. Reserve with mode 0 and the new ID, then release (verify).

CDB of DC (HostIDOperation): `DC 00 OP 00 00 WL 00 RL` (WL = data-out
length, RL = data-in length). Byte 0 of the data-in is the result. The
service accepts the result if `byte 0 & 0x7F` is 0 (as on USB).

| OP | CDB | Data-out | Data-in | Meaning |
|---|---|---|---|---|
| 0 | `DC 00 00 00 00 10 00 02` | 16 | 2 | Register |
| 1 | `DC 00 01 00 00 02 00 18` | 2 (index?) | 24 | Read the data |
| 3 | `DC 00 03 00 00 00 00 02` | 0 | 2 | Clear |
| 4 | `DC 00 04 00 00 00 00 04` | 0 | 4 | Read the length |

## 4. Network overview [HW]

| Item | Value |
|---|---|
| Key (bytes 0..3 or 4..7 of each message) | `56 45 4E 53` |
| Interface version | 0x0010 |
| Discovery and keepalive | UDP 52217 on the scanner |
| Port1 (commands) | TCP 53218 (the discovery reply gives the port) |
| Port2 (reserve and release) | TCP 53219 (the discovery reply gives the port) |
| Trigger port (button events) | A UDP port on the host. The host sends it in the reserve. Default 55265 |
| Password | Not set in the test (discovery reply +12 = 0) |
| TLS | Not implemented. The discovery reply has a TLS flag (+36 bit 3) [NV] |

All integers on the network are big-endian.

When a TCP connection to Port1 or Port2 opens, the scanner sends a 16-byte
greeting: BE32 0x10, the key, BE32 status 0, BE32 0 [HW].

## 5. Discovery, reserve and release

### 5.1 Search and keepalive packet (32 bytes) [HW]

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | Key |
| 4 | 4 | BE32: 0 = search, 1 = keepalive |
| 8 | 4 | Host IPv4 address |
| 12 | 6 | Host MAC address |
| 18 | 2 | 0 |
| 20 | 4 | The service sends (trigger port - 1). The SANE backend sends 0. The scanner accepts both |
| 24 | 2 | BE16: interface version 0x0010 |
| 26 | 6 | 0 |

The host sends a search to the broadcast address or to the scanner address.
A busy scanner does not answer all broadcast probes. Send each probe 3
times, 0.5 s apart [HW].

The scanner sends a keepalive reply (12 bytes: key, `80 01`, ...) only to the
host that has the reserve. Without a reserve, there is no reply [HW].

### 5.2 Search reply (144 bytes) [HW]

The scanner sends the reply to the source port of the probe.

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | Key |
| 4 | 2 | BE16 status 1 |
| 6 | 2 | BE16 status 2 (`FFFF`: not a valid reply) |
| 12 | 4 | Password flag (0 = no password) |
| 16 | 4 | Scanner IPv4 address |
| 20 | 4 | BE32 Port1 |
| 24 | 4 | BE32 Port2 |
| 28 | 6 | Scanner MAC address |
| 36 | 1 | Flags (bit 3 = TLS [NV]) |
| 37 | 1 | Flags |
| 38 | 2 | BE16 device status (2 = busy with a USB host or a stale job) |
| 40 | 64 | Device name (text) |
| 104 | 16 | Product name (text) |
| 120 | 4 | IPv4 address of the last owner of the reserve |
| 124 | 8 | Host ID of the last owner |
| 132 | 12 | Serial number (text) |

### 5.3 Reserve (Port2, 128 bytes) [HW]

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | BE32 message length (128) |
| 4 | 4 | Key |
| 8 | 4 | BE32 0x11 (reserve) |
| 16 | 6 | Host MAC address |
| 32 | 2 | BE16 interface version |
| 34 | 1 | Reserve timeout in seconds (the service uses 30) |
| 36 | 4 | BE32 mode (0 = normal scan) |
| 40 | 4 | BE32 password flag (0 = no password) |
| 44 | 4 | Host IPv4 address |
| 48 | 4 | BE32 trigger port (UDP, on the host) |
| 100 | 7 | Local time: BE16 year, month, day, hour, minute, second |
| 108 | 8 | Host ID |
| 116 | 4 | BE32 signed offset from UTC in seconds |

The other bytes are 0. The reply has 20 bytes: key at +4, signed BE32
result at +8.

| Result | Meaning |
|---|---|
| 0 | OK [HW] |
| -1 | General error |
| -2 | Password necessary |
| -3 | Wrong password |
| -4 | Scanner occupied by another host, or busy after a USB scan command [HW] |
| -5 | Interface version not supported |
| -6 | Host ID not registered [HW] |
| -7 | Host ID not valid |
| -8 | Sequence error |
| -9 | USB mode error |

Hold the reserve with a keepalive (section 5.1) each 15 s. The reserve
stays valid between scan jobs [HW]. The scanner Wi-Fi uses power save:
send up to 3 keepalive packets in each interval.

### 5.4 Release (Port2, 32 bytes) [HW]

BE32 length 32, key, BE32 0x12 at +8, host MAC at +16, BE32 force flag at
+24 (0 = normal). The reply has 16 bytes: signed BE32 result at +8 (0 = OK).
After the release, the discovery reply shows the host ID as the last owner.

## 6. Trigger packets (button and power events) [HW]

The scanner sends trigger packets to the trigger port of the host that has
the reserve. A packet has 48 bytes:

| Offset | Size | Value |
|---|---|---|
| 4 | 4 | Key |
| 8 | 4 | BE32 code |
| 12 | 4 | Condition bytes. Byte +14 bit 5 (`0x20`) = long press |
| 16 | 1 | Sequence number (1..255, then 1 again) |
| 17 | 1 | Reason |

| Code | Event | Status |
|---|---|---|
| 0x01 | Scan button | [HW] |
| 0x02 | Stop | [NV] |
| 0x03 | Cancel | [NV] |
| 0x10 | Power off | [HW] |
| 0x11 | Forced release | [NV] |
| 0x12 | Release request | [NV] |
| 0x20 | Power off in 5 minutes | [HW] (the implementations ignore it) |
| 0x30 | Power on | [NV] |

Rules [HW]:

- The scanner sends each event 3 times, 0.5 s apart, with the same sequence
  number. Use only the first copy. A copy can arrive late (for example
  during a scan job), so do not use a time limit for the duplicate test.
  Keep the last 16 sequence numbers for each code.
- The next event starts only after the 3 copies (about 1.0 s between two
  events). A fast double press arrives as two events about 1.0 s apart. No
  press was lost in 5 tests.
- The scanner sends one event for each press. There are no button-down or
  button-up events.
- Short press: the event comes at the release (+14 = `0x00`).
- Long press: hold the Scan button until it flashes blue (about 3 s). The
  event comes while you hold the button (+14 = `0x20`). The long press
  works with paper in the chute (3 of 3 tests).
- With no paper in the chute, a hold of 3 s just after a job runs the
  rollers, with no blue flash and no event. Read C2 before a job, and start
  no job with no paper (section 2.1).
- The button works also when the USB cable is connected.

Power off [HW]: the scanner turns itself off after about 4 hours with no
scan. A keepalive does not prevent this. The scanner first sends code 0x20
(5 minutes before), then code 0x10. No "power on" event comes when no host
holds a reserve. After the next power-on, the host must find the scanner
again with search probes (less than 60 s in the test). A TCP connection
without a search probe can fail with "No route to host" for a long time.

## 7. Port1 command frames [HW]

### 7.1 Request

A 64-byte header, then the data-out bytes:

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | BE32 total length (64 + data-out length) |
| 4 | 4 | Key |
| 8 | 4 | BE32 1 (one CDB) |
| 16 | 6 | Host MAC address |
| 24 | 4 | Host IPv4 address |
| 32 | 4 | BE32 CDB length |
| 36 | 4 | BE32 data-in length |
| 40 | 4 | BE32 data-out length |
| 48 | 16 | CDB |

### 7.2 Reply

A 40-byte header, then the data-in bytes:

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | BE32 total length (40 + data length) |
| 4 | 4 | Key |
| 8 | 4 | BE32 frame status (0 = OK) |
| 12 | 4 | BE32 SCSI status: 0 = good, 2 = CHECK CONDITION, 8 = busy |
| 40 | n | Data-in |

A reply can have a few bytes more than the data-in length of the request
(for example the 2-byte page and sequence ID of a READ).

### 7.3 Commands without a reserve [HW]

INQUIRY (`12`, the same 96 bytes as on USB), INQUIRY VPD (`12 01 F0`), and
GET HW STATUS (`C2`) work without a reserve. REQUEST SENSE (`03`) without a
reserve closes the connection. With a reserve, REQUEST SENSE works.

The commands SET WINDOW and SCAN do not give images over the network. The
scan job uses the command flow in section 8.

## 8. Scan job [HW]

### 8.1 Command flow

| Step | CDB | Data |
|---|---|---|
| Cancel read (clear) | `D8 00 00 00 00 00` | none |
| Pre-read settings | `E9 00 00 00 00 00 00 20 00 00` | 32 bytes out (section 8.2) |
| Scan parameters | `D4 00 00 00 50 00` | 0x50 bytes out (section 8.2) |
| Status | `C2 ... 30 00` | 0x30 bytes in |
| Start job | `D5 00 00 00 08 08` | 8 bytes out (0), 8 bytes in |
| For each sheet: next sheet | `E0 00 00 00 00 00` | none |
| Read the front side | READ, window 0x00, page n | image data |
| Pixel size of the front side | READ data type 0x80 | 32 bytes |
| Read the back side | READ, window 0x80, page n + 1 | image data |
| Pixel size of the back side | READ data type 0x80 | 32 bytes |
| End job | `D6 00 00 00 00 00` | none |

The first scan over Wi-Fi: one A4 sheet, duplex, 300 dpi, automatic color,
7 s from the reserve to the release. All commands answered OK [HW].

If a command gives CHECK CONDITION, send REQUEST SENSE (`03 00 00 00 12 00`,
18 bytes). If a command gives status 8 (busy), send it again after a short
wait.

### 8.2 E9 and D4 data

These values work on the scanner [HW]. The meaning of the bytes without a
description is not known.

E9 (32 bytes): BE16 X resolution, BE16 Y resolution, BE32 window width
(0x2880 = 219.5 mm in 1/1200 inch), BE32 pre-read length (0x4350), `05 00`,
then 18 zero bytes.

D4 (0x50 bytes): 32 bytes of parameters, then a 48-byte window descriptor.

- Parameters: `00 03 01 01 88 00 C1 80 80 80 80 80 80 00`, 17 zero bytes,
  `30`. Byte 6 = `C1` selects the automatic color mode. Byte 1 = 3 is the
  only tested value.
- Window descriptor: +1 = `10` (JPEG); +2 BE16 X resolution; +4 BE16 Y
  resolution; +6 = `05` (color) or `02` (gray [NV]); +7 = `82` at 300 dpi,
  else `81`; +8 = `0D` at 300 dpi, `0A` at 600 dpi, else `05`; +10 BE32
  window width (0x2880); +14 BE32 window length; +18 = `04`; +30 = `01`.
- Window length: 0xA543 (895.5 mm) is the automatic length. The scanner
  finds the real end of the paper.

Only 300 dpi and the automatic color mode were tested on the scanner. The
other resolutions and color modes are [NV].

Front slot (return path) job [HW]: three values change. All other bytes
are the same as for the top chute.

| Field | Top chute | Front slot |
|---|---|---|
| E9 +8, BE32 pre-read length | `00 00 43 50` | `00 00 44 10` |
| D4 parameters, byte 1 | `03` | `06` |
| D4 parameters, byte 4 | `88` | `00` |

Front slot D4 parameters: `00 06 01 01 00 00 C1 80 80 80 80 80 80 00`,
17 zero bytes, `30`.

### 8.3 Image READ

CDB: `28 00 00 00 00 WW 30 00 00 00 PP SS`

- `WW`: window (0x00 = front, 0x80 = back).
- Bytes 6..8: BE24 data-in length (0x300000).
- `PP`: page ID (front = 2 x sheet index, back = front + 1, modulo 256).
- `SS`: sequence number of the READ in this side.

Each reply has a 2-byte prefix (page ID, sequence ID), then JPEG bytes. The
JPEG data of the side is the sum of the replies.

- The first READ often gives CHECK CONDITION with sense 3/80/13 (temporary
  no data). Wait 0.1 s, then send the same READ again.
- End of a side: CHECK CONDITION, then REQUEST SENSE with sense key 0 and
  EOM = 1. The data of that last READ reply is also image data.
- Each side is one baseline JPEG (for example 2592 x 3424 pixels, RGB, 1.1
  to 1.4 MB for an A4 page at 300 dpi).

### 8.4 Pixel size and height correction [HW]

The JPEG height is the window length (with automatic length: 10576 lines
at 300 dpi = 895 mm). The lines after the end of the paper are gray filler.

After each side, READ with data type 0x80 gives the real size:

CDB: `28 00 80 00 00 WW 00 00 20 00 PP 00` (32 bytes data-in; some replies
have the 2-byte prefix first).

| Offset | Value in the test | Meaning |
|---|---|---|
| +0 | 2592 | Width in pixels |
| +4 | 10576 | Padded height |
| +8 | 2592 | Paper width |
| +12 | 9856 | Real length in lines (834 mm) |
| +16 | 300, 300 | Resolution |
| +20, +24 | 2592, 9863 | A second width and length |

Write the real length (+12) into the height field of the JPEG SOF marker.
The JPEG then shows only the paper. The data of the filler lines stays in
the file. libjpeg shows the warning "extraneous bytes before marker 0xd9".

Test: a receipt of 825 mm scanned completely in 12 s (duplex, 300 dpi, 2
READ replies for each side, about 3.5 MB for each side).

### 8.5 End of the job and the paper stack [HW]

- The scanner feeds the whole stack in one job (D5 ... D6). A host cannot
  stop the feed when it stops to read.
- The scanner prefetches. With 3 sheets in the chute, C2 showed an empty
  chute after sheet 1, while sheets 2 and 3 were in the paper path. A host
  that ends the job on the chute bit loses these sheets: the scanner feeds
  them out without a scan.
- Rule: after each sheet, send E0 again. End the job only when E0 (or the
  first READ of a sheet) gives sense 3/80/03 (no paper). Then send D6.
- At that time, the C2 exit bit can be set without a jam.

- Front slot: after the one sheet, the next E0 gave CHECK CONDITION, sense
  3/80/03 (no paper), after 0.1 s [HW]. The same loop thus ends a front
  slot job after one sheet (section 8.9).

### 8.6 Stop with sheets left (D8) [HW]

Measured with 5 sheets in the chute, after sheet 1:

| Commands | Result |
|---|---|
| D8, no command for 15 s, then D6 | The motor stops at once. Sheet 2 stays in the paper path near the exit. Sheet 3 is at the pick roller. 2 sheets stay in the chute. D6 does not eject. C2: feed = 1, exit = 1 |
| D8, then E0, then D6 | A READ after D8 gives sense 5/24/00 (invalid field). E0 makes the scanner feed all remaining sheets out without a scan. The paper path is then empty |

A second test with the SANE backend: D8 + E0 + wait for C2 + D6 with 3
sheets ejected sheets 2 and 3 in 2.8 s. D8 + D6 with 2 sheets: the
prefetched sheet 2 still came out completely, and C2 then showed exit = 1
with no paper. The halt point thus changes with the position of the next
sheet.

There is no clean stop after N sheets. A host can leave paper in the path
(D8 only) or feed the rest out without a scan (D8 + E0). **Load only the
sheets that you want to scan.**

### 8.7 Sense codes

| Key/ASC/ASCQ | Meaning | Status |
|---|---|---|
| 0, EOM = 1 | End of a side (normal) | [HW] |
| 3/80/01 | Paper jam | [HW] |
| 3/80/02 | Cover open | [NV] |
| 3/80/03 | No paper (chute empty) | [HW] |
| 3/80/07 | Double feed | [NV] |
| 3/80/13 | Temporary no data: send the same READ again | [HW] |
| 5/24/00 | Invalid field (READ after D8) | [HW] |

### 8.8 Paper jams over Wi-Fi [HW]

A jam over Wi-Fi does not give an error. The page ends with a normal EOM at
the jam point (688 mm in the test), and the release is OK. After the jam,
the C2 exit bit (byte 3 bit 0) stays set with no paper. Read C2 after the
last sheet: a set exit bit then shows a probable jam.

A skewed long receipt can jam or drift. Move the paper guides against the
receipt edges.

### 8.9 Front slot (return path) [HW]

Measured over Wi-Fi: two front slot scans of one A4 sheet, 300 dpi, duplex.

- Detection: read C2 before the job (section 2.1). Chute empty and paper at
  the feed sensor: a sheet is in the front slot.
- The command flow is the same as for the top chute (section 8.1), with the
  front slot E9 and D4 values (section 8.2). The front side is window
  `00`, the back side is window `80`. The pixel size READ and the JPEG
  height correction (section 8.4) are the same.
- Both sides came, 2592 x 3456 pixels at 300 dpi. The images are upside
  down, as for the top chute.
- After the sheet, E0 gives sense 3/80/03 (no paper): the job ends after
  one sheet. The sheet comes back out at the front.
- The front slot takes one sheet at a time. It does not take long paper:
  a receipt longer than an A4 sheet jammed in the load step (over USB).
- The SANE `fujitsu` backend calls this path "Card Front", "Card Back" and
  "Card Duplex". The `ix1300` SANE backend uses the same names.

### 8.10 Long page mode [NV]

The service uses this mode for the profile setting `length = "long"`
(documents up to 3,000 mm, top chute only). ScanSnap Home uses it for long
paper (its paper code 0x8003). All values come from the ScanSnap Home
programs (`SshResident`, `SsSvcPCWIFIFrmwk`, macOS arm64). The addresses
are in brackets. No hardware test confirmed the scanner side of this mode.

Settings. Only two values change. All other bytes are the same as for the
automatic length (section 8.2):

| Field | Automatic length [HW] | Long page mode [NV] | Source in ScanSnap Home |
|---|---|---|---|
| E9 +8, BE32 pre-read length | `00 00 43 50` | `00 02 36 80` (3,069.6 mm) | `DoSetPreReadMode` 0x10017d940 |
| E9 +4, BE32 width | `00 00 28 80` | `00 00 28 80` | 0x10017d63c |
| D4 parameters, byte 13 | `00` | `80` (mutual transfer) | `SetWindow` 0x10017e060-0x10017e094 |
| D4 parameters, byte 28 | `00` | `00` | 0x10017e0fc: `30` or `20` only for the front slot path |
| D4 descriptor +14, window length | `00 00 A5 43` | `00 00 A5 43` (also at 600 dpi) | `DoSetWindowInfo` 0x100156eb4, 0x100156f00 |
| READ data-in length (bytes 6..8) | `30 00 00` | `02 00 00` | `TelRead` 0x3a834, `MaxTransferSize_of_MutualRead` |

- The framework sets its "mutual" flag when D4 parameter byte 13 is 0x80
  (0x207f4). Then each image READ asks for 0x20000 bytes (0x29ebc).
- ScanSnap Home sends the E9 length 0x23680 only when its value
  [obj+0x1ec] is not 2; else 0x236C0 (0x45A4 at 600 dpi). The meaning of
  that value is not known. The service sends 0x23680.
- For the front slot path, ScanSnap Home sends the E9 length 0x4410 for
  all paper codes (0x10017d7cc). The service uses the automatic length for
  a front slot job, also with `length = "long"`.
- ScanSnap Home also uses the mutual transfer at 600 x 600 dpi. The service
  does not: a 3,000 mm page at 600 dpi has 70,866 lines, more than a JPEG
  can keep (65,535). `length = "long"` needs 150, 200 or 300 dpi.

Mutual READ (`getImageDataForMutualTransfer` 0x241d4):

CDB: `28 00 00 00 00 WW 02 00 00 00 PP SS`

1. Page IDs: front = c + 1, back = c + 2. The counter c starts at 0xFF
   and goes up by 2 for each sheet (0x242ac-0x242bc). So the page IDs are
   the same as in section 8.3: 0 and 1, then 2 and 3.
2. One sequence ID `SS` for the two windows. It starts at 0 and goes up by
   1 after each step (0x24730).
3. In each step: one READ for window 0x00, then one READ for window 0x80.
   A side that ended gets no more READs. The loop stops when both sides
   ended (0x2472c).
4. Each reply has the 2-byte prefix (page ID, sequence ID), then up to
   0x1FFFE image bytes. With status GOOD, the host adds the image bytes to
   the side of that window.
5. CHECK CONDITION: send REQUEST SENSE. EOM (sense byte 2, bit 6) must be
   set (0x24400). ILI is not examined. The sense key must be 0 (0x246c0).
   Then the side ended. The image bytes of this last reply are
   (0x20000 - 2) - (sense information, bytes 3..6, BE32) (0x24404-0x24414).
   If the information is more than 0x1FFFE, or the result is 0, the last
   reply adds no bytes (0x24420-0x24428).
6. The result is two buffers: the front image and the back image. A
   front-only scan in ScanSnap Home reads both and drops the back
   (`DoGetImageData` 0x10017f3a4-0x10017f3c8).
7. ScanSnap Home does not send the same READ again in this loop: a CHECK
   CONDITION without EOM is an error (0x2478c). The service sends the same
   READ again for status 8 (busy) and sense 3/80/13, as in section 8.3.
8. Pixel size: ScanSnap Home reads data type 0x80 one time after the sheet,
   for the last window and its page ID (`getImageInfo` 0x21d28-0x21d40).
   The service reads it for each window, as in section 8.4.

Rules of the service for the items that are not known:

- If the cut of step 5 removes the JPEG end marker (`FF D9`), and the full
  reply ends with it, the service keeps all bytes and logs a warning.
- The JPEG height of a long page is not known. The service writes the real
  length from the pixel size into the SOF height. In long page mode it also
  makes the height larger, up to the E9 length (36,256 lines at 300 dpi).
- `scanner.side_timeout` is the limit for the two sides of one sheet.
- The job manifest and `raw.json` record, for each side: the READ count,
  the reply count with data, the image bytes, the sense information of the
  end, and the bytes cut (`reads`).

## 9. Sides and page order [HW]

- The scanner feeds the top sheet of the stack first.
- The side that faces up in the top chute is the front image (window 0x00).
  The usual load is "printed side down", so the print is then on the back
  image (window 0x80).
- A stack loaded face up in reading order (first page on top, top edge
  first) gives the reading order when each sheet gives front, then back.
- The image of the printed side can be turned 180 degrees. The service
  uses automatic rotation and blank page removal.
- The SANE `fujitsu` backend over USB has the same side order: the side
  that faces up is "front".

## 10. Limits of this description

- TLS (the "secure" modes of the scanner) is not described and not
  implemented.
- The front slot (return path) was tested with one A4 sheet at 300 dpi
  over Wi-Fi (section 8.9), and over USB with the `fujitsu` backend. Other
  resolutions, color modes and paper sizes in the front slot are [NV].
- The long page mode (section 8.10) was tested with the emulator only.
- Only firmware 0J00 and one scanner were tested.
