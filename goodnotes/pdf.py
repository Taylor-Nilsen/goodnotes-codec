"""GoodNotes document -> vector PDF.

document_to_pdf: writes one PDF page per GoodNotes page (in document
order) with every element drawn as vector graphics, the way page_to_svg
draws it: ink of all pen kinds (ballpoint paths, fountain/brush outlines,
pencil centerlines, recognized shapes and their fills, highlighter as
Multiply blending),
shapes and text boxes (rotation, rounded corners, ellipses, polygons,
fill, dashed outlines, word-wrapped rich text in the base-14 Helvetica
fonts), sticky notes, lines/curves/elbow connectors with arrowheads,
placed images and math (JPEG passed through, PNG decoded here, PDF
stickers rasterized), legacy text boxes, Text Docs and the paper
background.

page_to_pdf_ops: the per-page core; returns the content stream and the
resources it uses.

Coordinates: everything is emitted in GoodNotes page units (origin top
left, y down) under a `[k 0 0 -k 0 H] cm` with k = PT_PER_UNIT, so the
MediaBox is the page size in points. Page rotation becomes /Rotate.

Usage:
    python -m goodnotes.pdf DOC.goodnotes OUT.pdf [--pages 0,2-5] [--no-background]
"""
from __future__ import annotations

import math
import re
import struct
import sys
import zlib
from dataclasses import dataclass

from .model import (DW_SCHEMA, PENCIL_SCHEMA, PT_PER_UNIT, VW_SCHEMA, Box, Document, FillShape,
                    Image, Item, LegacyText, Line, Page, RichText, Sticky, Stroke, TextDoc, TextRun,
                    f32, image_size, is_constant_width, read_point)

# --- numbers and paths ---------------------------------------------------------

_LIMIT = 1e6  # keep every number well inside what PDF readers accept
KAPPA = 0.5523  # cubic control distance for a quarter circle (x radius)


def _n(x, digits: int = 3) -> str:
    """A PDF number: 3 decimals, trailing zeros dropped, never nan/inf."""
    x = float(x)
    if not math.isfinite(x):
        x = 0.0
    x = max(-_LIMIT, min(_LIMIT, x))
    s = f"{x:.{digits}f}".rstrip("0").rstrip(".")
    return "0" if s in ("-0", "") else s


def _ns(*xs) -> str:
    return " ".join(_n(x) for x in xs)


def _cm(*m) -> str:
    """`cm` operator; matrices get 6 decimals (k = 6/11, rotations)."""
    return " ".join(_n(x, 6) for x in m) + " cm"


def _quad_to_cubic(p0, c, p1) -> tuple:
    """('C', c1, c2, p1) for the quadratic p0 -> c -> p1."""
    return ("C", (p0[0] + 2 / 3 * (c[0] - p0[0]), p0[1] + 2 / 3 * (c[1] - p0[1])),
            (p1[0] + 2 / 3 * (c[0] - p1[0]), p1[1] + 2 / 3 * (c[1] - p1[1])), p1)


def _arc_segs(cx, cy, rx, ry, a0, sweep, rot=0.0) -> list[tuple]:
    """Cubic pieces (<= 90 degrees each) of an elliptical arc starting at
    parameter angle a0 and sweeping `sweep` radians (positive = increasing
    angle = clockwise on a y-down page). The caller is at the start point."""
    if not sweep:
        return []
    cr, sr = math.cos(rot), math.sin(rot)

    def pt(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cx + cr * x - sr * y, cy + sr * x + cr * y)

    def d(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return (cr * x - sr * y, sr * x + cr * y)

    n = max(1, math.ceil(abs(sweep) / (math.pi / 2) - 1e-9))
    delta = sweep / n
    k = 4 / 3 * math.tan(delta / 4)
    out = []
    for i in range(n):
        a, b = a0 + i * delta, a0 + (i + 1) * delta
        pa, pb, da, db = pt(a), pt(b), d(a), d(b)
        out.append(("C", (pa[0] + k * da[0], pa[1] + k * da[1]), (pb[0] - k * db[0], pb[1] - k * db[1]), pb))
    return out


def _ellipse_segs(cx, cy, rx, ry, rot=0.0) -> list[tuple]:
    c, s = math.cos(rot), math.sin(rot)
    start = (cx + c * rx, cy + s * rx)
    return [("M", start)] + _arc_segs(cx, cy, rx, ry, 0.0, 2 * math.pi, rot) + [("Z",)]


def _rounded_rect_segs(x, y, w, h, r) -> list[tuple]:
    if r <= 0:
        return [("M", (x, y)), ("L", (x + w, y)), ("L", (x + w, y + h)), ("L", (x, y + h)), ("Z",)]
    k = r * (1 - KAPPA)
    x1, y1 = x + w, y + h
    return [("M", (x + r, y)), ("L", (x1 - r, y)), ("C", (x1 - k, y), (x1, y + k), (x1, y + r)),
            ("L", (x1, y1 - r)), ("C", (x1, y1 - k), (x1 - k, y1), (x1 - r, y1)),
            ("L", (x + r, y1)), ("C", (x + k, y1), (x, y1 - k), (x, y1 - r)),
            ("L", (x, y + r)), ("C", (x, y + k), (x + k, y), (x + r, y)), ("Z",)]


def _rotate_cm(angle: float, cx: float, cy: float) -> str:
    """`cm` operator rotating by `angle` radians (clockwise on the page)
    about (cx, cy); same matrix as SVG rotate()."""
    c, s = math.cos(angle), math.sin(angle)
    return _cm(c, s, -s, c, cx - c * cx + s * cy, cy - s * cx - c * cy)


# --- resources -----------------------------------------------------------------


@dataclass
class PdfImage:
    """An image XObject: `data` is already encoded with `filter`.
    Objects with equal `key` are written once per PDF."""
    key: object
    width: int
    height: int
    colorspace: str
    bpc: int
    data: bytes
    filter: str = "/FlateDecode"
    decode_parms: str | None = None
    decode: str | None = None
    mask: str | None = None          # color-key mask array (PNG tRNS)
    smask: "PdfImage | None" = None  # soft mask (alpha channel)

    def dict(self, smask_ref: str | None = None) -> str:
        parts = [f"/Type /XObject /Subtype /Image /Width {self.width} /Height {self.height}",
                 f"/ColorSpace {self.colorspace} /BitsPerComponent {self.bpc}",
                 f"/Filter {self.filter} /Length {len(self.data)}"]
        if self.decode_parms:
            parts.append(f"/DecodeParms {self.decode_parms}")
        if self.decode:
            parts.append(f"/Decode {self.decode}")
        if self.mask:
            parts.append(f"/Mask {self.mask}")
        if smask_ref:
            parts.append(f"/SMask {smask_ref}")
        return "<< " + " ".join(parts) + " >>"


class Resources:
    """What one page's content stream refers to, by local name:
    fonts {name: base-14 font}, ext_gstates {name: (stroke alpha, fill
    alpha, blend mode)}, xobjects {name: PdfImage}."""

    def __init__(self):
        self.fonts: dict[str, str] = {}
        self.ext_gstates: dict[str, tuple[float, float, str]] = {}
        self.xobjects: dict[str, PdfImage] = {}

    def font(self, base: str) -> str:
        for name, b in self.fonts.items():
            if b == base:
                return name
        name = f"F{len(self.fonts) + 1}"
        self.fonts[name] = base
        return name

    def gstate(self, stroke_alpha: float = 1.0, fill_alpha: float = 1.0, blend: str = "Normal") -> str:
        key = (round(_clamp01(stroke_alpha), 3), round(_clamp01(fill_alpha), 3), blend)
        for name, v in self.ext_gstates.items():
            if v == key:
                return name
        name = f"GS{len(self.ext_gstates) + 1}"
        self.ext_gstates[name] = key
        return name

    def image(self, img: PdfImage) -> str:
        for name, v in self.xobjects.items():
            if v.key == img.key:
                return name
        name = f"Im{len(self.xobjects) + 1}"
        self.xobjects[name] = img
        return name


def _clamp01(x) -> float:
    x = float(x)
    return min(max(x, 0.0), 1.0) if math.isfinite(x) else 1.0


class _Canvas:
    """Content-stream builder. Coordinates are page units, y down."""

    def __init__(self, res: Resources):
        self.res = res
        self.ops: list[str] = []

    def op(self, s: str) -> None:
        self.ops.append(s)

    def path(self, segs, dot_subpaths: bool = False) -> None:
        """Emits M/L/C/Z segments. dot_subpaths: a lone moveto gets a
        zero-length lineto so a round-capped stroke paints a dot."""
        lone = None
        for seg in segs:
            op = seg[0]
            if op == "M":
                if lone is not None and dot_subpaths:
                    self.op(f"{_ns(*lone)} l")
                self.op(f"{_ns(*seg[1])} m")
                lone = seg[1]
                continue
            lone = None
            if op == "L":
                self.op(f"{_ns(*seg[1])} l")
            elif op == "C":
                self.op(f"{_ns(*seg[1], *seg[2], *seg[3])} c")
            elif op == "Z":
                self.op("h")
        if lone is not None and dot_subpaths:
            self.op(f"{_ns(*lone)} l")

    def style(self, stroke=None, fill=None, width=None, cap=None, join=None, dash=None,
              multiply: bool = False) -> None:
        """Sets colors (RGBA; alpha through an ExtGState), line width,
        cap/join (0 butt/miter, 1 round, 2 square/bevel) and dash array."""
        if stroke is not None:
            self.op(f"{_ns(*stroke[:3])} RG")
        if fill is not None:
            self.op(f"{_ns(*fill[:3])} rg")
        sa = stroke[3] if stroke is not None else 1.0
        fa = fill[3] if fill is not None else 1.0
        if sa < 1 or fa < 1 or multiply:
            self.op(f"/{self.res.gstate(sa, fa, 'Multiply' if multiply else 'Normal')} gs")
        if width is not None:
            self.op(f"{_n(max(width, 0.0))} w")
        if cap is not None:
            self.op(f"{cap} J")
        if join is not None:
            self.op(f"{join} j")
        if dash:
            lens = [max(v, 0.0) for v in dash if math.isfinite(v)]
            if lens and sum(lens) > 0:
                self.op(f"[{_ns(*lens)}] 0 d")

    def image(self, img: PdfImage, cx, cy, w, h, angle: float = 0.0) -> None:
        """Draws `img` stretched to w x h, centered at (cx, cy), rotated."""
        name = self.res.image(img)
        self.op("q")
        if angle:
            self.op(_rotate_cm(angle, cx, cy))
        # image space is y-up: its top row goes to the top of the box
        self.op(f"{_cm(w, 0, 0, -h, cx - w / 2, cy + h / 2)} /{name} Do Q")


# --- text ------------------------------------------------------------------------

# Advance widths (1/1000 em) of the standard Helvetica AFMs, characters 32..126
_HELV = [278, 278, 355, 556, 556, 889, 667, 191, 333, 333, 389, 584, 278, 333, 278, 278,
         556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 278, 278, 584, 584, 584, 556,
         1015, 667, 667, 722, 722, 667, 611, 778, 722, 278, 500, 667, 556, 833, 722, 778,
         667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 278, 278, 278, 469, 556,
         333, 556, 556, 500, 556, 556, 278, 556, 556, 222, 222, 500, 222, 833, 556, 556,
         556, 556, 333, 500, 278, 556, 500, 722, 500, 500, 500, 334, 260, 334, 584]
_HELV_BOLD = [278, 333, 474, 556, 556, 889, 722, 238, 333, 333, 389, 584, 278, 333, 278, 278,
              556, 556, 556, 556, 556, 556, 556, 556, 556, 556, 333, 333, 584, 584, 584, 611,
              975, 722, 722, 722, 722, 667, 611, 778, 722, 278, 556, 722, 611, 833, 722, 778,
              667, 778, 722, 667, 611, 722, 667, 944, 667, 667, 611, 333, 278, 333, 584, 556,
              333, 556, 611, 556, 611, 556, 333, 611, 611, 278, 278, 556, 278, 889, 611, 611,
              611, 611, 389, 556, 333, 611, 556, 778, 556, 556, 500, 389, 280, 389, 584]
_BULLET = 0x95  # WinAnsi code of U+2022


def _font_name(bold: bool, italic: bool) -> str:
    """Every font family maps to base-14 Helvetica."""
    return "Helvetica" + {(False, False): "", (True, False): "-Bold", (False, True): "-Oblique",
                          (True, True): "-BoldOblique"}[(bool(bold), bool(italic))]


def _encode(text: str) -> bytes:
    """WinAnsiEncoding (cp1252); unencodable characters become '?'."""
    text = re.sub(r"[\x00-\x1f\x7f]", " ", text)
    return text.encode("cp1252", "replace")


def _text_width(text: str, font: str, size: float) -> float:
    table = _HELV_BOLD if "Bold" in font else _HELV
    total = 0
    for b in _encode(text):
        if 32 <= b <= 126:
            total += table[b - 32]
        else:
            total += 350 if b == _BULLET else 556
    return total * size / 1000


def _pdf_string(data: bytes) -> str:
    """A literal string for a latin-1 content stream."""
    out = []
    for b in data:
        if b in (0x28, 0x29, 0x5C):
            out.append("\\" + chr(b))
        elif b < 32:
            out.append(f"\\{b:03o}")
        else:
            out.append(chr(b))
    return "(" + "".join(out) + ")"


def _show(cv: _Canvas, text: str, font: str, size: float, x: float, y: float, color) -> None:
    """One text segment with its baseline at (x, y)."""
    cv.style(fill=color)
    name = cv.res.font(font)
    # the text matrix flips y back so glyphs stand upright on the y-down page
    cv.op(f"BT /{name} {_n(size)} Tf {_ns(1, 0, 0, -1, x, y)} Tm {_pdf_string(_encode(text))} Tj ET")


_HEADING = {1: 1.6, 2: 1.4, 3: 1.2}


@dataclass
class _Seg:
    run: TextRun
    text: str
    font: str
    size: float


def _wrap(runs: list[_Seg], max_w: float) -> list[list[_Seg]]:
    """Greedy word wrap of a paragraph's runs to `max_w`; a word longer
    than a line is broken between characters."""
    lines: list[list[_Seg]] = []
    cur: list[_Seg] = []
    cur_w = 0.0

    def add(seg_like: _Seg, tok: str) -> None:
        nonlocal cur_w
        if cur and cur[-1].run is seg_like.run:
            cur[-1].text += tok
        else:
            cur.append(_Seg(seg_like.run, tok, seg_like.font, seg_like.size))
        cur_w += _text_width(tok, seg_like.font, seg_like.size)

    def flush() -> None:
        nonlocal cur, cur_w
        while cur and not cur[-1].text.strip():
            cur.pop()
        if cur:
            cur[-1].text = cur[-1].text.rstrip()
        lines.append(cur)
        cur, cur_w = [], 0.0

    for seg in runs:
        for tok in re.findall(r"\s+|\S+", seg.text.replace("\t", "    ")):
            if tok.isspace():
                if cur or not lines:  # spaces after a wrap are dropped
                    add(seg, tok)
                continue
            tw = _text_width(tok, seg.font, seg.size)
            if cur and any(s.text.strip() for s in cur) and cur_w + tw > max_w:
                flush()
            while not cur and tw > max_w and len(tok) > 1:
                k = 1
                while k < len(tok) and _text_width(tok[:k + 1], seg.font, seg.size) <= max_w:
                    k += 1
                add(seg, tok[:k])
                flush()
                tok = tok[k:]
                tw = _text_width(tok, seg.font, seg.size)
            add(seg, tok)
    flush()
    return lines


def _layout(rt: RichText | None, width: float, default_size: float = 24.0,
            default_align: str = "left") -> tuple[list[tuple], float]:
    """Lays out rich text like _text_svg: one block per paragraph, line
    height size * 1.2 (or the paragraph's factor), wrapped at `width`.
    Returns ([(segments, line width, baseline offset, x offset)], height),
    offsets relative to the top-left of the text area."""
    if rt is None:
        return [], 0.0
    out, ly = [], 0.0
    for n, para in enumerate(rt.paragraphs()):
        first = para[0] if para else None
        if first and first.list_style:
            marker = "\u2022 " if first.list_style == "bullet" else f"{n + 1}. "
            para = [TextRun(marker, size=first.size, color=first.color)] + para
        segs = []
        for r in para:
            sz = r.size or default_size
            if r.heading:
                sz *= _HEADING.get(r.heading, 1.1)
            segs.append(_Seg(r, r.text, _font_name(r.bold or r.heading, r.italic), sz))
        size = max((s.size for s in segs), default=default_size)
        h = size * (first.line_height if first and first.line_height else 1.2)
        align = first.align if first and first.align else default_align
        indent = (first.indent or 0) * 24 if first else 0
        for line in _wrap(segs, max(width - indent, 1.0)):
            ly += h
            line_w = sum(_text_width(s.text, s.font, s.size) for s in line)
            dx = indent
            if align == "center":
                dx += (width - indent - line_w) / 2
            elif align == "right":
                dx += width - indent - line_w
            out.append((line, line_w, ly - h * 0.25, dx))
    return out, ly


def _draw_text(cv: _Canvas, rt: RichText | None, x: float, y: float, width: float,
               default_size: float = 24.0, default_align: str = "left",
               default_color=(0, 0, 0, 1), lines=None) -> None:
    """Draws rich text with its first line's top at (x, y); `lines` is a
    precomputed _layout() result."""
    if lines is None:
        lines = _layout(rt, width, default_size, default_align)[0]
    for line, _w, base_dy, dx in lines:
        tx, base = x + dx, y + base_dy
        for s in line:
            sw = _text_width(s.text, s.font, s.size)
            r = s.run
            color = r.color if r.color is not None else default_color
            cv.op("q")
            if r.highlight:
                cv.style(fill=r.highlight)
                cv.op(f"{_ns(tx, base - s.size * 0.85, sw, s.size * 1.1)} re f")
            if s.text:
                _show(cv, s.text, s.font, s.size, tx, base, color)
            for on, dy in ((r.underline, s.size * 0.1), (r.strike, -s.size * 0.28)):
                if on and sw > 0:
                    cv.style(stroke=color, width=s.size * 0.05, cap=0)
                    cv.op(f"{_ns(tx, base + dy)} m {_ns(tx + sw, base + dy)} l S")
            cv.op("Q")
            tx += sw


def _plain_lines(cv: _Canvas, lines: list[str], x: float, y: float, size: float, step: float) -> None:
    for i, line in enumerate(lines):
        if line:
            _show(cv, line, "Helvetica", size, x, y + step * (i + 1), (0, 0, 0, 1))


# --- elements --------------------------------------------------------------------

# outline command codes -> (op, float count), per schema (schema.VW_COMMAND / DW_COMMAND)
_OUTLINE_VW = {0: ("M", 2), 1: ("Q", 4), 2: ("C", 6), 3: ("A", 5), 4: ("M", 3), 5: ("Q", 5), 6: ("E", 7)}
_OUTLINE_DW = {2: ("M", 2), 3: ("Q", 4), 4: ("C", 6), 5: ("A", 5), 6: ("M", 3), 7: ("Q", 5), 8: ("E", 7)}


def _outline_segs(t, dx=0.0, dy=0.0) -> list[tuple]:
    """Path of a variable-width stroke's precomputed outline (port of
    svg._outline_d); arcs become cubics."""
    if t.schema == DW_SCHEMA:
        table, v = _OUTLINE_DW, t.values[2:]
    else:
        table, v = _OUTLINE_VW, t.values[1:]
    types = v[4]
    arrays, cw = {"M": v[5], "Q": v[6], "C": v[7], "A": v[8], "E": v[8]}, v[9]
    pos = {"M": 0, "Q": 0, "C": 0, "A": 0}
    ai = 0
    out, cur = [], None
    for code in types:
        if code not in table:
            continue
        op, n = table[code]
        key = "A" if op == "E" else op
        vals = [f32(x) for x in arrays[op][pos[key]:pos[key] + n]]
        pos[key] += n
        if len(vals) < n:
            break
        if op == "M":
            if out:
                out.append(("Z",))
            cur = (vals[0] + dx, vals[1] + dy)
            out.append(("M", cur))
        elif op == "Q":
            c, p = (vals[0] + dx, vals[1] + dy), (vals[2] + dx, vals[3] + dy)
            if cur is None:
                cur = c
                out.append(("M", cur))
            out.append(_quad_to_cubic(cur, c, p))
            cur = p
        elif op == "C":
            p = (vals[4] + dx, vals[5] + dy)
            if cur is None:
                cur = (vals[0] + dx, vals[1] + dy)
                out.append(("M", cur))
            out.append(("C", (vals[0] + dx, vals[1] + dy), (vals[2] + dx, vals[3] + dy), p))
            cur = p
        else:
            clockwise = bool(cw[ai]) if ai < len(cw) else False
            ai += 1
            rot = 0.0
            if op == "A":
                cx, cy, r, a0, a1 = vals
                rx = ry = r
            else:  # elliptical (marker pen's chisel nib): cx, cy, rx, ry, start, end, rotation
                cx, cy, rx, ry, a0, a1, rot = vals
                rx, ry = abs(rx), abs(ry)
            cx, cy = cx + dx, cy + dy
            if clockwise:
                sweep = -((a0 - a1) % (2 * math.pi))
            else:
                sweep = (a1 - a0) % (2 * math.pi)
            if not sweep and abs(a1 - a0) > 1e-6:  # a whole turn
                sweep = -2 * math.pi if clockwise else 2 * math.pi
            cr, sr = math.cos(rot), math.sin(rot)

            def at(a):
                px, py = rx * math.cos(a), ry * math.sin(a)
                return (cx + cr * px - sr * py, cy + sr * px + cr * py)
            start = at(a0)
            out.append(("L" if cur is not None else "M", start))
            out += _arc_segs(cx, cy, rx, ry, a0, sweep, rot)
            cur = at(a0 + sweep)
    if out:
        out.append(("Z",))
    return out


def _pencil_segs(t, dx=0.0, dy=0.0) -> tuple[list[tuple], float]:
    """Centerline + average width for the pencil schema (port of svg._pencil_d)."""
    th = f32(t.values[1])
    kinds, moves, quads = t.values[2], t.values[3], t.values[4]
    out, widths, mi, qi, cur = [], [], 0, 0, None
    for k in kinds:
        if k == 0 and mi < len(moves):
            m = [f32(x) for x in moves[mi]]
            cur = (m[0] + dx, m[1] + dy)
            out.append(("M", cur))
            widths.append(m[4])  # force
            mi += 1
        elif qi < len(quads):
            q = [f32(x) for x in quads[qi]]  # q[0] = texture seed
            c, p = (q[1] + dx, q[2] + dy), (q[6] + dx, q[7] + dy)
            if cur is None:
                cur = c
                out.append(("M", cur))
            out.append(_quad_to_cubic(cur, c, p))
            cur = p
            widths.append(q[10])
            qi += 1
    w = th * 2 * (0.4 + (sum(widths) / len(widths) if widths else 0.6))
    return out, max(w, 0.5)


def _shape_segs(shape, dx, dy) -> list[tuple] | None:
    """Recognized shape of a shape-tool stroke (rect / ellipse / polyline)."""
    if shape.sub(3) is not None:  # rectangle: center, size
        (cx, cy), (rw, rh) = read_point(shape.path(3, 1)), read_point(shape.path(3, 2))
        return _rounded_rect_segs(cx - rw / 2 + dx, cy - rh / 2 + dy, rw, rh, 0)
    if shape.sub(4) is not None:  # ellipse: center, radii, angle
        e = shape.sub(4)
        (cx, cy), (ew, eh), ang = read_point(e.sub(1)), read_point(e.sub(2)), e.float(3)
        return _ellipse_segs(cx + dx, cy + dy, ew, eh, ang)
    poly = shape.sub(1)
    pts = [read_point(p) for p in poly.subs(1)] if poly is not None else []
    if not pts:
        return None
    return [("M", (pts[0][0] + dx, pts[0][1] + dy))] + [("L", (x + dx, y + dy)) for x, y in pts[1:]]


def _draw_stroke(cv: _Canvas, s: Stroke) -> None:
    color = s.color
    dx, dy = s.translation
    mul = s.highlighter
    shape = s.shape
    if shape is not None:
        segs = _shape_segs(shape, dx, dy)
        if segs:
            cv.style(stroke=color, width=shape.float(15) or 2.0, cap=1, join=1, multiply=mul)
            cv.path(segs, dot_subpaths=True)
            cv.op("S")
            return
    t = s.template
    if is_constant_width(t.schema):
        # the same decoding as Stroke.commands, on the template parsed above
        kinds, moves, quads = t.values[2], t.values[3], t.values[4]
        segs, cur, mi, qi = [], None, 0, 0
        for kind in kinds:
            if kind == 0:
                m = moves[mi]
                mi += 1
                cur = (f32(m[0]) + dx, f32(m[1]) + dy)
                segs.append(("M", cur))
            else:
                q = quads[qi]
                qi += 1
                ctrl, p = (f32(q[0]) + dx, f32(q[1]) + dy), (f32(q[2]) + dx, f32(q[3]) + dy)
                if cur is None:
                    cur = ctrl
                    segs.append(("M", cur))
                segs.append(_quad_to_cubic(cur, ctrl, p))
                cur = p
        if not segs:
            return
        w = f32(t.values[1])
        dash, cap = None, 0 if mul else 1
        d = s.dash
        if d and d[0]:
            dash = [max(v * w, 0.01) for v in d[0]]
            cap = 0 if mul else d[1]
        cv.style(stroke=color, width=w, cap=cap, join=1, dash=dash, multiply=mul)
        cv.path(segs, dot_subpaths=True)
        cv.op("S")
    elif t.schema in (VW_SCHEMA, DW_SCHEMA):
        segs = _outline_segs(t, dx, dy)
        if segs:
            cv.style(fill=color, multiply=mul)
            cv.path(segs)
            cv.op("f")
    elif t.schema == PENCIL_SCHEMA:
        segs, w = _pencil_segs(t, dx, dy)
        if segs:
            cv.style(stroke=color, width=w, cap=1, join=1, multiply=mul)
            cv.path(segs, dot_subpaths=True)
            cv.op("S")


def _draw_fillshape(cv: _Canvas, f: FillShape) -> None:
    """Shape-tool fill: the recognized rect / ellipse / polygon, filled."""
    sh = f.shape
    if sh is None:
        return
    dx, dy = f.translation
    segs = _shape_segs(sh, dx, dy)
    if segs:
        cv.style(fill=f.color)
        cv.path(segs + [("Z",)] if segs[-1][0] != "Z" else segs)
        cv.op("f")


def _paint(cv: _Canvas, segs, fill, outline, dash, join=None) -> None:
    """Fills and/or strokes a closed path (outline = (width, rgba))."""
    if not fill and not outline:
        return
    cv.style(stroke=outline[1] if outline else None, fill=fill, width=outline[0] if outline else None,
             join=join, dash=[max(dash[0] * outline[0], 0.01), dash[1] * outline[0]] if outline and dash else None)
    cv.path(segs)
    cv.op("B" if fill and outline else "f" if fill else "S")


def _draw_box(cv: _Canvas, b: Box) -> None:
    (x, y), (w, h) = b.origin, b.size
    t = b.b.sub(32)
    pad = b.padding
    attrs = t.path(5, 1) if t is not None else None
    size = (attrs.float(40) if attrs is not None else 24.0) or 24.0
    para = t.path(5, 2) if t is not None else None
    align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(para.int(4), "left") if para is not None else "left"
    text = b.text
    if text is not None and not text.text.strip():
        text = None
    sizing = b.b.sub(21)
    auto = sizing.sub(3) if sizing is not None else None
    if auto is not None or (w, h) == (0.0, 0.0):  # auto-sized text box
        measured = t.sub(2) if t is not None else None
        if measured is not None:  # size GoodNotes measured
            mw, mh = read_point(measured)
            w, h = max(w, mw + 20), max(h, mh + 20)
        elif text is not None:  # not measured yet: wrap at the max width
            max_w = auto.float(2) if auto is not None else 0.0
            if not math.isfinite(max_w) or max_w <= 0:
                max_w = _LIMIT
            lines, th = _layout(text, max(max_w - 2 * pad, 1), size)
            natural = max((lw + dx for _, lw, _, dx in lines), default=0.0) + 2 * pad
            w, h = max(w, min(max_w, natural)), max(h, th + pad * 1.5)
    fill = b.fill
    outline = b.outline
    if not (outline and outline[1] is not None and outline[0] > 0):
        outline = None
    cv.op("q")
    if b.rotation:
        cv.op(_rotate_cm(b.rotation, x + w / 2, y + h / 2))
    cv.op("q")
    geo = b.geometry
    if geo == "ellipse":
        _paint(cv, _ellipse_segs(x + w / 2, y + h / 2, w / 2, h / 2), fill, outline, b.dash)
    elif geo == "polygon" and b.vertices:
        pts = [(x + vx * w, y + vy * h) for vx, vy in b.vertices]
        segs = [("M", pts[0])] + [("L", p) for p in pts[1:]] + [("Z",)]
        _paint(cv, segs, fill, outline, b.dash, join=1)
    else:
        r = max(min(b.corner_radius, w / 2, h / 2), 0.0)  # GoodNotes clamps like this (a pill is a huge radius)
        _paint(cv, _rounded_rect_segs(x, y, w, h, r), fill, outline, b.dash)
    cv.op("Q")
    if text is not None:
        _draw_text(cv, text, x + pad, y + pad * 0.5, max(w - 2 * pad, 1), size, default_align=align)
    cv.op("Q")


def _draw_sticky(cv: _Canvas, s: Sticky) -> None:
    (x, y), (w, h) = s.origin, s.size
    cv.op("q")
    if s.rotation:
        cv.op(_rotate_cm(s.rotation, x + w / 2, y + h / 2))
    cv.style(fill=s.color)
    cv.op(f"{_ns(x, y, w, h)} re f")
    head = bool(s.show_author and s.author[1])
    if head:
        _show(cv, s.author[1], "Helvetica", 11, x + 10, y + 16, (1 / 3, 1 / 3, 1 / 3, 1))
    _draw_text(cv, s.text, x + 10, y + (18 if head else 4), max(w - 20, 1), 24.0)
    cv.op("Q")


def _arrowhead(cv: _Canvas, tip, direction, style: int, width: float, color) -> None:
    """Open chevron (1) or filled triangle (2) pointing along `direction`
    at `tip`, sized like the SVG markers (5x / 4x the line width)."""
    dx, dy = direction
    n = math.hypot(dx, dy)
    if not n or style not in (1, 2):
        return
    ux, uy = dx / n, dy / n
    size = width * (5 if style == 1 else 4)
    tx, ty = tip[0] + ux * size * 0.1, tip[1] + uy * size * 0.1
    bx, by = tx - ux * size, ty - uy * size
    a, b = (bx - uy * size / 2, by + ux * size / 2), (bx + uy * size / 2, by - ux * size / 2)
    if style == 1:
        cv.op(f"q [] 0 d {_n(width * 0.75)} w 1 J 1 j {_ns(*a)} m {_ns(tx, ty)} l {_ns(*b)} l S Q")
    else:
        cv.op(f"q {_ns(*color[:3])} rg {_ns(*a)} m {_ns(tx, ty)} l {_ns(*b)} l h f Q")


def _draw_line(cv: _Canvas, ln: Line) -> None:
    if len(ln.points) < 2:
        return
    color = ln.color
    width = ln.width or 2
    start, end = ln.start, ln.end
    if ln.elbow:
        knee = ln.knee
        g = ln.b.sub(21)
        vertical = g is not None and g.int(4) == 1
        if knee is not None:
            pts = ([start, (start[0], knee[1]), (end[0], knee[1]), end] if vertical else
                   [start, (knee[0], start[1]), (knee[0], end[1]), end])
        else:
            pts = [start, (end[0], start[1]), end]
        segs = [("M", pts[0])] + [("L", p) for p in pts[1:]]
        uniq = [p for i, p in enumerate(pts) if i == 0 or math.dist(p, pts[i - 1]) > 1e-6]
        if len(uniq) < 2:
            uniq = [start, end]
        d_start = (uniq[0][0] - uniq[1][0], uniq[0][1] - uniq[1][1])
        d_end = (uniq[-1][0] - uniq[-2][0], uniq[-1][1] - uniq[-2][1])
    elif ln.mid is not None:
        mid = ln.mid
        ctrl = (2 * mid[0] - (start[0] + end[0]) / 2, 2 * mid[1] - (start[1] + end[1]) / 2)
        segs = [("M", start), _quad_to_cubic(start, ctrl, end)]
        d_start = (start[0] - ctrl[0], start[1] - ctrl[1])
        d_end = (end[0] - ctrl[0], end[1] - ctrl[1])
    else:
        segs = [("M", start), ("L", end)]
        d_start = (start[0] - end[0], start[1] - end[1])
        d_end = (end[0] - start[0], end[1] - start[1])
    d = ln.dash
    dash = [max(d[0] * width, 0.01), d[1] * width] if d else None
    cv.op("q")
    cv.style(stroke=color, fill=color, width=width, cap=1, join=1, dash=dash)
    cv.path(segs)
    cv.op("S")
    if ln.arrow:
        _arrowhead(cv, end, d_end, ln.arrow, width, color)
    if ln.start_arrow:
        _arrowhead(cv, start, d_start, ln.start_arrow, width, color)
    cv.op("Q")


def _legacy_text(cv: _Canvas, item: Item) -> None:
    v = LegacyText(item)
    x, y = v.origin
    _plain_lines(cv, v.text.split("\n"), x, y, 16, 16 * 1.2)


def _textdoc(cv: _Canvas, item: Item, page: Page) -> None:
    txt = TextDoc(item).plain_text
    if not txt:
        return
    w, _ = page.size
    lines, cur = [], ""
    for wd in txt.split():
        if len(cur) + len(wd) > w / 9:
            lines.append(cur)
            cur = wd
        else:
            cur = (cur + " " + wd).strip()
    lines.append(cur)
    _plain_lines(cv, lines, 40, 40, 16, 16 * 1.4)


# --- images ----------------------------------------------------------------------

_PNG_SIG = b"\x89PNG\r\n\x1a\n"
_ADAM7 = [(0, 0, 8, 8), (4, 0, 8, 8), (0, 4, 4, 8), (2, 0, 4, 4), (0, 2, 2, 4), (1, 0, 2, 2), (0, 1, 1, 2)]


def _unfilter(raw: bytes, pos: int, rows: int, stride: int, bpp: int) -> tuple[bytearray, int]:
    """Reverses PNG scanline filters (None, Sub, Up, Average, Paeth)."""
    out = bytearray()
    prev = bytearray(stride)
    for _ in range(rows):
        if pos + 1 + stride > len(raw):
            raise ValueError("truncated PNG data")
        ft = raw[pos]
        line = bytearray(raw[pos + 1:pos + 1 + stride])
        pos += 1 + stride
        if ft == 1:
            for i in range(bpp, stride):
                line[i] = (line[i] + line[i - bpp]) & 0xFF
        elif ft == 2:
            line = bytearray((a + b) & 0xFF for a, b in zip(line, prev))
        elif ft == 3:
            for i in range(stride):
                left = line[i - bpp] if i >= bpp else 0
                line[i] = (line[i] + ((left + prev[i]) >> 1)) & 0xFF
        elif ft == 4:
            for i in range(stride):
                a = line[i - bpp] if i >= bpp else 0
                b = prev[i]
                c = prev[i - bpp] if i >= bpp else 0
                p = a + b - c
                pa, pb, pc = abs(p - a), abs(p - b), abs(p - c)
                pred = a if pa <= pb and pa <= pc else b if pb <= pc else c
                line[i] = (line[i] + pred) & 0xFF
        elif ft != 0:
            raise ValueError(f"bad PNG filter {ft}")
        out += line
        prev = line
    return out, pos


def _samples(rows: bytes, width: int, height: int, channels: int, depth: int, scale: bool) -> bytearray:
    """Unfiltered scanlines -> one byte per sample (16-bit: high byte;
    1/2/4-bit: unpacked, scaled to 0..255 unless `scale` is False)."""
    if depth == 8:
        return bytearray(rows)
    if depth == 16:
        return bytearray(rows[0::2])
    stride = (width * channels * depth + 7) // 8
    per_row = width * channels
    mask, mul = (1 << depth) - 1, (255 // ((1 << depth) - 1)) if scale else 1
    out = bytearray()
    for y in range(height):
        row = rows[y * stride:(y + 1) * stride]
        vals = bytearray()
        for byte in row:
            for shift in range(8 - depth, -1, -depth):
                vals.append(((byte >> shift) & mask) * mul)
        out += vals[:per_row]
    return out


def _png_image(data: bytes, key) -> PdfImage:
    """PNG -> image XObject. Opaque 8-bit-or-less gray/RGB/palette images
    keep their compressed data (PDF understands PNG predictors); others are
    decoded here, with alpha split into a /SMask."""
    if data[:8] != _PNG_SIG:
        raise ValueError("not a PNG")
    pos, idat, plte, trns, ihdr = 8, [], None, None, None
    while pos + 8 <= len(data):
        n, typ = struct.unpack(">I4s", data[pos:pos + 8])
        body = data[pos + 8:pos + 8 + n]
        pos += 12 + n
        if typ == b"IHDR":
            ihdr = struct.unpack(">IIBBBBB", body[:13])
        elif typ == b"PLTE":
            plte = body
        elif typ == b"tRNS":
            trns = body
        elif typ == b"IDAT":
            idat.append(body)
        elif typ == b"IEND":
            break
    if ihdr is None or not idat:
        raise ValueError("incomplete PNG")
    w, h, depth, ctype, _comp, _filt, interlace = ihdr
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}[ctype]
    if depth not in (1, 2, 4, 8, 16) or w == 0 or h == 0:
        raise ValueError("unsupported PNG")
    if ctype == 3 and not plte:
        raise ValueError("PNG palette missing")
    stream = b"".join(idat)
    raw = zlib.decompress(stream)
    rgb_space = "/DeviceRGB" if channels >= 3 or ctype == 3 else "/DeviceGray"

    def palette_space() -> str:
        n = len(plte) // 3
        return f"[/Indexed /DeviceRGB {n - 1} <{plte[:n * 3].hex()}>]"

    # fast path: hand the zlib stream straight to the PDF
    if not interlace and depth <= 8 and ctype in (0, 2, 3) and not (ctype == 3 and trns):
        mask = None
        if trns and ctype == 0 and len(trns) >= 2:
            v = struct.unpack(">H", trns[:2])[0]
            mask = f"[{v} {v}]"
        elif trns and ctype == 2 and len(trns) >= 6:
            r, g, b = struct.unpack(">HHH", trns[:6])
            mask = f"[{r} {r} {g} {g} {b} {b}]"
        parms = f"<< /Predictor 15 /Colors {channels} /BitsPerComponent {depth} /Columns {w} >>"
        return PdfImage(key, w, h, palette_space() if ctype == 3 else rgb_space, depth, stream,
                        decode_parms=parms, mask=mask)

    # full decode
    bits = channels * depth
    bpp = max(1, bits // 8)
    samples = bytearray(w * h * channels)
    p = 0
    for x0, y0, sx, sy in (_ADAM7 if interlace else [(0, 0, 1, 1)]):
        pw, ph = (w - x0 + sx - 1) // sx, (h - y0 + sy - 1) // sy
        if pw <= 0 or ph <= 0:
            continue
        rows, p = _unfilter(raw, p, ph, (pw * bits + 7) // 8, bpp)
        sub = _samples(rows, pw, ph, channels, depth, ctype != 3)
        if not interlace:
            samples = sub
            break
        for yy in range(ph):
            for xx in range(pw):
                src = (yy * pw + xx) * channels
                dst = ((y0 + yy * sy) * w + x0 + xx * sx) * channels
                samples[dst:dst + channels] = sub[src:src + channels]
    npx = w * h
    alpha = None
    if ctype == 3:
        n = len(plte) // 3
        table = [plte[i * 3:i * 3 + 3] for i in range(n)]
        black = b"\0\0\0"
        color = bytearray(b"".join(table[i] if i < n else black for i in samples))
        if trns:
            amap = bytes(trns[i] if i < len(trns) else 255 for i in range(256))
            alpha = bytearray(samples).translate(amap)
    elif ctype in (4, 6):
        cc = channels - 1
        alpha = samples[cc::channels]
        if cc == 1:
            color = samples[0::2]
        else:
            color = bytearray(npx * 3)
            for c in range(3):
                color[c::3] = samples[c::4]
    else:
        color = samples
        if trns:  # color key -> alpha (compared on the 8-bit samples)
            keyv = [struct.unpack(">H", trns[i:i + 2])[0] for i in range(0, 2 * channels, 2)]
            keyv = [(v >> 8) if depth == 16 else v * (255 // ((1 << depth) - 1)) for v in keyv]
            kb = bytes(keyv)
            alpha = bytearray(0 if color[i * channels:(i + 1) * channels] == kb else 255 for i in range(npx))
    smask = None
    if alpha is not None and alpha.count(255) != len(alpha):
        smask = PdfImage((key, "alpha"), w, h, "/DeviceGray", 8, zlib.compress(bytes(alpha)))
    space = "/DeviceRGB" if len(color) == npx * 3 else "/DeviceGray"
    return PdfImage(key, w, h, space, 8, zlib.compress(bytes(color)), smask=smask)


def _jpeg_components(data: bytes) -> int:
    i = 2
    while i + 9 < len(data):
        if data[i] != 0xFF:
            i += 1
            continue
        marker = data[i + 1]
        if marker in (0xD8, 0x01, 0xFF) or 0xD0 <= marker <= 0xD7:
            i += 1 if marker == 0xFF else 2
            continue
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            return data[i + 9]
        i += 2 + struct.unpack(">H", data[i + 2:i + 4])[0]
    raise ValueError("no JPEG frame header")


def _jpeg_image(data: bytes, key) -> PdfImage:
    size = image_size(data)
    if not size:
        raise ValueError("no JPEG size")
    comps = _jpeg_components(data)
    space = {1: "/DeviceGray", 3: "/DeviceRGB", 4: "/DeviceCMYK"}.get(comps)
    if space is None:
        raise ValueError(f"unsupported JPEG with {comps} components")
    return PdfImage(key, size[0], size[1], space, 8, data, filter="/DCTDecode",
                    decode="[1 0 1 0 1 0 1 0]" if comps == 4 else None)


def _rasterize(data: bytes, page: int, size, scale: float) -> bytes | None:
    """One PDF page as PNG through svg._pdf_to_png (poppler), or None."""
    try:
        from .svg import _pdf_to_png
        return _pdf_to_png(data, page, size, scale)
    except Exception:  # poppler missing or failing: draw without it
        return None


def _load_image(data: bytes | None, key, cache: dict, pdf_page: int = 1, size=(100.0, 100.0),
                scale: float = 3.0) -> PdfImage | None:
    """Image XObject for attachment bytes (cached by key); None when the
    format can't be embedded (GIF, HEIC, broken data)."""
    if key in cache:
        return cache[key]
    img = None
    if data:
        try:
            if data[:4] == b"%PDF":
                png = _rasterize(data, pdf_page, size, scale)
                img = _png_image(png, key) if png else None
            elif data[:8] == _PNG_SIG:
                img = _png_image(data, key)
            elif data[:3] == b"\xff\xd8\xff":
                img = _jpeg_image(data, key)
        except (ValueError, KeyError, IndexError, struct.error, zlib.error):
            img = None
    cache[key] = img
    return img


def _draw_image(cv: _Canvas, img: Image, att: dict, cache: dict, attachment: str | None = None) -> None:
    aid = attachment or img.attachment
    data = att.get(aid) if aid else None
    if data is None:
        return
    (cx, cy), (w, h) = img.center, img.size
    key = ("att", aid, round(w, 1), round(h, 1)) if data[:4] == b"%PDF" else ("att", aid)
    pimg = _load_image(data, key, cache, 1, (w, h), 3.0)
    if pimg is not None:
        cv.image(pimg, cx, cy, w, h, img.angle)


# --- pages -----------------------------------------------------------------------


def page_to_pdf_ops(doc: Document, page: Page, background: bool = True,
                    cache: dict | None = None) -> tuple[bytes, Resources]:
    """Content stream (uncompressed) for one page plus the Resources it
    names. The stream starts with the unit -> point / y-flip CTM, so it is
    meant for a MediaBox of [0 0 w*k h*k]. `cache` shares decoded images
    between pages."""
    cache = {} if cache is None else cache
    w, h = page.size
    k = PT_PER_UNIT
    res = Resources()
    cv = _Canvas(res)
    cv.op(_cm(k, 0, 0, -k, 0, h * k))
    att = doc.attachments
    if background:
        aid, pdf_page = page.background
        data = att.get(aid) if aid else None
        img = None
        if data is not None:
            img = _load_image(data, ("bg", aid, pdf_page, round(w, 1), round(h, 1)), cache, pdf_page, (w, h), 2.0)
        # white paper, then the background image (if it could be embedded) over it
        cv.op(f"q 1 1 1 rg 0 0 {_ns(w, h)} re f Q")
        if img is not None:
            cv.image(img, w / 2, h / 2, w, h)
    items = [i for i in page.items if not i.deleted]

    def draw(fn, *args) -> None:
        # each element is built apart and kept only if it rendered cleanly
        sub = _Canvas(res)
        try:
            fn(sub, *args)
        except (ValueError, IndexError, KeyError, TypeError, AttributeError, struct.error, zlib.error):
            return
        if sub.ops:
            cv.op("q")
            cv.ops += sub.ops
            cv.op("Q")

    # placed images and math sit under the ink
    for it in items:
        if it.kind == "image":
            draw(_draw_image, Image(it), att, cache)
        elif it.kind == "math":
            draw(_draw_image, Image(it), att, cache, it.body.str(4) or "")
    for it in items:
        kind = it.kind
        if kind == "stroke":
            draw(_draw_stroke, Stroke(it))
        elif kind == "box":
            draw(_draw_box, Box(it))
        elif kind == "sticky":
            draw(_draw_sticky, Sticky(it))
        elif kind == "fillshape":
            draw(_draw_fillshape, FillShape(it))
        elif kind == "line":
            draw(_draw_line, Line(it))
        elif kind in ("text", "text2"):
            draw(_legacy_text, it)
        elif kind == "textdoc":
            draw(_textdoc, it, page)
    return ("\n".join(cv.ops) + "\n").encode("latin-1"), res


# --- file writing ----------------------------------------------------------------


def _text_string(s: str) -> str:
    """PDF text string (document info): literal if ASCII, else UTF-16BE."""
    if all(32 <= ord(c) < 127 for c in s):
        return _pdf_string(s.encode("ascii"))
    return "<feff" + s.encode("utf-16-be").hex() + ">"


class _Writer:
    """Collects numbered objects and writes them with an xref table."""

    def __init__(self):
        self.objs: list[bytes | None] = []

    def reserve(self) -> int:
        self.objs.append(None)
        return len(self.objs)

    def set(self, num: int, body: str | bytes) -> int:
        self.objs[num - 1] = body.encode("latin-1") if isinstance(body, str) else body
        return num

    def add(self, body: str | bytes) -> int:
        return self.set(self.reserve(), body)

    def stream(self, head: str, data: bytes) -> int:
        """`head` is a dictionary without its Length (unless it has one)."""
        if "/Length" not in head:
            head = head[:-2].rstrip() + f" /Length {len(data)} >>"
        return self.add(head.encode("latin-1") + b"\nstream\n" + data + b"\nendstream")

    def output(self, root: int, info: int | None) -> bytes:
        out = bytearray(b"%PDF-1.4\n%\xe2\xe3\xcf\xd3\n")
        offsets = []
        for n, body in enumerate(self.objs, 1):
            offsets.append(len(out))
            out += f"{n} 0 obj\n".encode() + (body or b"null") + b"\nendobj\n"
        xref = len(out)
        out += f"xref\n0 {len(self.objs) + 1}\n0000000000 65535 f \n".encode()
        for off in offsets:
            out += f"{off:010d} 00000 n \n".encode()
        trailer = f"<< /Size {len(self.objs) + 1} /Root {root} 0 R"
        if info:
            trailer += f" /Info {info} 0 R"
        out += f"trailer\n{trailer} >>\nstartxref\n{xref}\n%%EOF\n".encode()
        return bytes(out)


def document_to_pdf(doc: Document, path_or_fileobj, pages: list[int] | None = None,
                    background: bool = True) -> None:
    """Writes `doc` as a vector PDF, one page per GoodNotes page in
    document order. `pages` picks 0-based page indexes (default: all);
    background=False leaves out the paper."""
    selected = list(doc.pages) if pages is None else [doc.pages[i] for i in pages]
    if not selected:
        raise ValueError("no pages to export")
    wr = _Writer()
    catalog, pages_ref = wr.reserve(), wr.reserve()
    fonts: dict[str, int] = {}
    images: dict[object, int] = {}
    cache: dict = {}
    kids = []

    def image_ref(img: PdfImage) -> int:
        if img.key not in images:
            sm = f"{image_ref(img.smask)} 0 R" if img.smask is not None else None
            images[img.key] = wr.add(img.dict(sm).encode("latin-1") + b"\nstream\n" + img.data + b"\nendstream")
        return images[img.key]

    k = PT_PER_UNIT
    for page in selected:
        content, res = page_to_pdf_ops(doc, page, background, cache)
        res_parts = ["/ProcSet [/PDF /Text /ImageB /ImageC /ImageI]"]
        if res.fonts:
            for base in res.fonts.values():
                if base not in fonts:
                    fonts[base] = wr.add(f"<< /Type /Font /Subtype /Type1 /BaseFont /{base} "
                                         f"/Encoding /WinAnsiEncoding >>")
            res_parts.append("/Font << " + " ".join(f"/{n} {fonts[b]} 0 R" for n, b in res.fonts.items()) + " >>")
        if res.ext_gstates:
            res_parts.append("/ExtGState << " + " ".join(
                f"/{n} << /Type /ExtGState /CA {_n(sa)} /ca {_n(fa)} /BM /{bm} >>"
                for n, (sa, fa, bm) in res.ext_gstates.items()) + " >>")
        if res.xobjects:
            res_parts.append("/XObject << " + " ".join(
                f"/{n} {image_ref(img)} 0 R" for n, img in res.xobjects.items()) + " >>")
        contents = wr.stream("<< /Filter /FlateDecode >>", zlib.compress(content, 6))
        w, h = page.size
        try:
            rot = doc.rotation(page) if page.page_id else 0
        except (ValueError, IndexError, KeyError, AttributeError):
            rot = 0
        rot = (round(rot / 90) * 90) % 360
        kids.append(wr.add(f"<< /Type /Page /Parent {pages_ref} 0 R /MediaBox [0 0 {_ns(w * k, h * k)}]"
                           + (f" /Rotate {rot}" if rot else "")
                           + f" /Resources << {' '.join(res_parts)} >> /Contents {contents} 0 R >>"))
    wr.set(pages_ref, f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in kids)}] /Count {len(kids)} >>")
    wr.set(catalog, f"<< /Type /Catalog /Pages {pages_ref} 0 R >>")
    try:
        title = doc.title
    except (ValueError, IndexError, KeyError, AttributeError):
        title = None
    info = wr.add("<< /Producer (goodnotes-codec)" + (f" /Title {_text_string(title)}" if title else "") + " >>")
    data = wr.output(catalog, info)
    if hasattr(path_or_fileobj, "write"):
        path_or_fileobj.write(data)
    else:
        with open(path_or_fileobj, "wb") as f:
            f.write(data)


def _parse_pages(spec: str) -> list[int]:
    """'0,2-5' -> [0, 2, 3, 4, 5]."""
    out = []
    for part in spec.split(","):
        part = part.strip()
        if not part:
            continue
        if "-" in part:
            a, b = part.split("-", 1)
            out += range(int(a), int(b) + 1)
        else:
            out.append(int(part))
    return out


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("doc")
    ap.add_argument("out")
    ap.add_argument("--pages", help="0-based pages, e.g. 0,2-5 (default: all)")
    ap.add_argument("--no-background", action="store_true", help="leave out the paper")
    a = ap.parse_args(argv)
    doc = Document(a.doc)
    try:
        pages = _parse_pages(a.pages) if a.pages else None
        document_to_pdf(doc, a.out, pages, not a.no_background)
    except (ValueError, IndexError) as e:
        ap.error(f"bad --pages: {e}" if a.pages else str(e))


if __name__ == "__main__":
    _main(sys.argv[1:])
