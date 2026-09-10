"""Tests for the FastAPI backend (REST + SSE), isolated to a temp data dir.

The Claude chat is exercised with a fake Anthropic client so these run with no
key and no network.
"""

import importlib
import types

import pytest
from fastapi.testclient import TestClient

from src import config


# ---- a fake Anthropic client mimicking the streaming interface -------------


class _FakeStream:
    def __init__(self, pieces):
        self._pieces = pieces

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    @property
    def text_stream(self):
        yield from self._pieces

    def get_final_message(self):
        return types.SimpleNamespace(stop_reason="end_turn")


class _FakeMessages:
    def stream(self, **_kwargs):
        return _FakeStream(["Answer ", "with citation [S1]."])


class _FakeClient:
    def __init__(self):
        self.messages = _FakeMessages()


@pytest.fixture
def client(tmp_path, monkeypatch):
    """A TestClient whose Library lives in a throwaway temp dir."""
    papers_dir = tmp_path / "papers"
    data_dir = tmp_path / "data"
    papers_dir.mkdir()
    data_dir.mkdir()
    monkeypatch.setattr(config, "PAPERS_DIR", papers_dir)
    monkeypatch.setattr(config, "DATA_DIR", data_dir)
    monkeypatch.setattr(config, "DB_PATH", data_dir / "library.db")

    # Import (or reload) the API module so its module-level Library binds to the
    # patched temp paths rather than the real data/ folder.
    from src import api as api_module
    api = importlib.reload(api_module)

    # Chat: force a key check to pass and inject the fake client.
    monkeypatch.setattr(api.config, "get_api_key", lambda: "test-key")
    monkeypatch.setattr(
        api, "RagChat",
        lambda retriever, api_key=None: importlib.import_module("src.rag").RagChat(
            retriever, client=_FakeClient()
        ),
    )
    monkeypatch.setattr(
        api, "LibraryAgent",
        lambda retriever, api_key=None: importlib.import_module("src.agent").LibraryAgent(
            retriever, client=_FakeAgentClient()
        ),
    )

    with TestClient(api.app) as c:
        yield c


def _FakeAgentClient():
    """A scripted client: one search, then a final cited answer."""
    responses = [
        types.SimpleNamespace(
            content=[types.SimpleNamespace(
                type="tool_use", id="t1", name="search_library",
                input={"query": "retrieval", "k": 3})],
            stop_reason="tool_use"),
        types.SimpleNamespace(
            content=[types.SimpleNamespace(type="text", text="Agent answer [S1].")],
            stop_reason="end_turn"),
    ]

    class _Messages:
        def __init__(self):
            self._r = list(responses)

        def create(self, **_kwargs):
            return self._r.pop(0)

    return types.SimpleNamespace(messages=_Messages())


def test_health(client):
    r = client.get("/api/health")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok"
    assert body["papers"] == 0


def test_library_empty(client):
    r = client.get("/api/library")
    assert r.status_code == 200
    assert r.json() == {"total": 0, "categories": {}, "papers": []}


def test_upload_document_and_appears_in_library(client):
    content = b"reinforcement learning agent reward policy " * 20
    r = client.post(
        "/api/documents",
        files={"file": ("rl.txt", content, "text/plain")},
    )
    assert r.status_code == 200, r.text
    paper = r.json()
    assert paper["title"]
    assert "reinforcement" in paper["keywords"]

    lib = client.get("/api/library").json()
    assert lib["total"] == 1
    assert lib["categories"]  # at least one category


def test_upload_rejects_unsupported_type(client):
    r = client.post(
        "/api/documents",
        files={"file": ("bad.docx", b"nope", "application/octet-stream")},
    )
    assert r.status_code == 400


def test_chat_returns_answer_with_sources(client):
    # Seed a paper so retrieval has something to cite.
    content = b"retrieval augmented generation transformer attention " * 20
    client.post("/api/documents", files={"file": ("rag.txt", content, "text/plain")})

    r = client.post("/api/chat", json={"question": "what is retrieval", "k": 3})
    assert r.status_code == 200, r.text
    body = r.json()
    assert "citation" in body["answer"]
    assert body["sources"]  # a non-empty legend


def test_chat_stream_emits_sse_events(client):
    content = b"retrieval augmented generation transformer attention " * 20
    client.post("/api/documents", files={"file": ("rag.txt", content, "text/plain")})

    with client.stream("GET", "/api/chat/stream?question=what%20is%20retrieval") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        payload = "".join(r.iter_text())
    assert "event: token" in payload
    assert "event: sources" in payload
    assert "event: done" in payload


def test_chat_empty_question_rejected(client):
    r = client.post("/api/chat", json={"question": "   "})
    assert r.status_code == 400


def test_agent_stream_emits_tool_steps(client):
    content = b"retrieval augmented generation transformer attention " * 20
    client.post("/api/documents", files={"file": ("rag.txt", content, "text/plain")})

    with client.stream("GET", "/api/agent/stream?question=how%20does%20retrieval%20work") as r:
        assert r.status_code == 200
        assert "text/event-stream" in r.headers["content-type"]
        payload = "".join(r.iter_text())
    for evt in ("tool_call", "tool_result", "text", "sources", "done"):
        assert f"event: {evt}" in payload, f"missing {evt} in stream"
