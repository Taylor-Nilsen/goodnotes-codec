import math
import os
import tempfile
import unittest

from goodnotes.model import (ARROW_FILLED, DASHED, Document, new_box, new_image, new_line,
                             new_pencil_stroke, new_shape_stroke, new_sticky, new_stroke,
                             page_link, rich_text)
from tests import synthetic

PNG = b"\x89PNG\r\n\x1a\n" + b"\0" * 32


class BuildersTest(unittest.TestCase):
    def test_every_builder_round_trips(self):
        with tempfile.TemporaryDirectory() as d:
            doc = Document(synthetic.make(os.path.join(d, "s.goodnotes")))
            pg = doc.pages[0]
            items = [
                new_stroke(doc, [("M", (1, 2)), ("Q", (3, 4), (5, 6)), ("L", (7, 8))], (1, 0, 0, 1), 2.0),
                new_stroke(doc, [("M", (0, 0)), ("L", (100, 0))], (1, 1, 0, 0.5), 20, highlighter=True),
                new_pencil_stroke(doc, [("M", (0, 0)), ("L", (50, 50))]),
                new_shape_stroke(doc, "ellipse", center=(10, 10), size=(5, 3)),
                new_box(doc, (10, 10), (100, 50), text=rich_text("hi", link="https://example.com"),
                        fill=(0, 1, 0, 1), outline=(2, (0, 0, 0, 1), DASHED), ellipse=True),
                new_sticky(doc, (0, 0), text=rich_text("note")),
                new_line(doc, (0, 0), (10, 10), start_arrow=ARROW_FILLED, elbow=True),
                new_image(doc, PNG, (50, 50), (20, 10), math.radians(30)),
            ]
            for it in items:
                pg.append(it)
            doc.set_bookmarked(pg)
            doc.set_rotation(pg, 90)
            doc.add_outline("Start", pg)
            out = os.path.join(d, "o.goodnotes")
            doc.save(out)
            back = Document(out)
            p = back.pages[0]
            kinds = [i.kind for i in p.items]
            self.assertEqual(kinds, ["stroke"] * 4 + ["box", "sticky", "line", "image"])
            views = p.visible()
            self.assertEqual(views[0].commands[0], ("M", (1.0, 2.0)))
            self.assertTrue(views[1].highlighter)
            self.assertEqual(views[2].pen, "pencil")
            self.assertIsNotNone(views[3].shape)
            self.assertEqual(views[4].text.text, "hi")
            self.assertEqual(views[5].text.text, "note")
            self.assertTrue(views[6].elbow)
            self.assertAlmostEqual(views[7].angle, math.radians(30), places=5)
            self.assertEqual(back.attachments[views[7].attachment], PNG)
            self.assertTrue(back.bookmarked(p))
            self.assertEqual(back.rotation(p), 90)
            self.assertEqual(back.outline[0][0], "Start")
            self.assertIn("anchor=", page_link(back, p))

    def test_move_keeps_geometry(self):
        with tempfile.TemporaryDirectory() as d:
            doc = Document(synthetic.make(os.path.join(d, "s.goodnotes")))
            pg = doc.pages[0]
            pg.append(new_box(doc, (10, 20), (30, 40)))
            pg.move(pg.items[0], 5, -5)
            self.assertEqual(pg.visible()[0].origin, (15.0, 15.0))


if __name__ == "__main__":
    unittest.main()
