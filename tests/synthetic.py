"""A minimal, made-up .goodnotes file so tests need no real notes."""
import zipfile

from goodnotes.model import LEN, Msg, write_delimited, write_point

DOC, PAGE, NOTES, LAYER = ("D0C00000-0000-4000-8000-000000000000",
                           "9A6E0000-0000-4000-8000-000000000000",
                           "9A6E0000-0000-4000-8000-000000000001",  # page id + 1
                           "1A7E0000-0000-4000-8000-000000000000")


def make(path, pages=1):
    events = [Msg([[1, LEN, DOC.encode()],
                   [30, LEN, Msg([[1, LEN, DOC.encode()], [2, LEN, Msg([[1, LEN, b"Synthetic"]])]])]]),
              Msg([[1, LEN, LAYER.encode()],
                   [2, LEN, Msg([[1, LEN, DOC.encode()], [2, LEN, LAYER.encode()],
                                 [8, LEN, write_point(834.24, 1078.825)]])]])]
    index = []
    files = {}
    for i in range(pages):
        pid = PAGE[:-1] + str(i * 2)
        nid = PAGE[:-1] + str(i * 2 + 1)
        events.append(Msg([[1, LEN, pid.encode()],
                           [54, LEN, Msg([[1, LEN, DOC.encode()], [2, LEN, pid.encode()],
                                          [3, LEN, Msg([[1, LEN, LAYER.encode()]])],
                                          [4, LEN, Msg([[1, LEN, f"K{i}".encode()]])]])]]))
        index.append(Msg([[1, LEN, nid.encode()], [2, LEN, f"notes/{nid}".encode()]]))
        files[f"notes/{nid}"] = b""
    with zipfile.ZipFile(path, "w") as z:
        z.writestr("schema.pb", b"\x01\x1f")
        z.writestr("index.notes.pb", write_delimited(index))
        z.writestr("index.attachments.pb", b"")
        z.writestr("index.events.pb", write_delimited(events))
        for n, d in files.items():
            z.writestr(n, d)
    return path
