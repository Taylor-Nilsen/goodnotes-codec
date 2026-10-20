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

doc = Document.new("Homework 1", "dotted", "black", pages=3)  # or Document("exported.goodnotes")
page = doc.pages[0]                         # pages are in document order

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

`Document.new(title, template, color, size, pages)` builds a complete notebook
from nothing on Essentials paper, matching a GoodNotes 6 export event for event
(`tests/test_real_document.py` checks it against the real export in
`tests/fixtures`). A notebook exported from GoodNotes also works as a base.
`save()` refuses (ValueError) to write a file GoodNotes
won't open: the fake test document in `tests/fake_document.py`, pages missing
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
python -m goodnotes new blank.goodnotes --paper narrow_ruled --pages 5
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
| Pencil | ✅ | ✅ | ✅ grain renders (checked 2026-10-20); darkness follows pressure |
| Eraser (split ink), lasso move, group, z-order, duplicate, delete | ✅ | ✅ | ✅ |
| Shapes: rect/rounded/ellipse/polygon, fill, outline, dashes, rotation, shadow, lock, filled shapes | ✅ | ✅ | ✅ |
| Rich text: font, size, color, bold, italic, underline, strike, link, highlight, lists, headings, alignment | ✅ | ✅ | ✅ renders (alignment rendering not fully confirmed) |
| Text boxes that wrap | ✅ | ✅ | ✅ wraps at the max width (GoodNotes lays out at the stored measured size) |
| Sticky notes (author, expanded) | ✅ | ✅ | ✅ |
| Lines, curves, elbow connectors, arrowheads, dashes | ✅ | ✅ | ✅ |
| Images (placed, rotated, locked), PDF stickers | ✅ | ✅ | ✅ |
| Math conversions (LaTeX + image) | ✅ | ✅ | ⚠️ imports and the record survives, but GoodNotes does not draw it |
| Pages: add/insert/move/delete/duplicate, paper, PDF background, PDF/image import | ✅ | ✅ | ✅ |
| Built-in Essentials paper at any size, image backgrounds | ✅ | ✅ | ✅ narrow ruled renders; other templates and backgrounds not yet checked |
| Bookmarks, rotation, labels, outline (nested) | ✅ | ✅ | ✅ (read flag is per-user state; GoodNotes drops it on import) |
| Audio notes, transcripts | ✅ | ✅ | ✅ audio note, name and page references (checked 2026-10-20, GoodNotes re-export keeps them) |
| Comments (threads, replies, resolve) | ✅ | ✅ | ✅ import, pinned at the right spot, text shown (checked 2026-10-20); plain text only, no comment color |
| Legacy text boxes, Text Docs (Yjs), graph widgets | ✅ | round-trip | — |
| SVG export/import, vector PDF export | ✅ | ✅ | — |
