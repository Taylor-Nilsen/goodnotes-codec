import os
import tempfile
import unittest

from goodnotes.model import Document, key_between, uuid_plus
from tests import synthetic


class DocumentTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = synthetic.make(os.path.join(self.dir.name, "s.goodnotes"), pages=2)

    def tearDown(self):
        self.dir.cleanup()

    def test_load_save_is_lossless(self):
        doc = Document(self.path)
        out = os.path.join(self.dir.name, "o.goodnotes")
        doc.save(out)
        again = Document(out)
        self.assertEqual([m.encode() for m in again.events], [m.encode() for m in doc.events])
        self.assertEqual(doc.title, "Synthetic")

    def test_pages_ordered_by_key_and_sized(self):
        doc = Document(self.path)
        self.assertEqual([p.key for p in doc.pages], ["K0", "K1"])
        self.assertEqual(doc.pages[0].size, (834.239990234375, 1078.824951171875))
        self.assertEqual(doc.pages[0].uuid, uuid_plus(doc.pages[0].page_id))

    def test_key_between(self):
        for a, b in [(None, None), ("K0", "K1"), ("a", "a0"), (None, "4R"), ("z", None)]:
            k = key_between(a, b)
            self.assertTrue((a is None or a < k) and (b is None or k < b), (a, b, k))

    def test_add_move_delete_page(self):
        doc = Document(self.path)
        new = doc.add_page(index=0)
        self.assertIs(doc.pages[0], new)
        doc.move_page(new, 2)
        self.assertIs(doc.pages[-1], new)
        out = os.path.join(self.dir.name, "o.goodnotes")
        doc.save(out)
        self.assertEqual(len(Document(out).pages), 3)
        doc.delete_page(new)
        doc.save(out)
        self.assertEqual(len(Document(out).pages), 2)


if __name__ == "__main__":
    unittest.main()
