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


