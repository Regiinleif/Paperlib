"""Tests for the SQLite library store, isolated to a temp directory."""

import pytest

from paperlib import config, library


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


def test_add_file_categorizes(lib, tmp_path):
    src = _make_txt(tmp_path, "rl.txt",
                    "reinforcement reinforcement learning agent reward policy " * 10)
    paper = lib.add_file(str(src))
    assert paper["category"] == "Reinforcement"
    assert "reinforcement" in paper["keywords"]
    assert len(lib.all_papers()) == 1


def test_add_file_rejects_unsupported(lib, tmp_path):
    bad = tmp_path / "image.png"
    bad.write_bytes(b"\x89PNG")
    with pytest.raises(ValueError):
        lib.add_file(str(bad))


def test_papers_grouped_by_category(lib, tmp_path):
    lib.add_file(str(_make_txt(tmp_path, "a.txt", "vision vision image image cnn " * 10)))
    lib.add_file(str(_make_txt(tmp_path, "b.txt", "graph graph network node edge " * 10)))
    grouped = lib.papers_by_category()
    assert len(grouped) == 2


def test_project_flow(lib, tmp_path):
    paper = lib.add_file(str(_make_txt(tmp_path, "p.txt", "topic modeling lda " * 10)))
    proj = lib.create_project("My Project", "topic modeling")
    lib.add_paper_to_project(proj["id"], paper["id"])
    assert paper["id"] in lib.manual_project_paper_ids(proj["id"])
    lib.remove_paper_from_project(proj["id"], paper["id"])
    assert paper["id"] not in lib.manual_project_paper_ids(proj["id"])


def test_delete_paper(lib, tmp_path):
    paper = lib.add_file(str(_make_txt(tmp_path, "d.txt", "delete me test " * 10)))
    lib.delete_paper(paper["id"])
    assert lib.all_papers() == []
