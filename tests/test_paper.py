import os
import re
import struct
import tempfile
import unittest
import zlib

from goodnotes import paper
from goodnotes.model import Document
from tests import fake_document


def png(w, h, rgba):
    """Tiny RGBA PNG, every pixel `rgba`."""
    def chunk(kind, body):
        return struct.pack(">I", len(body)) + kind + body + struct.pack(">I", zlib.crc32(kind + body))
    rows = (b"\0" + bytes(rgba) * w) * h
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 6, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b""))


def mediabox(pdf):
    return tuple(float(v) for v in re.search(rb"/MediaBox \[0 0 ([\d.]+) ([\d.]+)\]", pdf).groups())


class PaperTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = fake_document.make(os.path.join(self.dir.name, "s.goodnotes"))

    def tearDown(self):
        self.dir.cleanup()

    def reload(self, doc):
        out = os.path.join(self.dir.name, "o.goodnotes")
        doc.save(out)
        return Document(out)

    def test_sizes(self):
        self.assertEqual(paper.SIZES["standard"], (834.24, 1078.825))
        w, h = paper.SIZES["a4"]
        self.assertAlmostEqual(w / paper.SCALE, 595.28, 2)
        self.assertAlmostEqual(h / paper.SCALE, 841.89, 2)
        self.assertEqual(paper.oriented((1, 2), True), (2, 1))
        self.assertEqual(paper.oriented((2, 1), False), (1, 2))
        self.assertEqual(paper.oriented((2, 1), None), (2, 1))

    def test_every_template_and_color_at_any_size(self):
        for size in (paper.SIZES["standard"], paper.oriented(paper.SIZES["a3"], True), (300, 900)):
            for t in paper.TEMPLATES:
                for c in paper.COLORS:
                    pdf = paper.paper_pdf(t, c, size)
                    self.assertEqual(mediabox(pdf), tuple(round(v / paper.SCALE, 4) for v in size))
        with self.assertRaises(ValueError):
            paper.paper_pdf("lined", "white")

    def test_ruled_lines_match_goodnotes_standard(self):
        # GoodNotes' Standard paper shows 30 narrow rules, 22 wide rules, 36 + 28 squared lines
        for t, n in (("narrow_ruled", 30), ("wide_ruled", 22), ("squared", 64)):
            body = zlib.decompress(re.search(rb"stream\n(.*)\nendstream", paper.paper_pdf(t), re.S).group(1))
            self.assertEqual(body.count(b" re f") - 1, n, t)

    def test_set_paper_resizes_page_and_round_trips(self):
        doc = Document(self.path)
        page = doc.pages[0]
        aid = doc.set_paper(page, "dotted", "black", paper.SIZES["letter"], landscape=True)
        again = self.reload(doc)
        p = again.pages[0]
        self.assertEqual(p.background, (aid, 1))
        self.assertAlmostEqual(p.size[0], 792 * paper.SCALE, 3)
        self.assertAlmostEqual(p.size[1], 612 * paper.SCALE, 3)
        self.assertIsNone(p.template)
        self.assertAlmostEqual(mediabox(again.attachments[aid])[0], 792, 3)

    def test_default_size_is_the_pages(self):
        doc = Document(self.path)
        doc.set_paper(doc.pages[0], "squared")
        self.assertEqual(doc.pages[0].size, (834.239990234375, 1078.824951171875))

    def test_custom_backgrounds(self):
        doc = Document(self.path)
        page = doc.pages[0]
        aid = doc.set_background(page, png(3, 2, (255, 0, 0, 128)), size=(400, 600))
        pdf = doc.attachments[aid]
        self.assertTrue(pdf.startswith(b"%PDF"))
        self.assertIn(b"/SMask 6 0 R", pdf)
        self.assertEqual(page.size, (400.0, 600.0))

        src = os.path.join(self.dir.name, "bg.pdf")
        with open(src, "wb") as f:
            f.write(paper.paper_pdf("wide_ruled", "yellow"))
        aid = doc.set_background(page, src, size=paper.SIZES["a5"], landscape=True)
        with open(src, "rb") as f:
            self.assertEqual(doc.attachments[aid], f.read())  # PDFs go in untouched
        self.assertGreater(page.size[0], page.size[1])

        # another notebook's page paper
        other = os.path.join(self.dir.name, "other.goodnotes")
        self.reload(doc).save(other)
        aid2 = doc.set_background(page, other)
        self.assertEqual(doc.attachments[aid2], doc.attachments[aid])

    def test_jpeg_header(self):
        jpg = b"\xff\xd8\xff\xe0\x00\x04ab\xff\xc0\x00\x0b\x08\x00\x20\x00\x40\x03\x01\x22\x00\xff\xd9"
        self.assertEqual(paper._jpeg_info(jpg), (64, 32, 3))
        self.assertIn(b"/DCTDecode", paper.image_pdf(jpg))


if __name__ == "__main__":
    unittest.main()
