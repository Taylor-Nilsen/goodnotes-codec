"""GoodNotes protobuf schema: field numbers and names.

Recovered from the GoodNotes web app (web.goodnotes.com), whose Swift core
ships as WebAssembly with SwiftProtobuf's field-name tables intact. Every
message below is transcribed from those tables, so the numbers and names are
GoodNotes' own, not guesses. Types in comments are inferred from the
.goodnotes files this library was built against.

Only the parts that matter for a .goodnotes file are listed (notes items,
rich text, the edit log). Classroom, marketplace and sync messages are left
out.
"""

# --- index.events.pb -----------------------------------------------------------
# Each record is Event {1 aggregateId, N body}; N is one of these.

EVENT = {
    2: "templateCreated", 3: "pageTemplateDidSet", 4: "notesItemsUpdated",
    5: "templateLineHeightDidSet", 6: "attachmentCreated", 7: "attachmentDeleted",
    8: "documentTombstoneCreated", 9: "pageTombstoneCreated", 10: "currentPageDidSet",
    11: "currentToolDidSet", 14: "tombstoneCreated", 16: "templateCustomThemesDidSet",
    30: "documentCreated", 31: "documentRenamed", 32: "documentDeleted", 33: "documentMoved",
    34: "documentFavourited", 35: "documentRestored", 41: "documentPasswordProtected",
    51: "pagesCreated", 52: "pagesMoved", 53: "pagesDeleted", 54: "pageCreated",
    55: "pageMoved", 56: "pageDeleted", 57: "pageFavourited", 58: "pagesRestored",
    59: "pageRestored", 60: "recognitionLanguageCodeDidSet", 61: "templateTombstoneCreated",
    62: "pageOutlineCreated", 63: "pageRotationDidSet", 64: "pageLabelsDidSet",
    65: "pageOutlineDidSet", 101: "notesItemsStateUpdated", 102: "notesItemsUpdatedV2",
    103: "attachmentIndexUpdated", 104: "notesIndexUpdated", 105: "indexStoreUpdated",
    106: "notesItemImported", 108: "documentSeenVersionDidSet", 109: "pageSeenVersionDidSet",
    110: "pageReadDidSet", 120: "addonDataUpdated", 121: "fileDeleted",
    130: "commentThreadCreated", 131: "commentThreadAnchorUpdated", 132: "commentThreadResolved",
    133: "commentCreated", 134: "commentEdited", 135: "commentDeleted",
    136: "commentThreadSeenVersionDidSet", 137: "commentThreadErased",
    150: "currentProblemDidSet", 151: "problemCreated", 152: "problemUpdated",
    153: "problemDeleted", 154: "problemRestored", 158: "studySetBackgroundUpdated",
    160: "audioNoteCreated", 161: "audioNoteReferencesUpdated", 162: "audioNotePositionMoved",
    163: "audioNoteDeleted", 164: "audioNoteRenamed", 165: "audioNoteDocumentRelationCreated",
    210: "transcriptionCreated", 211: "transcriptionTextUpdated", 212: "transcriptionDeleted",
    213: "transcriptionBatchCreated", 215: "transcriptionSummaryCreated",
    216: "transcriptionSummaryDeleted", 220: "embeddedAudioGroupCreated",
    221: "embeddedAudioCreated", 222: "embeddedAudioGroupCurrentFlickDidSet",
    223: "embeddedAudioGroupAnchorUpdated", 230: "timeKeeperTimerStarted",
    240: "timeKeeperStopwatchStarted", 280: "pageSmartTapeEnabled",
    281: "pageSmartTapeStateDidSet", 290: "patternCreated", 291: "patternDeleted",
    292: "pageCanvasTemplateDidSet", 300: "transcriptionLiveSummaryCreated",
}

# Common tail of every event body: 10 timestamp (ms, double), 11 event id,
# 12 userId, 13 replicaId, 14 logical_timestamp, 15 schemaVersion (the
# exact numbers shift by one on a few bodies; see each message).
EVENT_BODY = {
    "templateCreated": {1: "documentId", 2: "templateId", 4: "attachmentId", 5: "pageNumber",
                        6: "printingPageNumber", 7: "lineHeight", 8: "size",
                        9: "templateLibraryDocumentId", 10: "timestamp", 11: "id", 12: "lineHeightV2",
                        13: "lineHeightEnabled", 14: "userId", 15: "replicaId", 16: "logical_timestamp",
                        17: "backgroundStyle", 18: "textAreas", 19: "isFlashCard", 20: "themes",
                        21: "schemaVersion"},
    "pageTemplateDidSet": {1: "documentId", 2: "pageId", 3: "templateId"},
    "attachmentCreated": {1: "attachmentId", 2: "path", 3: "source", 4: "checksum", 5: "size",
                          6: "documentId", 7: "password", 8: "uti", 9: "drmId", 10: "timestamp",
                          11: "id", 12: "pageRange", 13: "userId", 14: "replicaId",
                          15: "logical_timestamp", 16: "schemaVersion", 17: "nexusId"},
    "currentPageDidSet": {1: "documentId", 2: "pageId", 3: "initiator"},
    "documentCreated": {1: "documentId", 2: "parentFolderId", 3: "position", 4: "rootFolderId"},
    "documentRenamed": {1: "documentId", 2: "name"},
    "documentFavourited": {1: "documentId", 2: "favourited"},
    "pagesCreated": {1: "documentId", 2: "templateIds", 3: "pageIds", 4: "afterPageId", 5: "beforePageId"},
    "pagesMoved": {1: "documentId", 2: "pageIds", 4: "afterPageId", 5: "beforePageId"},
    "pagesDeleted": {1: "documentId", 2: "pageIds"},
    "pageCreated": {1: "documentId", 2: "pageId", 3: "templateId", 4: "position", 16: "templateType",
                    17: "canvasTemplate", 18: "nexusID"},
    "pageMoved": {1: "documentId", 2: "pageId", 3: "position"},
    "pageDeleted": {1: "documentId", 2: "pageId", 3: "deleted", 4: "deletionGroupId"},
    "pageFavourited": {1: "documentId", 2: "pageId", 3: "favourited"},
    "pageOutlineCreated": {1: "pageId", 2: "outlineId", 3: "rect", 4: "parentOutlineId", 5: "title",
                           6: "position", 7: "version", 15: "outlineType", 16: "schemaVersion",
                           17: "documentId"},
    "pageRotationDidSet": {1: "documentId", 2: "pageId", 3: "rotation", 16: "schemaVersion"},
    "pageLabelsDidSet": {1: "documentId", 2: "pageId", 3: "labels"},
    "pageOutlineDidSet": {1: "pageId", 2: "outlineId", 3: "deleted", 4: "position", 5: "title",
                          6: "parentOutlineId", 15: "outlineType", 16: "schemaVersion", 17: "documentId"},
    "pageReadDidSet": {1: "pageId", 2: "documentId", 3: "read"},
    "notesItemsUpdatedV2": {1: "groupId", 2: "items", 16: "documentId"},
    "notesIndexUpdated": {1: "pageId", 2: "documentId", 3: "notesItemGroupId"},
    "commentThreadCreated": {1: "threadId", 2: "documentId", 3: "position", 4: "anchor",
                             5: "firstCommentId", 15: "tag", 16: "schemaVersion"},
    "commentThreadAnchorUpdated": {1: "threadId", 2: "documentId", 3: "anchor"},
    "commentThreadResolved": {1: "threadId", 2: "documentId", 3: "isResolved"},
    "commentThreadErased": {1: "threadId", 2: "documentId", 3: "isErased"},
    "commentCreated": {1: "commentId", 2: "threadId", 3: "documentId", 4: "content", 5: "position",
                       6: "authorId", 7: "authorName"},
    "commentEdited": {1: "commentId", 2: "threadId", 3: "documentId", 4: "content"},
    "commentDeleted": {1: "commentId", 2: "threadId", 3: "documentId", 4: "isDeleted"},
    "audioNoteCreated": {1: "audioId", 2: "attachmentId", 3: "documentId", 4: "durationFlicks",
                         5: "references", 6: "position"},
    "audioNoteReferencesUpdated": {1: "audioId", 2: "documentId", 3: "references"},
    "audioNotePositionMoved": {1: "audioId", 2: "documentId", 3: "position"},
    "audioNoteDeleted": {1: "audioId", 2: "documentId"},
    "audioNoteRenamed": {1: "audioId", 2: "documentId", 3: "audioName"},
    "transcriptionCreated": {1: "transcriptionId", 2: "documentId", 3: "audioId", 4: "audioStart",
                             5: "audioEnd", 6: "localeIdentifier", 7: "text"},
    "transcriptionTextUpdated": {1: "transcriptionId", 2: "documentId", 3: "text"},
    "embeddedAudioGroupCreated": {1: "embeddedAudioGroupId", 2: "documentId", 3: "pageId", 4: "anchor",
                                  5: "currentFlick"},
    "embeddedAudioCreated": {1: "embeddedAudioId", 2: "embeddedAudioGroupId", 3: "documentId",
                             4: "attachmentId", 5: "durationFlicks", 6: "position", 7: "audioName"},
    "patternCreated": {1: "patternId", 2: "patternLibraryDocumentId", 3: "attachmentId",
                       4: "foregroundColor", 5: "backgroundColor", 6: "accentColor", 7: "source"},
}

# Small messages used inside events
LWW = {1: "value", 2: "stamp"}                # last-writer-wins register
STAMP = {1: "counter", 2: "random"}           # version stamp
AUDIO_REFERENCE = {1: "timestampFlicks", 2: "pageId"}   # references = {1 values[]}
COMMENT_ANCHOR = {1: "pageId", 2: "region", 3: "stickyNote", 4: "parentId", 5: "migrated"}
COMMENT_STICKY = {1: "expanded", 2: "color", 3: "size", 4: "rotation", 5: "position", 6: "showAuthor"}
FLICKS_PER_SECOND = 705_600_000   # GoodNotes stores audio times in flicks

# --- notes/<id> ------------------------------------------------------------------
# Alternating metadata and payload records. Payload = NotesItem, a oneof:

ITEM = {1: "image", 2: "penStroke", 3: "fillEllipse", 4: "fillPath", 5: "text",
        6: "legacyVariableWidthLine2", 7: "penStroke2", 8: "text2", 9: "fillShape",
        10: "penEraser", 11: "math", 12: "textFragment", 13: "nexus", 15: "loadedAttachments",
        20: "stickyNote", 21: "shape", 22: "line"}

META = {1: "id", 2: "version", 3: "deleted", 4: "attachmentIds", 6: "modifiedId",
        7: "modifiedAttachmentIds", 8: "replicaId", 9: "logical_timestamp", 10: "logical_group",
        11: "importId", 12: "modifiedDocumentIds", 13: "originalDocumentIds", 14: "mergeableHash",
        15: "modifiedPageIds", 16: "schemaVersion", 20: "parentId", 21: "modifiedParentId",
        22: "startConnectionAnchorID", 23: "endConnectionAnchorID"}

PEN_STROKE2 = {1: "id", 2: "data", 3: "type", 4: "color", 5: "blendMode", 6: "translation",
               7: "position", 9: "shape", 10: "elementId", 11: "lineStyle", 14: "deleted",
               15: "version", 16: "erasers", 17: "tag", 18: "ignoredInAIChecks", 19: "logical_group",
               20: "tapeStyle", 21: "schemaVersion", 22: "mathSuggestionId", 100: "parent"}
STROKE_TYPE = {0: "CONSTANT_WIDTH", 1: "VARIABLE_WIDTH", 2: "LEGACY_VARIABLE_WIDTH",
               3: "LEGACY_VARIABLE_WIDTH2", 4: "DYNAMIC_WIDTH", 5: "PENCIL"}
BLEND_MODE = {0: "NORMAL", 1: "MULTIPLY"}     # highlighter = MULTIPLY
TAPE_STYLE = {1: "attachmentId", 2: "rotation", 3: "rotationAnchor", 4: "thickness", 5: "scale"}
LINE_STYLE = {1: "solid", 2: "pattern"}       # pattern = {1 lineDashLengths[], 2 lineCap}
LINE_CAP = {0: "BUTT", 1: "ROUND", 2: "SQUARE"}
STROKE_SHAPE = {1: "polyline", 2: "quadCurve", 3: "rectangle", 4: "ellipse", 5: "lineStyle",
                15: "thickness"}             # recognized-shape descriptor (field 9)
STROKE_ELLIPSE = {1: "center", 2: "radius", 3: "rotation"}
STROKE_QUAD = {1: "p0", 2: "p1", 3: "p2"}

# Template ("tpl") schemas of the geometry blob at PEN_STROKE2.data, per type
STROKE_SCHEMA = {
    "CONSTANT_WIDTH": "vuA(v)A(S(uu))A(S(uuuu))vA(f)",
    "VARIABLE_WIDTH": "vA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)",
    "DYNAMIC_WIDTH": "vuA(v)A(u)A(u)A(v)A(v)A(u)A(u)A(u)A(u)A(v)",
    "PENCIL": "vuA(v)A(S(uuuuu))A(S(uuuuuuuuuuu))A(S(uu))A(v)A(S(uu))A(S(uuuu))A(u)",
    "LEGACY_VARIABLE_WIDTH2": "A(S(uu))A(S(uu))A(v)",
}
# Command codes inside those blobs. VARIABLE_WIDTH (fountain pen) has one
# enum for both centerline and outline; DYNAMIC_WIDTH (brush) has a nine-member
# enum whose constant-width members may only appear on the centerline.
VW_COMMAND = {0: "moveTo", 1: "quadCurveTo", 2: "cubicCurveTo", 3: "addArc",
              4: "moveToWithRotation", 5: "quadCurveToWithRotation", 6: "addEllipticalArc"}
DW_COMMAND = {0: "constantMoveTo", 1: "constantQuadCurveTo", 2: "variableMoveTo",
              3: "variableQuadCurveTo", 4: "variableCubicCurveTo", 5: "variableAddArc",
              6: "variableMoveToWithRotation", 7: "variableQuadCurveToWithRotation",
              8: "variableAddEllipticalArc"}

IMAGE = {1: "id", 2: "rect", 3: "actualRect", 4: "attachmentId", 5: "position", 6: "format",
         7: "elementId", 14: "deleted", 15: "version", 16: "tag", 17: "logical_group",
         18: "schemaVersion", 19: "mathGraphId", 20: "mathGraphData", 21: "lockStatus", 100: "parent"}
IMAGE_FORMAT = {0: "PNG", 1: "JPEG", 2: "JPEG2000", 3: "PDF", 4: "HEIC", 5: "GIF"}
LOCK_STATUS = {0: "UNLOCKED", 1: "LOCKED"}

MATH = {1: "id", 2: "rect", 3: "actualRect", 4: "attachmentId", 5: "position", 6: "deleted",
        7: "version", 8: "tag", 9: "dataString", 10: "originalStrokeData",
        11: "originalColoredPenStroke", 12: "mathGroupId", 25: "logical_group",
        26: "schemaVersion", 100: "parent"}
COLORED_STROKE = {1: "data", 2: "color", 3: "type", 4: "translation"}

SHAPE = {1: "id", 2: "schemaVersion", 3: "version", 4: "deleted", 5: "tag", 6: "parent",
         7: "position", 8: "elementID", 9: "kind", 10: "lockStatus", 20: "transform",
         21: "sizingStrategy", 22: "geometry", 30: "fill", 31: "stroke", 32: "text", 33: "shadow"}
SHAPE_KIND = {0: "shape", 1: "textbox"}
TRANSFORM = {1: "translation", 2: "rotation", 3: "scale"}
SIZING = {1: "fixedWidthFixedHeight", 2: "fixedWidthAutoHeight", 3: "autoWidthAutoHeight"}
SIZING_FIXED = {1: "width", 2: "height"}
SIZING_AUTO_HEIGHT = {1: "width", 2: "minHeight", 3: "maxHeight"}
SIZING_AUTO = {1: "minWidth", 2: "maxWidth", 3: "minHeight", 4: "maxHeight"}
GEOMETRY = {1: "roundedRectangle", 2: "ellipse", 3: "hobbyPath"}   # roundedRectangle {1 cornerRadius}
HOBBY_PATH = {1: "contours"}            # contour {1 points[], 2 closed}; point {1 location, 2 corner, 3 cornerRadius}
FILL = {1: "shading"}                   # shading {1 color}
SHAPE_STROKE = {1: "width", 2: "pattern", 3: "shading"}   # pattern {1 solid | 2 dashed {1 dash, 2 gap}}
SHADOW = {1: "color", 2: "radius", 3: "offset"}
TEXT_CONTAINER = {1: "raw", 2: "size", 5: "defaultAttributes", 10: "padding", 11: "lineHeight"}
TEXT_RAW = {1: "type", 2: "raw", 3: "hash", 4: "version"}   # type 0 = ATTRIBUTED_TEXT; raw = bv41 blob
DEFAULT_ATTRIBUTES = {1: "inlineAttributes", 2: "blockAttributes"}

STICKY_NOTE = {1: "id", 2: "schemaVersion", 3: "version", 4: "deleted", 5: "tag", 6: "parent",
               7: "position", 8: "elementID", 10: "lockStatus", 20: "transform",
               21: "sizingStrategy", 30: "color", 31: "richText", 32: "authorId",
               33: "authorName", 40: "expanded", 41: "showAuthor"}

LINE = {1: "id", 2: "schemaVersion", 3: "version", 4: "deleted", 5: "tag", 6: "parent",
        7: "position", 8: "elementID", 20: "hobby", 21: "elbow", 30: "startArrowStyle",
        31: "endArrowStyle", 32: "stroke"}
HOBBY_LINE = {1: "start", 2: "middlePoints", 3: "end"}        # start/end = LineEnd
ELBOW_LINE = {1: "start", 2: "middlePoints", 3: "end", 4: "startDirection", 5: "rotation"}
LINE_END = {1: "point", 2: "connection"}                       # connection {1 anchor, 2 angle}
ELBOW_ROTATION = {1: "angle", 2: "origin"}
ARROW_STYLE = {0: "none", 1: "line", 2: "triangle"}
ELBOW_DIRECTION = {0: "horizontal", 1: "vertical"}

TEXT_LEGACY = {1: "id", 2: "rect", 3: "textRect", 4: "textRectTransform", 5: "position", 6: "rtfData",
               7: "borderColor", 8: "borderWidth", 9: "backgroundColor", 10: "padding",
               13: "glyphRects", 14: "deleted", 15: "version", 16: "cornerRadius",
               17: "shadowRadius", 18: "shadowOffset", 19: "shadowColor", 20: "preset",
               21: "lineHeight", 22: "elementId", 23: "shouldClipToRect", 24: "tag",
               25: "logical_group", 27: "schemaVersion", 100: "parent"}
TEXT2 = {1: "id", 2: "rect", 3: "textRect", 4: "position", 5: "content", 6: "borderColor",
         7: "borderWidth", 8: "backgroundColor", 9: "padding", 10: "width", 11: "widthWasSet",
         12: "alignment", 13: "glyphRects", 14: "deleted", 15: "version", 16: "transform",
         17: "elementId", 18: "tag", 19: "logical_group", 20: "schemaVersion"}
NEXUS = {1: "id", 2: "schemaVersion", 3: "version", 4: "deleted", 5: "tag", 10: "yjsData",
         11: "yjsSnapshot", 12: "clientId", 13: "thumbnailData"}       # "Text Docs" (Yjs)
FILL_SHAPE = {1: "id", 2: "rect", 3: "position", 4: "shapes", 5: "originalStrokeIds",
              6: "translation", 7: "color", 8: "elementId"}
PEN_ERASER = {1: "id", 2: "commands", 3: "commandTranslation", 4: "color", 5: "constantRadius",
              6: "blendMode", 7: "position", 8: "rect"}
COLOR = {1: "r", 2: "g", 3: "b", 4: "a", 5: "dynamic"}
POINT = {1: "x", 2: "y"}
RECT = {1: "origin", 2: "size"}

# --- rich text (the bv41 blob at TEXT_RAW.raw) ----------------------------------
RICH_TEXT = {1: "segments"}
SEGMENT = {1: "text", 2: "inlineAttrs", 3: "blockAttrs"}
INLINE_ATTRS = {1: "strike", 2: "underline", 3: "color", 4: "link", 5: "highlight",
                30: "fontFamily", 40: "fontSize", 50: "fontItalic", 60: "fontWeight", 70: "fontWidth"}
BLOCK_ATTRS = {1: "blockType", 2: "lineHeight", 3: "listStyle", 4: "textAlignment",
               5: "paragraphSpacingAfter", 6: "paragraphSpacingBefore", 7: "indentationLevel"}
BLOCK_TYPE = {1: "paragraph", 2: "heading"}          # heading {1 value}
LIST_STYLE = {1: "type", 2: "startingItemNumber"}    # type: 0 BULLETED, 1 NUMBERED
LIST_TYPE = {0: "BULLETED", 1: "NUMBERED"}
LINE_HEIGHT = {1: "type", 2: "value"}                # type: 0 AUTOMATIC, 1 FIXED
TEXT_ALIGNMENT = {0: "LEFT", 1: "CENTER", 2: "RIGHT", 3: "JUSTIFIED"}
INHERIT = -404       # "use the default attributes" sentinel GoodNotes writes in char attrs

PAGE_ROTATION = {0: "CLOCKWISE_0", 1: "CLOCKWISE_90", 2: "CLOCKWISE_180", 3: "CLOCKWISE_270"}
SCHEMA_VERSION = 24


def describe(kind: str) -> dict:
    """Field map for a message name ('SHAPE', 'pageCreated', ...)."""
    return globals().get(kind) or EVENT_BODY[kind]
