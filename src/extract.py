"""Text extraction and keyword-based categorization for papers.

Kept dependency-light on purpose: pypdf pulls the text out of a PDF, and the
keyword logic is plain Python (a stopword list + term-frequency counting) so
you can read exactly how a paper gets categorized. There is no ML model here.
"""

from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

# A small English stopword list. Good enough to keep keyword extraction from
# being swamped by "the", "and", "of", etc. Extend it as you notice noise.
STOPWORDS = {
    "the", "and", "for", "are", "but", "not", "you", "all", "any", "can", "had",
    "her", "was", "one", "our", "out", "day", "get", "has", "him", "his", "how",
    "man", "new", "now", "old", "see", "two", "way", "who", "boy", "did", "its",
    "let", "put", "say", "she", "too", "use", "with", "this", "that", "from",
    "they", "will", "would", "there", "their", "what", "about", "which", "when",
    "were", "been", "have", "your", "here", "than", "then", "them", "these",
    "some", "into", "more", "such", "also", "only", "very", "over", "most",
    "other", "using", "used", "based", "paper", "we", "our", "results", "method",
    "methods", "approach", "propose", "proposed", "show", "shown", "figure",
    "table", "section", "et", "al", "eq", "fig", "abstract", "introduction",
    "conclusion", "references", "however", "therefore", "thus", "between",
    "each", "both", "may", "given", "where", "while", "within", "via", "per",
    "data", "model", "models", "problem", "different", "number", "set", "case",
    "study", "work", "research", "analysis", "system", "systems", "process",
}

WORD_RE = re.compile(r"[A-Za-z][A-Za-z\-]{2,}")


def extract_text(path: str | Path) -> str:
    """Extract plain text from a file. Supports .pdf and .txt.

    Returns "" if nothing could be read (encrypted PDF, image-only scan, etc.).
    """
    path = Path(path)
    suffix = path.suffix.lower()
    if suffix == ".txt":
        try:
            return path.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            return ""
    if suffix == ".pdf":
        return _extract_pdf(path)
    return ""


def _extract_pdf(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError:
        raise RuntimeError(
            "pypdf is not installed. Run: pip install -r requirements.txt"
        )
    try:
        reader = PdfReader(str(path))
    except Exception:
        return ""
    parts: list[str] = []
    for page in reader.pages:
        try:
            parts.append(page.extract_text() or "")
        except Exception:
            continue
    return "\n".join(parts)


def guess_title(text: str, fallback: str) -> str:
    """Best-effort paper title: the first substantial line of the text."""
    for line in text.splitlines():
        line = line.strip()
        # A title is usually a reasonably long line that isn't an email/url.
        if 15 <= len(line) <= 200 and "@" not in line and "http" not in line:
            return line
    return fallback


def extract_keywords(text: str, top_n: int = 8) -> list[str]:
    """Return the most salient single-word keywords in the text.

    Frequency-based: tokenize, drop stopwords and very common academic filler,
    then take the highest-frequency remaining terms.
    """
    tokens = [w.lower() for w in WORD_RE.findall(text)]
    counts = Counter(
        t for t in tokens if t not in STOPWORDS and not t.isdigit()
    )
    return [word for word, _ in counts.most_common(top_n)]


def categorize(keywords: list[str]) -> str:
    """Derive a single category label from a paper's keywords.

    First draft rule: the top keyword, title-cased. Simple and predictable;
    you can later map keyword clusters to nicer category names.
    """
    if not keywords:
        return "Uncategorized"
    return keywords[0].replace("-", " ").title()
