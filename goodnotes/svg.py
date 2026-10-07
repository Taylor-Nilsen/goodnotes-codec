"""GoodNotes page <-> SVG.

page_to_svg: renders every decoded element (all pen types, recognized
shapes, shapes/text boxes, sticky notes, lines/arrows, images/stickers,
math conversions, legacy text boxes) plus the paper background.

svg_to_items: builds GoodNotes elements from an SVG: <path>/<line>/
<polyline>/<polygon> -> ink strokes, <rect>/<ellipse>/<circle> -> shapes,
<text> -> text boxes, <image> -> placed images.

Usage:
    python -m pipeline.extract.svg export DOC.goodnotes PAGE OUT.svg
    python -m pipeline.extract.svg import TEMPLATE.goodnotes OUT.goodnotes IN.svg [--page N] [--keep]
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

from .model import (PSTROKE, Box, Document, Image, Item, Line, Math, Msg, Page, RichText,
                    Sticky, Stroke, f32, new_box, new_image, new_line, new_stroke, read_color,
                    read_point, rich_text)

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


# fountain/ball pen outline command codes, per schema variant (see
# FORMAT_NOTES "Variable-width strokes"): code -> (op, float count)
_OUTLINE_NO_THICKNESS = {0: ("M", 2), 1: ("Q", 4), 2: ("C", 6), 3: ("A", 5), 6: ("E", 7)}
_OUTLINE_THICKNESS = {2: ("M", 2), 3: ("Q", 4), 4: ("C", 6), 5: ("A", 5), 6: ("E", 7)}


def _arc_path(cx, cy, r, a0, a1, clockwise, start_new):
    x0, y0 = cx + r * math.cos(a0), cy + r * math.sin(a0)
    x1, y1 = cx + r * math.cos(a1), cy + r * math.sin(a1)
    sweep = (a1 - a0) % (2 * math.pi) if not clockwise else (a0 - a1) % (2 * math.pi)
    large = 1 if sweep > math.pi else 0
    flag = 0 if clockwise else 1
    head = "M" if start_new else "L"
    return f"{head} {_fmt(x0, y0)} A {_fmt(r, r)} 0 {large} {flag} {_fmt(x1, y1)}"


def _outline_d(t) -> str:
    """SVG path for a variable-width stroke's precomputed outline."""
    if t.schema.startswith("vu"):
        table, v = _OUTLINE_THICKNESS, t.values[2:]
    else:
        table, v = _OUTLINE_NO_THICKNESS, t.values[1:]
    types, arrays, cw = v[4], {"M": v[5], "Q": v[6], "C": v[7], "A": v[8], "E": v[8]}, v[9]
    pos = {"M": 0, "Q": 0, "C": 0, "A": 0}
    ai = 0
    parts, have_point = [], False
    for code in types:
        if code not in table:
            continue  # close/marker codes carry no coordinates
        op, n = table[code]
        key = "A" if op == "E" else op
        vals = [f32(x) for x in arrays[op][pos[key]:pos[key] + n]]
        pos[key] += n
        if len(vals) < n:
            break
        if op == "M":
            parts.append(f"M {_fmt(*vals)}")
            have_point = True
        elif op == "Q":
            parts.append(f"Q {_fmt(*vals)}")
        elif op == "C":
            parts.append(f"C {_fmt(*vals)}")
        else:
            clockwise = bool(cw[ai]) if ai < len(cw) else False
            ai += 1
            if op == "A":
                cx, cy, r, a0, a1 = vals
            else:  # elliptical: cx, cy, rx, ry, rotation, start, end -> circular approx
                cx, cy, rx, ry, _rot, a0, a1 = vals
                r = (abs(rx) + abs(ry)) / 2
            parts.append(_arc_path(cx, cy, r, a0, a1, clockwise, not have_point))
            have_point = True
    return " ".join(parts) + " Z" if parts else ""


def _brush_d(t) -> tuple[str, float]:
    """Centerline path + average width for the pressure brush schema."""
    th = f32(t.values[1])
    kinds, moves, quads = t.values[2], t.values[3], t.values[4]
    parts, widths, mi, qi = [], [], 0, 0
    for k in kinds:
        if k == 0 and mi < len(moves):
            m = [f32(x) for x in moves[mi]]
            parts.append(f"M {_fmt(m[0], m[1])}")
            widths.append(m[4])  # force
            mi += 1
        elif qi < len(quads):
            q = [f32(x) for x in quads[qi]]
            parts.append(f"Q {_fmt(q[1], q[2], q[6], q[7])}")  # q[0] = texture seed
            widths.append(q[10])
            qi += 1
    w = th * 2 * (0.4 + (sum(widths) / len(widths) if widths else 0.6))
    return " ".join(parts), max(w, 0.5)


def _stroke_svg(s: Stroke) -> str:
    color, alpha = _rgba(s.color)
    shape = s.shape
    if shape is not None:
        w = shape.float(15) or 2.0
        common = f'fill="none" stroke="{color}" stroke-opacity="{alpha:.3f}" stroke-width="{w:.2f}" stroke-linejoin="round" stroke-linecap="round"'
        if shape.sub(3) is not None:  # rectangle: center, size
            (cx, cy), (rw, rh) = read_point(shape.path(3, 1)), read_point(shape.path(3, 2))
            x, y = cx - rw / 2, cy - rh / 2
            return f'<rect x="{x:.2f}" y="{y:.2f}" width="{rw:.2f}" height="{rh:.2f}" {common}/>'
        if shape.sub(4) is not None:  # ellipse
            e = shape.sub(4)
            (cx, cy), (ew, eh), ang = read_point(e.sub(1)), read_point(e.sub(2)), e.float(3)
            return (f'<ellipse cx="{cx:.2f}" cy="{cy:.2f}" rx="{ew:.2f}" ry="{eh:.2f}" '
                    f'transform="rotate({math.degrees(ang):.3f} {cx:.2f} {cy:.2f})" {common}/>')
        poly = shape.sub(1)
        pts = [read_point(p) for p in poly.subs(1)] if poly is not None else []
        if pts:
            return f'<polyline points="{" ".join(_fmt(*p) for p in pts)}" {common}/>'
    t = s.template
    if t.schema == PSTROKE:
        d = []
        for c in s.commands:
            d.append(f"{c[0]} " + " ".join(_fmt(*p) for p in c[1:]))
        if not d:
            return ""
        w = f32(t.values[1])
        op = alpha
        return (f'<path d="{" ".join(d)}" fill="none" stroke="{color}" stroke-opacity="{op:.3f}" '
                f'stroke-width="{w:.2f}" stroke-linecap="{"butt" if s.highlighter else "round"}" stroke-linejoin="round"/>')
    if "A(u)A(u)A(v)A(v)" in t.schema:
        d = _outline_d(t)
        return f'<path d="{d}" fill="{color}" fill-opacity="{alpha:.3f}" fill-rule="nonzero"/>' if d else ""
    if t.schema.startswith("vuA(v)A(S(uuuuu))"):
        d, w = _brush_d(t)
        return (f'<path d="{d}" fill="none" stroke="{color}" stroke-opacity="{alpha:.3f}" '
                f'stroke-width="{w:.2f}" stroke-linecap="round" stroke-linejoin="round"/>') if d else ""
    return f"<!-- unrendered stroke schema {html.escape(t.schema)} -->"


def _text_svg(rt: RichText | None, x: float, y: float, default_size: float = 24.0,
              default_font: str = "Helvetica Neue") -> str:
    if rt is None:
        return ""
    lines, cur = [], []
    for r in rt.runs:
        a = r.sub(2) or Msg()
        size = a.float(40)
        size = default_size if size <= 0 else size
        font = a.str(30) or default_font
        color, alpha = _rgba(read_color(a.sub(3), (0, 0, 0, 1)))
        weight = "bold" if a.sint64(60) == -30 else "normal"
        style = "italic" if a.int(50) else "normal"
        for i, chunk in enumerate(r.str(1, "").split("\n")):
            if i:
                lines.append(cur)
                cur = []
            if chunk:
                cur.append((chunk, size, font, color, alpha, weight, style))
    lines.append(cur)
    out, ly = [], y
    for line in lines:
        h = max((c[1] for c in line), default=default_size) * 1.2
        ly += h
        spans = "".join(
            f'<tspan font-family="{html.escape(f)}" font-size="{sz:.1f}" fill="{col}" fill-opacity="{al:.3f}" '
            f'font-weight="{wt}" font-style="{st}">{html.escape(txt)}</tspan>'
            for txt, sz, f, col, al, wt, st in line)
        if spans:
            out.append(f'<text x="{x:.2f}" y="{ly - h * 0.25:.2f}" xml:space="preserve">{spans}</text>')
    return "".join(out)


def _box_svg(b: Box) -> str:
    (x, y), (w, h) = b.origin, b.size
    t = b.b.sub(32)
    if (w, h) == (0.0, 0.0) and t is not None:  # auto-sized text box
        w, h = read_point(t.sub(2))
        w, h = w + 20, h + 20
    fill = b.fill
    outline = b.outline
    fill_attr = f'fill="{_rgba(fill)[0]}" fill-opacity="{fill[3]:.3f}"' if fill else 'fill="none"'
    if outline and outline[1] is not None and outline[0] > 0:
        col, a = _rgba(outline[1])
        stroke_attr = f'stroke="{col}" stroke-opacity="{a:.3f}" stroke-width="{outline[0]:.2f}"' + _dash(b.b.sub(31), outline[0])
    else:
        stroke_attr = 'stroke="none"'
    verts = b.vertices
    out = []
    geo = b.b.sub(22)
    if geo is not None and geo.sub(2) is not None:  # ellipse
        out.append(f'<ellipse cx="{x + w / 2:.2f}" cy="{y + h / 2:.2f}" rx="{w / 2:.2f}" ry="{h / 2:.2f}" {fill_attr} {stroke_attr}/>')
    elif verts:
        pts = " ".join(_fmt(x + vx * w, y + vy * h) for vx, vy in verts)
        out.append(f'<polygon points="{pts}" {fill_attr} {stroke_attr} stroke-linejoin="round"/>')
    elif fill or "stroke=\"none\"" not in stroke_attr:
        geo = b.b.sub(22)
        r = geo.path(1).float(1) if geo is not None and geo.sub(1) is not None else 0.0
        r = min(r, w / 2, h / 2)  # GoodNotes clamps like this (a pill is a huge radius)
        out.append(f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" rx="{r:.2f}" ry="{r:.2f}" {fill_attr} {stroke_attr}/>')
    ins = t.sub(10) if t is not None else None
    pad = ins.float(1) if ins is not None else 10.0
    attrs = t.path(5, 1) if t is not None else None
    size = attrs.float(40) if attrs is not None else 24.0
    out.append(_text_svg(b.text, x + pad, y + pad * 0.5, size or 24.0))
    return "".join(out)


def _sticky_svg(s: Sticky) -> str:
    (x, y), (w, h) = s.origin, s.size
    col, a = _rgba(s.color)
    return (f'<rect x="{x:.2f}" y="{y:.2f}" width="{w:.2f}" height="{h:.2f}" fill="{col}" fill-opacity="{a:.3f}"/>'
            + _text_svg(s.text, x + 10, y + 4, 24.0))


def _line_svg(ln: Line, marker_id: str) -> str:
    pts = ln.points
    if len(pts) < 2:
        return ""
    col, a = _rgba(ln.color)
    if ln.elbow:  # start, end, knee: horizontal leg first, then vertical
        start, end = pts[0], pts[1]
        d = f"M {_fmt(*start)} H {end[0]:.2f} V {end[1]:.2f}"
    elif len(pts) == 3:  # start, mid handle (on the curve), end
        start, mid, end = pts
        ctrl = (2 * mid[0] - (start[0] + end[0]) / 2, 2 * mid[1] - (start[1] + end[1]) / 2)
        d = f"M {_fmt(*start)} Q {_fmt(*ctrl)} {_fmt(*end)}"
    else:
        d = f"M {_fmt(*pts[0])} L {_fmt(*pts[-1])}"
    marker = ""
    if ln.arrow:
        marker += f' marker-end="url(#{marker_id}{ln.arrow})"'
    if ln.b.int(30):
        marker += f' marker-start="url(#{marker_id}{ln.b.int(30)})"'
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
    rot = f' transform="rotate({math.degrees(img.angle):.3f} {cx:.2f} {cy:.2f})"' if img.angle else ""
    return (f'<image x="{cx - w / 2:.2f}" y="{cy - h / 2:.2f}" width="{w:.2f}" height="{h:.2f}" '
            f'preserveAspectRatio="none" href="{_data_uri(data, mime)}"{rot}/>')


def _rtf_to_text(rtf: str) -> str:
    """Just enough RTF stripping for legacy text boxes."""
    rtf = re.sub(r"\\'([0-9a-f]{2})", lambda m: chr(int(m.group(1), 16)), rtf)
    rtf = re.sub(r"\{\\\*[^{}]*\}|\{\\(fonttbl|colortbl|stylesheet)[^{}]*(\{[^{}]*\}[^{}]*)*\}", "", rtf)
    rtf = rtf.replace("\\par", "\n").replace("\\\n", "\n")
    rtf = re.sub(r"\\[a-zA-Z]+-?\d* ?|[{}]", "", rtf)
    return rtf.strip()


def _legacy_text_svg(item: Item) -> str:
    b = item.body
    (x, y) = read_point(b.path(3, 1))
    rtf = b.bytes(6) or b""
    txt = _rtf_to_text(rtf.decode("utf-8", "replace"))
    lines = "".join(f'<tspan x="{x:.2f}" dy="1.2em">{html.escape(l)}</tspan>' for l in txt.split("\n"))
    return f'<text x="{x:.2f}" y="{y:.2f}" font-family="Helvetica" font-size="16">{lines}</text>'


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
            elif mime:
                body.append(f'<image x="0" y="0" width="{w:.2f}" height="{h:.2f}" preserveAspectRatio="none" href="{_data_uri(data, mime)}"/>')
        else:
            body.append(f'<rect width="{w:.2f}" height="{h:.2f}" fill="white"/>')
    items = [i for i in page.items if not i.deleted]
    # placed images and widgets sit under the ink (see FORMAT_NOTES "Z-order")
    for it in items:
        if it.kind == "image":
            img = it.typed()
            body.append(_image_svg(img, att.get(img.attachment)))
        elif it.kind == "math":
            m = it.typed()
            data = att.get(m.b.str(4) or "")
            body.append(_image_svg(Image(it), data))
    for n, it in enumerate(items):
        k = it.kind
        try:
            if k == "stroke":
                body.append(_stroke_svg(it.typed()))
            elif k == "box":
                body.append(_box_svg(it.typed()))
            elif k == "sticky":
                body.append(_sticky_svg(it.typed()))
            elif k == "line":
                body.append(_line_svg(it.typed(), "arrow"))
            elif k == "field8":
                body.append(_legacy_text_svg(it))
        except (ValueError, IndexError, KeyError) as e:
            body.append(f"<!-- {k} {it.id}: {html.escape(str(e))} -->")
    defs = ('<defs><marker id="arrow1" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="5" markerHeight="5" '
            'orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M 0 0 L 10 5 L 0 10" fill="none" '
            'stroke="context-stroke" stroke-width="1.5"/></marker>'
            '<marker id="arrow2" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="4" markerHeight="4" '
            'orient="auto-start-reverse" markerUnits="strokeWidth"><path d="M 0 0 L 10 5 L 0 10 Z" '
            'fill="context-stroke"/></marker></defs>')
    return (f'<svg xmlns="http://www.w3.org/2000/svg" xmlns:xlink="http://www.w3.org/1999/xlink" '
            f'viewBox="0 0 {w:.2f} {h:.2f}" width="{w:.0f}" height="{h:.0f}">{defs}{"".join(body)}</svg>')


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
        a = math.atan2(ux * vy - uy * vx, ux * vx + uy * vy)
        return a

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
    if re.fullmatch(r"#[0-9a-f]{3}", s):
        return tuple(int(c * 2, 16) / 255 for c in s[1:]) + (1.0,)
    m = re.fullmatch(r"rgba?\(([^)]*)\)", s)
    if m:
        p = [float(v.strip().rstrip("%")) for v in m.group(1).split(",")]
        return (p[0] / 255, p[1] / 255, p[2] / 255, p[3] if len(p) > 3 else 1.0)
    named = {"black": (0, 0, 0), "white": (1, 1, 1), "red": (1, 0, 0), "green": (0, 0.5, 0),
             "blue": (0, 0, 1), "yellow": (1, 1, 0), "orange": (1, 0.647, 0), "purple": (0.5, 0, 0.5),
             "gray": (0.5, 0.5, 0.5), "grey": (0.5, 0.5, 0.5)}
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
        else:
            continue
        a, b, c, d, e, f = m
        a2, b2, c2, d2, e2, f2 = n
        m = (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
             a * e2 + c * f2 + e, b * e2 + d * f2 + f)
    return m


def _apply(m, p):
    a, b, c, d, e, f = m
    return (a * p[0] + c * p[1] + e, b * p[0] + d * p[1] + f)


def svg_to_items(doc: Document, svg_path: str, offset=(0.0, 0.0), scale=1.0) -> list[Item]:
    """Builds GoodNotes items from an SVG (1 user unit = `scale` page
    units). Group transforms and style inheritance are honored."""
    root = ET.parse(svg_path).getroot()
    base = (scale, 0.0, 0.0, scale, offset[0], offset[1])
    svg_dir = Path(svg_path).parent
    items: list[Item] = []

    def style_of(el, inherited):
        st = dict(inherited)
        for k in ("fill", "stroke", "stroke-width", "font-size", "font-family", "font-weight", "opacity",
                  "fill-opacity", "stroke-opacity"):
            if el.get(k) is not None:
                st[k] = el.get(k)
        for kv in (el.get("style") or "").split(";"):
            if ":" in kv:
                k, v = kv.split(":", 1)
                st[k.strip()] = v.strip()
        return st

    def walk(el, m, st):
        tag = el.tag.split("}")[-1]
        if tag in ("defs", "clipPath", "mask", "marker", "symbol", "style", "title", "desc"):
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
        sw = float(re.match(_NUM, st.get("stroke-width", "1")).group()) * k
        num = lambda name, d=0.0: float(re.match(_NUM, el.get(name, str(d))).group())

        if tag in ("path", "line", "polyline", "polygon"):
            if tag == "path":
                cmds = parse_path(el.get("d", ""))
            elif tag == "line":
                cmds = [("M", (num("x1"), num("y1"))), ("L", (num("x2"), num("y2")))]
            else:
                pts = [float(x) for x in re.findall(_NUM, el.get("points", ""))]
                pairs = list(zip(pts[::2], pts[1::2]))
                cmds = [("M", pairs[0])] + [("L", p) for p in pairs[1:]] if pairs else []
                if tag == "polygon" and pairs:
                    cmds.append(("L", pairs[0]))
            cmds = [(c[0], *[_apply(m, p) for p in c[1:]]) for c in cmds]
            color = stroke or fill  # an unstroked filled path still becomes visible ink
            if cmds and color:
                items.append(new_stroke(doc, cmds, color, max(sw if stroke else 1.0, 0.1)))
        elif tag in ("rect", "circle", "ellipse"):
            if tag == "rect":
                x, y, w, h = num("x"), num("y"), num("width"), num("height")
                rr = num("rx", 0) * k
                verts = None
            else:
                cx, cy = num("cx"), num("cy")
                rx = num("r") if tag == "circle" else num("rx")
                ry = num("r") if tag == "circle" else num("ry")
                x, y, w, h = cx - rx, cy - ry, 2 * rx, 2 * ry
                rr = 0
                verts = [(0.5 + 0.5 * math.cos(t * math.pi / 32), 0.5 + 0.5 * math.sin(t * math.pi / 32))
                         for t in range(64)]
            (x0, y0), (x1, y1) = _apply(m, (x, y)), _apply(m, (x + w, y + h))
            items.append(new_box(doc, (min(x0, x1), min(y0, y1)), (abs(x1 - x0), abs(y1 - y0)),
                                 fill=fill, outline=(sw, stroke) if stroke else None,
                                 corner_radius=rr or None, vertices=verts))
        elif tag == "text":
            text = "".join(el.itertext())
            size = float(re.match(_NUM, st.get("font-size", "16")).group()) * k
            x, y = _apply(m, (num("x"), num("y")))
            rt = rich_text(text, fill or (0, 0, 0, 1), st.get("font-family", "Helvetica Neue").split(",")[0].strip("'\" "),
                           size, bold=st.get("font-weight") in ("bold", "700", "800", "900"))
            width = max(32.0, 0.6 * size * len(text) + 20)
            items.append(new_box(doc, (x - 10, y - size * 1.2 - 2), (width, size * 1.4 + 20), text=rt,
                                 font_size=size))
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


def _mult(m, n):
    a, b, c, d, e, f = m
    a2, b2, c2, d2, e2, f2 = n
    return (a * a2 + c * b2, b * a2 + d * b2, a * c2 + c * d2, b * c2 + d * d2,
            a * e2 + c * f2 + e, b * e2 + d * f2 + f)


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
    a = ap.parse_args(argv)
    if a.cmd == "export":
        doc = Document(a.doc)
        Path(a.out).write_text(page_to_svg(doc, doc.pages[a.page], not a.no_background))
    else:
        doc = Document(a.template)
        page = doc.pages[a.page]
        if not a.keep:
            page.records = []
        for it in svg_to_items(doc, a.svg, tuple(a.offset), a.scale):
            page.append(it)
        doc.save(a.out)


if __name__ == "__main__":
    _main(sys.argv[1:])
