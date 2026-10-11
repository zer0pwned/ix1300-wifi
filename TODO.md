# TODO

This list shows the work that is still open. The guided self-test
(`ix1300 selftest`, see `service/README.md`) does most hardware checks in one
session, and it records the data for the analysis.

## Tests on the scanner

Each item needs the scanner and some paper.

1. **Receipts longer than 89.5 cm.** Long page mode works on the scanner
   (tested with an 83 cm receipt on 2026-10-11). A receipt longer than the
   limit of the normal mode is not tested yet. Self-test step:
   `--steps long_receipt`.
2. **Double-feed detection on and off.** Use 2 sheets that are taped
   together with an overlap. Self-test step: `--steps double_feed`.
3. **Host ID registration over Wi-Fi (the write).** The read is tested. The
   write replaces the ID of all other computers, so ScanSnap Home on these
   computers must register again. Do this test only with the approval of
   the owner of the scanner.
4. **The effect of a new auto power-off time.** The scanner keeps the value,
   and a read gives it back (tested). Set 15 minutes and check that the
   scanner turns off after 15 minutes. Then set the old value again.
5. **Other scan modes.** Gray, black and white, 150, 200 and 600 dpi, and
   fixed paper sizes. Only colour at 300 dpi with automatic length is tested.
   First, add these checks to the self-test.
6. **OCR.** Make a PDF with a text layer from real scans, and examine the
   text. First, add this check to the self-test.
7. **SANE backend on the scanner.** The front slot (`Card Duplex`) and a stop
   with Ctrl-C during a scan.
8. **Cover open during a scan** (sense 3/80/02).

## Software

1. **Page and sequence checks of READ replies.** The service and the SANE
   backend log a wrong page number in a READ reply, but they keep the data.
   First, record real traces to learn which values the scanner sends. Then
   refuse the wrong replies. (Codex review 3, possible finding 2.)
2. **Emulator default for E6.** A new scanner gives `00 00` (no value set).
   The emulator gives `10 1E`.

## For the owner of the repository

1. Make the GitHub repository public when you are ready.

## Not planned

- Wi-Fi setup without a USB cable.
- A test with a real Paperless-ngx server.
- The scanner password and TLS mode.
- The carrier sheet.
