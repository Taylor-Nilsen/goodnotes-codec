"""Minimal LZ4 block-format decompressor (no frame header).

GoodNotes stores each stroke's geometry as an LZ4-compressed block wrapped
in a small custom header (see goodnotes.py). Implemented here directly
rather than pulling in a dependency: the block format is tiny, and this
keeps the parser dependency-free.

Format reference: each sequence is
    [token][literal-length extension...][literals][offset:2][match-length extension...]
where token's high nibble is the literal length (15 = read extension bytes,
each 255 meaning "keep reading") and the low nibble is match length minus 4
(15 = read extension the same way). The final sequence in a block is
literals only, with no trailing offset/match.
"""
from __future__ import annotations


def decompress_block(src: bytes, expected_size: int | None = None) -> bytes:
    """Decompresses one raw LZ4 block.

    Raises ValueError if the stream is malformed or, when expected_size is
    given, if the result doesn't match it — callers rely on that to detect
    a wrong guess about where the compressed payload starts.
    """
    dst = bytearray()
    i = 0
    n = len(src)
    while i < n:
        token = src[i]
        i += 1

        literal_len = token >> 4
        if literal_len == 15:
            while True:
                b = src[i]
                i += 1
                literal_len += b
                if b != 255:
                    break
        dst += src[i:i + literal_len]
        i += literal_len

        # a block's last sequence is literals only, with no match after it
        if i >= n:
            break

        offset = src[i] | (src[i + 1] << 8)
        i += 2
        if offset == 0:
            raise ValueError("invalid LZ4 match offset 0")

        match_len = (token & 0xF) + 4
        if (token & 0xF) == 15:
            while True:
                b = src[i]
                i += 1
                match_len += b
                if b != 255:
                    break

        start = len(dst) - offset
        if start < 0:
            raise ValueError("LZ4 match offset points before start of output")
        # byte-at-a-time: matches may overlap the region being written
        # (that's how LZ4 encodes runs), so a slice copy would be wrong
        for k in range(match_len):
            dst.append(dst[start + k])

    if expected_size is not None and len(dst) != expected_size:
        raise ValueError(f"LZ4 output {len(dst)} bytes, expected {expected_size}")
    return bytes(dst)
