"""Command line: python -m goodnotes <command> ...

  info DOC.goodnotes                      pages, items, outline, audio, comments
  new OUT.goodnotes [--title T] [--paper plain|lined|grid|dotted] [--pages N] [--size WxH]
  from-pdf IN.pdf OUT.goodnotes           one page per PDF page
  export-svg DOC.goodnotes PAGE OUT.svg
  export-pdf DOC.goodnotes OUT.pdf [--pages 0,2-4]
  import-svg DOC.goodnotes OUT.goodnotes IN.svg [--page N] [--keep] [--pen ...]
  dump DOC.goodnotes [--events] [--page N]  raw records, for reverse engineering
  testkit DIR                             one .goodnotes per feature group, for import testing
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from .model import Document, STANDARD, dump


def _info(doc: Document) -> str:
    out = [f"title: {doc.title}", f"document id: {doc.doc_id}", f"pages: {len(doc.pages)}"]
    for n, p in enumerate(doc.pages):
        items = [i for i in p.items if not i.deleted]
        kinds = {}
        for i in items:
            kinds[i.kind] = kinds.get(i.kind, 0) + 1
        flags = []
        if doc.bookmarked(p):
            flags.append("bookmarked")
        if doc.rotation(p):
            flags.append(f"rotated {doc.rotation(p)}")
        if doc.labels(p):
            flags.append("labels " + ",".join(doc.labels(p)))
        bg = p.background[0]
        out.append(f"  page {n}: {p.size[0]:.0f}x{p.size[1]:.0f}, {len(items)} items "
                   f"{dict(sorted(kinds.items()))} {'bg ' + bg[:8] if bg else ''} {' '.join(flags)}")
    if doc.outline:
        out.append("outline:")
        for title, page, depth, _ in doc.outline_tree():
            out.append("  " + "  " * depth + f"{title} -> page {doc.pages.index(page) if page else '?'}")
    for a in doc.audio_notes:
        out.append(f"audio: {a['name'] or a['id'][:8]} {a['duration']:.1f}s on {len(a['pages'])} page refs")
    for c in doc.comments():
        out.append(f"comment thread on page {doc.pages.index(c['page']) if c['page'] else '?'}: "
                   + " | ".join(f"{x['author'] or '?'}: {x['text']}" for x in c["comments"]))
    return "\n".join(out)


def make_testkit(directory, only=None) -> list[str]:
    """Writes one small document per feature group into `directory`, so an
    import failure in GoodNotes points at one group. Returns the paths."""
    import math
    import struct
    import zlib
    from .model import (ARROW_FILLED, ARROW_OPEN, DASHED, RichText, TextRun, new_box, new_brush_stroke,
                        new_fill_shape, new_fountain_stroke, new_image, new_line, new_math,
                        new_pencil_stroke, new_shape_stroke, new_sticky, new_stroke, new_tape,
                        new_text_box, paper_pdf, rich_text)
    out = Path(directory)
    out.mkdir(parents=True, exist_ok=True)

    def png(w=64, h=48, rgb=(220, 60, 60)):
        raw = b"".join(b"\0" + bytes(rgb) * w for _ in range(h))

        def chunk(t, d):
            return struct.pack(">I", len(d)) + t + d + struct.pack(">I", zlib.crc32(t + d) & 0xFFFFFFFF)
        return (b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack(">IIBBBBB", w, h, 8, 2, 0, 0, 0))
                + chunk(b"IDAT", zlib.compress(raw)) + chunk(b"IEND", b""))

    wave = [("M", (60, 120))] + [("Q", (80 + 40 * i, 120 + (60 if i % 2 else -60)), (100 + 40 * i, 120)) for i in range(8)]

    def shifted(dy):
        return [(c[0], *[(x, y + dy) for x, y in c[1:]]) for c in wave]

    kits = {}
    kits["01_blank"] = lambda d: None

    def ballpoint(d):
        p = d.pages[0]
        p.append(new_stroke(d, wave, (0.1, 0.1, 0.8, 1), 3))
        p.append(new_stroke(d, [("M", (60, 260)), ("L", (400, 260))], (1, 0.9, 0.2, 0.5), 24, highlighter=True))
        p.append(new_stroke(d, [("M", (60, 320)), ("L", (400, 320))], (0, 0, 0, 1), 3, dash=[3, 2]))
        p.append(new_shape_stroke(d, "rect", center=(230, 450), size=(200, 100)))
        p.append(new_shape_stroke(d, "ellipse", center=(230, 650), size=(100, 50), angle=0.3))
    kits["02_ballpoint_highlighter_dash_shapes"] = ballpoint

    def variable(d):
        p = d.pages[0]
        p.append(new_fountain_stroke(d, wave, (0.8, 0.1, 0.1, 1), 8, pressure=lambda t: 0.3 + 0.7 * math.sin(math.pi * t)))
        p.append(new_fountain_stroke(d, shifted(120), (0, 0, 0, 1), 4))
        p.append(new_brush_stroke(d, shifted(240), (0.1, 0.6, 0.1, 1), 10, pressure=lambda t: 0.2 + t))
        p.append(new_tape(d, [("M", (60, 480)), ("L", (400, 500))]))
    kits["03_fountain_brush_tape"] = variable

    def pencil(d):
        d.pages[0].append(new_pencil_stroke(d, wave, (0.2, 0.2, 0.2, 1), 3))
    kits["04_pencil"] = pencil

    def text(d):
        p = d.pages[0]
        p.append(new_text_box(d, (60, 80), "A long line of text that should wrap inside the box when GoodNotes lays it out, "
                                           "instead of being clipped at the right edge.", max_width=500))
        p.append(new_text_box(d, (60, 260), RichText.build([
            TextRun("Heading\n", heading=1, bold=True),
            TextRun("bold ", bold=True), TextRun("italic ", italic=True), TextRun("underline ", underline=True),
            TextRun("strike ", strike=True), TextRun("link ", link="https://example.com"),
            TextRun("highlight", highlight=(1, 1, 0, 1)),
            TextRun("\nfirst item", list_style="bullet"), TextRun("\nsecond item", list_style="bullet"),
            TextRun("\ncentered", align="center"), TextRun("\nright", align="right")]), max_width=500))
        p.append(new_box(d, (60, 700), (300, 80), text=rich_text("fixed box, 28pt", size=28)))
    kits["05_text_boxes_rich_text"] = text

    def shapes(d):
        p = d.pages[0]
        p.append(new_box(d, (60, 80), (200, 120), fill=(0.9, 0.9, 1, 1), outline=(2, (0, 0, 0.5, 1), DASHED), corner_radius=16))
        p.append(new_box(d, (300, 80), (200, 120), fill=(1, 0.9, 0.3, 1), outline=(3, (0, 0, 0, 1)), ellipse=True))
        p.append(new_box(d, (540, 80), (200, 120), fill=(0.9, 1, 0.9, 1), vertices=[(0.5, 0), (1, 1), (0, 1)]))
        p.append(new_box(d, (60, 260), (200, 120), fill=(1, 0.8, 0.8, 1), rotation=0.4, shadow=((0, 0, 0, 0.5), 6, (3, 3))))
        p.append(new_box(d, (300, 260), (200, 120), fill=(0.8, 0.8, 0.8, 1), locked=True))
        p.append(new_line(d, (60, 460), (400, 560), mid=(300, 420), arrow=ARROW_FILLED, start_arrow=ARROW_OPEN, width=4))
        p.append(new_line(d, (450, 460), (750, 560), mid=(750, 460), elbow=True, width=3, dash=DASHED))
        p.append(new_sticky(d, (60, 640), text=rich_text("sticky note", size=20), author=("u", "Taylor")))
        p.append(new_fill_shape(d, "ellipse", (0.2, 0.5, 0.9, 0.3), center=(500, 760), size=(120, 60), angle=0.4))
    kits["06_shapes_lines_sticky_fill"] = shapes

    def images(d):
        p = d.pages[0]
        p.append(new_image(d, png(), (200, 200), (128, 96)))
        p.append(new_image(d, png(32, 32, (40, 120, 220)), (500, 200), (120, 120), angle=math.radians(30), locked=True))
        p.append(new_image(d, paper_pdf((200, 200), "grid", 20), (200, 500), (160, 160)))  # sticker (PDF)
        p.append(new_math(d, r"x^2 + y^2 = r^2", png(120, 40, (0, 0, 0)), (500, 500), (240, 80)))
    kits["07_images_sticker_math"] = images

    def pages_pdf(d):
        p0 = d.pages[0]
        p1 = d.add_page()
        d.add_page()
        p0.append(new_stroke(d, wave, (0, 0, 0, 1), 2))
        p1.append(new_stroke(d, wave, (0, 0, 1, 1), 2))
        d.import_pdf(paper_pdf((600, 800), "dotted", 24))
    kits["08a_pages_pdf_import"] = pages_pdf

    def page_flags(d):
        p1 = d.add_page()
        d.set_bookmarked(p1)
        d.set_rotation(p1, 90)
        d.favourite = True
    kits["08b_bookmark_rotation_favourite"] = page_flags

    def labels(d):
        d.set_labels(d.pages[0], ["todo"])
        d.set_read(d.pages[0])
    kits["08c_labels_read"] = labels

    def outline(d):
        p1 = d.add_page()
        top = d.add_outline("Chapter 1", d.pages[0])
        d.add_outline("Section 1.1", p1, parent=top)
    kits["08d_outline"] = outline

    def comment(d):
        d.add_comment(d.pages[0], (200, 200), "a comment", author="Taylor")
    kits["08e_comment"] = comment

    def erase(d):
        p = d.pages[0]
        p.append(new_stroke(d, [("M", (60, 200)), ("L", (500, 200))], (0, 0, 0, 1), 3))
        p.append(new_stroke(d, [("M", (60, 300)), ("L", (500, 300))], (0, 0, 0, 1), 3))
        p.erase(d, (280, 200), 20)
        p.delete(p.items[1])
        a = new_box(d, (60, 400), (100, 60), fill=(1, 0, 0, 1))
        b = new_box(d, (200, 400), (100, 60), fill=(0, 1, 0, 1))
        p.append(a)
        p.append(b)
        p.group_items([a, b])
        p.duplicate(b, 150, 0)
    kits["09_erase_delete_group_duplicate"] = erase

    def audio(d):
        d.add_audio_note(_silent_wav(2.0), 2.0, page=d.pages[0], name="Test recording", offsets=[0.0, 1.0])
    kits["10a_audio_note_with_page_refs"] = audio

    def audio_plain(d):
        d.add_audio_note(_silent_wav(2.0), 2.0)
    kits["10b_audio_note_plain"] = audio_plain

    def wrap_variants(d):
        """Which sizing wraps a long line: each box says which variant it is."""
        from .model import Box, Msg, LEN, write_point
        long = " a long line of text that must wrap inside the box instead of running off the right edge of the page."
        p = d.pages[0]
        y = 60
        for label, strategy, measured in (("A", "auto", True), ("B", "fixed-auto-height", True),
                                          ("C", "fixed-auto-height", False), ("D", "auto", False)):
            it = new_text_box(d, (60, y), f"{label}:{long}", max_width=500)
            b = Box(it)
            if strategy == "fixed-auto-height":
                dims = write_point(500, 40)
                dims.set_float(3, float("inf"))
                it.body.set_bytes(21, Msg([[2, LEN, dims]]))
            if not measured:
                it.body.sub(32).remove(2)
            p.append(it)
            y += 220
    kits["11_text_wrap_variants"] = wrap_variants

    def pencil_variants(d):
        p = d.pages[0]
        p.append(new_pencil_stroke(d, wave))                                   # GoodNotes defaults
        p.append(new_pencil_stroke(d, shifted(120), width=6))
        p.append(new_pencil_stroke(d, shifted(240), pressure=lambda t: 0.6))   # old default force
        p.append(new_pencil_stroke(d, shifted(360), pressure=lambda t: t))
    kits["12_pencil_variants"] = pencil_variants

    paths = []
    for name, build in kits.items():
        if only and not any(name.startswith(o) for o in only):
            continue
        doc = Document.new(name, paper="lined")
        build(doc)
        path = out / f"{name}.goodnotes"
        doc.save(str(path))
        paths.append(str(path))
    return paths


def _silent_wav(seconds: float, rate: int = 8000) -> bytes:
    import struct
    n = int(seconds * rate)
    data = b"\0\0" * n
    return (b"RIFF" + struct.pack("<I", 36 + len(data)) + b"WAVEfmt " + struct.pack("<IHHIIHH", 16, 1, 1, rate, rate * 2, 2, 16)
            + b"data" + struct.pack("<I", len(data)) + data)


def main(argv=None):
    ap = argparse.ArgumentParser(prog="goodnotes", description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sp = ap.add_subparsers(dest="cmd", required=True)
    p = sp.add_parser("info")
    p.add_argument("doc")
    p = sp.add_parser("new")
    p.add_argument("out")
    p.add_argument("--title", default="Untitled")
    p.add_argument("--paper", default="plain", choices=["plain", "lined", "grid", "dotted"])
    p.add_argument("--pages", type=int, default=1)
    p.add_argument("--size", default=None, help="WxH in page units, e.g. 834.24x1078.825")
    p = sp.add_parser("from-pdf")
    p.add_argument("pdf")
    p.add_argument("out")
    p.add_argument("--title", default=None)
    p = sp.add_parser("export-svg")
    p.add_argument("doc")
    p.add_argument("page", type=int)
    p.add_argument("out")
    p.add_argument("--no-background", action="store_true")
    p = sp.add_parser("export-pdf")
    p.add_argument("doc")
    p.add_argument("out")
    p.add_argument("--pages", default=None)
    p.add_argument("--no-background", action="store_true")
    p = sp.add_parser("import-svg")
    p.add_argument("doc")
    p.add_argument("out")
    p.add_argument("svg")
    p.add_argument("--page", type=int, default=0)
    p.add_argument("--keep", action="store_true")
    p.add_argument("--scale", type=float, default=1.0)
    p.add_argument("--pen", default="ballpoint")
    p = sp.add_parser("testkit")
    p.add_argument("dir")
    p.add_argument("--only", default=None, help="comma-separated name prefixes, e.g. 11,12")
    p = sp.add_parser("dump")
    p.add_argument("doc")
    p.add_argument("--events", action="store_true")
    p.add_argument("--page", type=int, default=None)
    a = ap.parse_args(argv)

    if a.cmd == "info":
        print(_info(Document(a.doc)))
    elif a.cmd == "new":
        size = tuple(float(v) for v in a.size.lower().split("x")) if a.size else STANDARD
        Document.new(a.title, size, a.paper, a.pages).save(a.out)
    elif a.cmd == "from-pdf":
        data = Path(a.pdf).read_bytes()
        Document.from_pdf(data, a.title or Path(a.pdf).stem).save(a.out)
    elif a.cmd == "export-svg":
        from .svg import page_to_svg
        doc = Document(a.doc)
        Path(a.out).write_text(page_to_svg(doc, doc.pages[a.page], not a.no_background))
    elif a.cmd == "export-pdf":
        from .pdf import document_to_pdf
        pages = None
        if a.pages:
            pages = []
            for part in a.pages.split(","):
                lo, _, hi = part.partition("-")
                pages += list(range(int(lo), int(hi or lo) + 1))
        document_to_pdf(Document(a.doc), a.out, pages, not a.no_background)
    elif a.cmd == "import-svg":
        from .svg import svg_to_items
        doc = Document(a.doc)
        page = doc.pages[a.page]
        if not a.keep:
            page.records = []
        for it in svg_to_items(doc, a.svg, scale=a.scale, pen=a.pen):
            page.append(it)
        doc.save(a.out)
    elif a.cmd == "testkit":
        for name in make_testkit(a.dir, a.only.split(",") if a.only else None):
            print(name)
    elif a.cmd == "dump":
        doc = Document(a.doc)
        if a.events or a.page is None:
            for e in doc.events:
                print(dump(e))
                print("---")
        if a.page is not None:
            for r in doc.pages[a.page].records:
                print(dump(r))
                print("---")


if __name__ == "__main__":
    main(sys.argv[1:])
