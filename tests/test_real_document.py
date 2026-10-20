"""Checks against a real GoodNotes export (tests/fixtures)."""
import os
import tempfile
import unittest

from goodnotes.model import Document, _event_body

FIXTURE = os.path.join(os.path.dirname(__file__), "fixtures", "gn-mac-mixed-pens.goodnotes")


def layout(doc):
    """{event kind: field numbers its bodies use}"""
    out = {}
    for e in doc.events:
        kind = next(f[0] for f in e.fields if f[0] != 1)
        out.setdefault(kind, set()).update(f[0] for f in _event_body(e).fields)
    return out


class RealDocumentTest(unittest.TestCase):
    def test_real_export_passes_the_save_checks(self):
        self.assertEqual(Document(FIXTURE).problems(), [])

    def test_new_document_matches_real_event_layout(self):
        real, mine = layout(Document(FIXTURE)), layout(Document.new("x", "dotted", "black", pages=3))
        self.assertEqual(set(real), set(mine))
        for k in real:
            # 2.9 = built-in paper id (generated paper has none); 105.5 / 104.12 = re-index versions
            optional = {2: {9}, 105: {5}, 104: {12}}.get(k, set())
            self.assertEqual(real[k] - mine[k] - optional, set(), f"event {k} missing fields")

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


if __name__ == "__main__":
    unittest.main()
