"""Turn a PDF page into readable, single-column reading-order blocks.

Papers are typically laid out in two columns: text runs down the left column,
then down the right, then onto the next page. pypdf's plain extraction jumbles
that (it reads across both columns line by line) and its layout mode keeps the
columns side by side - both are painful to read top-to-bottom.

This module reconstructs a natural reading order from the *positions* of the
text (pypdf hands us each fragment's transform via a visitor callback):

  1. group fragments into lines by their y coordinate,
  2. work out the column layout and emit left-column lines then right-column
     lines (full-width lines - titles, spanning figures - break the columns),
  3. merge lines back into paragraphs that re-wrap to the reader's width, joining
     hyphenated word-breaks, and
  4. tag each block as ``heading`` / ``body`` / ``caption`` by font size so the
     UI can render a real typographic hierarchy.

The position/paragraph logic is plain functions over fragment dicts
(``{"t", "x", "y", "size", "bold"}``) so it can be unit-tested without a PDF.
``page_blocks`` is the one function that needs a real pypdf page.
"""

from __future__ import annotations

import re
from collections import Counter

# Some PDFs embed subset fonts with no ToUnicode map; pypdf then emits raw glyph
# names like "/uni00000013" instead of characters. Two cases are recoverable:
#   * a standard Adobe name "/uniXXXX" (<=4 hex) is a real Unicode code point;
#   * a longer "/uniNNNNNNNN" is a glyph id into a font laid out in the standard
#     TrueType order (glyph 3 = space = U+0020), so char = chr(gid + 29).
# Anything that doesn't land in printable ASCII is dropped rather than shown raw.
# Match exactly the 8-hex glyph-id form or the 4-hex Unicode form, so a real
# letter right after a token (e.g. ".../uni00000003b") isn't swallowed as hex.
_UNI_RE = re.compile(r"/uni([0-9A-Fa-f]{8}|[0-9A-Fa-f]{4})")
_TT_GLYPH_OFFSET = 29  # U+0020 (space, 0x20) sits at glyph id 3 -> 0x20 - 3 = 29


def _decode_glyph(match: "re.Match") -> str:
    hexval = match.group(1)
    value = int(hexval, 16)
    if len(hexval) <= 4:
        try:
            return chr(value)
        except ValueError:
            return ""
    ascii_code = value + _TT_GLYPH_OFFSET
    return chr(ascii_code) if 32 <= ascii_code <= 126 else ""


def clean_text(s: str) -> str:
    """Repair the "/uniXXXX" glyph-name artifacts pypdf emits for such fonts."""
    if "/uni" in s:
        s = _UNI_RE.sub(_decode_glyph, s)
    return s

# A line is a horizontal run of fragments sharing (roughly) a y coordinate.
_LINE_Y_TOLERANCE = 3.0
# A line counts as "full width" (not part of a column) if it starts left of and
# ends right of the page midline by at least this margin.
_FULL_WIDTH_MARGIN = 40.0
# A horizontal gap wider than this (in points) between two fragments on the same
# baseline is treated as a column gutter, so two-column rows that share a
# baseline are split into separate column segments rather than merged.
_COLUMN_GAP = 24.0
# Rough average glyph width as a fraction of font size, to estimate where a
# fragment ends (pypdf's visitor gives us a start position but no width).
_AVG_CHAR_WIDTH = 0.5


def _matmul(m1: list[float], m2: list[float]) -> list[float]:
    """Multiply two PDF 2x3 affine matrices (as 6-element lists)."""
    a1, b1, c1, d1, e1, f1 = m1
    a2, b2, c2, d2, e2, f2 = m2
    return [a1 * a2 + b1 * c2, a1 * b2 + b1 * d2,
            c1 * a2 + d1 * c2, c1 * b2 + d1 * d2,
            e1 * a2 + f1 * c2 + e2, e1 * b2 + f1 * d2 + f2]


def collect_fragments(page) -> list[dict]:
    """Pull text fragments (with position, size and boldness) from a pypdf page."""
    fragments: list[dict] = []

    def visit(text, cm, tm, font_dict, font_size):
        if not text or not text.strip():
            return
        trm = _matmul(tm, cm)  # text rendering matrix in user space
        size = abs(font_size) if font_size else abs(trm[3])
        name = str(font_dict.get("/BaseFont", "")) if font_dict else ""
        fragments.append({
            "t": clean_text(text), "x": trm[4], "y": trm[5],
            "size": size, "bold": "bold" in name.lower(),
        })

    try:
        page.extract_text(visitor_text=visit)
    except Exception:
        return []
    return fragments


def _representative(frs: list[dict]) -> tuple[float, bool]:
    """Size/boldness of a line = that of its longest fragment.

    Using the longest fragment (rather than the max size) keeps a stray large
    glyph - a superscript, an inline symbol - from making a body line look like
    a heading.
    """
    longest = max(frs, key=lambda f: len(f["t"].strip()))
    return round(longest["size"], 1), longest["bold"]


def build_lines(fragments: list[dict]) -> list[dict]:
    """Group fragments into lines, top-to-bottom, left-to-right within a line."""
    frs = [f for f in fragments if f["t"].strip()]
    frs.sort(key=lambda f: (-f["y"], f["x"]))
    rows: list[list[dict]] = []
    current: list[dict] = []
    row_y = None
    for f in frs:
        if row_y is None or abs(f["y"] - row_y) <= _LINE_Y_TOLERANCE:
            current.append(f)
            if row_y is None:
                row_y = f["y"]
        else:
            rows.append(current)
            current = [f]
            row_y = f["y"]
    if current:
        rows.append(current)

    lines: list[dict] = []
    for row in rows:
        row.sort(key=lambda f: f["x"])
        for segment in _split_columns(row):
            size, bold = _representative(segment)
            lines.append({
                "x0": segment[0]["x"],
                "x1": max(f["x"] for f in segment),
                "y": segment[0]["y"],
                "t": " ".join(f["t"].strip() for f in segment),
                "size": size,
                "bold": bold,
            })
    return lines


def _split_columns(row: list[dict]) -> list[list[dict]]:
    """Split an x-sorted row wherever a column-gutter-sized gap appears.

    Two-column layouts sometimes share a baseline; without this, the left and
    right column text on that row would be glued into one line.
    """
    segments: list[list[dict]] = []
    segment: list[dict] = [row[0]]
    for prev, frag in zip(row, row[1:]):
        prev_end = prev["x"] + len(prev["t"].strip()) * prev["size"] * _AVG_CHAR_WIDTH
        if frag["x"] - prev_end > _COLUMN_GAP:
            segments.append(segment)
            segment = [frag]
        else:
            segment.append(frag)
    segments.append(segment)
    return segments


def reading_order(lines: list[dict], page_width: float) -> list[dict]:
    """Reorder lines into single-column reading order (left column, then right).

    Full-width lines flush the current column block and stay in place, so a
    spanning title or figure between column runs lands where it belongs.
    """
    mid = page_width / 2

    def is_full_width(l: dict) -> bool:
        return l["x0"] < mid - _FULL_WIDTH_MARGIN and l["x1"] > mid + _FULL_WIDTH_MARGIN

    ordered: list[dict] = []
    block: list[dict] = []

    def flush() -> None:
        left = [l for l in block if (l["x0"] + l["x1"]) / 2 < mid]
        right = [l for l in block if (l["x0"] + l["x1"]) / 2 >= mid]
        ordered.extend(left)
        ordered.extend(right)

    for l in lines:
        if is_full_width(l):
            flush()
            block = []
            ordered.append(l)
        else:
            block.append(l)
    flush()
    return ordered


def body_font_size(fragments: list[dict]) -> int:
    """The dominant body font size = the size covering the most characters."""
    counts: Counter = Counter()
    for f in fragments:
        counts[round(f["size"])] += len(f["t"].strip())
    return counts.most_common(1)[0][0] if counts else 10


def classify_line(line: dict, body_size: int) -> str:
    """Label a line 'heading', 'caption' or 'body' from its size and text."""
    text = line["t"].strip()
    if text[:6].lower().startswith(("figure", "fig.", "fig ", "table")):
        return "caption"
    if line["size"] >= body_size + 1 or (
        line["bold"] and line["size"] >= body_size and len(text) < 70
    ):
        return "heading"
    if line["size"] <= body_size - 1.5:
        return "caption"
    return "body"


def _join(prev: str, nxt: str) -> str:
    """Join two lines of a paragraph, healing hyphenated word-breaks."""
    if prev.endswith("-") and len(prev) > 1 and prev[-2].isalpha() and nxt[:1].isalpha():
        return prev[:-1] + nxt
    return prev + " " + nxt


def paragraphs(ordered_lines: list[dict], body_size: int) -> list[tuple[str, str]]:
    """Merge ordered lines into (style, text) paragraph blocks.

    Body lines flow together (re-wrappable, de-hyphenated) until a vertical gap,
    a style change, or a heading/caption - which are each their own block.
    """
    blocks: list[tuple[str, str]] = []
    buf = ""
    style: str | None = None
    last_y: float | None = None

    for line in ordered_lines:
        text = line["t"].strip()
        if not text:
            continue
        this_style = classify_line(line, body_size)
        gap = last_y is not None and (last_y - line["y"]) > line["size"] * 1.9

        if this_style != "body":
            # Headings and captions stand alone: close any open body paragraph
            # first, then emit this line as its own block.
            if buf:
                blocks.append((style or "body", buf))
                buf = ""
            blocks.append((this_style, text))
            style = None
            last_y = None
            continue

        if buf and (style != "body" or gap):
            blocks.append((style or "body", buf))
            buf = ""
        if not buf:
            buf, style = text, "body"
        else:
            buf = _join(buf, text)
        last_y = line["y"]

    if buf:
        blocks.append((style or "body", buf))
    return blocks


def page_blocks(page) -> list[tuple[str, str]]:
    """Reflow one pypdf page into (style, text) blocks in reading order.

    Falls back to plain text extraction if the visitor yields nothing (some
    PDFs don't drive it), so the reader always shows *something*.
    """
    fragments = collect_fragments(page)
    if not fragments:
        try:
            text = page.extract_text(extraction_mode="layout") or ""
        except Exception:
            try:
                text = page.extract_text() or ""
            except Exception:
                text = ""
        text = clean_text(text)
        return [("body", text)] if text.strip() else []

    try:
        page_width = float(page.mediabox.width)
    except Exception:
        page_width = 612.0  # US Letter default; only used to find the midline
    lines = build_lines(fragments)
    ordered = reading_order(lines, page_width)
    return paragraphs(ordered, body_font_size(fragments))
