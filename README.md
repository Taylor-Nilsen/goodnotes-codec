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

doc = Document("template.goodnotes")       # any existing document
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
| Bookmarks, page rotation, outline | ✅ | ✅ | not yet checked |
| Legacy RTF text boxes, math conversions (LaTeX) | ✅ | — | — |
| Audio recordings, graph widgets, comments, Text Docs | round-trip only | — | — |
