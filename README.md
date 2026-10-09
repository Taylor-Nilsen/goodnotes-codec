# goodnotes-codec

Read, edit and write GoodNotes `.goodnotes` files in pure Python (stdlib only).

- **Lossless.** Every record round-trips byte-for-byte (verified on 206 real
  documents, 4,720 pages, and on the real export in `tests/fixtures`), so
  anything this library doesn't model survives a load/save untouched.
- **Built on GoodNotes' own schema.** Field numbers and names come from the
  protobuf definitions inside GoodNotes' own code (`goodnotes/schema.py`),
  not guesswork: every notes item, every edit-log event, rich text, enums.
- **Every element, in and out:** ballpoint, fountain, brush, marker/tape,
  pencil and highlighter ink (with dashes, partial erasing, lasso moves),
  shapes with fill/outline/rotation/shadow, auto-wrapping text boxes with
  full rich text (bold, italic, underline, strike, links, highlight, lists,
  headings, alignment), sticky notes, lines/curves/elbow connectors with
  arrowheads, images, stickers, math conversions, filled shapes, pages,
  paper, bookmarks, rotation, labels, outline, audio notes, comments.
- **New documents from nothing**, PDF and image import, SVG in/out, vector
  PDF export, a CLI.

Reverse-engineered; not affiliated with GoodNotes. Format notes:
[`docs/FORMAT.md`](docs/FORMAT.md).

## Quick start

```python
from goodnotes.model import (Document, Stroke, TextRun, RichText, new_box, new_fountain_stroke,
                             new_line, new_stroke, new_text_box, rich_text, ARROW_FILLED)

doc = Document.new("Homework 1", paper="dotted", pages=3)   # or Document("existing.goodnotes")
page = doc.pages[0]

page.append(new_stroke(doc, [("M", (60, 100)), ("Q", (200, 40), (340, 100))],
                       color=(0.9, 0.1, 0.1, 1), thickness=3))
page.append(new_fountain_stroke(doc, [("M", (60, 160)), ("L", (340, 160))], width=6,
                                pressure=lambda t: 0.4 + 0.6 * t))
page.append(new_text_box(doc, (60, 220), "Long lines wrap at the box's max width, "
                         "the way GoodNotes' own text boxes do.", max_width=500))
page.append(new_box(doc, (60, 400), (300, 80), fill=(1, 0.9, 0.3, 1), corner_radius=40,
                    text=rich_text("Pill", size=28, bold=True, align="center")))
page.append(new_line(doc, (60, 520), (400, 600), mid=(300, 500), arrow=ARROW_FILLED))

doc.add_outline("Problem 1", page)
doc.set_bookmarked(doc.pages[1])
doc.add_comment(page, (100, 100), "check units", author="Taylor")

for item in page.visible():               # typed views of what GoodNotes draws
    if isinstance(item, Stroke):
        item.color = (0, 0, 1, 1)

doc.save("out.goodnotes")                 # File > Import in GoodNotes
```

```bash
python -m goodnotes new blank.goodnotes --paper lined --pages 5
python -m goodnotes from-pdf lecture.pdf lecture.goodnotes
python -m goodnotes info notes.goodnotes
python -m goodnotes export-svg notes.goodnotes 0 page.svg
python -m goodnotes export-pdf notes.goodnotes notes.pdf
python -m goodnotes import-svg blank.goodnotes out.goodnotes drawing.svg --pen fountain
python -m goodnotes testkit kit/     # one file per feature group, for import testing in GoodNotes
python -m unittest
```

Coordinates are GoodNotes page units (standard paper is 834.24 × 1078.825;
1 unit = 6/11 PDF point).

### Starting point

`Document.new()` writes a complete document: every event carries its id,
timestamp, device id, sync clock and schema version, every attribute its
version stamp, and the page-setup events GoodNotes expects, matching a
GoodNotes 6 export field for field (`tests/test_real_document.py` checks
this against a real one). A real export (File > Export > Goodnotes) works
just as well as a base. `tests/fake_document.py` is **not** a GoodNotes
document; it only exists so the parser tests run without one, and it will
crash GoodNotes if imported.

### Text boxes

GoodNotes' text tool makes *auto-sizing* boxes: a flag plus a maximum
width, and the text wraps at that width. `new_text_box(doc, origin, text,
max_width)` builds one. A fixed-size `new_box()` is a shape; text inside it
is clipped, not wrapped.

## Status

Everything reads and writes. "In GoodNotes" is what a round trip through the
macOS app confirmed: the generated test kit (`python -m goodnotes testkit`)
was imported, re-exported, and diffed against the originals, and GoodNotes'
own page renderings were checked.

| Feature | Read | Write | In GoodNotes |
|---|---|---|---|
| New document from scratch (`Document.new`) | — | ✅ | ✅ imports; GoodNotes keeps every item |
| Ballpoint ink, highlighter, dashed ink, shape-tool strokes | ✅ | ✅ | ✅ |
| Fountain pen, brush, marker/tape (variable width, pressure) | ✅ | ✅ | ✅ kept byte-for-byte, renders with pressure |
| Pencil | ✅ | ✅ | ⚠️ imports; rendered very faint with the old defaults, now written like GoodNotes' own (re-check pending) |
| Eraser (split ink), lasso move, group, z-order, duplicate, delete | ✅ | ✅ | ✅ |
| Shapes: rect/rounded/ellipse/polygon, fill, outline, dashes, rotation, shadow, lock, filled shapes | ✅ | ✅ | ✅ |
| Rich text: font, size, color, bold, italic, underline, strike, link, highlight, lists, headings, alignment | ✅ | ✅ | ✅ all attributes render |
| Text boxes that wrap | ✅ | ✅ | ⚠️ measured size now stored (`11_text_wrap_variants` in the kit tells which strategy wraps; re-check pending) |
| Sticky notes (author, expanded) | ✅ | ✅ | ✅ |
| Lines, curves, elbow connectors, arrowheads, dashes | ✅ | ✅ | ✅ |
| Images (placed, rotated, locked), PDF stickers | ✅ | ✅ | ✅ |
| Math conversions (LaTeX + image) | ✅ | ✅ | ⚠️ imports and the record survives, but GoodNotes does not draw it |
| Pages: add/insert/move/delete/duplicate, paper, PDF background, PDF/image import | ✅ | ✅ | ✅ (kit 08 not yet re-exported) |
| Bookmarks, rotation, labels, read flag, outline (nested) | ✅ | ✅ | bookmarks/rotation ✅; rest not yet checked |
| Audio notes with page references, transcripts | ✅ | ✅ | not yet checked (kit 10) |
| Comments (threads, replies, resolve) | ✅ | ✅ | not yet checked (kit 08) |
| Legacy text boxes, Text Docs (Yjs), graph widgets | ✅ | round-trip | — |
| SVG export/import, vector PDF export | ✅ | ✅ | — |
