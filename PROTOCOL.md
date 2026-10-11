# ScanSnap iX1300: USB setup and Wi-Fi scan protocol

This document describes the messages that go between a computer (the
"host") and a ScanSnap iX1300 scanner. The service (`service/`) and the SANE
backend (`sane-backend/`) use these messages. The document describes only
the messages on the USB cable and on the network. It does not describe the
vendor software.

## Overview

Read this overview first. It explains the protocol in plain words. The
numbered sections after it give the byte tables.

### Two transports

The scanner has two connections to a host. Each connection has a different
job.

| Transport | When you use it | What it does |
|---|---|---|
| USB cable | One time, at setup | Gives the scanner the Wi-Fi network and the key. Sets the scanner to start as a Wi-Fi client. Registers the host ID |
| Wi-Fi (network) | Each day, for each scan | Finds the scanner, reserves it, receives the button events, and reads the images |

After the setup, you can disconnect the USB cable. The scanner then
connects to the Wi-Fi network each time you open its cover.

### Network ports

| Port | Side | Protocol | Role |
|---|---|---|---|
| 52217 | Scanner | UDP | Search for the scanner. Keep the reserve alive |
| 53218 ("Port1") | Scanner | TCP | Scan commands (SCSI commands in a network frame) |
| 53219 ("Port2") | Scanner | TCP | Reserve and release the scanner |
| Trigger port (default 55265) | Host | UDP | The scanner sends button and power events here |

### How a scan works

The scanner does not start a scan itself. When you push the Scan button,
the scanner only sends an event to the host. The host then sends all the
commands of the scan, and reads the images. Each side of each sheet comes
as one JPEG image.

### One scan, step by step

1. The host sends a search packet to UDP port 52217. The scanner replies
   with its address and its TCP ports (section 5.1, 5.2).
2. The host connects to Port2 and sends a reserve. The reserve contains the
   host ID and the trigger port of the host (section 5.3).
3. Each 15 s, the host sends a keepalive to UDP port 52217. The reserve then
   stays valid (section 5.1).
4. You put paper in the scanner and push the Scan button. The scanner sends
   a trigger packet to the trigger port, 3 times (section 6).
5. The host connects to Port1 and reads the paper sensors with GET HW
   STATUS (C2). This shows the paper path: top chute or front slot (section
   2.1).
6. The host sends the settings (E9, D4) and starts the job (D5) (section
   8.1, 8.2).
7. For each sheet, the host sends E0 (next sheet). Then it reads the front
   image and the back image with READ commands (section 8.3).
8. After each side, the host reads the real size of the page. It writes the
   real height into the JPEG (section 8.4).
9. When E0 reports "no paper", the host ends the job with D6 (section 8.5).
10. The host keeps the reserve for the next scan, or sends a release on
    Port2 (section 5.4).

### Marks in this document

Each fact has one of these two marks:

- **[HW]**: we tested this item on a real iX1300 (firmware 0J00).
- **[NV]**: not verified on the scanner. The implementation uses this item,
  but no hardware test confirmed it.

A mark on a section heading applies to all items in the section, if an
item does not have a different mark.

### Conventions

- All examples use the documentation address range (192.0.2.0/24) and
  false example values (host ID `0123456789ABCDEF`).
- Hex bytes show as `00 1A`. "BE16" and "BE32" are big-endian integers of 2
  and 4 bytes. "LE32" is a little-endian integer of 4 bytes.
- "+12" is the byte offset 12 in a message.
- A CDB is the SCSI command block. Its first byte (for example `C2`) also
  gives the name of the command in this document.

### Contents

| Section | Topic | When you need it |
|---|---|---|
| 1 | The scanner | Identify the device |
| 2 | USB transport | Setup over USB, paper sensors |
| 3 | Setup messages over USB | Wi-Fi setup, host ID |
| 4 | Network overview | Ports, key, byte order |
| 5 | Discovery, reserve and release | Find and reserve the scanner |
| 6 | Trigger packets | Button and power events |
| 7 | Port1 command frames | Send a SCSI command over the network |
| 8 | Scan job | Commands of a scan, images, errors |
| 9 | Sides and page order | Put the pages in the correct order |
| 10 | Limits of this description | Items that we did not test |

## 1. The scanner

This section gives the data that identifies the scanner, and the two paper
paths.

| Item | Value |
|---|---|
| USB ID | `04c5:162c` [HW] |
| INQUIRY | `FUJITSU ScanSnap iX1300 0J00` (vendor, product, firmware version) [HW] |
| Device name | `iX1300-<serial number>` [HW] |
| Power | Open the cover (top paper chute) to turn the scanner on. Close the cover to turn it off. The USB device then disconnects [HW] |
| Paper paths | Top chute (U-turn, stack of sheets, long paper) and front slot (return path, one sheet) [HW] |

## 2. USB transport [HW]

You need this section only for the one-time setup over USB (section 3).
It shows how one SCSI command goes over the USB cable.

The scanner uses two bulk endpoints: `0x02` (out) and `0x81` (in).

Each command has three phases:

1. The host sends a 31-byte command block. Byte 0 is `0x43`. The SCSI CDB
   starts at offset 19. The other bytes are 0.
2. Data phase (optional): the host sends the data-out bytes, or reads the
   data-in bytes.
3. The host reads a 13-byte status block. Byte 0 is `0x53` ("S"). The
   SCSI status is `byte 9 & 0x3E`. 0 is "good".

Over USB, the standard SANE `fujitsu` backend scans correctly [HW]. Thus
this document does not describe the scan commands over USB. The setup needs
only the commands below.

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

C2 reads the paper sensors, the buttons and the registered host ID. Use it
before each job to find where the paper is. The reply has the same layout
over USB and over the network (Port1).

| Byte, bit | Meaning |
|---|---|
| 2, bit 7 | Paper at the feed sensor (also set for the front slot) |
| 3, bit 7 | Chute empty (1 = no paper in the top chute) |
| 3, bit 0 | Paper at the exit sensor (also the front slot) |
| 4, bit 0 | Scan button, short press |
| 4, bit 5 | Scan button, long press |
| 0x20..0x27 | Registered host ID (8 bytes; `FF..FF` = no ID). Only in a reply of 0x30 bytes |

Paper path before a job [HW]. We measured these values over Wi-Fi with one
A4 sheet in the front slot: byte 2 = `80`, byte 3 = `81` (chute empty, exit
sensor set). After the front slot job, the path was clear: byte 2 = `00`,
byte 3 = `80`.

The exit bit (byte 3 bit 0) can stay set with no paper (we saw this after
a halt). Thus do not use the exit bit alone. Before a job, use these rules:

- Byte 3 bit 7 = 0: paper in the top chute.
- Byte 3 bit 7 = 1 and byte 2 bit 7 = 1: a sheet in the front slot.
- Else: no paper (also with byte 2 = `00`, byte 3 = `81`).

These rules apply only before the first sheet. During a job, the scanner
pulls sheets into the paper path (section 8.5).

The button bits change over USB. Over the network without a reserve, the
button bits did not change (60 s test) [HW]. For the button, use the
trigger packets (section 6).

## 3. Setup messages over USB [HW]

This section describes the one-time setup over USB. You need it to put the
scanner on your Wi-Fi network and to register the host ID. All setup
messages go in a session (section 3.1).

### 3.1 Session

A setup session starts with `SETUP NET START` and ends with `SETUP NET END`.
Each tag is 16 ASCII bytes (with spaces at the end). The host sends the tag
with SEND DIAGNOSTIC, and reads a 1-byte reply with RECEIVE DIAGNOSTIC.

- Reply `0x00`: OK.
- Reply `0x82` to `SETUP NET START`: the scanner refused the session. To
  clear this state, turn the scanner off and on again.

### 3.2 SETUP NET CMD frame

Each setup command (an "ope code", section 3.3) goes in this frame.

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

This table lists the setup commands that we know.

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

This is the data of the command that writes the Wi-Fi network.

| Offset | Size | Field |
|---|---|---|
| 0x00 | 33 | SSID, with NUL bytes at the end (32 bytes maximum) |
| 0x24 | 8 | Authentication: `WPA2`, `WPA`, `MIX`, `OPEN`, `SHARED` |
| 0x2C | 8 | Cipher: `AES`, `TKIP`, `AUTO`, `WEP`, `NONE` |
| 0x34 | 193 | Key, encoded (see below) |

Key encoding: for each byte `b` at index `i` of the key, write the 3 decimal
digits of `b + 11 + K[i % 16]`, with `K = "pFusCANsNapFiPfu"`. Example: the
key `1234` gives `172131179178`.

A key of 64 hex digits is a raw PSK. A shorter key is a passphrase. The
scanner saves the settings also when the connection fails.

We tested only `WPA2`/`AES` on the scanner [HW]. The other values are [NV].

### 3.5 Wi-Fi setup procedure [HW]

Do these steps to put the scanner on a Wi-Fi network.

1. Send `SETUP NET START`.
2. Send ope 0x1006 with the SSID, `WPA2`, `AES`, and the key. Expect result
   0.
3. Send ope 0x1106 with the data `00 00 00 00` (boot mode = client). Expect
   result 0.
4. Optional: send ope 0x0103. Expect 0.
5. Read ope 0x0007 until the IP address is not `0.0.0.0`.
6. Send `SETUP NET END`.

Step 3 is necessary. Without step 3, the scanner connects, but after a
power cycle it starts its own access point again (the factory boot mode is
1). The Wi-Fi LED is then yellow. With step 3, the LED is blue after a
power cycle, and the scanner connects to the network again.

### 3.6 Host ID registration [HW]

The scanner accepts a network reserve only from the registered host ID.
You need this section if the reserve gives the result -6 (section 5.3).
You can register the ID over USB or over Wi-Fi.

The scanner keeps one registered host ID (8 bytes). A reserve over the
network with a different ID gets the result -6. Each computer can use a
random 64-bit value. The MAC address is not necessary.

To make a new ID (16 hex digits):

```
python3 -c "import secrets; print(secrets.token_hex(8).upper())"
```

- Read the ID: GET HW STATUS with 0x30 bytes
  (`C2 00 00 00 00 00 00 00 30 00`). The ID is at bytes 0x20..0x27.
  `FF..FF` = no ID.
- Write the ID over USB:
  1. Send `SETUP NET START`.
  2. Send `CLR HOSTID ADATA` (16-byte tag, 2-byte reply). Send it only if an
     ID is registered.
  3. Send `REG HOSTID DATA` + the 8-byte ID + 8 zero bytes (2-byte reply).
  4. Send `SETUP NET END`.

  A 2-byte reply is OK if `byte 0 & 0x7F` is 0.

Caution: the vendor software on a different computer registers its own ID
when it connects. Then the reserve of your host gets -6 again. You can also
use the ID of that computer, if you know it.

#### Registration over Wi-Fi

This sequence comes from the vendor software. The service implements it
(`wifi-register-host`, `scanner.auto_register`). The first steps and the
read commands are [HW]. The register and clear commands (DC op 0 and op 3)
are [NV].

1. Read the registered ID with GET HW STATUS (C2 with 0x30 bytes). C2
   works without a reserve (section 7.3).
2. Send a reserve (section 5.3) with mode (+36) = 2 and the new host ID at
   +108. The scanner accepts this reserve also with an ID that is not
   registered [HW].
3. Port1 `DC 00 03 00 00 00 00 02` (clear, no data-out, 2 bytes in). The
   vendor software sends it only if the scanner has an ID [NV].
4. Port1 `DC 00 00 00 00 10 00 02` (register) with 16 bytes data-out: the
   8-byte ID + 8 zero bytes. 2 bytes in [NV].
5. Release (section 5.4).
6. Reserve with mode 0 and the new ID, then release (verify).

CDB of DC (host ID operation): `DC 00 OP 00 00 WL 00 RL` (WL = data-out
length, RL = data-in length). For op 0 and op 3, byte 0 of the data-in is
the result. The service accepts the result if `byte 0 & 0x7F` is 0 (as on
USB) [NV].

| OP | CDB | Data-out | Data-in | Meaning | Status |
|---|---|---|---|---|---|
| 0 | `DC 00 00 00 00 10 00 02` | 16 | 2 | Register | [NV] |
| 1 | `DC 00 01 00 00 02 00 18` | 2 (index?) | 24 | Read the data | [HW] |
| 3 | `DC 00 03 00 00 00 00 02` | 0 | 2 | Clear | [NV] |
| 4 | `DC 00 04 00 00 00 00 04` | 0 | 4 | Read the length | [HW] |

Replies in a mode-2 reserve [HW]:

- Op 4 gives `00 18 00 00`. 0x18 (24) is the length of the op 1 data.
- Op 1 gives 24 bytes: `01`, 7 zero bytes, the registered host ID at bytes
  8..15, then 8 zero bytes.

## 4. Network overview [HW]

This section gives the values that all network messages share. Read it
before sections 5 to 8.

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

A host must find the scanner and reserve it before a scan. This section
gives the search packet, the reserve and the release. The keepalive keeps
the reserve valid between scans.

### 5.1 Search and keepalive packet (32 bytes) [HW]

The search packet and the keepalive packet have the same layout. Only the
value at +4 is different.

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

The scanner sends the reply to the source port of the probe. The reply
gives the TCP ports, the device status, and the last owner of the reserve.

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
| 38 | 2 | BE16 device status (2 = busy with a USB host or with an old job) |
| 40 | 64 | Device name (text) |
| 104 | 16 | Product name (text) |
| 120 | 4 | IPv4 address of the last owner of the reserve |
| 124 | 8 | Host ID of the last owner |
| 132 | 12 | Serial number (text) |

### 5.3 Reserve (Port2, 128 bytes) [HW]

The reserve gives one host the use of the scanner. The scanner then sends
the button events to the trigger port of that host.

| Offset | Size | Value |
|---|---|---|
| 0 | 4 | BE32 message length (128) |
| 4 | 4 | Key |
| 8 | 4 | BE32 0x11 (reserve) |
| 16 | 6 | Host MAC address |
| 32 | 2 | BE16 interface version |
| 34 | 1 | Reserve timeout in seconds (the service uses 30) |
| 36 | 4 | BE32 mode (0 = normal scan, 2 = host ID registration, section 3.6) |
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
| -4 | Scanner occupied by a different host, or busy after a USB scan command [HW] |
| -5 | Interface version not supported |
| -6 | Host ID not registered [HW] |
| -7 | Host ID not valid |
| -8 | Sequence error |
| -9 | USB mode error |

Send a keepalive (section 5.1) each 15 s to keep the reserve. The reserve
stays valid between scan jobs [HW]. The scanner Wi-Fi uses power save. Thus
send up to 3 keepalive packets in each interval.

### 5.4 Release (Port2, 32 bytes) [HW]

The release ends the reserve. Then a different host can reserve the
scanner.

BE32 length 32, key, BE32 0x12 at +8, host MAC at +16, BE32 force flag at
+24 (0 = normal). The reply has 16 bytes: signed BE32 result at +8 (0 = OK).
After the release, the discovery reply shows the host ID as the last owner.

## 6. Trigger packets (button and power events) [HW]

The scanner sends a trigger packet when you push the Scan button, and
before it turns itself off. You need this section to start a scan from
the button.

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
  during a scan job). Thus do not use a time limit for the duplicate test.
  Keep the last 16 sequence numbers for each code.
- The next event starts only after the 3 copies (about 1.0 s between two
  events). A fast double press arrives as two events about 1.0 s apart. We
  lost no press in 5 tests.
- The scanner sends one event for each press. There are no "button down"
  or "button up" events.
- Short press: the event comes when you release the button (+14 = `0x00`).
- Long press: hold the Scan button until it flashes blue (about 3 s). The
  event comes while you hold the button (+14 = `0x20`). The long press
  works with paper in the chute (3 of 3 tests).
- With no paper in the chute, a hold of 3 s just after a job turns the
  rollers, with no blue flash and no event. Read C2 before a job. Do not
  start a job with no paper (section 2.1).
- The button works also when the USB cable is connected.

Power off [HW]: the scanner turns itself off after about 4 hours with no
scan (section 8.11 gives this time). A keepalive does not prevent this. The
scanner first sends code 0x20 (5 minutes before), then code 0x10. No
"power on" event comes when no host has a reserve.

After the next power-on, the host must find the scanner again with search
probes (less than 60 s in the test). A TCP connection without a search
probe can fail with "No route to host" for a long time.

## 7. Port1 command frames [HW]

Over the network, each SCSI command goes in a frame on Port1. This section
gives the frame of the request and of the reply. Sections 8 and 3.6 use
these frames.

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

Most commands need a reserve. These commands work without a reserve:
INQUIRY (`12`, the same 96 bytes as on USB), INQUIRY VPD (`12 01 F0`), and
GET HW STATUS (`C2`).

REQUEST SENSE (`03`) without a reserve closes the connection. With a
reserve, REQUEST SENSE works.

The commands SET WINDOW and SCAN do not give images over the network. The
scan job uses the command flow in section 8.

## 8. Scan job [HW]

This section gives the commands of one scan job over Wi-Fi, from the
settings to the last image. It also describes the paper stack, the stop,
the errors, and the two special paper modes.

### 8.1 Command flow

Send these commands in this order. The steps "for each sheet" repeat until
E0 reports "no paper" (section 8.5).

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
18 bytes). If a command gives status 8 (busy), wait a short time and send
it again.

### 8.2 E9 and D4 data

E9 and D4 send the scan settings: resolution, color, page size and paper
path. These values work on the scanner [HW]. We do not know the meaning of
the bytes that have no description.

E9 (32 bytes): BE16 X resolution, BE16 Y resolution, BE32 window width
(0x2880 = 219.5 mm in 1/1200 inch), BE32 pre-read length (0x4350), `05 00`,
then 18 zero bytes.

D4 (0x50 bytes): 32 bytes of parameters, then a 48-byte window descriptor.

- Parameters: `00 03 01 01 88 00 C1 80 80 80 80 80 80 00`, 17 zero bytes,
  `30`. Byte 6 = `C1` selects the automatic color mode. Byte 1 = 3 is the
  only tested value.
- Parameter byte 4: double-feed detection [NV]. `88` = on (the value in
  all tests). `00` = off. The vendor software sends `00` for the front slot
  and when the user turns the detection off. The scanner does not keep
  this value. Thus the host sends it in each job.
- Window descriptor: +1 = `10` (JPEG); +2 BE16 X resolution; +4 BE16 Y
  resolution; +6 = `05` (color) or `02` (gray [NV]); +7 = `82` at 300 dpi,
  else `81`; +8 = `0D` at 300 dpi, `0A` at 600 dpi, else `05`; +10 BE32
  window width (0x2880); +14 BE32 window length; +18 = `04`; +30 = `01`.
- Window length: 0xA543 (895.5 mm) is the automatic length. The scanner
  finds the real end of the paper.

We tested only 300 dpi and the automatic color mode on the scanner. The
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

READ gets the JPEG data of one side. The host sends READ again and again
until the side ends.

CDB: `28 00 00 00 00 WW 30 00 00 00 PP SS`

- `WW`: window (0x00 = front, 0x80 = back).
- Bytes 6..8: BE24 data-in length (0x300000).
- `PP`: page ID (front = 2 x sheet index, back = front + 1, modulo 256).
- `SS`: sequence number of the READ in this side.

Each reply has a 2-byte prefix (page ID, sequence ID), then JPEG bytes. The
JPEG data of the side is all the replies in sequence.

- The first READ often gives CHECK CONDITION with sense 3/80/13 (temporary
  no data). Wait 0.1 s, then send the same READ again.
- End of a side: CHECK CONDITION, then REQUEST SENSE with sense key 0 and
  EOM = 1. The data of that last READ reply is also image data.
- Each side is one baseline JPEG (for example 2592 x 3424 pixels, RGB, 1.1
  to 1.4 MB for an A4 page at 300 dpi).

### 8.4 Pixel size and height correction [HW]

The JPEG from the scanner is always as high as the window length. This
section shows how to find the real length of the page and correct the
JPEG.

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

This section tells when to end a job. If the host ends the job too early,
the scanner ejects sheets without a scan.

- The scanner feeds the whole stack in one job (D5 ... D6). A host cannot
  stop the feed when it stops the READ commands.
- The scanner pulls the next sheets early. With 3 sheets in the chute, C2
  showed an empty chute after sheet 1, while sheets 2 and 3 were in the
  paper path. A host that ends the job on the chute bit loses these sheets:
  the scanner feeds them out without a scan.
- Rule: after each sheet, send E0 again. End the job only when E0 (or the
  first READ of a sheet) gives sense 3/80/03 (no paper). Then send D6.
- At that time, the C2 exit bit can be set without a jam.
- Front slot: after the one sheet, the next E0 gave CHECK CONDITION, sense
  3/80/03 (no paper), after 0.1 s. Thus the same loop ends a front slot
  job after one sheet (section 8.9).

### 8.6 Stop with sheets left (D8) [HW]

You need this section to stop a job before the scanner has fed all sheets.
There is no clean stop. The stop sequence D8, E0, D6 feeds the remaining
sheets out without a scan [HW].

We measured this with 5 sheets in the chute, after sheet 1:

| Commands | Result |
|---|---|
| D8, no command for 15 s, then D6 | The motor stops immediately. Sheet 2 stays in the paper path near the exit. Sheet 3 is at the pick roller. 2 sheets stay in the chute. D6 does not eject. C2: feed = 1, exit = 1 |
| D8, then E0, then D6 | A READ after D8 gives sense 5/24/00 (invalid field). E0 makes the scanner feed all remaining sheets out without a scan. The paper path is then empty |

A second test with the SANE backend: D8 + E0 + wait for C2 + D6 with 3
sheets ejected sheets 2 and 3 in 2.8 s. D8 + D6 with 2 sheets: sheet 2 (in
the paper path already) still came out completely. C2 then showed exit = 1
with no paper. Thus the halt point changes with the position of the next
sheet.

A host cannot stop cleanly after N sheets. It can leave paper in the path
(D8 only), or it can feed the rest out without a scan (D8 + E0 + D6).
**Load only the sheets that you want to scan.**

### 8.7 Sense codes

REQUEST SENSE gives these codes after a CHECK CONDITION.

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

Over Wi-Fi, a jam does not give an error. This section shows how a host
can find a probable jam.

The page ends with a normal EOM at the jam point (688 mm in the test), and
the release is OK. After the jam, the C2 exit bit (byte 3 bit 0) stays set
with no paper. Read C2 after the last sheet. If the exit bit is set, a jam
is probable.

A long receipt that is not straight can jam or move to the side. Move the
paper guides against the edges of the receipt.

### 8.9 Front slot (return path) [HW]

The front slot takes one sheet. The sheet comes back out at the front.
This section gives the differences from a top chute job.

We measured this over Wi-Fi: two front slot scans of one A4 sheet, 300 dpi,
duplex.

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

Continue scan [HW]: to put several front slot sheets into one document, the
host runs one job for each sheet. The host then joins the images. This is
a host function. It needs no special message to the scanner.

### 8.10 Long page mode [HW]

Use this mode for paper that is longer than a normal page, for example
long receipts. The scanner then sends the two sides together, in smaller
parts ("mutual READ").

The service uses this mode for the profile setting `length = "long"`
(documents up to 3,000 mm, top chute only). The vendor software uses it for
long paper. The values come from the vendor software.

Hardware test [HW] (2026-10-11): an 83 cm receipt at 300 dpi, duplex. The
scanner accepted the E9 length 0x23680, D4 parameter byte 13 = 0x80, and
mutual READs of 0x20000 bytes. The front side took 59 READs, the back side
30 READs. The image was complete.

Still [NV]: a page longer than the window length (about 895 mm), and the
other resolutions.

Settings. Only two setting values change: the E9 pre-read length and D4
parameter byte 13. The READ length also changes. All other bytes are the
same as for the automatic length (section 8.2):

| Field | Automatic length | Long page mode |
|---|---|---|
| E9 +8, BE32 pre-read length | `00 00 43 50` | `00 02 36 80` (3,069.6 mm) |
| E9 +4, BE32 width | `00 00 28 80` | `00 00 28 80` |
| D4 parameters, byte 13 | `00` | `80` (mutual transfer) |
| D4 parameters, byte 28 | `00` | `00` (the vendor software sends `30` or `20` only for the front slot path) |
| D4 descriptor +14, window length | `00 00 A5 43` | `00 00 A5 43` (also at 600 dpi) |
| READ data-in length (bytes 6..8) | `30 00 00` | `02 00 00` |

- When D4 parameter byte 13 is 0x80, each image READ asks for 0x20000
  bytes.
- The vendor software sometimes sends the E9 length 0x236C0 (0x45A4 at 600
  dpi) and not 0x23680. We do not know the condition. The service sends
  0x23680.
- For the front slot path, the vendor software sends the E9 length 0x4410
  for all paper sizes. The service uses the automatic length for a front
  slot job, also with `length = "long"`.
- The vendor software also uses the mutual transfer at 600 x 600 dpi. The
  service does not: a 3,000 mm page at 600 dpi has 70,866 lines. A JPEG can
  keep a maximum of 65,535 lines. Thus `length = "long"` needs 150, 200 or
  300 dpi.

Mutual READ:

CDB: `28 00 00 00 00 WW 02 00 00 00 PP SS`

1. Page IDs: front = c + 1, back = c + 2. The counter c starts at 0xFF
   and increases by 2 for each sheet. Thus the page IDs are the same as in
   section 8.3: 0 and 1, then 2 and 3.
2. One sequence ID `SS` for the two windows. It starts at 0 and increases
   by 1 after each step.
3. In each step: one READ for window 0x00, then one READ for window 0x80.
   A side that ended gets no more READs. The loop stops when both sides
   ended.
4. Each reply has the 2-byte prefix (page ID, sequence ID), then up to
   0x1FFFE image bytes. With status GOOD, the host adds the image bytes to
   the side of that window.
5. CHECK CONDITION: send REQUEST SENSE. EOM (sense byte 2, bit 6) must be
   set. The vendor software does not examine ILI. The sense key must be 0.
   Then the side ended. The image bytes of this last reply are
   (0x20000 - 2) - (sense information, bytes 3..6, BE32). If the
   information is more than 0x1FFFE, or the result is 0, the last reply
   adds no bytes.
6. The result is two buffers: the front image and the back image. For a
   front-only scan, the vendor software reads both sides and discards the
   back.
7. The vendor software does not send the same READ again in this loop: a
   CHECK CONDITION without EOM is an error. The service sends the same
   READ again for status 8 (busy) and sense 3/80/13, as in section 8.3.
8. Pixel size: the vendor software reads data type 0x80 one time after the
   sheet, for the last window and its page ID. The service reads it for
   each window, as in section 8.4.

Rules of the service for the items that we do not know:

- If the cut of step 5 removes the JPEG end marker (`FF D9`), and the full
  reply ends with it, the service keeps all bytes and logs a warning.
- The JPEG height of a page longer than the window length is not known
  [NV]. The service writes the real length from the pixel size into the
  SOF height. In long page mode it also makes the height larger, up to the
  E9 length (36,256 lines at 300 dpi).
- `scanner.side_timeout` is the limit for the two sides of one sheet.
- The job manifest and `raw.json` record these values for each side: the
  READ count, the count of replies with data, the image bytes, the sense
  information of the end, and the bytes cut (`reads`).

### 8.11 Auto power-off time (E6)

E6 reads and sets the time after which the scanner turns itself off. It
needs a reserve.

On our scanner, a set and a get worked: the get gave back the bytes of the
set [HW]. We did not test the effect on the real power-off time.

| Step | CDB | Data |
|---|---|---|
| Get | `E6 00 01 00 00 00 00 04` | 4 bytes in: `CC BB 00 00` |
| Set | `E6 00 00 00 00 04 00 00` | 4 bytes out: `CC BB 00 00` |

- `00 00`: no value was set before. The scanner then uses 240 min [HW].
- `CC` bits 0-6: the time in 15-minute units (`01` = 15 min to `7E` =
  1890 min). `10` = 240 min, the factory value. `7F` = a special value of
  3 minutes.
- `CC` bit 7 = 1: auto power-off is off. The vendor software keeps the time
  bits.
- `BB`: `00` on our scanner, `1E` from the vendor software. Probably the
  warning time before the power-off, in units of 10 s (`1E` = 5 min). The
  service writes `BB` back with the value that it read.
- The scanner probably keeps the value after a power-off.
- Only a job start (D5) starts the idle time again. A keepalive does not
  [HW for the keepalive].

## 9. Sides and page order [HW]

This section tells which image is the printed side, and how to put the
pages in reading order.

- The scanner feeds the top sheet of the stack first.
- The side that faces up in the top chute is the front image (window 0x00).
  The usual load is "printed side down". Thus the print is then on the back
  image (window 0x80).
- Load the stack face up in reading order (first page on top, top edge
  first). Then the images come in reading order when each sheet gives
  front, then back.
- The image of the printed side can be turned 180 degrees. The service
  uses automatic rotation and blank page removal.
- The SANE `fujitsu` backend over USB has the same side order: the side
  that faces up is "front".

## 10. Limits of this description

This section lists the items that we did not test or did not describe.

- TLS (the "secure" modes of the scanner) is not described and not
  implemented.
- We tested the front slot (return path) with one A4 sheet at 300 dpi over
  Wi-Fi (section 8.9), and over USB with the `fujitsu` backend. Other
  resolutions, color modes and paper sizes in the front slot are [NV].
- We tested the long page mode (section 8.10) on the scanner with an 83 cm
  receipt at 300 dpi. A page longer than the window length (about 895 mm)
  is [NV].
- The host ID register and clear commands over Wi-Fi (DC op 0 and op 3,
  section 3.6) are [NV].
- Double-feed detection (D4 parameter byte 4, sense 3/80/07) is [NV].
- We tested only firmware 0J00 and one scanner.
