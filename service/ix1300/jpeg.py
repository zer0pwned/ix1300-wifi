"""Small JPEG header tools (standard library only).

All functions accept damaged data. They do not read past the end of the data.
"""
import struct

SOF_MARKERS = (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF)


def _segments(data):
    """Yield (marker, offset of marker, segment length) up to SOS.

    A segment goes out only if its full length is in DATA.
    """
    if data[:2] != b"\xff\xd8":
        return
    i = 2
    n = len(data)
    while i + 4 <= n:
        if data[i] != 0xFF:
            return
        m = data[i + 1]
        if m == 0xFF:              # fill byte
            i += 1
            continue
        if m in (0x01,) or 0xD0 <= m <= 0xD7:
            i += 2
            continue
        seglen = struct.unpack_from(">H", data, i + 2)[0]
        if seglen < 2 or i + 2 + seglen > n:
            return                 # damaged or cut data
        yield m, i, seglen
        if m == 0xDA:              # start of scan: stop
            return
        i += 2 + seglen


def sof(data):
    """Return (precision, height, width, components, offset) of the first SOF, or None."""
    for m, i, seglen in _segments(data):
        if m in SOF_MARKERS:
            if seglen < 8:
                return None
            p, h, w, nc = struct.unpack_from(">BHHB", data, i + 4)
            return p, h, w, nc, i
    return None


def info(data):
    """Return (width, height, components) from the first SOF marker, or None."""
    s = sof(data)
    return None if s is None else (s[2], s[1], s[3])


def check(data):
    """Return (width, height, components) of a JPEG that a PDF can show. Else raise ValueError."""
    s = sof(data)
    if s is None:
        raise ValueError("data is not a JPEG (no SOF)")
    p, h, w, nc, _ = s
    if p != 8 or nc not in (1, 3, 4) or not 0 < w <= 65500 or not 0 < h <= 65500:
        raise ValueError("JPEG not usable: %d bit, %d components, %dx%d" % (p, nc, w, h))
    return w, h, nc


def density(data):
    """Return (xdpi, ydpi) from the JFIF APP0 segment, or None."""
    for m, i, seglen in _segments(data):
        if m == 0xE0 and seglen >= 14 and data[i + 4:i + 9] == b"JFIF\0":
            unit = data[i + 11]
            x, y = struct.unpack_from(">HH", data, i + 12)
            if unit == 1 and x and y:
                return x, y
            if unit == 2 and x and y:
                return round(x * 2.54), round(y * 2.54)
            return None
    return None


def fix_height(data, lines):
    """Write LINES into the SOF height.

    The scanner pads the image to the window length. The real length comes
    from the pixel size read. The function only makes the height smaller.
    """
    s = sof(data)
    if s is None:
        return data
    old, i = s[1], s[4]
    if 0 < lines < old:
        return data[:i + 5] + struct.pack(">H", lines) + data[i + 7:]
    return data
