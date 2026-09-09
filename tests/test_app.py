"""Tests for the pure, GUI-free bits of the main window (app.py)."""

from src.app import _import_summary


def test_import_summary_basic():
    assert _import_summary(3, 0, 0) == "Added 3 paper(s)."


def test_import_summary_reports_empty_text():
    # Scanned/image PDFs extract to empty text; the user should be told so
    # instead of them landing silently as "Uncategorized".
    msg = _import_summary(5, 0, 2)
    assert "Added 5 paper(s)." in msg
    assert "2 had no readable text" in msg
    assert "scanned" in msg


def test_import_summary_reports_skipped():
    msg = _import_summary(1, 2, 0)
    assert "Skipped 2 unsupported file(s)." in msg
    assert "no readable text" not in msg


def test_import_summary_reports_empty_and_skipped():
    msg = _import_summary(4, 1, 1)
    assert "Added 4 paper(s)." in msg
    assert "1 had no readable text" in msg
    assert "Skipped 1 unsupported file(s)." in msg
