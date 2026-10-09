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

Everything reads and writes; "in GoodNotes" says what has been confirmed by
importing a generated file into the app.

| Feature | Read | Write | In GoodNotes |
|---|---|---|---|
| Ballpoint ink, highlighter, dashed ink | ✅ | ✅ | ✅ |
| Fountain pen, brush, marker/tape (variable width) | ✅ | ✅ | rewritten to match real strokes (one outline per segment); import not yet re-checked |
| Pencil, shape-tool strokes, filled shapes | ✅ | ✅ | pencil/shape not yet checked |
| Eraser (split ink), lasso move, group, z-order, duplicate, delete | ✅ | ✅ | — |
| Shapes: rect/rounded/ellipse/polygon, fill, outline, dashes, rotation, shadow, lock | ✅ | ✅ | ✅ (rotation/shadow/lock not yet checked) |
| Text boxes (auto-wrap), rich text: font, size, color, bold, italic, underline, strike, link, highlight, lists, headings, alignment, line height | ✅ | ✅ | basic text ✅; decorations/lists not yet checked |
| Sticky notes (author, expanded) | ✅ | ✅ | ✅ |
| Lines, curves, elbow connectors, arrowheads, dashes | ✅ | ✅ | ✅ |
| Images (placed, rotated, locked), stickers, elements | ✅ | ✅ | ✅ |
| Math conversions (LaTeX + image) | ✅ | ✅ | not yet checked |
| Pages: add/insert/move/delete/duplicate, paper, PDF background, PDF/image import | ✅ | ✅ | ✅ |
| Bookmarks, rotation, labels, read flag, outline (nested) | ✅ | ✅ | bookmarks/rotation ✅ |
| Audio notes with page references, transcripts | ✅ | ✅ | not yet checked |
| Comments (threads, replies, resolve) | ✅ | ✅ | not yet checked |
| New document from scratch (`Document.new`) | — | ✅ | layout matches a real export; import not yet re-checked |
| Legacy text boxes, Text Docs (Yjs), graph widgets | ✅ | round-trip | — |
| SVG export/import, vector PDF export | ✅ | ✅ | — |
