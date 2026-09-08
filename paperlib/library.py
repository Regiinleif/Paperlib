"""The library: a SQLite store of papers and research projects.

Responsibilities:
  * Import a dropped/added file into papers/ and record its metadata.
  * Extract text once and cache it in the DB (so RAG never re-parses PDFs).
  * List papers, grouped by category.
  * Create research projects and remember manually-added papers.

The DB lives at data/library.db. Full paper text is stored in the DB too -
for a personal library that is simplest and keeps retrieval fast.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from datetime import datetime
from pathlib import Path

from . import config, extract

SCHEMA = """
CREATE TABLE IF NOT EXISTS papers (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    filename   TEXT NOT NULL,
    path       TEXT NOT NULL,
    title      TEXT NOT NULL,
    keywords   TEXT NOT NULL DEFAULT '[]',
    category   TEXT NOT NULL DEFAULT 'Uncategorized',
    text       TEXT NOT NULL DEFAULT '',
    added_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS projects (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    name       TEXT NOT NULL,
    topic      TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS project_papers (
    project_id INTEGER NOT NULL,
    paper_id   INTEGER NOT NULL,
    PRIMARY KEY (project_id, paper_id)
);
"""


class Library:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.conn = sqlite3.connect(config.DB_PATH)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        self.conn.commit()

    # ---- papers -------------------------------------------------------

    def add_file(self, source_path: str | Path) -> dict:
        """Import a file into the library.

        Copies it into papers/ (unless it is already there), extracts text,
        derives keywords + category, and inserts a row. Returns the new paper
        as a dict. Raises ValueError for unsupported/empty files.
        """
        source_path = Path(source_path)
        if source_path.suffix.lower() not in (".pdf", ".txt"):
            raise ValueError(f"Unsupported file type: {source_path.suffix}")

        dest = config.PAPERS_DIR / source_path.name
        # Avoid clobbering a different file with the same name.
        if source_path.resolve() != dest.resolve():
            dest = self._unique_dest(source_path.name)
            shutil.copy2(source_path, dest)

        # Skip if this exact path is already in the library.
        existing = self.conn.execute(
            "SELECT * FROM papers WHERE path = ?", (str(dest),)
        ).fetchone()
        if existing:
            return dict(existing)

        text = extract.extract_text(dest)
        keywords = extract.extract_keywords(text)
        category = extract.categorize(keywords)
        title = extract.guess_title(text, fallback=dest.stem)

        cur = self.conn.execute(
            "INSERT INTO papers (filename, path, title, keywords, category, text, added_at)"
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                dest.name,
                str(dest),
                title,
                json.dumps(keywords),
                category,
                text,
                datetime.now().isoformat(timespec="seconds"),
            ),
        )
        self.conn.commit()
        return self.get_paper(cur.lastrowid)

    def _unique_dest(self, name: str) -> Path:
        dest = config.PAPERS_DIR / name
        if not dest.exists():
            return dest
        stem, suffix = Path(name).stem, Path(name).suffix
        i = 1
        while True:
            candidate = config.PAPERS_DIR / f"{stem} ({i}){suffix}"
            if not candidate.exists():
                return candidate
            i += 1

    def sync_folder(self) -> list[dict]:
        """Import any .pdf/.txt in papers/ that isn't in the DB yet.

        Lets the user drop files into the folder in Explorer and have them
        picked up next time the app scans. Returns the newly added papers.
        """
        known = {row["path"] for row in self.conn.execute("SELECT path FROM papers")}
        added: list[dict] = []
        for path in sorted(config.PAPERS_DIR.iterdir()):
            if path.suffix.lower() in (".pdf", ".txt") and str(path) not in known:
                try:
                    added.append(self.add_file(path))
                except ValueError:
                    continue
        return added

    def get_paper(self, paper_id: int) -> dict:
        row = self.conn.execute(
            "SELECT * FROM papers WHERE id = ?", (paper_id,)
        ).fetchone()
        return self._row_to_paper(row) if row else None

    def all_papers(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM papers ORDER BY category, title"
        ).fetchall()
        return [self._row_to_paper(r) for r in rows]

    def papers_by_category(self) -> dict[str, list[dict]]:
        grouped: dict[str, list[dict]] = {}
        for paper in self.all_papers():
            grouped.setdefault(paper["category"], []).append(paper)
        return grouped

    def delete_paper(self, paper_id: int, remove_file: bool = False) -> None:
        paper = self.get_paper(paper_id)
        if paper and remove_file:
            try:
                Path(paper["path"]).unlink(missing_ok=True)
            except OSError:
                pass
        self.conn.execute("DELETE FROM papers WHERE id = ?", (paper_id,))
        self.conn.execute("DELETE FROM project_papers WHERE paper_id = ?", (paper_id,))
        self.conn.commit()

    @staticmethod
    def _row_to_paper(row: sqlite3.Row) -> dict:
        paper = dict(row)
        paper["keywords"] = json.loads(paper.get("keywords") or "[]")
        return paper

    # ---- projects -----------------------------------------------------

    def create_project(self, name: str, topic: str) -> dict:
        cur = self.conn.execute(
            "INSERT INTO projects (name, topic, created_at) VALUES (?, ?, ?)",
            (name, topic, datetime.now().isoformat(timespec="seconds")),
        )
        self.conn.commit()
        row = self.conn.execute(
            "SELECT * FROM projects WHERE id = ?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)

    def all_projects(self) -> list[dict]:
        rows = self.conn.execute(
            "SELECT * FROM projects ORDER BY created_at DESC"
        ).fetchall()
        return [dict(r) for r in rows]

    def delete_project(self, project_id: int) -> None:
        self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
        self.conn.execute(
            "DELETE FROM project_papers WHERE project_id = ?", (project_id,)
        )
        self.conn.commit()

    def add_paper_to_project(self, project_id: int, paper_id: int) -> None:
        self.conn.execute(
            "INSERT OR IGNORE INTO project_papers (project_id, paper_id) VALUES (?, ?)",
            (project_id, paper_id),
        )
        self.conn.commit()

    def remove_paper_from_project(self, project_id: int, paper_id: int) -> None:
        self.conn.execute(
            "DELETE FROM project_papers WHERE project_id = ? AND paper_id = ?",
            (project_id, paper_id),
        )
        self.conn.commit()

    def manual_project_paper_ids(self, project_id: int) -> set[int]:
        rows = self.conn.execute(
            "SELECT paper_id FROM project_papers WHERE project_id = ?", (project_id,)
        ).fetchall()
        return {r["paper_id"] for r in rows}

    def close(self) -> None:
        self.conn.close()
