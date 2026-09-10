# PaperLib

A private, offline-first research-paper library with a Claude-powered **RAG**
chat built in — drop in your PDFs, and ask questions that are answered with
cited passages from your own papers.

**Stack:** Python 3 · FastAPI (REST + SSE backend) · HTML/JS dashboard · SQLite ·
Anthropic Claude SDK · a from-scratch TF-IDF retrieval engine · pytest ·
PyInstaller (Windows build). A legacy Tkinter desktop client is still included.

The retrieval engine is written in plain Python (TF-IDF) rather than pulled
from a library, so every step of the RAG pipeline — chunking, scoring,
augmentation, generation — is inspectable; the generation step calls Claude
through the official SDK.

<!-- Add a screenshot or GIF of the app here — it's the fastest way for a
     reader to grasp what PaperLib does. Drop an image in docs/ and reference it:
     ![PaperLib](docs/screenshot.png) -->
<!-- ![PaperLib](docs/screenshot.png) -->

## What it does

- **Drop box** — drag downloaded PDFs (or `.txt`) onto the box in the dashboard
  (or click to browse). Files are uploaded and copied into `papers/`.
- **Automatic categorization** — each paper's text is extracted, its top
  keywords are pulled out, and it's filed under a keyword-derived category.
  The dashboard shows live library stats grouped by category.
- **RAG chat with Claude** — ask a question in the chat panel. The app
  retrieves the most relevant passages across your library, feeds them to
  Claude as context, and streams back a cited answer (each source tagged
  `[S1]`, `[S2]`, …) token-by-token over Server-Sent Events.

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

PaperLib is a **web app**: a FastAPI backend that serves an HTML/JS dashboard.

```bash
python run_api.py
```

Then open <http://127.0.0.1:8000> in your browser. Drop a PDF onto the box,
watch it get categorized, and ask questions in the chat panel — answers stream
in token-by-token and cite the passages they came from.

### API endpoints

| Method | Path                | Purpose                                   |
| ------ | ------------------- | ----------------------------------------- |
| GET    | `/api/health`       | Liveness + whether a Claude key is set    |
| GET    | `/api/library`      | Library stats + the full paper list       |
| POST   | `/api/documents`    | Upload a `.pdf`/`.txt` and ingest it       |
| POST   | `/api/chat`         | Question → full cited answer              |
| GET    | `/api/chat/stream`  | Question → Server-Sent-Events token stream |
| GET    | `/api/agent/stream` | Tool-calling agent → SSE search steps + answer |

**Agent mode.** Tick *Agent mode* in the dashboard (or hit `/api/agent/stream`)
to let Claude drive the retrieval itself: it's given a `search_library` tool and
decides when and what to search, so it can do **multi-step retrieval** — search,
read, refine, then answer — with the search steps streamed live. Plain chat
(`/api/chat`) is the single-shot retrieve-then-answer path. See `src/agent.py`.

### Legacy desktop client

The original Tkinter GUI still runs (`python run.py`), but the web app above is
now the primary interface.

## How the RAG works (the learning part)

See `src/rag.py`. The pipeline is:

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
  src/               # the application package
    api.py           # FastAPI backend: REST + SSE endpoints
    static/          # the HTML/JS dashboard served by the backend
    library.py       # SQLite store
    extract.py       # PDF text + keyword extraction/categorization
    rag.py           # chunk -> retrieve -> augment -> generate
    config.py        # paths + settings
    app.py           # legacy Tkinter main window + drop box
    project_window.py# legacy Tkinter research session window + chat
    reader.py        # legacy in-app PDF page reader
  run_api.py         # entry point (web app)
  run.py             # entry point (legacy Tkinter GUI)
```

## Notes & limits (first draft)

- Image-only/scanned PDFs yield no text (no OCR yet).
- Categorization is a simple top-keyword rule — easy to improve later.
- Retrieval is TF-IDF, not embeddings — fast and dependency-free, but swap in
  embeddings for better semantic matching.
