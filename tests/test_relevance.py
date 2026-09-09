"""Tests for LLM-based semantic ranking (rag.rank_by_topic_llm).

These NEVER hit the real Anthropic API: a fake client is injected via the
``client`` parameter, so no ``anthropic.Anthropic`` is ever constructed and no
network call is made. The fallback paths construct no client at all.
"""

import re

from src import rag


# --- a fake Anthropic client -------------------------------------------------
#
# It mimics the shape rank_by_topic_llm relies on: ``client.messages.create``
# returns an object whose ``.content`` is a list of blocks, one of which is a
# ``tool_use`` block carrying ``.input`` (the structured JSON). It reads the
# ``[id=N]`` markers out of the prompt so batching works naturally.


class _FakeToolUse:
    type = "tool_use"

    def __init__(self, rankings):
        self.input = {"rankings": rankings}


class _FakeResponse:
    def __init__(self, rankings):
        self.content = [_FakeToolUse(rankings)]


class _FakeMessages:
    def __init__(self, parent):
        self.parent = parent

    def create(self, **kwargs):
        self.parent.n_calls += 1
        self.parent.last_kwargs = kwargs
        content = kwargs["messages"][0]["content"]
        ids = [int(x) for x in re.findall(r"\[id=(\d+)\]", content)]
        rankings = [
            {"id": i, "score": self.parent.scores[i][0],
             "reason": self.parent.scores[i][1]}
            for i in ids
            if i in self.parent.scores
        ]
        return _FakeResponse(rankings)


class FakeClient:
    def __init__(self, scores):
        # scores: {paper_id: (score, reason)}
        self.scores = scores
        self.messages = _FakeMessages(self)
        self.n_calls = 0
        self.last_kwargs = None


class BoomClient:
    """A client whose API call always raises, to exercise the error fallback."""

    class _Boom:
        def create(self, **kwargs):
            raise RuntimeError("network is down")

    def __init__(self):
        self.messages = self._Boom()


def _papers():
    return [
        {"id": 1, "title": "Muon Tomography of Volcanoes",
         "keywords": ["muon", "tomography"],
         "text": "muon tomography imaging density " * 20},
        {"id": 2, "title": "Silicon Detectors",
         "keywords": ["silicon", "detector"],
         "text": "silicon detector charged particle tracking " * 20},
        {"id": 3, "title": "Sourdough Bread",
         "keywords": ["bread", "flour"],
         "text": "flour water yeast bake bread " * 20},
    ]


# --- structured-output parsing ----------------------------------------------

def test_parses_merges_sorts_and_drops_below_threshold():
    scores = {
        1: (0.90, "directly about muon tomography"),
        2: (0.70, "detectors are used in muon tomography"),
        3: (0.05, "unrelated food topic"),
    }
    client = FakeClient(scores)
    ranked = rag.rank_by_topic_llm(
        "muon tomography", _papers(), api_key="unused", client=client)

    # Sorted by score desc; the below-threshold bread paper is dropped.
    assert [p["id"] for p in ranked] == [1, 2]
    assert ranked[0]["score"] == 0.90
    assert ranked[0]["reason"] == "directly about muon tomography"
    assert all(p["id"] != 3 for p in ranked)


def test_semantic_relevance_surfaces_papers_with_no_shared_words():
    # The silicon-detector paper shares no words with "muon tomography" yet the
    # model rates it relevant -- something lexical TF-IDF would miss.
    scores = {1: (0.9, "core"), 2: (0.8, "detectors enable it"), 3: (0.0, "no")}
    client = FakeClient(scores)
    ranked = rag.rank_by_topic_llm(
        "muon tomography", _papers(), api_key="unused", client=client)
    assert 2 in [p["id"] for p in ranked]


def test_top_n_applied():
    scores = {1: (0.9, "a"), 2: (0.7, "b"), 3: (0.5, "c")}
    client = FakeClient(scores)
    ranked = rag.rank_by_topic_llm(
        "topic", _papers(), api_key="unused", top_n=1, client=client)
    assert len(ranked) == 1
    assert ranked[0]["id"] == 1


def test_uses_rank_model_and_forced_tool():
    client = FakeClient({1: (0.9, "a"), 2: (0.9, "b"), 3: (0.9, "c")})
    rag.rank_by_topic_llm("topic", _papers(), api_key="unused", client=client)
    kw = client.last_kwargs
    assert kw["model"] == rag.RANK_MODEL == "claude-haiku-4-5"
    assert kw["tool_choice"] == {"type": "tool", "name": "record_relevance"}
    assert kw["tools"][0]["name"] == "record_relevance"


def test_batches_large_libraries():
    papers = [
        {"id": i, "title": f"Paper {i}", "keywords": [], "text": "text " * 5}
        for i in range(1, 86)  # 85 papers
    ]
    scores = {i: (0.5, "ok") for i in range(1, 86)}
    client = FakeClient(scores)
    ranked = rag.rank_by_topic_llm(
        "topic", papers, api_key="unused", client=client)
    # 85 papers / batch size 40 -> 3 API calls, and every paper scored.
    assert client.n_calls == 3
    assert len(ranked) == 85


# --- graceful fallback -------------------------------------------------------

def test_falls_back_to_lexical_when_no_api_key():
    papers = _papers()
    result = rag.rank_by_topic_llm("muon tomography", papers, api_key=None)
    expected = rag.rank_by_topic("muon tomography", papers)
    assert result == expected


def test_falls_back_to_lexical_on_client_error():
    papers = _papers()
    result = rag.rank_by_topic_llm(
        "muon tomography", papers, api_key="unused", client=BoomClient())
    expected = rag.rank_by_topic("muon tomography", papers)
    assert result == expected


def test_falls_back_to_lexical_on_empty_response():
    papers = _papers()
    # A client that returns no rankings at all -> degrade, don't return [].
    client = FakeClient({})
    result = rag.rank_by_topic_llm(
        "muon tomography", papers, api_key="unused", client=client)
    expected = rag.rank_by_topic("muon tomography", papers)
    assert result == expected


def test_empty_paper_list_returns_empty():
    assert rag.rank_by_topic_llm("topic", [], api_key="unused") == []


def test_threshold_and_model_constants_are_sane():
    assert 0.0 < rag.RANK_THRESHOLD < 0.5
    assert rag.RANK_BATCH_SIZE > 0
    assert rag.RANK_MODEL == "claude-haiku-4-5"
