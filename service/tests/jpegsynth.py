"""Make small valid baseline JPEG files without Pillow.

Each 8x8 block has a DC value and, for "text" blocks, some AC values.
The quantization table is all 1. The Huffman tables are small and custom:
  DC: the standard luminance DC code lengths (categories 0..11);
  AC: EOB (0x00) = "0", run 0 / size 10 (0x0A) = "10".
"""
import random
import struct

DC_BITS = [0, 1, 5, 1, 1, 1, 1, 1, 1, 0, 0, 0, 0, 0, 0, 0]
DC_VALS = list(range(12))
AC_BITS = [1, 1] + [0] * 14
AC_VALS = [0x00, 0x0A]


def _codes(bits, vals):
    codes, code, k = {}, 0, 0
    for length in range(1, 17):
        for _ in range(bits[length - 1]):
            codes[vals[k]] = (code, length)
            code += 1
            k += 1
        code <<= 1
    return codes


DC_CODES = _codes(DC_BITS, DC_VALS)
AC_CODES = _codes(AC_BITS, AC_VALS)


def _value(bits, v):
    """Write a coefficient value with its category. Return nothing."""
    cat = abs(v).bit_length()
    return cat, (v if v >= 0 else v - 1) & ((1 << cat) - 1)


def synth(width, height, components=1, content_rows=None, density=0.5, level=240, filler=128,
          dpi=300, seed=1, ac_count=8):
    """Return JPEG bytes.

    content_rows: pixel rows with paper (white + text blocks). The rows after it
    are flat gray filler, as the scanner pads the image to the window length.
    density: part of the paper blocks with "text" (0 = blank page).
    The result for the same arguments is always the same (a cache keeps it).
    """
    key = (width, height, components, content_rows, density, level, filler, dpi, seed, ac_count)
    data = _CACHE.get(key)
    if data is None:
        data = _synth(*key)
        _CACHE[key] = data
    return data


_CACHE = {}


def _dc_bits(diff):
    """The DC code and the value bits of DIFF, as a text of "0" and "1"."""
    cat, v = _value(None, diff)
    code, length = DC_CODES[cat]
    return format(code, "0%db" % length) + (format(v, "0%db" % cat) if cat else "")


def _ac_bits(v):
    code, length = AC_CODES[0x0A]
    return format(code, "0%db" % length) + format(_value(None, v)[1], "010b")


# The AC coefficient of a text block: magnitude 512..1023 and a sign.
_AC_POS = [_ac_bits(m) if m >= 512 else "" for m in range(1024)]
_AC_NEG = [_ac_bits(-m) if m >= 512 else "" for m in range(1024)]
_EOB = format(AC_CODES[0x00][0], "0%db" % AC_CODES[0x00][1])


def _synth(width, height, components, content_rows, density, level, filler, dpi, seed, ac_count):
    """The encoder of synth(). It writes the bits as text, then makes the bytes in one step.

    It uses the random numbers in the same order as Random.random, randint and choice,
    so that the image is the same as the image of the first encoder (bit by bit).
    """
    rng = random.Random(seed)
    rand, bits_of = rng.random, rng.getrandbits
    content_rows = height if content_rows is None else content_rows
    bw, bh = (width + 7) // 8, (height + 7) // 8
    crow = (content_rows + 7) // 8
    out = []
    put = out.append
    dc_cache = {}
    pred = [0] * components
    zero = _dc_bits(0) + _EOB                  # a component with no change and no AC values
    for by in range(bh):
        paper = by < crow
        for bx in range(bw):
            text = paper and density > 0 and rand() < density
            for c in range(components):
                if c == 0:
                    dc = ((level if paper else filler) - 128) * 8
                    if text:
                        r = bits_of(7)                 # randint(20, 120): 101 values, 7 bits
                        while r >= 101:
                            r = bits_of(7)
                        dc = (20 + r - 128) * 8
                else:
                    dc = 0
                diff = dc - pred[c]
                pred[c] = dc
                if not diff and not (text and c == 0):
                    put(zero)
                    continue
                s = dc_cache.get(diff)
                if s is None:
                    s = dc_cache[diff] = _dc_bits(diff)
                put(s)
                if text and c == 0:
                    for _ in range(ac_count):
                        m = bits_of(10)                # randint(512, 1023): 512 values, 10 bits
                        while m >= 512:
                            m = bits_of(10)
                        sign = bits_of(2)              # choice((1, -1)): 2 values, 2 bits
                        while sign >= 2:
                            sign = bits_of(2)
                        put(_AC_POS[512 + m] if sign == 0 else _AC_NEG[512 + m])
                put(_EOB)
    text_bits = "".join(out)
    pad = -len(text_bits) % 8
    text_bits += "1" * pad                     # fill the last byte with 1 bits
    n = len(text_bits) // 8
    data = int(text_bits, 2).to_bytes(n, "big") if n else b""
    data = data.replace(b"\xff", b"\xff\x00")   # byte stuffing

    out = bytearray(b"\xff\xd8")
    out += b"\xff\xe0" + struct.pack(">H5sBBBHHBB", 16, b"JFIF\0", 1, 1, 1, dpi, dpi, 0, 0)
    out += b"\xff\xdb" + struct.pack(">HB", 67, 0) + bytes([1] * 64)
    sof = struct.pack(">BHHB", 8, height, width, components)
    for c in range(components):
        sof += bytes([c + 1, 0x11, 0])
    out += b"\xff\xc0" + struct.pack(">H", 2 + len(sof)) + sof
    for cls, bl, vals in ((0, DC_BITS, DC_VALS), (1, AC_BITS, AC_VALS)):
        body = bytes([cls << 4]) + bytes(bl) + bytes(vals)
        out += b"\xff\xc4" + struct.pack(">H", 2 + len(body)) + body
    sos = bytes([components]) + b"".join(bytes([c + 1, 0x00]) for c in range(components)) + b"\x00\x3f\x00"
    out += b"\xff\xda" + struct.pack(">H", 2 + len(sos)) + sos
    out += data + b"\xff\xd9"
    return bytes(out)
