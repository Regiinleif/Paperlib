"""The research project session window.

Opened when you click "Add Project Session" (or open an existing one). It:
  * asks for a research topic,
  * pulls the relevant papers from your library that match that topic,
  * lets you add more papers manually,
  * and gives you a Claude chat box that answers via RAG over exactly the
    papers included in this session.
"""

from __future__ import annotations

import threading
import tkinter as tk
from tkinter import messagebox, ttk

from . import config, rag
from .library import Library


class ProjectWindow(tk.Toplevel):
    def __init__(self, master, lib: Library, project: dict):
        super().__init__(master)
        self.lib = lib
        self.project = project
        self.title(f"Research Session - {project['name']}")
        self.geometry("1000x680")
        self.minsize(820, 560)

        # Papers included in this session: {paper_id: paper_dict}
        self.included: dict[int, dict] = {}
        self.chat: rag.RagChat | None = None

        self._build_ui()
        self._load_relevant_papers()

    # ---- layout -------------------------------------------------------

    def _build_ui(self) -> None:
        topbar = ttk.Frame(self, padding=8)
        topbar.pack(fill="x")
        ttk.Label(topbar, text="Research topic:").pack(side="left")
        self.topic_var = tk.StringVar(value=self.project.get("topic", ""))
        topic_entry = ttk.Entry(topbar, textvariable=self.topic_var, width=60)
        topic_entry.pack(side="left", padx=6)
        ttk.Button(topbar, text="Find relevant papers",
                   command=self._load_relevant_papers).pack(side="left")

        # Split: left = papers in this session, right = chat.
        panes = ttk.PanedWindow(self, orient="horizontal")
        panes.pack(fill="both", expand=True, padx=8, pady=(0, 8))

        left = ttk.Frame(panes)
        panes.add(left, weight=1)
        ttk.Label(left, text="Papers in this session",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")
        self.papers_list = tk.Listbox(left, height=20)
        self.papers_list.pack(fill="both", expand=True, pady=4)
        btns = ttk.Frame(left)
        btns.pack(fill="x")
        ttk.Button(btns, text="Add paper manually...",
                   command=self._add_manually).pack(side="left")
        ttk.Button(btns, text="Remove selected",
                   command=self._remove_selected).pack(side="left", padx=4)

        right = ttk.Frame(panes)
        panes.add(right, weight=2)
        ttk.Label(right, text="Ask Claude (RAG over the papers above)",
                  font=("Segoe UI", 10, "bold")).pack(anchor="w")

        self.transcript = tk.Text(right, wrap="word", state="disabled",
                                  height=20, font=("Segoe UI", 10))
        self.transcript.pack(fill="both", expand=True, pady=4)
        self.transcript.tag_configure("you", foreground="#1a56db",
                                      font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("claude", foreground="#047857",
                                      font=("Segoe UI", 10, "bold"))
        self.transcript.tag_configure("sources", foreground="#6b7280",
                                      font=("Segoe UI", 9, "italic"))

        entry_row = ttk.Frame(right)
        entry_row.pack(fill="x")
        self.msg_var = tk.StringVar()
        self.msg_entry = ttk.Entry(entry_row, textvariable=self.msg_var)
        self.msg_entry.pack(side="left", fill="x", expand=True)
        self.msg_entry.bind("<Return>", lambda e: self._send())
        self.send_btn = ttk.Button(entry_row, text="Send", command=self._send)
        self.send_btn.pack(side="left", padx=(6, 0))

        quick = ttk.Frame(right)
        quick.pack(fill="x", pady=(4, 0))
        ttk.Button(quick, text="Summarize these papers",
                   command=lambda: self._send(
                       "Summarize the key findings across these papers.")
                   ).pack(side="left")

    # ---- session papers ----------------------------------------------

    def _load_relevant_papers(self) -> None:
        """Populate the session with papers matching the topic, keeping any
        manually added ones."""
        topic = self.topic_var.get().strip()
        manual_ids = self.lib.manual_project_paper_ids(self.project["id"])

        # Start from manual picks so they always stay in.
        self.included = {
            pid: self.lib.get_paper(pid)
            for pid in manual_ids
            if self.lib.get_paper(pid)
        }

        if topic:
            all_papers = self.lib.all_papers()
            ranked = rag.rank_by_topic(topic, all_papers, top_n=12)
            for paper in ranked:
                self.included.setdefault(paper["id"], paper)

        self._refresh_papers_list()
        self._rebuild_index()

    def _refresh_papers_list(self) -> None:
        self.papers_list.delete(0, "end")
        self._list_order = list(self.included.values())
        for paper in self._list_order:
            score = paper.get("score")
            tag = f"  (match {score:.2f})" if score else ""
            self.papers_list.insert("end", f"{paper['title']}{tag}")

    def _rebuild_index(self) -> None:
        """Rebuild the RAG retriever over exactly the included papers."""
        retriever = rag.TfidfRetriever()
        for paper in self.included.values():
            retriever.add_document(paper["id"], paper["title"], paper.get("text", ""))
        self._retriever = retriever
        self.chat = None  # rebuilt lazily on first send, with the new retriever

    def _add_manually(self) -> None:
        AddPaperDialog(self, self.lib, on_pick=self._on_manual_pick)

    def _on_manual_pick(self, paper: dict) -> None:
        self.lib.add_paper_to_project(self.project["id"], paper["id"])
        self.included[paper["id"]] = paper
        self._refresh_papers_list()
        self._rebuild_index()

    def _remove_selected(self) -> None:
        sel = self.papers_list.curselection()
        if not sel:
            return
        paper = self._list_order[sel[0]]
        self.included.pop(paper["id"], None)
        self.lib.remove_paper_from_project(self.project["id"], paper["id"])
        self._refresh_papers_list()
        self._rebuild_index()

    # ---- chat ---------------------------------------------------------

    def _append(self, text: str, tag: str | None = None) -> None:
        self.transcript.configure(state="normal")
        if tag:
            self.transcript.insert("end", text, tag)
        else:
            self.transcript.insert("end", text)
        self.transcript.see("end")
        self.transcript.configure(state="disabled")

    def _send(self, forced_text: str | None = None) -> None:
        question = forced_text if forced_text is not None else self.msg_var.get().strip()
        if not question:
            return
        if not self.included:
            messagebox.showinfo(
                "No papers",
                "This session has no papers yet. Set a topic and click "
                "'Find relevant papers', or add one manually.",
            )
            return

        api_key = config.get_api_key()
        if not api_key:
            messagebox.showwarning(
                "No API key",
                "No Anthropic API key found.\n\nSet the ANTHROPIC_API_KEY "
                "environment variable, or add one via Settings on the main "
                "window, then try again.",
            )
            return

        self.msg_var.set("")
        self._append(f"\nYou: ", "you")
        self._append(f"{question}\n")
        self._append("Claude: ", "claude")

        self.send_btn.configure(state="disabled")
        thread = threading.Thread(
            target=self._run_chat, args=(question, api_key), daemon=True
        )
        thread.start()

    def _run_chat(self, question: str, api_key: str) -> None:
        """Runs on a worker thread; marshals UI updates back to the main loop."""
        try:
            if self.chat is None:
                self.chat = rag.RagChat(self._retriever, api_key=api_key)
            for piece in self.chat.ask_stream(question):
                if isinstance(piece, tuple) and piece[0] == "__sources__":
                    legend = piece[1]
                    if legend:
                        srclist = "\n".join(f"  {s}" for s in legend)
                        self.after(0, self._append,
                                   f"\n\nSources used:\n{srclist}\n", "sources")
                else:
                    self.after(0, self._append, piece)
        except Exception as exc:  # surface any API/network error in the transcript
            self.after(0, self._append, f"\n[Error: {exc}]\n", "sources")
        finally:
            self.after(0, lambda: self.send_btn.configure(state="normal"))
            self.after(0, lambda: self._append("\n"))


class AddPaperDialog(tk.Toplevel):
    """A little picker listing every paper in the library to add to a session."""

    def __init__(self, master, lib: Library, on_pick):
        super().__init__(master)
        self.lib = lib
        self.on_pick = on_pick
        self.title("Add a paper to this session")
        self.geometry("560x420")
        self.transient(master)

        ttk.Label(self, text="Select a paper from your library:",
                  padding=8).pack(anchor="w")
        self.listbox = tk.Listbox(self)
        self.listbox.pack(fill="both", expand=True, padx=8)
        self.papers = lib.all_papers()
        for paper in self.papers:
            self.listbox.insert("end", f"{paper['title']}  [{paper['category']}]")

        row = ttk.Frame(self, padding=8)
        row.pack(fill="x")
        ttk.Button(row, text="Add", command=self._add).pack(side="right")
        ttk.Button(row, text="Cancel", command=self.destroy).pack(side="right", padx=6)

    def _add(self) -> None:
        sel = self.listbox.curselection()
        if not sel:
            return
        self.on_pick(self.papers[sel[0]])
        self.destroy()
