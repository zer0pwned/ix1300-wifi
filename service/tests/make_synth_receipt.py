"""Make the synthetic receipt pair in tests/data (synth_0_front.jpg, synth_1_back.jpg).

The pair is a model of one receipt sheet that the iX1300 scanned in duplex over
Wi-Fi at 300 dpi:

- back (print side): a narrow receipt (79 mm) at an angle of 3 degrees, with a
  logo block, a bar code and lines of fake text, on the gray scanner backing;
- front (blank side): the same paper (mirror image), with faint show-through only.

The text is not real text: each "character" is a few random strokes. The image
is 2592 x 2072 pixels (the size of a real scan of a 175 mm receipt).

The generator uses the Python standard library and the cjpeg program (libjpeg).
The output is the same for each run with the same cjpeg version.

Usage: python3 make_synth_receipt.py [OUTPUT_DIR]   (default: tests/data)
"""
import math
import os
import random
import shutil
import subprocess
import sys
import tempfile

W, H, DPI = 2592, 2072, 300
PAPER_W = int(round(79.0 / 25.4 * DPI))       # 933 pixels: 79 mm thermal paper
PAPER_L = 2010                                 # 170 mm
ANGLE = 3.0                                    # degrees
CX, CY = 1290.0, 1036.0                        # centre of the paper in the image
SEED = 1300

# Pixel classes. Each class has a base colour; a noise value (0..7) is added.
BACKING, PAPER, INK, SHOW = 0, 1, 2, 3
BASE = {BACKING: (197, 203, 211), PAPER: (245, 245, 242), INK: (44, 44, 50), SHOW: (226, 226, 224)}
NOISE = 5                                      # noise values 0..NOISE-1


def _rect(mask, u0, v0, u1, v1):
    u0, u1 = max(0, int(u0)), min(PAPER_W, int(u1))
    v0, v1 = max(0, int(v0)), min(PAPER_L, int(v1))
    if u1 <= u0:
        return
    row = b"\x01" * (u1 - u0)
    for v in range(v0, v1):
        mask[v * PAPER_W + u0:v * PAPER_W + u1] = row


def _glyph(mask, rng, u, v, cw=22, ch=32, t=6):
    """A fake character: 2 to 4 random strokes in a CW x CH cell."""
    strokes = [(0, 0, t, ch), (cw - t, 0, cw, ch), (0, 0, cw, t), (0, ch - t, cw, ch),
               (0, (ch - t) // 2, cw, (ch + t) // 2), ((cw - t) // 2, 0, (cw + t) // 2, ch)]
    for s in rng.sample(strokes, rng.randint(2, 4)):
        _rect(mask, u + s[0], v + s[1], u + s[2], v + s[3])


def _text(mask, rng, u, v, n, cw=22, gap=6):
    for i in range(n):
        if rng.random() < 0.15:
            continue                           # a space
        _glyph(mask, rng, u + i * (cw + gap), v, cw)


def ink_mask():
    """The print of the receipt (1 = ink) in paper coordinates (u across, v along)."""
    rng = random.Random(SEED)
    m = bytearray(PAPER_W * PAPER_L)
    # Logo: a dark block with light fake letters.
    _rect(m, 290, 80, 650, 300)
    # The light letters: clear windows in the dark block.
    for i in range(3):
        for v in range(135, 235):
            a = v * PAPER_W + 345 + i * 105
            m[a:a + 40] = b"\x00" * 40
    _text(m, rng, 400, 330, 5, cw=18)
    # Bar code.
    u = 200
    while u < 730:
        bw = rng.choice((4, 6, 8, 12))
        _rect(m, u, 420, u + bw, 560)
        u += bw + rng.choice((4, 6, 8))
    # Address block (centred lines).
    v = 610
    for n in (16, 14, 18, 15, 12):
        _text(m, rng, (PAPER_W - n * 28) // 2, v, n)
        v += 46
    v += 60
    for n in (20, 17):
        _text(m, rng, (PAPER_W - n * 28) // 2, v, n)
        v += 46
    v += 60
    # Item lines: text on the left, an amount on the right.
    for _ in range(12):
        _text(m, rng, 70, v, rng.randint(8, 18))
        if rng.random() < 0.7:
            _text(m, rng, PAPER_W - 70 - 6 * 28, v, 6)
        v += 46
    # Dashed separator.
    v += 20
    for u in range(70, PAPER_W - 70, 30):
        _rect(m, u, v, u + 18, v + 5)
    v += 40
    # Totals.
    for _ in range(4):
        _text(m, rng, 420, v, 8)
        _text(m, rng, PAPER_W - 70 - 6 * 28, v, 6)
        v += 46
    assert v < PAPER_L - 60, v
    return m


def labels(mask, mirror):
    """Yield one row of pixel classes for each image row."""
    r = math.radians(ANGLE)
    c, s = math.cos(r), math.sin(r)
    cu, cv = PAPER_W / 2.0, PAPER_L / 2.0
    on_ink = INK if not mirror else SHOW
    for y in range(H):
        row = bytearray(W)                     # all BACKING (0)
        dy = y - CY
        # u = cu + dx*c + dy*s, v = cv - dx*s + dy*c (dx = x - CX); find the x range with paper.
        lo, hi = 0, W - 1
        ok = True
        for (k, base, top) in ((c, cu + dy * s, PAPER_W), (-s, cv + dy * c, PAPER_L)):
            # 0 <= base + k*dx < top
            if abs(k) < 1e-12:
                if not 0 <= base < top:
                    ok = False
                continue
            a, b = (0 - base) / k, (top - base) / k
            if a > b:
                a, b = b, a
            lo = max(lo, int(math.ceil(CX + a)))
            hi = min(hi, int(math.floor(CX + b - 1e-9)))
        if ok and lo <= hi:
            for x in range(lo, hi + 1):
                dx = x - CX
                u = int(cu + dx * c + dy * s)
                v = int(cv - dx * s + dy * c)
                if 0 <= u < PAPER_W and 0 <= v < PAPER_L:
                    if mirror:
                        u = PAPER_W - 1 - u
                    row[x] = on_ink if mask[v * PAPER_W + u] else PAPER
        if mirror:
            row.reverse()                      # the other face of the sheet: mirror image
        yield bytes(row)


def ppm(mask, mirror, seed):
    rng = random.Random(seed)
    tables = []
    for ch in range(3):
        t = bytearray(256)
        for cls, rgb in BASE.items():
            for n in range(8):
                t[cls * 8 + n] = max(0, min(255, rgb[ch] + n - NOISE // 2))
        tables.append(bytes(t))
    noise_tab = bytes(v % NOISE for v in range(256))
    out = bytearray(b"P6\n%d %d\n255\n" % (W, H))
    rgb = bytearray(3 * W)
    for lab in labels(mask, mirror):
        for ch in range(3):
            noise = rng.randbytes(W).translate(noise_tab)
            idx = ((int.from_bytes(lab, "big") << 3) | int.from_bytes(noise, "big")).to_bytes(W, "big")
            rgb[ch::3] = idx.translate(tables[ch])
        out += rgb
    return bytes(out)


def set_density(jpeg, dpi=DPI):
    """Write DPI into the JFIF APP0 segment (cjpeg writes "no unit, 1:1")."""
    i = jpeg.find(b"JFIF\x00")
    if jpeg[:4] != b"\xff\xd8\xff\xe0" or i != 6:
        raise ValueError("no JFIF APP0 segment")
    b = bytearray(jpeg)
    b[i + 7] = 1                               # units: dots per inch
    b[i + 8:i + 12] = bytes([dpi >> 8, dpi & 255, dpi >> 8, dpi & 255])
    return bytes(b)


def main(argv):
    out_dir = argv[1] if len(argv) > 1 else os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    cjpeg = shutil.which("cjpeg")
    if not cjpeg:
        sys.exit("cjpeg not found (libjpeg-turbo-progs or libjpeg-turbo)")
    mask = ink_mask()
    os.makedirs(out_dir, exist_ok=True)
    for name, mirror, seed in (("synth_0_front.jpg", True, 1), ("synth_1_back.jpg", False, 2)):
        with tempfile.NamedTemporaryFile(suffix=".ppm") as tmp:
            tmp.write(ppm(mask, mirror, seed))
            tmp.flush()
            r = subprocess.run([cjpeg, "-quality", "91", "-sample", "2x1", tmp.name],
                               capture_output=True, check=True)
        path = os.path.join(out_dir, name)
        with open(path, "wb") as fh:
            fh.write(set_density(r.stdout))
        print("%s: %d bytes" % (path, len(r.stdout)))


if __name__ == "__main__":
    main(sys.argv)
