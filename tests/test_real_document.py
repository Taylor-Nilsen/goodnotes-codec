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

    def test_comment_events_match_goodnotes(self):
        """add_comment writes the fields and shapes GoodNotes 7 writes for a comment
        and a reply (real events from reports/import-20261020-104843)."""
        thread = Msg.parse(bytes.fromhex(
            "0a2438423932383145312d414432452d343333342d413435352d3441304634364141304537439208f7010a24384239"
            "32383145312d414432452d343333342d413435352d344130463436414130453743122443303338464641342d443941"
            "332d344533362d393135392d4341363232304533433244351801223e0a320a244533433931303430"
            "2d464233332d343843442d423137342d453745423637413636354539120a0d0080d04315004000441208080110b3"
            "caf584072a243333303135373341" "2d313139352d344445452d394344442d32333430443838434234453751e4832dff"
            "8e157a425a2434304637364241302d303736322d344139462d42453437" "2d33354231304144423430453368f0ab93"
            "ed9bedb09e0e70a88acfbf9134800118")).sub(130)
        reply = Msg.parse(bytes.fromhex(
            "0a2434424342413332432d343831312d343645352d383834422d364136453533414243334537aa08ff010a24344243"
            "42413332432d343831312d343645352d383834422d364136453533414243334537122438423932383145312d414432"
            "452d343333342d413435352d3441304634364141304537431a244330333846464134" "2d443941332d344533362d39"
            "3135392d43413632323045334332443522150a090a0761207265706c791208080110c1e083b309280232215f396564"
            "6635343236" "3730636532393865353666326433363831313834376462303a0d5461796c6f72204e696c73656e5156bc7b"
            "018f157a425a2443363944453543362d314432452d" "343439422d383539392d43433043333445433335314268f0ab"
            "93ed9bedb09e0e70ab8acfbf91347818")).sub(133)
        doc = Document.new("c")
        t = doc.add_comment(doc.pages[0], (417, 513), "a comment", author="Taylor Nilsen", author_id="_u")
        doc.add_comment(doc.pages[0], (0, 0), "a reply", author="Taylor Nilsen", author_id="_u", thread=t)
        mine_t = next(e.sub(130) for e in doc.events if e.has(130))
        mine_r = [e.sub(133) for e in doc.events if e.has(133)][1]
        shape = lambda m: {f[0] for f in m.fields}
        self.assertEqual(shape(mine_t), shape(thread))
        self.assertEqual(shape(mine_r), shape(reply))
        self.assertEqual((mine_t.int(3), mine_r.int(5)), (thread.int(3), reply.int(5)))  # 1, 2
        self.assertEqual(shape(mine_t.sub(4)), shape(thread.sub(4)))                       # {1 anchor, 2 stamp}
        self.assertEqual(shape(mine_t.path(4, 1)), shape(thread.path(4, 1)))               # {1 page, 2 point}
        self.assertEqual(mine_t.path(4, 2).int(1), thread.path(4, 2).int(1))               # stamp counter 1
        self.assertEqual(mine_r.sub(4).sub(1).encode(), reply.sub(4).sub(1).encode())     # {1 "a reply"}
        self.assertEqual(doc.comments()[0]["position"], (417.0, 513.0))

if __name__ == "__main__":
    unittest.main()
