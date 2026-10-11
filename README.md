# ix1300-wifi

**Use a ScanSnap iX1300 with Linux, over Wi-Fi. Push the Scan button, and get a PDF.**

The ScanSnap iX1300 is a small document scanner from PFU (formerly Fujitsu).
Its own software, ScanSnap Home, runs only on Windows and macOS. This project
replaces it on Linux: you put paper in the scanner, you push the button, and
the PDF arrives in Paperless-ngx, in a folder, or at any web address that you
choose. You do not need a computer screen or a USB cable for daily use.

```
  ┌─────────────┐   Wi-Fi    ┌──────────────────┐        ┌────────────────┐
  │  iX1300     │ ─────────► │  ix1300 service  │ ─────► │ Paperless-ngx  │
  │  (Scan      │  button +  │  (Docker, any    │  PDF   │ a folder       │
  │   button)   │  images    │   Linux host)    │        │ a webhook      │
  └─────────────┘            └──────────────────┘        └────────────────┘
```

The project also has a **SANE backend**. With it, the normal Linux scan
programs (`scanimage`, `scanadf`, `gscan2pdf`, `sane-scan-pdf`) can use the
scanner over Wi-Fi too.

> This is an independent project. It is not from Ricoh, PFU, or Fujitsu.
> It works well on our scanner, but it has had only one scanner to test on.

---

## What it does

- **One button, two kinds of documents.** A short press scans normal
  documents. A long press (hold the button until it flashes blue) scans
  receipts. You can send each kind to a different place.
- **Clean PDFs.** The service removes blank pages, turns upside-down pages,
  and puts the pages in reading order. For receipts, it also straightens the
  image and cuts away the empty space around the paper.
- **Long receipts.** Receipts of up to about 3 m can go through in one piece
  (new, see "Good to know").
- **Two ways to load paper.** The top chute takes a stack of sheets. The front
  slot takes one card, photo, or thick sheet at a time.
- **No lost scans.** The service keeps each scan on disk until every output
  has received it. If your Paperless server is down, the service tries again
  later.
- **No cloud.** Everything stays in your network.

---

## What you need

- A ScanSnap iX1300.
- A Linux computer that is always on, with Docker (for example a Raspberry
  Pi, a NAS, or a home server). A Raspberry Pi 5 works well. A very small
  computer (512 MB of memory) works for normal documents, but not with the
  extra image work for receipts.
- The scanner and the Linux computer in the same network.
- A USB cable and about 15 minutes, **one time only**, to put the scanner on
  your Wi-Fi network.

---

## Setup

The setup has three steps. You do steps 1 and 2 only one time.

### Step 1: Prepare the service

```
git clone https://github.com/zer0pwned/ix1300-wifi.git
cd ix1300-wifi/service
mkdir -p config data
cp config.example.toml config/config.toml
docker compose build
```

The scanner accepts commands only from a computer that it knows. This
computer has a "host ID": a random number of 16 hex digits. Make one:

```
python3 -c "import secrets; print(secrets.token_hex(8).upper())"
```

Write it into `config/config.toml` as `host_id` in the `[scanner]` section.

### Step 2: Connect the scanner to Wi-Fi (USB cable, one time)

Connect the scanner to the Linux computer with the USB cable. Open the cover
to turn the scanner on. Then run these two commands. The first one asks for
your Wi-Fi password. The second one tells the scanner your host ID.

```
docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-wifi-setup --ssid YourNetworkName

docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
    ix1300 usb-register-host --host-id YOUR_HOST_ID
```

Disconnect the USB cable. In your router, give the scanner a fixed IP address,
and write that address into `config/config.toml` as `ip`.

### Step 3: Tell the service where to send the PDFs, and start it

In `config/config.toml`, fill in at least one output: Paperless-ngx (address
and API token), a folder, or a webhook address. The file has examples, and
[`service/README.md`](service/README.md) explains each setting. Then start
the service:

```
sudo chown 1000:1000 data && chmod 700 data
docker compose up -d
docker compose logs -f
```

When the log shows `reserved scanner`, the service is ready.

---

## Daily use

1. Open the cover. The scanner turns on.
2. Put the paper in:
   - **Top chute** (a stack): the first page on top, the printed side up,
     top edge first.
   - **Front slot** (one card or thick sheet): face up, top edge first.
3. Push the Scan button:
   - **Short press:** a normal document. All sheets go into one PDF.
   - **Long press:** hold the button until it **flashes blue**, then release.
     The service uses the receipt settings.
4. The PDF arrives in your outputs a few seconds later.

If you push the button with no paper in the scanner, nothing happens. That
is normal.

The scanner turns itself off after some hours with no use. Open and close the
cover to turn it on again. The service finds it again in a few seconds.

---

## Good to know

- **Load only the sheets that you want to scan.** The scanner always takes
  the whole stack. It has no way to stop after a number of sheets.
- **Receipts.** Use the top chute, not the front slot (the front slot takes
  only short paper). Set the paper guides close to the receipt, and hold the
  end of a long receipt loosely so that it goes in straight.
- **New features that wait for a test on real hardware:** receipts longer
  than about 90 cm, host registration over Wi-Fi, and double-feed detection
  off. The scanner accepts the auto power-off setting, but a full power-off
  test with a new time is still to do. If the scanner rejects one of these,
  the service uses the tested way instead and writes the reason in its log.
- **Security.** The scanner protocol has no password and no encryption. Use
  it only in a network that you trust.
- **One program at a time.** The scanner works with one computer at a time.
  Do not run the service and a SANE program at the same time. ScanSnap Home
  on another computer can take over the scanner; the service log then shows
  how to fix it.

---

## The SANE backend (optional)

If you prefer the normal Linux scan programs, install the SANE backend on a
Debian or Raspberry Pi OS computer:

```
cd sane-backend
sudo HOSTID=YOUR_HOST_ID ./install-on-debian.sh
scanimage -L
```

Use `--source "ADF Duplex"` for the top chute and `--source "Card Duplex"`
for the front slot. [`sane-backend/README.md`](sane-backend/README.md) has
the details.

---

## For developers

| Folder or file | What is in it |
|---|---|
| [`service/`](service/README.md) | The scan service (Python, Docker) and its tests |
| [`sane-backend/`](sane-backend/README.md) | The SANE backend, the patch for sane-backends 1.3.1, and its tests |
| `tools/` | Two small USB tools for the one-time setup without Docker |
| [`PROTOCOL.md`](PROTOCOL.md) | How the scanner talks over USB and Wi-Fi |

The tests need no scanner and no network:

```
python3 -m unittest discover -s service/tests
make -C sane-backend/tests
```

---

## License and disclaimer

The SANE backend is under the GPL version 2 or later, with the SANE exception,
like all other SANE backends ([`sane-backend/LICENSE`](sane-backend/LICENSE)).
All other files are under the MIT license ([`LICENSE`](LICENSE)).

ScanSnap is a trademark of PFU Limited. This project is not from, or supported
by, Ricoh, PFU, or Fujitsu. It contains no vendor software and no vendor
firmware. `PROTOCOL.md` describes only the messages that go over the USB cable
and the network.

Use this software at your own risk. It can change the Wi-Fi settings of your
scanner, and paper can jam or go through without a scan. There is no warranty.
