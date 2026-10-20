"""Checks against a real GoodNotes export (tests/fixtures)."""
import io
import os
import tempfile
import unittest
import zipfile

from goodnotes.model import Document, FillShape, Stroke, is_constant_width, new_fill_shape, new_fountain_stroke
from goodnotes.model import Msg, _audio_refs, _event_kind
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
        real, mine = layout(self.doc), layout(Document.new("x", "dotted", "black", pages=3))
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

    def test_real_export_passes_the_save_checks(self):
        self.assertEqual(Document(FIXTURE).problems(), [])

    def test_new_document_saves_and_reloads(self):
        doc = Document.new("HW", "dotted", "black", pages=2)
        doc.add_page()
        with tempfile.TemporaryDirectory() as d:
            out = os.path.join(d, "o.goodnotes")
            doc.save(out)
            again = Document(out)
        self.assertEqual(again.title, "HW")
        self.assertEqual([p.uuid for p in again.pages], [p.uuid for p in doc.pages])
        self.assertEqual(again.files["schema.pb"], b"\x08\x18")  # {1: 24}; the real export has {1: 25}


    def test_audio_references_decode_real_event(self):
        """An audioNoteReferencesUpdated (161) event from a real GoodNotes library
        (reports/import-report-20261020-100658.txt): references are
        {1 map<ref id, {1 timestampFlicks, 2 pageId}>}; this ref has no timestamp."""
        raw = bytes.fromhex(
            "0a2433384441454443382d363833322d344133342d414641342d3636323443333231333938378a0a83020a24"
            "33384441454443382d363833322d344133342d414641342d363632344333323133393837122444414537353832"
            "382d324336442d343045452d413437332d4542364444453831394544331a500a4e0a24433241464646304"
            "52d393337312d344133342d393135382d4139353243393830334146351226122439313445433642392d4637"
            "39342d343230412d394533452d37363931364131393339453451604728"
            "90df9979425a2439344244343142362d384536462d343644362d424545332d41423944353830393743424262"
            "215f396564663534323637306365323938653536663264333638313138343764623068f0ab93ed9bedb09e0e"
            "70a4a5eec392337818")
        body = Msg.parse(raw).sub(161)
        page = "914EC6B9-F794-420A-9E3E-76916A1939E4"
        self.assertEqual(_audio_refs(body.sub(3), {page: "p"}), [(0.0, "p")])

if __name__ == "__main__":
    unittest.main()
