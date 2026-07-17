from quickjoiner.agent.agent import MAX_TOOL_ROUNDS, OnboardingAgent
from quickjoiner.llm.base import AgentTool, ChatResult, LLMProvider, ToolCall, ToolSpec


class ScriptedProvider(LLMProvider):
    """Returns a scripted sequence of results and records what it was sent."""

    name = "scripted"

    def __init__(self, results):
        self.results = list(results)
        self.calls = []

    def chat(self, messages, system=None, tools=None, on_stream=None):
        self.calls.append({"messages": list(messages), "system": system, "tools": tools})
        result = self.results.pop(0)
        if on_stream and result.text:
            on_stream("text", result.text)  # simulate one-shot streaming
        return result


def _echo_tool():
    return AgentTool(
        spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: f"echoed:{kw.get('value', '')}",
    )


def test_agent_runs_tools_then_answers():
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={"value": "hi"})]),
            ChatResult(text="final answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")

    answer, history = agent.ask("question")
    assert answer == "final answer"

    # Second model call must include the tool result in history.
    second_call_messages = provider.calls[1]["messages"]
    tool_msgs = [m for m in second_call_messages if m["role"] == "tool"]
    assert tool_msgs and tool_msgs[0]["content"] == "echoed:hi"
    assert history[-1] == {"role": "assistant", "content": "final answer"}


def test_agent_reports_unknown_tool_instead_of_crashing():
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="missing", input={})]),
            ChatResult(text="done"),
        ]
    )
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q")
    assert answer == "done"
    tool_msg = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]
    assert "unknown tool" in tool_msg["content"]


def _big_tool(size):
    return AgentTool(
        spec=ToolSpec(name="dump", description="dumps a lot", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: "x" * size,
    )


def test_agent_caps_large_tool_output_before_feeding_back():
    """An unbounded connector tool (e.g. the full Octopus dashboard) must be truncated
    before it re-enters the model context, or it overflows the window."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="dump", input={})]),
            ChatResult(text="answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_big_tool(500_000)], system="sys", tool_result_max_chars=1000)
    agent.ask("q")
    tool_msg = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) < 1100  # capped near the limit, not 500k
    assert tool_msg["content"].endswith("[tool output truncated]")


def test_agent_makes_final_tool_free_turn_when_round_limit_hit():
    """A model that loops on tool calls forever should still get one last tool-free turn
    to answer (or properly refuse) from what it gathered — not a canned limit message."""
    # Every scripted result asks for a tool, so the loop never converges on its own...
    looping = [
        ChatResult(text="", tool_calls=[ToolCall(id=f"c{i}", name="echo", input={})])
        for i in range(MAX_TOOL_ROUNDS)
    ]
    # ...until the final tool-free turn, which returns real text.
    looping.append(ChatResult(text="I haven't learned that yet."))
    provider = ScriptedProvider(looping)
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")

    answer, _ = agent.ask("q")
    assert answer == "I haven't learned that yet."
    # The last provider call is the tool-free backstop.
    assert provider.calls[-1]["tools"] is None


def test_agent_falls_back_gracefully_if_final_turn_also_fails():
    class FlakyProvider(ScriptedProvider):
        def chat(self, messages, system=None, tools=None, on_stream=None):
            if tools is None:  # the final tool-free turn
                raise RuntimeError("broker down")
            return super().chat(messages, system, tools, on_stream)

    provider = FlakyProvider(
        [ChatResult(text="", tool_calls=[ToolCall(id=f"c{i}", name="echo", input={})]) for i in range(MAX_TOOL_ROUNDS)]
    )
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q")
    assert "wasn't able to finish" in answer  # graceful message, no crash


# ------------------------------------------------ candidates event (plan 06 §C)

_SEARCH_RESULT = "[source: Deploys | uri: file://d.md | kind: doc | score: 0.71]\nWe deploy on Fridays."
_ANSWER_WITH_BLOCK = (
    "Two readings exist.\n\n"
    "```candidates\n"
    "1. Weekly cadence per the handbook | confidence=0.80 | sources: Deploys\n"
    "```"
)


def _searchish_tool():
    return AgentTool(
        spec=ToolSpec(name="echo", description="echo", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: _SEARCH_RESULT,
    )


def test_ask_emits_candidates_event_with_valid_block():
    provider = ScriptedProvider([
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={})]),
        ChatResult(text=_ANSWER_WITH_BLOCK),
    ])
    events = []
    agent = OnboardingAgent(provider, [_searchish_tool()], system="sys",
                            score_ledger={"deploys": 0.62})
    answer, history = agent.ask("q", on_event=lambda t, d: events.append((t, d)))

    cand_events = [d for t, d in events if t == "candidates"]
    assert len(cand_events) == 1
    import json as _json
    payload = _json.loads(cand_events[0])
    assert payload[0]["summary"] == "Weekly cadence per the handbook"
    assert payload[0]["confidence"] == 0.62  # ledger value, NOT the LLM's 0.80
    assert "```candidates" not in answer  # prose returned stripped
    assert "Two readings exist." in answer
    assert "```candidates" in history[-1]["content"]  # history keeps the raw text


def test_ask_without_block_emits_no_candidates_and_returns_text_verbatim():
    provider = ScriptedProvider([ChatResult(text="plain answer")])
    events = []
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q", on_event=lambda t, d: events.append((t, d)))
    assert answer == "plain answer"  # §2.5: unchanged experience
    assert not [t for t, _ in events if t == "candidates"]


def test_unresolvable_candidates_leave_text_untouched():
    """A block whose sources don't resolve to this turn's tool refs is dropped
    whole — the fence stays visible in the answer, no event fires."""
    fabricated = _ANSWER_WITH_BLOCK.replace("sources: Deploys", "sources: Invented Doc")
    provider = ScriptedProvider([
        ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={})]),
        ChatResult(text=fabricated),
    ])
    events = []
    agent = OnboardingAgent(provider, [_searchish_tool()], system="sys")
    answer, _ = agent.ask("q", on_event=lambda t, d: events.append((t, d)))
    assert answer == fabricated
    assert not [t for t, _ in events if t == "candidates"]


def test_candidates_postprocessing_never_raises(monkeypatch):
    import quickjoiner.agent.candidates as cand_mod

    def boom(_text):
        raise RuntimeError("parser exploded")

    monkeypatch.setattr(cand_mod, "parse_candidates", boom)
    provider = ScriptedProvider([ChatResult(text=_ANSWER_WITH_BLOCK)])
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q", on_event=lambda t, d: None)
    assert answer == _ANSWER_WITH_BLOCK  # exception swallowed, answer unchanged
