"""FastAPI backend for PaperLib.

This is the HTTP face of the app: the same library, extraction and RAG code
that the old Tkinter GUI used, now exposed as a small REST + SSE API that the
web dashboard (``src/static/index.html``) talks to.

Endpoints
---------
    GET  /api/health        - liveness + whether a Claude key is configured
    GET  /api/library       - library stats + the full paper list
    POST /api/documents     - upload a .pdf/.txt, ingest it, return the paper
    POST /api/chat          - question -> full cited answer (non-streaming)
    GET  /api/chat/stream   - question -> Server-Sent-Events token stream

The retriever is built lazily over every digested paper and rebuilt whenever a
document is added, so retrieval always reflects the current library.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

from fastapi import FastAPI, File, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .agent import LibraryAgent
from .library import Library
from .rag import RagChat, TfidfRetriever, build_context

app = FastAPI(title="PaperLib API", version="1.0")

_STATIC_DIR = Path(__file__).resolve().parent / "static"


class _State:
    """Process-wide singletons: one Library and a cached retriever.

    The retriever is invalidated (set to None) on ingest and rebuilt on next
    use, so we never rescore against a stale index.
    """

    def __init__(self) -> None:
        self.library = Library()
        self._retriever: TfidfRetriever | None = None

    def retriever(self) -> TfidfRetriever:
        if self._retriever is None:
            r = TfidfRetriever()
            for paper in self.library.all_papers():
                if paper.get("text"):
                    r.add_document(paper["id"], paper["title"], paper["text"])
            self._retriever = r
        return self._retriever

    def invalidate(self) -> None:
        self._retriever = None


state = _State()


# ---- schemas ----------------------------------------------------------------


class ChatRequest(BaseModel):
    question: str
    k: int = 5


# ---- helpers ----------------------------------------------------------------


def _require_key() -> str:
    api_key = config.get_api_key()
    if not api_key:
        raise HTTPException(
            status_code=400,
            detail="No Anthropic API key configured. Set ANTHROPIC_API_KEY.",
        )
    return api_key


def _new_chat() -> RagChat:
    """Build a RagChat over the current library, or 400 if no key is set."""
    return RagChat(state.retriever(), api_key=_require_key())


def _new_agent() -> LibraryAgent:
    """Build a tool-calling LibraryAgent over the current library."""
    return LibraryAgent(state.retriever(), api_key=_require_key())


def _clean_error(exc: Exception) -> str:
    """Pull a human-readable message out of an exception for the UI.

    Anthropic SDK errors expose the API's message on ``.body['error']['message']``;
    fall back to ``str(exc)`` for everything else.
    """
    body = getattr(exc, "body", None)
    if isinstance(body, dict):
        msg = body.get("error", {})
        if isinstance(msg, dict) and msg.get("message"):
            return str(msg["message"])
    return str(exc)


# ---- endpoints --------------------------------------------------------------


@app.get("/api/health")
def health() -> dict:
    return {
        "status": "ok",
        "papers": len(state.library.all_papers()),
        "has_api_key": bool(config.get_api_key()),
        "model": config.get_model(),
    }


@app.get("/api/library")
def library_view() -> dict:
    grouped = state.library.papers_by_category()
    papers = [
        {
            "id": p["id"],
            "title": p["title"],
            "category": p["category"],
            "keywords": p["keywords"],
            "added_at": p["added_at"],
        }
        for p in state.library.all_papers()
    ]
    return {
        "total": len(papers),
        "categories": {cat: len(items) for cat, items in grouped.items()},
        "papers": papers,
    }


@app.post("/api/documents")
async def add_document(file: UploadFile = File(...)) -> dict:
    suffix = Path(file.filename or "").suffix.lower()
    if suffix not in (".pdf", ".txt"):
        raise HTTPException(status_code=400, detail="Only .pdf and .txt are supported.")

    # Persist the upload to a temp file, then hand it to the library (which
    # copies it into papers/ and extracts text). The temp file is cleaned up.
    data = await file.read()
    tmp = tempfile.NamedTemporaryFile(
        prefix="paperlib-", suffix=suffix, delete=False
    )
    try:
        tmp.write(data)
        tmp.close()
        # Copy under the real filename so papers/ keeps a sensible name.
        source = Path(tmp.name)
        named = source.with_name(Path(file.filename).name)
        source.replace(named)
        try:
            paper = state.library.add_file(named)
        finally:
            named.unlink(missing_ok=True)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc

    state.invalidate()
    return {
        "id": paper["id"],
        "title": paper["title"],
        "category": paper["category"],
        "keywords": paper["keywords"],
    }


@app.post("/api/chat")
def chat(req: ChatRequest) -> dict:
    if not req.question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    chat = _new_chat()
    answer_parts: list[str] = []
    sources: list[str] = []
    for item in chat.ask_stream(req.question, k=req.k):
        if isinstance(item, tuple) and item and item[0] == "__sources__":
            sources = item[1]
        else:
            answer_parts.append(item)
    return {"answer": "".join(answer_parts), "sources": sources}


@app.get("/api/chat/stream")
def chat_stream(question: str, k: int = 5) -> StreamingResponse:
    if not question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    chat = _new_chat()

    def events():
        try:
            for item in chat.ask_stream(question, k=k):
                if isinstance(item, tuple) and item and item[0] == "__sources__":
                    payload = json.dumps({"sources": item[1]})
                    yield f"event: sources\ndata: {payload}\n\n"
                else:
                    payload = json.dumps({"text": item})
                    yield f"event: token\ndata: {payload}\n\n"
        except Exception as exc:  # surface the real cause instead of a silent drop
            yield f"event: stream_error\ndata: {json.dumps({'message': _clean_error(exc)})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


@app.get("/api/agent/stream")
def agent_stream(question: str, k: int = 5) -> StreamingResponse:
    """Multi-step tool-calling agent, streamed as Server-Sent Events.

    Emits the agent's tool_call / tool_result steps alongside the answer text
    and the final source legend, so the UI can show the search process.
    """
    if not question.strip():
        raise HTTPException(status_code=400, detail="Question must not be empty.")
    agent = _new_agent()

    def events():
        try:
            for ev in agent.run_stream(question):
                etype = ev.pop("type")
                yield f"event: {etype}\ndata: {json.dumps(ev)}\n\n"
        except Exception as exc:
            yield f"event: stream_error\ndata: {json.dumps({'message': _clean_error(exc)})}\n\n"
        yield "event: done\ndata: {}\n\n"

    return StreamingResponse(events(), media_type="text/event-stream")


# ---- static dashboard (mounted last so /api/* wins) -------------------------

if _STATIC_DIR.exists():
    app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")
