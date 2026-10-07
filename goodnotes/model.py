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


