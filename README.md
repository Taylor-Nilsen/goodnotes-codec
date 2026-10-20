# goodnotes-codec

Read, edit and write GoodNotes `.goodnotes` files in pure Python (stdlib only).

- **Lossless.** Every record round-trips byte-for-byte — verified on 206 real
  documents (4,720 pages, 307,070 strokes) — so features this library doesn't
  understand yet survive a load/save untouched.
- **Typed editing** of ink, shapes, text boxes, sticky notes, lines/arrows,
  images, pages, bookmarks, rotation and outline.
- **SVG in/out.** Render any page to SVG, or turn an SVG into GoodNotes elements.

Reverse-engineered; not affiliated with GoodNotes. Format notes:
[`docs/FORMAT.md`](docs/FORMAT.md).

## Quick start

```python
from goodnotes.model import Document, Stroke, new_box, new_stroke, rich_text

doc = Document.new("Homework 1", "dotted", "black", pages=3)  # or Document("exported.goodnotes")
page = doc.pages[0]                         # pages are in document order

page.append(new_stroke(doc, [("M", (60, 100)), ("Q", (200, 40), (340, 100))],
                       color=(0.9, 0.1, 0.1, 1), thickness=3))
page.append(new_box(doc, (60, 160), (300, 80), fill=(1, 0.9, 0.3, 1),
                    outline=(2, (0, 0, 0, 1)), corner_radius=40,
                    text=rich_text("Hello", size=28, bold=True)))

for item in page.visible():                 # edit what's already there
    if isinstance(item, Stroke):
        item.color = (0, 0, 1, 1)

doc.save("out.goodnotes")                   # File > Import in GoodNotes
```

`Document.new(title, template, color, size, pages)` builds a complete notebook
from nothing on Essentials paper, matching a GoodNotes 6 export event for event
(`tests/test_real_document.py` checks it against the real export in
`tests/fixtures`). A notebook exported from GoodNotes also works as a base.
`save()` refuses (ValueError) to write a file GoodNotes
won't open: the fake test document in `tests/synthetic.py`, pages missing
their events or index entries, duplicate page order keys, or attachments
missing from the zip, `index.attachments.pb` or the event log. It re-reads the
file before replacing `path`. `doc.problems()` lists the same checks.

### Paper and backgrounds

Every page size is a custom `(w, h)` in page units; `paper.SIZES` holds
GoodNotes' menu (standard, a3, a4, a5, a6, a7, letter, tabloid) as presets.
Size defaults to the page's current size (Standard on new notebooks).

```python
from goodnotes import paper

doc.set_paper(page, "dotted", "black")                        # keeps the page's size
doc.set_paper(page, "wide_ruled", "yellow", paper.SIZES["a4"], landscape=True)
doc.set_paper(page, "squared", "white", (900, 1200))          # any custom size

doc.set_background(page, "photo.jpg", size=paper.SIZES["letter"])  # PNG/JPEG (others need Pillow)
doc.set_background(page, "planner.pdf", pdf_page=3)                # a PDF page
doc.set_background(page, "other.goodnotes", pdf_page=2)            # another notebook's page 2 paper
```

Templates: `blank`, `dotted`, `squared`, `narrow_ruled`, `wide_ruled`; colors:
`white`, `yellow`, `black`. Built-in paper is regenerated at the page's size
(the pattern repeats, it doesn't stretch); custom backgrounds are stretched to
fill the page.

```bash
python -m goodnotes.svg export doc.goodnotes 0 page.svg
python -m goodnotes.svg import template.goodnotes out.goodnotes drawing.svg --keep
python -m unittest
```

Coordinates are GoodNotes page units (a standard page is 834.24 × 1078.82).

## Status

Checked by importing generated files into GoodNotes for macOS:

| Feature | Read | Write | Seen working in GoodNotes |
|---|---|---|---|
| New document from scratch (`Document.new`) | — | ✅ | ✅ imported (checked on `feat/full-format`, ported from there) |
| Ballpoint ink, highlighter | ✅ | ✅ | ✅ |
| Fountain / brush ink (variable width) | ✅ | ⚠️ | ❌ crashes — see issues |
| Pencil, tape, shape-tool strokes | ✅ | ✅ | not yet checked |
| Shapes (rect, rounded, ellipse, polygon) + fill/outline | ✅ | ✅ | ✅ |
| Text boxes (font, size, color, bold, italic) | ✅ | ✅ | ✅ |
| Sticky notes | ✅ | ✅ | ✅ |
| Lines, curves, elbow connectors, arrows | ✅ | ✅ | ✅ |
| Dashes, filled arrowheads, hyperlinks | ✅ | ✅ | not yet checked |
| Images (placed, rotated), stickers | ✅ | ✅ | ✅ |
| Pages: order, add/insert/move/delete, custom PDF background | ✅ | ✅ | ✅ |
| Built-in Essentials paper at any size, image backgrounds | ✅ | ✅ | not yet checked |
| Bookmarks, page rotation, outline | ✅ | ✅ | not yet checked |
| Legacy RTF text boxes, math conversions (LaTeX) | ✅ | — | — |
| Audio recordings, graph widgets, comments, Text Docs | round-trip only | — | — |
