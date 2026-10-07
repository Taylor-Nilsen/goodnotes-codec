import os
import unittest

from goodnotes.model import (I32, LEN, VARINT, Msg, Template, blob_compress, blob_decompress,
                             f32, u32)


class CoreTest(unittest.TestCase):
    def test_msg_roundtrip_is_byte_exact(self):
        inner = Msg([[1, I32, b"\x00\x00\x80?"], [2, VARINT, (1 << 64) - 1]])
        m = Msg([[1, LEN, b"ABC"], [5, LEN, inner], [9, VARINT, 300]])
        raw = m.encode()
        again = Msg.parse(raw)
        self.assertEqual(again.encode(), raw)
        self.assertEqual(again.sub(5).float(1), 1.0)
        self.assertEqual(again.sub(5).sint64(2), -1)

    def test_lazy_sub_edits_persist(self):
        m = Msg.parse(Msg([[4, LEN, Msg([[1, VARINT, 7]])]]).encode())
        m.sub(4).set_int(1, 8)
        self.assertEqual(Msg.parse(m.encode()).sub(4).int(1), 8)

    def test_blob_roundtrip_including_multiblock(self):
        for n in (0, 14, 15, 300, 32768, 70000):
            body = os.urandom(n)
            self.assertEqual(blob_decompress(blob_compress(body)), body)

    def test_template_roundtrip(self):
        schema = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"
        t = Template(schema, [2, u32(1.5), [0, 1], [[u32(1), u32(2)]],
                              [[u32(3), u32(4), u32(5), u32(6)]], 1, [0.5]], extra=b"\x01\x02")
        back = Template.parse(t.encode())
        self.assertEqual((back.schema, back.values, back.extra), (t.schema, t.values, t.extra))
        self.assertEqual(f32(back.values[1]), 1.5)


if __name__ == "__main__":
    unittest.main()
