"""PaperLib main window.

Layout:
  * A menu bar (File / Edit / Tools / Projects) at the top, like a normal
    desktop app. Projects live under the Projects menu - each entry opens a
    session window with topic-matched papers and a Claude RAG chat.
  * A drop box - drag downloaded PDFs onto it (or click to browse). Dropped
    files are copied into papers/ instantly as "Pending"; the slow
    parsing/keywording/categorizing runs later via Tools > Digest papers.
  * The library on the left, grouped by category. Double-click a paper to read
    its extracted text in the reader panel on the right.
  * Settings (Anthropic API key / model) under the File menu.

Drag-and-drop uses tkinterdnd2 when available; if it isn't installed the app
still runs and you use the "Add files..." button instead.
"""

from __future__ import annotations

import io
import os
import threading
import tkinter as tk
from pathlib import Path
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import config
from . import reader as reader_reflow
from .library import Library
from .project_window import ProjectWindow

# Drag-and-drop is optional; fall back gracefully if tkinterdnd2 is missing.
try:
    from tkinterdnd2 import DND_FILES, TkinterDnD
    _DND_ROOT = TkinterDnD.Tk
    _HAS_DND = True
except Exception:  # pragma: no cover - environment dependent
    _DND_ROOT = tk.Tk
    _HAS_DND = False

# Pillow handles the rendered page/figure images in the reader panel.
try:
    from PIL import Image, ImageTk
    _HAS_PIL = True
except Exception:  # pragma: no cover - environment dependent
    _HAS_PIL = False

# pypdfium2 renders whole PDF pages to images - equations, vector figures and
# exact layout, pixel-perfect. It's the primary reader path; if it's missing we
# fall back to extracting/reflowing the text (see reader_reflow).
try:
    import pypdfium2 as _pdfium
    _HAS_PDFIUM = True
except Exception:  # pragma: no cover - environment dependent
    _HAS_PDFIUM = False

# Guard-rails for the reader so a huge PDF (e.g. a 1000-page textbook) can't
# lock up the worker or exhaust memory. Beyond these we stop and point the user
# at "Open original file".
_READER_MAX_PAGES = 60
_READER_MAX_IMAGES = 80


def _is_reader_noise(text: str) -> bool:
    """True for stray fragments worth dropping from the reader (page numbers,
    lone footnote markers, e.g. "5" or "2 2") - short and with no letters."""
    return len(text) <= 3 and not any(ch.isalpha() for ch in text)


def _import_summary(added: int, skipped: int, empty: int) -> str:
    """Build the final status-bar message after an import run.

    Factored out (and pure) so the counting/wording is unit-testable without a
    GUI. ``empty`` is how many *added* papers had no readable extracted text -
    typically scanned/image-only PDFs added silently as "Uncategorized".
    """
    msg = f"Added {added} paper(s)."
    if empty:
        msg += f" ({empty} had no readable text - likely scanned PDFs)"
    if skipped:
        msg += f" Skipped {skipped} unsupported file(s)."
    return msg


class PaperLibApp:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.lib = Library()
        self._importing = False
        self._current_paper_id: int | None = None
        # Reader state: a generation counter cancels a stale/in-flight PDF render
        # when the user opens another paper; _reader_images keeps PhotoImage refs
        # alive (Tk drops images that nothing references).
        self._reader_gen = 0
        self._reader_started = False
        self._reader_images: list = []

        self.root = _DND_ROOT()
        self.root.title("PaperLib - Research Paper Library")
        self.root.geometry("980x640")
        self.root.minsize(820, 520)

        self._build_ui()
        self.lib.sync_folder()  # pick up anything dropped into papers/ via Explorer
        self._refresh_library()
        self._refresh_projects()

    # ---- layout -------------------------------------------------------

    def _build_ui(self) -> None:
        self._build_menu()

        header = ttk.Frame(self.root, padding=(10, 8))
        header.pack(fill="x")
        ttk.Label(header, text="PaperLib", font=("Segoe UI", 15, "bold")).pack(side="left")

        # Drop box
        self.drop = tk.Label(
            self.root,
            text=self._drop_text(),
            relief="ridge", borderwidth=2, height=4,
            bg="#eef2ff", fg="#3730a3", font=("Segoe UI", 11),
            cursor="hand2",
        )
        self.drop.pack(fill="x", padx=10, pady=(0, 8))
        self.drop.bind("<Button-1>", lambda e: self._browse_files())
        if _HAS_DND:
            self.drop.drop_target_register(DND_FILES)
            self.drop.dnd_bind("<<Drop>>", self._on_drop)

        # Body: library (left) + paper reader (right)
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(body)
        body.add(left, weight=2)
        ttk.Label(left, text="Library (grouped by keyword category)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tree = ttk.Treeview(left, columns=("keywords",), show="tree headings")
        self.tree.heading("#0", text="Paper / Category")
        self.tree.heading("keywords", text="Keywords")
        self.tree.column("keywords", width=200)
        self.tree.pack(fill="both", expand=True, pady=4)
        # Double-click a paper to open it in the reader panel on the right.
        self.tree.bind("<Double-Button-1>", lambda e: self._open_selected_paper())
        lbtns = ttk.Frame(left)
        lbtns.pack(fill="x")
        self.add_btn = ttk.Button(lbtns, text="Add files...", command=self._browse_files)
        self.add_btn.pack(side="left")
        ttk.Button(lbtns, text="Delete selected",
                   command=self._delete_selected_paper).pack(side="left", padx=4)

        # Right: the paper reader panel, populated by a double-click on the left.
        right = ttk.Frame(body)
        body.add(right, weight=3)
        self.reader_title = ttk.Label(right, text="No paper open",
                                      font=("Segoe UI", 12, "bold"),
                                      wraplength=480, anchor="w", justify="left")
        self.reader_title.pack(anchor="w")
        self.reader_meta = ttk.Label(
            right, text="Double-click a paper on the left to read it here.",
            foreground="#6b7280", wraplength=480, anchor="w", justify="left")
        self.reader_meta.pack(anchor="w", pady=(0, 4))

        textwrap = ttk.Frame(right)
        textwrap.pack(fill="both", expand=True)
        self.reader_text = tk.Text(textwrap, wrap="word", state="disabled",
                                   font=("Segoe UI", 12), padx=16, pady=10,
                                   spacing1=2, spacing2=3, spacing3=2,
                                   background="#ffffff", relief="solid", borderwidth=1)
        rscroll = ttk.Scrollbar(textwrap, orient="vertical",
                                command=self.reader_text.yview)
        self.reader_text.configure(yscrollcommand=rscroll.set)
        rscroll.pack(side="right", fill="y")
        self.reader_text.pack(side="left", fill="both", expand=True)
        self._configure_reader_tags()

        self.open_file_btn = ttk.Button(right, text="Open original file",
                                        command=self._open_original_file,
                                        state="disabled")
        self.open_file_btn.pack(anchor="w", pady=(6, 0))

        # Digest progress lives at the very bottom; idle it sits at 0.
        self.progress = ttk.Progressbar(self.root, mode="determinate",
                                        maximum=100, value=0)
        self.progress.pack(fill="x", side="bottom")

        self.status = ttk.Label(self.root, text="", relief="sunken", anchor="w",
                                padding=(6, 2))
        self.status.pack(fill="x", side="bottom")

    def _build_menu(self) -> None:
        """A classic desktop menu bar: File / Edit / Tools / Projects.

        The Projects cascade is (re)populated by ``_refresh_projects``: it lists
        every saved project as its own command that opens that session.
        """
        menubar = tk.Menu(self.root)

        self.file_menu = tk.Menu(menubar, tearoff=0)
        self.file_menu.add_command(label="Add files...", command=self._browse_files)
        self.file_menu.add_command(label="Rescan folder", command=self._rescan)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Settings", command=self._open_settings)
        self.file_menu.add_separator()
        self.file_menu.add_command(label="Exit", command=self.root.destroy)
        menubar.add_cascade(label="File", menu=self.file_menu)

        edit_menu = tk.Menu(menubar, tearoff=0)
        edit_menu.add_command(label="Delete selected paper",
                              command=self._delete_selected_paper)
        menubar.add_cascade(label="Edit", menu=edit_menu)

        self.tools_menu = tk.Menu(menubar, tearoff=0)
        self.tools_menu.add_command(label="Digest papers", command=self._digest_papers)
        menubar.add_cascade(label="Tools", menu=self.tools_menu)

        self.projects_menu = tk.Menu(menubar, tearoff=0)  # filled by _refresh_projects
        menubar.add_cascade(label="Projects", menu=self.projects_menu)

        self.root.config(menu=menubar)

    def _configure_reader_tags(self) -> None:
        """Typographic hierarchy for the reader: headings big/bold, body a touch
        larger for comfortable reading, captions smaller and muted."""
        self.reader_text.tag_configure(
            "heading", font=("Segoe UI", 15, "bold"),
            spacing1=14, spacing3=4, foreground="#111827")
        self.reader_text.tag_configure(
            "body", font=("Segoe UI", 12), spacing1=2, spacing3=8,
            lmargin1=2, lmargin2=2)
        self.reader_text.tag_configure(
            "caption", font=("Segoe UI", 10, "italic"), foreground="#4b5563",
            spacing1=2, spacing3=8, lmargin1=12, lmargin2=12)
        self.reader_text.tag_configure(
            "pagemark", font=("Segoe UI", 9, "bold"), foreground="#9ca3af",
            justify="center", spacing1=18, spacing3=10)

    def _drop_text(self) -> str:
        if _HAS_DND:
            return "Drop downloaded papers (PDF/TXT) here  -  or click to browse"
        return ("Click here to add papers (PDF/TXT)\n"
                "(install tkinterdnd2 to enable drag-and-drop)")

    # ---- drop / import ------------------------------------------------

    def _on_drop(self, event) -> None:
        paths = self._parse_drop(event.data)
        self._import_paths(paths)

    @staticmethod
    def _parse_drop(data: str) -> list[str]:
        """tkdnd gives space-separated paths, brace-wrapping ones with spaces."""
        paths: list[str] = []
        token = ""
        in_brace = False
        for ch in data:
            if ch == "{":
                in_brace = True
            elif ch == "}":
                in_brace = False
                paths.append(token)
                token = ""
            elif ch == " " and not in_brace:
                if token:
                    paths.append(token)
                token = ""
            else:
                token += ch
        if token:
            paths.append(token)
        return paths

    def _browse_files(self) -> None:
        paths = filedialog.askopenfilenames(
            title="Add papers",
            filetypes=[("Papers", "*.pdf *.txt"), ("All files", "*.*")],
        )
        if paths:
            self._import_paths(list(paths))

    def _import_paths(self, paths: list[str]) -> None:
        """Kick off registration on a background thread so the UI stays responsive.

        register_file only copies the file in and records a pending row - the
        slow PDF parsing/keywording is deferred to Digest. It's fast, but we
        still run it on a worker and marshal every UI update back with
        self.root.after(0, ...) to stay uniform with the digest flow. A busy flag
        stops overlapping runs (e.g. a second drop while one is still running).
        """
        if self._importing or not paths:
            return
        self._importing = True
        self._set_import_enabled(False)
        thread = threading.Thread(
            target=self._run_import, args=(list(paths),), daemon=True
        )
        thread.start()

    def _run_import(self, paths: list[str]) -> None:
        """Runs on a worker thread; marshals UI updates back to the main loop."""
        total = len(paths)
        added, skipped = 0, 0
        try:
            for i, path in enumerate(paths, start=1):
                self.root.after(0, self._set_status, f"Adding {i} of {total}...")
                try:
                    self.lib.register_file(path)
                    added += 1
                except Exception:
                    # ValueError = unsupported file; anything else (missing path,
                    # locked/unreadable file, unexpected error) is counted as a
                    # skipped file too so one bad drop can't kill the worker.
                    skipped += 1
        finally:
            # Always re-enable the UI, even if something unexpected blows up.
            self.root.after(0, self._finish_import, added, skipped)

    def _finish_import(self, added: int, skipped: int) -> None:
        """Back on the main thread: re-enable input, refresh, show the summary."""
        self._importing = False
        self._set_import_enabled(True)
        self._refresh_library()  # sets a "N paper(s) in library." status...
        # Registration extracts nothing, so there's no empty-text notice here -
        # that moves to digest. Point the user at the Digest button instead.
        msg = _import_summary(added, skipped, 0)
        if added:
            msg += " Click 'Digest' to process them."
        self._set_status(msg)    # ...must come after _refresh_library's status.

    def _set_import_enabled(self, enabled: bool) -> None:
        """Toggle the drop box / Add / Digest entry points so runs can't overlap."""
        state = "normal" if enabled else "disabled"
        self.add_btn.configure(state=state)
        # The Add/Digest actions also live on the menu bar - keep them in sync.
        for menu, label in ((self.file_menu, "Add files..."),
                            (self.tools_menu, "Digest papers")):
            try:
                menu.entryconfig(label, state=state)
            except tk.TclError:  # pragma: no cover - label lookup shouldn't fail
                pass
        if enabled:
            self.drop.configure(text=self._drop_text(), bg="#eef2ff", cursor="hand2")
        else:
            self.drop.configure(text="Working... please wait", bg="#e5e7eb",
                                cursor="watch")

    # ---- digest -------------------------------------------------------

    def _digest_papers(self) -> None:
        """Process all pending papers on a worker thread, with a progress bar.

        Mirrors the import threading pattern: the slow extraction now lives in
        library.digest_paper and runs off the UI thread; every widget update is
        marshalled back via self.root.after(0, ...).
        """
        if self._importing:
            return
        pending = self.lib.pending_papers()
        if not pending:
            self._set_status("No papers to digest.")
            return
        self._importing = True
        self._set_import_enabled(False)
        ids = [p["id"] for p in pending]
        self.progress.configure(maximum=len(ids), value=0)
        thread = threading.Thread(
            target=self._run_digest, args=(ids,), daemon=True
        )
        thread.start()

    def _run_digest(self, ids: list[int]) -> None:
        """Runs on a worker thread; marshals UI updates back to the main loop."""
        total = len(ids)
        empty = 0
        try:
            for i, pid in enumerate(ids, start=1):
                try:
                    paper = self.lib.digest_paper(pid)
                    if paper is not None and not (paper.get("text") or "").strip():
                        empty += 1
                except Exception:
                    # A single paper failing to digest must not abort the batch
                    # or leave the UI stuck; treat it like an empty/no-text one.
                    empty += 1
                self.root.after(0, self._advance_digest, i, total)
        finally:
            # Always re-enable the UI, even if something unexpected blows up.
            self.root.after(0, self._finish_digest, total, empty)

    def _advance_digest(self, i: int, total: int) -> None:
        self.progress.configure(value=i)
        self._set_status(f"Digesting {i} of {total}...")

    def _finish_digest(self, total: int, empty: int) -> None:
        """Back on the main thread: reset the bar, re-enable input, summarize."""
        self._importing = False
        self.progress.configure(value=0)
        self._set_import_enabled(True)
        self._refresh_library()  # sets a "N paper(s) in library." status...
        msg = f"Digested {total} paper(s)."
        if empty:
            msg += f" ({empty} had no readable text - likely scanned PDFs)"
        self._set_status(msg)    # ...so this summary must come after it.

    def _rescan(self) -> None:
        new = self.lib.sync_folder()
        self._refresh_library()
        self._set_status(f"Rescan complete. {len(new)} new paper(s) found in papers/.")

    # ---- paper reader -------------------------------------------------

    def _open_selected_paper(self) -> None:
        """Double-click handler: load the selected paper into the reader panel."""
        pid = self._selected_paper_id()
        if pid is None:  # a category row (or nothing) was double-clicked
            return
        paper = self.lib.get_paper(pid)
        if paper is not None:
            self._show_paper(paper)

    def _show_paper(self, paper: dict) -> None:
        """Open a paper in the reader panel.

        PDF pages are rasterised to images (pypdfium2) so you see the real paper
        - equations, figures and layout intact - and even before it's digested.
        Text files show their content. The heavy PDF work runs on a worker thread
        and streams in page by page; if pypdfium2 is unavailable it falls back to
        extracting and reflowing the text.
        """
        self._current_paper_id = paper["id"]
        self.reader_title.configure(text=paper["title"] or "(untitled)")
        keywords = ", ".join(paper.get("keywords") or []) or "-"
        self.reader_meta.configure(text=f"{paper['category']}   •   {keywords}")
        self.open_file_btn.configure(state="normal")

        path = paper.get("path") or ""
        if not path or not os.path.exists(path):
            self._reader_gen += 1  # cancel any in-flight render
            self._set_reader_text(
                "The file for this paper could not be found:\n" + (path or "(none)"))
            return
        if path.lower().endswith(".pdf"):
            self._render_pdf_async(path)
        else:
            self._reader_gen += 1  # a plain text file supersedes any PDF render
            try:
                content = Path(path).read_text(encoding="utf-8", errors="ignore")
            except OSError:
                content = paper.get("text") or ""
            self._set_reader_text(content or "(empty file)")

    def _set_reader_text(self, text: str) -> None:
        """Replace the reader with a single block of plain text (no images)."""
        self._reader_images = []
        self.reader_text.configure(state="normal")
        self.reader_text.delete("1.0", "end")
        self.reader_text.insert("1.0", text)
        self.reader_text.see("1.0")
        self.reader_text.configure(state="disabled")

    def _render_pdf_async(self, path: str) -> None:
        """Start a fresh streaming render of a PDF into the reader panel."""
        self._reader_gen += 1
        self._reader_started = False
        self._reader_images = []
        self._set_reader_text("Loading PDF...")
        # Panel width now (main thread) sets the render resolution; fall back to
        # a sensible default if the panel hasn't been laid out yet.
        target_w = max(self.reader_text.winfo_width() - 24, 360)
        thread = threading.Thread(
            target=self._render_pdf_worker,
            args=(path, self._reader_gen, target_w), daemon=True,
        )
        thread.start()

    def _render_pdf_worker(self, path: str, gen: int, target_w: int) -> None:
        """Worker thread: render each page to an image and stream it to the UI.

        Uses pypdfium2 to rasterise real pages (equations/figures/layout intact).
        If pdfium isn't available, falls back to the text-reflow path. Bails out
        as soon as ``gen`` changes (the user opened another paper).
        """
        if not (_HAS_PDFIUM and _HAS_PIL):
            self._render_text_worker(path, gen)
            return
        try:
            doc = _pdfium.PdfDocument(path)
        except Exception:
            self._render_text_worker(path, gen)
            return
        try:
            total = len(doc)
            shown = min(total, _READER_MAX_PAGES)
            for pi in range(shown):
                if gen != self._reader_gen:
                    return  # superseded - stop rendering
                try:
                    page = doc[pi]
                    page_w = page.get_size()[0] or 595.0
                    # Render at ~2x the display width for crispness; _make_photo
                    # then downscales to the panel width.
                    scale = min(max((target_w * 2) / page_w, 1.0), 4.0)
                    pil = page.render(scale=scale).to_pil().convert("RGB")
                    page.close()
                    buf = io.BytesIO()
                    pil.save(buf, format="PNG")
                    data = buf.getvalue()
                except Exception:
                    continue
                blocks: list = [("image", data, None)]
                if pi == shown - 1 and total > shown:
                    blocks.append(("text",
                        f"Showing the first {shown} of {total} pages. "
                        "Use 'Open original file' for the full document.", "caption"))
                self.root.after(0, self._append_reader_blocks, gen, blocks)
        finally:
            try:
                doc.close()
            except Exception:
                pass

    def _render_text_worker(self, path: str, gen: int) -> None:
        """Fallback: extract text + embedded figures per page and stream them.

        Used when pypdfium2/Pillow aren't available. Reflows two-column text into
        reading order and inlines embedded figures (see reader_reflow).
        """
        try:
            from pypdf import PdfReader
            reader = PdfReader(path)
        except Exception as err:
            self.root.after(0, self._append_reader_blocks, gen,
                            [("text", f"Could not open this PDF:\n{err}\n\n"
                                      "Use 'Open original file' to view it.", "body")])
            return

        total = len(reader.pages)
        shown = min(total, _READER_MAX_PAGES)
        budget = _READER_MAX_IMAGES
        for pi in range(shown):
            if gen != self._reader_gen:
                return  # superseded - stop rendering
            page = reader.pages[pi]
            # Split the reflowed text into the running body (headings + paragraphs)
            # and the figure/table captions. Captions read as "randomly in the
            # middle" if left inline, so we collect them and place them *after*
            # the page's figures, where a caption belongs.
            body_blocks: list = []
            caption_blocks: list = []
            try:
                for style, text in reader_reflow.page_blocks(page):
                    text = text.strip()
                    if not text or _is_reader_noise(text):
                        continue
                    if style == "caption":
                        caption_blocks.append(("text", text, "caption"))
                    else:
                        body_blocks.append(("text", text, style))
            except Exception:
                pass
            image_blocks: list = []
            if budget > 0 and _HAS_PIL:
                try:
                    images = page.images
                except Exception:
                    images = []
                for im in images:
                    if budget <= 0:
                        break
                    try:
                        data = im.data
                    except Exception:
                        continue
                    image_blocks.append(("image", data, None))
                    budget -= 1
            # Order per page: marker, body text, then figures with their captions.
            blocks: list = [("text", f"Page {pi + 1}", "pagemark")]
            blocks += body_blocks + image_blocks + caption_blocks
            if pi == shown - 1 and total > shown:
                blocks.append(("text",
                    f"Showing the first {shown} of {total} pages. "
                    "Use 'Open original file' for the full document.", "caption"))
            self.root.after(0, self._append_reader_blocks, gen, blocks)

    def _append_reader_blocks(self, gen: int, blocks: list) -> None:
        """Main thread: append a page's text/figures to the reader as they arrive."""
        if gen != self._reader_gen:
            return  # a newer render (or navigation) has taken over
        self.reader_text.configure(state="normal")
        if not self._reader_started:
            # First batch for this render: clear the "Loading PDF..." placeholder.
            self.reader_text.delete("1.0", "end")
            self._reader_started = True
        for kind, payload, style in blocks:
            if kind == "image":
                photo = self._make_photo(payload)
                if photo is not None:
                    self._reader_images.append(photo)
                    self.reader_text.image_create("end", image=photo)
                    self.reader_text.insert("end", "\n\n")
            else:
                self.reader_text.insert("end", payload + "\n", (style or "body",))
        self.reader_text.configure(state="disabled")

    def _make_photo(self, data: bytes):
        """Decode embedded image bytes and scale them to the panel width."""
        if not _HAS_PIL:
            return None
        try:
            img = Image.open(io.BytesIO(data))
            img.load()
            if img.mode not in ("RGB", "RGBA", "L"):
                img = img.convert("RGB")
            maxw = max(self.reader_text.winfo_width() - 24, 320)
            if img.width > maxw:
                ratio = maxw / img.width
                img = img.resize((maxw, max(1, int(img.height * ratio))))
            return ImageTk.PhotoImage(img)
        except Exception:  # pragma: no cover - malformed/unsupported image
            return None

    def _clear_reader(self) -> None:
        """Reset the panel to its empty state (e.g. the open paper was deleted)."""
        self._reader_gen += 1  # cancel any in-flight render
        self._current_paper_id = None
        self.reader_title.configure(text="No paper open")
        self.reader_meta.configure(
            text="Double-click a paper on the left to read it here.")
        self._set_reader_text("")
        self.open_file_btn.configure(state="disabled")

    def _open_original_file(self) -> None:
        """Launch the open paper's file in the OS default viewer."""
        if self._current_paper_id is None:
            return
        paper = self.lib.get_paper(self._current_paper_id)
        if paper is None:
            return
        try:
            os.startfile(paper["path"])  # Windows-only; this is a Windows app
        except OSError as err:
            messagebox.showerror("Open file", f"Could not open the file:\n{err}")

    # ---- library view -------------------------------------------------

    def _refresh_library(self) -> None:
        self.tree.delete(*self.tree.get_children())
        grouped = self.lib.papers_by_category()
        for category in sorted(grouped):
            parent = self.tree.insert("", "end", text=f"{category} ({len(grouped[category])})",
                                      open=True)
            for paper in grouped[category]:
                self.tree.insert(parent, "end", iid=f"paper:{paper['id']}",
                                 text=paper["title"],
                                 values=(", ".join(paper["keywords"][:6]),))
        total = sum(len(v) for v in grouped.values())
        self._set_status(f"{total} paper(s) in library.")
        # Keep the reader in sync after a refresh: if the open paper was deleted,
        # clear the panel; otherwise just refresh its header (title/keywords/
        # category may have changed after a digest) without re-rendering, so the
        # user's scroll position in the document is preserved.
        if self._current_paper_id is not None:
            paper = self.lib.get_paper(self._current_paper_id)
            if paper is None:
                self._clear_reader()
            else:
                self.reader_title.configure(text=paper["title"] or "(untitled)")
                keywords = ", ".join(paper.get("keywords") or []) or "-"
                self.reader_meta.configure(
                    text=f"{paper['category']}   •   {keywords}")

    def _selected_paper_id(self) -> int | None:
        sel = self.tree.selection()
        if sel and sel[0].startswith("paper:"):
            return int(sel[0].split(":", 1)[1])
        return None

    def _delete_selected_paper(self) -> None:
        pid = self._selected_paper_id()
        if pid is None:
            return
        paper = self.lib.get_paper(pid)
        remove_file = messagebox.askyesno(
            "Delete paper",
            f"Remove '{paper['title']}' from the library?\n\n"
            "Click Yes to also delete the file from papers/, No to keep the file.",
        )
        self.lib.delete_paper(pid, remove_file=remove_file)
        self._refresh_library()

    # ---- projects -----------------------------------------------------

    def _refresh_projects(self) -> None:
        """Rebuild the Projects menu: New..., one command per project, Delete..."""
        self._projects = self.lib.all_projects()
        menu = self.projects_menu
        menu.delete(0, "end")
        menu.add_command(label="New Project Session...", command=self._add_project)
        menu.add_separator()
        if self._projects:
            for i, proj in enumerate(self._projects):
                topic = f" - {proj['topic']}" if proj["topic"] else ""
                menu.add_command(label=f"{proj['name']}{topic}",
                                 command=lambda idx=i: self._open_project_by_index(idx))
        else:
            menu.add_command(label="(no projects yet)", state="disabled")
        menu.add_separator()
        menu.add_command(label="Delete a project...",
                         command=self._delete_project_dialog)

    def _add_project(self) -> None:
        name = simpledialog.askstring("New research session",
                                      "Project name:", parent=self.root)
        if not name:
            return
        topic = simpledialog.askstring(
            "New research session",
            "Research topic (used to pull relevant papers):",
            parent=self.root,
        ) or ""
        project = self.lib.create_project(name.strip(), topic.strip())
        self._refresh_projects()
        ProjectWindow(self.root, self.lib, project)

    def _open_project_by_index(self, idx: int) -> None:
        if 0 <= idx < len(self._projects):
            ProjectWindow(self.root, self.lib, self._projects[idx])

    def _delete_project_dialog(self) -> None:
        """Small picker so a project can be deleted now that the list is a menu."""
        if not self._projects:
            messagebox.showinfo("Delete project", "There are no projects to delete.")
            return
        dlg = tk.Toplevel(self.root)
        dlg.title("Delete a project")
        dlg.geometry("360x260")
        dlg.transient(self.root)
        ttk.Label(dlg, text="Select a project to delete:").pack(
            anchor="w", padx=10, pady=(10, 4))
        listbox = tk.Listbox(dlg)
        for proj in self._projects:
            topic = f" - {proj['topic']}" if proj["topic"] else ""
            listbox.insert("end", f"{proj['name']}{topic}")
        listbox.pack(fill="both", expand=True, padx=10)

        def do_delete() -> None:
            sel = listbox.curselection()
            if not sel:
                return
            project = self._projects[sel[0]]
            if messagebox.askyesno("Delete project",
                                   f"Delete '{project['name']}'?", parent=dlg):
                self.lib.delete_project(project["id"])
                self._refresh_projects()
                dlg.destroy()

        btns = ttk.Frame(dlg, padding=10)
        btns.pack(fill="x")
        ttk.Button(btns, text="Delete", command=do_delete).pack(side="right")
        ttk.Button(btns, text="Cancel", command=dlg.destroy).pack(side="right", padx=6)

    # ---- settings -----------------------------------------------------

    def _open_settings(self) -> None:
        SettingsDialog(self.root)

    # ---- misc ---------------------------------------------------------

    def _set_status(self, text: str) -> None:
        self.status.configure(text=text)

    def run(self) -> None:
        self.root.mainloop()
        self.lib.close()


class SettingsDialog(tk.Toplevel):
    def __init__(self, master):
        super().__init__(master)
        self.title("Settings")
        self.geometry("520x220")
        self.transient(master)
        cfg = config.load_config()

        frm = ttk.Frame(self, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="Anthropic API key:").grid(row=0, column=0, sticky="w")
        self.key_var = tk.StringVar(value=cfg.get("api_key", ""))
        ttk.Entry(frm, textvariable=self.key_var, show="*", width=48).grid(
            row=0, column=1, pady=4)

        env_note = ("Leave blank to use the ANTHROPIC_API_KEY environment "
                    "variable, or a .env file in the project root, instead "
                    "(recommended).")
        ttk.Label(frm, text=env_note, foreground="#6b7280",
                  wraplength=380).grid(row=1, column=1, sticky="w")

        ttk.Label(frm, text="Model:").grid(row=2, column=0, sticky="w", pady=(10, 0))
        self.model_var = tk.StringVar(value=cfg.get("model", config.DEFAULT_MODEL))
        ttk.Entry(frm, textvariable=self.model_var, width=48).grid(
            row=2, column=1, pady=(10, 0))

        btns = ttk.Frame(frm)
        btns.grid(row=3, column=1, sticky="e", pady=14)
        ttk.Button(btns, text="Save", command=self._save).pack(side="right")
        ttk.Button(btns, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def _save(self) -> None:
        cfg = config.load_config()
        cfg["api_key"] = self.key_var.get().strip()
        cfg["model"] = self.model_var.get().strip() or config.DEFAULT_MODEL
        config.save_config(cfg)
        messagebox.showinfo("Settings", "Saved.")
        self.destroy()


def main() -> None:
    PaperLibApp().run()


if __name__ == "__main__":
    main()
