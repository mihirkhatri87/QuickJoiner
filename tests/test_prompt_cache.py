"""Prompt caching (Anthropic cache_control) + stable-prefix discipline.

The Anthropic client needs credentials, so these tests exercise the pure
request-shaping layer: _mark_cache_breakpoints and _build_kwargs on a provider
instance built without the client (__new__ + attrs), plus the agent-side
deterministic tool ordering that all three providers' caching depends on.
"""

from quickjoiner.agent.agent import OnboardingAgent
from quickjoiner.llm.anthropic_provider import (
    _CACHE_SPACING_BLOCKS,
    _MAX_MESSAGE_MARKS,
    AnthropicProvider,
)
from quickjoiner.llm.base import AgentTool, ChatResult, ToolCall, ToolSpec

from tests.test_agent_loop import ScriptedProvider


def _provider(cache: bool = True, thinking: bool = False) -> AnthropicProvider:
    p = AnthropicProvider.__new__(AnthropicProvider)
    p._model = "claude-opus-4-8"
    p._max_tokens = 8192
    p._thinking = thinking
    p._cache = cache
    return p


def _marked(wire):
    """(message_index, block_index) of every cache_control marker."""
    out = []
    for mi, msg in enumerate(wire):
        content = msg["content"]
        if isinstance(content, list):
            for bi, block in enumerate(content):
                if "cache_control" in block:
                    out.append((mi, bi))
    return out


# --- _mark_cache_breakpoints (pure) ---


def test_moving_breakpoint_on_last_block_of_last_message():
    wire = AnthropicProvider._to_wire([{"role": "user", "content": "hello"}])
    wire = AnthropicProvider._mark_cache_breakpoints(wire)
    # String content converted to a text block carrying the marker.
    assert wire[0]["content"] == [
        {"type": "text", "text": "hello", "cache_control": {"type": "ephemeral"}}
    ]


def test_tool_loop_gets_marker_on_last_tool_result():
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c1", name="t", input={})]},
        {"role": "tool", "tool_call_id": "c1", "name": "t", "content": "out"},
    ]
    wire = AnthropicProvider._mark_cache_breakpoints(AnthropicProvider._to_wire(messages))
    last = wire[-1]["content"][-1]
    assert last["type"] == "tool_result" and last["cache_control"] == {"type": "ephemeral"}


def test_intermediate_markers_stay_inside_lookback_and_cap():
    # A long tool-heavy conversation: many rounds, several blocks per round.
    messages = [{"role": "user", "content": "q"}]
    for i in range(20):
        calls = [ToolCall(id=f"c{i}-{j}", name="t", input={}) for j in range(3)]
        messages.append({"role": "assistant", "content": "step", "tool_calls": calls})
        for c in calls:
            messages.append({"role": "tool", "tool_call_id": c.id, "name": "t", "content": "out"})
    wire = AnthropicProvider._mark_cache_breakpoints(AnthropicProvider._to_wire(messages))

    marks = _marked(wire)
    assert 1 <= len(marks) <= _MAX_MESSAGE_MARKS
    # Newest marker is the last block of the last message.
    assert marks[-1] == (len(wire) - 1, len(wire[-1]["content"]) - 1)
    # Consecutive markers must sit within the API's 20-block lookback window of
    # each other, or the newest breakpoint can't find the previous cache entry.
    flat = []
    for mi, msg in enumerate(wire):
        for bi, _ in enumerate(msg["content"]):
            flat.append((mi, bi))
    positions = [flat.index(m) for m in marks]
    gaps = [b - a for a, b in zip(positions, positions[1:])]
    assert all(g <= 20 for g in gaps), gaps
    assert _CACHE_SPACING_BLOCKS < 20  # the invariant the spacing constant encodes


def test_marker_never_lands_on_a_thinking_block():
    # An assistant turn whose only blocks are thinking blocks (no text, no tools)
    # must not receive a marker — cache_control is invalid on thinking blocks.
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant",
            "content": "",
            "thinking_blocks": [{"type": "thinking", "thinking": "hmm", "signature": "s"}],
        },
    ]
    wire = AnthropicProvider._mark_cache_breakpoints(AnthropicProvider._to_wire(messages))
    assert "cache_control" not in wire[-1]["content"][-1]
    # The marker fell back to an earlier cacheable message.
    assert _marked(wire) == [(0, 0)]


def test_history_thinking_blocks_are_not_mutated():
    # Thinking blocks ride history by reference; marking must copy, not mutate.
    tb = {"type": "thinking", "thinking": "hmm", "signature": "s"}
    messages = [
        {"role": "user", "content": "q"},
        {"role": "assistant", "content": "ans", "thinking_blocks": [tb]},
    ]
    AnthropicProvider._mark_cache_breakpoints(AnthropicProvider._to_wire(messages))
    assert "cache_control" not in tb


# --- _build_kwargs ---


def test_build_kwargs_caches_system_and_last_message():
    p = _provider(cache=True)
    spec = ToolSpec(name="search", description="d", input_schema={"type": "object"})
    kwargs = p._build_kwargs([{"role": "user", "content": "q"}], system="SYS", tools=[spec])
    # One breakpoint on the system block caches tools+system together.
    assert kwargs["system"] == [
        {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}
    ]
    assert "cache_control" in kwargs["messages"][-1]["content"][-1]
    assert kwargs["tools"][0]["name"] == "search"


def test_build_kwargs_cache_off_is_byte_identical_to_before():
    p = _provider(cache=False)
    kwargs = p._build_kwargs([{"role": "user", "content": "q"}], system="SYS", tools=None)
    assert kwargs["system"] == "SYS"
    assert kwargs["messages"] == [{"role": "user", "content": "q"}]


def test_build_kwargs_thinking_is_adaptive():
    kwargs = _provider(thinking=True)._build_kwargs([{"role": "user", "content": "q"}], None, None)
    assert kwargs["thinking"] == {"type": "adaptive"}


# --- stable-prefix discipline: deterministic tool ordering ---


def _tool(name: str) -> AgentTool:
    return AgentTool(
        spec=ToolSpec(name=name, description=name, input_schema={"type": "object"}),
        fn=lambda **kw: "ok",
    )


def test_agent_sends_tool_specs_sorted_by_name():
    provider = ScriptedProvider([ChatResult(text="done")])
    agent = OnboardingAgent(provider, [_tool("zeta"), _tool("alpha"), _tool("mid")], system="s")
    agent.ask("q")
    names = [t.name for t in provider.calls[0]["tools"]]
    assert names == sorted(names) == ["alpha", "mid", "zeta"]
