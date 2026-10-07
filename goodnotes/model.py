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


