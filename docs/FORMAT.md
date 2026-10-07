# The `.goodnotes` format

Reverse-engineered from documents exported by GoodNotes for macOS (schema
version 24), schema strings in the app binary, and documents built one
feature at a time in the app. Field numbers are protobuf field numbers.

## Container

A zip archive:

| entry | contents |
|---|---|
| `index.notes.pb` | one record per page: `{1 notes id, 2 "notes/<id>"}` — **creation order, not page order** |
| `notes/<id>` | the page's elements (below); empty if the page has no elements |
| `index.events.pb` | the edit log: document, pages, paper, attachments, bookmarks, outline… |
| `index.attachments.pb` | `{1 attachment id, 2 "attachments/<id>"}` |
| `attachments/<id>` | raw PDF / PNG / JPEG bytes |
| `index.search.pb`, `search/<id>` | GoodNotes' handwriting/OCR index |
| `thumbnail.jpg`, `schema.pb`, `document.info.pb` | preview, schema version, (empty) |

Every `.pb` file is a stream of `[varint length][message]` records.

## Compressed blobs (`bv41`)

Geometry and rich text are stored as one or more chunks of
`"bv41", u32 raw size, u32 payload size, <LZ4 block>` followed by `"bv4$"`.
Bodies over 32 KiB span several chunks. A body is either a protobuf message
(rich text) or a **template**: `"tpl\0", u32 total size, schema string\0,
values`, where the schema language is `v` u16, `u` u32 (float bits), `f`
f32, `A(x)` u32 count + items, `S(...)` struct.

## Page elements

Each element is two records: **metadata** `{1 id, 2 version stamp, 3 deleted,
4 attachment id, 6 audio-recording link, 8 device id, 9 creation sequence,
14 5381, 16 schema version, 20 group id}` and a **payload** whose single
top-level field names its kind. A "version stamp" is `{1 edit counter,
2 random tag}`. Deleted elements stay in the file with meta field 3 = 1.
Record order is z-order.

### 7 — ink stroke

`{1 id, 2 geometry blob, 3 pen, 4 color {1 r, 2 g, 3 b, 4 a}, 5 highlighter,
9 shape-tool descriptor, 20 tape marker}`. Proto3 drops zero values, so a
missing color channel is 0.

| pen (field 3) | schema | geometry |
|---|---|---|
| 0 ballpoint | `vuA(v)A(S(uu))A(S(uuuu))vA(f)` | version, thickness, command kinds (0 move, 1 quad), moves, quads `(cx, cy, x, y)`, 1, width samples |
| 1 fountain | `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | centerline (kinds; move `x y w`; quad `cx cy cw x y w`), then a precomputed outline: per-subpath command counts (one subpath per centerline segment), command codes (0 move·2, 2 cubic·6, 3 arc·5 `cx cy r a0 a1`, 6 elliptical arc·7), moves, quads, cubics, arcs, arc-clockwise flags |
| 4 brush | `vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` | as fountain plus thickness; outline codes 2 move, 4 cubic |
| 5 pencil | `vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))…` | points `x y azimuth altitude force`; each quad starts with a random texture seed |

Shape-tool strokes keep an empty path plus descriptor 9: `1 {1 point…}`
polyline, `3 {1 center, 2 size}` rectangle, `4 {1 center, 2 radii, 3 angle}`
ellipse, `15` line width.

### 21 — shape or text box

`20 {1 origin, 3 scale}`, `21 {2 {w, h, ∞}}` (or `21 {3 {min w, max w, min h,
max h}}` with `9 = 1` for a box that auto-sizes to its text), `22` geometry
(`{1 {1 corner radius}}` rectangle — clamped to half the short side, so a big
radius is a pill; `{2}` ellipse; `{3 {1 vertices in the unit square}}`
polygon), `30 {1 {1 fill color}}`, `31` outline style, `32` text
`{1 {2 rich-text blob, 3 8-byte content hash, 4 1}, 2 measured size,
5 default attributes, 10 insets}`, `33` shadow, `8` element group id.

Style: `{1 width, 2 {1 solid | 2 {1 dash, 2 gap}}, 3 {1 color}}`.

Rich text: runs `{1 text, 2 char attrs, 3 paragraph attrs}`; char attrs
`3 color, 4 link URL, 30 font, 40 size, 50 italic, 60 weight (-30 bold),
-404 = inherit`. Links to pages are
`https://app.goodnotes.com/documents/?anchor=<b64 "page-<page id>">#<b64 doc id>`.
The content hash's algorithm is unknown (not FNV, djb2, xxh64, MurmurHash3,
MD5/SHA prefixes); GoodNotes accepts any value.

### 20 — sticky note

As 21 with `30 color`, `31 text`, `32 author id`, `33 author name`.

### 22 — line / connector

`20 {1 {1 start}, 2 mid handle (on the curve), 3 {1 end}}` straight or
curved; `21 {1 {1 start}, 3 {1 end}, 5 {2 knee}}` elbow; `30` start / `31`
end arrowhead (1 open, 2 filled); `32` style.

### 1 — image, sticker, element

`2 {origin, size}` axis-aligned bounds of the rotated image, `3 {center,
size, 3 angle in radians}`, `4 attachment id`, `6 kind (1 image, 3 element)`,
`7 group id`. Stickers and element sticky notes are vector PDF attachments.
Cropping writes a new, cropped attachment.

### Others

`11` math conversion (`9 LaTeX`, `11` original strokes), `8` legacy text box
(RTF at field 6).

## Edit log (`index.events.pb`)

Events are `{1 target id, N body}`; bodies carry `10 time (ms, double)`,
`11 event id`, `13 device`, `14 hybrid logical clock`. Attribute events are
last-writer-wins.

| N | meaning |
|---|---|
| 30 / 31 | document: `2 {1 title}` |
| 2 | paper layer: `4 background attachment, 5 PDF page, 8 page size, 9 template name` |
| 54 | page: `2 page id, 3 {1 layer id}, 4 {1 order key}` |
| 104 / 102 / 10 | page ink file created / linked / page inserted |
| 6 | attachment added: `5 byte size, 6 document id` — **required**, or GoodNotes ignores the attachment |
| 57 | bookmark `3 {1 1}` · 63 rotation `3 {1 quarter turns}` · 65 outline entry `4 order key, 5 title` |
| 160, 161, 210, 213 | audio recordings and per-word transcript timings |

**Page order** is the page events' order keys compared as ASCII strings;
the ink for a page lives in `notes/<page id + 1>`.
