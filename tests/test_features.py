"""Round trips for every document-level and item-level feature, built on
Document.new() (a complete document, unlike tests/fake_document.py)."""
import io
import math
import os
import shutil
import tempfile
import unittest
import zlib
import struct

from goodnotes import schema as S
from goodnotes.model import (A4, ARROW_FILLED, DASHED, Box, Document, Image, Line, RichText, Sticky,
                             Stroke, TextRun, Template, blob_decompress, new_box, new_brush_stroke,
                             new_fountain_stroke, new_image, new_line, new_math, new_pencil_stroke,
                             new_shape_stroke, new_sticky, new_stroke, new_tape, new_text_box,
                             paper_pdf, pdf_pages, rich_text, uuid_plus)


def png(w=4, h=3, rgb=(255, 0, 0)):
    raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))

    def chunk(t, d):
        return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
    return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
            + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))


def reload(doc, d):
    out = os.path.join(d, "o.goodnotes")
    doc.save(out)
    return Document(out)


class NewDocumentTest(unittest.TestCase):
    def test_new_document_is_complete(self):
        doc = Document.new("Hello", pages=3, paper="dotted")
        self.assertEqual(len(doc.pages), 3)
        self.assertEqual(doc.title, "Hello")
        self.assertEqual(doc.files["schema.pb"], b"\x08\x18")  # {1: 24}
        kinds = sorted({f[0] for e in doc.events for f in e.fields if f[0] != 1})
        self.assertEqual(kinds, [2, 6, 10, 30, 54, 102, 104, 105])
        for e in doc.events:  # every event carries id, timestamp, device, clock, schema version
            body = next(f[2] for f in e.fields if f[0] != 1)
            for num in (10, 11):
                self.assertTrue(body.has(num), (body, num))
            self.assertTrue(any(body.int(n) == S.SCHEMA_VERSION for n in (15, 16, 19, 20, 21)), body)
        page = doc.pages[0]
        self.assertEqual(page.uuid, uuid_plus(page.page_id))
        self.assertEqual(page.event.path(3).str(1), page.layer.str(2))  # page -> template
        self.assertTrue(page.event.path(3).sub(2).has(2))  # stamp inside the value
        self.assertTrue(page.event.path(4).sub(2).has(2))
        aid, n = page.background
        self.assertTrue(doc.attachments[aid].startswith(b"%PDF"))
        self.assertEqual(n, 1)
        self.assertAlmostEqual(page.size[0], 834.24, places=2)
        with tempfile.TemporaryDirectory() as d:
            back = reload(doc, d)
            self.assertEqual([p.page_id for p in back.pages], [p.page_id for p in doc.pages])
            self.assertEqual(back.title, "Hello")
            back.title = "Renamed"
            self.assertEqual(reload(back, d).title, "Renamed")

    def test_paper_sizes_and_pdf_import(self):
        pdf = paper_pdf(A4, "grid")
        sizes = pdf_pages(pdf)
        self.assertAlmostEqual(sizes[0][0], 595.28, places=1)
        doc = Document.from_pdf(pdf, "A4")
        self.assertAlmostEqual(doc.pages[0].size[1], A4[1], places=1)
        doc.import_pdf(paper_pdf((400, 300), "lined"))
        self.assertEqual(len(doc.pages), 2)
        self.assertAlmostEqual(doc.pages[1].size[0], 400, places=1)
        self.assertEqual(doc.pages[1].background[1], 1)
        page = doc.import_image(png(40, 20))
        self.assertEqual(page.visible()[0].size[0] / page.visible()[0].size[1], 2.0)

    def test_page_attributes_outline_audio_comments(self):
        doc = Document.new("Attrs", pages=2)
        p0, p1 = doc.pages
        doc.set_bookmarked(p1)
        doc.set_rotation(p1, 270)
        doc.set_labels(p1, ["todo", "exam"])
        doc.set_read(p1)
        doc.favourite = True
        top = doc.add_outline("Chapter 1", p0)
        doc.add_outline("Section 1.1", p1, parent=top)
        gone = doc.add_outline("Scratch", p1)
        doc.remove_outline(gone)
        audio = doc.add_audio_note(b"RIFF....WAVE", 12.5, page=p1, name="Lecture", offsets=[0.0, 3.5])
        thread = doc.add_comment(p0, (100, 200), "First!", author="Taylor")
        doc.add_comment(p0, (0, 0), "Reply", author="Sam", thread=thread)
        doc.resolve_comment(thread)
        with tempfile.TemporaryDirectory() as d:
            back = reload(doc, d)
            b0, b1 = back.pages
            self.assertTrue(back.bookmarked(b1) and not back.bookmarked(b0))
            self.assertEqual(back.rotation(b1), 270)
            self.assertEqual(back.labels(b1), ["todo", "exam"])
            self.assertTrue(back.read(b1))
            self.assertTrue(back.favourite)
            self.assertEqual([(t, d_) for t, _, d_, _ in back.outline_tree()], [("Chapter 1", 0), ("Section 1.1", 1)])
            self.assertIs(back.outline[1][1], b1)
            note = back.audio_notes[0]
            self.assertEqual((note["name"], note["duration"]), ("Lecture", 12.5))
            self.assertEqual([t for t, _ in note["pages"]], [0.0, 3.5])
            self.assertEqual(back.attachments[note["attachment"]], b"RIFF....WAVE")
            c = back.comments()[0]
            self.assertEqual([x["text"] for x in c["comments"]], ["First!", "Reply"])
            self.assertEqual((c["position"], c["resolved"]), ((100.0, 200.0), True))
            self.assertIs(c["page"], b0)

    def test_page_operations(self):
        doc = Document.new("Pages", pages=1)
        page = doc.pages[0]
        a = new_stroke(doc, [("M", (0, 0)), ("L", (100, 100))])
        b = new_box(doc, (10, 10), (50, 50), fill=(1, 0, 0, 1))
        page.append(a)
        page.append(b)
        page.send_to_back(b)
        self.assertEqual([i.kind for i in page.items], ["box", "stroke"])
        page.bring_to_front(b)
        self.assertEqual([i.kind for i in page.items], ["stroke", "box"])
        gid = page.group_items([a, b])
        self.assertEqual(len(page.group(a)), 2)
        page.move(a, 5, 5)
        self.assertEqual(Box(b).origin, (15.0, 15.0))
        page.ungroup([a, b])
        c = page.duplicate(b)
        self.assertNotEqual(c.id, b.id)
        self.assertEqual(Box(c).origin, (35.0, 35.0))
        page.delete(a)
        self.assertTrue(a.deleted)
        self.assertEqual(len(page.visible()), 2)
        page.restore(a)
        dup = doc.duplicate_page(page)
        self.assertEqual(len(dup.visible()), 3)
        with tempfile.TemporaryDirectory() as d:
            back = reload(doc, d)
            self.assertEqual(len(back.pages), 2)
            self.assertEqual(len(back.pages[1].visible()), 3)

    def test_eraser_splits_ballpoint(self):
        doc = Document.new("Erase")
        page = doc.pages[0]
        page.append(new_stroke(doc, [("M", (0, 50)), ("L", (200, 50))], thickness=2))
        hit = page.erase(doc, (100, 50), 10)
        self.assertEqual(len(hit), 1)
        vis = page.visible()
        self.assertEqual(len(vis), 2)
        xs = sorted(p[0] for s in vis for p in s.points())
        self.assertTrue(all(x <= 90.5 or x >= 109.5 for x in xs), xs[:5])


class ItemsTest(unittest.TestCase):
    def setUp(self):
        self.doc = Document.new("Items")
        self.page = self.doc.pages[0]

    def test_variable_width_strokes_have_one_outline_per_segment(self):
        for builder in (new_fountain_stroke, new_brush_stroke):
            it = builder(self.doc, [("M", (10, 10)), ("Q", (60, 80), (120, 10)), ("L", (200, 40))],
                         width=4, pressure=lambda t: 0.5 + t)
            s = Stroke(it)
            t = s.template
            off = 2 if t.schema.startswith("vu") else 1
            kinds, counts, types = t.values[off], t.values[off + 3], t.values[off + 4]
            quads = sum(1 for k in kinds if k in (1, 3))
            self.assertEqual(len(counts), quads)
            self.assertEqual(sum(counts), len(types))
            self.assertEqual(len(s.widths), len(kinds))
            self.assertAlmostEqual(s.widths[0], 1.0, places=5)
            self.assertAlmostEqual(s.widths[-1], 3.0, places=5)
            self.assertEqual(len(s.points()), len(kinds))
            self.assertIsNotNone(s.bounds())
        self.assertEqual(Stroke(new_tape(self.doc, [("M", (0, 0)), ("L", (50, 0))])).pen, "fountain")
        self.assertTrue(Stroke(new_tape(self.doc, [("M", (0, 0)), ("L", (50, 0))])).tape)

    def test_stroke_properties(self):
        it = new_stroke(self.doc, [("M", (0, 0)), ("L", (10, 0))], (0, 0, 1, 1), 3, dash=[2, 1])
        s = Stroke(it)
        self.assertEqual(s.dash, ([2.0, 1.0], 1))
        s.translation = (5, 5)
        self.assertEqual(s.points()[0], (5.0, 5.0))
        s.translate(1, 1)  # ballpoint ink is rewritten, translation folded in
        self.assertEqual(s.translation, (0.0, 0.0))
        self.assertEqual(s.points()[0], (6.0, 6.0))
        s.highlighter = True
        self.assertTrue(s.highlighter)
        s.color = (1, 0, 0, 0.5)
        self.assertEqual(it.version, 2)
        p = Stroke(new_pencil_stroke(self.doc, [("M", (0, 0)), ("L", (30, 0))]))
        self.assertEqual(p.pen, "pencil")
        self.assertGreater(len(p.points()), 2)
        sh = Stroke(new_shape_stroke(self.doc, "rect", center=(50, 50), size=(20, 10)))
        self.assertEqual(sh.bounds()[2], 40 + sh.thickness)

    def test_rich_text_round_trip(self):
        runs = [TextRun("Title\n", size=32, bold=True, heading=1, align="center"),
                TextRun("plain ", color=(0, 0, 0, 1)),
                TextRun("link", link="https://example.com", underline=True),
                TextRun(" struck", strike=True, italic=True, highlight=(1, 1, 0, 1)),
                TextRun("\nitem one", list_style="bullet", indent=1, line_height=1.5, font="Courier", size=14)]
        rt = RichText.build(runs)
        it = new_text_box(self.doc, (20, 20), rt, max_width=400)
        self.page.append(it)
        with tempfile.TemporaryDirectory() as d:
            back = reload(self.doc, d)
            box = back.pages[0].visible()[0]
            self.assertTrue(box.is_text_box)
            self.assertEqual(box.sizing, "auto")
            self.assertEqual(box.b.path(21, 3).float(2), 400.0)
            spans = box.text.spans()
            self.assertEqual([s.text for s in spans], [r.text for r in runs])
            self.assertEqual((spans[0].heading, spans[0].align, spans[0].bold), (1, "center", True))
            self.assertEqual((spans[2].link, spans[2].underline), ("https://example.com", True))
            self.assertEqual((spans[3].strike, spans[3].italic, spans[3].highlight[:3]), (True, True, (1.0, 1.0, 0.0)))
            self.assertEqual((spans[4].list_style, spans[4].indent, spans[4].line_height, spans[4].font, spans[4].size),
                             ("bullet", 1, 1.5, "Courier", 14.0))
            self.assertEqual(len(box.text.paragraphs()), 3)
            rt2 = box.text
            rt2.set_style(bold=True)
            box.text = rt2
            self.assertTrue(all(s.bold for s in box.text.spans()))

    def test_box_properties(self):
        it = new_box(self.doc, (0, 0), (100, 60), fill=(0, 1, 0, 1), outline=(2, (0, 0, 0, 1), DASHED),
                     corner_radius=8, rotation=math.pi / 4, shadow=((0, 0, 0, 0.5), 4, (2, 2)), locked=True)
        b = Box(it)
        self.assertAlmostEqual(b.rotation, math.pi / 4, places=6)
        self.assertEqual(b.dash, DASHED)
        self.assertEqual(b.corner_radius, 8.0)
        self.assertEqual(b.shadow[1], 4.0)
        self.assertTrue(b.locked)
        self.assertFalse(b.is_text_box)
        b.vertices = [(0, 0), (1, 0), (0.5, 1)]
        self.assertEqual(b.geometry, "polygon")
        b.set_ellipse()
        self.assertEqual(b.geometry, "ellipse")
        b.fill = None
        self.assertIsNone(b.fill)
        b.outline = None
        self.assertIsNone(b.outline)
        b.set_auto_size(300)
        self.assertEqual(b.sizing, "auto")
        self.assertTrue(b.is_text_box)

    def test_sticky_line_image_math(self):
        st = Sticky(new_sticky(self.doc, (0, 0), text=rich_text("note"), author=("u1", "Taylor"),
                               rotation=0.1, expanded=False, show_author=True))
        self.assertEqual(st.author, ("u1", "Taylor"))
        self.assertFalse(st.expanded)
        self.assertTrue(st.show_author)
        self.assertAlmostEqual(st.rotation, 0.1, places=6)
        ln = Line(new_line(self.doc, (0, 0), (100, 50), mid=(30, 60), start_arrow=ARROW_FILLED))
        self.assertEqual(ln.mid, (30.0, 60.0))
        ln.set_points((1, 1), (99, 49))
        self.assertEqual((ln.start, ln.end), ((1.0, 1.0), (99.0, 49.0)))
        ln.width = 5
        ln.dash = DASHED
        ln.arrow = 0
        self.assertEqual((ln.width, ln.dash, ln.arrow, ln.start_arrow), (5.0, DASHED, 0, ARROW_FILLED))
        el = Line(new_line(self.doc, (0, 0), (50, 50), mid=(50, 0), elbow=True, vertical_first=True))
        self.assertEqual(el.knee, (50.0, 0.0))
        img = Image(new_image(self.doc, png(8, 4), (100, 100)))
        self.assertEqual((img.format, img.size), ("PNG", (8.0, 4.0)))
        pdf = paper_pdf((100, 100))
        stk = Image(new_image(self.doc, pdf, (50, 50), (40, 40), locked=True))
        self.assertTrue(stk.is_sticker and stk.locked)
        m = new_math(self.doc, r"x^2", png(), (50, 50), (40, 20))
        self.page.append(m)
        self.assertEqual(self.page.visible()[-1].latex, "x^2")
        self.assertEqual(self.page.visible()[-1].bounds()[2], 40.0)
        self.page.append(st.item)
        self.page.append(ln.item)
        self.page.append(el.item)
        self.page.append(img.item)
        self.page.append(stk.item)
        with tempfile.TemporaryDirectory() as d:
            back = reload(self.doc, d)
            self.assertEqual([i.kind for i in back.pages[0].items], ["math", "sticky", "line", "line", "image", "image"])


if __name__ == "__main__":
    unittest.main()
