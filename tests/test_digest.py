"""Tests for the register/digest split: fast drop, deferred extraction."""

import pytest

from src import config, extract, library


@pytest.fixture
def lib(tmp_path, monkeypatch):
    """A Library backed by a throwaway temp dir, not the real data/ folder."""
    papers_dir = tmp_path / "papers"
    data_dir = tmp_path / "data"
    papers_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setattr(config, "PAPERS_DIR", papers_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "library.db")
    lib = library.Library()
    yield lib
    lib.close()


def _make_txt(tmp_path, name, body):
    p = tmp_path / name
    p.write_text(body, encoding="utf-8")
    return p


def test_register_file_creates_pending_row(lib, tmp_path):
    src = _make_txt(tmp_path, "rl.txt",
                    "reinforcement reinforcement learning agent reward policy " * 10)
    paper = lib.register_file(str(src))
    assert paper["category"] == "Pending"
    assert paper["text"] == ""
    assert paper["keywords"] == []
    assert paper["processed"] == 0
    assert paper["title"] == "rl"  # filename stem
    assert len(lib.all_papers()) == 1


def test_register_file_does_no_extraction(lib, tmp_path, monkeypatch):
    # Registration must never touch the (slow) extraction code path.
    def boom(*args, **kwargs):  # pragma: no cover - only runs on failure
        raise AssertionError("register_file must not extract text")

    monkeypatch.setattr(extract, "extract_text", boom)
    monkeypatch.setattr(extract, "extract_keywords", boom)
    monkeypatch.setattr(extract, "categorize", boom)

    src = _make_txt(tmp_path, "noextract.txt", "some readable content here " * 10)
    paper = lib.register_file(str(src))
    assert paper["text"] == ""
    assert paper["processed"] == 0


def test_register_file_dedups_by_content(lib, tmp_path):
    src = _make_txt(tmp_path, "dup.txt", "duplicate content dedup test " * 10)
    first = lib.register_file(str(src))
    second = lib.register_file(str(src))
    assert first["id"] == second["id"]
    assert len(lib.all_papers()) == 1
    assert len(list(config.PAPERS_DIR.iterdir())) == 1


def test_pending_papers_returns_only_undigested(lib, tmp_path):
    lib.register_file(str(_make_txt(tmp_path, "a.txt", "alpha content here " * 10)))
    lib.register_file(str(_make_txt(tmp_path, "b.txt", "beta content here " * 10)))
    # add_file inserts an already-digested row, which must NOT be pending.
    lib.add_file(str(_make_txt(tmp_path, "c.txt", "gamma content here " * 10)))
    pending = lib.pending_papers()
    assert len(pending) == 2
    assert all(p["processed"] == 0 for p in pending)


def test_digest_paper_populates_and_flips_processed(lib, tmp_path):
    src = _make_txt(tmp_path, "rl.txt",
                    "reinforcement reinforcement learning agent reward policy " * 10)
    paper = lib.register_file(str(src))
    digested = lib.digest_paper(paper["id"])
    assert digested["processed"] == 1
    assert digested["category"] == "Reinforcement"
    assert "reinforcement" in digested["keywords"]
    assert digested["text"].strip() != ""
    # Once digested it drops out of the pending list.
    assert lib.pending_papers() == []


def test_migration_marks_existing_text_rows_processed(tmp_path, monkeypatch):
    # Simulate an OLD DB (pre-`processed` column) with one text-bearing row and
    # one empty-text row, then reopen so _migrate runs.
    import sqlite3

    papers_dir = tmp_path / "papers"
    data_dir = tmp_path / "data"
    papers_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setattr(config, "PAPERS_DIR", papers_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "library.db")

    conn = sqlite3.connect(config.DB_PATH)
    conn.execute(
        "CREATE TABLE papers (id INTEGER PRIMARY KEY AUTOINCREMENT, filename TEXT,"
        " path TEXT, title TEXT, keywords TEXT DEFAULT '[]', category TEXT,"
        " text TEXT DEFAULT '', content_hash TEXT DEFAULT '', added_at TEXT)"
    )
    conn.execute(
        "INSERT INTO papers (filename, path, title, category, text, added_at)"
        " VALUES ('has.txt', 'has.txt', 'Has', 'Cat', 'real text here', '2020')"
    )
    conn.execute(
        "INSERT INTO papers (filename, path, title, category, text, added_at)"
        " VALUES ('empty.txt', 'empty.txt', 'Empty', 'Pending', '', '2020')"
    )
    conn.commit()
    conn.close()

    lib = library.Library()  # opens + migrates
    try:
        pending = lib.pending_papers()
        assert len(pending) == 1
        assert pending[0]["title"] == "Empty"
    finally:
        lib.close()
