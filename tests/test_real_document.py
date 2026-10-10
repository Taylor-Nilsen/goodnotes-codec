"""Checks against a real GoodNotes export (tests/fixtures)."""
import io
import os
import unittest
import zipfile

from goodnotes.model import Document, FillShape, Stroke, is_constant_width, new_fill_shape, new_fountain_stroke
from goodnotes.model import _event_kind
from goodnotes.svg import page_to_svg

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "gn-mac-mixed-pens.goodnotes")


class RealDocumentTest(unittest.TestCase):
    def setUp(self):
        self.doc = Document(FIXTURE)

    def test_round_trip_is_byte_identical(self):
        buf = io.BytesIO()
        self.doc.save(buf)
        buf.seek(0)
        with zipfile.ZipFile(FIXTURE) as a, zipfile.ZipFile(buf) as b:
            for name in a.namelist():
                self.assertEqual(a.read(name), b.read(name), name)

    def test_items_decode(self):
        page = self.doc.pages[0]
        kinds = {}
        for v in page.visible():
            key = (v.pen if isinstance(v, Stroke) else type(v).__name__)
            kinds[key] = kinds.get(key, 0) + 1
        self.assertEqual(kinds, {"fountain": 3, "ballpoint": 3, "pencil": 1, "FillShape": 1})
        for v in page.visible():
            if isinstance(v, Stroke):
                self.assertIsNotNone(v.points())
                t = v.template
                if t.schema.startswith("vA("):  # fountain / marker: one outline subpath per quad segment
                    counts, types = t.values[4], t.values[5]
                    self.assertEqual(len(counts), len(t.values[1]) - 1)
                    self.assertEqual(sum(counts), len(types))
                    self.assertTrue(all(c == 1 for c in t.values[10]))  # every cap arc is clockwise
        svg = page_to_svg(self.doc, page, background=False)
        self.assertEqual(svg.split("</defs>")[1].count("<path"), 6)  # one ballpoint stroke is empty
        self.assertIn("<ellipse", svg)

    def test_generated_document_matches_real_event_layout(self):
        """Document.new() writes the same event kinds with the same fields
        as a GoodNotes export (the fake test document does not, and
        crashes GoodNotes)."""
        def layout(doc):
            out = {}
            for e in doc.events:
                k = _event_kind(e)
                body = e.sub(k)
                out.setdefault(k, set()).update(f[0] for f in body.fields)
            return out
        real, mine = layout(self.doc), layout(Document.new("x"))
        self.assertEqual(set(real), set(mine))
        for k in real:
            # 2.9 = built-in paper id (custom paper has none); 105.5 / 104.12 = re-index versions
            optional = {2: {9}, 105: {5}, 104: {12}}.get(k, set())
            self.assertEqual(real[k] - mine[k] - optional, set(), f"event {k} missing fields")
        self.assertEqual(self.doc.files["schema.pb"][:1], Document.new("x").files["schema.pb"][:1])

    def test_fill_shape_and_fountain_follow_real_layout(self):
        doc = self.doc
        real = next(v for v in doc.pages[0].visible() if isinstance(v, FillShape))
        mine = FillShape(new_fill_shape(doc, "ellipse", center=(10, 10), size=(5, 3), angle=0.3))
        self.assertEqual({f[0] for f in real.b.fields}, {f[0] for f in mine.b.fields})
        real_vw = next(v for v in doc.pages[0].visible() if isinstance(v, Stroke) and v.pen == "fountain"
                       and 1 in v.template.values[1])
        mine_vw = Stroke(new_fountain_stroke(doc, [("M", (0, 0)), ("L", (30, 0)), ("L", (30, 30))]))
        self.assertEqual({f[0] for f in real_vw.b.fields}, {f[0] for f in mine_vw.b.fields})
        self.assertEqual(sorted(set(real_vw.template.values[5])), sorted(set(mine_vw.template.values[5])))


if __name__ == "__main__":
    unittest.main()
