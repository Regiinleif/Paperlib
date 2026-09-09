"""The RAG (Retrieval-Augmented Generation) engine.

This is the learning core of the app. It does the four classic RAG steps with
no black boxes:

    1. CHUNK    - split each paper's text into overlapping passages.
    2. RETRIEVE - score chunks against the question with TF-IDF cosine
                  similarity and keep the top-k most relevant.
    3. AUGMENT  - stitch those chunks into a context block with source tags.
    4. GENERATE - send question + context to Claude and stream the answer.

TF-IDF is implemented here in plain Python so every step is inspectable. When
you want to level up, swap `TfidfRetriever` for real embeddings (e.g. a
sentence-transformers model or a hosted embedding API) - the interface is just
`add_document()` and `retrieve()`.
"""

from __future__ import annotations

import math
import re
from collections import Counter
from dataclasses import dataclass, field

from . import config
from .extract import STOPWORDS

WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{1,}")


def tokenize(text: str) -> list[str]:
    return [
        w.lower()
        for w in WORD_RE.findall(text)
        if w.lower() not in STOPWORDS
    ]


def chunk_text(text: str, size: int = 900, overlap: int = 150) -> list[str]:
    """Split text into ~`size`-character chunks that overlap by `overlap`.

    Overlap keeps sentences that straddle a boundary retrievable from either
    side. Splitting on paragraph-ish whitespace keeps chunks readable.
    """
    text = re.sub(r"\s+", " ", text).strip()
    if not text:
        return []
    # Guarantee forward progress: the step (size - overlap) must be positive,
    # otherwise `start` never advances and we loop forever. Clamp overlap to
    # [0, size - 1]. Normal defaults (size=900, overlap=150) are unaffected.
    size = max(size, 1)
    overlap = min(max(overlap, 0), size - 1)
    chunks: list[str] = []
    start = 0
    while start < len(text):
        end = min(start + size, len(text))
        chunks.append(text[start:end])
        if end == len(text):
            break
        start = end - overlap
    return chunks


@dataclass
class Chunk:
    paper_id: int
    title: str
    text: str
    tokens: Counter = field(default_factory=Counter)


class TfidfRetriever:
    """A tiny TF-IDF vector store over paper chunks.

    Usage:
        r = TfidfRetriever()
        r.add_document(paper_id, title, full_text)
        ...
        hits = r.retrieve("my question", k=5)
    """

    def __init__(self) -> None:
        self.chunks: list[Chunk] = []
        self.doc_freq: Counter = Counter()   # term -> number of chunks containing it
        self._idf: dict[str, float] = {}

    def add_document(self, paper_id: int, title: str, text: str) -> None:
        for passage in chunk_text(text):
            counts = Counter(tokenize(passage))
            if not counts:
                continue
            self.chunks.append(Chunk(paper_id, title, passage, counts))
            for term in counts:
                self.doc_freq[term] += 1

    def _finalize_idf(self) -> None:
        n = max(len(self.chunks), 1)
        self._idf = {
            term: math.log((1 + n) / (1 + df)) + 1.0
            for term, df in self.doc_freq.items()
        }

    def _vector(self, counts: Counter) -> dict[str, float]:
        vec = {t: (1 + math.log(c)) * self._idf.get(t, 0.0) for t, c in counts.items()}
        norm = math.sqrt(sum(v * v for v in vec.values())) or 1.0
        return {t: v / norm for t, v in vec.items()}

    def retrieve(self, query: str, k: int = 5) -> list[tuple[Chunk, float]]:
        if not self.chunks:
            return []
        self._finalize_idf()
        q_vec = self._vector(Counter(tokenize(query)))
        scored: list[tuple[Chunk, float]] = []
        for chunk in self.chunks:
            c_vec = self._vector(chunk.tokens)
            score = sum(q_vec.get(t, 0.0) * w for t, w in c_vec.items())
            if score > 0:
                scored.append((chunk, score))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:k]


def rank_by_topic(topic: str, papers: list[dict], top_n: int | None = None) -> list[dict]:
    """Rank papers by relevance to a free-text research `topic`.

    `papers` is a list of dicts with at least `id`, `title`, `keywords`
    (list[str]) and `text`. Returns the same dicts, each with a `score` key,
    ordered most-relevant first. Papers scoring 0 are dropped.
    """
    retriever = TfidfRetriever()
    # One "chunk" per paper here: title + keywords + a slice of the body. That
    # keeps topic-matching about the paper as a whole rather than one passage.
    for p in papers:
        blob = f"{p['title']} {' '.join(p.get('keywords', []))} {p.get('text', '')[:4000]}"
        retriever.add_document(p["id"], p["title"], blob)
    retriever._finalize_idf()

    q_vec = retriever._vector(Counter(tokenize(topic)))
    by_paper: dict[int, float] = {}
    for chunk in retriever.chunks:
        c_vec = retriever._vector(chunk.tokens)
        score = sum(q_vec.get(t, 0.0) * w for t, w in c_vec.items())
        by_paper[chunk.paper_id] = max(by_paper.get(chunk.paper_id, 0.0), score)

    ranked = []
    for p in papers:
        score = by_paper.get(p["id"], 0.0)
        if score > 0:
            ranked.append({**p, "score": score})
    ranked.sort(key=lambda p: p["score"], reverse=True)
    return ranked[:top_n] if top_n else ranked


# --------------------------------------------------------------------------
# Generation: send retrieved context + question to Claude.
# --------------------------------------------------------------------------

# Generous cap so long answers (e.g. multi-source syntheses) are not cut off
# mid-thought. Streaming is used below, so large values don't risk HTTP timeouts.
MAX_TOKENS = 16000

SYSTEM_PROMPT = (
    "You are a research assistant embedded in the user's personal paper "
    "library. Answer using ONLY the excerpts provided in the CONTEXT section. "
    "Each excerpt is tagged with its source like [S1], [S2]. Cite the sources "
    "you rely on inline using those tags. If the context does not contain the "
    "answer, say so plainly rather than guessing. Be concise and precise."
)


def build_context(hits: list[tuple[Chunk, float]]) -> tuple[str, list[str]]:
    """Turn retrieved chunks into a numbered CONTEXT block + a source legend."""
    lines: list[str] = []
    legend: list[str] = []
    for i, (chunk, score) in enumerate(hits, start=1):
        tag = f"S{i}"
        lines.append(f"[{tag}] (from \"{chunk.title}\")\n{chunk.text}")
        legend.append(f"[{tag}] {chunk.title}")
    return "\n\n".join(lines), legend


class RagChat:
    """Holds one conversation and answers questions against a retriever."""

    def __init__(self, retriever: TfidfRetriever, api_key: str | None = None):
        import anthropic

        self.retriever = retriever
        self.client = anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
        self.model = config.get_model()
        self.history: list[dict] = []

    def ask_stream(self, question: str, k: int = 5):
        """Yield the answer text incrementally. Returns via generator.

        The final item yielded is a special ("__sources__", legend) tuple so
        the UI can show which papers were used.
        """
        hits = self.retriever.retrieve(question, k=k)
        context, legend = build_context(hits)

        if context:
            user_content = (
                f"CONTEXT:\n{context}\n\n"
                f"QUESTION: {question}"
            )
        else:
            user_content = (
                f"(No relevant excerpts were found in the library.)\n\n"
                f"QUESTION: {question}"
            )

        self.history.append({"role": "user", "content": user_content})

        answer_parts: list[str] = []
        with self.client.messages.stream(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=SYSTEM_PROMPT,
            messages=self.history,
        ) as stream:
            for text in stream.text_stream:
                answer_parts.append(text)
                yield text
            final = stream.get_final_message()

        # If we still hit the cap, tell the user rather than silently truncating.
        if getattr(final, "stop_reason", None) == "max_tokens":
            note = "\n\n[Answer truncated - hit max length.]"
            answer_parts.append(note)
            yield note

        full_answer = "".join(answer_parts)
        self.history.append({"role": "assistant", "content": full_answer})
        yield ("__sources__", legend)
