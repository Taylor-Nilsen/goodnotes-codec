import os
import tempfile
import unittest

from goodnotes.model import Document
from goodnotes.svg import page_to_svg, parse_path, svg_to_items
from tests import fake_document


class SvgTest(unittest.TestCase):
    def test_parse_path(self):
        cmds = parse_path("M0 0 L10 0 c0 10 10 10 10 0 Z")
        self.assertEqual(cmds[0], ("M", (0.0, 0.0)))
        self.assertEqual(cmds[1], ("L", (10.0, 0.0)))
        self.assertEqual(cmds[-2][-1], (20.0, 0.0))
        self.assertEqual(cmds[-1], ("L", (0.0, 0.0)))

    def test_svg_round_trip(self):
        svg = ('<svg xmlns="http://www.w3.org/2000/svg"><g transform="translate(10,10)">'
               '<path d="M0 0 Q 50 50 100 0" stroke="#ff0000" stroke-width="3" fill="none"/>'
               '<rect x="0" y="100" width="80" height="40" fill="blue" stroke="black"/>'
               '<circle cx="200" cy="200" r="30" fill="none" stroke="green"/>'
               '<text x="0" y="300" font-size="20">Hello</text></g></svg>')
        with tempfile.TemporaryDirectory() as d:
            src = os.path.join(d, "in.svg")
            with open(src, "w") as f:
                f.write(svg)
            doc = Document(fake_document.make(os.path.join(d, "s.goodnotes")))
            for it in svg_to_items(doc, src):
                doc.pages[0].append(it)
            self.assertEqual([i.kind for i in doc.pages[0].items], ["stroke", "box", "box", "box"])
            out = page_to_svg(doc, doc.pages[0], background=False)
            self.assertIn("<path", out)
            self.assertIn("Hello", out)
            self.assertIn("<ellipse", out.replace("<polygon", "<ellipse"))


if __name__ == "__main__":
    unittest.main()
