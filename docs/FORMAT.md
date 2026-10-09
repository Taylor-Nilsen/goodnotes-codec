# The `.goodnotes` format

GoodNotes 6 export format (schema versions 24 and 25). Field numbers and
names below are GoodNotes' own: its web app ships the Swift core as
WebAssembly with SwiftProtobuf's field-name tables intact, and
`goodnotes/schema.py` transcribes them. Layouts were then checked against
real exports (`tests/fixtures`) and a 206-document library.

## Container

A zip archive:

| entry | contents |
|---|---|
| `schema.pb` | `{1 schema_version}` (24 or 25) |
| `index.notes.pb` | one record per page: `{1 notes id, 2 "notes/<id>"}` — creation order, not page order |
| `notes/<id>` | the page's elements; empty if the page has none |
| `index.events.pb` | the edit log: document, pages, paper, attachments, bookmarks, outline, audio, comments |
| `index.attachments.pb` | `{1 attachment id, 2 "attachments/<id>"}` |
| `attachments/<id>` | raw PDF / PNG / JPEG / audio bytes |
| `index.search.pb`, `search/<id>` | GoodNotes' handwriting/OCR index (`{1 id, 2 path, 3 1}`) |
| `thumbnail.jpg`, `document.info.pb` | preview, (empty) |

Every `.pb` file but `schema.pb` is a stream of `[varint length][message]`
records.

## Compressed blobs (`bv41`)

Geometry and rich text are stored as one or more chunks of
`"bv41", u32 raw size, u32 payload size, <LZ4 block>` (or `"bv4-", u32
size, <raw bytes>` for an uncompressed chunk) followed by `"bv4$"`. Bodies
over 32 KiB span several chunks. A body is either a protobuf message (rich
text) or a **template**: `"tpl\0", u32 total size, schema string\0,
values`, where the schema language is `v` u16, `u` u32 (float bits), `f`
f32, `A(x)` u32 count + items, `S(...)` struct.

## Page elements (`notes/<id>`)

Each element is two records: **metadata** `{1 id, 2 version {1 counter,
2 random}, 3 deleted, 4 attachmentIds, 8 replicaId (device), 9
logical_timestamp (creation sequence), 14 mergeableHash (5381 = djb2 of
""), 16 schemaVersion, 20 parentId}` and a **payload** `NotesItem`, a
oneof whose field names the kind:

| field | item | view |
|---|---|---|
| 1 | image (images, stickers, elements, graph widgets) | `Image` |
| 5, 8 | legacy text boxes (RTF / tagged content) | `LegacyText` |
| 7 | penStroke2: all ink | `Stroke` |
| 9 | fillShape: shape-tool fill | `FillShape` |
| 10 | penEraser | — |
| 11 | math conversion | `Math` |
| 13 | nexus: Text Doc (Yjs CRDT) | `TextDoc` |
| 20 | stickyNote | `Sticky` |
| 21 | shape: shapes and text boxes | `Box` |
| 22 | line: lines and connectors | `Line` |

Deleted elements stay in the file with meta field 3 = 1. Record order is
z-order, except that images and widgets always draw under ink.

### 7 — ink (penStroke2)

`{1 id, 2 geometry blob, 3 type, 4 color {1 r, 2 g, 3 b, 4 a}, 5 blendMode
(1 = MULTIPLY: highlighter), 6 translation {1 dx, 2 dy} (lasso moves add
this to every point), 7 position {1 stamp}, 9 shape descriptor, 10
elementId (group), 11 lineStyle, 15 version, 16 erasers, 17 tag, 18
ignoredInAIChecks, 20 tapeStyle, 21 schemaVersion}`. Proto3 drops zero
values, so a missing color channel is 0.

| type (3) | pen | template schema |
|---|---|---|
| 0 CONSTANT_WIDTH | ballpoint, highlighter, shape tool | `vuA(v)A(S(uu))A(S(uuuu))vA(f)` (older files: without `vA(f)`) |
| 1 VARIABLE_WIDTH | fountain pen, marker/tape | `vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` |
| 4 DYNAMIC_WIDTH | brush | `vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)` |
| 5 PENCIL | pencil | `vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)` |

**Constant width:** version 2, thickness, command kinds (0 moveTo, 1
quadCurveTo), moves `(x, y)`, quads `(cx, cy, x, y)`, 1, width samples.

**Variable width (fountain):** version 2, then the centerline: command
kinds, move floats, quad floats; then the precomputed **outline** GoodNotes
fills: per-segment command counts, command codes, move floats, quad floats,
cubic floats, arc floats, arc-clockwise flags. Centerline commands are
`0 moveTo (x y w)`, `1 quadCurveTo (cx cy cw x y w)`, `4 moveToWithRotation
(x y w rot)` and `5 quadCurveToWithRotation (9 floats)` for the marker's
fixed-angle nib. Outline codes: `0 moveTo·2`, `1 quad·4`, `2 cubic·6`,
`3 arc·5 (cx cy r a0 a1)`, `6 elliptical arc·7 (cx cy rx ry a0 a1 rot)`.
There is exactly **one closed outline subpath per quad segment** (the
protobuf model keeps each segment's outline on its quadCurveTo, and the
deserializer checks the per-segment counts; a mismatch is the crash earlier
versions of this library caused). GoodNotes writes each subpath as: move,
start-cap arc, cubics along one side, end-cap arc, cubics back; every cap
has the clockwise flag set, meaning a decreasing angle.

**Dynamic width (brush):** as fountain plus a thickness, with a nine-member
command enum: `0 constantMoveTo, 1 constantQuadCurveTo, 2 variableMoveTo,
3 variableQuadCurveTo, 4 variableCubicCurveTo, 5 variableAddArc,
6 variableMoveToWithRotation, 7 variableQuadCurveToWithRotation,
8 variableAddEllipticalArc`; the outline uses codes 2–8.

**Pencil:** points `x y azimuth altitude force`; each quad starts with a
random texture seed.

Shape-tool strokes keep an empty path plus descriptor 9 `{1 polyline {1
point…}, 2 quadCurve {p0 p1 p2}, 3 rectangle {1 center, 2 size}, 4 ellipse
{1 center, 2 radii, 3 rotation}, 5 lineStyle, 15 thickness}`. Dashed ink:
`11 {2 {1 lineDashLengths (packed float32, in multiples of width), 2
lineCap (0 BUTT, 1 ROUND, 2 SQUARE)}}`. Tape: `20 {1 attachmentId, 2
rotation, 3 rotationAnchor, 4 thickness, 5 scale}`. Partial erasing stores
eraser strokes in 16 (`PEraserStroke`, schema `vS(vvvvvvvv)S(uuuuuu)`).

### 21 — shape or text box

`{1 id, 2 schemaVersion (35), 3 version, 4 deleted, 5 tag, 6 parent, 7
position, 8 elementID (group), 9 kind (0 shape, 1 textbox), 10 lockStatus,
20 transform {1 translation (origin), 2 rotation (radians), 3 scale}, 21
sizingStrategy, 22 geometry, 30 fill {1 shading {1 color}}, 31 stroke, 32
text, 33 shadow {1 color, 2 radius, 3 offset}}`.

Sizing: `21 {1 fixedWidthFixedHeight {w, h}}`, `{2 fixedWidthAutoHeight
{width, minHeight, maxHeight}}` (shapes), or `{3 autoWidthAutoHeight
{minW, maxW, minH, maxH}}` with `9 = 1` — the text tool's box, which
**wraps at maxW**; text in a fixed box is clipped.

Geometry: `{1 roundedRectangle {1 cornerRadius}}` (clamped to half the
short side, so a big radius is a pill), `{2 ellipse}`, `{3 hobbyPath {1
contours {1 points {1 location, 2 corner, 3 cornerRadius}, 2 closed}}}`
(vertices in the unit square).

Stroke style: `{1 width, 2 pattern {1 solid | 2 dashed {1 dash, 2 gap}},
3 shading {1 color}}` (dash/gap in multiples of width).

Text (32): `{1 raw {1 type (0 ATTRIBUTED_TEXT), 2 rich-text blob, 3 8-byte
content hash, 4 version}, 2 measured size, 5 defaultAttributes {1 inline,
2 block}, 10 padding, 11 lineHeight}`. The hash algorithm is unknown;
GoodNotes accepts any value.

**Rich text** blob: `{1 segments}`, segment `{1 text, 2 inlineAttrs, 3
blockAttrs}`. Inline: `1 strike, 2 underline, 3 color, 4 link, 5
highlight, 30 fontFamily, 40 fontSize, 50 fontItalic, 60 fontWeight (-30
bold), 70 fontWidth`; -404 = inherit the box default. Block: `1 blockType
{1 paragraph | 2 heading {1 level}}, 2 lineHeight {1 type (0 AUTOMATIC, 1
FIXED), 2 value}, 3 listStyle {1 type (0 BULLETED, 1 NUMBERED), 2
startingItemNumber}, 4 textAlignment (0 LEFT, 1 CENTER, 2 RIGHT, 3
JUSTIFIED), 5 paragraphSpacingAfter, 6 paragraphSpacingBefore, 7
indentationLevel`. Links to pages are
`https://app.goodnotes.com/documents/?anchor=<b64 "page-<page id>">#<b64 doc id>`.

### 20 — sticky note

As 21 with `30 color, 31 richText, 32 authorId, 33 authorName, 40 expanded,
41 showAuthor`.

### 22 — line / connector

`20 hobby {1 start {1 point, 2 connection {1 anchor, 2 angle}}, 2 middle
points, 3 end}` straight or curved (the middle point lies on the curve);
`21 elbow {1 start, 2 middlePoints, 3 end, 4 startDirection (0 horizontal,
1 vertical), 5 rotation {1 angle, 2 origin}}`; `30 startArrowStyle`, `31
endArrowStyle` (0 none, 1 line, 2 triangle); `32 stroke` as above.

### 1 — image, sticker, element

`2 rect {origin, size}` axis-aligned bounds of the rotated image, `3
actualRect {center, size, 3 angle}`, `4 attachmentId`, `6 format (0 PNG, 1
JPEG, 2 JPEG2000, 3 PDF, 4 HEIC, 5 GIF)`, `7 elementId`, `19 mathGraphId`,
`20 mathGraphData` (graph widget JSON), `21 lockStatus`. Stickers and
elements are vector PDF attachments. Cropping writes a new attachment.

### 9 — filled shape

`2 rect, 3 position, 4 shapes (as the stroke descriptor: 3 rectangle, 4
ellipse, 1 polyline), 5 originalStrokeIds, 6 translation, 7 color`.

### 11 — math

`2/3 rects as image, 4 attachment (rendered image), 9 dataString (LaTeX),
10 originalStrokeData, 11 originalColoredPenStroke {1 data, 2 color, 3
type, 4 translation}, 12 mathGroupId`.

### 13 — Text Doc

`10 yjsData, 11 yjsSnapshot, 12 clientId, 13 thumbnailData`: a Yjs
document; not decoded beyond extracting readable strings.

## Edit log (`index.events.pb`)

Events are `{1 aggregate id, N body}`; `schema.EVENT` names all N. Bodies
carry `10 timestamp (ms, double)`, `11 event id`, `13 replicaId (device)`,
`14 logical_timestamp` and a `schemaVersion`; attribute values are
last-writer-wins registers `{1 value, 2 stamp {1 counter, 2 random}}`.

| N | body |
|---|---|
| 30 documentCreated | `1 doc, 2 name, 3 parent folder, 6 orientation ("P"/"L"), 7 root folder, 9 recognition language, 20 schemaVersion` |
| 31 documentRenamed, 34 documentFavourited | `2 name` / `2 favourited` |
| 2 templateCreated (paper) | `2 templateId, 4 background attachment, 5 pageNumber, 6 printingPageNumber, 7 lineHeight, 8 size {w, h}, 9 templateLibraryDocumentId (built-in paper), 12 lineHeightV2, 13 lineHeightEnabled, 17 backgroundStyle, 18 textAreas, 19 isFlashCard, 21 schemaVersion` |
| 6 attachmentCreated | `1 id, 2 path, 5 byte size, 6 doc, 12 pageRange {1 first, 2 count}` — **required**, or GoodNotes ignores the attachment |
| 54 pageCreated | `2 pageId, 3 templateId, 4 position (order key), 17 canvasTemplate` |
| 55 pageMoved / 56 pageDeleted | `3 position` / `3 deleted, 4 deletionGroupId` |
| 10 currentPageDidSet | `2 pageId, 3 initiator` |
| 104 notesIndexUpdated, 105 indexStoreUpdated, 102 notesItemsUpdatedV2 | link a page's notes group (ink file) to the document |
| 57 pageFavourited (bookmark) · 63 pageRotationDidSet (`3` quarter turns) · 64 pageLabelsDidSet · 110 pageReadDidSet | per-page attributes |
| 65 pageOutlineDidSet | `2 outlineId, 3 deleted, 4 position, 5 title, 6 parentOutlineId, 15 outlineType, 17 doc` (schema 26) |
| 160–165 audio notes | `160: 2 attachment, 4 durationFlicks, 5 references {1 {1 timestampFlicks, 2 pageId}…}, 6 position`; 164 `3 audioName` |
| 210–216 transcriptions | `210: 3 audioId, 4 audioStart, 5 audioEnd, 6 locale, 7 text` |
| 130–137 comments | `130: 3 position, 4 anchor {1 pageId, 2 region, 3 stickyNote {1 expanded, 2 color, 3 size, 4 rotation, 5 position, 6 showAuthor}}, 5 firstCommentId`; `133: 4 content, 6 authorId, 7 authorName`; 132 `3 isResolved` |

Times in audio events are flicks (1/705,600,000 s). **Page order** is the
page events' order keys compared as ASCII strings; the ink for a page
lives in `notes/<page id + 1>` (or in the group named by its 104/105
events). A page's size is `templateCreated.8` in page units; the paper PDF
is in points, 1 unit = 6/11 pt (standard paper: 834.24 × 1078.825 units =
455.04 × 588.45 pt).
