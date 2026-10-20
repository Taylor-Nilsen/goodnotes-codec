"""Every page of the GoodNotes test kit exports to SVG and PDF with no element failing.
(The exporters normally skip a failing element; STRICT turns that into an error.)"""
import io
import tempfile
import unittest

from goodnotes import pdf, svg
from goodnotes.__main__ import make_testkit
from goodnotes.model import Document


class KitExportTest(unittest.TestCase):
    def setUp(self):
        svg.STRICT = pdf.STRICT = True

    def tearDown(self):
        svg.STRICT = pdf.STRICT = False

    def test_every_kit_page_exports(self):
        with tempfile.TemporaryDirectory() as d:
            for path in make_testkit(d):
                doc = Document(path)
                with self.subTest(path=path.rsplit("/", 1)[-1]):
                    for page in doc.pages:
                        svg.page_to_svg(doc, page)
                    pdf.document_to_pdf(doc, io.BytesIO())


if __name__ == "__main__":
    unittest.main()
