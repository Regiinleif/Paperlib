"""A Claude tool-calling agent over the paper library.

Where :class:`~src.rag.RagChat` does a single retrieve-then-answer pass, this
agent hands Claude a ``search_library`` tool and lets *it* decide when and what
to search. That enables multi-step retrieval: Claude can search, read the
passages, then refine the query or search a different angle before answering -
all with inline citations to the passages it actually used.

The loop is deliberately explicit (no black-box framework):

    1. Ask Claude with the tool available.
    2. If it emits tool_use blocks, run each search, feed the tagged passages
       back as tool_result, and loop.
    3. When it stops calling tools, that turn's text is the final answer.

Retrieval itself is the offline TF-IDF :class:`~src.rag.TfidfRetriever` - so the
search tool works with no network. (Semantic, Claude-judged relevance ranking
with a lexical fallback already lives in ``rag.rank_by_topic_llm`` /
``rag.rank_by_topic`` and is used for project scoping.)

Every event is yielded as a small dict so the API/SSE layer and the tests can
consume the same stream:

    {"type": "tool_call",   "query": str, "k": int}
    {"type": "tool_result", "query": str, "hits": int}
    {"type": "text",        "text": str}
    {"type": "sources",     "sources": list[str]}
    {"type": "note",        "text": str}          # e.g. truncation / step cap
"""

from __future__ import annotations

from . import config
from .rag import TfidfRetriever

# Cap tool rounds so a confused model can't loop forever. Five is plenty for
# "search, refine, maybe one more angle, answer".
MAX_STEPS = 5

# Generous per-turn cap; the final synthesis can be long.
MAX_TOKENS = 8000

SEARCH_TOOL = {
    "name": "search_library",
    "description": (
        "Search the user's personal research-paper library for passages "
        "relevant to a query. Returns the most relevant passages, each tagged "
        "with a source id like [S1]. Call this as many times as you need - "
        "refine the query or try a different angle - before you answer. You "
        "MUST cite the [S#] tags of any passage you rely on."
    ),
    "input_schema": {
        "type": "object",
        "properties": {
            "query": {
                "type": "string",
                "description": "What to search for, in natural language.",
            },
            "k": {
                "type": "integer",
                "description": "How many passages to return (default 5).",
            },
        },
        "required": ["query"],
    },
}

SYSTEM_PROMPT = (
    "You are a research assistant embedded in the user's personal paper "
    "library. You cannot see the papers directly - use the search_library "
    "tool to find relevant passages, and search more than once if the first "
    "results are thin or a follow-up angle would help. Answer using ONLY the "
    "passages the tool returns, and cite the [S#] tags of the passages you "
    "rely on. If the library does not contain the answer, say so plainly "
    "rather than guessing. Be concise and precise."
)


class LibraryAgent:
    """Runs one agentic conversation over a retriever."""

    def __init__(
        self,
        retriever: TfidfRetriever,
        api_key: str | None = None,
        client=None,
        max_steps: int = MAX_STEPS,
    ):
        if client is not None:
            self.client = client
        else:
            import anthropic

            self.client = (
                anthropic.Anthropic(api_key=api_key) if api_key else anthropic.Anthropic()
            )
        self.retriever = retriever
        self.model = config.get_model()
        self.max_steps = max_steps

    def _run_search(self, query: str, k: int, legend: dict[str, str]) -> tuple[str, int]:
        """Execute one search; return (tool_result_text, hit_count).

        Assigns each returned passage a stable [S#] tag (unique across the whole
        run) and records it in ``legend`` so the final answer's citations map
        back to real papers.
        """
        hits = self.retriever.retrieve(query, k=k)
        blocks: list[str] = []
        for chunk, _score in hits:
            tag = f"S{len(legend) + 1}"
            legend[tag] = chunk.title
            blocks.append(f'[{tag}] (from "{chunk.title}")\n{chunk.text}')
        text = "\n\n".join(blocks) or "(no relevant passages found for this query)"
        return text, len(hits)

    def run_stream(self, question: str):
        """Drive the tool loop, yielding event dicts (see module docstring)."""
        legend: dict[str, str] = {}
        messages: list[dict] = [{"role": "user", "content": question}]

        for _step in range(self.max_steps):
            response = self.client.messages.create(
                model=self.model,
                max_tokens=MAX_TOKENS,
                system=SYSTEM_PROMPT,
                tools=[SEARCH_TOOL],
                messages=messages,
            )
            messages.append({"role": "assistant", "content": response.content})

            tool_uses = []
            for block in response.content:
                btype = getattr(block, "type", None)
                if btype == "text" and getattr(block, "text", ""):
                    yield {"type": "text", "text": block.text}
                elif btype == "tool_use":
                    tool_uses.append(block)

            # No tool calls -> this turn's text was the final answer.
            if getattr(response, "stop_reason", None) != "tool_use" or not tool_uses:
                if getattr(response, "stop_reason", None) == "max_tokens":
                    yield {"type": "note", "text": "[Answer truncated - hit max length.]"}
                break

            tool_results = []
            for tu in tool_uses:
                query = str(tu.input.get("query", "")).strip()
                k = int(tu.input.get("k", 5) or 5)
                yield {"type": "tool_call", "query": query, "k": k}
                result_text, n = self._run_search(query, k, legend)
                yield {"type": "tool_result", "query": query, "hits": n}
                tool_results.append(
                    {"type": "tool_result", "tool_use_id": tu.id, "content": result_text}
                )
            messages.append({"role": "user", "content": tool_results})
        else:
            # Loop ran the full budget without a plain-text finish.
            yield {"type": "note", "text": f"[Stopped after {self.max_steps} search steps.]"}

        yield {"type": "sources", "sources": [f"[{t}] {title}" for t, title in legend.items()]}
