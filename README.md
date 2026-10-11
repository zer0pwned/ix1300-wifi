# ix1300-wifi: ScanSnap iX1300 on Linux over Wi-Fi

This repository lets a Linux computer use a PFU (Fujitsu) ScanSnap iX1300
over Wi-Fi, without the vendor software (ScanSnap Home). It has two parts:

- **Scan service** (`service/`): a Docker service. You push the Scan button
  on the scanner. The service scans all sheets, makes a PDF, and sends it to
  Paperless-ngx, to an HTTP webhook, or to a directory.
- **SANE backend** (`sane-backend/`): the `ix1300` backend for SANE. The
  standard SANE programs (`scanimage`, `scanadf`, `gscan2pdf`) can then scan
  over Wi-Fi.

The repository also has the tools for the one-time USB setup (Wi-Fi network
and host ID), and a description of the protocol (`PROTOCOL.md`).

## Status and limits

- We tested the code with one iX1300 (firmware 0J00) on a WPA2-PSK network.
- Only the top paper chute is available. The front slot (return path) is not
  available over Wi-Fi.
- TLS is not implemented. The scanner protocol has no authentication and no
  encryption. Use the scanner only in a trusted network.
- The scanner feeds all sheets in the chute in one job. There is no clean
  stop after a number of sheets. Load only the sheets that you want to scan.
- Only 300 dpi and the automatic color mode were tested on the scanner.
  `PROTOCOL.md` and `service/README.md` (section 9) show the items that are
  not verified.

## Quick start

### 1. One-time setup over USB

Do these steps one time, on a Linux computer with the scanner on USB. Open
the cover to turn the scanner on. Stop `saned` and other scanner programs
first.

1. Make a host ID (16 hex digits). The scanner accepts commands over Wi-Fi
   only from a host with the registered ID:

   ```
   python3 -c "import secrets; print(secrets.token_hex(8).upper())"
   ```

   The examples below use `0123456789ABCDEF`. Use your own value.

2. Build the service image, and make the configuration:

   ```
   cd service
   mkdir -p config data
   cp config.example.toml config/config.toml
   docker compose build
   ```

   Write the host ID into `scanner.host_id` in `config/config.toml`.

3. Connect the scanner to your Wi-Fi network (the command asks for the
   key):

   ```
   docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
       ix1300 usb-wifi-setup --ssid MyNetwork
   ```

4. Register the host ID:

   ```
   docker compose run --rm --user root --privileged -v /dev/bus/usb:/dev/bus/usb \
       ix1300 usb-register-host --host-id 0123456789ABCDEF
   ```

5. Disconnect the USB cable. Give the scanner a fixed IP address in your
   router (DHCP reservation), and write it into `scanner.ip`.

Without Docker, you can use `tools/setwifi.py` and `tools/ixusb.py`
(`reghostid:<ID>`) as root. They need `libusb-1.0`.

### 2. Run the service

1. Write the outputs (Paperless-ngx, webhook, or directory) into
   `config/config.toml`. Refer to `service/README.md`, section 4.
2. Start the service:

   ```
   sudo chown 1000:1000 data && chmod 700 data
   docker compose up -d
   docker compose logs -f
   ```

3. Put paper in the chute, printed side down. Push the Scan button.

The service uses the host network. The scanner sends the button events to
UDP port 55265 on the host. Open this port in the host firewall for the
scanner.

### 3. SANE backend (optional)

On Debian, the script builds the backend from the sane-backends 1.3.1
source and installs it:

```
cd sane-backend
sudo HOSTID=0123456789ABCDEF ./install-on-debian.sh
scanimage -L
```

Refer to `sane-backend/README.md`. Do not use the service and the SANE
backend at the same time: the scanner accepts one host only.

## Button gestures

| Gesture | Result |
|---|---|
| Short press (push and release) | Scan with `service.short_press_profile` |
| Long press: hold the Scan button until it flashes blue (about 3 s), then release | Scan with `service.long_press_profile` (for example a receipt profile) |

A press with an empty chute starts no job. A long press works only with
paper in the chute.

## Repository layout

| Path | Content |
|---|---|
| `service/` | The scan service (Python, Docker). Refer to [`service/README.md`](service/README.md) |
| `sane-backend/` | The SANE backend `ix1300`, the patch for sane-backends 1.3.1, and a test harness. Refer to [`sane-backend/README.md`](sane-backend/README.md) |
| `tools/ixusb.py` | Raw USB tool: SCSI commands, setup messages, host ID registration |
| `tools/setwifi.py` | Wi-Fi setup over USB |
| [`PROTOCOL.md`](PROTOCOL.md) | The USB setup messages and the Wi-Fi scan protocol |

## Tests

The tests use no scanner and no network:

```
python3 -m unittest discover -s service/tests
make -C sane-backend/tests
```

The service tests use only the Python standard library. Some tests need a
JPEG decoder (Pillow or `djpeg`). The SANE harness needs a C compiler and
the libjpeg headers.

## License

- The SANE backend (`sane-backend/`) is under the GNU General Public License
  version 2 or later, with the SANE exception (the same license as the other
  SANE backends). Refer to [`sane-backend/LICENSE`](sane-backend/LICENSE).
- All other files are under the MIT license. Refer to [`LICENSE`](LICENSE).

## Disclaimer

This project is not affiliated with, endorsed by, or supported by Ricoh,
PFU, or Fujitsu. ScanSnap is a trademark of PFU Limited. Other names can be
trademarks of their owners.

`PROTOCOL.md` describes only the messages on the USB cable and on the
network. The repository contains no vendor software and no vendor firmware.

Use this software at your own risk. It can cause paper jams, and it can
feed paper out without a scan. It can change the Wi-Fi settings and the
registered host ID of your scanner. There is no warranty (refer to the
license files).
