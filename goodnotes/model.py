"""Lossless read/write model of a .goodnotes document.

Two layers:

1. **Lossless core.** Every protobuf record is parsed into a `Msg` tree that
   re-encodes byte-for-byte, and every `bv41` blob into a `Blob` whose
   decompressed body is either a schema-driven template (`Template`, e.g.
   stroke geometry) or a nested protobuf (e.g. rich text). Nothing is
   interpreted unless asked for, so features that haven't been decoded
   still survive a load/save, and can be edited at field level.

2. **Typed views** (`Stroke`, `Box`, `Sticky`, `Line`, `Image`, `Math`,
   `TextDoc`, ...) over the core, with properties for every field GoodNotes
   itself names: position, size, rotation, color, text, geometry, lock
   state, groups, and so on. Setting a property edits the underlying `Msg`
   in place.

Field numbers and names come from GoodNotes' own protobuf definitions
(`goodnotes/schema.py`), recovered from the GoodNotes web app binary, and
were cross-checked against documents built in the macOS app.
"""
from __future__ import annotations

import math
import random
import re
import struct
import time
import uuid as uuidlib
import zipfile

from . import schema as S
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


BV_RAW = b"bv4-"


def blob_decompress(blob: bytes) -> bytes:
    """bv41 container: one or more [b"bv41", raw size, payload size, LZ4
    block] chunks (or [b"bv4-", size, raw bytes] for a stored block),
    then b"bv4$". Bodies over 32 KiB span several chunks."""
    out, pos = bytearray(), 0
    while blob[pos:pos + 4] in (BV_MAGIC, BV_RAW):
        if blob[pos:pos + 4] == BV_RAW:
            (raw,) = struct.unpack_from("<I", blob, pos + 4)
            out += blob[pos + 8:pos + 8 + raw]
            pos += 8 + raw
            continue
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

    `kind` names the payload by its top-level field (schema.ITEM): 'stroke'
    (7 penStroke2), 'box' (21 shape: shapes and text boxes), 'sticky' (20),
    'line' (22), 'image' (1: images, stickers, elements), 'math' (11),
    'text' (5, legacy RTF), 'text2' (8, legacy), 'textdoc' (13 nexus, the
    Yjs-backed Text Docs), 'fillshape' (9), 'eraser' (10). Unknown kinds keep
    their field number and still round-trip.
    """
    KINDS = {7: "stroke", 21: "box", 20: "sticky", 22: "line", 1: "image", 11: "math",
             5: "text", 8: "text2", 13: "textdoc", 9: "fillshape", 10: "eraser",
             2: "stroke1", 3: "fillellipse", 4: "fillpath", 6: "legacyline", 12: "textfragment"}

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

    @deleted.setter
    def deleted(self, on: bool) -> None:
        if self.meta is None:
            return
        if on:
            self.meta.set_int(3, 1)
        else:
            self.meta.remove(3)
        self.touch()

    @property
    def sequence(self) -> int:
        return self.meta.int(9) if self.meta is not None else 0

    @property
    def version(self) -> int:
        v = self.meta.sub(2) if self.meta is not None else None
        return v.int(1) if v is not None else 0

    def touch(self) -> None:
        """Bumps the edit counter and random tag (what GoodNotes does on
        every edit) so a synced copy knows the item changed."""
        if self.meta is None:
            return
        v = self.meta.sub(2, create=True)
        v.set_int(1, v.int(1) + 1)
        v.set_int(2, random.getrandbits(32))
        b = self.body
        if b is not None:
            for num in (3, 15, 7):  # shapes/lines/stickies: 3; strokes/images: 15; 7 position{1 stamp}
                s = b.sub(num)
                if s is not None and s.has(2) and isinstance(s.get(2), int):
                    s.set_int(1, s.int(1) + 1)
                    s.set_int(2, random.getrandbits(32))
                    break

    def typed(self):
        cls = _VIEWS.get(self.kind)
        return cls(self) if cls else self

    def copy(self, new_id: str | None = None) -> "Item":
        """Deep copy with a fresh id (and a fresh element id)."""
        nid = new_id or new_uuid()
        meta = self.meta.copy() if self.meta is not None else None
        payload = self.payload.copy()
        if meta is not None:
            meta.set_bytes(1, nid)
            meta.remove(3)
        body = Item(None, payload).body
        body.set_bytes(1, nid)
        return Item(meta, payload)

    def __repr__(self) -> str:
        return f"<{self.kind} {self.id}{' deleted' if self.deleted else ''}>"


class View:
    """Base for typed views; `item.body` is the payload submessage."""

    GROUP_FIELD = 8  # elementID: shapes, stickies, lines (strokes/images override)

    def __init__(self, item: Item):
        self.item = item
        self.b = item.body

    @property
    def id(self):
        return self.item.id

    @property
    def group(self) -> str | None:
        """Element-group id shared by items selected and grouped together."""
        g = self.b.str(self.GROUP_FIELD)
        return g if g and _UUID_RE.match(g.encode()) else None

    @group.setter
    def group(self, gid: str | None) -> None:
        if gid:
            self.b.set_bytes(self.GROUP_FIELD, gid)
        else:
            self.b.remove(self.GROUP_FIELD)

    def bounds(self) -> tuple[float, float, float, float] | None:
        """(x, y, w, h) in page units, if the item has a known extent."""
        return None

    def __repr__(self) -> str:
        return f"<{type(self).__name__} {self.id}>"


# --- strokes ---

PEN_KINDS = {0: "ballpoint", 1: "fountain", 2: "legacy", 3: "legacy2", 4: "brush", 5: "pencil"}
PEN_CODES = {v: k for k, v in PEN_KINDS.items()}
PSTROKE = S.STROKE_SCHEMA["CONSTANT_WIDTH"]
PSTROKE_OLD = "vuA(v)A(S(uu))A(S(uuuu))"  # pre-2021 files: no trailing version / width samples


def is_constant_width(schema: str) -> bool:
    return schema in (PSTROKE, PSTROKE_OLD)
VW_SCHEMA = S.STROKE_SCHEMA["VARIABLE_WIDTH"]
DW_SCHEMA = S.STROKE_SCHEMA["DYNAMIC_WIDTH"]
PENCIL_SCHEMA = S.STROKE_SCHEMA["PENCIL"]
ROUND, BUTT, SQUARE = 1, 0, 2  # line caps for dashed pen strokes


def _quad_points(p0, c, p1, step=3.0):
    n = max(1, int(math.dist(p0, p1) / step))
    out = []
    for i in range(1, n + 1):
        t = i / n
        out.append(((1 - t) ** 2 * p0[0] + 2 * (1 - t) * t * c[0] + t * t * p1[0],
                    (1 - t) ** 2 * p0[1] + 2 * (1 - t) * t * c[1] + t * t * p1[1]))
    return out


class Stroke(View):
    """Ink (penStroke2). Geometry lives in a bv41 template blob (field 2)
    whose schema depends on the pen (`pen`): ballpoint = constant width
    quadratic path, fountain/brush = variable width centerline plus a
    precomputed outline, pencil = textured points. A recognized shape
    (shape tool) adds a descriptor at field 9 and stores a placeholder
    path. Field 6 is a translation applied on top of the stored points
    (GoodNotes moves ink this way instead of rewriting it)."""

    GROUP_FIELD = 10

    @property
    def color(self):
        return read_color(self.b.sub(4))

    @color.setter
    def color(self, rgba):
        self.b.set_bytes(4, write_color(rgba))
        self.item.touch()

    @property
    def pen(self) -> str:
        """ballpoint, fountain, brush, pencil (or legacy / legacy2)."""
        return PEN_KINDS.get(self.b.int(3), f"pen{self.b.int(3)}")

    @property
    def highlighter(self) -> bool:
        """Highlighter ink is a stroke drawn with the MULTIPLY blend mode."""
        return self.b.int(5) == 1

    @highlighter.setter
    def highlighter(self, on: bool) -> None:
        if on:
            self.b.set_int(5, 1)
        else:
            self.b.remove(5)

    @property
    def tape(self) -> bool:
        return bool(self.b.bytes(20))

    @property
    def translation(self) -> tuple[float, float]:
        """Offset GoodNotes adds to every stored point (set when ink is
        moved with the lasso). Rendering must add it."""
        return read_point(self.b.sub(6)) if self.b.sub(6) is not None else (0.0, 0.0)

    @translation.setter
    def translation(self, xy) -> None:
        self.b.set_bytes(6, write_point(*xy))

    @property
    def dash(self) -> tuple[list[float], int] | None:
        """(dash lengths in multiples of width, line cap) for a dashed pen
        stroke, None when solid."""
        ls = self.b.sub(11)
        pat = ls.sub(2) if ls is not None else None
        if pat is None:
            return None
        raw = pat.get(1)
        lens = []
        if isinstance(raw, Msg):
            raw = raw.encode()
        if isinstance(raw, (bytes, bytearray)):
            lens = [struct.unpack_from("<f", raw, i)[0] for i in range(0, len(raw) - 3, 4)]
        return lens, pat.int(2)

    @dash.setter
    def dash(self, value) -> None:
        self.b.remove(11)
        if value:
            lens, cap = (value, ROUND) if not isinstance(value, tuple) or len(value) != 2 or not isinstance(value[1], int) else value
            pat = Msg()
            pat.set_bytes(1, b"".join(struct.pack("<f", float(v)) for v in lens))
            if cap:
                pat.set_int(2, cap)
            self.b.set_bytes(11, Msg([[2, LEN, pat]]))

    @property
    def locked_by_ai(self) -> bool:
        return self.b.int(18) == 1

    @property
    def template(self) -> Template:
        return Template.parse(blob_decompress(self.b.bytes(2)))

    @template.setter
    def template(self, t: Template):
        self.b.set_bytes(2, blob_compress(t.encode()))

    @property
    def shape(self) -> Msg | None:
        """Recognized-shape descriptor (schema.STROKE_SHAPE): 1 polyline
        {1 point...}, 3 rectangle {1 center, 2 size}, 4 ellipse {1 center,
        2 radii, 3 angle}, 15 line width."""
        s = self.b.sub(9)
        return s if s is not None and s.fields else None

    @property
    def erasers(self) -> list[Msg]:
        """Partial-eraser strokes GoodNotes subtracts from this ink
        (field 16); kept verbatim."""
        return self.b.subs(16)

    # geometry -----------------------------------------------------------
    @property
    def thickness(self) -> float:
        t = self.template
        if t.schema.startswith("vu"):
            return f32(t.values[1])
        if t.schema == VW_SCHEMA:
            w = self.widths
            return 2 * max(w) if w else 0.0
        return 0.0

    @property
    def commands(self) -> list[tuple]:
        """[('M', (x, y)) | ('Q', (cx, cy), (x, y))] for ballpoint ink."""
        t = self.template
        if not is_constant_width(t.schema):
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

    def centerline(self) -> list[tuple]:
        """'M'/'Q' commands of the stroke's centerline for every pen kind,
        with per-point half-widths for variable-width pens appended as a
        third tuple element on 'M' and fourth on 'Q': ('M', (x, y), w),
        ('Q', (cx, cy), (x, y), w)."""
        t = self.template
        if is_constant_width(t.schema):
            return self.commands
        out = []
        if t.schema in (VW_SCHEMA, DW_SCHEMA):
            off = 1 if t.schema == VW_SCHEMA else 2
            kinds, moves, quads = t.values[off], t.values[off + 1], t.values[off + 2]
            mi = qi = 0
            codes = S.VW_COMMAND if t.schema == VW_SCHEMA else S.DW_COMMAND
            for k in kinds:
                name = codes.get(k, "")
                if name == "constantMoveTo":
                    out.append(("M", (f32(moves[mi]), f32(moves[mi + 1])), f32(t.values[1]) / 2))
                    mi += 2
                elif name == "constantQuadCurveTo":
                    q = [f32(x) for x in quads[qi:qi + 4]]
                    out.append(("Q", (q[0], q[1]), (q[2], q[3]), f32(t.values[1]) / 2))
                    qi += 4
                elif "WithRotation" in name and "oveTo" in name:   # x y w rotation
                    out.append(("M", (f32(moves[mi]), f32(moves[mi + 1])), f32(moves[mi + 2])))
                    mi += 4
                elif "WithRotation" in name:   # cx cy cw x y w alt1 alt2 k (marker pen)
                    q = [f32(x) for x in quads[qi:qi + 9]]
                    out.append(("Q", (q[0], q[1]), (q[3], q[4]), q[5]))
                    qi += 9
                elif name.endswith("moveTo") or name.endswith("MoveTo"):
                    out.append(("M", (f32(moves[mi]), f32(moves[mi + 1])), f32(moves[mi + 2])))
                    mi += 3
                elif "quadCurveTo" in name or "QuadCurveTo" in name:
                    q = [f32(x) for x in quads[qi:qi + 6]]
                    out.append(("Q", (q[0], q[1]), (q[3], q[4]), q[5]))
                    qi += 6
                else:
                    break  # cubic/arc centerlines are not produced by any pen
            return out
        if t.schema == PENCIL_SCHEMA:
            kinds, moves, quads = t.values[2], t.values[3], t.values[4]
            mi = qi = 0
            for k in kinds:
                if k == 0 and mi < len(moves):
                    m = [f32(x) for x in moves[mi]]
                    out.append(("M", (m[0], m[1]), m[4]))
                    mi += 1
                elif qi < len(quads):
                    q = [f32(x) for x in quads[qi]]
                    out.append(("Q", (q[1], q[2]), (q[6], q[7]), q[10]))
                    qi += 1
            return out
        raise ValueError(f"no path decoder for schema {t.schema}")

    @property
    def widths(self) -> list[float]:
        """Per-point half widths of a variable-width stroke."""
        return [c[-1] for c in self.centerline() if len(c) > 2 and not isinstance(c[-1], tuple)]

    def points(self, step: float = 3.0) -> list[tuple[float, float]]:
        """Flattened centerline (translation applied)."""
        dx, dy = self.translation
        pts, cur = [], None
        for c in self.centerline():
            if c[0] == "M":
                cur = c[1]
                pts.append(cur)
            else:
                pts += _quad_points(cur, c[1], c[2], step)
                cur = c[2]
        return [(x + dx, y + dy) for x, y in pts]

    def bounds(self):
        pts = self.points(6.0)
        if not pts:
            s = self.shape
            if s is None:
                return None
            pts = [read_point(p) for p in (s.sub(1).subs(1) if s.sub(1) is not None else [])]
            for n in (3, 4):
                r = s.sub(n)
                if r is not None:
                    (cx, cy), (w, h) = read_point(r.sub(1)), read_point(r.sub(2))
                    pts += [(cx - w, cy - h), (cx + w, cy + h)]
            if not pts:
                return None
        half = self.thickness / 2
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs) - half, min(ys) - half, max(xs) - min(xs) + 2 * half, max(ys) - min(ys) + 2 * half)

    def set_path(self, commands, thickness: float | None = None) -> None:
        """Replace geometry with a ballpoint path ('M'/'Q'/'L' commands)."""
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
        self.b.remove(6)

    def translate(self, dx: float, dy: float) -> None:
        """Moves the ink the way GoodNotes does: by its translation field."""
        if self.shape is not None:
            _translate_shape(self.shape, dx, dy)
        if is_constant_width(self.template.schema):
            tx, ty = self.translation
            self.set_path([(c[0], *[(x + tx + dx, y + ty + dy) for x, y in c[1:]]) for c in self.commands],
                          self.thickness)
            return
        tx, ty = self.translation
        self.translation = (tx + dx, ty + dy)


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


# --- rich text ---

class TextRun:
    """One run of text with inline attributes (schema.INLINE_ATTRS) and the
    paragraph attributes of the paragraph it is in (schema.BLOCK_ATTRS).
    Build with keyword arguments; None means "inherit the box default"."""

    def __init__(self, text: str, color=None, font: str | None = None, size: float | None = None,
                 bold: bool | None = None, italic: bool | None = None, underline: bool = False,
                 strike: bool = False, link: str | None = None, highlight=None,
                 align: str | None = None, list_style: str | None = None, heading: int | None = None,
                 line_height: float | None = None, indent: int | None = None,
                 space_after: float | None = None, space_before: float | None = None):
        self.text, self.color, self.font, self.size = text, color, font, size
        self.bold, self.italic, self.underline, self.strike = bold, italic, underline, strike
        self.link, self.highlight = link, highlight
        self.align, self.list_style, self.heading = align, list_style, heading
        self.line_height, self.indent = line_height, indent
        self.space_after, self.space_before = space_after, space_before

    ALIGN = {"left": 0, "center": 1, "right": 2, "justify": 3, "justified": 3}
    LISTS = {"bullet": 0, "bulleted": 0, "number": 1, "numbered": 1}

    def to_msg(self) -> Msg:
        run = Msg()
        run.set_bytes(1, self.text)
        a = Msg()
        if self.strike:
            a.set_int(1, 1)
        if self.underline:
            a.set_int(2, 1)
        a.set_bytes(3, write_color(self.color if self.color is not None else (0.118, 0.106, 0.106, 1)))
        if self.link:
            a.set_bytes(4, self.link)
        if self.highlight is not None:
            a.set_bytes(5, write_color(self.highlight))
        if self.font:
            a.set_bytes(30, self.font)
        a.set_float(40, self.size if self.size is not None else S.INHERIT)
        if self.italic:
            a.set_int(50, 1)
        a.set_int(60, -30 if self.bold else S.INHERIT)
        a.set_float(70, S.INHERIT)
        run.set_bytes(2, a)
        p = Msg()
        if self.heading:
            p.set_bytes(1, Msg([[2, LEN, Msg([[1, VARINT, int(self.heading)]])]]))
        else:
            p.set_bytes(1, Msg([[1, LEN, b""]]))
        if self.line_height is not None:
            lh = Msg([[1, VARINT, 1]])
            lh.set_float(2, self.line_height)
            p.set_bytes(2, lh)
        else:
            p.set_bytes(2, Msg([[1, VARINT, (1 << 64) - 1]]))
        if self.list_style is not None:
            p.set_bytes(3, Msg([[1, VARINT, self.LISTS[self.list_style]], [2, VARINT, 1]]))
        else:
            p.set_bytes(3, Msg([[1, VARINT, (1 << 64) - 1], [2, VARINT, (1 << 64) - 1]]))
        if self.align:
            p.set_int(4, self.ALIGN[self.align])
        if self.space_after is not None:
            p.set_float(5, self.space_after)
        if self.space_before is not None:
            p.set_float(6, self.space_before)
        if self.indent:
            p.set_int(7, self.indent)
        run.set_bytes(3, p)
        return run

    @classmethod
    def from_msg(cls, run: Msg) -> "TextRun":
        a = run.sub(2) or Msg()
        p = run.sub(3) or Msg()
        size = a.float(40)
        weight = a.sint64(60)
        r = cls(run.str(1, ""),
                color=read_color(a.sub(3), None) if a.sub(3) is not None else None,
                font=a.str(30), size=size if size > 0 else None,
                bold=True if weight == -30 else (False if weight not in (S.INHERIT, 0) else None),
                italic=bool(a.int(50)), underline=bool(a.int(2)), strike=bool(a.int(1)),
                link=a.str(4), highlight=read_color(a.sub(5), None) if a.sub(5) is not None else None)
        bt = p.sub(1)
        if bt is not None and bt.sub(2) is not None:
            r.heading = bt.path(2).int(1) or 1
        lh = p.sub(2)
        if lh is not None and lh.int(1) == 1:
            r.line_height = lh.float(2)
        ls = p.sub(3)
        if ls is not None and ls.int(1) in (0, 1) and ls.has(1) and ls.int(1) < (1 << 63):
            r.list_style = "bullet" if ls.int(1) == 0 else "number"
        if p.has(4):
            r.align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(p.int(4))
        if p.has(5):
            r.space_after = p.float(5)
        if p.has(6):
            r.space_before = p.float(6)
        if p.has(7):
            r.indent = p.int(7)
        return r

    def __repr__(self):
        flags = [k for k in ("bold", "italic", "underline", "strike") if getattr(self, k)]
        return f"<TextRun {self.text!r} {' '.join(flags)}>"


class RichText:
    """Attributed text: a list of runs (schema.RICH_TEXT / SEGMENT).

    `runs` are the raw run messages; `spans()` decodes them to TextRun
    objects; `text` is the plain text. Build new text with `rich_text()`
    or `RichText.build([TextRun(...), ...])`.
    """

    def __init__(self, msg: Msg):
        self.msg = msg

    @classmethod
    def build(cls, runs: list[TextRun]) -> "RichText":
        """Builds attributed text. Paragraph attributes (align, list style,
        heading, ...) are taken from the first run of each paragraph and
        applied to every character of it, including the newline that ends
        it, which is how GoodNotes reads them; a run that starts with a
        newline therefore styles the paragraph *after* the newline."""
        pieces = []  # (text, run) with paragraph attrs normalized
        para_attrs = None
        for r in runs:
            parts = r.text.split("\n")
            for i, part in enumerate(parts):
                if i:
                    pieces.append(("\n", r, para_attrs if para_attrs is not None else r))
                    para_attrs = None
                if part:
                    if para_attrs is None:
                        para_attrs = r
                    pieces.append((part, r, para_attrs))
        out = []
        for text, r, pa in pieces:
            run = TextRun.__new__(TextRun)
            run.__dict__.update(r.__dict__)
            run.text = text
            for k in ("align", "list_style", "heading", "line_height", "indent", "space_after", "space_before"):
                setattr(run, k, getattr(pa, k))
            if out and out[-1].__dict__ | {"text": None} == run.__dict__ | {"text": None}:
                out[-1].text += text
            else:
                out.append(run)
        return cls(Msg([[1, LEN, r.to_msg()] for r in out]))

    @property
    def runs(self) -> list[Msg]:
        return self.msg.subs(1)

    def spans(self) -> list[TextRun]:
        return [TextRun.from_msg(r) for r in self.runs]

    @property
    def text(self) -> str:
        return "".join(r.str(1, "") for r in self.runs)

    def paragraphs(self) -> list[list[TextRun]]:
        """Runs grouped by paragraph (split on newlines)."""
        out, cur = [], []
        for r in self.spans():
            parts = r.text.split("\n")
            for i, part in enumerate(parts):
                if i:
                    out.append(cur)
                    cur = []
                if part:
                    piece = TextRun.__new__(TextRun)
                    piece.__dict__.update(r.__dict__)
                    piece.text = part
                    cur.append(piece)
        out.append(cur)
        return out

    def append(self, run: TextRun) -> None:
        self.msg.add(1, LEN, run.to_msg())

    def set_text(self, s: str) -> None:
        """Replace all text, keeping the first run's formatting."""
        runs = self.runs
        first = runs[0].copy() if runs else TextRun("").to_msg()
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

    def set_style(self, **attrs) -> None:
        """Re-applies TextRun attributes (bold=, underline=, align=, ...) to
        every run, keeping each run's text."""
        new = []
        for r in self.spans():
            for k, v in attrs.items():
                setattr(r, k, v)
            new.append(r)
        self.msg.fields = [[1, LEN, r.to_msg()] for r in new]

    def __repr__(self):
        return f"<RichText {self.text!r}>"


# a shape's empty text, exactly as GoodNotes writes it, with its hash
_EMPTY_TEXT = (b"\n]\x12.\x1a\x14\r\xf1\xf0\xf0=\x15\xd9\xd8\xd8=\x1d\xd9\xd8\xd8=%\x00\x00\x80?\xc5\x02"
               b"\x00\x00\x80A\xe0\x03\xec\xfc\xff\xff\xff\xff\xff\xff\xff\x01\xb5\x04\x00\x00\xca\xc3\x1a+"
               b"\n\x02\n\x00\x12\x0b\x08\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01\x1a\x16\x08\xff\xff\xff"
               b"\xff\xff\xff\xff\xff\xff\x01\x10\xff\xff\xff\xff\xff\xff\xff\xff\xff\x01 \x02")
_EMPTY_TEXT_HASH = bytes.fromhex("afd2f3d38e516ada")


# --- boxes: shapes, text boxes and sticky notes ---

class _Rect(View):
    """Shared transform (20: translation, rotation, scale) and sizing
    strategy (21) handling for shapes, text boxes and sticky notes."""

    @property
    def origin(self):
        return read_point(self.b.path(20, 1))

    @origin.setter
    def origin(self, xy):
        self.b.sub(20, create=True).set_bytes(1, write_point(*xy))

    @property
    def rotation(self) -> float:
        """Rotation about the box center, radians clockwise on screen."""
        t = self.b.sub(20)
        return t.float(2) if t is not None else 0.0

    @rotation.setter
    def rotation(self, radians: float) -> None:
        t = self.b.sub(20, create=True)
        if radians:
            t.set_float(2, radians)
        else:
            t.remove(2)

    @property
    def scale(self) -> float:
        t = self.b.sub(20)
        return t.float(3) if t is not None and t.has(3) else 1.0

    @property
    def size(self):
        s = self.b.sub(21)
        if s is None:
            return (0.0, 0.0)
        fixed = s.sub(1)
        if fixed is not None:
            return (fixed.float(1), fixed.float(2))
        ah = s.sub(2)
        if ah is not None:
            return (ah.float(1), ah.float(2))
        auto = s.sub(3)
        if auto is not None:
            return (auto.float(1), auto.float(3))
        return (0.0, 0.0)

    @size.setter
    def size(self, wh):
        s = self.b.sub(21, create=True)
        s.fields = []
        d = s.sub(2, create=True)
        d.set_float(1, wh[0])
        d.set_float(2, wh[1])
        d.set_float(3, float("inf"))

    @property
    def sizing(self) -> str:
        """'fixed' (w, h), 'auto-height' (w, min h, max h) or 'auto' (text
        box grows with its text)."""
        s = self.b.sub(21)
        if s is None:
            return "fixed"
        return {1: "fixed", 2: "auto-height", 3: "auto"}.get(s.fields[0][0] if s.fields else 2, "fixed")

    def set_auto_size(self, max_width: float = float("inf"), min_width: float = 0.0,
                      min_height: float = 0.0, max_height: float = float("inf")) -> None:
        """Let the box size itself to its text, wrapping at `max_width`."""
        s = self.b.sub(21, create=True)
        s.fields = []
        a = s.sub(3, create=True)
        a.set_float(1, min_width)
        a.set_float(2, max_width)
        a.set_float(3, min_height)
        a.set_float(4, max_height)
        self.b.set_int(9, 1)

    @property
    def locked(self) -> bool:
        return self.b.int(10) == 1

    @locked.setter
    def locked(self, on: bool) -> None:
        if on:
            self.b.set_int(10, 1)
        else:
            self.b.remove(10)

    def bounds(self):
        (x, y), (w, h) = self.origin, self.size
        return (x, y, w, h)

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
    """Shapes and text boxes share one record (21, schema.SHAPE).

    20 transform {1 origin, 2 rotation, 3 scale}; 21 sizing strategy;
    22 geometry ({1 {1 corner radius}} rectangle, {2} ellipse, {3 hobby
    path} polygon/freeform); 30 fill {1 {1 color}}; 31 outline {1 width,
    2 pattern, 3 {1 color}}; 32 text; 33 shadow {1 color, 2 radius,
    3 offset}; 8 element group id; 9 kind (0 shape, 1 text box); 10 lock."""

    @property
    def is_text_box(self) -> bool:
        return self.b.int(9) == 1

    @property
    def fill(self):
        f = self.b.path(30, 1, 1)
        return read_color(f, None) if f is not None else None

    @fill.setter
    def fill(self, rgba):
        self.b.remove(30)
        if rgba is not None:
            self.b.sub(30, create=True).sub(1, create=True).set_bytes(1, write_color(rgba))
        else:
            self.b.set_bytes(30, Msg([[1, LEN, b""]]))

    @property
    def outline(self):
        """(width, rgba) or None."""
        o = self.b.sub(31)
        if o is None or o.sub(3) is None:
            return None
        return o.float(1), read_color(o.path(3, 1), None)

    @outline.setter
    def outline(self, width_rgba):
        if width_rgba is None:
            self.b.set_bytes(31, Msg([[2, LEN, Msg([[1, LEN, b""]])]]))
            return
        width, rgba = width_rgba[0], width_rgba[1]
        dash = width_rgba[2] if len(width_rgba) > 2 else None
        self.b.set_bytes(31, _stroke_style(width, rgba, dash))

    @property
    def dash(self) -> tuple[float, float] | None:
        return _read_dash(self.b.sub(31))

    @property
    def shadow(self):
        """(rgba, radius, (dx, dy)) or None."""
        s = self.b.sub(33)
        if s is None or not s.fields:
            return None
        return read_color(s.sub(1)), s.float(2), read_point(s.sub(3))

    @shadow.setter
    def shadow(self, value):
        self.b.remove(33)
        if value:
            rgba, radius, offset = value
            s = Msg()
            s.set_bytes(1, write_color(rgba))
            s.set_float(2, radius)
            s.set_bytes(3, write_point(*offset))
            self.b.set_bytes(33, s)

    @property
    def text(self) -> RichText | None:
        return self._text_msg(32)

    @text.setter
    def text(self, rt: RichText):
        self._set_text_msg(32, rt)
        self.measure()

    def measure(self) -> None:
        """Stores the text's laid-out size (container field 2) for the
        box's wrap width: the box's max width for an auto-sizing text box,
        its width otherwise, minus padding. GoodNotes wraps and renders at
        this size (and re-measures when the text is edited)."""
        t = self.b.sub(32)
        rt = self.text
        if t is None or rt is None:
            return
        s = self.b.sub(21)
        auto = s.sub(3) if s is not None else None
        width = auto.float(2) if auto is not None else self.size[0]
        if not (0 < width < float("inf")):
            width = self.size[0] or 700.0
        attrs = t.path(5, 1)
        default = attrs.float(40) if attrs is not None and attrs.float(40) > 0 else 24.0
        w, h = estimate_text_size(rt, width - 2 * self.padding, default)
        t.set_bytes(2, write_point(w, h))
        t.fields.sort(key=lambda f: f[0])

    @property
    def padding(self) -> float:
        ins = self.b.path(32, 10)
        return ins.float(1) if ins is not None else 10.0

    @property
    def geometry(self) -> str:
        g = self.b.sub(22)
        if g is None or not g.fields:
            return "rectangle"
        return {1: "rectangle", 2: "ellipse", 3: "polygon"}.get(g.fields[0][0], "rectangle")

    @property
    def corner_radius(self) -> float:
        g = self.b.path(22, 1)
        return g.float(1) if g is not None else 0.0

    @corner_radius.setter
    def corner_radius(self, r: float) -> None:
        geo = self.b.sub(22, create=True)
        geo.fields = []
        geo.set_bytes(1, Msg([[1, I32, struct.pack("<f", r)]]) if r else b"")

    @property
    def vertices(self) -> list[tuple[float, float]] | None:
        poly = self.b.path(22, 3, 1)
        return [read_point(v.sub(1)) for v in poly.subs(1)] if poly is not None else None

    @vertices.setter
    def vertices(self, pts) -> None:
        geo = self.b.sub(22, create=True)
        geo.fields = []
        geo.set_bytes(3, _polygon(pts))

    def set_ellipse(self) -> None:
        geo = self.b.sub(22, create=True)
        geo.fields = []
        geo.set_bytes(2, b"")


def estimate_text_size(rt: "RichText", width: float, default_size: float = 24.0) -> tuple[float, float]:
    """(width, height) of rich text wrapped at `width` page units, with an
    average glyph width of 0.5 em and 1.2 em line height."""
    max_w, height = 0.0, 0.0
    for para in rt.paragraphs():
        first = para[0] if para else None
        size = max((r.size or default_size for r in para), default=default_size)
        lines, cur = [], 0.0
        for r in para:
            em = 0.5 * (r.size or default_size)
            for word in re.split(r"(\s+)", r.text):
                if not word:
                    continue
                ww = em * len(word)
                if cur and cur + ww > width and not word.isspace():
                    lines.append(cur)
                    cur = 0.0
                cur += ww
        lines.append(cur)
        max_w = max(max_w, max(lines))
        height += len(lines) * size * (first.line_height if first and first.line_height else 1.2)
    return (min(max_w, width), height)


def _polygon(vertices, closed=True) -> Msg:
    poly = Msg()
    for x, y in vertices:
        v = Msg()
        v.set_bytes(1, write_point(x, y))
        v.set_int(2, 1)
        poly.add(1, LEN, v)
    if closed:
        poly.set_int(2, 1)
    return Msg([[1, LEN, poly]])


def _read_dash(style) -> tuple[float, float] | None:
    pat = style.path(2, 2) if style is not None and style.sub(2) is not None else None
    return (pat.float(1), pat.float(2)) if pat is not None else None


class Sticky(_Rect):
    """Sticky-note tool (20, schema.STICKY_NOTE): 30 paper color, 31 text
    (as Box 32), 32 author id, 33 author name, 40 expanded, 41 show author."""

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

    @property
    def author(self) -> tuple[str | None, str | None]:
        return self.b.str(32), self.b.str(33)

    @author.setter
    def author(self, id_name):
        aid, name = id_name
        self.b.set_bytes(32, aid or "")
        self.b.set_bytes(33, name or "")

    @property
    def expanded(self) -> bool:
        return self.b.int(40) == 1

    @expanded.setter
    def expanded(self, on: bool):
        self.b.set_int(40, 1 if on else 0)

    @property
    def show_author(self) -> bool:
        return self.b.int(41) == 1

    @show_author.setter
    def show_author(self, on: bool):
        self.b.set_int(41, 1 if on else 0)


class Line(View):
    """Lines and connectors (22, schema.LINE). 20 'hobby' line (straight
    or curved through middle points): {1 {1 start}, 2 mid handle (on the
    curve), 3 {1 end}}; 21 elbow connector: {1 {1 start}, 2 middle, 3 {1
    end}, 4 first leg direction, 5 rotation {1 angle, 2 origin}}. 30/31
    start/end arrowheads (0 none, 1 open, 2 filled), 32 stroke {1 width,
    2 pattern, 3 {1 color}}. Line ends can carry a connection
    {1 anchor id, 2 angle} to a shape they are glued to."""

    def _geom(self) -> Msg | None:
        return self.b.sub(20) or self.b.sub(21)

    def _point_msgs(self) -> list[Msg]:
        g = self._geom()
        if g is None:
            return []
        out = []
        for f in g.fields:
            if f[1] != LEN or f[0] not in (1, 2, 3, 5):
                continue
            p = g._parsed(f)
            if f[0] == 2 and g is self.b.sub(21):  # elbow middle points: repeated points
                out.append(p)
                continue
            inner = p.sub(1) or p.sub(2)
            out.append(inner if inner is not None and inner.fields else p)
        return out

    @property
    def points(self) -> list[tuple[float, float]]:
        """start, [mid handle / knee], end in stored order."""
        return [read_point(p) for p in self._point_msgs()]

    @property
    def start(self):
        g = self._geom()
        return read_point(g.path(1, 1)) if g is not None else (0.0, 0.0)

    @property
    def end(self):
        g = self._geom()
        return read_point(g.path(3, 1)) if g is not None else (0.0, 0.0)

    @property
    def mid(self):
        """Point on the curve a curved line passes through (None if straight)."""
        g = self.b.sub(20)
        if g is None or g.sub(2) is None:
            return None
        m = read_point(g.sub(2))
        s, e = self.start, self.end
        if abs(m[0] - (s[0] + e[0]) / 2) < 1e-3 and abs(m[1] - (s[1] + e[1]) / 2) < 1e-3:
            return None
        return m

    @property
    def elbow(self) -> bool:
        return self.b.sub(21) is not None

    @property
    def knee(self):
        """Elbow connector: the corner's origin."""
        r = self.b.path(21, 5)
        return read_point(r.sub(2)) if r is not None else None

    def set_points(self, start, end, mid=None) -> None:
        g = self._geom()
        if g is None:
            g = self.b.sub(20, create=True)
        g.sub(1, create=True).set_bytes(1, write_point(*start))
        g.sub(3, create=True).set_bytes(1, write_point(*end))
        if not self.elbow:
            mid = mid or ((start[0] + end[0]) / 2, (start[1] + end[1]) / 2)
            g.set_bytes(2, write_point(*mid))
        elif mid is not None:
            g.sub(5, create=True).set_bytes(2, write_point(*mid))

    @property
    def connections(self) -> tuple[str | None, str | None]:
        """Ids of the shapes the start and end are glued to."""
        g = self._geom()
        if g is None:
            return None, None
        return (g.path(1, 2).str(1) if g.path(1, 2) is not None else None,
                g.path(3, 2).str(1) if g.path(3, 2) is not None else None)

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

    @width.setter
    def width(self, w: float):
        self.b.sub(32, create=True).set_float(1, w)

    @property
    def dash(self) -> tuple[float, float] | None:
        return _read_dash(self.b.sub(32))

    @dash.setter
    def dash(self, pattern):
        s = self.b.sub(32, create=True)
        self.b.set_bytes(32, _stroke_style(s.float(1) or 3.0, read_color(s.path(3, 1)), pattern))

    @property
    def arrow(self) -> int:
        return self.b.int(31)

    @arrow.setter
    def arrow(self, style: int):
        if style:
            self.b.set_int(31, style)
        else:
            self.b.remove(31)

    @property
    def start_arrow(self) -> int:
        return self.b.int(30)

    @start_arrow.setter
    def start_arrow(self, style: int):
        if style:
            self.b.set_int(30, style)
        else:
            self.b.remove(30)

    def bounds(self):
        pts = self.points
        if not pts:
            return None
        xs, ys = [p[0] for p in pts], [p[1] for p in pts]
        return (min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys))

    def translate(self, dx, dy):
        for p in self._point_msgs():
            p.set_float(1, p.float(1) + dx)
            p.set_float(2, p.float(2) + dy)


def image_format(data: bytes) -> int:
    """schema.IMAGE_FORMAT code for raw image bytes."""
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return 0
    if data[:3] == b"\xff\xd8\xff":
        return 1
    if data[:4] == b"%PDF":
        return 3
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return 5
    if data[4:12] in (b"ftypheic", b"ftypheix", b"ftypmif1", b"ftypheif"):
        return 4
    if data[:12] == b"\x00\x00\x00\x0cjP  \r\n\x87\n" or data[:4] == b"\xff\x4f\xff\x51":
        return 2
    return 1


class Image(View):
    """Placed image, sticker or element (1, schema.IMAGE). 2 {origin,
    size} = axis-aligned bounds of the rotated image; 3 {center, size,
    3 angle in radians}; 4 attachment id; 6 format (0 PNG, 1 JPEG, 3 PDF
    for stickers/elements, 4 HEIC, 5 GIF); 7 element group id; 19/20
    graph-widget data; 21 lock status."""

    GROUP_FIELD = 7

    @property
    def attachment(self) -> str:
        return self.b.str(4)

    @attachment.setter
    def attachment(self, aid: str):
        self.b.set_bytes(4, aid)
        if self.item.meta is not None:
            self.item.meta.set_bytes(4, aid)

    @property
    def format(self) -> str:
        return S.IMAGE_FORMAT.get(self.b.int(6), "PNG")

    @property
    def is_sticker(self) -> bool:
        return self.b.int(6) == 3

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

    @property
    def locked(self) -> bool:
        return self.b.int(21) == 1

    @locked.setter
    def locked(self, on: bool):
        if on:
            self.b.set_int(21, 1)
        else:
            self.b.remove(21)

    @property
    def graph(self) -> str | None:
        """Graph-widget JSON (math graphs), if this image is one."""
        return self.b.str(20)

    def bounds(self):
        r = self.b.sub(2)
        if r is None:
            return None
        (x, y), (w, h) = read_point(r.sub(1)), read_point(r.sub(2))
        return (x, y, w, h)

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
    """Handwriting converted to math (11, schema.MATH): 2/3 rects as
    Image, 4 attachment (rendered image), 9 LaTeX, 11 the original
    strokes {1 blob, 2 color, 3 type, 4 translation}."""

    @property
    def latex(self) -> str | None:
        return self.b.str(9)

    @latex.setter
    def latex(self, s: str):
        self.b.set_bytes(9, s)

    @property
    def attachment(self) -> str | None:
        return self.b.str(4)

    @property
    def original_strokes(self) -> list[Msg]:
        return self.b.subs(11)

    def bounds(self):
        return Image(self.item).bounds()

    def translate(self, dx, dy):
        Image(self.item).translate(dx, dy)


class LegacyText(View):
    """Pre-2022 text boxes (5 and 8): RTF or tagged content, read-only."""

    @property
    def origin(self):
        return read_point(self.b.path(2, 1))

    @property
    def size(self):
        return read_point(self.b.path(2, 2))

    @property
    def text(self) -> str:
        if self.item.field == 5:
            return _rtf_to_text((self.b.bytes(6) or b"").decode("utf-8", "replace"))
        c = self.b.sub(5)
        return c.str(1, "") if c is not None else ""

    def bounds(self):
        (x, y), (w, h) = self.origin, self.size
        return (x, y, w, h)

    def translate(self, dx, dy):
        p = self.b.path(2, 1, create=True)
        p.set_float(1, p.float(1) + dx)
        p.set_float(2, p.float(2) + dy)


def _rtf_to_text(rtf: str) -> str:
    """Just enough RTF stripping for legacy text boxes."""
    rtf = re.sub(r"\\'([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), rtf)
    rtf = re.sub(r"\{\\\*[^{}]*\}|\{\\(fonttbl|colortbl|stylesheet)[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", rtf)
    rtf = rtf.replace("\\par", "\n").replace("\\\n", "\n")
    rtf = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", rtf)
    return rtf.strip()


class TextDoc(View):
    """A Text Doc (13 nexus): a Yjs document. The CRDT update (10) and
    snapshot (11) are exposed as bytes; `plain_text` scrapes readable
    strings out of the Yjs update for search/indexing."""

    @property
    def yjs_update(self) -> bytes | None:
        return self.b.bytes(10)

    @property
    def yjs_snapshot(self) -> bytes | None:
        return self.b.bytes(11)

    @property
    def plain_text(self) -> str:
        data = self.yjs_update or b""
        return " ".join(m.decode() for m in re.findall(rb"[\x20-\x7e\xc2-\xf4][\x20-\x7e\x80-\xbf]{3,}", data))


class FillShape(View):
    """A filled shape made by the shape tool's fill option (9 fillShape):
    2 rect, 4 shapes {3 rectangle {1 center, 2 size} | 4 ellipse {1
    center, 2 radii, 3 angle} | 1 polyline {1 points}}, 5 the stroke it
    was recognized from, 6 translation, 7 fill color."""

    GROUP_FIELD = 8

    @property
    def color(self):
        return read_color(self.b.sub(7))

    @color.setter
    def color(self, rgba):
        self.b.set_bytes(7, write_color(rgba))

    @property
    def shape(self) -> Msg | None:
        s = self.b.sub(4)
        return s if s is not None and s.fields else None

    @property
    def translation(self):
        return read_point(self.b.sub(6)) if self.b.sub(6) is not None else (0.0, 0.0)

    def bounds(self):
        r = self.b.sub(2)
        if r is None:
            return None
        (x, y), (w, h) = read_point(r.sub(1)), read_point(r.sub(2))
        dx, dy = self.translation
        return (x + dx, y + dy, w, h)

    def translate(self, dx, dy):
        tx, ty = self.translation
        self.b.set_bytes(6, write_point(tx + dx, ty + dy))


_VIEWS = {"stroke": Stroke, "box": Box, "sticky": Sticky, "line": Line,
          "image": Image, "math": Math, "text": LegacyText, "text2": LegacyText, "textdoc": TextDoc,
          "fillshape": FillShape}
# --- pages and documents -----------------------------------------------------

def _is_meta(m: Msg) -> bool:
    return m.has(8) and m.has(9) and isinstance(m.get(1), (bytes, bytearray)) and len(m.get(1)) == 36


_UUID_RE = re.compile(rb"^[0-9A-Fa-f]{8}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{4}-[0-9A-Fa-f]{12}$")

PT_PER_UNIT = 6 / 11          # GoodNotes page units -> PDF points (455.04 pt = 834.24 units)
UNITS_PER_PT = 11 / 6
STANDARD = (834.24, 1078.825)  # GoodNotes "Standard" paper, portrait
A4 = (595.276 * UNITS_PER_PT, 841.89 * UNITS_PER_PT)
LETTER = (612.0 * UNITS_PER_PT, 792.0 * UNITS_PER_PT)
FLICKS = S.FLICKS_PER_SECOND


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


def _circle_hits(pts, center, radius) -> list[bool]:
    cx, cy = center
    return [math.dist(p, (cx, cy)) <= radius for p in pts]


class Page:
    """One page: its ink/element records plus, when the event log has
    them, its page event (54 pageCreated) and template event (2
    templateCreated, the paper)."""

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
        return read_point(sz) if sz is not None else STANDARD

    @property
    def background(self) -> tuple[str | None, int]:
        """(attachment id of the background PDF/image, 1-based PDF page)."""
        if self.layer is None:
            return None, 1
        return self.layer.str(4), self.layer.int(5, 1) or 1

    @property
    def template(self) -> str | None:
        """Template-library document id of a built-in paper, if any."""
        return self.layer.str(9) if self.layer is not None else None

    # items --------------------------------------------------------------
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

    def find(self, item_id: str) -> Item | None:
        for i in self.items:
            if i.id == item_id:
                return i
        return None

    def append(self, item: Item) -> None:
        self.records += [r for r in (item.meta, item.payload) if r is not None]

    def insert(self, index: int, item: Item) -> None:
        """Insert at z index (0 = bottom)."""
        pos = 0
        for n, i in enumerate(self.items):
            if n == index:
                pos = self.records.index(i.meta if i.meta is not None else i.payload)
                break
        else:
            pos = len(self.records)
        self.records[pos:pos] = [r for r in (item.meta, item.payload) if r is not None]

    def remove(self, item: Item) -> None:
        """Drops the records outright (no tombstone)."""
        self.records = [r for r in self.records if r is not item.meta and r is not item.payload]

    def delete(self, item: Item) -> None:
        """Deletes the way GoodNotes does: the item stays as a tombstone."""
        if item.meta is None:
            self.remove(item)
        else:
            item.deleted = True

    def restore(self, item: Item) -> None:
        item.deleted = False

    def duplicate(self, item: Item, dx: float = 20.0, dy: float = 20.0) -> Item:
        c = item.copy()
        if c.meta is not None:
            c.meta.set_int(9, max((r.int(9) for r in self.records if _is_meta(r)), default=0) + 1)
        self.append(c)
        v = c.typed()
        if hasattr(v, "translate"):
            v.translate(dx, dy)
        return c

    def bring_to_front(self, item: Item) -> None:
        self.remove(item)
        self.append(item)

    def send_to_back(self, item: Item) -> None:
        self.remove(item)
        self.insert(0, item)

    def group(self, item: Item) -> list[Item]:
        """`item` plus everything sharing its element group."""
        gid = item.typed().group if item.kind in _VIEWS else None
        if not gid:
            return [item]
        return [i for i in self.items if not i.deleted and i.kind in _VIEWS and i.typed().group == gid]

    def group_items(self, items: list[Item]) -> str:
        """Groups items (one lasso group) and returns the group id."""
        gid = new_uuid()
        for i in items:
            v = i.typed()
            if isinstance(v, View):
                v.group = gid
        return gid

    def ungroup(self, items: list[Item]) -> None:
        for i in items:
            v = i.typed()
            if isinstance(v, View):
                v.group = None

    def move(self, item: Item, dx: float, dy: float) -> None:
        """Moves an item (and its element group) by (dx, dy)."""
        for i in self.group(item):
            v = i.typed()
            if hasattr(v, "translate"):
                v.translate(dx, dy)

    def erase(self, doc: "Document", center, radius: float, whole: bool = False) -> list[Item]:
        """Eraser tool. Ballpoint ink crossing the circle is split into the
        pieces outside it (new strokes) and the original is tombstoned;
        other ink and whole=True remove the item entirely. Returns the
        items that were affected."""
        hit = []
        for it in self.items:
            if it.deleted or it.kind != "stroke":
                continue
            s = Stroke(it)
            pts = s.points(2.0)
            if not any(_circle_hits(pts, center, radius)):
                continue
            hit.append(it)
            self.delete(it)
            if whole or not is_constant_width(s.template.schema) or s.shape is not None:
                continue
            dx, dy = s.translation
            runs, cur = [], []
            for p in pts:
                if math.dist(p, center) <= radius:
                    if len(cur) > 1:
                        runs.append(cur)
                    cur = []
                else:
                    cur.append(p)
            if len(cur) > 1:
                runs.append(cur)
            for run in runs:
                piece = new_stroke(doc, [("M", run[0])] + [("L", p) for p in run[1:]], s.color,
                                   s.thickness, highlighter=s.highlighter)
                Stroke(piece).dash = s.dash
                self.append(piece)
        return hit

    def __repr__(self) -> str:
        return f"<Page {self.key!r} {self.uuid} {len(self.records)} records>"


def _event_body(e: Msg) -> Msg | None:
    """An event is {1: target id, N: body}; returns body (N varies by type)."""
    for f in e.fields:
        if f[0] != 1 and f[1] == LEN:
            return e._parsed(f)
    return None


def _event_kind(e: Msg) -> int | None:
    for f in e.fields:
        if f[0] != 1 and f[1] == LEN:
            return f[0]
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


def lww(value, counter: int = 0) -> Msg:
    """{1 value, 2 stamp}: the last-writer-wins register GoodNotes keeps
    page and document attributes in."""
    m = Msg()
    if isinstance(value, Msg):
        m.set_bytes(1, value)
    elif isinstance(value, (bytes, str)):
        m.set_bytes(1, value)
    elif isinstance(value, bool):
        if value:
            m.set_int(1, 1)
    elif isinstance(value, int):
        if value:
            m.set_int(1, value)
    elif isinstance(value, float):
        m.set_float(1, value)
    m.set_bytes(2, stamp(counter))
    return m


def pdf_pages(data: bytes) -> list[tuple[float, float]]:
    """(width, height) in points of every page of a PDF. Uses poppler's
    pdfinfo when installed, otherwise a best-effort scan of the file."""
    import shutil
    import subprocess
    import tempfile
    if shutil.which("pdfinfo"):
        with tempfile.NamedTemporaryFile(suffix=".pdf", delete=False) as fh:
            fh.write(data)
            name = fh.name
        try:
            out = subprocess.run(["pdfinfo", "-f", "1", "-l", "100000", name], capture_output=True,
                                 text=True, check=False).stdout
        finally:
            import os
            os.unlink(name)
        pages = []
        for m in re.finditer(r"Page\s+(\d+)\s+size:\s+([\d.]+)\s+x\s+([\d.]+)", out):
            pages.append((float(m.group(2)), float(m.group(3))))
        if pages:
            return pages
    boxes = [tuple(map(float, m.groups())) for m in
             re.finditer(rb"/MediaBox\s*\[\s*([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s+([-\d.]+)\s*\]", data)]
    sizes = [(abs(b[2] - b[0]), abs(b[3] - b[1])) for b in boxes]
    count = len(re.findall(rb"/Type\s*/Page\b", data))
    m = re.search(rb"/Type\s*/Pages\b[^>]*?/Count\s+(\d+)", data)
    if m:
        count = max(count, int(m.group(1)))
    if not sizes:
        sizes = [(612.0, 792.0)]
    return [sizes[min(i, len(sizes) - 1)] for i in range(max(count, 1))]


def paper_pdf(size=STANDARD, kind: str = "plain", spacing: float = 36.0, color=(1, 1, 1),
              line_color=(0.78, 0.78, 0.78), line_width: float = 0.5) -> bytes:
    """A one-page PDF to use as paper: 'plain', 'lined', 'grid' or
    'dotted'. `size` is in page units; `spacing` in points."""
    w, h = size[0] * PT_PER_UNIT, size[1] * PT_PER_UNIT
    ops = [f"{color[0]:.3f} {color[1]:.3f} {color[2]:.3f} rg 0 0 {w:.2f} {h:.2f} re f",
           f"{line_color[0]:.3f} {line_color[1]:.3f} {line_color[2]:.3f} RG "
           f"{line_color[0]:.3f} {line_color[1]:.3f} {line_color[2]:.3f} rg {line_width:.2f} w"]
    ys = [y for y in _frange(h - spacing, spacing / 2, -spacing)]
    xs = [x for x in _frange(spacing, w - spacing / 2, spacing)]
    if kind in ("lined", "grid"):
        ops += [f"0 {y:.2f} m {w:.2f} {y:.2f} l S" for y in ys]
    if kind == "grid":
        ops += [f"{x:.2f} 0 m {x:.2f} {h:.2f} l S" for x in xs]
    if kind == "dotted":
        r = max(line_width, 0.9)
        ops += [f"{x - r:.2f} {y - r:.2f} {2 * r:.2f} {2 * r:.2f} re f" for y in ys for x in xs]
    content = "\n".join(ops).encode()
    objs = [b"<< /Type /Catalog /Pages 2 0 R >>",
            b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {w:.2f} {h:.2f}] /Contents 4 0 R >>".encode(),
            b"<< /Length " + str(len(content)).encode() + b" >>\nstream\n" + content + b"\nendstream"]
    out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
    offsets = []
    for i, o in enumerate(objs, 1):
        offsets.append(len(out))
        out += f"{i} 0 obj\n".encode() + o + b"\nendobj\n"
    xref = len(out)
    out += f"xref\n0 {len(objs) + 1}\n0000000000 65535 f \n".encode()
    for off in offsets:
        out += f"{off:010d} 00000 n \n".encode()
    out += f"trailer\n<< /Size {len(objs) + 1} /Root 1 0 R >>\nstartxref\n{xref}\n%%EOF\n".encode()
    return bytes(out)


def _frange(a, b, step):
    x = a
    while (step > 0 and x <= b) or (step < 0 and x >= b):
        yield x
        x += step


class Document:
    """A whole .goodnotes file. Untouched zip entries are written back
    byte-for-byte; pages and the event log are re-encoded from their Msg
    trees (identical bytes unless edited).

    `pages` is in **document order**: sorted by each page event's key.
    index.notes.pb is creation order, not page order (they differ in 53 of
    206 library files; confirmed against the GoodNotes app)."""

    def __init__(self, path):
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
        # notesIndexUpdated (104) names the notes group of pages whose ink
        # file is not at page id + 1
        for e in self.events:
            b = e.sub(104)
            if b is not None and b.str(3) and b.str(1):
                for pe in page_events.values():
                    if pe.str(2) == b.str(1):
                        page_events[b.str(3).upper()] = pe
        self.pages = []
        for rec in index:
            uid = rec.str(1)
            data = self.files.get(f"notes/{uid}", b"")
            ev = page_events.get(uid)
            layer = layers.get(ev.path(3).str(1)) if ev is not None and ev.sub(3) is not None else None
            self.pages.append(Page(uid, [Msg.parse(m) for m in iter_delimited(data)], ev, layer))
        if all(p.event is not None for p in self.pages):
            self.pages.sort(key=lambda p: p.key)

    @classmethod
    def new(cls, title: str = "Untitled", size=STANDARD, paper: str | bytes = "plain",
            pages: int = 1, spacing: float = 36.0) -> "Document":
        """A fresh document with `pages` pages of the given paper: 'plain',
        'lined', 'grid', 'dotted', or the bytes of a one-page PDF."""
        import io
        did, tid, aid = new_uuid(), new_uuid(), new_uuid()
        pdf = paper if isinstance(paper, (bytes, bytearray)) else paper_pdf(size, paper, spacing)
        if isinstance(paper, (bytes, bytearray)):
            pts = pdf_pages(pdf)[0]
            size = (pts[0] * UNITS_PER_PT, pts[1] * UNITS_PER_PT)
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as z:
            z.writestr("schema.pb", Msg([[1, VARINT, S.SCHEMA_VERSION]]).encode())  # {1: version}
            z.writestr("document.info.pb", b"")
            z.writestr("index.notes.pb", b"")
            z.writestr("index.attachments.pb", b"")
            z.writestr("index.events.pb", b"")
        buf.seek(0)
        doc = cls(buf)
        device = doc.device
        now = time.time() * 1000

        def tail(body, ids=(13, 14, 15), version_field=15):
            body.set_double(10, now)
            body.set_bytes(11, new_uuid())
            body.set_bytes(12, b"")
            body.set_int(ids[0], device)
            body.set_int(ids[1], int(now))
            body.set_int(version_field, S.SCHEMA_VERSION)
            return body

        # Event bodies mirror a GoodNotes 6 macOS export field for field.
        folder = new_uuid()
        created = Msg()
        created.set_bytes(1, did)
        created.set_bytes(2, lww(title, 1))
        created.set_bytes(3, lww(folder, 1))
        created.set_bytes(6, lww("P" if size[0] <= size[1] else "L", 1))
        created.set_bytes(7, lww(folder, 1))
        created.set_bytes(9, "auto")
        tail(created, version_field=20)
        created.set_bytes(17, b"")
        created.set_bytes(18, b"")
        created.set_bytes(19, Msg([[2, LEN, stamp(1)]]))
        created.remove(12)
        created.fields.sort(key=lambda f: f[0])
        doc.events.append(Msg([[1, LEN, did.encode()], [30, LEN, created]]))
        doc.add_attachment(pdf, aid)
        tpl = Msg()
        tpl.set_bytes(1, did)
        tpl.set_bytes(2, tid)
        tpl.set_bytes(4, aid)
        tpl.set_int(5, 1)
        tpl.set_int(6, 1)
        tpl.set_double(7, spacing * UNITS_PER_PT if isinstance(paper, str) and paper == "lined" else 29.33333396911621)
        tpl.set_bytes(8, write_point(*size))
        tpl.set_double(10, now)
        tpl.set_bytes(11, new_uuid())
        lh = Msg()
        lh.set_double(1, tpl.float(7))
        lh.set_bytes(2, stamp(1))
        tpl.set_bytes(12, lh)
        tpl.set_bytes(13, lww(True, 1))
        tpl.set_int(15, device)
        tpl.set_int(16, int(now))
        tpl.set_bytes(17, lww(1, 1))
        # textAreas: the typing area GoodNotes uses for full-page text (margins as on standard paper)
        mx, my = 44.0 * size[0] / STANDARD[0], 58.6667 * size[1] / STANDARD[1]
        area = Msg([[1, LEN, Msg([[1, LEN, write_point(mx, my)], [2, LEN, write_point(size[0] - 2 * mx, size[1] - 2 * my)]])]])
        area.set_float(2, 28.416668)
        area.set_float(3, 0.9166667)
        area.set_int(5, 1)
        tpl.set_bytes(18, area)
        tpl.set_bytes(19, Msg([[2, LEN, stamp(1)]]))
        tpl.set_int(21, S.SCHEMA_VERSION)
        doc.events.append(Msg([[1, LEN, tid.encode()], [2, LEN, tpl]]))
        key = None
        for _ in range(pages):
            pid = new_uuid()
            key = key_between(key, None)
            pe = Msg()
            pe.set_bytes(1, did)
            pe.set_bytes(2, pid)
            pe.set_bytes(3, lww(tid, 1))
            pe.set_bytes(4, lww(key, 1))
            tail(pe)
            # canvasTemplate: canvas colors (dark/light) GoodNotes stores per page
            canvas = Msg([[2, LEN, Msg([[1, LEN, write_color((0.8667, 0.8667, 0.8667, 1))],
                                        [2, LEN, write_color((1, 1, 1, 1))]])]])
            pe.set_bytes(17, Msg([[1, LEN, canvas], [2, LEN, stamp(1)]]))
            doc.events.append(Msg([[1, LEN, pid.encode()], [54, LEN, pe]]))
            nid = uuid_plus(pid)
            doc._register_notes(nid, did)
            doc.pages.append(Page(nid, [], pe, tpl))
        cur = Msg()
        cur.set_bytes(1, did)
        cur.set_bytes(2, doc.pages[0].page_id)
        cur.set_bytes(3, f"PagingViewServiceUpdater:{new_uuid()}")
        doc.events.append(Msg([[1, LEN, did.encode()], [10, LEN, tail(cur)]]))
        return doc

    def _register_notes(self, nid: str, did: str) -> None:
        """index.notes.pb entry, empty ink file and the index/link events
        (105, 104, 102) GoodNotes writes for a page's notes group."""
        now = time.time() * 1000
        idx = Msg()
        idx.set_bytes(1, nid)
        idx.set_bytes(2, f"notes/{nid}")
        self.files["index.notes.pb"] = self.files.get("index.notes.pb", b"") + write_delimited([idx])
        self.files[f"notes/{nid}"] = b""
        store = Msg()
        store.set_int(1, 1)
        store.set_bytes(2, did)
        store.set_bytes(4, nid)
        store.set_bytes(6, "auto")
        self._event(105, nid, store)
        ni = Msg()
        ni.set_bytes(1, did)
        ni.set_bytes(2, nid)
        ni.set_double(10, now)
        ni.set_bytes(11, new_uuid())
        ni.set_bytes(13, "auto")
        ni.set_int(14, 1)
        ni.set_bytes(15, new_uuid())
        ni.set_int(17, self.device)
        ni.set_int(18, int(now))
        ni.set_int(19, S.SCHEMA_VERSION)
        self.events.append(Msg([[1, LEN, nid.encode()], [104, LEN, ni]]))
        upd = Msg()
        upd.set_bytes(1, nid)
        self._event(102, nid, upd).sub(102).set_bytes(16, did)

    @classmethod
    def from_pdf(cls, data: bytes, title: str = "Imported") -> "Document":
        """A document whose pages are the pages of a PDF (File > Import)."""
        sizes = pdf_pages(data)
        first = (sizes[0][0] * UNITS_PER_PT, sizes[0][1] * UNITS_PER_PT)
        doc = cls.new(title, first, data[:0] + paper_pdf(first, "plain"), pages=1)
        aid = doc.add_attachment(data)
        for n, (w, h) in enumerate(sizes):
            page = doc.pages[0] if n == 0 else doc.add_page(doc.pages[0])
            layer = page.layer
            layer.set_bytes(4, aid)
            layer.set_int(5, n + 1)
            layer.set_bytes(8, write_point(w * UNITS_PER_PT, h * UNITS_PER_PT))
        return doc

    # attachments
    @property
    def attachments(self) -> dict[str, bytes]:
        return {n.split("/", 1)[1]: d for n, d in self.files.items() if n.startswith("attachments/")}

    @property
    def doc_id(self) -> str | None:
        for e in self.events:
            if e.has(30):
                return e.str(1)
        for e in self.events:
            b = e.sub(54)
            if b is not None and b.str(1):
                return b.str(1)
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
            d = e.sub(31) or e.sub(30)
            if d is not None and d.sub(2) is not None:
                return d.path(2).str(1)
        return None

    @title.setter
    def title(self, name: str) -> None:
        for e in self.events:
            d = e.sub(30)
            if d is not None:
                d.sub(2, create=True).set_bytes(1, name)
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, lww(name))
        self._event(31, self.doc_id, body)

    @property
    def favourite(self) -> bool:
        found = False
        for e in self.events:
            b = e.sub(34)
            if b is not None:
                found = b.path(2).int(1) == 1 if b.sub(2) is not None else b.int(2) == 1
        return found

    @favourite.setter
    def favourite(self, on: bool) -> None:
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, lww(bool(on)))
        self._event(34, self.doc_id, body)

    def add_attachment(self, data: bytes, attachment_id: str | None = None) -> str:
        """Stores raw bytes and registers them the three ways GoodNotes
        requires (zip entry, index.attachments.pb, attachmentCreated event)."""
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
        pages = len(pdf_pages(data)) if data[:4] == b"%PDF" else 1
        body.set_bytes(12, Msg([[1, VARINT, 1], [2, VARINT, pages]]))  # pageRange {firstPage, numberOfPages}
        body.set_int(14, self.device)
        body.set_int(15, int(now))
        body.set_int(16, S.SCHEMA_VERSION)
        ev = Msg()
        ev.set_bytes(1, aid)
        ev.set_bytes(6, body)
        self.events.append(ev)
        store = Msg()  # indexStoreUpdated for the attachment (search index)
        store.set_bytes(2, self.doc_id or new_uuid())
        store.set_bytes(3, aid)
        self._event(105, aid, store)
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
        structural = (2, 10, 54, 102, 104, 105)  # template, current page, page, notes updated/index/store
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
        if any(c.has(104) or c.has(105) for c in copies):
            idx = Msg()
            idx.set_bytes(1, notes_id)
            idx.set_bytes(2, f"notes/{notes_id}")
            self.files["index.notes.pb"] = self.files.get("index.notes.pb", b"") + write_delimited([idx])
            self.files[f"notes/{notes_id}"] = b""
        else:
            self._register_notes(notes_id, self.doc_id or like.event.str(1))
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

    def duplicate_page(self, page: Page, index: int | None = None) -> Page:
        new = self.add_page(page, len(self.pages) if index is None else index)
        for it in page.items:
            if not it.deleted:
                c = it.copy()
                if c.meta is not None:
                    c.meta.set_int(9, self.next_sequence())
                new.append(c)
        return new

    def set_background(self, page: Page, data: bytes, pdf_page: int = 1,
                       size: tuple[float, float] | None = None, fit: bool = True) -> str:
        """Replaces a page's paper with a PDF (or image) attachment.
        `size` resizes the page; with fit=True a PDF's own page size is
        used, otherwise the page keeps its size."""
        aid = self.add_attachment(data)
        layer = page.layer
        layer.set_bytes(4, aid)
        layer.set_int(5, pdf_page)
        layer.remove(9)  # template library id: no longer a built-in paper
        if size is None and fit and data[:4] == b"%PDF":
            sizes = pdf_pages(data)
            w, h = sizes[min(pdf_page - 1, len(sizes) - 1)]
            size = (w * UNITS_PER_PT, h * UNITS_PER_PT)
        if size is not None:
            layer.set_bytes(8, write_point(*size))
        return aid

    def import_pdf(self, data: bytes, index: int | None = None) -> list[Page]:
        """Appends (or inserts at `index`) one page per PDF page."""
        aid = self.add_attachment(data)
        out = []
        for n, (w, h) in enumerate(pdf_pages(data)):
            page = self.add_page(index=None if index is None else index + n)
            page.layer.set_bytes(4, aid)
            page.layer.set_int(5, n + 1)
            page.layer.remove(9)
            page.layer.set_bytes(8, write_point(w * UNITS_PER_PT, h * UNITS_PER_PT))
            out.append(page)
        return out

    def import_image(self, data: bytes, index: int | None = None, margin: float = 40.0) -> Page:
        """A new page with the image placed on it, scaled to fit."""
        page = self.add_page(index=index)
        w, h = page.size
        iw, ih = image_size(data) or (w - 2 * margin, h - 2 * margin)
        k = min((w - 2 * margin) / iw, (h - 2 * margin) / ih)
        page.append(new_image(self, data, (w / 2, h / 2), (iw * k, ih * k)))
        return page

    # page attributes and outline (each is its own event; the last one wins)
    def _event(self, kind: int, target: str, body: Msg, version_field: int = 15) -> Msg:
        now = time.time() * 1000
        body.set_double(10, now)
        body.set_bytes(11, new_uuid())
        body.set_int(13, self.device)
        body.set_int(14, int(now) + random.getrandbits(8))
        body.set_int(version_field, S.SCHEMA_VERSION)
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
        body.set_bytes(3, lww(bool(on), 1))
        self._event(57, page.page_id, body)

    def rotation(self, page: Page) -> int:
        """Page rotation in degrees clockwise (stored in quarter turns)."""
        b = self._page_attr(63, page)
        return 90 * (b.path(3).int(1) if b is not None else 0)

    def set_rotation(self, page: Page, degrees: int) -> None:
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, page.page_id)
        body.set_bytes(3, lww((degrees % 360) // 90, 1))
        self._event(63, page.page_id, body, version_field=16)

    def labels(self, page: Page) -> list[str]:
        """Page labels (pageLabelsDidSet)."""
        b = self._page_attr(64, page)
        if b is None or b.sub(3) is None:
            return []
        return [v.decode("utf-8", "replace") if isinstance(v, (bytes, bytearray)) else v.encode().decode()
                for v in b.path(3).all(1) if isinstance(v, (bytes, bytearray, Msg))]

    def set_labels(self, page: Page, labels: list[str]) -> None:
        body = Msg()
        body.set_bytes(1, self.doc_id)
        body.set_bytes(2, page.page_id)
        v = Msg()
        for l in labels:
            v.add(1, LEN, l.encode())
        v.set_bytes(2, stamp(1))
        body.set_bytes(3, v)
        self._event(64, page.page_id, body)

    def read(self, page: Page) -> bool:
        found = False
        for e in self.events:
            b = e.sub(110)
            if b is not None and b.str(1) == page.page_id:
                found = (b.path(3).int(1) if b.sub(3) is not None else b.int(3)) == 1
        return found

    def set_read(self, page: Page, on: bool = True) -> None:
        body = Msg()
        body.set_bytes(1, page.page_id)
        body.set_bytes(2, self.doc_id)
        body.set_bytes(3, lww(bool(on), 1))
        self._event(110, page.page_id, body)

    # outline ------------------------------------------------------------
    def _outline_entries(self) -> dict[str, Msg]:
        entries = {}
        for e in self.events:
            b = e.sub(65) if e.has(65) else None
            if b is not None:
                entries[b.str(2)] = b
        return {k: b for k, b in entries.items() if not (b.sub(3) is not None and b.path(3).int(1) == 1)}

    @property
    def outline(self) -> list[tuple[str, Page | None]]:
        """(title, page) entries in outline order (nested entries
        flattened; see `outline_tree`)."""
        return [(t, p) for t, p, _, _ in self.outline_tree()]

    def outline_tree(self) -> list[tuple[str, Page | None, int, str]]:
        """(title, page, depth, entry id) in outline order."""
        entries = self._outline_entries()
        by_id = {p.page_id: p for p in self.pages}
        children = {}
        for oid, b in entries.items():
            parent = b.path(6).str(1) if b.sub(6) is not None else None
            parent = parent if parent in entries else None
            children.setdefault(parent, []).append((b.path(4).str(1, "") if b.sub(4) else "", oid, b))
        out = []

        def walk(parent, depth):
            for _, oid, b in sorted(children.get(parent, []), key=lambda t: t[0]):
                out.append((b.path(5).str(1, "") if b.sub(5) else "", by_id.get(b.str(1)), depth, oid))
                walk(oid, depth + 1)
        walk(None, 0)
        return out

    def add_outline(self, title: str, page: Page, parent: str | None = None) -> str:
        """Adds an outline entry (under `parent` entry id, if given) and
        returns its id."""
        last = None
        for b in self._outline_entries().values():
            if b.sub(4) is not None:
                k = b.path(4).str(1, "")
                last = k if last is None or k > last else last
        oid = new_uuid()
        body = Msg()
        body.set_bytes(1, page.page_id)
        body.set_bytes(2, oid)
        body.set_bytes(3, Msg([[2, LEN, stamp(0)]]))
        body.set_bytes(4, Msg([[1, LEN, key_between(last, None).encode()], [2, LEN, stamp(0)]]))
        body.set_bytes(5, Msg([[1, LEN, title.encode()], [2, LEN, stamp(0)]]))
        body.set_bytes(6, Msg([[1, LEN, (parent or "").encode()], [2, LEN, stamp(0)]]))
        body.set_bytes(15, Msg([[1, LEN, b""], [2, LEN, stamp(0)]]))
        body.set_bytes(17, self.doc_id)
        ev = self._event(65, page.page_id, body, version_field=16)
        ev.sub(65).set_int(16, 26)  # outline records carry 26, not 24
        return oid

    def remove_outline(self, entry_id: str) -> None:
        b = self._outline_entries().get(entry_id)
        if b is None:
            return
        body = b.copy()
        body.set_bytes(3, Msg([[1, VARINT, 1], [2, LEN, stamp(1)]]))
        for n in (10, 11, 13, 14):
            body.remove(n)
        ev = self._event(65, b.str(1), body, version_field=16)
        ev.sub(65).set_int(16, 26)

    # audio notes --------------------------------------------------------
    @property
    def audio_notes(self) -> list[dict]:
        """[{id, attachment, duration (s), name, pages: [(seconds, Page)]}]."""
        notes, names = {}, {}
        by_id = {p.page_id: p for p in self.pages}
        for e in self.events:
            b = e.sub(160)
            if b is not None:
                refs = []
                r = b.sub(5)
                for v in (r.subs(1) if r is not None else []):
                    refs.append((v.int(1) / FLICKS, by_id.get(v.str(2))))
                notes[b.str(1)] = {"id": b.str(1), "attachment": b.str(2), "duration": b.int(4) / FLICKS,
                                   "pages": refs, "name": None}
            b = e.sub(161)
            if b is not None and b.str(1) in notes:
                refs = []
                r = b.sub(3)
                r = r.sub(1) if r is not None and r.sub(1) is not None and not r.subs(1) else r
                for v in (r.subs(1) if r is not None else []):
                    refs.append((v.int(1) / FLICKS, by_id.get(v.str(2))))
                notes[b.str(1)]["pages"] = refs
            b = e.sub(164)
            if b is not None:
                names[b.str(1)] = b.path(3).str(1) if b.sub(3) is not None else b.str(3)
            b = e.sub(163)
            if b is not None:
                notes.pop(b.str(1), None)
        for k, v in names.items():
            if k in notes:
                notes[k]["name"] = v
        return list(notes.values())

    def add_audio_note(self, data: bytes, duration: float, page: Page | None = None,
                       name: str | None = None, offsets: list[float] | None = None) -> str:
        """Attaches a recording (m4a/mp3/wav bytes) to the document. `page`
        and `offsets` (seconds into the recording) link it to the page the
        way GoodNotes does for ink written while recording."""
        aid = self.add_attachment(data)
        audio_id = new_uuid()
        body = Msg()
        body.set_bytes(1, audio_id)
        body.set_bytes(2, aid)
        body.set_bytes(3, self.doc_id)
        body.set_int(4, int(duration * FLICKS))
        refs = Msg()
        for t in (offsets if offsets is not None else ([0.0] if page is not None else [])):
            v = Msg()
            v.set_int(1, int(t * FLICKS))
            v.set_bytes(2, page.page_id)
            refs.add(1, LEN, v)
        body.set_bytes(5, refs)
        body.set_bytes(6, lww(key_between(None, None)))
        self._event(160, audio_id, body)
        if name:
            nb = Msg()
            nb.set_bytes(1, audio_id)
            nb.set_bytes(2, self.doc_id)
            nb.set_bytes(3, lww(name))
            self._event(164, audio_id, nb)
        return audio_id

    def transcripts(self) -> list[dict]:
        """Audio transcriptions: [{audio, start, end, locale, text}]."""
        out = {}
        for e in self.events:
            b = e.sub(210)
            if b is not None:
                out[b.str(1)] = {"audio": b.str(3), "start": b.int(4) / FLICKS, "end": b.int(5) / FLICKS,
                                 "locale": b.str(6), "text": b.str(7, "")}
            b = e.sub(211)
            if b is not None and b.str(1) in out:
                out[b.str(1)]["text"] = b.str(3, "")
        return list(out.values())

    # comments -----------------------------------------------------------
    def comments(self) -> list[dict]:
        """Comment threads: [{id, page, position, resolved, comments:
        [{id, author, text}]}]."""
        threads, by_id = {}, {p.page_id: p for p in self.pages}
        for e in self.events:
            b = e.sub(130)
            if b is not None:
                anchor = b.sub(4)
                if anchor is not None and anchor.sub(1) is not None and anchor.sub(1).has(1):
                    anchor = anchor.sub(1)  # written as an LWW register {1 anchor, 2 stamp}
                page = by_id.get(anchor.str(1)) if anchor is not None else None
                pos = read_point(anchor.path(3, 5)) if anchor is not None and anchor.path(3, 5) is not None else (0.0, 0.0)
                threads[b.str(1)] = {"id": b.str(1), "page": page, "position": pos, "resolved": False, "comments": []}
            b = e.sub(133)
            if b is not None and b.str(2) in threads:
                content = b.sub(4)
                text = content.str(1, "") if content is not None else b.str(4, "")
                threads[b.str(2)]["comments"].append({"id": b.str(1), "author": b.str(7), "text": text})
            b = e.sub(134)
            if b is not None and b.str(2) in threads:
                for c in threads[b.str(2)]["comments"]:
                    if c["id"] == b.str(1):
                        content = b.sub(4)
                        c["text"] = content.str(1, "") if content is not None else b.str(4, "")
            b = e.sub(135)
            if b is not None and b.str(2) in threads:
                threads[b.str(2)]["comments"] = [c for c in threads[b.str(2)]["comments"] if c["id"] != b.str(1)]
            b = e.sub(132)
            if b is not None and b.str(1) in threads:
                threads[b.str(1)]["resolved"] = (b.path(3).int(1) if b.sub(3) is not None else b.int(3)) == 1
            b = e.sub(137)
            if b is not None:
                threads.pop(b.str(1), None)
        return list(threads.values())

    def add_comment(self, page: Page, position, text: str, author: str = "",
                    author_id: str = "", thread: str | None = None,
                    color=(1.0, 0.87, 0.3, 1.0)) -> str:
        """Starts a comment thread pinned at `position` on `page` (or adds
        to `thread`). Returns the thread id."""
        cid = new_uuid()
        if thread is None:
            thread = new_uuid()
            tb = Msg()
            tb.set_bytes(1, thread)
            tb.set_bytes(2, self.doc_id)
            tb.set_bytes(3, lww(key_between(None, None)))
            anchor = Msg()
            anchor.set_bytes(1, page.page_id)
            sticky = Msg()
            sticky.set_int(1, 1)
            sticky.set_bytes(2, write_color(color))
            sticky.set_bytes(3, write_point(256.0, 256.0))
            sticky.set_bytes(5, write_point(*position))
            sticky.set_int(6, 1)
            anchor.set_bytes(3, sticky)
            tb.set_bytes(4, lww(anchor))
            tb.set_bytes(5, cid)
            self._event(130, thread, tb, version_field=16)
        cb = Msg()
        cb.set_bytes(1, cid)
        cb.set_bytes(2, thread)
        cb.set_bytes(3, self.doc_id)
        cb.set_bytes(4, lww(text))
        cb.set_bytes(5, lww(key_between(None, None)))
        cb.set_bytes(6, author_id)
        cb.set_bytes(7, author)
        self._event(133, cid, cb)
        return thread

    def resolve_comment(self, thread: str, resolved: bool = True) -> None:
        body = Msg()
        body.set_bytes(1, thread)
        body.set_bytes(2, self.doc_id)
        body.set_bytes(3, lww(bool(resolved), 1))
        self._event(132, thread, body)

    def save(self, path) -> None:
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


def image_size(data: bytes) -> tuple[int, int] | None:
    """Pixel size of a PNG, JPEG or GIF."""
    if data[:8] == b"\x89PNG\r\n\x1a\n" and data[12:16] == b"IHDR":
        return struct.unpack(">II", data[16:24])
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return struct.unpack("<HH", data[6:10])
    if data[:3] == b"\xff\xd8\xff":
        i = 2
        while i + 9 < len(data):
            if data[i] != 0xFF:
                i += 1
                continue
            marker = data[i + 1]
            if marker in (0xD8, 0x01) or 0xD0 <= marker <= 0xD7:
                i += 2
                continue
            seg = struct.unpack(">H", data[i + 2:i + 4])[0]
            if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
                h, w = struct.unpack(">HH", data[i + 5:i + 9])
                return w, h
            i += 2 + seg
    return None
# --- building new items ------------------------------------------------------

def _meta(doc: Document, item_id: str, st: Msg, version: int = S.SCHEMA_VERSION,
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


def _stroke_shell(doc: Document, color, pen: int = 0, highlighter: bool = False,
                  dash=None) -> tuple[Item, Msg]:
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
    b.set_int(21, S.SCHEMA_VERSION)
    item = Item(_meta(doc, sid, st), Msg([[7, LEN, b]]))
    if dash:
        Stroke(item).dash = dash
        b.fields.sort(key=lambda f: f[0])
    return item, b


def new_stroke(doc: Document, commands, color=(0, 0, 0, 1), thickness: float = 1.5,
               highlighter: bool = False, dash=None) -> Item:
    """A ballpoint stroke from 'M'/'L'/'Q' commands in page units.
    `dash` = [on, off, ...] in multiples of the width (or ([..], cap))."""
    item, b = _stroke_shell(doc, color, highlighter=highlighter, dash=dash)
    Stroke(item).set_path(commands, thickness)
    return item


def _sample(commands, step: float = 3.0) -> list[tuple[float, float]]:
    """Flattens M/L/Q commands to points about `step` apart (one subpath)."""
    pts, cur = [], None
    for c in commands:
        if c[0] == "M":
            cur = c[1]
            pts.append(cur)
            continue
        ctrl, end = (((cur[0] + c[1][0]) / 2, (cur[1] + c[1][1]) / 2), c[1]) if c[0] == "L" else (c[1], c[2])
        pts += _quad_points(cur, ctrl, end, step)
        cur = end
    out = []
    for p in pts:  # drop repeats: a zero-length segment has no normal
        if not out or math.dist(out[-1], p) > 1e-4:
            out.append(p)
    return out


def _variable_width_blob(pts, half, dynamic: bool) -> Template:
    """Centerline + per-segment outline for a fountain (VARIABLE_WIDTH) or
    brush (DYNAMIC_WIDTH) stroke.

    GoodNotes stores the outline it will fill, one closed subpath per
    quad-curve segment of the centerline (the protobuf model keeps each
    segment's `outline` on its quadCurveTo command, and the deserializer
    checks the per-segment command counts; every real stroke inspected has
    exactly as many counts as quad segments). Each subpath is written the
    way GoodNotes writes it: start on one side at the start cap, sweep the
    cap (clockwise flag, decreasing angle), run the far side as cubics,
    sweep the end cap, and return along the near side. Overlapping stadiums
    fill to the full stroke."""
    n = len(pts)
    # command codes differ between the two schemas (schema.VW_COMMAND / DW_COMMAND)
    if dynamic:
        K_MOVE, K_QUAD, O_MOVE, O_CUBIC, O_ARC = 2, 3, 2, 4, 5
    else:
        K_MOVE, K_QUAD, O_MOVE, O_CUBIC, O_ARC = 0, 1, 0, 2, 3
    kinds, moves, quads = [K_MOVE], [u32(pts[0][0]), u32(pts[0][1]), u32(half[0])], []
    counts, types, o_moves, o_quads, cubics, arcs, cw = [], [], [], [], [], [], []

    def cubic(a, b):
        types.append(O_CUBIC)
        cubics.extend(u32(v) for v in (a[0] + (b[0] - a[0]) / 3, a[1] + (b[1] - a[1]) / 3,
                                       a[0] + 2 * (b[0] - a[0]) / 3, a[1] + 2 * (b[1] - a[1]) / 3, b[0], b[1]))

    def cap(c, r, frm):
        a0 = math.atan2(frm[1] - c[1], frm[0] - c[0])
        types.append(O_ARC)
        arcs.extend(u32(v) for v in (c[0], c[1], r, a0, a0 - math.pi))
        cw.append(1)  # clockwise = decreasing angle, as GoodNotes writes every cap

    for i in range(1, n):
        (x0, y0), (x1, y1) = pts[i - 1], pts[i]
        kinds.append(K_QUAD)
        quads += [u32((x0 + x1) / 2), u32((y0 + y1) / 2), u32((half[i - 1] + half[i]) / 2),
                  u32(x1), u32(y1), u32(half[i])]
        dx, dy = x1 - x0, y1 - y0
        L = math.hypot(dx, dy) or 1.0
        nx, ny = -dy / L, dx / L
        r0, r1 = max(half[i - 1], 0.05), max(half[i], 0.05)
        left0, right0 = (x0 + nx * r0, y0 + ny * r0), (x0 - nx * r0, y0 - ny * r0)
        left1, right1 = (x1 + nx * r1, y1 + ny * r1), (x1 - nx * r1, y1 - ny * r1)
        before = len(types)
        # decreasing angle from -n sweeps through -d (behind the start) to +n,
        # and from +n through +d (past the end) back to -n
        types.append(O_MOVE)
        o_moves += [u32(right0[0]), u32(right0[1])]
        cap(pts[i - 1], r0, right0)  # start cap: -n -> +n behind the segment
        cubic(left0, left1)
        cap(pts[i], r1, left1)       # end cap: +n -> -n past the segment
        cubic(right1, right0)
        counts.append(len(types) - before)
    values = [2, kinds, moves, quads, counts, types, o_moves, o_quads, cubics, arcs, cw]
    if dynamic:
        values.insert(1, u32(2 * max(half)))
    return Template(DW_SCHEMA if dynamic else VW_SCHEMA, values)


def _half_widths(pts, width, pressure):
    n = len(pts)
    return [width / 2 * (pressure(i / max(n - 1, 1)) if pressure else 1.0) for i in range(n)]


def new_fountain_stroke(doc: Document, commands, color=(0, 0, 0, 1), width: float = 3.0,
                        pressure=None, tape: bool = False, highlighter: bool = False) -> Item:
    """Variable-width ink (fountain pen; tape=True draws washi tape).
    `pressure` maps 0..1 along the stroke to a width factor (default:
    constant)."""
    pts = _sample(commands)
    if len(pts) < 2:
        pts = pts + [(pts[0][0] + 0.01, pts[0][1])]
    item, b = _stroke_shell(doc, color, pen=1, highlighter=highlighter)
    if tape:
        b.set_bytes(20, Msg([[1, LEN, b""]]))
    b.set_bytes(2, blob_compress(_variable_width_blob(pts, _half_widths(pts, width, pressure), False).encode()))
    return item


def new_brush_stroke(doc: Document, commands, color=(0, 0, 0, 1), width: float = 4.0,
                     pressure=None, highlighter: bool = False) -> Item:
    """Brush pen (DYNAMIC_WIDTH) ink; `pressure` as new_fountain_stroke."""
    pts = _sample(commands)
    if len(pts) < 2:
        pts = pts + [(pts[0][0] + 0.01, pts[0][1])]
    item, b = _stroke_shell(doc, color, pen=4, highlighter=highlighter)
    b.set_bytes(2, blob_compress(_variable_width_blob(pts, _half_widths(pts, width, pressure), True).encode()))
    return item


def new_pencil_stroke(doc: Document, commands, color=(0.118, 0.106, 0.106, 1), width: float = 3.1,
                      pressure=None, azimuth: float = 0.5236, altitude: float = 1.0472) -> Item:
    """Textured pencil ink: per point (x, y, azimuth, altitude, force), as
    GoodNotes writes it for a mouse/trackpad stroke (force 0, pencil held
    at 30 degrees azimuth, 60 degrees altitude). `pressure` maps 0..1 along
    the stroke to force."""
    pts = _sample(commands, 1.5)  # GoodNotes stamps texture per segment: real strokes are this dense
    n = len(pts)
    force = [pressure(i / max(n - 1, 1)) if pressure else 0.0 for i in range(n)]

    def p5(i):
        return [u32(pts[i][0]), u32(pts[i][1]), u32(azimuth), u32(altitude), u32(force[i])]
    kinds, moves, quads = [0], [p5(0)], []
    for i in range(1, n):
        kinds.append(1)
        mid = [u32((pts[i - 1][0] + pts[i][0]) / 2), u32((pts[i - 1][1] + pts[i][1]) / 2),
               u32(azimuth), u32(altitude), u32((force[i - 1] + force[i]) / 2)]
        quads.append([random.getrandbits(32)] + mid + p5(i))  # first value: texture seed
    item, b = _stroke_shell(doc, color, pen=5)
    t = Template(PENCIL_SCHEMA, [1, u32(width / 2), kinds, moves, quads, [], [], [], [], []])
    b.set_bytes(2, blob_compress(t.encode()))
    return item


def new_shape_stroke(doc: Document, kind: str, color=(0, 0, 0, 1), width: float = 4.68,
                     points=None, center=None, size=None, angle: float = 0.0,
                     highlighter: bool = False) -> Item:
    """Shape-tool ink: kind 'line'/'polyline' (points), 'rect' (center,
    size), 'ellipse' (center, radii=size, angle)."""
    item, b = _stroke_shell(doc, color, highlighter=highlighter)
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


def new_fill_shape(doc: Document, kind: str, color=(0.118, 0.106, 0.106, 0.1), points=None,
                   center=None, size=None, angle: float = 0.0) -> Item:
    """A filled shape (shape tool with fill): kind 'rect' (center, size),
    'ellipse' (center, radii, angle) or 'polygon' (points)."""
    fid, st = new_uuid(), stamp(0)
    b = Msg()
    b.set_bytes(1, fid)
    d = Msg()
    if kind == "rect":
        d.set_bytes(3, Msg([[1, LEN, write_point(*center)], [2, LEN, write_point(*size)]]))
        x, y, w, h = center[0] - size[0] / 2, center[1] - size[1] / 2, size[0], size[1]
    elif kind == "ellipse":
        e = Msg([[1, LEN, write_point(*center)], [2, LEN, write_point(*size)]])
        if angle:
            e.set_float(3, angle)
        d.set_bytes(4, e)
        r = max(size)
        x, y, w, h = center[0] - r, center[1] - r, 2 * r, 2 * r
    elif kind == "polygon":
        d.set_bytes(1, Msg([[1, LEN, write_point(*p)] for p in points]))
        xs, ys = [p[0] for p in points], [p[1] for p in points]
        x, y, w, h = min(xs), min(ys), max(xs) - min(xs), max(ys) - min(ys)
    else:
        raise ValueError(kind)
    b.set_bytes(2, Msg([[1, LEN, write_point(x, y)], [2, LEN, write_point(w, h)]]))
    b.set_bytes(3, Msg([[1, LEN, stamp(6)]]))
    b.set_bytes(4, d)
    b.set_bytes(5, new_uuid())
    b.set_bytes(6, b"")
    b.set_bytes(7, write_color(color))
    b.set_bytes(15, st.copy())
    b.set_int(18, S.SCHEMA_VERSION)
    return Item(_meta(doc, fid, st), Msg([[9, LEN, b]]))


def new_tape(doc: Document, commands, color=(0.98, 0.85, 0.4, 0.85), width: float = 24.0) -> Item:
    """Washi tape (a wide fountain stroke with the tape flag)."""
    return new_fountain_stroke(doc, commands, color, width, tape=True)


_RICH_ATTR_FALLBACK = S.INHERIT


def rich_text(text: str, color=(0.118, 0.106, 0.106, 1), font: str | None = None,
              size: float | None = None, bold: bool = False, italic: bool = False,
              link: str | None = None, underline: bool = False, strike: bool = False,
              highlight=None, align: str | None = None, list_style: str | None = None,
              heading: int | None = None, line_height: float | None = None,
              indent: int | None = None) -> RichText:
    """One run of formatted text. `link` is a URL, or page_link(doc, page)
    for a link to a page; `align` left/center/right/justify; `list_style`
    'bullet' or 'number'; `heading` 1..4. For several runs, build
    `TextRun`s and use RichText.build()."""
    return RichText.build([TextRun(text, color=color, font=font, size=size, bold=bold, italic=italic,
                                   underline=underline, strike=strike, link=link, highlight=highlight,
                                   align=align, list_style=list_style, heading=heading,
                                   line_height=line_height, indent=indent)])


def _default_text_attrs(font="Helvetica Neue", size=24.0, color=None, align: str | None = None) -> Msg:
    a = Msg()
    a.set_bytes(3, write_color(color) if color else Msg([[4, I32, struct.pack("<f", 1.0)]]))
    a.set_bytes(30, font)
    a.set_float(40, size)
    a.set_int(60, -60)
    a.set_float(70, -20.0)
    p = Msg()
    p.set_bytes(1, Msg([[1, LEN, b""]]))
    p.set_bytes(2, b"")
    p.set_bytes(3, Msg([[1, VARINT, (1 << 64) - 1], [2, VARINT, (1 << 64) - 1]]))
    if align:
        p.set_int(4, TextRun.ALIGN[align])
    return Msg([[1, LEN, a], [2, LEN, p]])


BOX_VERSION = 35


def new_box(doc: Document, origin, size, text: RichText | None = None, fill=None,
            outline=None, corner_radius: float | None = None,
            vertices: list[tuple[float, float]] | None = None, ellipse: bool = False,
            font="Helvetica Neue", font_size=24.0, rotation: float = 0.0,
            shadow=None, auto_size: bool | float = False, locked: bool = False,
            align: str | None = None, padding: float = 10.0, text_box: bool | None = None) -> Item:
    """A shape and/or text box: a rectangle (optional corner radius; a
    radius >= half the height makes a pill), an ellipse, or a polygon from
    `vertices` in the unit square (0..1). fill/outline None = none;
    outline = (width, rgba[, dash]); shadow = (rgba, radius, (dx, dy));
    rotation in radians; auto_size (True or a max width) lets the box grow
    with its text and wrap at that width, like GoodNotes' own text boxes."""
    bid, st = new_uuid(), stamp()
    b = Msg()
    b.set_bytes(1, bid)
    b.set_int(2, BOX_VERSION)
    b.set_bytes(3, st.copy())
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    if text_box is None:
        text_box = text is not None and fill is None and outline is None and not vertices and not ellipse
    if text_box or auto_size:
        b.set_int(9, 1)
    if locked:
        b.set_int(10, 1)
    tr = Msg([[1, LEN, write_point(*origin)]])
    if rotation:
        tr.set_float(2, rotation)
    tr.set_float(3, 1.0)
    b.set_bytes(20, tr)
    if auto_size:
        # autoWidthAutoHeight: GoodNotes lays the text out up to max width
        # and grows the box, wrapping long lines (a fixed-size box clips)
        max_w = float(auto_size) if not isinstance(auto_size, bool) else float(size[0]) if size else float("inf")
        a = Msg()
        a.set_float(1, 0.0)
        a.set_float(2, max_w)
        a.set_float(3, 0.0)
        a.set_float(4, float("inf"))
        b.set_bytes(21, Msg([[3, LEN, a]]))
    else:
        dims = write_point(*size)
        dims.set_float(3, float("inf"))
        b.set_bytes(21, Msg([[2, LEN, dims]]))
    geo = Msg()
    if ellipse:
        geo.set_bytes(2, b"")
    elif vertices:
        geo.set_bytes(3, _polygon(vertices))
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
    t.set_bytes(5, _default_text_attrs(font, font_size, align=align))
    t.set_bytes(10, Msg([[i, I32, struct.pack("<f", padding)] for i in (1, 2, 3, 4)]))
    b.set_bytes(32, t)
    if shadow:
        s = Msg()
        s.set_bytes(1, write_color(shadow[0]))
        s.set_float(2, shadow[1])
        s.set_bytes(3, write_point(*shadow[2]))
        b.set_bytes(33, s)
    item = Item(_meta(doc, bid, st, BOX_VERSION), Msg([[21, LEN, b]]))
    Box(item).text = text if text is not None else RichText(Msg.parse(_EMPTY_TEXT))
    t.fields.sort(key=lambda f: f[0])
    return item


def new_text_box(doc: Document, origin, text: RichText | str, max_width: float = 700.0,
                 font="Helvetica Neue", font_size=24.0, **kw) -> Item:
    """A text box the way GoodNotes' text tool makes one: it auto-sizes
    to its text and wraps lines at `max_width`. (A fixed-size box from
    new_box() clips long lines instead of wrapping them.)"""
    if isinstance(text, str):
        text = rich_text(text, font=font, size=font_size)
    return new_box(doc, origin, (max_width, font_size * 1.5 + 20), text=text, font=font,
                   font_size=font_size, text_box=True, auto_size=max_width, **kw)


ARROW_NONE, ARROW_OPEN, ARROW_FILLED = 0, 1, 2
DASHED, DOTTED = (3.0, 4.0), (0.0, 2.0)  # dash patterns, in multiples of line width


def new_line(doc: Document, start, end, mid=None, color=(0, 0, 0, 1),
             width: float = 3.0, arrow: int = ARROW_OPEN, elbow: bool = False,
             start_arrow: int = ARROW_NONE, dash: tuple[float, float] | None = None,
             vertical_first: bool = False) -> Item:
    """A line (curved through `mid` if given) or, with elbow=True, an
    elbow connector (`mid` = the corner). arrow/start_arrow:
    ARROW_NONE/OPEN/FILLED."""
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
        if vertical_first:
            g.set_int(4, 1)
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


def new_image(doc: Document, data: bytes, center, size=None, angle: float = 0.0,
              locked: bool = False) -> Item:
    """Registers `data` (PNG, JPEG, GIF, HEIC or a one-page PDF for a
    sticker) as an attachment and places it; `size` defaults to the
    image's pixel size."""
    aid = doc.add_attachment(data)
    if size is None:
        size = image_size(data) or (200.0, 200.0)
    iid, st = new_uuid(), stamp(6)
    b = Msg()
    b.set_bytes(1, iid)
    b.set_bytes(4, aid)
    b.set_bytes(5, Msg([[1, LEN, stamp()]]))
    fmt = image_format(data)
    if fmt:
        b.set_int(6, fmt)
    b.set_bytes(15, st.copy())
    b.set_int(18, S.SCHEMA_VERSION)
    if locked:
        b.set_int(21, 1)
    item = Item(_meta(doc, iid, st, attachment=aid), Msg([[1, LEN, b]]))
    Image(item).place(center, size, angle)
    # keep GoodNotes' field order: 1, 2, 3, 4, ...
    b.fields.sort(key=lambda f: f[0])
    return item


new_sticker = new_image  # stickers and elements are PDF attachments placed like images


def new_sticky(doc: Document, origin, size=(256.0, 256.0), text: RichText | None = None,
               color=(0.980, 0.906, 0.471, 1.0), author: tuple[str, str] | None = None,
               rotation: float = 0.0, expanded: bool = True, show_author: bool = True) -> Item:
    """A sticky note; `author` = (author id, display name)."""
    sid, st = new_uuid(), stamp(4)
    b = Msg()
    b.set_bytes(1, sid)
    b.set_int(2, BOX_VERSION)
    b.set_bytes(3, st.copy())
    b.set_bytes(7, Msg([[1, LEN, stamp()]]))
    tr = Msg([[1, LEN, write_point(*origin)]])
    if rotation:
        tr.set_float(2, rotation)
    tr.set_float(3, 1.0)
    b.set_bytes(20, tr)
    dims = write_point(*size)
    dims.set_float(3, float("inf"))
    b.set_bytes(21, Msg([[2, LEN, dims]]))
    b.set_bytes(30, write_color(color))
    t = Msg()
    t.set_bytes(5, _default_text_attrs())
    b.set_bytes(31, t)
    if author:
        b.set_bytes(32, author[0])
        b.set_bytes(33, author[1])
    b.set_int(40, 1 if expanded else 0)
    b.set_int(41, 1 if show_author else 0)
    item = Item(_meta(doc, sid, st, BOX_VERSION), Msg([[20, LEN, b]]))
    if text is not None:
        Sticky(item).text = text
    return item


def new_math(doc: Document, latex: str, image: bytes, center, size) -> Item:
    """A math conversion (what GoodNotes makes of handwriting it
    recognizes as an equation): a rendered image plus its LaTeX."""
    aid = doc.add_attachment(image)
    mid, st = new_uuid(), stamp(1)
    b = Msg()
    b.set_bytes(1, mid)
    b.set_bytes(4, aid)
    b.set_bytes(5, Msg([[1, LEN, stamp()]]))
    b.set_bytes(7, st.copy())
    b.set_bytes(9, latex)
    b.set_int(26, S.SCHEMA_VERSION)
    item = Item(_meta(doc, mid, st, attachment=aid), Msg([[11, LEN, b]]))
    Image(item).place(center, size)
    b.fields.sort(key=lambda f: f[0])
    return item
