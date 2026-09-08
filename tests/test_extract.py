"""Tests for text extraction and keyword categorization."""

from paperlib import extract


def test_extract_keywords_ignores_stopwords():
    text = "The attention mechanism and the transformer model use attention heavily."
    kws = extract.extract_keywords(text, top_n=5)
    assert "attention" in kws
    # common stopwords must not appear
    assert "the" not in kws and "and" not in kws


def test_extract_keywords_ranks_by_frequency():
    text = "graph graph graph network network node"
    kws = extract.extract_keywords(text, top_n=3)
    assert kws[0] == "graph"  # most frequent term first


def test_categorize_uses_top_keyword():
    assert extract.categorize(["diffusion", "image"]) == "Diffusion"


def test_categorize_empty_is_uncategorized():
    assert extract.categorize([]) == "Uncategorized"


def test_extract_text_reads_txt(tmp_path):
    p = tmp_path / "note.txt"
    p.write_text("hello world", encoding="utf-8")
    assert extract.extract_text(p) == "hello world"


def test_extract_text_unsupported_returns_empty(tmp_path):
    p = tmp_path / "data.bin"
    p.write_bytes(b"\x00\x01")
    assert extract.extract_text(p) == ""


def test_guess_title_falls_back(tmp_path):
    assert extract.guess_title("", fallback="mypaper") == "mypaper"
