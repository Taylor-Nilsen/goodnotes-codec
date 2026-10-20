import math
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
import zlib
from pathlib import Path

from goodnotes.model import (ARROW_FILLED, ARROW_OPEN, DASHED, Document, RichText, TextRun,
                             new_box, new_brush_stroke, new_fountain_stroke, new_image, new_line,
                             new_math, new_pencil_stroke, new_shape_stroke, new_sticky, new_stroke,
                             new_text_box, rich_text)
from goodnotes.pdf import _main, _png_image, document_to_pdf, page_to_pdf_ops


def _png(w, h, color_type=6):
    """A small PNG (RGBA checkerboard by default) using every filter type."""
    ch = {0: 1, 2: 3, 4: 2, 6: 4}[color_type]
    raw, prev = bytearray(), bytearray(w * ch)
    for y in range(h):
        row = bytearray()
        for x in range(w):
            r, g, b, a = 255 * x // w, 255 * y // h, 128, 255 if (x // 4 + y // 4) % 2 else 40
            row += bytes({0: [r], 2: [r, g, b], 4: [r, a], 6: [r, g, b, a]}[color_type])
        f = y % 5
        out = bytearray()
        for i, v in enumerate(row):
            a_, b_ = row[i - ch] if i >= ch else 0, prev[i]
            c_ = prev[i - ch] if i >= ch else 0
            p = a_ + b_ - c_
            pred = [0, a_, b_, (a_ + b_) >> 1,
                    a_ if abs(p - a_) <= abs(p - b_) and abs(p - a_) <= abs(p - c_)
                    else b_ if abs(p - b_) <= abs(p - c_) else c_][f]
            out.append((v - pred) & 0xFF)
        raw += bytes([f]) + out
        prev = row

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d))
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, color_type, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(bytes(raw))) + chunk(b"IEND", b""))


def _jpeg(d):
    """A JPEG from ImageMagick, or None when it isn't installed."""
    if not shutil.which("convert"):
        return None
    out = os.path.join(d, "red.jpg")
    subprocess.run(["convert", "-size", "40x30", "xc:red", out], check=True, capture_output=True)
    return Path(out).read_bytes()


def _build(d):
    """A two-page document with one of every element on page 0."""
    doc = Document.new("T", pages=2)
    pg = doc.pages[0]
    items = [
        new_image(doc, _png(24, 16), (650, 120), (120, 80), math.radians(20)),
        new_math(doc, "x^2", _png(12, 8, 0), (760, 380), (60, 40)),
        new_stroke(doc, [("M", (40, 60)), ("Q", (120, 0), (200, 60))], (1, 0, 0, 1), 3),
        new_stroke(doc, [("M", (40, 100)), ("L", (360, 100))], (0, 0, 1, 1), 3, dash=[3, 2]),
        new_stroke(doc, [("M", (40, 140)), ("L", (360, 140))], (1, 0.9, 0, 0.5), 20, highlighter=True),
        new_fountain_stroke(doc, [("M", (40, 180)), ("Q", (200, 120), (360, 200))], (0, 0.5, 0, 1), 8),
        new_brush_stroke(doc, [("M", (40, 230)), ("Q", (200, 290), (360, 230))], (0.5, 0, 0.5, 1), 10),
        new_pencil_stroke(doc, [("M", (40, 270)), ("Q", (200, 330), (360, 270))]),
        new_shape_stroke(doc, "rect", center=(100, 380), size=(100, 60)),
        new_shape_stroke(doc, "ellipse", center=(250, 380), size=(60, 30), angle=0.4),
        new_shape_stroke(doc, "polyline", points=[(320, 350), (360, 410), (400, 350)]),
        new_box(doc, (40, 460), (200, 100), text=rich_text("Rotated box (wraps)", size=18),
                fill=(0.6, 0.8, 1, 1), outline=(2, (0, 0, 0.5, 1), DASHED), corner_radius=20, rotation=0.2),
        new_box(doc, (280, 460), (120, 80), fill=(1, 0.8, 0.6, 0.7), outline=(3, (0, 0, 0, 1)), ellipse=True),
        new_box(doc, (440, 460), (120, 100), fill=(0.8, 1, 0.6, 1), vertices=[(0.5, 0), (1, 1), (0, 1)]),
        new_text_box(doc, (40, 600), RichText.build([
            TextRun("Heading\n", size=20, heading=1), TextRun("Bold ", bold=True, size=16),
            TextRun("italic ", italic=True, size=16), TextRun("under\n", underline=True, size=16),
            TextRun("bullet\n", list_style="bullet", size=16), TextRun("café 中", align="right", size=16)]),
            max_width=360),
        new_sticky(doc, (40, 820), (200, 200), text=rich_text("Sticky"), author=("a1", "Ann"), rotation=-0.1),
        new_line(doc, (300, 830), (500, 830), arrow=ARROW_OPEN, start_arrow=ARROW_FILLED),
        new_line(doc, (300, 880), (500, 900), mid=(400, 940), arrow=ARROW_FILLED, dash=DASHED),
        new_line(doc, (550, 820), (760, 980), mid=(650, 900), elbow=True),
        new_line(doc, (550, 1000), (760, 1040), mid=(600, 1020), elbow=True, vertical_first=True),
    ]
    jpeg = _jpeg(d)
    if jpeg:
        items.append(new_image(doc, jpeg, (500, 120), (80, 60)))
    for it in items:
        pg.append(it)
    doc.pages[1].append(new_stroke(doc, [("M", (100, 100)), ("L", (700, 900))], (0, 0, 0, 1), 5))
    doc.set_rotation(doc.pages[1], 90)
    return doc


def _pdfinfo(path):
    if not shutil.which("pdfinfo"):
        return None
    out = subprocess.run(["pdfinfo", "-f", "1", "-l", "99", path], check=True, capture_output=True, text=True).stdout
    return out


class PdfTest(unittest.TestCase):
    def test_document_to_pdf(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _build(d)
            out = os.path.join(d, "out.pdf")
            document_to_pdf(doc, out)
            data = Path(out).read_bytes()
            self.assertTrue(data.startswith(b"%PDF"))
            self.assertIn(b"/Type /Page ", data)
            self.assertTrue(data.rstrip().endswith(b"%%EOF"))
            # every xref offset points at its object
            xref = int(re.search(rb"startxref\s+(\d+)", data).group(1))
            entries = re.findall(rb"(\d{10}) 00000 n", data[xref:])
            for n, off in enumerate(entries, 1):
                self.assertTrue(data[int(off):].startswith(f"{n} 0 obj".encode()), n)
            info = _pdfinfo(out)
            if info is None:
                self.skipTest("pdfinfo not installed")
            self.assertIn("Pages:           2", info)
            self.assertIn("455.04 x 588.45 pts", info)
            self.assertRegex(info, r"Page\s+2 rot:\s+90")

    def test_page_selection_cli(self):
        with tempfile.TemporaryDirectory() as d:
            src, out = os.path.join(d, "t.goodnotes"), os.path.join(d, "o.pdf")
            _build(d).save(src)
            _main([src, out, "--pages", "1", "--no-background"])
            data = Path(out).read_bytes()
            self.assertIn(b"/Count 1", data)
            self.assertIn(b"/Rotate 90", data)
            info = _pdfinfo(out)
            if info is not None:
                self.assertIn("Pages:           1", info)

    def test_page_ops(self):
        with tempfile.TemporaryDirectory() as d:
            doc = _build(d)
            content, res = page_to_pdf_ops(doc, doc.pages[0], background=False)
            text = content.decode("latin-1")
            self.assertRegex(text, r"^0\.545455 0 0 -0\.545455 0 588\.4[0-9]* cm\n")
            self.assertEqual(len(re.findall(r"(?m)(?:^| )q(?= |$)", text)), len(re.findall(r"(?m)(?:^| )Q(?= |$)", text)))
            self.assertIsNone(re.search(r"nan|inf", text))
            self.assertIn("Helvetica", res.fonts.values())
            self.assertIn("Helvetica-Bold", res.fonts.values())
            self.assertIn("Multiply", [g[2] for g in res.ext_gstates.values()])
            self.assertGreaterEqual(len(res.xobjects), 2)  # PNG + math (+ JPEG)
            self.assertIn("(caf\xe9 ?)", text)  # WinAnsi, unencodable -> '?'

    def test_png_decoding(self):
        rgba = _png_image(_png(9, 7), "k")
        self.assertIsNotNone(rgba.smask)
        color, alpha = zlib.decompress(rgba.data), zlib.decompress(rgba.smask.data)
        self.assertEqual(len(color), 9 * 7 * 3)
        for x, y in ((0, 0), (5, 3), (8, 6)):
            i = y * 9 + x
            self.assertEqual(tuple(color[i * 3:i * 3 + 3]), (255 * x // 9, 255 * y // 7, 128))
            self.assertEqual(alpha[i], 255 if (x // 4 + y // 4) % 2 else 40)
        rgb = _png_image(_png(9, 7, 2), "k2")  # opaque: compressed data passed through
        self.assertIn("/Predictor 15", rgb.decode_parms)
        self.assertIsNone(rgb.smask)


if __name__ == "__main__":
    unittest.main()
