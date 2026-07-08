from quickjoiner.agent.agent import OnboardingAgent
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
