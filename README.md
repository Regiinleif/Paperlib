# PaperLib

A private, offline-first research-paper library with a Claude-powered **RAG**
chat built in — drop in your PDFs, and ask questions that are answered with
cited passages from your own papers.

**Stack:** Python 3 · Tkinter (desktop GUI) · SQLite · Anthropic Claude SDK ·
a from-scratch TF-IDF retrieval engine · pytest · PyInstaller (Windows build).

The retrieval engine is written in plain Python (TF-IDF) rather than pulled
from a library, so every step of the RAG pipeline — chunking, scoring,
augmentation, generation — is inspectable; the generation step calls Claude
through the official SDK.

<!-- Add a screenshot or GIF of the app here — it's the fastest way for a
     reader to grasp what PaperLib does. Drop an image in docs/ and reference it:
     ![PaperLib](docs/screenshot.png) -->
<!-- ![PaperLib](docs/screenshot.png) -->

## What it does

- **Drop box** — drag downloaded PDFs (or `.txt`) onto the box at the top of
  the window (or click to browse). Files are copied into `papers/`.
- **Automatic categorization** — each paper's text is extracted, its top
  keywords are pulled out, and it's filed under a keyword-derived category.
- **Research project sessions** — click **Add Project Session**, give it a
  topic, and a new window opens listing the papers in your library that match
  that topic (plus a button to add any paper manually).
- **RAG chat with Claude** — inside a session, ask questions or click
  *Summarize these papers*. The app retrieves the most relevant passages from
  exactly the papers in that session, feeds them to Claude as context, and
  streams back a cited answer.

## Setup

```bash
git clone https://github.com/Regiinleif/Paperlib.git
cd Paperlib
python -m pip install -r requirements.txt
```

> The GUI and packaged installer target Windows, but the app runs from source
> anywhere Python and Tkinter are available.

### Anthropic API key (needed for the chat)

The library, drop box, and categorization all work with no key. The Claude chat
needs an Anthropic API key. Either:

- set an environment variable (recommended):
  ```powershell
  setx ANTHROPIC_API_KEY "sk-ant-..."
  ```
  (open a new terminal afterwards), **or**
- create a `.env` file in the project root with:
  ```
  ANTHROPIC_API_KEY=sk-ant-...
  ```
  (this file is gitignored, so your key is never committed), **or**
- enter it via **Settings** in the app (stored in `data/config.json`).

If more than one is set, a real environment variable wins, then `.env`, then
the key saved in `data/config.json`.

## Running

```bash
python run.py
```

(Or, from a packaged Windows install, launch **PaperLib** from the Start menu /
Desktop shortcut.)

## How the RAG works (the learning part)

See `paperlib/rag.py`. The pipeline is:

1. **Chunk** each paper into overlapping ~900-char passages.
2. **Retrieve** the top-k passages for your question using TF-IDF cosine
   similarity.
3. **Augment** — assemble those passages into a `CONTEXT` block tagged `[S1]`,
   `[S2]`, …
4. **Generate** — Claude answers using only that context and cites the tags.

To upgrade to vector embeddings later, replace `TfidfRetriever` with an
embedding-backed store; the rest of the app only calls `add_document()` and
`retrieve()`.

## Project layout

```
paperlib/            (project root)
  papers/            # your PDFs live here (the drop box copies into it)
  data/              # library.db (metadata) + config.json (settings)
  paperlib/
    app.py           # main window + drop box + library/projects
    project_window.py# research session window + chat
    library.py       # SQLite store
    extract.py       # PDF text + keyword extraction/categorization
    rag.py           # chunk -> retrieve -> augment -> generate
    reader.py        # in-app PDF page reader
    config.py        # paths + settings
  run.py             # entry point
```

## Notes & limits (first draft)

- Image-only/scanned PDFs yield no text (no OCR yet).
- Categorization is a simple top-keyword rule — easy to improve later.
- Retrieval is TF-IDF, not embeddings — fast and dependency-free, but swap in
  embeddings for better semantic matching.
