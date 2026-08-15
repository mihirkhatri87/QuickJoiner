import json

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


def test_empty_completion_falls_through_to_a_final_answer():
    # Regression ("qj ended with no response"): a reasoning model that returns NO tool call
    # and NO text must not yield a blank answer — the agent makes a final tool-free turn.
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={})]),
            ChatResult(text="   "),  # no tool calls, blank text -> the bug trigger
            ChatResult(text="here is the build link"),  # forced final tool-free turn
        ]
    )
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("give me the link")
    assert answer == "here is the build link"
    assert provider.calls[-1]["tools"] is None  # the final turn ran without tools


def test_blank_everywhere_yields_graceful_fallback_never_empty():
    provider = ScriptedProvider([ChatResult(text=""), ChatResult(text="")])
    agent = OnboardingAgent(provider, [_echo_tool()], system="sys")
    answer, _ = agent.ask("q")
    assert answer.strip() and "wasn't able to finish" in answer  # never a blank message


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
    assert len(tool_msg["content"]) < 1250  # capped near the limit, not 500k
    assert "[tool output truncated" in tool_msg["content"]


def test_truncation_marker_states_how_much_was_dropped():
    """A bare "truncated" marker tells the model a boundary exists but not which side of
    it the answer is on: losing 200 characters and losing 499,000 read identically, so a
    list that stops 0.2% in gets summarised as though it were the whole thing. State the
    scale — the same no-silent-caps rule the crawler and the graph tools follow."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="dump", input={})]),
            ChatResult(text="answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_big_tool(500_000)], system="sys", tool_result_max_chars=1000)
    agent.ask("q")
    content = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert "1,000" in content and "500,000" in content and "499,000" in content
    assert "PARTIAL" in content, "the model must be told the view is incomplete, not just cut"


def test_output_exactly_at_the_limit_is_not_marked_truncated():
    """Nothing was dropped, so claiming a partial view would be its own dishonesty —
    and would push the model to hedge an answer that is in fact complete."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="dump", input={})]),
            ChatResult(text="answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_big_tool(1000)], system="sys", tool_result_max_chars=1000)
    agent.ask("q")
    content = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]["content"]
    assert content == "x" * 1000
    assert "truncated" not in content


def _named_big_tool(name, size):
    return AgentTool(
        spec=ToolSpec(name=name, description="dumps a lot", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: "x" * size,
    )


def test_uncapped_tool_bypasses_the_char_cap():
    """graph_relations et al already bound themselves to a fixed shape (e.g. 400
    relationships) and state their own truncation in the returned text. Regression for
    "you missed some teams": the outer char cap used to re-truncate their already-complete,
    already-honest output, silently dropping data below whatever the tool itself reported."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="graph_relations", input={})]),
            ChatResult(text="answer"),
        ]
    )
    agent = OnboardingAgent(
        provider, [_named_big_tool("graph_relations", 500_000)], system="sys",
        tool_result_max_chars=1000, uncapped_tools=frozenset({"graph_relations"}),
    )
    agent.ask("q")
    tool_msg = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]
    assert len(tool_msg["content"]) == 500_000  # untouched — not capped, not truncated
    assert "[tool output truncated" not in tool_msg["content"]


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


# -- trace events -------------------------------------------------------------

def _events(agent, question="q"):
    seen = []
    agent.ask(question, on_event=lambda kind, data: seen.append((kind, data)))
    return seen


def test_tool_events_carry_arguments_and_outcome_in_order():
    """The reasoning-trace timeline is built entirely from these events, so each call
    must name what it was actually doing (its arguments) and how it came back, and the
    two must arrive in that order and pair up by id."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="echo", input={"value": "hi"})]),
            ChatResult(text="answer"),
        ]
    )
    seen = _events(OnboardingAgent(provider, [_echo_tool()], system="sys"))
    kinds = [k for k, _ in seen]
    assert kinds.index("tool_call") < kinds.index("tool_result")

    call = json.loads(dict(seen)["tool_call"])
    assert call == {"id": "c1", "name": "echo", "args": {"value": "hi"}}
    result = json.loads(dict(seen)["tool_result"])
    assert result == {"id": "c1", "name": "echo", "ok": True,
                      "summary": "echoed:hi", "chars": len("echoed:hi")}


def test_a_failing_tool_is_reported_as_not_ok():
    """A trace that renders a crashed call as a normal step would misrepresent the run."""
    boom = AgentTool(
        spec=ToolSpec(name="boom", description="raises", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: (_ for _ in ()).throw(RuntimeError("nope")),
    )
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="boom", input={})]),
            ChatResult(text="answer"),
        ]
    )
    result = json.loads(dict(_events(OnboardingAgent(provider, [boom], system="sys")))["tool_result"])
    assert result["ok"] is False and "nope" in result["summary"]


def test_event_payloads_are_bounded_but_state_the_true_size():
    """A single tool result can be 24k chars and an argument arbitrarily long; neither
    should be pushed down the SSE stream in full just to render a line of trace. The
    preview is clipped, and `chars` carries the real length so a clipped preview can
    never be mistaken for the whole output."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="dump", input={"q": "y" * 5_000})]),
            ChatResult(text="answer"),
        ]
    )
    agent = OnboardingAgent(provider, [_big_tool(50_000)], system="sys", tool_result_max_chars=10_000)
    seen = dict(_events(agent))

    args = json.loads(seen["tool_call"])["args"]
    assert len(args["q"]) < 500 and args["q"].endswith("…")

    result = json.loads(seen["tool_result"])
    assert len(result["summary"]) < 1_000
    # `chars` measures the output as the MODEL received it — i.e. after `_cap`, whose
    # own truncation marker rides inside it. The trace reports the run as it happened,
    # not a pre-cap size the model never saw.
    tool_msg = [m for m in provider.calls[1]["messages"] if m["role"] == "tool"][0]
    assert result["chars"] == len(tool_msg["content"]) > len(result["summary"])


def _cited_tool(name, out):
    return AgentTool(
        spec=ToolSpec(name=name, description="d", input_schema={"type": "object", "properties": {}}),
        fn=lambda **kw: out,
    )


_HIT = ("[source: Caffeine - Team Charter | uri: https://wiki/Caffeine | kind: page | "
        "score: 0.81]\nMembers: Liu Maumasi (TL).")


def test_sources_event_carries_the_uri_behind_each_cited_title():
    """The tools already hand the MODEL a uri per hit; without this event the client
    only ever sees the label the model chose to write, so a citation of a readable
    title has nothing to link to."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="search_memory", input={"query": "teams"})]),
            ChatResult(text="Caffeine has 1 member [Caffeine - Team Charter]."),
        ]
    )
    seen = _events(OnboardingAgent(provider, [_cited_tool("search_memory", _HIT)], system="sys"))
    (refs,) = [json.loads(d) for k, d in seen if k == "sources"]
    assert refs[0]["label"] == "Caffeine - Team Charter"
    assert refs[0]["uri"] == "https://wiki/Caffeine"
    assert refs[0]["snippet"].startswith("Members: Liu Maumasi")


def test_a_source_already_announced_is_not_sent_again():
    """Round after round of searching returns the same documents; re-sending them would
    grow the stream quadratically and duplicate every entry in the sources list."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="search_memory", input={})]),
            ChatResult(text="", tool_calls=[ToolCall(id="c2", name="search_memory", input={})]),
            ChatResult(text="answer"),
        ]
    )
    seen = _events(OnboardingAgent(provider, [_cited_tool("search_memory", _HIT)], system="sys"))
    batches = [json.loads(d) for k, d in seen if k == "sources"]
    assert len(batches) == 1 and len(batches[0]) == 1


def test_a_tool_result_with_no_citable_sources_emits_no_event():
    """A refusal, or a graph tool whose evidence carries no uri, must not produce an
    empty sources batch for the client to render."""
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="search_memory", input={})]),
            ChatResult(text="I haven't learned that yet."),
        ]
    )
    tool = _cited_tool("search_memory", "NO_RESULTS: nothing relevant found in learned memory.")
    seen = _events(OnboardingAgent(provider, [tool], system="sys"))
    assert not [k for k, _ in seen if k == "sources"]


def test_a_cloned_repo_file_ref_carries_a_browsable_link_not_its_identity():
    """A git file's uri is "<clone-url>::<path>", which begins with https:// and would
    otherwise be offered to the reader as a link straight to a 404. `link` is where it
    actually opens; `uri` stays the identity the tools reported."""
    hit = ("[source: Connector/Program.cs | uri: "
           "https://gitlab.otxlab.net/zix/dev/appriver.connector.git::Source/Program.cs "
           "| kind: code | score: 0.7]\nnamespace AppRiver;")
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="search_memory", input={})]),
            ChatResult(text="See [Connector/Program.cs]."),
        ]
    )
    seen = _events(OnboardingAgent(provider, [_cited_tool("search_memory", hit)], system="sys"))
    (ref,) = json.loads(dict(seen)["sources"])
    assert "::" in ref["uri"]  # identity preserved
    assert ref["link"] == (
        "https://gitlab.otxlab.net/zix/dev/appriver.connector/-/blob/HEAD/Source/Program.cs")


def test_a_ref_with_no_browsable_address_carries_no_link_at_all():
    """A local file and a distilled conversation open nothing; offering either as a link
    is a dead click, so the field is simply absent."""
    hit = "[source: handbook.md | uri: file:///C:/docs/handbook.md | kind: doc | score: 0.8]\nLeave policy"
    provider = ScriptedProvider(
        [
            ChatResult(text="", tool_calls=[ToolCall(id="c1", name="search_memory", input={})]),
            ChatResult(text="answer"),
        ]
    )
    seen = _events(OnboardingAgent(provider, [_cited_tool("search_memory", hit)], system="sys"))
    (ref,) = json.loads(dict(seen)["sources"])
    assert ref["uri"].startswith("file://") and "link" not in ref
