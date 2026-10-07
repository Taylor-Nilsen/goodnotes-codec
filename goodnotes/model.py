"""Lossless read/write model of a .goodnotes document.

Two layers:

1. **Lossless core.** Every protobuf record is parsed into a `Msg` tree that
   re-encodes byte-for-byte, and every `bv41` blob into a `Blob` whose
   decompressed body is either a schema-driven template (`Template`, e.g.
   stroke geometry) or a nested protobuf (e.g. rich text). Nothing is
   interpreted unless asked for, so features that haven't been decoded
   still survive a load/save, and can be edited at field level.

2. **Typed views** (`Stroke`, `Box`, `Sticky`, `Line`, `Image`, `Math`) over
   the core, with properties for the parts that are understood: position,
   size, color, text, geometry, and so on. Setting a property edits the
   underlying `Msg` in place.

Field numbers come from comparing records against documents built in the
GoodNotes macOS app with one feature at a time (see docs/FORMAT_NOTES.md,
"Element catalog").
"""
from __future__ import annotations

import math
import random
import re
import struct
import time
import uuid as uuidlib
import zipfile

from .lz4_block import decompress_block

# --- lossless protobuf -------------------------------------------------------

VARINT, I64, LEN, SGROUP, EGROUP, I32 = 0, 1, 2, 3, 4, 5


def _read_varint(buf: bytes, pos: int) -> tuple[int, int]:
    result = shift = 0
    while True:
        b = buf[pos]
        pos += 1
        result |= (b & 0x7F) << shift
        if not b & 0x80:
            return result, pos
        shift += 7


def _varint(n: int) -> bytes:
    out = bytearray()
    while True:
        b = n & 0x7F
        n >>= 7
        if n:
            out.append(b | 0x80)
        else:
            out.append(b)
            return bytes(out)


class Msg:
    """A protobuf message as an ordered list of [field, wire type, value].

    Values: int (varint), 8 raw bytes (I64), 4 raw bytes (I32), bytes or a
    parsed Msg (LEN), Msg (group). Fixed-width values stay raw bytes so
    floats round-trip bit-exactly. A LEN value is only parsed into a Msg
    when sub() asks for it, since bytes and submessages look alike on the
    wire.
    """
    __slots__ = ("fields",)

    def __init__(self, fields: list | None = None):
        self.fields = fields if fields is not None else []

    # parse / encode
    @classmethod
    def parse(cls, buf: bytes) -> "Msg":
        msg, pos = cls._parse(buf, 0, len(buf), None)
        return msg

    @classmethod
    def _parse(cls, buf, pos, end, group):
        fields = []
        while pos < end:
            tag, pos = _read_varint(buf, pos)
            num, wt = tag >> 3, tag & 7
            if wt == VARINT:
                v, pos = _read_varint(buf, pos)
            elif wt == I64:
                v, pos = buf[pos:pos + 8], pos + 8
            elif wt == LEN:
                n, pos = _read_varint(buf, pos)
                v, pos = buf[pos:pos + n], pos + n
            elif wt == I32:
                v, pos = buf[pos:pos + 4], pos + 4
            elif wt == SGROUP:
                v, pos = cls._parse(buf, pos, end, num)
            elif wt == EGROUP and num == group:
                return cls(fields), pos
            else:
                raise ValueError(f"bad wire type {wt} for field {num}")
            if pos > end:
                raise ValueError("truncated message")
            fields.append([num, wt, v])
        if group is not None:
            raise ValueError(f"unterminated group {group}")
        return cls(fields), pos

    def encode(self) -> bytes:
        out = bytearray()
        for num, wt, v in self.fields:
            out += _varint(num << 3 | wt)
            if wt == VARINT:
                out += _varint(v)
            elif wt == LEN:
                b = v.encode() if isinstance(v, Msg) else v
                out += _varint(len(b)) + b
            elif wt == SGROUP:
                out += v.encode() + _varint(num << 3 | EGROUP)
            else:
                out += v
        return bytes(out)

    def copy(self) -> "Msg":
        return Msg.parse(self.encode())

    # read
    def has(self, num: int) -> bool:
        return any(f[0] == num for f in self.fields)

    def get(self, num: int, default=None):
        for f in self.fields:
            if f[0] == num:
                return f[2]
        return default

    def all(self, num: int) -> list:
        return [f[2] for f in self.fields if f[0] == num]

    def int(self, num: int, default=0) -> int:
        v = self.get(num)
        return v if isinstance(v, int) else default

    def sint64(self, num: int, default=0) -> int:
        """A varint read as signed 64-bit (GoodNotes stores e.g. -1 as 2^64-1)."""
        v = self.get(num)
        if not isinstance(v, int):
            return default
        return v - (1 << 64) if v >= 1 << 63 else v

    def float(self, num: int, default=0.0) -> float:
        v = self.get(num)
        if isinstance(v, (bytes, bytearray)) and len(v) == 4:
            return struct.unpack("<f", v)[0]
        if isinstance(v, (bytes, bytearray)) and len(v) == 8:
            return struct.unpack("<d", v)[0]
        return default

    def str(self, num: int, default: str | None = None) -> str | None:
        v = self.get(num)
        return v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else default

    def bytes(self, num: int) -> bytes | None:
        v = self.get(num)
        return v.encode() if isinstance(v, Msg) else v

    def _parsed(self, f) -> "Msg":
        if not isinstance(f[2], Msg):
            f[2] = Msg.parse(f[2])
        return f[2]

    def sub(self, num: int, create: bool = False) -> "Msg | None":
        """The submessage at `num`, parsed in place so edits persist."""
        for f in self.fields:
            if f[0] == num and f[1] == LEN:
                return self._parsed(f)
        if create:
            m = Msg()
            self.fields.append([num, LEN, m])
            return m
        return None

    def subs(self, num: int) -> list["Msg"]:
        return [self._parsed(f) for f in self.fields if f[0] == num and f[1] == LEN]

    def path(self, *nums: int, create: bool = False) -> "Msg | None":
        m = self
        for n in nums:
            m = m.sub(n, create)
            if m is None:
                return None
        return m

    # write: replace the first occurrence in place (keeps field order) or append
    def _put(self, num: int, wt: int, v) -> None:
        for f in self.fields:
            if f[0] == num:
                f[1], f[2] = wt, v
                return
        self.fields.append([num, wt, v])

    def set_int(self, num: int, v: int) -> None:
        self._put(num, VARINT, v & ((1 << 64) - 1))

    def set_float(self, num: int, v: float) -> None:
        self._put(num, I32, struct.pack("<f", v))

    def set_double(self, num: int, v: float) -> None:
        self._put(num, I64, struct.pack("<d", v))

    def set_bytes(self, num: int, v: "bytes | str | Msg") -> None:
        self._put(num, LEN, v.encode() if isinstance(v, str) else v)

    def remove(self, num: int) -> None:
        self.fields = [f for f in self.fields if f[0] != num]

    def add(self, num: int, wt: int, v) -> None:
        self.fields.append([num, wt, v])

    def __repr__(self) -> str:
        return dump(self, max_depth=2)


def dump(msg: Msg, indent: int = 0, max_depth: int = 99) -> str:
    """Readable tree for debugging; LEN values shown as text if printable."""
    lines = []
    for num, wt, v in msg.fields:
        pad = "  " * indent
        if isinstance(v, Msg):
            lines.append(f"{pad}{num}: {{")
            if indent < max_depth:
                lines.append(dump(v, indent + 1, max_depth))
            lines.append(f"{pad}}}")
        elif wt == LEN:
            try:
                s = v.decode()
                shown = repr(s) if s.isprintable() else f"<{len(v)} bytes>"
            except UnicodeDecodeError:
                shown = f"<{len(v)} bytes>"
            lines.append(f"{pad}{num}: {shown}")
        elif wt == I32:
            lines.append(f"{pad}{num}: {struct.unpack('<f', v)[0]!r}f")
        elif wt == I64:
            lines.append(f"{pad}{num}: {struct.unpack('<d', v)[0]!r}d")
        else:
            lines.append(f"{pad}{num}: {v}")
    return "\n".join(lines)


def iter_delimited(data: bytes):
    pos = 0
    while pos < len(data):
        n, pos = _read_varint(data, pos)
        yield data[pos:pos + n]
        pos += n


def write_delimited(msgs) -> bytes:
    out = bytearray()
    for m in msgs:
        b = m.encode() if isinstance(m, Msg) else m
        out += _varint(len(b)) + b
    return bytes(out)


# --- bv41 blobs ------------------------------------------------------------

BV_MAGIC, BV_END = b"bv41", b"bv4$"


BV_BLOCK = 32768  # GoodNotes splits bodies into blocks of at most this many raw bytes


def blob_decompress(blob: bytes) -> bytes:
    """bv41 container: one or more [b"bv41", raw size, payload size, LZ4
    block] chunks, then b"bv4$". Bodies over 32 KiB span several chunks."""
    out, pos = bytearray(), 0
    while blob[pos:pos + 4] == BV_MAGIC:
        raw, size = struct.unpack_from("<II", blob, pos + 4)
        out += decompress_block(blob[pos + 12:pos + 12 + size], expected_size=raw)
        pos += 12 + size
    if blob[pos:pos + 4] != BV_END or pos == 0:
        raise ValueError("malformed bv41 blob")
    return bytes(out)


def _lz4_literals(raw: bytes) -> bytes:
    """A valid LZ4 block that stores `raw` as one literal run."""
    n = len(raw)
    if n < 15:
        return bytes([n << 4]) + raw
    rest = n - 15
    return b"\xf0" + b"\xff" * (rest // 255) + bytes([rest % 255]) + raw


def blob_compress(body: bytes) -> bytes:
    out = bytearray()
    for i in range(0, max(len(body), 1), BV_BLOCK):
        chunk = body[i:i + BV_BLOCK]
        payload = _lz4_literals(chunk)
        out += BV_MAGIC + struct.pack("<II", len(chunk), len(payload)) + payload
    return bytes(out) + BV_END


# --- schema-driven templates ("tpl" bodies) ----------------------------------

def _parse_schema(s: str, i: int = 0):
    """'vuA(v)S(uu)' -> ['v', 'u', ('A', [...]), ('S', [...])]."""
    out = []
    while i < len(s) and s[i] != ")":
        c = s[i]
        if c in "AS":
            inner, i = _parse_schema(s, i + 2)  # skip 'A('
            out.append((c, inner))
            i += 1  # ')'
        else:
            out.append(c)
            i += 1
    return out, i


_SIZE = {"v": ("<H", 2), "u": ("<I", 4), "f": ("<f", 4)}


def _read_values(spec, buf, pos):
    vals = []
    for t in spec:
        if isinstance(t, tuple):
            kind, inner = t
            if kind == "S":
                v, pos = _read_values(inner, buf, pos)
                vals.append(v)
            else:
                (n,) = struct.unpack_from("<I", buf, pos)
                pos += 4
                arr = []
                for _ in range(n):
                    v, pos = _read_values(inner, buf, pos)
                    arr.append(v[0] if len(inner) == 1 else v)
                vals.append(arr)
        else:
            fmt, size = _SIZE[t]
            vals.append(struct.unpack_from(fmt, buf, pos)[0])
            pos += size
    return vals, pos


def _write_values(spec, vals, out):
    for t, v in zip(spec, vals):
        if isinstance(t, tuple):
            kind, inner = t
            if kind == "S":
                _write_values(inner, v, out)
            else:
                out += struct.pack("<I", len(v))
                for e in v:
                    _write_values(inner, [e] if len(inner) == 1 else e, out)
        else:
            out += struct.pack(_SIZE[t][0], v)


class Template:
    """A decoded 'tpl' body: schema string + nested value lists.

    `u` values stay uint32 bit patterns (lossless); use f32()/u32() to view
    or store them as floats. `extra` keeps any trailing bytes the schema
    doesn't account for (seen on ~0.04% of real strokes).
    """

    def __init__(self, schema: str, values: list, extra: bytes = b""):
        self.schema, self.values, self.extra = schema, values, extra

    @classmethod
    def parse(cls, body: bytes) -> "Template":
        if body[:4] != b"tpl\0":
            raise ValueError("not a template body")
        end = body.index(b"\0", 8)
        schema = body[8:end].decode("ascii")
        spec, _ = _parse_schema(schema)
        values, pos = _read_values(spec, body, end + 1)
        return cls(schema, values, body[pos:])

    def encode(self) -> bytes:
        spec, _ = _parse_schema(self.schema)
        vals = bytearray()
        _write_values(spec, self.values, vals)
        schema = self.schema.encode() + b"\0"
        size = 8 + len(schema) + len(vals) + len(self.extra)
        return b"tpl\0" + struct.pack("<I", size) + schema + bytes(vals) + self.extra


def f32(bits: int) -> float:
    return struct.unpack("<f", struct.pack("<I", bits))[0]


def u32(x: float) -> int:
    return struct.unpack("<I", struct.pack("<f", x))[0]


# --- small helpers ---------------------------------------------------------

def new_uuid() -> str:
    return str(uuidlib.uuid4()).upper()


def stamp(counter: int = 1) -> Msg:
    """{1: edit counter, 2: random tag} — the CRDT version GoodNotes writes
    beside most values."""
    m = Msg()
    if counter:
        m.set_int(1, counter)
    m.set_int(2, random.getrandbits(32))
    return m


def read_color(m: Msg | None, default=(0.0, 0.0, 0.0, 1.0)) -> tuple:
    """RGBA from {1 r, 2 g, 3 b, 4 a}. Proto3 drops zero fields, so a
    missing channel is 0 — including alpha, though GoodNotes always writes
    alpha when it is nonzero."""
    if m is None:
        return default
    return tuple(m.float(i) for i in (1, 2, 3, 4))


def write_color(rgba) -> Msg:
    m = Msg()
    for i, c in enumerate(rgba if len(rgba) == 4 else (*rgba, 1.0), 1):
        if c:
            m.set_float(i, c)
    return m


def read_point(m: Msg | None) -> tuple[float, float]:
    return (m.float(1), m.float(2)) if m is not None else (0.0, 0.0)


def write_point(x: float, y: float) -> Msg:
    m = Msg()
    if x:
        m.set_float(1, x)
    if y:
        m.set_float(2, y)
    return m


SCHEMA_VERSION = 24  # stamped on every record GoodNotes writes (field 16/18/21/...)
DJB2_EMPTY = 5381


# --- page items ------------------------------------------------------------

class Item:
    """One element on a page: a metadata record plus a payload record.

    `kind` names the payload by its top-level field: 'stroke' (7), 'box'
    (21: shapes and text boxes), 'sticky' (20), 'line' (22), 'image' (1:
    images, stickers and element sticky notes), 'math' (11). Unknown kinds
    keep their field number and still round-trip.
    """
    KINDS = {7: "stroke", 21: "box", 20: "sticky", 22: "line", 1: "image", 11: "math"}

    def __init__(self, meta: Msg | None, payload: Msg | None):
        self.meta, self.payload = meta, payload

    @property
    def field(self) -> int | None:
        if self.payload is None or not self.payload.fields:
            return None
        return self.payload.fields[0][0]

    @property
    def kind(self) -> str:
        return self.KINDS.get(self.field, f"field{self.field}")

    @property
    def body(self) -> Msg:
        return self.payload.sub(self.field)

    @property
    def id(self) -> str | None:
        return self.meta.str(1) if self.meta is not None else self.body.str(1)

    @property
    def deleted(self) -> bool:
        """Tombstoned (erased/deleted) items stay in the file with this flag."""
        return self.meta is not None and self.meta.int(3) == 1

    @property
    def sequence(self) -> int:
        return self.meta.int(9) if self.meta is not None else 0

    def typed(self):
        cls = _VIEWS.get(self.kind)
        return cls(self) if cls else self

    def __repr__(self) -> str:
        return f"<{self.kind} {self.id}{' deleted' if self.deleted else ''}>"


class View:
    """Base for typed views; `item.body` is the payload submessage."""

    def __init__(self, item: Item):
        self.item = item
        self.b = item.body

    @property
    def id(self):
        return self.item.id

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"


# --- strokes ---

PEN_KINDS = {0: "ballpoint", 1: "fountain", 4: "brush", 5: "pencil"}  # body field 3


class Stroke(View):
    """Ink. Geometry lives in a bv41 template blob (field 2) whose schema
    depends on the pen; `commands` decodes the common PStroke schema.
    A recognized shape (shape tool / hold-to-snap) adds a descriptor at
    field 9 and stores a placeholder path."""

    @property
    def color(self):
        return read_color(self.b.sub(4))

    @color.setter
    def color(self, rgba):
        self.b.set_bytes(4, write_color(rgba))

    @property
    def pen(self) -> str:
        """ballpoint (constant width, PStroke), fountain / brush (variable
        width with a precomputed outline), pencil (per-point width and
        opacity). Highlighter, tape and shape-tool strokes are flags on top."""
        return PEN_KINDS.get(self.b.int(3), f"pen{self.b.int(3)}")

    @property
    def tape(self) -> bool:
        return bool(self.b.bytes(20))

    @property
    def highlighter(self) -> bool:
        return self.b.int(5) == 1

    @property
    def template(self) -> Template:
        return Template.parse(blob_decompress(self.b.bytes(2)))

    @template.setter
    def template(self, t: Template):
        self.b.set_bytes(2, blob_compress(t.encode()))

    @property
    def shape(self) -> Msg | None:
        """Recognized-shape descriptor: 1 {1 point...} = polyline/line,
        3 {1 center, 2 size} = rectangle, 4 {1 center, 2 radii, 3 angle} =
        ellipse; 5 {2 closed?}; 15 = line width."""
        s = self.b.sub(9)
        return s if s is not None and s.fields else None

    # PStroke ('vuA(v)A(S(uu))A(S(uuuu))vA(f)') helpers
    @property
    def thickness(self) -> float:
        t = self.template
        return f32(t.values[1]) if t.schema.startswith("vu") else 0.0

    @property
    def commands(self) -> list[tuple]:
        """[('M', (x, y)) | ('Q', (cx, cy), (x, y))] for PStroke blobs."""
        t = self.template
        if t.schema != PSTROKE:
            raise ValueError(f"no path decoder for schema {t.schema}")
        kinds, moves, quads = t.values[2], t.values[3], t.values[4]
        out, mi, qi = [], 0, 0
        for k in kinds:
            if k == 0:
                out.append(("M", tuple(map(f32, moves[mi]))))
                mi += 1
            else:
                q = list(map(f32, quads[qi]))
                out.append(("Q", (q[0], q[1]), (q[2], q[3])))
                qi += 1
        return out

    def set_path(self, commands, thickness: float | None = None) -> None:
        """Replace geometry with a PStroke path ('M'/'Q'/'L' commands)."""
        th = self.thickness if thickness is None else thickness
        kinds, moves, quads = [], [], []
        cur = (0.0, 0.0)
        for c in commands:
            if c[0] == "M":
                kinds.append(0)
                moves.append([u32(c[1][0]), u32(c[1][1])])
                cur = c[1]
            else:
                if c[0] == "L":
                    ctrl, end = ((cur[0] + c[1][0]) / 2, (cur[1] + c[1][1]) / 2), c[1]
                else:
                    ctrl, end = c[1], c[2]
                kinds.append(1)
                quads.append([u32(ctrl[0]), u32(ctrl[1]), u32(end[0]), u32(end[1])])
                cur = end
        self.template = Template(PSTROKE, [2, u32(th), kinds, moves, quads, 1, []])
        self.b.remove(3)  # pen kinds other than 0 use other schemas

    def translate(self, dx: float, dy: float) -> None:
        if self.template.schema == PSTROKE:
            self.set_path([(c[0], *[(x + dx, y + dy) for x, y in c[1:]]) for c in self.commands])
        s = self.shape
        if s is not None:
            _translate_shape(s, dx, dy)


PSTROKE = "vuA(v)A(S(uu))A(S(uuuu))vA(f)"


def _translate_shape(s: Msg, dx, dy):
    poly = s.sub(1)
    for p in (poly.subs(1) if poly is not None else []):  # polyline points
        p.set_float(1, p.float(1) + dx)
        p.set_float(2, p.float(2) + dy)
    for n in (3, 4):  # rect origin / ellipse center
        r = s.sub(n)
        if r is not None:
            p = r.sub(1, create=True)
            p.set_float(1, p.float(1) + dx)
            p.set_float(2, p.float(2) + dy)


# --- boxes: shapes and text boxes (21) ---

class RichText:
    """Attributed text: runs of {1 text, 2 char attrs, 3 paragraph attrs}.

    Char attrs: 3 color, 30 font name, 40 size (-404 = inherit), 50 italic,
    60 weight (signed; -404 inherit, -30 bold), 70 ?.
    Paragraph attrs: 2 alignment/spacing, 4 list style (2 = bullet).
    """

    def __init__(self, msg: Msg):
        self.msg = msg

    @property
    def runs(self) -> list[Msg]:
        return self.msg.subs(1)

    @property
    def text(self) -> str:
        return "".join(r.str(1, "") for r in self.runs)

    def set_text(self, s: str) -> None:
        """Replace all text, keeping the first run's formatting."""
        runs = self.runs
        first = runs[0].copy() if runs else Msg()
        first.set_bytes(1, s)
        self.msg.remove(1)
        self.msg.fields.insert(0, [1, LEN, first])

    def set_color(self, rgba) -> None:
        for r in self.runs:
            r.sub(2, create=True).set_bytes(3, write_color(rgba))

    def set_font(self, name: str | None = None, size: float | None = None) -> None:
        for r in self.runs:
            a = r.sub(2, create=True)
            if name is not None:
                a.set_bytes(30, name)
            if size is not None:
                a.set_float(40, size)


# a shape's empty text, exactly as GoodNotes writes it, with its hash
_EMPTY_TEXT = (b"\n]\x12.\x1a\x14\r\xf1\xf0\xf0=\x15\xd9\xd8\xd8=\x1d\xd9\xd8\xd8=%\x00\x00\x80?\xc5\x02"
               b"\x00\x00\x80A\xe0\x03\xec\xfc\xff\xff\xff\xff\xff\xff\xff\x01\xb5\x04\x00\x00\xca\xc3\x1a+"
               b"\n\x02\n\x00\x12\x0b\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01\x1a\x16\x08\xff\xff\xff"
               b"\xff\xff\xff\xff\xff\xff\x01\x10\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01 \x02")
_EMPTY_TEXT_HASH = bytes.fromhex("afd2f3d38e516ada")


class _Rect(View):
    """Shared origin (20.1) and size (21.2) handling for box-like items."""

    @property
    def origin(self):
        return read_point(self.b.path(20, 1))

    @origin.setter
    def origin(self, xy):
        self.b.sub(20, create=True).set_bytes(1, write_point(*xy))

    @property
    def size(self):
        s = self.b.sub(21)
        dims = s.sub(2) if s is not None else None
        return (dims.float(1), dims.float(2)) if dims is not None else (0.0, 0.0)

    @size.setter
    def size(self, wh):
        d = self.b.sub(21, create=True).sub(2, create=True)
        d.set_float(1, wh[0])
        d.set_float(2, wh[1])

    def translate(self, dx, dy):
        x, y = self.origin
        self.origin = (x + dx, y + dy)

    def _text_msg(self, num: int) -> RichText | None:
        t = self.b.sub(num)
        blob = t.path(1).bytes(2) if t is not None and t.sub(1) is not None else None
        return RichText(Msg.parse(blob_decompress(blob))) if blob else None

    def _set_text_msg(self, num: int, rt: RichText) -> None:
        holder = self.b.sub(num, create=True).sub(1, create=True)
        body = rt.msg.encode()
        holder.set_bytes(2, blob_compress(body))
        # 8-byte content hash: 1:1 with the text body across the library,
        # algorithm not identified; GoodNotes doesn't reject other values
        holder.set_bytes(3, _EMPTY_TEXT_HASH if body == _EMPTY_TEXT else random.getrandbits(64).to_bytes(8, "little"))
        holder.set_int(4, 1)


class Box(_Rect):
    """Shapes and text boxes share one record (21).

    20 {1 origin, 3 scale}; 21 {2 {w, h, max}}; 22 geometry ({1 {1 corner
    radius}} = rectangle, {2} = ellipse, {3 {1 polygon vertices in unit
    square}} = other shapes); 30 fill {1 {1 color}}; 31 outline {1 width,
    3 {1 color}}; 32 text {1 {2 rich-text blob}, 2 text size, 5 default
    attrs, 10 insets}; 33 shadow; 8 group id (ties a sticker's editable
    text to its image)."""

    @property
    def fill(self):
        f = self.b.path(30, 1, 1)
        return read_color(f, None) if f is not None else None

    @fill.setter
    def fill(self, rgba):
        self.b.remove(30)
        if rgba is not None:
            self.b.sub(30, create=True).sub(1, create=True).set_bytes(1, write_color(rgba))

    @property
    def outline(self):
        o = self.b.sub(31)
        if o is None:
            return None
        return o.float(1), read_color(o.path(3, 1), None)

    @outline.setter
    def outline(self, width_rgba):
        o = self.b.sub(31, create=True)
        width, rgba = width_rgba
        o.set_float(1, width)
        o.sub(3, create=True).set_bytes(1, write_color(rgba))

    @property
    def text(self) -> RichText | None:
        return self._text_msg(32)

    @text.setter
    def text(self, rt: RichText):
        self._set_text_msg(32, rt)
        self.b.sub(32).remove(2)  # measured text size; GoodNotes re-measures

    @property
    def vertices(self) -> list[tuple[float, float]] | None:
        poly = self.b.path(22, 3, 1)
        return [read_point(v.sub(1)) for v in poly.subs(1)] if poly is not None else None


class Sticky(_Rect):
    """Sticky-note tool (20): 30 paper color, 31 text (as Box 32),
    32 author id, 33 author name."""

    @property
    def color(self):
        return read_color(self.b.sub(30))

    @color.setter
    def color(self, rgba):
        self.b.set_bytes(30, write_color(rgba))

    @property
    def text(self) -> RichText | None:
        return self._text_msg(31)

    @text.setter
    def text(self, rt: RichText):
        self._set_text_msg(31, rt)


class Line(View):
    """Lines and connectors (22). 20 = straight or curved line: {1 {1
    start}, 2 mid handle (on the curve), 3 {1 end}}; 21 = elbow connector:
    {1 {1 start}, 3 {1 end}, 5 {2 knee}}. 31 arrowheads (1 = end arrow),
    32 stroke {1 width, 3 {1 color}}."""

    def _point_msgs(self) -> list[Msg]:
        g = self.b.sub(20) or self.b.sub(21)
        if g is None:
            return []
        out = []
        for f in g.fields:
            if f[1] != LEN:
                continue
            p = g._parsed(f)
            inner = p.sub(1) or p.sub(2)
            out.append(inner if inner is not None and inner.fields else p)
        return out

    @property
    def points(self) -> list[tuple[float, float]]:
        """start, [corner/control], end — in stored order."""
        return [read_point(p) for p in self._point_msgs()]

    @property
    def elbow(self) -> bool:
        return self.b.sub(21) is not None

    @property
    def color(self):
        return read_color(self.b.path(32, 3, 1))

    @color.setter
    def color(self, rgba):
        self.b.sub(32, create=True).sub(3, create=True).set_bytes(1, write_color(rgba))

    @property
    def width(self) -> float:
        s = self.b.sub(32)
        return s.float(1) if s is not None else 0.0

    @property
    def arrow(self) -> int:
        return self.b.int(31)

    def translate(self, dx, dy):
        for p in self._point_msgs():
            p.set_float(1, p.float(1) + dx)
            p.set_float(2, p.float(2) + dy)


class Image(View):
    """Placed image, sticker or element (1). 2 {origin, size}; 3 {center,
    size, 3 angle in radians}; 4 attachment id; 6 kind (1 image,
    3 element/sticker); 7 element group id."""

    @property
    def attachment(self) -> str:
        return self.b.str(4)

    @property
    def center(self):
        return read_point(self.b.path(3, 1))

    @property
    def size(self):
        return read_point(self.b.path(3, 2))

    @property
    def angle(self) -> float:
        r = self.b.sub(3)
        return r.float(3) if r is not None else 0.0

    def place(self, center, size, angle: float = 0.0) -> None:
        cx, cy = center
        w, h = size
        # field 2 is the rotated image's axis-aligned bounding box
        bw = abs(w * math.cos(angle)) + abs(h * math.sin(angle))
        bh = abs(w * math.sin(angle)) + abs(h * math.cos(angle))
        r2 = self.b.sub(2, create=True)
        r2.fields = []
        r2.set_bytes(1, write_point(cx - bw / 2, cy - bh / 2))
        r2.set_bytes(2, write_point(bw, bh))
        r3 = self.b.sub(3, create=True)
        r3.fields = []
        r3.set_bytes(1, write_point(cx, cy))
        r3.set_bytes(2, write_point(w, h))
        if angle:
            r3.set_float(3, angle)

    def translate(self, dx, dy):
        (cx, cy), size = self.center, self.size
        self.place((cx + dx, cy + dy), size, self.angle)


class Math(View):
    """Handwriting converted to math (11): 2/3 rects as Image, 4
    attachment (rendered image), 9 LaTeX, 11 the original strokes
    {1 blob, 2 color}."""

    @property
    def latex(self) -> str | None:
        return self.b.str(9)


_VIEWS = {"stroke": Stroke, "box": Box, "sticky": Sticky, "line": Line,
          "image": Image, "math": Math}


# --- pages and documents -----------------------------------------------------

def _is_meta(m: Msg) -> bool:
    return m.has(8) and m.has(9) and isinstance(m.get(1), (bytes, bytearray)) and len(m.get(1)) == 36


_UUID_RE = re.compile(rb"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")


def uuid_plus(u: str, n: int = 1) -> str:
    """A page's ink lives in notes/<page id + 1> (true for 192 of 206
    library files; the rest are found through index.notes.pb)."""
    return str(uuidlib.UUID(int=(uuidlib.UUID(u).int + n) % (1 << 128))).upper()


# Page keys are fractional indexes compared as plain ASCII; this alphabet
# covers every character seen in real keys.
_KEY_ALPHABET = "!-0123456789ABCDEFGHIJKLMNOPQRSTUVWXYZ_abcdefghijklmnopqrstuvwxyz~"


def key_between(a: str | None, b: str | None) -> str:
    """A key that sorts strictly between a and b (None = open end)."""
    lo, hi = _KEY_ALPHABET[0], _KEY_ALPHABET[-1]
    a = a or ""
    out = ""
    i = 0
    while True:
        ca = a[i] if i < len(a) else lo
        cb = b[i] if b is not None and i < len(b) else None
        ia = _KEY_ALPHABET.index(ca) if ca in _KEY_ALPHABET else 0
        ib = _KEY_ALPHABET.index(cb) if cb is not None and cb in _KEY_ALPHABET else len(_KEY_ALPHABET)
        if ib - ia > 1:
            return out + _KEY_ALPHABET[(ia + ib) // 2]
        out += ca if i < len(a) else lo
        if cb is not None and ib - ia == 1:
            b = None  # anything after this prefix is below b now
        i += 1


def page_link(doc: "Document", page: "Page") -> str:
    """URL GoodNotes uses for a link to a page of this document."""
    import base64
    import urllib.parse
    anchor = base64.b64encode(f"page-{page.page_id}".encode()).decode()
    frag = base64.b64encode(doc.doc_id.encode()).decode().rstrip("=")
    return f"https://app.goodnotes.com/documents/?anchor={urllib.parse.quote(anchor)}#{frag}"


class Page:
    """One page: its ink/element records plus, when the event log has
    them, its page event (54) and paper layer event (2)."""

    def __init__(self, uuid: str, records: list[Msg], event: Msg | None = None,
                 layer: Msg | None = None):
        self.uuid, self.records = uuid, records  # uuid = notes id
        self.event, self.layer = event, layer

    @property
    def page_id(self) -> str | None:
        return self.event.str(2) if self.event is not None else None

    @property
    def key(self) -> str:
        k = self.event.path(4) if self.event is not None else None
        return k.str(1, "") if k is not None else ""

    @property
    def size(self) -> tuple[float, float]:
        """Page size in page units (standard paper is 834.24 x 1078.82)."""
        sz = self.layer.sub(8) if self.layer is not None else None
        return read_point(sz) if sz is not None else (834.24, 1078.825)

    @property
    def background(self) -> tuple[str | None, int]:
        """(attachment id of the background PDF/image, 1-based PDF page)."""
        if self.layer is None:
            return None, 1
        return self.layer.str(4), self.layer.int(5, 1) or 1

    @property
    def template(self) -> str | None:
        return self.layer.str(9) if self.layer is not None else None

    @property
    def items(self) -> list[Item]:
        """Elements in document (= z) order, deleted ones included."""
        metas = {r.str(1): r for r in self.records if _is_meta(r)}
        out = []
        for r in self.records:
            if _is_meta(r):
                continue
            it = Item(None, r)
            body = it.body
            it.meta = metas.get(body.str(1)) if body is not None else None
            out.append(it)
        return out

    def visible(self) -> list:
        """Typed views of the items GoodNotes draws."""
        return [i.typed() for i in self.items if not i.deleted]

    def remove(self, item: Item) -> None:
        self.records = [r for r in self.records if r is not item.meta and r is not item.payload]

    def group(self, item: Item) -> list[Item]:
        """`item` plus everything sharing its element group (a sticker's
        image and its editable text: image body field 7 == box field 8)."""
        gid = item.body.str(7) if item.kind == "image" else item.body.str(8) if item.kind == "box" else None
        if not gid or not _UUID_RE.match(gid.encode()):
            return [item]
        return [i for i in self.items if not i.deleted and
                ((i.kind == "image" and i.body.str(7) == gid) or (i.kind == "box" and i.body.str(8) == gid))]

    def move(self, item: Item, dx: float, dy: float) -> None:
        """Moves an item (and its element group) by (dx, dy)."""
        for i in self.group(item):
            i.typed().translate(dx, dy)

    def append(self, item: Item) -> None:
        self.records += [r for r in (item.meta, item.payload) if r is not None]

    def __repr__(self) -> str:
        return f"<Page {self.key!r} {self.uuid} {len(self.records)} records>"


def _event_body(e: Msg) -> Msg | None:
    """An event is {1: target id, N: body}; returns body (N varies by type)."""
    for f in e.fields:
        if f[0] != 1 and f[1] == LEN:
            return e._parsed(f)
    return None


def _replace_ids(m: Msg, mapping: dict[bytes, bytes]) -> None:
    """Rewrites every UUID-valued field found in `mapping`, recursively;
    event ids (field 11) get fresh UUIDs so copies don't collide."""
    for f in m.fields:
        if f[1] != LEN:
            continue
        v = f[2]
        if isinstance(v, Msg):
            _replace_ids(v, mapping)
        elif _UUID_RE.match(v):
            if v.upper() in mapping:
                f[2] = mapping[v.upper()]
            elif f[0] == 11:
                f[2] = new_uuid().encode()
        elif v and not v.isascii() or (v and v[:1] in (b"\n", b"\x12", b"\x1a", b"\"")):
            try:
                sub = Msg.parse(v)
            except (ValueError, IndexError):
                continue
            if sub.fields:
                before = sub.encode()
                _replace_ids(sub, mapping)
                if sub.encode() != before:
                    f[2] = sub


class Document:
    """A whole .goodnotes file. Untouched zip entries are written back
    byte-for-byte; pages and the event log are re-encoded from their Msg
    trees (identical bytes unless edited).

    `pages` is in **document order**: sorted by each page event's key.
    index.notes.pb is creation order, not page order (they differ in 53 of
    206 library files; confirmed against the GoodNotes app)."""

    def __init__(self, path: str):
        with zipfile.ZipFile(path) as z:
            self.infos = z.infolist()
            self.files = {i.filename: z.read(i) for i in self.infos}
        self.events = [Msg.parse(m) for m in iter_delimited(self.files.get("index.events.pb", b""))]
        index = [Msg.parse(m) for m in iter_delimited(self.files.get("index.notes.pb", b""))]
        layers, page_events = {}, {}
        for e in self.events:
            if e.sub(2) is not None and e.path(2).has(8):
                layers[e.path(2).str(2)] = e.sub(2)
            pe = e.sub(54)
            if pe is not None:
                page_events[uuid_plus(pe.str(2))] = pe
        self.pages = []
        for rec in index:
            uid = rec.str(1)
            data = self.files.get(f"notes/{uid}", b"")
            ev = page_events.get(uid)
            layer = layers.get(ev.path(3).str(1)) if ev is not None and ev.sub(3) is not None else None
            self.pages.append(Page(uid, [Msg.parse(m) for m in iter_delimited(data)], ev, layer))
        if all(p.event is not None for p in self.pages):
            self.pages.sort(key=lambda p: p.key)

    # attachments
    @property
    def attachments(self) -> dict[str, bytes]:
        return {n.split("/", 1)[1]: d for n, d in self.files.items() if n.startswith("attachments/")}

    @property
    def doc_id(self) -> str | None:
        for e in self.events:
            if e.has(30):
                return e.str(1)
        return None

    @property
    def device(self) -> int:
        for p in self.pages:
            for r in p.records:
                if _is_meta(r):
                    return r.int(8)
        if getattr(self, "_device", None) is None:
            self._device = random.getrandbits(63)  # no ink yet: any 63-bit id
        return self._device

    def next_sequence(self) -> int:
        """Creation counter for new items (meta field 9), continuing the
        highest one already in the document."""
        if getattr(self, "_seq", None) is None:
            self._seq = max((r.int(9) for p in self.pages for r in p.records if _is_meta(r)), default=0)
        self._seq += 1
        return self._seq

    @property
    def title(self) -> str | None:
        for e in reversed(self.events):
            d = e.sub(30) or e.sub(31)
            if d is not None and d.sub(2) is not None:
                return d.path(2).str(1)
        return None

    @title.setter
    def title(self, name: str) -> None:
        for e in self.events:
            d = e.sub(30)
            if d is not None:
                d.sub(2, create=True).set_bytes(1, name)

    def add_attachment(self, data: bytes, attachment_id: str | None = None) -> str:
        """Stores raw bytes and registers them the three ways GoodNotes
        requires (zip entry, index.attachments.pb, attachment event)."""
        aid = attachment_id or new_uuid()
        self.files[f"attachments/{aid}"] = data
        idx = Msg()
        idx.set_bytes(1, aid)
        idx.set_bytes(2, f"attachments/{aid}")
        self.files["index.attachments.pb"] = self.files.get("index.attachments.pb", b"") + write_delimited([idx])
        now = time.time() * 1000
        body = Msg()
        body.set_bytes(1, aid)
        body.set_bytes(2, aid)
        body.set_int(5, len(data))
        body.set_bytes(6, self.doc_id or new_uuid())
        body.set_double(10, now)
        body.set_bytes(11, new_uuid())
        body.set_bytes(12, b"")
        body.set_int(14, self.device)
        body.set_int(15, int(now))
        body.set_int(16, SCHEMA_VERSION)
        ev = Msg()
        ev.set_bytes(1, aid)
        ev.set_bytes(6, body)
        self.events.append(ev)
        return aid

    # page structure
    def _page_event_ids(self, page: Page) -> set[bytes]:
        ids = {page.uuid.encode()}
        if page.page_id:
            ids.add(page.page_id.encode())
        if page.layer is not None:
            ids.add(page.layer.str(2).encode())
        return ids

    def _events_for(self, page: Page) -> list[Msg]:
        ids = self._page_event_ids(page)
        out = []
        for e in self.events:
            body = _event_body(e)
            if (e.bytes(1) or b"").upper() in ids or (body is not None and (body.bytes(2) or b"").upper() in ids):
                out.append(e)
        return out

    def add_page(self, like: Page | None = None, index: int | None = None) -> Page:
        """New empty page with the same paper as `like` (default: last page),
        inserted at `index` (default: end)."""
        like = like or self.pages[-1]
        new_pid = new_uuid()
        mapping = {like.uuid.encode(): uuid_plus(new_pid).encode()}
        if like.page_id:
            mapping[like.page_id.encode()] = new_pid.encode()
        if like.layer is not None:
            mapping[like.layer.str(2).encode()] = new_uuid().encode()
        copies = []
        structural = (2, 10, 54, 102, 104)  # layer, page added, page, notes created/linked
        for e in self._events_for(like):
            if not any(e.has(k) for k in structural):
                continue  # bookmarks, outline entries, rotation stay with `like`
            c = e.copy()
            _replace_ids(c, mapping)
            copies.append(c)
        self.events += copies
        notes_id = uuid_plus(new_pid)
        page_ev = next(_event_body(c) for c in copies if c.has(54))
        layer_ev = next((_event_body(c) for c in copies if c.sub(2) is not None and c.path(2).has(8)), None)
        page = Page(notes_id, [], page_ev, layer_ev)
        idx = Msg()
        idx.set_bytes(1, notes_id)
        idx.set_bytes(2, f"notes/{notes_id}")
        self.files["index.notes.pb"] = self.files.get("index.notes.pb", b"") + write_delimited([idx])
        self.files[f"notes/{notes_id}"] = b""
        self.pages.append(page)
        self.move_page(page, len(self.pages) - 1 if index is None else index)
        return page

    def move_page(self, page: Page, index: int) -> None:
        others = [p for p in self.pages if p is not page]
        index = max(0, min(index, len(others)))
        before = others[index - 1].key if index > 0 else None
        after = others[index].key if index < len(others) else None
        page.event.path(4, create=True).set_bytes(1, key_between(before, after))
        others.insert(index, page)
        self.pages = others

    def delete_page(self, page: Page) -> None:
        drop = set(map(id, self._events_for(page)))
        self.events = [e for e in self.events if id(e) not in drop]
        keep = [m for m in iter_delimited(self.files.get("index.notes.pb", b""))
                if Msg.parse(m).str(1) != page.uuid]
        self.files["index.notes.pb"] = write_delimited(keep)
        self.files.pop(f"notes/{page.uuid}", None)
        self.pages.remove(page)

    def set_background(self, page: Page, data: bytes, pdf_page: int = 1,
                       size: tuple[float, float] | None = None) -> str:
        """Replaces a page's paper with a PDF (or image) attachment.
        `size` resizes the page; default keeps it."""
        aid = self.add_attachment(data)
        layer = page.layer
        layer.set_bytes(4, aid)
        layer.set_int(5, pdf_page)
        layer.remove(9)  # template name: no longer a built-in paper
        if size is not None:
            layer.set_bytes(8, write_point(*size))
        return aid

    # page attributes and outline (each is its own event; the last one wins)
    def _event(self, kind: int, target: str, body: Msg, version_field: int = 15) -> Msg:
        now = time.time() * 1000
        body.set_double(10, now)
        body.set_bytes(11, new_uuid())
        body.set_int(13, self.device)
        body.set_int(14, int(now) + random.getrandbits(8))
        body.set_int(version_field, SCHEMA_VERSION)
        ev = Msg()
        ev.set_bytes(1, target)
        ev.set_bytes(kind, body)
        self.events.append(ev)
        return ev

    def _page_attr(self, kind: int, page: Page) -> Msg | None:
        found = None
        for e in self.events:
            b = e.sub(kind) if e.has(kind) else None
            if b is not None and b.str(2) == page.page_id:
                found = b
        return found

    def bookmarked(self, page: Page) -> bool:
        b = self._page_attr(57, page)
        return b is not None and b.path(3).int(1) == 1

    def set_bookmarked(self, page: Page, on: bool = True) -> None:
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, page.page_id)
        v = Msg()
        if on:
            v.set_int(1, 1)
        v.set_bytes(2, stamp())
        body.set_bytes(3, v)
        self._event(57, page.page_id, body)

    def rotation(self, page: Page) -> int:
        """Page rotation in degrees clockwise (stored in quarter turns)."""
        b = self._page_attr(63, page)
        return 90 * (b.path(3).int(1) if b is not None else 0)

    def set_rotation(self, page: Page, degrees: int) -> None:
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, page.page_id)
        v = Msg()
        if degrees % 360:
            v.set_int(1, (degrees % 360) // 90)
        v.set_bytes(2, stamp())
        body.set_bytes(3, v)
        self._event(63, page.page_id, body, version_field=16)

    @property
    def outline(self) -> list[tuple[str, Page | None]]:
        """(title, page) entries in outline order."""
        entries = {}
        for e in self.events:
            b = e.sub(65) if e.has(65) else None
            if b is not None:
                entries[b.str(2)] = b
        by_id = {p.page_id: p for p in self.pages}
        rows = sorted(entries.values(), key=lambda b: b.path(4).str(1, "") if b.sub(4) else "")
        return [(b.path(5).str(1, "") if b.sub(5) else "", by_id.get(b.str(1))) for b in rows]

    def add_outline(self, title: str, page: Page) -> None:
        last = None
        for e in self.events:
            b = e.sub(65) if e.has(65) else None
            if b is not None and b.sub(4) is not None:
                k = b.path(4).str(1, "")
                last = k if last is None or k > last else last
        body = Msg()
        body.set_bytes(1, page.page_id)
        body.set_bytes(2, new_uuid())
        body.set_bytes(3, Msg([[2, LEN, stamp(0)]]))
        body.set_bytes(4, Msg([[1, LEN, key_between(last, None).encode()], [2, LEN, stamp(0)]]))
        body.set_bytes(5, Msg([[1, LEN, title.encode()], [2, LEN, stamp(0)]]))
        body.set_bytes(6, Msg([[2, LEN, stamp(0)]]))
        body.set_bytes(15, Msg([[1, LEN, b""], [2, LEN, stamp(0)]]))
        body.set_bytes(17, self.doc_id)
        ev = self._event(65, page.page_id, body, version_field=16)
        ev.sub(65).set_int(16, 26)  # outline records carry 26, not 24

    def save(self, path: str) -> None:
        files = dict(self.files)
        files["index.events.pb"] = write_delimited(self.events)
        for p in self.pages:
            files[f"notes/{p.uuid}"] = write_delimited(p.records)
        with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
            written = set()
            for info in self.infos:
                if info.filename in files:
                    z.writestr(info, files[info.filename])
                    written.add(info.filename)
            for name, data in files.items():
                if name not in written:
                    z.writestr(name, data)


# --- building new items ------------------------------------------------------

def _meta(doc: Document, item_id: str, st: Msg, version: int = SCHEMA_VERSION,
          attachment: str | None = None) -> Msg:
    m = Msg()
    m.set_bytes(1, item_id)
    m.set_bytes(2, st)
    if attachment:
        m.set_bytes(4, attachment)
    m.set_int(8, doc.device)
    m.set_int(9, doc.next_sequence())
    m.set_int(14, DJB2_EMPTY)
    m.set_int(16, version)
    return m


def new_stroke(doc: Document, commands, color=(0, 0, 0, 1), thickness: float = 1.5,
               highlighter: bool = False) -> Item:
    """A plain pen stroke from 'M'/'L'/'Q' commands in page units."""
    sid, st = new_uuid(), stamp()
    b = Msg()
    b.set_bytes(1, sid)
    b.set_bytes(2, b"")
    b.set_bytes(4, write_color(color))
    if highlighter:
        b.set_int(5, 1)
    b.set_bytes(6, b"")
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    b.set_bytes(9, b"")
    b.set_bytes(15, st.copy())
    b.set_bytes(20, b"")
    b.set_int(21, SCHEMA_VERSION)
    payload = Msg([[7, LEN, b]])
    item = Item(_meta(doc, sid, st), payload)
    Stroke(item).set_path(commands, thickness)
    return item


VW_SCHEMA = "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)"
PENCIL_SCHEMA = "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)"


def _sample(commands, step: float = 3.0) -> list[tuple[float, float]]:
    """Flattens M/L/Q commands to points about `step` apart (one subpath)."""
    pts, cur = [], None
    for c in commands:
        if c[0] == "M":
            cur = c[1]
            pts.append(cur)
            continue
        ctrl, end = (((cur[0] + c[1][0]) / 2, (cur[1] + c[1][1]) / 2), c[1]) if c[0] == "L" else (c[1], c[2])
        n = max(1, int(math.dist(cur, end) / step))
        for i in range(1, n + 1):
            t = i / n
            pts.append(((1 - t) ** 2 * cur[0] + 2 * (1 - t) * t * ctrl[0] + t * t * end[0],
                        (1 - t) ** 2 * cur[1] + 2 * (1 - t) * t * ctrl[1] + t * t * end[1]))
        cur = end
    return pts


def _stroke_shell(doc: Document, color, pen: int = 0, highlighter: bool = False) -> tuple[Item, Msg]:
    sid, st = new_uuid(), stamp()
    b = Msg()
    b.set_bytes(1, sid)
    b.set_bytes(2, b"")
    if pen:
        b.set_int(3, pen)
    b.set_bytes(4, write_color(color))
    if highlighter:
        b.set_int(5, 1)
    b.set_bytes(6, b"")
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    b.set_bytes(9, b"")
    b.set_bytes(15, st.copy())
    b.set_bytes(20, b"")
    b.set_int(21, SCHEMA_VERSION)
    return Item(_meta(doc, sid, st), Msg([[7, LEN, b]])), b


def new_fountain_stroke(doc: Document, commands, color=(0, 0, 0, 1), width: float = 3.0,
                        pressure=None, tape: bool = False) -> Item:
    """KNOWN BROKEN: GoodNotes crashes on import of these. The outline
    must be one subpath per centerline segment (true of all 4,363 such
    strokes in a real library); this writes a single subpath.

    Variable-width ink (fountain pen; tape=True draws washi tape).
    `pressure` maps 0..1 along the stroke to a width factor (default:
    constant). GoodNotes draws these from a precomputed outline, which is
    generated here: offset sides joined by cubic segments, round caps."""
    pts = _sample(commands)
    if len(pts) < 2:
        pts = pts + [(pts[0][0] + 0.01, pts[0][1])]
    n = len(pts)
    half = [width / 2 * (pressure(i / (n - 1)) if pressure else 1.0) for i in range(n)]
    # centerline: moveTo (x, y, w) then quads (control, end) with widths
    center_kinds, center_moves, center_quads = [0], [u32(pts[0][0]), u32(pts[0][1]), u32(half[0])], []
    for i in range(1, n):
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        center_kinds.append(1)
        center_quads += [u32((x0 + x1) / 2), u32((y0 + y1) / 2), u32((half[i - 1] + half[i]) / 2),
                         u32(x1), u32(y1), u32(half[i])]
    # outline
    def normal(i):
        a, b = pts[max(i - 1, 0)], pts[min(i + 1, n - 1)]
        dx, dy = b[0] - a[0], b[1] - a[1]
        L = math.hypot(dx, dy) or 1.0
        return -dy / L, dx / L
    left = [(pts[i][0] + normal(i)[0] * half[i], pts[i][1] + normal(i)[1] * half[i]) for i in range(n)]
    right = [(pts[i][0] - normal(i)[0] * half[i], pts[i][1] - normal(i)[1] * half[i]) for i in range(n)]
    types, moves, cubics, arcs, cw = [0], [u32(left[0][0]), u32(left[0][1])], [], [], []

    def seg(a, b):
        types.append(2)
        cubics.extend(u32(v) for v in (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3,
                                       a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3, b[0], b[1]))

    def cap(i, frm):
        nx, ny = normal(i)
        start = math.atan2(frm[1] - pts[i][1], frm[0] - pts[i][0])
        types.append(3)
        arcs.extend(u32(v) for v in (pts[i][0], pts[i][1], half[i], start, start + math.pi))
        cw.append(0)

    for i in range(1, n):
        seg(left[i - 1], left[i])
    cap(n - 1, left[-1])
    for i in range(n - 1, 0, -1):
        seg(right[i], right[i - 1])
    cap(0, right[0])
    item, b = _stroke_shell(doc, color, pen=1)
    if tape:
        b.set_bytes(20, Msg([[1, LEN, b""]]))
    t = Template(VW_SCHEMA, [2, center_kinds, center_moves, center_quads, [len(types)], types,
                              moves, [], cubics, arcs, cw])
    b.set_bytes(2, blob_compress(t.encode()))
    return item


def new_pencil_stroke(doc: Document, commands, color=(0.2, 0.2, 0.2, 1), width: float = 2.0,
                      pressure=None) -> Item:
    """Textured pencil ink: per point (x, y, azimuth, altitude, force);
    `pressure` maps 0..1 along the stroke to force (default 0.6)."""
    pts = _sample(commands, 6.0)
    n = len(pts)
    force = [pressure(i / max(n - 1, 1)) if pressure else 0.6 for i in range(n)]

    def p5(i):
        return [u32(pts[i][0]), u32(pts[i][1]), u32(0.5), u32(1.0), u32(force[i])]
    kinds, moves, quads = [0], [p5(0)], []
    for i in range(1, n):
        kinds.append(1)
        mid = [u32((pts[i - 1][0] + pts[i][0]) / 2), u32((pts[i - 1][1] + pts[i][1]) / 2),
               u32(0.5), u32(1.0), u32((force[i - 1] + force[i]) / 2)]
        quads.append([random.getrandbits(32)] + mid + p5(i))  # first value: texture seed
    item, b = _stroke_shell(doc, color, pen=5)
    t = Template(PENCIL_SCHEMA, [1, u32(width / 2), kinds, moves, quads, [], [], [], [], []])
    b.set_bytes(2, blob_compress(t.encode()))
    return item


def new_shape_stroke(doc: Document, kind: str, color=(0, 0, 0, 1), width: float = 4.68,
                     points=None, center=None, size=None, angle: float = 0.0) -> Item:
    """Shape-tool ink: kind 'line'/'polyline' (points), 'rect' (center,
    size), 'ellipse' (center, radii=size, angle)."""
    item, b = _stroke_shell(doc, color)
    b.set_bytes(2, blob_compress(Template(PSTROKE, [2, u32(width / 2), [], [], [], 1, []]).encode()))
    d = Msg()
    if kind in ("line", "polyline"):
        d.set_bytes(1, Msg([[1, LEN, write_point(*p)] for p in points]))
    elif kind == "rect":
        d.set_bytes(3, Msg([[1, LEN, write_point(*center)], [2, LEN, write_point(*size)]]))
    elif kind == "ellipse":
        e = Msg([[1, LEN, write_point(*center)], [2, LEN, write_point(*size)]])
        if angle:
            e.set_float(3, angle)
        d.set_bytes(4, e)
    else:
        raise ValueError(kind)
    d.set_bytes(5, Msg([[2, VARINT, 1]]))
    d.set_float(15, width)
    b.set_bytes(9, d)
    return item


_RICH_ATTR_FALLBACK = -404  # "inherit" sentinel used in char attrs


def rich_text(text: str, color=(0.118, 0.106, 0.106, 1), font: str | None = None,
              size: float | None = None, bold: bool = False, italic: bool = False,
              link: str | None = None) -> RichText:
    """One run of formatted text. `link` is a URL, or page_link(doc, page)
    for a link to a page."""
    run = Msg()
    run.set_bytes(1, text)
    a = Msg()
    a.set_bytes(3, write_color(color))
    if link:
        a.set_bytes(4, link)
    if font:
        a.set_bytes(30, font)
    a.set_float(40, size if size is not None else _RICH_ATTR_FALLBACK)
    if italic:
        a.set_int(50, 1)
    a.set_int(60, -30 if bold else _RICH_ATTR_FALLBACK)
    a.set_float(70, _RICH_ATTR_FALLBACK)
    run.set_bytes(2, a)
    p = Msg()
    p.set_bytes(1, Msg([[1, LEN, b""]]))
    p.set_bytes(2, Msg([[1, VARINT, (1 << 64) - 1]]))
    p.set_bytes(3, Msg([[1, VARINT, (1 << 64) - 1], [2, VARINT, (1 << 64) - 1]]))
    run.set_bytes(3, p)
    return RichText(Msg([[1, LEN, run]]))


def _default_text_attrs(font="Helvetica Neue", size=24.0) -> Msg:
    a = Msg()
    a.set_bytes(3, Msg([[4, I32, struct.pack("<f", 1.0)]]))
    a.set_bytes(30, font)
    a.set_float(40, size)
    a.set_int(60, -60)
    a.set_float(70, -20.0)
    p = Msg()
    p.set_bytes(1, Msg([[1, LEN, b""]]))
    p.set_bytes(2, b"")
    p.set_bytes(3, Msg([[1, VARINT, (1 << 64) - 1], [2, VARINT, (1 << 64) - 1]]))
    return Msg([[1, LEN, a], [2, LEN, p]])


BOX_VERSION = 35


def new_box(doc: Document, origin, size, text: RichText | None = None, fill=None,
            outline=None, corner_radius: float | None = None,
            vertices: list[tuple[float, float]] | None = None, ellipse: bool = False,
            font="Helvetica Neue", font_size=24.0) -> Item:
    """A shape and/or text box: a rectangle (optional corner radius; a
    radius >= half the height makes a pill), an ellipse, or a polygon from
    `vertices` in the unit square (0..1). fill/outline None = none;
    outline = (width, rgba)."""
    bid, st = new_uuid(), stamp()
    b = Msg()
    b.set_bytes(1, bid)
    b.set_int(2, BOX_VERSION)
    b.set_bytes(3, st.copy())
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    b.set_bytes(20, Msg([[1, LEN, write_point(*origin)], [3, I32, struct.pack("<f", 1.0)]]))
    dims = write_point(*size)
    dims.set_float(3, float("inf"))
    b.set_bytes(21, Msg([[2, LEN, dims]]))
    geo = Msg()
    if ellipse:
        geo.set_bytes(2, b"")
    elif vertices:
        poly = Msg()
        for x, y in vertices:
            v = Msg()
            v.set_bytes(1, write_point(x, y))
            v.set_int(2, 1)
            poly.add(1, LEN, v)
        poly.set_int(2, 1)
        geo.set_bytes(3, Msg([[1, LEN, poly]]))
    elif corner_radius:
        geo.set_bytes(1, Msg([[1, I32, struct.pack("<f", corner_radius)]]))
    else:
        geo.set_bytes(1, b"")
    b.set_bytes(22, geo)
    fill_msg = Msg([[1, LEN, Msg([[1, LEN, write_color(fill)]]) if fill else b""]])
    b.set_bytes(30, fill_msg)
    if outline:
        o = _stroke_style(outline[0], outline[1], outline[2] if len(outline) > 2 else None)
    else:
        o = Msg([[2, LEN, Msg([[1, LEN, b""]])]])
    b.set_bytes(31, o)
    t = Msg()
    t.set_bytes(5, _default_text_attrs(font, font_size))
    t.set_bytes(10, Msg([[i, I32, struct.pack("<f", 10.0)] for i in (1, 2, 3, 4)]))
    b.set_bytes(32, t)
    item = Item(_meta(doc, bid, st, BOX_VERSION), Msg([[21, LEN, b]]))
    Box(item).text = text if text is not None else RichText(Msg.parse(_EMPTY_TEXT))
    t.fields.sort(key=lambda f: f[0])
    return item


ARROW_NONE, ARROW_OPEN, ARROW_FILLED = 0, 1, 2
DASHED, DOTTED = (3.0, 4.0), (0.0, 2.0)  # dash patterns, in multiples of line width


def new_line(doc: Document, start, end, mid=None, color=(0, 0, 0, 1),
             width: float = 3.0, arrow: int = ARROW_OPEN, elbow: bool = False,
             start_arrow: int = ARROW_NONE, dash: tuple[float, float] | None = None) -> Item:
    """A line (curved through `mid` if given) or, with elbow=True, an
    elbow connector. arrow/start_arrow: ARROW_NONE/OPEN/FILLED."""
    lid, st = new_uuid(), stamp()
    b = Msg()
    b.set_bytes(1, lid)
    b.set_int(2, 31)
    b.set_bytes(3, st.copy())
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    mid = mid or ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
    g = Msg()
    g.set_bytes(1, Msg([[1, LEN, write_point(*start)]]))
    if elbow:
        g.set_bytes(3, Msg([[1, LEN, write_point(*end)]]))
        g.set_bytes(5, Msg([[2, LEN, write_point(*mid)]]))
        b.set_bytes(21, g)
    else:
        g.set_bytes(2, write_point(*mid))
        g.set_bytes(3, Msg([[1, LEN, write_point(*end)]]))
        b.set_bytes(20, g)
    if start_arrow:
        b.set_int(30, start_arrow)
    if arrow:
        b.set_int(31, int(arrow))
    b.set_bytes(32, _stroke_style(width, color, dash))
    return Item(_meta(doc, lid, st, 31), Msg([[22, LEN, b]]))


def _stroke_style(width, color, dash=None) -> Msg:
    """{1 width, 2 {1 solid | 2 {1 dash, 2 gap}}, 3 {1 color}}."""
    s = Msg()
    s.set_float(1, width)
    if dash:
        pat = Msg()
        if dash[0]:
            pat.set_float(1, dash[0])
        pat.set_float(2, dash[1])
        s.set_bytes(2, Msg([[2, LEN, pat]]))
    else:
        s.set_bytes(2, Msg([[1, LEN, b""]]))
    s.set_bytes(3, Msg([[1, LEN, write_color(color)]]))
    return s


def new_image(doc: Document, data: bytes, center, size, angle: float = 0.0) -> Item:
    """Registers `data` as an attachment and places it."""
    aid = doc.add_attachment(data)
    iid, st = new_uuid(), stamp(6)
    b = Msg()
    b.set_bytes(1, iid)
    b.set_bytes(4, aid)
    b.set_bytes(5, Msg([[1, LEN, stamp()]]))
    b.set_int(6, 1)
    b.set_bytes(15, st.copy())
    b.set_int(18, SCHEMA_VERSION)
    item = Item(_meta(doc, iid, st, attachment=aid), Msg([[1, LEN, b]]))
    Image(item).place(center, size, angle)
    # keep GoodNotes' field order: 1, 2, 3, 4, ...
    b.fields.sort(key=lambda f: f[0])
    return item


def new_sticky(doc: Document, origin, size=(256.0, 256.0), text: RichText | None = None,
               color=(0.980, 0.906, 0.471, 1.0)) -> Item:
    sid, st = new_uuid(), stamp(4)
    b = Msg()
    b.set_bytes(1, sid)
    b.set_int(2, BOX_VERSION)
    b.set_bytes(3, st.copy())
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    b.set_bytes(20, Msg([[1, LEN, write_point(*origin)], [3, I32, struct.pack("<f", 1.0)]]))
    dims = write_point(*size)
    dims.set_float(3, float("inf"))
    b.set_bytes(21, Msg([[2, LEN, dims]]))
    b.set_bytes(30, write_color(color))
    t = Msg()
    t.set_bytes(5, _default_text_attrs())
    b.set_bytes(31, t)
    b.set_int(40, 1)
    b.set_int(41, 1)
    item = Item(_meta(doc, sid, st, BOX_VERSION), Msg([[20, LEN, b]]))
    if text is not None:
        Sticky(item).text = text
    return item
