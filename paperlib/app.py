"""PaperLib main window.

Layout:
  * A drop box at the top - drag downloaded PDFs onto it (or click to browse).
    Dropped files are copied into papers/, parsed, keyworded and categorized.
  * The library on the left, grouped by category.
  * Research projects on the right - "Add Project Session" opens a session
    window with topic-matched papers and a Claude RAG chat.
  * Settings for the Anthropic API key / model.

Drag-and-drop uses tkinterdnd2 when available; if it isn't installed the app
still runs and you use the "Add files..." button instead.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import filedialog, messagebox, simpledialog, ttk

from . import config
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


class PaperLibApp:
    def __init__(self) -> None:
        config.ensure_dirs()
        self.lib = Library()

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
        header = ttk.Frame(self.root, padding=(10, 8))
        header.pack(fill="x")
        ttk.Label(header, text="PaperLib", font=("Segoe UI", 15, "bold")).pack(side="left")
        ttk.Button(header, text="Settings", command=self._open_settings).pack(side="right")
        ttk.Button(header, text="Rescan folder",
                   command=self._rescan).pack(side="right", padx=6)

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

        # Body: library (left) + projects (right)
        body = ttk.PanedWindow(self.root, orient="horizontal")
        body.pack(fill="both", expand=True, padx=10, pady=(0, 10))

        left = ttk.Frame(body)
        body.add(left, weight=2)
        ttk.Label(left, text="Library (grouped by keyword category)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.tree = ttk.Treeview(left, columns=("keywords",), show="tree headings")
        self.tree.heading("#0", text="Paper / Category")
        self.tree.heading("keywords", text="Keywords")
        self.tree.column("keywords", width=260)
        self.tree.pack(fill="both", expand=True, pady=4)
        lbtns = ttk.Frame(left)
        lbtns.pack(fill="x")
        ttk.Button(lbtns, text="Add files...", command=self._browse_files).pack(side="left")
        ttk.Button(lbtns, text="Delete selected",
                   command=self._delete_selected_paper).pack(side="left", padx=4)

        right = ttk.Frame(body)
        body.add(right, weight=1)
        ttk.Label(right, text="Research projects",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.projects_list = tk.Listbox(right, height=15)
        self.projects_list.pack(fill="both", expand=True, pady=4)
        self.projects_list.bind("<Double-Button-1>", lambda e: self._open_project())
        rbtns = ttk.Frame(right)
        rbtns.pack(fill="x")
        ttk.Button(rbtns, text="Add Project Session",
                   command=self._add_project).pack(side="left")
        ttk.Button(rbtns, text="Open", command=self._open_project).pack(side="left", padx=4)
        ttk.Button(rbtns, text="Delete", command=self._delete_project).pack(side="left")

        self.status = ttk.Label(self.root, text="", relief="sunken", anchor="w",
                                padding=(6, 2))
        self.status.pack(fill="x", side="bottom")

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
        added, skipped = 0, 0
        for path in paths:
            try:
                self.lib.add_file(path)
                added += 1
            except ValueError:
                skipped += 1
        self._refresh_library()
        msg = f"Added {added} paper(s)."
        if skipped:
            msg += f" Skipped {skipped} unsupported file(s)."
        self._set_status(msg)

    def _rescan(self) -> None:
        new = self.lib.sync_folder()
        self._refresh_library()
        self._set_status(f"Rescan complete. {len(new)} new paper(s) found in papers/.")

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
        self.projects_list.delete(0, "end")
        self._projects = self.lib.all_projects()
        for proj in self._projects:
            topic = f" - {proj['topic']}" if proj["topic"] else ""
            self.projects_list.insert("end", f"{proj['name']}{topic}")

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

    def _open_project(self) -> None:
        sel = self.projects_list.curselection()
        if not sel:
            return
        project = self._projects[sel[0]]
        ProjectWindow(self.root, self.lib, project)

    def _delete_project(self) -> None:
        sel = self.projects_list.curselection()
        if not sel:
            return
        project = self._projects[sel[0]]
        if messagebox.askyesno("Delete project", f"Delete '{project['name']}'?"):
            self.lib.delete_project(project["id"])
            self._refresh_projects()

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
