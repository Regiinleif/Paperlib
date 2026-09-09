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
# Semantic ranking: let Claude judge relevance by MEANING, not shared words.
# --------------------------------------------------------------------------

# The fast, cheap triage model used only to score topic relevance. The main
# RAG chat still uses the (bigger) model from ``config.get_model()``; this one
# is deliberately a Haiku-class model because ranking is a high-volume,
# low-stakes classification job where speed and price matter more than depth.
RANK_MODEL = "claude-haiku-4-5"

# Papers below this score are treated as not relevant and dropped. Kept small
# so borderline-but-related papers survive; clearly off-topic ones fall away.
RANK_THRESHOLD = 0.15

# How many papers to score per API call. Keeps each prompt bounded so we stay
# well within context/output limits; larger libraries are split into batches.
RANK_BATCH_SIZE = 40

# Characters of body text sent per paper. Enough for Claude to judge the
# subject matter without shipping whole papers (which would blow up cost).
RANK_SNIPPET_CHARS = 700

# The structured-output contract. Forcing this tool means Claude must answer
# with schema-valid JSON (an array of {id, score, reason}) instead of prose we
# would have to parse by hand.
_RANK_TOOL = {
    "name": "record_relevance",
    "description": (
        "Record how relevant each paper is to the research topic. Include one "
        "entry for every paper you were given, using its exact numeric id."
    ),
    "strict": True,
    "input_schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "rankings": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "id": {
                            "type": "integer",
                            "description": "The paper's numeric id.",
                        },
                        "score": {
                            "type": "number",
                            "description": (
                                "Relevance from 0.0 (unrelated) to 1.0 "
                                "(directly on-topic)."
                            ),
                        },
                        "reason": {
                            "type": "string",
                            "description": "One short line explaining the score.",
                        },
                    },
                    "required": ["id", "score", "reason"],
                },
            }
        },
        "required": ["rankings"],
    },
}

_RANK_SYSTEM_PROMPT = (
    "You are a research librarian judging how relevant papers are to a "
    "research topic. Judge by CONCEPTUAL relevance - what the paper is "
    "actually about - not by whether it repeats the topic's exact words. A "
    "paper can be highly relevant even if it shares no vocabulary with the "
    "topic (for example, a 'silicon detector' or 'charged-particle energy in "
    "the atmosphere' paper is relevant to 'muon tomography'). Score every "
    "paper you are given by calling the record_relevance tool."
)


def _rank_catalog(papers: list[dict]) -> str:
    """Render a compact, token-bounded catalog of papers for the prompt."""
    lines: list[str] = []
    for p in papers:
        keywords = ", ".join(p.get("keywords", []))
        snippet = re.sub(r"\s+", " ", p.get("text", "")).strip()[:RANK_SNIPPET_CHARS]
        lines.append(
            f"[id={p['id']}] {p['title']}\n"
            f"  keywords: {keywords or '(none)'}\n"
            f"  excerpt: {snippet or '(no text)'}"
        )
    return "\n\n".join(lines)


def _rank_batch(client, model: str, topic: str, papers: list[dict]) -> dict[int, dict]:
    """Score one batch of papers via Claude. Returns {id: {score, reason}}.

    Raises on any API/parse failure so the caller can fall back to lexical.
    """
    user_content = (
        f"RESEARCH TOPIC: {topic}\n\n"
        f"PAPERS:\n{_rank_catalog(papers)}\n\n"
        "Call record_relevance with a score and one-line reason for every "
        "paper above."
    )
    response = client.messages.create(
        model=model,
        max_tokens=4096,
        system=_RANK_SYSTEM_PROMPT,
        tools=[_RANK_TOOL],
        tool_choice={"type": "tool", "name": "record_relevance"},
        messages=[{"role": "user", "content": user_content}],
    )

    results: dict[int, dict] = {}
    for block in response.content:
        if getattr(block, "type", None) != "tool_use":
            continue
        for row in block.input.get("rankings", []):
            try:
                pid = int(row["id"])
                score = float(row["score"])
            except (KeyError, TypeError, ValueError):
                continue
            results[pid] = {
                "score": max(0.0, min(1.0, score)),
                "reason": str(row.get("reason", "")).strip(),
            }
    return results


def rank_by_topic_llm(
    topic: str,
    papers: list[dict],
    api_key: str | None,
    model: str = RANK_MODEL,
    top_n: int | None = None,
    client=None,
) -> list[dict]:
    """Rank papers by SEMANTIC relevance to `topic`, judged by Claude.

    Unlike :func:`rank_by_topic` (which is purely lexical TF-IDF), this asks a
    fast Haiku-class model to judge each paper's relevance by meaning, so a
    conceptually-related paper that shares no words with the topic can still
    surface. Each returned dict gains a ``score`` (0-1) and a short ``reason``.

    Robustness first: on ANY failure - no API key, network/API error, or
    unparseable output - this falls back to the lexical
    :func:`rank_by_topic` so the feature never hard-fails.
    """
    if not papers:
        return []
    if not api_key and client is None:
        return rank_by_topic(topic, papers, top_n)

    try:
        if client is None:
            import anthropic

            client = anthropic.Anthropic(api_key=api_key)

        scores: dict[int, dict] = {}
        for start in range(0, len(papers), RANK_BATCH_SIZE):
            batch = papers[start:start + RANK_BATCH_SIZE]
            scores.update(_rank_batch(client, model, topic, batch))

        if not scores:
            # Nothing usable came back; degrade to lexical rather than return
            # an empty list.
            return rank_by_topic(topic, papers, top_n)

        ranked = []
        for p in papers:
            hit = scores.get(p["id"])
            if hit and hit["score"] >= RANK_THRESHOLD:
                ranked.append({**p, "score": hit["score"], "reason": hit["reason"]})
        ranked.sort(key=lambda p: p["score"], reverse=True)
        return ranked[:top_n] if top_n else ranked
    except Exception:
        # Any API/network/parse error -> lexical fallback keeps the UI working.
        return rank_by_topic(topic, papers, top_n)


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
