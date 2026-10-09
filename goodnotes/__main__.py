"""Command line: python -m goodnotes <command> ...

  info DOC.goodnotes                      pages, items, outline, audio, comments
  new OUT.goodnotes [--title T] [--paper plain|lined|grid|dotted] [--pages N] [--size WxH]
  from-pdf IN.pdf OUT.goodnotes           one page per PDF page
  export-svg DOC.goodnotes PAGE OUT.svg
  export-pdf DOC.goodnotes OUT.pdf [--pages 0,2-4]
  import-svg DOC.goodnotes OUT.goodnotes IN.svg [--page N] [--keep] [--pen ...]
  dump DOC.goodnotes [--events] [--page N]  raw records, for reverse engineering
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
