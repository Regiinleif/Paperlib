"""Tests for the PDF reflow logic (src.reader).

These exercise the pure position/paragraph functions with synthetic fragment
and line dicts, so no real PDF is needed.
"""

from src import reader


def _frag(t, x, y, size=10.0, bold=False):
    return {"t": t, "x": x, "y": y, "size": size, "bold": bold}


def _line(t, size=10.0, bold=False, y=0.0, x0=0.0, x1=0.0):
    return {"t": t, "size": size, "bold": bold, "y": y, "x0": x0, "x1": x1}


def test_reading_order_left_column_then_right():
    """Two columns sharing a baseline must read down the left, then the right."""
    frags = [
        _frag("LeftA", 50, 700), _frag("RightA", 350, 700),
        _frag("LeftB", 50, 688), _frag("RightB", 350, 688),
    ]
    ordered = reader.reading_order(reader.build_lines(frags), page_width=600)
    texts = [l["t"] for l in ordered]
    # every left-column line comes before every right-column line
    assert texts.index("LeftA") < texts.index("RightA")
    assert texts.index("LeftB") < texts.index("RightA")
    assert texts.index("LeftB") < texts.index("RightB")


def test_full_width_line_breaks_the_columns():
    """A full-width line (title/figure) stays in place between column blocks."""
    lines = [
        _line("L1", y=700, x0=50, x1=250),
        _line("R1", y=700, x0=350, x1=550),
        _line("WIDE", y=650, x0=50, x1=550),
        _line("L2", y=600, x0=50, x1=250),
        _line("R2", y=600, x0=350, x1=550),
    ]
    ordered = [l["t"] for l in reader.reading_order(lines, page_width=600)]
    assert ordered == ["L1", "R1", "WIDE", "L2", "R2"]


def test_classify_line():
    assert reader.classify_line(_line("A Big Heading", size=12), 10) == "heading"
    assert reader.classify_line(_line("Bold Short", size=10, bold=True), 10) == "heading"
    assert reader.classify_line(_line("Figure 1: a caption", size=10), 10) == "caption"
    assert reader.classify_line(_line("tiny note", size=8), 10) == "caption"
    assert reader.classify_line(_line("ordinary body text", size=10), 10) == "body"


def test_paragraphs_merge_and_dehyphenate():
    lines = [
        _line("hyphen-", y=700),
        _line("ated word here", y=690),
    ]
    assert reader.paragraphs(lines, 10) == [("body", "hyphenated word here")]


def test_paragraphs_headings_stand_alone():
    lines = [
        _line("1 Intro", size=12, y=700),
        _line("body one", y=688),
        _line("body two", y=676),
    ]
    blocks = reader.paragraphs(lines, 10)
    assert blocks == [("heading", "1 Intro"), ("body", "body one body two")]


def test_clean_text_decodes_glyphname_artifacts():
    # Glyph-id fallback (8 hex): standard TrueType order, char = chr(gid + 29).
    assert reader.clean_text("/uni00000039/uni00000052/uni0000004f") == "Vol"
    assert reader.clean_text("/uni00000013/uni00000011/uni00000019/uni00000018") == "0.65"
    assert reader.clean_text("a/uni00000003b") == "a b"  # 0x03 -> space
    # Standard Adobe /uniXXXX (<=4 hex) is a real Unicode code point.
    assert reader.clean_text("/uni03A6") == "Φ"  # Greek capital Phi
    # Plain text is untouched.
    assert reader.clean_text("no glyphs here") == "no glyphs here"


def test_paragraph_break_on_vertical_gap():
    """A big vertical gap between body lines starts a new paragraph."""
    lines = [
        _line("first para", size=10, y=700),
        _line("second para", size=10, y=650),  # gap 50 >> 1.9*size
    ]
    blocks = reader.paragraphs(lines, 10)
    assert blocks == [("body", "first para"), ("body", "second para")]
