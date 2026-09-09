"""GUI-level regression tests for the drop/import path through PaperLibApp.

These exercise the REAL threaded drop flow (``_import_paths`` -> worker ->
``_finish_import``) that previously hung forever because the worker called
``self.after(...)`` on a non-widget, killing the thread before the UI was
re-enabled. They need a Tk display, so they self-skip on headless CI.
"""

import os
import time

import pytest

from src import config


@pytest.fixture
def app(tmp_path, monkeypatch):
    """A real PaperLibApp backed by a throwaway temp dir, not the user's data.

    Skips cleanly when there is no display (headless CI), where constructing a
    Tk root raises tk.TclError.
    """
    import tkinter as tk

    papers_dir = tmp_path / "papers"
    data_dir = tmp_path / "data"
    monkeypatch.setattr(config, "PAPERS_DIR", papers_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "library.db")
    monkeypatch.setattr(config, "CONFIG_PATH", data_dir / "config.json")

    from src.app import PaperLibApp

    # Constructing the Tk root raises tk.TclError on a headless box (no display)
    # -> skip. tkinterdnd2 can also throw a *transient* TclError re-loading its
    # Tcl package when a prior root was created/destroyed earlier in the same
    # process, so retry a few times and only skip if it never comes up.
    instance = None
    last_err = None
    for _ in range(4):
        try:
            instance = PaperLibApp()
            break
        except tk.TclError as err:
            last_err = err
            time.sleep(0.1)
    if instance is None:
        pytest.skip(f"no display / Tk unavailable: {last_err}")

    instance.root.withdraw()
    try:
        yield instance
    finally:
        # Flush any pending events before tearing the root down; destroying a
        # tkinterdnd2 root without draining first can leave Tcl state that makes
        # the NEXT test's root creation flaky.
        try:
            instance.root.update()
        except tk.TclError:
            pass
        try:
            instance.root.destroy()
        except tk.TclError:
            pass
        instance.lib.close()


def _pump_until_idle(app, timeout=5.0):
    """Drive the Tk event loop until the import flag clears or we time out.

    A background worker's cross-thread ``root.after(...)`` callbacks are only
    marshalled onto the UI thread while it is inside a real ``mainloop()`` (not
    while spinning on ``update()``), so we run the loop for real and poll the
    busy flag from a main-thread watchdog that quits the loop when idle.
    """
    start = time.monotonic()
    result = {"elapsed": timeout}

    def poll():
        elapsed = time.monotonic() - start
        if not app._importing or elapsed >= timeout:
            result["elapsed"] = elapsed
            app.root.quit()  # exit mainloop without destroying the window
            return
        app.root.after(20, poll)

    app.root.after(20, poll)
    app.root.mainloop()
    return result["elapsed"]


def test_valid_drop_completes_fast(app, tmp_path):
    """A valid dropped file must import and re-enable the UI well under timeout.

    Before the fix (worker calling self.after on a non-widget) this hangs to
    the full timeout with zero papers added; after the fix it finishes fast.
    """
    src = tmp_path / "note.txt"
    src.write_text("a small sample paper about testing", encoding="utf-8")

    app._import_paths([str(src)])
    elapsed = _pump_until_idle(app, timeout=5.0)

    assert app._importing is False
    assert elapsed < 2.0  # must not crawl to the timeout
    papers = app.lib.all_papers()
    assert len(papers) == 1
    assert papers[0]["category"] == "Pending"


def test_bad_path_does_not_hang(app):
    """A dropped path that cannot be read must not wedge the UI forever.

    The worker should recover (count it as skipped) and re-enable input rather
    than dying and leaving ``_importing`` stuck True.
    """
    app._import_paths(["C:/nope/does/not/exist.pdf"])
    elapsed = _pump_until_idle(app, timeout=5.0)

    assert app._importing is False
    assert elapsed < 2.0


def _reader_text(app):
    return app.reader_text.get("1.0", "end").strip()


def test_double_click_opens_digested_paper_in_reader(app, tmp_path):
    """Selecting a digested paper and firing the double-click handler shows its
    extracted text in the right-hand reader panel and enables 'Open file'."""
    src = tmp_path / "paper.txt"
    src.write_text("unique reader body content about transformers", encoding="utf-8")
    paper = app.lib.add_file(str(src))  # add_file inserts an already-digested row
    app._refresh_library()

    app.tree.selection_set(f"paper:{paper['id']}")
    app._open_selected_paper()

    assert app._current_paper_id == paper["id"]
    assert "unique reader body content" in _reader_text(app)
    assert str(app.open_file_btn["state"]) == "normal"


def test_reader_shows_content_before_digest(app, tmp_path):
    """A pending (not-yet-digested) paper is still readable: the reader shows the
    file's actual content straight from disk, not a blank panel or a stub."""
    src = tmp_path / "pending.txt"
    src.write_text("readable body before any digest happens", encoding="utf-8")
    paper = app.lib.register_file(str(src))  # pending row, no extracted text yet
    app._refresh_library()

    app.tree.selection_set(f"paper:{paper['id']}")
    app._open_selected_paper()

    assert "readable body before any digest" in _reader_text(app)


def test_reader_handles_missing_file(app, tmp_path):
    """If a paper's file is gone, the reader says so instead of crashing."""
    src = tmp_path / "gone.txt"
    src.write_text("temporary", encoding="utf-8")
    paper = app.lib.register_file(str(src))
    os.remove(paper["path"])  # delete the copied file out from under the app
    app._refresh_library()

    app.tree.selection_set(f"paper:{paper['id']}")
    app._open_selected_paper()

    assert "could not be found" in _reader_text(app)
