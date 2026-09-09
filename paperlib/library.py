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

import hashlib
import json
import shutil
import sqlite3
import threading
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
    content_hash TEXT NOT NULL DEFAULT '',
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
        # check_same_thread=False lets the background import worker (app.py) call
        # add_file off the UI thread without sqlite's cross-thread guard tripping.
        # This app has very low concurrency, so we serialize EVERY DB access
        # through self._lock (reentrant, since some methods call others) to keep
        # that safe.
        self._lock = threading.RLock()
        self.conn = sqlite3.connect(config.DB_PATH, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        with self._lock:
            self.conn.executescript(SCHEMA)
            self._migrate()
            self.conn.commit()

    def _migrate(self) -> None:
        """Bring older DBs up to the current schema (backward-compatible)."""
        with self._lock:
            cols = {r["name"] for r in self.conn.execute("PRAGMA table_info(papers)")}
            if "content_hash" not in cols:
                self.conn.execute(
                    "ALTER TABLE papers ADD COLUMN content_hash TEXT NOT NULL DEFAULT ''"
                )

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

        # Dedup on file CONTENT, not on the post-copy path: the same file added
        # twice must not create a second row or a second copy on disk. We hash
        # the source bytes before copying so a duplicate is caught up front.
        content_hash = self._hash_file(source_path)
        with self._lock:
            existing = self.conn.execute(
                "SELECT id FROM papers WHERE content_hash = ?", (content_hash,)
            ).fetchone()
            if existing:
                return self.get_paper(existing["id"])

        dest = config.PAPERS_DIR / source_path.name
        # Avoid clobbering a different file with the same name.
        if source_path.resolve() != dest.resolve():
            dest = self._unique_dest(source_path.name)
            shutil.copy2(source_path, dest)

        text = extract.extract_text(dest)
        keywords = extract.extract_keywords(text)
        category = extract.categorize(keywords)
        title = extract.guess_title(text, fallback=dest.stem)

        with self._lock:
            cur = self.conn.execute(
                "INSERT INTO papers (filename, path, title, keywords, category, text, content_hash, added_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    dest.name,
                    str(dest),
                    title,
                    json.dumps(keywords),
                    category,
                    text,
                    content_hash,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            self.conn.commit()
            return self.get_paper(cur.lastrowid)

    @staticmethod
    def _hash_file(path: Path) -> str:
        """Return a sha256 hex digest of a file's raw bytes."""
        h = hashlib.sha256()
        with open(path, "rb") as f:
            for block in iter(lambda: f.read(65536), b""):
                h.update(block)
        return h.hexdigest()

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
        with self._lock:
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
        with self._lock:
            row = self.conn.execute(
                "SELECT * FROM papers WHERE id = ?", (paper_id,)
            ).fetchone()
        return self._row_to_paper(row) if row else None

    def all_papers(self) -> list[dict]:
        with self._lock:
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
        with self._lock:
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
        with self._lock:
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
        with self._lock:
            rows = self.conn.execute(
                "SELECT * FROM projects ORDER BY created_at DESC"
            ).fetchall()
        return [dict(r) for r in rows]

    def delete_project(self, project_id: int) -> None:
        with self._lock:
            self.conn.execute("DELETE FROM projects WHERE id = ?", (project_id,))
            self.conn.execute(
                "DELETE FROM project_papers WHERE project_id = ?", (project_id,)
            )
            self.conn.commit()

    def add_paper_to_project(self, project_id: int, paper_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "INSERT OR IGNORE INTO project_papers (project_id, paper_id) VALUES (?, ?)",
                (project_id, paper_id),
            )
            self.conn.commit()

    def remove_paper_from_project(self, project_id: int, paper_id: int) -> None:
        with self._lock:
            self.conn.execute(
                "DELETE FROM project_papers WHERE project_id = ? AND paper_id = ?",
                (project_id, paper_id),
            )
            self.conn.commit()

    def manual_project_paper_ids(self, project_id: int) -> set[int]:
        with self._lock:
            rows = self.conn.execute(
                "SELECT paper_id FROM project_papers WHERE project_id = ?", (project_id,)
            ).fetchall()
        return {r["paper_id"] for r in rows}

    def close(self) -> None:
        with self._lock:
            self.conn.close()
