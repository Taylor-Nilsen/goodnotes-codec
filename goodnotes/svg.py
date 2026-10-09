"""GoodNotes page <-> SVG.

page_to_svg: renders every decoded element (all pen kinds, recognized
shapes, shapes/text boxes with rotation, sticky notes, lines/arrows with
dashes, images/stickers, math conversions, legacy text boxes, Text Docs)
plus the paper background.

svg_to_items: builds GoodNotes elements from an SVG: <path>/<polyline>/
<polygon> -> ink strokes (data-pen="fountain|brush|pencil|highlighter"
picks the pen), <line> -> line elements (marker-end/-start -> arrowheads,
stroke-dasharray -> dashes), <rect>/<ellipse>/<circle> -> shapes, <text>
(with <tspan> lines) -> text boxes, <image> -> placed images.

Usage:
    python -m goodnotes.svg export DOC.goodnotes PAGE OUT.svg
    python -m goodnotes.svg import TEMPLATE.goodnotes OUT.goodnotes IN.svg [--page N] [--keep]
"""
from __future__ import annotations

import base64
import html
import math
import re
import subprocess
import sys
import tempfile
import xml.etree.ElementTree as ET
from pathlib import Path

from . import schema as S
from .model import (DW_SCHEMA, PENCIL_SCHEMA, PSTROKE, VW_SCHEMA, Box, Document, FillShape, Image, Item,
                    LegacyText, Line, Math, Msg, Page, RichText, Sticky, Stroke, TextDoc, f32, is_constant_width,
                    new_box, new_brush_stroke, new_fountain_stroke, new_image, new_line,
                    new_pencil_stroke, new_stroke, read_color, read_point, rich_text, TextRun,
                    ARROW_FILLED, ARROW_OPEN)

# --- rendering ---------------------------------------------------------------


def _rgba(c) -> tuple[str, float]:
    r, g, b, a = c
    return f"rgb({round(r * 255)},{round(g * 255)},{round(b * 255)})", a


def _fmt(*xs) -> str:
    return " ".join(f"{x:.2f}" for x in xs)


def _mime(data: bytes) -> str | None:
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return "image/png"
    if data[:3] == b"\xff\xd8\xff":
        return "image/jpeg"
    if data[:4] == b"%PDF":
        return "application/pdf"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    return None


def _pdf_to_png(data: bytes, page: int, size: tuple[float, float], scale: float = 2.0) -> bytes | None:
    """Rasterizes one PDF page (poppler's pdftoppm); None if unavailable."""
    with tempfile.TemporaryDirectory() as d:
        src = Path(d) / "in.pdf"
        src.write_bytes(data)
        try:
            subprocess.run(["pdftoppm", "-png", "-f", str(page), "-l", str(page),
                            "-scale-to-x", str(int(size[0] * scale)), "-scale-to-y", str(int(size[1] * scale)),
                            str(src), str(Path(d) / "out")], check=True, capture_output=True)
        except (OSError, subprocess.CalledProcessError):
            return None
        out = next(Path(d).glob("out*.png"), None)
        return out.read_bytes() if out else None


def _data_uri(data: bytes, mime: str) -> str:
    return f"data:{mime};base64,{base64.b64encode(data).decode()}"


# outline command codes -> (op, float count), per schema (schema.VW_COMMAND / DW_COMMAND)
_OUTLINE_VW = {0: ("M", 2), 1: ("Q", 4), 2: ("C", 6), 3: ("A", 5), 4: ("M", 3), 5: ("Q", 5), 6: ("E", 7)}
_OUTLINE_DW = {2: ("M", 2), 3: ("Q", 4), 4: ("C", 6), 5: ("A", 5), 6: ("M", 3), 7: ("Q", 5), 8: ("E", 7)}


def _arc_path(cx, cy, r, a0, a1, clockwise, start_new, ry=None, rot=0.0):
    """Circular (or elliptical: rx=r, ry, rotation) arc as an SVG path
    piece. GoodNotes' clockwise flag means the angle decreases."""
    ry = r if ry is None else ry
    c, sn = math.cos(rot), math.sin(rot)

    def at(a):
        px, py = r * math.cos(a), ry * math.sin(a)
        return cx + c * px - sn * py, cy + sn * px + c * py
    (x0, y0), (x1, y1) = at(a0), at(a1)
    sweep = (a1 - a0) % (2 * math.pi) if not clockwise else (a0 - a1) % (2 * math.pi)
    large = 1 if sweep > math.pi else 0
    flag = 0 if clockwise else 1
    head = "M" if start_new else "L"
    return f"{head} {_fmt(x0, y0)} A {_fmt(r, ry)} {math.degrees(rot):.2f} {large} {flag} {_fmt(x1, y1)}"


def _outline_d(t, dx=0.0, dy=0.0) -> str:
    """SVG path for a variable-width stroke's precomputed outline."""
    if t.schema == DW_SCHEMA:
        table, v = _OUTLINE_DW, t.values[2:]
    else:
        table, v = _OUTLINE_VW, t.values[1:]
    counts, types = v[3], v[4]
    arrays, cw = {"M": v[5], "Q": v[6], "C": v[7], "A": v[8], "E": v[8]}, v[9]
    pos = {"M": 0, "Q": 0, "C": 0, "A": 0}
    ai = 0
    parts, have_point = [], False
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
            parts.append(f"M {_fmt(vals[0] + dx, vals[1] + dy)}")
            have_point = True
        elif op == "Q":
            parts.append(f"Q {_fmt(vals[0] + dx, vals[1] + dy, vals[2] + dx, vals[3] + dy)}")
        elif op == "C":
            parts.append(f"C {_fmt(vals[0] + dx, vals[1] + dy, vals[2] + dx, vals[3] + dy, vals[4] + dx, vals[5] + dy)}")
        else:
            clockwise = bool(cw[ai]) if ai < len(cw) else False
            ai += 1
            if op == "A":
                cx, cy, r, a0, a1 = vals
                parts.append(_arc_path(cx + dx, cy + dy, r, a0, a1, clockwise, not have_point))
            else:  # elliptical (marker pen's chisel nib): cx, cy, rx, ry, start, end, rotation
                cx, cy, rx, ry, a0, a1, rot = vals
                parts.append(_arc_path(cx + dx, cy + dy, abs(rx), a0, a1, clockwise, not have_point, abs(ry), rot))
            have_point = True
    return " ".join(parts) + " Z" if parts else ""


def _pencil_d(t, dx=0.0, dy=0.0) -> tuple[str, float]:
    """Centerline path + average width for the pencil schema."""
    th = f32(t.values[1])
    kinds, moves, quads = t.values[2], t.values[3], t.values[4]
    parts, widths, mi, qi = [], [], 0, 0
    for k in kinds:
        if k == 0 and mi < len(moves):
            m = [f32(x) for x in moves[mi]]
            parts.append(f"M {_fmt(m[0] + dx, m[1] + dy)}")
            widths.append(m[4])  # force
            mi += 1
        elif qi < len(quads):
            q = [f32(x) for x in quads[qi]]
            parts.append(f"Q {_fmt(q[1] + dx, q[2] + dy, q[6] + dx, q[7] + dy)}")  # q[0] = texture seed
            widths.append(q[10])
            qi += 1
    w = th * 2 * (0.4 + (sum(widths) / len(widths) if widths else 0.6))
    return " ".join(parts), max(w, 0.5)


def _stroke_dash(s: Stroke, width: float) -> str:
    d = s.dash
    if not d or not d[0]:
        return ""
    return ' stroke-dasharray="' + " ".join(f"{max(v * width, 0.01):.2f}" for v in d[0]) + '"'


def _stroke_svg(s: Stroke) -> str:
    color, alpha = _rgba(s.color)
    dx, dy = s.translation
    blend = ' style="mix-blend-mode:multiply"' if s.highlighter else ""
    shape = s.shape
    if shape is not None:
        w = shape.float(15) or 2.0
        common = (f'fill="none" stroke="{color}" stroke-opacity="{alpha:.3f}" stroke-width="{w:.2f}" '
                  f'stroke-linejoin="round" stroke-linecap="round"{blend}')
        if shape.sub(3) is not None:  # rectangle: center, size
            (cx, cy), (rw, rh) = read_point(shape.path(3, 1)), read_point(shape.path(3, 2))
            x, y = cx - rw / 2 + dx, cy - rh / 2 + dy
            return f'<rect x="{x:.2f}" y="{y:.2f}" width="{rw:.2f}" height="{rh:.2f}" {common}/>'
        if shape.sub(4) is not None:  # ellipse
            e = shape.sub(4)
            (cx, cy), (ew, eh), ang = read_point(e.sub(1)), read_point(e.sub(2)), e.float(3)
            cx, cy = cx + dx, cy + dy
            return (f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{ew:.2f}" ry="{eh:.2f}" '
                    f'transform="rotate({math.degrees(ang):.3f} {cx:.2f} {cy:.2f})" {common}/>')
        poly = shape.sub(1)
        pts = [read_point(p) for p in poly.subs(1)] if poly is not None else []
        if pts:
            return f'<polyline points="{" ".join(_fmt(p[0] + dx, p[1] + dy) for p in pts)}" {common}/>'
    t = s.template
    if is_constant_width(t.schema):
        d = []
        for c in s.commands:
            d.append(f"{c[0]} " + " ".join(_fmt(p[0] + dx, p[1] + dy) for p in c[1:]))
        if not d:
            return ""
        w = f32(t.values[1])
        return (f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-opacity="{alpha:.3f}" '
                f'stroke-width="{w:.2f}" stroke-linecap="{"butt" if s.highlighter else "round"}" '
                f'stroke-linejoin="round"{_stroke_dash(s, w)}{blend}/>')
    if t.schema in (VW_SCHEMA, DW_SCHEMA):
        d = _outline_d(t, dx, dy)
        return f'<path d="{d}" fill="{color}" fill-opacity="{alpha:.3f}" fill-rule="nonzero"{blend}/>' if d else ""
    if t.schema == PENCIL_SCHEMA:
        d, w = _pencil_d(t, dx, dy)
        return (f'<path d="{d}" fill="none" stroke="{color}" stroke-opacity="{alpha:.3f}" '
                f'stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round"{blend}/>') if d else ""
    return f"<!-- unrendered stroke schema {html.escape(t.schema)} -->"


def _text_svg(rt: RichText | None, x: float, y: float, width: float, default_size: float = 24.0,
              default_font: str = "Helvetica Neue", default_align: str = "left",
              default_color=(0, 0, 0, 1)) -> str:
    """Lays rich text out in a box `width` wide, wrapping long lines the
    way the box wraps in GoodNotes (average glyph width 0.5 em)."""
    if rt is None:
        return ""
    out, ly = [], y
    for n, para in enumerate(rt.paragraphs()):
        first = para[0] if para else None
        if first and first.list_style:
            marker = "• " if first.list_style == "bullet" else f"{n + 1}. "
            para = [TextRun(marker, size=first.size, color=first.color)] + para
        for line in _wrap(para, width, default_size):
            size = max((r.size or default_size for r in line), default=default_size)
            h = size * (first.line_height if first and first.line_height else 1.2)
            ly += h
            out.append(_text_line(line, first, x, ly - h * 0.25, width, default_size, default_font,
                                  default_align, default_color))
    return "".join(out)


def _wrap(para, width, default_size):
    """Greedy word wrap of a paragraph's runs at `width` page units."""
    lines, cur, cur_w = [], [], 0.0
    for r in para:
        size = r.size or default_size
        for w in re.split(r"(\s+)", r.text):
            if not w:
                continue
            ww = 0.5 * size * len(w)
            if cur and cur_w + ww > width and not w.isspace():
                lines.append(cur)
                cur, cur_w = [], 0.0
            piece = TextRun.__new__(TextRun)
            piece.__dict__.update(r.__dict__)
            piece.text = w
            if cur and cur[-1].__dict__ | {"text": None} == piece.__dict__ | {"text": None}:
                cur[-1].text += w
            else:
                cur.append(piece)
            cur_w += ww
    lines.append(cur)
    return lines


def _text_line(para, first, x, baseline, width, default_size, default_font, default_align, default_color):
    align = (first.align if first and first.align else default_align)
    spans = []
    for r in para:
        col, al = _rgba(r.color if r.color is not None else default_color)
        deco = " ".join(k for k, on in (("underline", r.underline), ("line-through", r.strike)) if on)
        sz = r.size or default_size
        if r.heading:
            sz *= {1: 1.6, 2: 1.4, 3: 1.2}.get(r.heading, 1.1)
        style = f"font-style:{'italic' if r.italic else 'normal'};font-weight:{'bold' if r.bold or r.heading else 'normal'}"
        if deco:
            style += f";text-decoration:{deco}"
        if r.highlight:
            hc, ha = _rgba(r.highlight)
            style += f";background:{hc}"
        span = (f'<tspan font-family="{html.escape(r.font or default_font)}" font-size="{sz:.1f}" '
                f'fill="{col}" fill-opacity="{al:.3f}" style="{style}">{html.escape(r.text)}</tspan>')
        if r.link:
            span = f'<a href="{html.escape(r.link)}">{span}</a>'
        spans.append(span)
    if not spans:
        return ""
    anchor = {"left": "start", "center": "middle", "right": "end", "justify": "start"}[align]
    tx = x + (width / 2 if align == "center" else width if align == "right" else 0)
    tx += (first.indent or 0) * 24 if first else 0
    return f'<text x="{tx:.2f}" y="{baseline:.2f}" text-anchor="{anchor}" xml:space="preserve">{"".join(spans)}</text>'


def _rotate(angle: float, cx: float, cy: float) -> str:
    return f' transform="rotate({math.degrees(angle):.3f} {cx:.2f} {cy:.2f})"' if angle else ""


def _box_svg(b: Box) -> str:
    (x, y), (w, h) = b.origin, b.size
    t = b.b.sub(32)
    if b.sizing == "auto":  # auto-sized text box: wraps at its max width
        mw = b.b.path(21, 3).float(2)
        measured = read_point(t.sub(2)) if t is not None and t.sub(2) is not None else (0.0, 0.0)
        w = mw if 0 < mw < float("inf") else (measured[0] + 20 if measured[0] else 400.0)
        h = measured[1] + 20 if measured[1] else h
    fill = b.fill
    outline = b.outline
    fill_attr = f'fill="{_rgba(fill)[0]}" fill-opacity="{fill[3]:.3f}"' if fill else 'fill="none"'
    if outline and outline[1] is not None and outline[0] > 0:
        col, a = _rgba(outline[1])
        stroke_attr = f'stroke="{col}" stroke-opacity="{a:.3f}" stroke-width="{outline[0]:.2f}"' + _dash(b.b.sub(31), outline[0])
    else:
        stroke_attr = 'stroke="none"'
    shadow = b.shadow
    filt = ""
    if shadow and shadow[1] > 0:
        sc, sa = _rgba(shadow[0])
        filt = (f'<filter id="sh{b.id}" x="-20%" y="-20%" width="140%" height="140%">'
                f'<feDropShadow dx="{shadow[2][0]:.2f}" dy="{shadow[2][1]:.2f}" stdDeviation="{shadow[1] / 2:.2f}" '
                f'flood-color="{sc}" flood-opacity="{sa:.3f}"/></filter>')
    filt_attr = f' filter="url(#sh{b.id})"' if filt else ""
    out = [filt, f'<g{_rotate(b.rotation, x + w / 2, y + h / 2)}>']
    geo = b.geometry
    if geo == "ellipse":
        out.append(f'<ellipse cx="{x + w / 2:.2f}" cy="{y + h / 2:.2f}" rx="{w / 2:.2f}" ry="{h / 2:.2f}" {fill_attr} {stroke_attr}{filt_attr}/>')
    elif geo == "polygon" and b.vertices:
        pts = " ".join(_fmt(x + vx * w, y + vy * h) for vx, vy in b.vertices)
        out.append(f'<polygon points="{pts}" {fill_attr} {stroke_attr} stroke-linejoin="round"{filt_attr}/>')
    elif fill or "stroke=\"none\"" not in stroke_attr:
        r = min(b.corner_radius, w / 2, h / 2)  # GoodNotes clamps like this (a pill is a huge radius)
        out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{r:.2f}" ry="{r:.2f}" {fill_attr} {stroke_attr}{filt_attr}/>')
    pad = b.padding
    attrs = t.path(5, 1) if t is not None else None
    size = attrs.float(40) if attrs is not None else 24.0
    para = t.path(5, 2) if t is not None else None
    align = {0: "left", 1: "center", 2: "right", 3: "justify"}.get(para.int(4), "left") if para is not None else "left"
    out.append(_text_svg(b.text, x + pad, y + pad * 0.5, max(w - 2 * pad, 1), size or 24.0, default_align=align))
    out.append("</g>")
    return "".join(out)


def _fillshape_svg(f: FillShape) -> str:
    sh = f.shape
    if sh is None:
        return ""
    col, a = _rgba(f.color)
    dx, dy = f.translation
    attrs = f'fill="{col}" fill-opacity="{a:.3f}"'
    if sh.sub(3) is not None:
        (cx, cy), (w, h) = read_point(sh.path(3, 1)), read_point(sh.path(3, 2))
        return f'<rect x="{cx - w / 2 + dx:.2f}" y="{cy - h / 2 + dy:.2f}" width="{w:.2f}" height="{h:.2f}" {attrs}/>'
    if sh.sub(4) is not None:
        e = sh.sub(4)
        (cx, cy), (rx, ry), ang = read_point(e.sub(1)), read_point(e.sub(2)), e.float(3)
        cx, cy = cx + dx, cy + dy
        return (f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{rx:.2f}" ry="{ry:.2f}" '
                f'transform="rotate({math.degrees(ang):.3f} {cx:.2f} {cy:.2f})" {attrs}/>')
    poly = sh.sub(1)
    pts = [read_point(p) for p in poly.subs(1)] if poly is not None else []
    return f'<polygon points="{" ".join(_fmt(p[0] + dx, p[1] + dy) for p in pts)}" {attrs}/>' if pts else ""


def _sticky_svg(s: Sticky) -> str:
    (x, y), (w, h) = s.origin, s.size
    col, a = _rgba(s.color)
    head = ""
    if s.show_author and s.author[1]:
        head = f'<text x="{x + 10:.2f}" y="{y + 16:.2f}" font-family="Helvetica Neue" font-size="11" fill="#555">{html.escape(s.author[1])}</text>'
    return (f'<g{_rotate(s.rotation, x + w / 2, y + h / 2)}><rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" '
            f'fill="{col}" fill-opacity="{a:.3f}"/>{head}' + _text_svg(s.text, x + 10, y + (18 if head else 4), w - 20, 24.0) + "</g>")


def _line_svg(ln: Line, marker_id: str) -> str:
    pts = ln.points
    if len(pts) < 2:
        return ""
    col, a = _rgba(ln.color)
    start, end = ln.start, ln.end
    if ln.elbow:
        knee = ln.knee
        g = ln.b.sub(21)
        vertical = g is not None and g.int(4) == 1
        if knee is not None:
            d = (f"M {_fmt(*start)} V {knee[1]:.2f} H {end[0]:.2f} V {end[1]:.2f}" if vertical else
                 f"M {_fmt(*start)} H {knee[0]:.2f} V {end[1]:.2f} H {end[0]:.2f}")
        else:
            d = f"M {_fmt(*start)} H {end[0]:.2f} V {end[1]:.2f}"
    elif ln.mid is not None:
        mid = ln.mid
        ctrl = (2 * mid[0] - (start[0] + end[0]) / 2, 2 * mid[1] - (start[1] + end[1]) / 2)
        d = f"M {_fmt(*start)} Q {_fmt(*ctrl)} {_fmt(*end)}"
    else:
        d = f"M {_fmt(*start)} L {_fmt(*end)}"
    marker = ""
    if ln.arrow:
        marker += f' marker-end="url(#{marker_id}{ln.arrow})"'
    if ln.start_arrow:
        marker += f' marker-start="url(#{marker_id}{ln.start_arrow})"'
    return (f'<path d="{d}" fill="none" stroke="{col}" stroke-opacity="{a:.3f}" stroke-width="{ln.width or 2:.2f}" '
            f'stroke-linecap="round" stroke-linejoin="round"{_dash(ln.b.sub(32), ln.width)}{marker}/>')


def _dash(style, width) -> str:
    """stroke-dasharray from a {2 {2 {1 dash, 2 gap}}} style (multiples of width)."""
    pat = style.path(2, 2) if style is not None and style.sub(2) is not None else None
    if pat is None:
        return ""
    w = width or 1.0
    return f' stroke-dasharray="{max(pat.float(1) * w, 0.01):.2f} {pat.float(2) * w:.2f}"'


def _image_svg(img, data: bytes | None, page: int = 1) -> str:
    if data is None:
        return ""
    cx, cy = img.center
    w, h = img.size
    mime = _mime(data)
    if mime == "application/pdf":
        data = _pdf_to_png(data, page, (w, h), 3.0)
        mime = "image/png" if data else None
    if mime is None:
        return f"<!-- image {img.attachment}: unknown format -->"
    return (f'<image x="{cx - w / 2:.2f}" y="{cy - h / 2:.2f}" width="{w:.2f}" height="{h:.2f}" '
            f'preserveAspectRatio="none" href="{_data_uri(data, mime)}"{_rotate(img.angle, cx, cy)}/>')


def _legacy_text_svg(item: Item) -> str:
    v = LegacyText(item)
    (x, y) = v.origin
    lines = "".join(f'<tspan x="{x:.2f}" dy="1.2em">{html.escape(l)}</tspan>' for l in v.text.split("\n"))
    return f'<text x="{x:.2f}" y="{y:.2f}" font-family="Helvetica" font-size="16">{lines}</text>'


def _textdoc_svg(item: Item, page: Page) -> str:
    txt = TextDoc(item).plain_text
    if not txt:
        return ""
    w, _ = page.size
    words, lines, cur = txt.split(), [], ""
    for wd in words:
        if len(cur) + len(wd) > w / 9:
            lines.append(cur)
            cur = wd
        else:
            cur = (cur + " " + wd).strip()
    lines.append(cur)
    spans = "".join(f'<tspan x="40" dy="1.4em">{html.escape(l)}</tspan>' for l in lines)
    return f'<text x="40" y="40" font-family="Helvetica Neue" font-size="16">{spans}</text>'


def page_to_svg(doc: Document, page: Page, background: bool = True) -> str:
    w, h = page.size
    att = doc.attachments
    body = []
    if background:
        aid, pdf_page = page.background
        data = att.get(aid) if aid else None
        if data is not None:
            mime = _mime(data)
            if mime == "application/pdf":
                png = _pdf_to_png(data, pdf_page, (w, h))
                if png:
                    body.append(f'<image x="0" y="0" width="{w:.2f}" height="{h:.2f}" preserveAspectRatio="none" href="{_data_uri(png, "image/png")}"/>')
                else:
                    body.append(f'<rect width="{w:.2f}" height="{h:.2f}" fill="white"/>')
            elif mime:
                body.append(f'<image x="0" y="0" width="{w:.2f}" height="{h:.2f}" preserveAspectRatio="none" href="{_data_uri(data, mime)}"/>')
        else:
            body.append(f'<rect width="{w:.2f}" height="{h:.2f}" fill="white"/>')
    items = [i for i in page.items if not i.deleted]
    # placed images and widgets sit under the ink
    for it in items:
        if it.kind == "image":
            img = it.typed()
            body.append(_image_svg(img, att.get(img.attachment)))
        elif it.kind == "math":
            m = it.typed()
            body.append(_image_svg(Image(it), att.get(m.b.str(4) or "")))
    for it in items:
        k = it.kind
        try:
            if k == "stroke":
                body.append(_stroke_svg(it.typed()))
            elif k == "box":
                body.append(_box_svg(it.typed()))
            elif k == "sticky":
                body.append(_sticky_svg(it.typed()))
            elif k == "fillshape":
                body.append(_fillshape_svg(it.typed()))
            elif k == "line":
                body.append(_line_svg(it.typed(), "arrow"))
            elif k in ("text", "text2"):
                body.append(_legacy_text_svg(it))
            elif k == "textdoc":
                body.append(_textdoc_svg(it, page))
        except (ValueError, IndexError, KeyError) as e:
            body.append(f"<!-- {k} {it.id}: {html.escape(str(e))} -->")
    defs = ('<defs><marker id="arrow1" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" '
            'orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M 0 0 L 10 5 L 0 10" fill="none" '
            'stroke="context-stroke" stroke-width="1.5"/></marker>'
            '<marker id="arrow2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" '
            'orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M 0 0 L 10 5 L 0 10 Z" '
            'fill="context-stroke"/></marker></defs>')
    rot = doc.rotation(page) if page.page_id else 0
    vw, vh = (h, w) if rot in (90, 270) else (w, h)
    g_open = f'<g transform="rotate({rot} {w / 2:.2f} {h / 2:.2f}) translate({(w - vw) / 2:.2f} {(h - vh) / 2:.2f})">' if rot else ""
    g_close = "</g>" if rot else ""
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {vw:.2f} {vh:.2f}" width="{vw:.0f}" height="{vh:.0f}">{defs}{g_open}{"".join(body)}{g_close}</svg>')


# --- SVG -> GoodNotes --------------------------------------------------------

_NUM = r"[-+]?(?:\d*\.\d+|\d+\.?)(?:[eE][-+]?\d+)?"
_TOKEN = re.compile(rf"([MmLlHhVvCcSsQqTtAaZz])|({_NUM})")


def _cubic_to_quads(p0, p1, p2, p3, n=4):
    """Approximates a cubic Bezier with n quadratics (GoodNotes ink is
    quadratic-only)."""
    def at(t):
        mt = 1 - t
        return tuple(mt**3 * a + 3 * mt * mt * t * b + 3 * mt * t * t * c + t**3 * d
                     for a, b, c, d in zip(p0, p1, p2, p3))

    def d_at(t):
        mt = 1 - t
        return tuple(3 * mt * mt * (b - a) + 6 * mt * t * (c - b) + 3 * t * t * (d - c)
                     for a, b, c, d in zip(p0, p1, p2, p3))

    out = []
    for i in range(n):
        t0, t1 = i / n, (i + 1) / n
        a, b = at(t0), at(t1)
        da, db = d_at(t0), d_at(t1)
        hh = (t1 - t0) / 2
        c = tuple(((a[k] + da[k] * hh) + (b[k] - db[k] * hh)) / 2 for k in range(2))
        out.append((c, b))
    return out


def _arc_to_cubics(p0, rx, ry, phi, large, sweep, p1):
    """SVG elliptical arc -> cubic Beziers (standard endpoint conversion)."""
    if rx == 0 or ry == 0 or p0 == p1:
        return [(p0, p1, p1)]
    cphi, sphi = math.cos(phi), math.sin(phi)
    dx, dy = (p0[0] - p1[0]) / 2, (p0[1] - p1[1]) / 2
    x1p, y1p = cphi * dx + sphi * dy, -sphi * dx + cphi * dy
    rx, ry = abs(rx), abs(ry)
    lam = x1p ** 2 / rx ** 2 + y1p ** 2 / ry ** 2
    if lam > 1:
        rx, ry = rx * math.sqrt(lam), ry * math.sqrt(lam)
    num = rx * rx * ry * ry - rx * rx * y1p * y1p - ry * ry * x1p * x1p
    den = rx * rx * y1p * y1p + ry * ry * x1p * x1p
    co = math.sqrt(max(0.0, num / den)) * (-1 if large == sweep else 1)
    cxp, cyp = co * rx * y1p / ry, -co * ry * x1p / rx
    cx = cphi * cxp - sphi * cyp + (p0[0] + p1[0]) / 2
    cy = sphi * cxp + cphi * cyp + (p0[1] + p1[1]) / 2

    def ang(ux, uy, vx, vy):
        return math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)

    t1 = ang(1, 0, (x1p - cxp) / rx, (y1p - cyp) / ry)
    dt = ang((x1p - cxp) / rx, (y1p - cyp) / ry, (-x1p - cxp) / rx, (-y1p - cyp) / ry)
    if not sweep and dt > 0:
        dt -= 2 * math.pi
    elif sweep and dt < 0:
        dt += 2 * math.pi
    segs = max(1, math.ceil(abs(dt) / (math.pi / 2)))
    delta = dt / segs
    k = 4 / 3 * math.tan(delta / 4)
    out = []

    def pt(t):
        x, y = rx * math.cos(t), ry * math.sin(t)
        return (cphi * x - sphi * y + cx, sphi * x + cphi * y + cy)

    def dpt(t):
        x, y = -rx * math.sin(t), ry * math.cos(t)
        return (cphi * x - sphi * y, sphi * x + cphi * y)

    for i in range(segs):
        a, b = t1 + i * delta, t1 + (i + 1) * delta
        pa, pb, da, db = pt(a), pt(b), dpt(a), dpt(b)
        out.append(((pa[0] + k * da[0], pa[1] + k * da[1]), (pb[0] - k * db[0], pb[1] - k * db[1]), pb))
    return out


def parse_path(d: str) -> list[tuple]:
    """SVG path data -> [('M', p) | ('L', p) | ('Q', c, p)] (absolute)."""
    toks = [(c, float(n) if n else None) for c, n in _TOKEN.findall(d)]
    out, i, op = [], 0, None
    cur = start = (0.0, 0.0)
    last_c = last_q = None

    def take(k):
        nonlocal i
        vals = [toks[i + j][1] for j in range(k)]
        i += k
        return vals

    while i < len(toks):
        if toks[i][0]:
            op = toks[i][0]
            i += 1
            if op in "Zz":
                if cur != start:
                    out.append(("L", start))
                cur, last_c, last_q = start, None, None
                continue
        if op is None:
            break
        rel = op.islower()
        ox, oy = cur if rel else (0.0, 0.0)
        o = op.upper()
        if o == "M":
            x, y = take(2)
            cur = start = (ox + x, oy + y)
            out.append(("M", cur))
            op = "l" if rel else "L"
            last_c = last_q = None
        elif o in "LHV":
            if o == "L":
                x, y = take(2)
                e = (ox + x, oy + y)
            elif o == "H":
                e = (take(1)[0] + ox, cur[1])
            else:
                e = (cur[0], take(1)[0] + oy)
            out.append(("L", e))
            cur, last_c, last_q = e, None, None
        elif o in "QT":
            if o == "Q":
                cx, cy, x, y = take(4)
                c = (ox + cx, oy + cy)
            else:
                x, y = take(2)
                c = (2 * cur[0] - last_q[0], 2 * cur[1] - last_q[1]) if last_q else cur
            e = (ox + x, oy + y)
            out.append(("Q", c, e))
            cur, last_q, last_c = e, c, None
        elif o in "CS":
            if o == "C":
                x1, y1, x2, y2, x, y = take(6)
                c1 = (ox + x1, oy + y1)
            else:
                x2, y2, x, y = take(4)
                c1 = (2 * cur[0] - last_c[0], 2 * cur[1] - last_c[1]) if last_c else cur
            c2, e = (ox + x2, oy + y2), (ox + x, oy + y)
            out += [("Q", qc, qe) for qc, qe in _cubic_to_quads(cur, c1, c2, e)]
            cur, last_c, last_q = e, c2, None
        elif o == "A":
            rx, ry, rot, large, sweep, x, y = take(7)
            e = (ox + x, oy + y)
            for c1, c2, pe in _arc_to_cubics(cur, rx, ry, math.radians(rot), int(large), int(sweep), e):
                p0 = out[-1][-1] if out else cur
                out += [("Q", qc, qe) for qc, qe in _cubic_to_quads(p0, c1, c2, pe, 2)]
            cur, last_c, last_q = e, None, None
    return out


def _parse_color(s: str | None, default=(0, 0, 0, 1)):
    s = (s or "").strip().lower()
    if not s or s == "none":
        return None if s == "none" else default
    if re.fullmatch(r"#[0-9a-f]{6}", s):
        return tuple(int(s[k:k + 2], 16) / 255 for k in (1, 3, 5)) + (1.0,)
    if re.fullmatch(r"#[0-9a-f]{8}", s):
        return tuple(int(s[k:k + 2], 16) / 255 for k in (1, 3, 5, 7))
    if re.fullmatch(r"#[0-9a-f]{3}", s):
        return tuple(int(c * 2, 16) / 255 for c in s[1:]) + (1.0,)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", s)
    if m:
        p = [float(v.strip().rstrip("%")) for v in re.split(r"[,\s/]+", m.group(1).strip()) if v.strip()]
        return (p[0] / 255, p[1] / 255, p[2] / 255, p[3] if len(p) > 3 else 1.0)
    named = {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 0.5, 0),
             "blue": (0, 0, 1), "yellow": (1, 1, 0), "orange": (1, 0.647, 0), "purple": (0.5, 0, 0.5),
             "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5), "cyan": (0, 1, 1), "magenta": (1, 0, 1),
             "pink": (1, 0.75, 0.8), "brown": (0.65, 0.16, 0.16), "lime": (0, 1, 0), "navy": (0, 0, 0.5)}
    return named[s] + (1.0,) if s in named else default


def _matrix(t: str | None):
    """SVG transform attribute -> affine (a, b, c, d, e, f)."""
    m = (1.0, 0.0, 0.0, 1.0, 0.0, 0.0)
    for name, args in re.findall(r"(\w+)\s*\(([^)]*)\)", t or ""):
        v = [float(x) for x in re.findall(_NUM, args)]
        if name == "translate":
            n = (1, 0, 0, 1, v[0], v[1] if len(v) > 1 else 0)
        elif name == "scale":
            n = (v[0], 0, 0, v[1] if len(v) > 1 else v[0], 0, 0)
        elif name == "rotate":
            a = math.radians(v[0])
            cx, cy = (v[1], v[2]) if len(v) > 2 else (0, 0)
            ca, sa = math.cos(a), math.sin(a)
            n = (ca, sa, -sa, ca, cx - ca * cx + sa * cy, cy - sa * cx - ca * cy)
        elif name == "matrix":
            n = tuple(v[:6])
        elif name == "skewx":
            n = (1, 0, math.tan(math.radians(v[0])), 1, 0, 0)
        elif name == "skewy":
            n = (1, math.tan(math.radians(v[0])), 0, 1, 0, 0)
        else:
            continue
        m = _mult(m, n)
    return m


def _mult(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


def _apply(m, p):
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def _num(s, default=0.0) -> float:
    m = re.match(_NUM, (s or "").strip())
    return float(m.group()) if m else default


def svg_to_items(doc: Document, svg_path: str, offset=(0.0, 0.0), scale=1.0,
                 pen: str = "ballpoint") -> list[Item]:
    """Builds GoodNotes items from an SVG (1 user unit = `scale` page
    units). Group transforms and style inheritance are honored. `pen`
    (ballpoint/fountain/brush/pencil/highlighter) is the default pen for
    paths; a `data-pen` attribute overrides it per element."""
    root = ET.parse(svg_path).getroot()
    base = (scale, 0.0, 0.0, scale, offset[0], offset[1])
    svg_dir = Path(svg_path).parent
    items: list[Item] = []
    styles = _stylesheet(root)

    def style_of(el, inherited):
        st = dict(inherited)
        cls = (el.get("class") or "").split()
        for sel in ["*"] + [f".{c}" for c in cls] + [f"#{el.get('id')}"] + [el.tag.split("}")[-1]]:
            st.update(styles.get(sel, {}))
        for k in ("fill", "stroke", "stroke-width", "font-size", "font-family", "font-weight", "opacity",
                  "fill-opacity", "stroke-opacity", "stroke-dasharray", "font-style", "text-decoration",
                  "text-anchor", "marker-end", "marker-start", "data-pen"):
            if el.get(k) is not None:
                st[k] = el.get(k)
        for kv in (el.get("style") or "").split(";"):
            if ":" in kv:
                k, v = kv.split(":", 1)
                st[k.strip()] = v.strip()
        return st

    def ink(cmds, color, width, st):
        kind = st.get("data-pen", pen)
        if kind == "fountain":
            return new_fountain_stroke(doc, cmds, color, width)
        if kind == "brush":
            return new_brush_stroke(doc, cmds, color, width)
        if kind == "pencil":
            return new_pencil_stroke(doc, cmds, color, width)
        dash = None
        if st.get("stroke-dasharray") not in (None, "none", ""):
            lens = [float(x) for x in re.findall(_NUM, st["stroke-dasharray"])]
            dash = [max(v / max(width, 0.01), 0.0) for v in lens]
        return new_stroke(doc, cmds, color, width, highlighter=kind == "highlighter", dash=dash)

    def walk(el, m, st):
        tag = el.tag.split("}")[-1]
        if tag in ("defs", "clipPath", "mask", "marker", "symbol", "style", "title", "desc", "metadata"):
            return
        m = _mult(m, _matrix(el.get("transform")))
        st = style_of(el, st)
        k = math.sqrt(abs(m[0] * m[3] - m[1] * m[2]))
        stroke = _parse_color(st.get("stroke"), None) if st.get("stroke") not in (None, "none") else None
        fill = _parse_color(st.get("fill"), (0, 0, 0, 1)) if st.get("fill", "black") != "none" else None
        for o, key in (("stroke-opacity", "s"), ("fill-opacity", "f"), ("opacity", "both")):
            if st.get(o):
                a = float(st[o])
                if key in ("s", "both") and stroke:
                    stroke = (*stroke[:3], stroke[3] * a)
                if key in ("f", "both") and fill:
                    fill = (*fill[:3], fill[3] * a)
        sw = _num(st.get("stroke-width", "1"), 1.0) * k
        num = lambda name, d=0.0: _num(el.get(name), d)

        if tag == "line":
            p1, p2 = _apply(m, (num("x1"), num("y1"))), _apply(m, (num("x2"), num("y2")))
            if stroke:
                dash = None
                if st.get("stroke-dasharray") not in (None, "none", ""):
                    lens = [float(x) for x in re.findall(_NUM, st["stroke-dasharray"])]
                    if lens:
                        dash = (lens[0] / max(sw, 0.01), (lens[1] if len(lens) > 1 else lens[0]) / max(sw, 0.01))
                arrow = _marker(st.get("marker-end"))
                items.append(new_line(doc, p1, p2, color=stroke, width=max(sw, 0.5), arrow=arrow,
                                      start_arrow=_marker(st.get("marker-start")), dash=dash))
            return
        if tag in ("path", "polyline", "polygon"):
            if tag == "path":
                cmds = parse_path(el.get("d", ""))
            else:
                pts = [float(x) for x in re.findall(_NUM, el.get("points", ""))]
                pairs = list(zip(pts[::2], pts[1::2]))
                cmds = [("M", pairs[0])] + [("L", p) for p in pairs[1:]] if pairs else []
                if tag == "polygon" and pairs:
                    cmds.append(("L", pairs[0]))
            cmds = [(c[0], *[_apply(m, p) for p in c[1:]]) for c in cmds]
            if tag == "polygon" and fill and not stroke and cmds:
                pts = [c[-1] for c in cmds[:-1]]
                xs, ys = [p[0] for p in pts], [p[1] for p in pts]
                x0, y0, w, h = min(xs), min(ys), max(xs) - min(xs) or 1, max(ys) - min(ys) or 1
                items.append(new_box(doc, (x0, y0), (w, h), fill=fill,
                                     vertices=[((x - x0) / w, (y - y0) / h) for x, y in pts]))
                return
            color = stroke or fill  # an unstroked filled path still becomes visible ink
            if cmds and color:
                items.append(ink(cmds, color, max(sw if stroke else 1.0, 0.1), st))
        elif tag in ("rect", "circle", "ellipse"):
            if tag == "rect":
                x, y, w, h = num("x"), num("y"), num("width"), num("height")
                rr = num("rx", 0) * k
                ellipse = False
            else:
                cx, cy = num("cx"), num("cy")
                rx = num("r") if tag == "circle" else num("rx")
                ry = num("r") if tag == "circle" else num("ry")
                x, y, w, h = cx - rx, cy - ry, 2 * rx, 2 * ry
                rr = 0
                ellipse = True
            (x0, y0), (x1, y1) = _apply(m, (x, y)), _apply(m, (x + w, y + h))
            angle = math.atan2(m[1], m[0])
            dash = None
            if st.get("stroke-dasharray") not in (None, "none", "") and stroke:
                lens = [float(v) for v in re.findall(_NUM, st["stroke-dasharray"])]
                if lens:
                    dash = (lens[0] / max(sw, 0.01), (lens[1] if len(lens) > 1 else lens[0]) / max(sw, 0.01))
            cx, cy = (x0 + x1) / 2, (y0 + y1) / 2
            bw, bh = w * k, h * k
            items.append(new_box(doc, (cx - bw / 2, cy - bh / 2), (bw, bh),
                                 fill=fill, outline=(sw, stroke, dash) if stroke else None,
                                 corner_radius=rr or None, ellipse=ellipse, rotation=angle))
        elif tag == "text":
            runs = _text_runs(el, st, k, fill)
            if not runs:
                return
            size = max(r.size or 16 for r in runs)
            x, y = _apply(m, (num("x"), num("y")))
            text = "".join(r.text for r in runs)
            longest = max(len(l) for l in text.split("\n"))
            width = max(32.0, 0.6 * size * longest + 20)
            anchor = st.get("text-anchor", "start")
            if anchor == "middle":
                x -= width / 2
            elif anchor == "end":
                x -= width
            n_lines = text.count("\n") + 1
            items.append(new_box(doc, (x - 10, y - size * 1.2 - 2), (width, size * 1.4 * n_lines + 20),
                                 text=RichText.build(runs), font_size=size,
                                 align={"middle": "center", "end": "right"}.get(anchor)))
            return
        elif tag == "image":
            href = el.get("href") or el.get("{http://www.w3.org/1999/xlink}href") or ""
            if href.startswith("data:"):
                data = base64.b64decode(href.split(",", 1)[1])
            else:
                data = (svg_dir / href).read_bytes()
            x, y, w, h = num("x"), num("y"), num("width"), num("height")
            c = _apply(m, (x + w / 2, y + h / 2))
            ang = math.atan2(m[1], m[0])
            items.append(new_image(doc, data, c, (w * k, h * k), ang))
            return
        for child in el:
            walk(child, m, st)

    walk(root, base, {})
    return items


def _marker(ref: str | None) -> int:
    if not ref or ref == "none":
        return 0
    return ARROW_FILLED if re.search(r"fill|triangle|2", ref) else ARROW_OPEN


def _stylesheet(root) -> dict[str, dict[str, str]]:
    """Minimal <style> support: simple selectors (tag, .class, #id, *)."""
    rules = {}
    for st in root.iter():
        if st.tag.split("}")[-1] != "style":
            continue
        css = re.sub(r"/\*.*?\*/", "", st.text or "", flags=re.S)
        for sels, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css):
            decl = {}
            for kv in body.split(";"):
                if ":" in kv:
                    k, v = kv.split(":", 1)
                    decl[k.strip()] = v.strip()
            for sel in sels.split(","):
                rules.setdefault(sel.strip(), {}).update(decl)
    return rules


def _text_runs(el, st, k, fill) -> list[TextRun]:
    """<text> with nested <tspan>s -> TextRuns (a tspan with x/dy starts
    a new line)."""
    runs = []

    def run(text, s):
        if not text:
            return
        size = _num(s.get("font-size", "16"), 16.0) * k
        deco = s.get("text-decoration", "")
        runs.append(TextRun(text, color=fill or (0, 0, 0, 1),
                            font=s.get("font-family", "Helvetica Neue").split(",")[0].strip("'\" "),
                            size=size, bold=s.get("font-weight") in ("bold", "bolder", "600", "700", "800", "900"),
                            italic=s.get("font-style") in ("italic", "oblique"),
                            underline="underline" in deco, strike="line-through" in deco))

    def visit(node, s, first):
        s = dict(s)
        for key in ("font-size", "font-family", "font-weight", "font-style", "text-decoration"):
            if node.get(key) is not None:
                s[key] = node.get(key)
        for kv in (node.get("style") or "").split(";"):
            if ":" in kv:
                kk, v = kv.split(":", 1)
                s[kk.strip()] = v.strip()
        if node is not el and not first and (node.get("x") is not None or node.get("dy") is not None):
            runs.append(TextRun("\n", size=_num(s.get("font-size", "16"), 16.0) * k))
        run((node.text or "").replace("\n", " ").strip("\r") if node is not el else (node.text or "").strip(), s)
        for i, child in enumerate(node):
            if child.tag.split("}")[-1] == "tspan":
                visit(child, s, first and i == 0 and not (node.text or "").strip())
            if child.tail and child.tail.strip():
                run(child.tail.strip(), s)
    visit(el, st, True)
    return runs


def _main(argv):
    import argparse
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    sp = ap.add_subparsers(dest="cmd", required=True)
    ex = sp.add_parser("export")
    ex.add_argument("doc")
    ex.add_argument("page", type=int)
    ex.add_argument("out")
    ex.add_argument("--no-background", action="store_true")
    im = sp.add_parser("import")
    im.add_argument("template")
    im.add_argument("out")
    im.add_argument("svg")
    im.add_argument("--page", type=int, default=0)
    im.add_argument("--keep", action="store_true", help="add to the page instead of replacing it")
    im.add_argument("--scale", type=float, default=1.0)
    im.add_argument("--offset", type=float, nargs=2, default=(0.0, 0.0))
    im.add_argument("--pen", default="ballpoint", choices=["ballpoint", "fountain", "brush", "pencil", "highlighter"])
    a = ap.parse_args(argv)
    if a.cmd == "export":
        doc = Document(a.doc)
        Path(a.out).write_text(page_to_svg(doc, doc.pages[a.page], not a.no_background))
    else:
        doc = Document(a.template)
        page = doc.pages[a.page]
        if not a.keep:
            page.records = []
        for it in svg_to_items(doc, a.svg, tuple(a.offset), a.scale, a.pen):
            page.append(it)
        doc.save(a.out)


if __name__ == "__main__":
    _main(sys.argv[1:])
