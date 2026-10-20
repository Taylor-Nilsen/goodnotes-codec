"""Page sizes, GoodNotes' built-in Essentials paper, and custom backgrounds.

Every background ends up as a one-page PDF attachment drawn over the whole
page. Built-in paper is regenerated here at any size from the geometry of
GoodNotes' own template PDFs; images are wrapped in a page-sized PDF;
PDFs are used as they are (GoodNotes scales the PDF page to the page size).

Units: page units, like the rest of the library. GoodNotes draws its paper
PDFs at 6/11 of the page size (Standard: 455.04 x 588.45 pt PDF on an
834.24 x 1078.825 page), so SCALE page units = 1 PDF point.
"""
import io
import struct
import zlib

SCALE = 11 / 6  # page units per PDF point
_MM = 72 / 25.4  # points per mm


def _pt(w, h):
    return (w * SCALE, h * SCALE)


def _mm(w, h):
    return _pt(w * _MM, h * _MM)


# GoodNotes' page-size menu, portrait. Any (w, h) works as a custom size.
SIZES = {
    "standard": _pt(455.04, 588.45),
    "a3": _mm(297, 420),
    "a4": _mm(210, 297),
    "a5": _mm(148, 210),
    "a6": _mm(105, 148),
    "a7": _mm(74, 105),  # "A7 (iPhone)"
    "letter": _pt(612, 792),
    "tabloid": _pt(792, 1224),
}

# Essentials paper: name -> GoodNotes template id (layer field 9 prefix)
TEMPLATES = {
    "blank": "B03314A5-9B9F-556B-B373-4678E13D0DAC",
    "dotted": "CC365888-75C9-5DBF-B1D4-A7E8E0AD3B7F",
    "squared": "9FE8F365-4BEE-5057-8573-1A56C77CAC19",
    "narrow_ruled": "238EE861-EE9A-51BD-B010-832CD3C50D2C",
    "wide_ruled": "AA273C17-F440-5125-B34E-F9C3C62F89E2",
}

_LIGHT_INK = (0.8156863, 0.8235294, 0.827451)
_DARK_INK = (0.34509805, 0.34901962, 0.35686275)
# color -> (paper, line/dot color)
COLORS = {
    "white": ((1, 1, 1), _LIGHT_INK),
    "yellow": ((0.972549, 0.96862745, 0.9137255), _LIGHT_INK),
    "black": ((0.2, 0.2, 0.2), _DARK_INK),
}


def oriented(size, landscape: bool | None) -> tuple[float, float]:
    """`size` turned so its long side runs across (landscape) or down
    (portrait); None leaves it as given."""
    w, h = size
    if landscape is None or (w > h) == landscape:
        return (w, h)
    return (h, w)


# --- PDF writing ---------------------------------------------------------------

def _num(x: float) -> str:
    return f"{x:.4f}".rstrip("0").rstrip(".")


def _pdf(size_pt, content: bytes, image: bytes | None = None, mask: bytes | None = None) -> bytes:
    """One-page PDF; `image` (object 5, drawn as /Im0) and its soft mask
    (object 6) are full stream objects."""
    w, h = size_pt
    res = "/XObject << /Im0 5 0 R >>" if image else ""
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Count 1 /Kids [3 0 R] >>",
            (f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {_num(w)} {_num(h)}] /Contents 4 0 R "
             f"/Resources << {res} >> >>").encode(),
            _stream(b"", content)] + [o for o in (image, mask) if o]
    out = io.BytesIO()
    out.write(b"%PDF-1.7\n%\x80\x80\x80\x80\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(out.tell())
        out.write(f"{i} 0 obj\n".encode() + o + b"\nendobj\n")
    xref = out.tell()
    out.write(f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode())
    out.write(b"".join(f"{o:010d} 00000 n \n".encode() for o in offsets))
    out.write(f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode())
    return out.getvalue()


def _stream(dict_body: bytes, data: bytes, compress: bool = True) -> bytes:
    if compress:
        data = zlib.compress(data, 9)
        dict_body += b" /Filter /FlateDecode"
    return b"<< " + dict_body + f" /Length {len(data)} >>\nstream\n".encode() + data + b"\nendstream"


# --- built-in paper ------------------------------------------------------------

def paper_pdf(template: str = "blank", color: str = "white", size=SIZES["standard"]) -> bytes:
    """GoodNotes Essentials paper as a PDF for a page of `size` (page units).
    Pattern measured from GoodNotes' Standard paper PDFs (points, from the
    top-left corner); larger or smaller pages repeat it, they don't stretch it."""
    if template not in TEMPLATES:
        raise ValueError(f"unknown template {template!r}; one of {sorted(TEMPLATES)}")
    if color not in COLORS:
        raise ValueError(f"unknown color {color!r}; one of {sorted(COLORS)}")
    w, h = size[0] / SCALE, size[1] / SCALE
    paper, ink = COLORS[color]
    rgb = lambda c: " ".join(_num(v) for v in c)  # noqa: E731
    ops = [f"{rgb(paper)} rg 0 0 {_num(w)} {_num(h)} re f", f"{rgb(ink)} rg"]

    def band(top, x0=0.0, x1=w, thick=0.5):  # horizontal 0.5 pt rule, `top` from the page top
        ops.append(f"{_num(x0)} {_num(h - top - thick)} {_num(x1 - x0)} {_num(thick)} re f")

    if template == "dotted":  # r 0.5 dots on a 16 pt grid anchored at the top-left corner
        k = 0.5 * 0.5522847  # bezier circle handle
        for j in range(int(h // 16) + 1):
            y = h - 16 * j
            for i in range(int(w // 16) + 1):
                x = 16 * i
                ops.append(f"{_num(x + .5)} {_num(y)} m {_num(x + .5)} {_num(y - k)} {_num(x + k)} {_num(y - .5)} {_num(x)} {_num(y - .5)} c "
                           f"{_num(x - k)} {_num(y - .5)} {_num(x - .5)} {_num(y - k)} {_num(x - .5)} {_num(y)} c "
                           f"{_num(x - .5)} {_num(y + k)} {_num(x - k)} {_num(y + .5)} {_num(x)} {_num(y + .5)} c "
                           f"{_num(x + k)} {_num(y + .5)} {_num(x + .5)} {_num(y + k)} {_num(x + .5)} {_num(y)} c f")
    elif template == "squared":  # 0.5 pt lines ending on every 16 pt from the top-left
        j = 1
        while 16 * j - 0.5 < h:
            band(16 * j - 0.5)
            j += 1
        i = 1
        while 16 * i - 0.5 < w:
            ops.append(f"{_num(16 * i - 0.5)} 0 0.5 {_num(h)} re f")
            i += 1
    elif template in ("narrow_ruled", "wide_ruled"):
        first, step = (50.0, 17.5) if template == "narrow_ruled" else (49.5, 25.0)
        top = first
        # ponytail: GoodNotes' Standard paper stops at 30.95 pt (narrow) and 13.45 pt (wide)
        # above the bottom; a 13.2 pt floor reproduces both. Re-measure if another size disagrees.
        while top + 0.5 <= h - 13.2:
            band(top)
            top += step
    return _pdf((w, h), "\n".join(ops).encode())


# --- images --------------------------------------------------------------------

def _png_pixels(data: bytes) -> tuple[int, int, bytes, bytes | None]:
    """(width, height, RGB8 bytes, alpha8 bytes or None) of a PNG."""
    pos, chunks, idat = 8, {}, []
    while pos < len(data):
        n, kind = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if kind == b"IDAT":
            idat.append(body)
        else:
            chunks.setdefault(kind, body)
        if kind == b"IEND":
            break
    w, h, depth, ctype, _, _, interlace = struct.unpack(">IIBBBBB", chunks[b"IHDR"])
    if interlace:
        raise ValueError("interlaced PNG not supported; re-save it without interlacing")
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    bpp = max(1, channels * depth // 8)
    stride = (w * channels * depth + 7) // 8
    raw = zlib.decompress(b"".join(idat))
    rows, prev = [], bytearray(stride)
    for y in range(h):
        f = raw[y * (stride + 1)]
        row = bytearray(raw[y * (stride + 1) + 1:(y + 1) * (stride + 1)])
        for x in range(stride):
            a = row[x - bpp] if x >= bpp else 0
            b = prev[x]
            c = prev[x - bpp] if x >= bpp else 0
            if f == 1:
                row[x] = (row[x] + a) & 255
            elif f == 2:
                row[x] = (row[x] + b) & 255
            elif f == 3:
                row[x] = (row[x] + (a + b) // 2) & 255
            elif f == 4:
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                row[x] = (row[x] + (a if pa <= pb and pa <= pc else b if pb <= pc else c)) & 255
        rows.append(row)
        prev = row
    # samples -> 8 bit
    samples = []
    for row in rows:
        if depth == 8:
            s = list(row)
        elif depth == 16:
            s = list(row[::2])
        else:
            s = [(byte >> (8 - depth - k)) & ((1 << depth) - 1) for byte in row for k in range(0, 8, depth)]
            s = s[:w * channels]
            if ctype != 3:
                s = [v * 255 // ((1 << depth) - 1) for v in s]
        samples.append(s)
    rgb, alpha = bytearray(), bytearray()
    trns = chunks.get(b"tRNS")
    plte = chunks.get(b"PLTE")
    for s in samples:
        for i in range(w):
            if ctype == 3:
                k = s[i]
                rgb += plte[3 * k:3 * k + 3]
                alpha.append(trns[k] if trns and k < len(trns) else 255)
            elif ctype in (0, 4):
                g = s[i * channels]
                rgb += bytes((g, g, g))
                alpha.append(s[i * 2 + 1] if ctype == 4 else 255)
            else:
                rgb += bytes(s[i * channels:i * channels + 3])
                alpha.append(s[i * 4 + 3] if ctype == 6 else 255)
    has_alpha = ctype in (4, 6) or (ctype == 3 and trns)
    return w, h, bytes(rgb), bytes(alpha) if has_alpha else None


def _jpeg_info(data: bytes) -> tuple[int, int, int]:
    """(width, height, components) from a JPEG's start-of-frame marker."""
    pos = 2
    while pos < len(data):
        if data[pos] != 0xFF:
            pos += 1
            continue
        marker = data[pos + 1]
        if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
            pos += 2
            continue
        n = struct.unpack(">H", data[pos + 2:pos + 4])[0]
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            h, w, comps = struct.unpack(">HHB", data[pos + 5:pos + 10])
            return w, h, comps
        pos += 2 + n
    raise ValueError("JPEG without a frame header")


def image_pdf(data: bytes, size=SIZES["standard"]) -> bytes:
    """Wraps a PNG or JPEG (anything else via Pillow, if installed) in a
    PDF the shape of a `size` page, stretched to fill it."""
    w, h = size[0] / SCALE, size[1] / SCALE
    draw = f"q {_num(w)} 0 0 {_num(h)} 0 0 cm /Im0 Do Q".encode()
    if data[:3] == b"\xff\xd8\xff":
        iw, ih, comps = _jpeg_info(data)
        cs = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}[comps]
        decode = " /Decode [1 0 1 0 1 0 1 0]" if comps == 4 and b"Adobe" in data[:4096] else ""
        img = _stream(f"/Type /XObject /Subtype /Image /Width {iw} /Height {ih} /ColorSpace {cs} "
                      f"/BitsPerComponent 8 /Filter /DCTDecode{decode}".encode(), data, compress=False)
        return _pdf((w, h), draw, img)
    if data[:8] != b"\x89PNG\r\n\x1a\n":
        try:
            from PIL import Image as PILImage  # optional: HEIC/GIF/WebP/TIFF/BMP…
        except ImportError:
            raise ValueError("background must be a PDF, PNG or JPEG (install Pillow for other formats)") from None
        buf = io.BytesIO()
        PILImage.open(io.BytesIO(data)).save(buf, "PNG")
        data = buf.getvalue()
    iw, ih, rgb, alpha = _png_pixels(data)
    head = f"/Type /XObject /Subtype /Image /Width {iw} /Height {ih} /BitsPerComponent 8".encode()
    img = _stream(head + b" /ColorSpace /DeviceRGB" + (b" /SMask 6 0 R" if alpha else b""), rgb)
    mask = _stream(head + b" /ColorSpace /DeviceGray", alpha) if alpha else None
    return _pdf((w, h), draw, img, mask)
