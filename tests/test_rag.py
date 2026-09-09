"""Tests for the RAG retrieval pipeline (chunk -> retrieve -> rank)."""

import threading

from src import rag


def test_chunk_text_overlaps():
    text = "a" * 2000
    chunks = rag.chunk_text(text, size=900, overlap=150)
    assert len(chunks) >= 2
    # every chunk within size bound
    assert all(len(c) <= 900 for c in chunks)


def test_chunk_empty_text():
    assert rag.chunk_text("   ") == []


def test_chunk_text_overlap_ge_size_terminates():
    # With overlap >= size the old code looped forever. Run in a thread with a
    # timeout so a regression fails the test instead of hanging the suite.
    result = {}

    def run():
        result["chunks"] = rag.chunk_text("x" * 500, size=100, overlap=150)

    t = threading.Thread(target=run, daemon=True)
    t.start()
    t.join(timeout=5)
    assert not t.is_alive(), "chunk_text hung with overlap >= size"
    chunks = result["chunks"]
    assert chunks, "expected non-empty chunks"
    assert all(c for c in chunks)
    assert all(len(c) <= 100 for c in chunks)
    # The chunks should still cover the whole text.
    assert "".join(chunks).count("x") >= 500


def test_max_tokens_is_generous():
    # L2: the answer cap was bumped well above the old 4096 to avoid truncation.
    assert rag.MAX_TOKENS >= 16000


def test_retriever_finds_relevant_chunk():
    r = rag.TfidfRetriever()
    r.add_document(1, "Transformers",
                   "The transformer uses self attention over tokens. " * 20)
    r.add_document(2, "Cooking",
                   "Boil the water then add salt and pasta. " * 20)
    hits = r.retrieve("how does attention work in transformers", k=1)
    assert hits, "expected at least one hit"
    top_chunk, score = hits[0]
    assert top_chunk.paper_id == 1  # transformer paper, not cooking
    assert score > 0


def test_retriever_empty_returns_nothing():
    r = rag.TfidfRetriever()
    assert r.retrieve("anything") == []


def test_rank_by_topic_excludes_irrelevant():
    papers = [
        {"id": 1, "title": "Attention Networks",
         "keywords": ["attention", "transformer"],
         "text": "attention transformer sequence model " * 20},
        {"id": 2, "title": "Bread Recipes",
         "keywords": ["bread", "flour"],
         "text": "flour water yeast bake bread " * 20},
    ]
    ranked = rag.rank_by_topic("transformer attention models", papers)
    assert ranked[0]["id"] == 1
    # the bread paper scores zero and is dropped
    assert all(p["id"] != 2 for p in ranked)


def test_build_context_tags_sources():
    r = rag.TfidfRetriever()
    r.add_document(1, "Paper A", "retrieval augmented generation " * 20)
    hits = r.retrieve("retrieval", k=1)
    context, legend = rag.build_context(hits)
    assert "[S1]" in context
    assert legend and legend[0].startswith("[S1]")
