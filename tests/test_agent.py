"""Tests for the tool-calling LibraryAgent, driven by a scripted fake client."""

import types

from src import agent as agent_mod
from src.agent import LibraryAgent
from src.rag import TfidfRetriever


def _block(type, **kw):
    return types.SimpleNamespace(type=type, **kw)


def _resp(content, stop_reason):
    return types.SimpleNamespace(content=content, stop_reason=stop_reason)


class _ScriptedMessages:
    """Returns queued responses in order; records each create() call."""

    def __init__(self, responses):
        self._responses = list(responses)
        self.calls = []

    def create(self, **kwargs):
        # Snapshot the messages list: the agent mutates it in place after the
        # call, so a live reference would not reflect the state at call time.
        snapshot = dict(kwargs)
        snapshot["messages"] = list(kwargs.get("messages", []))
        self.calls.append(snapshot)
        return self._responses.pop(0)


class _AlwaysToolMessages:
    """Always asks for another tool call - to exercise the step cap."""

    def __init__(self):
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return _resp(
            [_block("tool_use", id=f"t{len(self.calls)}", name="search_library",
                    input={"query": "loop", "k": 1})],
            "tool_use",
        )


class _Client:
    def __init__(self, messages):
        self.messages = messages


def _retriever():
    r = TfidfRetriever()
    r.add_document(1, "Retrieval Paper", "retrieval augmented generation transformer " * 20)
    return r


def _collect(gen):
    events = list(gen)
    by_type = {}
    for e in events:
        by_type.setdefault(e["type"], []).append(e)
    return events, by_type


def test_agent_searches_then_answers():
    responses = [
        _resp([_block("tool_use", id="t1", name="search_library",
                      input={"query": "retrieval", "k": 3})], "tool_use"),
        _resp([_block("text", text="Retrieval works like this [S1].")], "end_turn"),
    ]
    msgs = _ScriptedMessages(responses)
    ag = LibraryAgent(_retriever(), client=_Client(msgs))

    events, by_type = _collect(ag.run_stream("how does retrieval work"))

    assert by_type["tool_call"][0]["query"] == "retrieval"
    assert by_type["tool_result"][0]["hits"] >= 1
    assert "".join(e["text"] for e in by_type["text"]) == "Retrieval works like this [S1]."
    assert by_type["sources"][0]["sources"]  # legend populated with [S1] ...

    # The second model call must have been fed the tool_result back.
    second_msgs = msgs.calls[1]["messages"]
    assert second_msgs[-1]["role"] == "user"
    assert second_msgs[-1]["content"][0]["type"] == "tool_result"


def test_agent_answers_without_searching():
    responses = [_resp([_block("text", text="No search needed.")], "end_turn")]
    ag = LibraryAgent(_retriever(), client=_Client(_ScriptedMessages(responses)))

    _events, by_type = _collect(ag.run_stream("hi"))

    assert "tool_call" not in by_type
    assert by_type["text"][0]["text"] == "No search needed."
    assert by_type["sources"][0]["sources"] == []  # nothing searched, empty legend


def test_agent_respects_step_cap():
    ag = LibraryAgent(_retriever(), client=_Client(_AlwaysToolMessages()), max_steps=2)

    _events, by_type = _collect(ag.run_stream("loop forever"))

    # Exactly max_steps searches, then a note that it stopped, then sources.
    assert len(by_type["tool_call"]) == 2
    assert any("Stopped after" in n["text"] for n in by_type["note"])
    assert "sources" in by_type
