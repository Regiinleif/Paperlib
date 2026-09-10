"""A small, dependency-free model-evaluation harness for PaperLib.

The RAG pipeline (retrieve -> augment -> generate) is easy to *demo* and hard to
*trust*: a slick answer can be built on the wrong passages, and a citation can
point at a source the model never actually read. This harness measures both
halves so a change to chunking, k, or the model can be judged by numbers rather
than vibes.

It answers two questions against a labeled question set:

    1. RETRIEVAL - did the retriever surface the right papers?
         * recall@k     - of the papers we labeled relevant, how many made the
                          top-k?
         * hit@k        - did at least one relevant paper make the top-k?
       (No API key or network needed - this is pure local TF-IDF scoring.)

    2. ANSWER - is the generated answer grounded in what was retrieved?
         * citation faithfulness - every [S#] the answer cites must correspond
                                   to a source that was actually retrieved (a
                                   citation to a non-existent [S#] is a
                                   hallucinated reference).
         * citation relevance    - at least one cited source is a paper we
                                   labeled relevant to the question.
         * grounded rate         - the answer cited *something* rather than
                                   answering from thin air.
       (Answer eval needs a way to generate answers; see ``run_answer_eval``.
       It is optional so retrieval can be scored with zero API cost.)

A "labeled" item is a question plus a list of *labels* - substrings matched
case-insensitively against each paper's filename or title. Substrings (not ids)
keep the question set portable: paper ids differ between databases, but
"borehole" still identifies the borehole paper in anyone's library.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

from .rag import TfidfRetriever

# Matches source tags like [S1], [S12] in an answer or a legend line.
_CITE_RE = re.compile(r"\[S(\d+)\]")


# --------------------------------------------------------------------------
# The labeled question set.
# --------------------------------------------------------------------------


@dataclass
class QAItem:
    """One labeled evaluation question.

    ``relevant`` is a list of substrings; a paper counts as relevant to this
    question if any substring appears (case-insensitively) in its filename or
    title. ``note`` is free-text for humans and is ignored by the metrics.
    """

    question: str
    relevant: list[str]
    note: str = ""


def load_question_set(path: str | Path) -> list[QAItem]:
    """Load a JSON question set into ``QAItem``s.

    The file is a JSON list of objects with ``question`` and ``relevant`` keys
    (and an optional ``note``). Raises ``ValueError`` with a pointed message if
    an entry is malformed, so a typo in the dataset fails loudly rather than
    silently scoring nothing.
    """
    raw = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise ValueError("Question set must be a JSON list of items.")
    items: list[QAItem] = []
    for i, entry in enumerate(raw):
        if not isinstance(entry, dict) or "question" not in entry:
            raise ValueError(f"Item {i} is missing a 'question' field.")
        relevant = entry.get("relevant", [])
        if not isinstance(relevant, list) or not all(isinstance(r, str) for r in relevant):
            raise ValueError(f"Item {i} 'relevant' must be a list of strings.")
        items.append(
            QAItem(
                question=str(entry["question"]),
                relevant=list(relevant),
                note=str(entry.get("note", "")),
            )
        )
    return items


def paper_matches(paper: dict, label: str) -> bool:
    """True if ``label`` (a substring) identifies this paper.

    Matched case-insensitively against the paper's filename and title so a
    portable label like "borehole" finds the borehole paper regardless of its
    numeric id in a given database.
    """
    needle = label.lower()
    haystack = f"{paper.get('filename', '')} {paper.get('title', '')}".lower()
    return needle in haystack


def relevant_ids(papers: list[dict], item: QAItem) -> set[int]:
    """The ids of papers in ``papers`` that match any of the item's labels."""
    ids: set[int] = set()
    for paper in papers:
        if any(paper_matches(paper, label) for label in item.relevant):
            ids.add(paper["id"])
    return ids


# --------------------------------------------------------------------------
# Retrieval evaluation (no API key required).
# --------------------------------------------------------------------------


def build_retriever(papers: list[dict]) -> TfidfRetriever:
    """Build a retriever over every paper that has extracted text."""
    retriever = TfidfRetriever()
    for paper in papers:
        if paper.get("text"):
            retriever.add_document(paper["id"], paper["title"], paper["text"])
    return retriever


@dataclass
class RetrievalResult:
    """Per-question retrieval outcome."""

    question: str
    expected: set[int]              # ids we labeled relevant
    retrieved_ids: list[int]        # distinct paper ids in the top-k passages
    recall: float                   # |found ∩ expected| / |expected|
    hit: bool                       # at least one expected id retrieved

    @property
    def found(self) -> set[int]:
        return self.expected & set(self.retrieved_ids)


@dataclass
class RetrievalReport:
    """Aggregate retrieval metrics across a whole question set."""

    k: int
    results: list[RetrievalResult] = field(default_factory=list)

    @property
    def scored(self) -> list[RetrievalResult]:
        """Only questions that actually had labeled-relevant papers present.

        A question whose relevant papers aren't in this library can't be scored
        for recall, so it's excluded from the averages rather than counted as a
        zero (which would unfairly punish a smaller library).
        """
        return [r for r in self.results if r.expected]

    @property
    def mean_recall(self) -> float:
        scored = self.scored
        return sum(r.recall for r in scored) / len(scored) if scored else 0.0

    @property
    def hit_rate(self) -> float:
        scored = self.scored
        return sum(1 for r in scored if r.hit) / len(scored) if scored else 0.0

    @property
    def skipped(self) -> int:
        """Questions with no labeled-relevant paper in this library."""
        return len(self.results) - len(self.scored)


def evaluate_retrieval(
    papers: list[dict], qaset: list[QAItem], k: int = 5
) -> RetrievalReport:
    """Score retrieval recall@k / hit@k for every question in ``qaset``."""
    retriever = build_retriever(papers)
    report = RetrievalReport(k=k)
    for item in qaset:
        expected = relevant_ids(papers, item)
        hits = retriever.retrieve(item.question, k=k)
        # Distinct paper ids, best-first, because several top-k passages can
        # come from the same paper.
        retrieved_ids: list[int] = []
        for chunk, _score in hits:
            if chunk.paper_id not in retrieved_ids:
                retrieved_ids.append(chunk.paper_id)
        found = expected & set(retrieved_ids)
        recall = len(found) / len(expected) if expected else 0.0
        report.results.append(
            RetrievalResult(
                question=item.question,
                expected=expected,
                retrieved_ids=retrieved_ids,
                recall=recall,
                hit=bool(found),
            )
        )
    return report


# --------------------------------------------------------------------------
# Answer evaluation: is the generated answer grounded in what was retrieved?
# --------------------------------------------------------------------------


def parse_citations(answer: str) -> set[int]:
    """Extract the distinct source numbers cited in an answer ([S3] -> 3)."""
    return {int(n) for n in _CITE_RE.findall(answer)}


def legend_titles(legend: list[str]) -> dict[int, str]:
    """Map a source legend (``["[S1] Title", ...]``) to {number: title}."""
    mapping: dict[int, str] = {}
    for line in legend:
        m = _CITE_RE.search(line)
        if m:
            mapping[int(m.group(1))] = line[m.end():].strip()
    return mapping


@dataclass
class AnswerResult:
    """Per-question grounding outcome for a generated answer."""

    question: str
    cited: set[int]                 # source numbers the answer cited
    valid_cited: set[int]           # of those, the ones that exist in the legend
    relevant_cited: set[int]        # valid citations pointing at a labeled paper
    n_sources: int                  # how many sources were retrieved/offered

    @property
    def faithful(self) -> bool:
        """Every citation the answer made points at a real retrieved source."""
        return self.cited == self.valid_cited

    @property
    def grounded(self) -> bool:
        """The answer cited at least one real source."""
        return bool(self.valid_cited)

    @property
    def relevant(self) -> bool:
        """At least one cited source is a labeled-relevant paper."""
        return bool(self.relevant_cited)


@dataclass
class AnswerReport:
    results: list[AnswerResult] = field(default_factory=list)

    def _rate(self, pred) -> float:
        return (
            sum(1 for r in self.results if pred(r)) / len(self.results)
            if self.results
            else 0.0
        )

    @property
    def faithfulness(self) -> float:
        """Fraction of answers with zero hallucinated citations."""
        return self._rate(lambda r: r.faithful)

    @property
    def grounded_rate(self) -> float:
        return self._rate(lambda r: r.grounded)

    @property
    def relevance(self) -> float:
        return self._rate(lambda r: r.relevant)


def score_answer(
    papers: list[dict], item: QAItem, answer: str, legend: list[str]
) -> AnswerResult:
    """Score one generated answer for citation faithfulness and relevance.

    ``legend`` is the source list the generator returned (the ``__sources__``
    payload from :meth:`RagChat.ask_stream`): lines like ``"[S1] <title>"``.
    """
    cited = parse_citations(answer)
    titles = legend_titles(legend)
    valid = cited & set(titles)

    # Which papers did the user label relevant for this question?
    expected = relevant_ids(papers, item)
    id_by_title = {p["title"]: p["id"] for p in papers}

    relevant_cited: set[int] = set()
    for tag in valid:
        pid = id_by_title.get(titles[tag])
        if pid is not None and pid in expected:
            relevant_cited.add(tag)

    return AnswerResult(
        question=item.question,
        cited=cited,
        valid_cited=valid,
        relevant_cited=relevant_cited,
        n_sources=len(titles),
    )


def run_answer_eval(
    papers: list[dict],
    qaset: list[QAItem],
    answer_fn,
    k: int = 5,
) -> AnswerReport:
    """Generate an answer per question and score each for grounding.

    ``answer_fn(question, k) -> (answer_text, legend)`` produces the answer and
    its source legend. It is injected (rather than hard-wiring ``RagChat``) so
    the caller controls the model/key - and so tests can pass a deterministic
    stub with no network. See :func:`ragchat_answer_fn` for the real wiring.
    """
    report = AnswerReport()
    for item in qaset:
        answer, legend = answer_fn(item.question, k)
        report.results.append(score_answer(papers, item, answer, legend))
    return report


def ragchat_answer_fn(retriever: TfidfRetriever, api_key: str, model: str | None = None):
    """Build an ``answer_fn`` backed by a real :class:`~src.rag.RagChat`.

    Each call runs one retrieve-then-answer pass and returns
    ``(answer_text, legend)``. Kept separate from :func:`run_answer_eval` so the
    harness core stays import-light and testable without the Anthropic SDK.
    """
    from .rag import RagChat

    def answer_fn(question: str, k: int) -> tuple[str, list[str]]:
        chat = RagChat(retriever, api_key=api_key)
        if model:
            chat.model = model
        parts: list[str] = []
        legend: list[str] = []
        for item in chat.ask_stream(question, k=k):
            if isinstance(item, tuple) and item and item[0] == "__sources__":
                legend = item[1]
            else:
                parts.append(item)
        return "".join(parts), legend

    return answer_fn


# --------------------------------------------------------------------------
# Reporting.
# --------------------------------------------------------------------------


def format_report(
    retrieval: RetrievalReport, answer: AnswerReport | None = None
) -> str:
    """Render a human-readable text report of the metrics."""
    lines: list[str] = []
    lines.append("=" * 60)
    lines.append("PaperLib evaluation")
    lines.append("=" * 60)
    lines.append("")
    lines.append(f"RETRIEVAL  (k={retrieval.k}, {len(retrieval.scored)} scored"
                 f"{f', {retrieval.skipped} skipped' if retrieval.skipped else ''})")
    lines.append(f"  recall@{retrieval.k}:  {retrieval.mean_recall:.0%}")
    lines.append(f"  hit@{retrieval.k}:     {retrieval.hit_rate:.0%}")
    lines.append("")
    for r in retrieval.results:
        if not r.expected:
            mark = "-- "
            detail = "no labeled paper in this library"
        else:
            mark = "OK " if r.hit else "XX "
            detail = f"recall {r.recall:.0%}  ({len(r.found)}/{len(r.expected)})"
        lines.append(f"  {mark}{r.question[:52]:<52} {detail}")

    if answer is not None:
        lines.append("")
        lines.append(f"ANSWER  ({len(answer.results)} questions)")
        lines.append(f"  citation faithfulness: {answer.faithfulness:.0%}"
                     "  (no hallucinated [S#])")
        lines.append(f"  citation relevance:    {answer.relevance:.0%}"
                     "  (cited a labeled paper)")
        lines.append(f"  grounded rate:         {answer.grounded_rate:.0%}"
                     "  (cited any source)")
        lines.append("")
        for r in answer.results:
            bad = r.cited - r.valid_cited
            flag = "OK " if r.faithful and r.relevant else "XX "
            note = ""
            if bad:
                note = f"  hallucinated {sorted(bad)}"
            elif not r.grounded:
                note = "  no citations"
            elif not r.relevant:
                note = "  cited off-label source"
            lines.append(f"  {flag}{r.question[:52]:<52}{note}")

    lines.append("")
    return "\n".join(lines)
