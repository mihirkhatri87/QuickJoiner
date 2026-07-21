"""LiteLLM (OpenAI-compatible) provider: URL construction, neutral<->wire translation,
streamed tool-call accumulation, lenient argument parsing, and end-to-end request/response
via httpx.MockTransport (no live proxy needed).
"""

from __future__ import annotations

import json

import httpx

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import ToolCall, ToolSpec, sanitize_tool_name
from quickjoiner.llm import litellm_provider
from quickjoiner.llm.litellm_provider import (
    LiteLLMProvider,
    _parse_arguments,
    _retry_delay,
    _sse_json,
    _strip_rejected,
    _tool_calls_from_acc,
    accumulate_delta,
)


def _cfg(**kw) -> LLMConfig:
    kw.setdefault("base_url", "http://proxy:4000")
    return LLMConfig(provider="litellm", **kw)


# --------------------------------------------------------------- endpoint URL

def test_endpoint_url_construction():
    assert LiteLLMProvider(_cfg(base_url="http://p:4000"))._url == "http://p:4000/chat/completions"
    assert LiteLLMProvider(_cfg(base_url="http://p:4000/"))._url == "http://p:4000/chat/completions"
    # a /v1 base and a full endpoint are both respected, not doubled
    assert LiteLLMProvider(_cfg(base_url="http://p:4000/v1"))._url == "http://p:4000/v1/chat/completions"
    assert (
        LiteLLMProvider(_cfg(base_url="http://p:4000/chat/completions"))._url
        == "http://p:4000/chat/completions"
    )


# --------------------------------------------------------------- _to_wire

def test_to_wire_translates_neutral_to_openai():
    messages = [
        {"role": "user", "content": "hi"},
        {"role": "assistant", "content": "", "tool_calls": [ToolCall(id="c1", name="echo", input={"v": 1})]},
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "out"},
    ]
    wire = LiteLLMProvider._to_wire(messages, "sys")
    assert wire[0] == {"role": "system", "content": "sys"}
    assert wire[1] == {"role": "user", "content": "hi"}
    asst = wire[2]
    assert asst["role"] == "assistant"
    assert asst["content"] is None  # null content alongside tool calls
    tc = asst["tool_calls"][0]
    assert tc["id"] == "c1" and tc["type"] == "function"
    assert tc["function"]["name"] == "echo"
    assert json.loads(tc["function"]["arguments"]) == {"v": 1}  # arguments are a JSON string
    assert wire[3] == {"role": "tool", "tool_call_id": "c1", "content": "out"}


def test_to_wire_omits_system_when_absent():
    wire = LiteLLMProvider._to_wire([{"role": "user", "content": "q"}], None)
    assert wire == [{"role": "user", "content": "q"}]


# --------------------------------------------------------------- streaming folds

def test_accumulate_delta_folds_content_reasoning_and_streamed_tool_calls():
    events: list[tuple[str, str]] = []
    acc = {"text": "", "thinking": "", "tool_calls": {}}
    cb = lambda k, d: events.append((k, d))  # noqa: E731
    accumulate_delta({"reasoning_content": "think "}, acc, cb)
    accumulate_delta({"content": "Hello "}, acc, cb)
    accumulate_delta({"content": "world"}, acc, cb)
    # one tool call arriving across two fragments, merged by index
    accumulate_delta(
        {"tool_calls": [{"index": 0, "id": "call_a", "function": {"name": "echo", "arguments": '{"v'}}]},
        acc, cb,
    )
    accumulate_delta({"tool_calls": [{"index": 0, "function": {"arguments": '": 1}'}}]}, acc, cb)

    assert acc["text"] == "Hello world"
    assert acc["thinking"] == "think "
    calls = _tool_calls_from_acc(acc["tool_calls"])
    assert len(calls) == 1
    assert calls[0].id == "call_a" and calls[0].name == "echo" and calls[0].input == {"v": 1}
    # reasoning is surfaced on the thinking channel, before the first text token
    assert events[0] == ("thinking", "think ")
    assert events[1] == ("text", "Hello ")


def test_accumulate_delta_handles_two_parallel_tool_calls():
    acc = {"text": "", "thinking": "", "tool_calls": {}}
    accumulate_delta(
        {"tool_calls": [
            {"index": 0, "id": "a", "function": {"name": "one", "arguments": "{}"}},
            {"index": 1, "id": "b", "function": {"name": "two", "arguments": "{}"}},
        ]},
        acc, None,
    )
    calls = _tool_calls_from_acc(acc["tool_calls"])
    assert [c.name for c in calls] == ["one", "two"]  # ordered by index


# --------------------------------------------------------------- argument parsing

def test_parse_arguments_is_lenient():
    assert _parse_arguments('{"a": 1}') == {"a": 1}
    assert _parse_arguments({"a": 1}) == {"a": 1}  # already a dict
    assert _parse_arguments("") == {}
    assert _parse_arguments(None) == {}
    assert _parse_arguments("{not json") == {}  # malformed -> empty, never raises
    assert _parse_arguments("[1, 2]") == {}  # valid JSON but not an object


# --------------------------------------------------------------- SSE line parsing

def test_sse_json_skips_keepalives_and_done():
    assert _sse_json('data: {"x": 1}') == {"x": 1}
    assert _sse_json("data: [DONE]") is None
    assert _sse_json(": keepalive comment") is None
    assert _sse_json("") is None


# --------------------------------------------------------------- end-to-end (MockTransport)

def test_chat_once_parses_tool_call():
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content)
        assert body["model"] == "gpt-4o-mini"
        assert body["stream"] is False
        assert body["tools"][0]["function"]["name"] == "echo"
        assert str(request.url) == "http://proxy:4000/chat/completions"
        return httpx.Response(
            200,
            json={
                "choices": [
                    {
                        "finish_reason": "tool_calls",
                        "message": {
                            "role": "assistant",
                            "content": None,
                            "tool_calls": [
                                {
                                    "id": "c9",
                                    "type": "function",
                                    "function": {"name": "echo", "arguments": '{"value": "x"}'},
                                }
                            ],
                        },
                    }
                ]
            },
        )

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    tools = [ToolSpec(name="echo", description="echoes input", input_schema={"type": "object"})]
    result = prov.chat([{"role": "user", "content": "call echo"}], system="sys", tools=tools)

    assert result.text == ""
    assert result.stop_reason == "tool_calls"
    assert len(result.tool_calls) == 1
    assert result.tool_calls[0].name == "echo"
    assert result.tool_calls[0].input == {"value": "x"}


def test_chat_stream_accumulates_sse():
    chunks = [
        {"choices": [{"delta": {"content": "Hel"}}]},
        {"choices": [{"delta": {"content": "lo"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"

    def handler(request: httpx.Request) -> httpx.Response:
        assert json.loads(request.content)["stream"] is True
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    seen: list[tuple[str, str]] = []
    result = prov.chat([{"role": "user", "content": "hi"}], on_stream=lambda k, d: seen.append((k, d)))

    assert result.text == "Hello"
    assert result.stop_reason == "stop"
    assert ("text", "Hel") in seen and ("text", "lo") in seen


# --------------------------------------------------------------- auth header

def test_api_key_header_from_env(monkeypatch):
    monkeypatch.setenv("MY_PROXY_KEY", "sk-secret")
    prov = LiteLLMProvider(_cfg(api_key_env="MY_PROXY_KEY"))
    assert prov._headers == {"Authorization": "Bearer sk-secret"}


def test_no_api_key_header_when_env_unset(monkeypatch):
    monkeypatch.delenv("LITELLM_API_KEY", raising=False)
    prov = LiteLLMProvider(_cfg())
    assert prov._headers == {}


# --------------------------------------------------------------- tool-name sanitization

def test_sanitize_tool_name_enforces_openai_pattern():
    # Spaces (the gpt-oss Harmony "to=functions.<name>" 500 trigger) and other invalid
    # chars collapse to underscores; length is capped at 64.
    assert sanitize_tool_name("octopus_deployment_status_Appriver Octopus") == "octopus_deployment_status_Appriver_Octopus"
    assert sanitize_tool_name("a.b/c d") == "a_b_c_d"
    assert sanitize_tool_name("  spaced  ") == "spaced"
    assert sanitize_tool_name("") == "tool"
    assert len(sanitize_tool_name("x" * 100)) == 64


def test_tool_name_sanitized_in_request_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    tools = [ToolSpec(name="octopus_status_Appriver Octopus", description="d", input_schema={"type": "object"})]
    prov.chat([{"role": "user", "content": "hi"}], tools=tools)
    assert captured["body"]["tools"][0]["function"]["name"] == "octopus_status_Appriver_Octopus"


# --------------------------------------------------------------- transient-error retry

def test_retries_transient_status_then_succeeds(monkeypatch):
    monkeypatch.setattr(litellm_provider.time, "sleep", lambda *_: None)
    statuses = [503, 401, 200]  # flaky broker: two blips then success

    def handler(request: httpx.Request) -> httpx.Response:
        code = statuses.pop(0)
        if code != 200:
            return httpx.Response(code, json={"error": "transient"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "recovered"}}]})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    result = prov.chat([{"role": "user", "content": "hi"}])
    assert result.text == "recovered"
    assert statuses == []  # all three attempts consumed


def test_retries_exhaust_then_raise(monkeypatch):
    monkeypatch.setattr(litellm_provider.time, "sleep", lambda *_: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(500, json={"error": "down"})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    try:
        prov.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError after exhausting retries"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 500
    assert attempts["n"] == litellm_provider._MAX_ATTEMPTS  # bounded, not infinite


def test_non_retriable_status_raises_immediately(monkeypatch):
    monkeypatch.setattr(litellm_provider.time, "sleep", lambda *_: None)
    attempts = {"n": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        attempts["n"] += 1
        return httpx.Response(400, json={"error": "bad request"})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    try:
        prov.chat([{"role": "user", "content": "hi"}])
        assert False, "expected HTTPStatusError"
    except httpx.HTTPStatusError as exc:
        assert exc.response.status_code == 400
    assert attempts["n"] == 1  # 400 is a real client error — no retry


# --------------------------------------------------------------- reasoning round-trip

def test_to_wire_roundtrips_reasoning_on_tool_call_turns_only():
    messages = [
        {"role": "user", "content": "q"},
        {
            "role": "assistant", "content": "", "reasoning": "plan: call echo",
            "tool_calls": [ToolCall(id="c1", name="echo", input={})],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "echo", "content": "out"},
        # A finished turn's reasoning is NOT sent back (Harmony drops prior-turn CoT).
        {"role": "assistant", "content": "final", "reasoning": "leftover"},
    ]
    wire = LiteLLMProvider._to_wire(messages, None)
    assert wire[1]["reasoning_content"] == "plan: call echo"
    assert "reasoning_content" not in wire[3]


def test_agent_attaches_reasoning_to_tool_call_history():
    from quickjoiner.agent.agent import OnboardingAgent
    from quickjoiner.llm.base import ChatResult
    from tests.test_agent_loop import ScriptedProvider, _echo_tool

    provider = ScriptedProvider([
        ChatResult(text="", thinking="plan: echo it",
                   tool_calls=[ToolCall(id="c1", name="echo", input={})]),
        ChatResult(text="done"),
    ])
    agent = OnboardingAgent(provider, [_echo_tool()], system="s")
    _, history = agent.ask("q")
    tool_turn = [m for m in history if m["role"] == "assistant" and m.get("tool_calls")][0]
    assert tool_turn["reasoning"] == "plan: echo it"
    # The final (no-tool-call) assistant message never carries reasoning.
    assert "reasoning" not in history[-1]


def test_sessions_strip_reasoning_on_persist():
    from quickjoiner.sessions import messages_to_json

    raw = messages_to_json([
        {"role": "assistant", "content": "", "reasoning": "cot",
         "tool_calls": [ToolCall(id="c1", name="echo", input={})]},
    ])
    assert "reasoning" not in json.loads(raw)[0]
    assert "cot" not in raw


# --------------------------------------------------------------- payload knobs

def test_reasoning_effort_and_parallel_tool_calls_in_payload():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    prov = LiteLLMProvider(
        _cfg(reasoning_effort="high", parallel_tool_calls=False),
        transport=httpx.MockTransport(handler),
    )
    tools = [ToolSpec(name="echo", description="d", input_schema={"type": "object"})]
    prov.chat([{"role": "user", "content": "hi"}], tools=tools)
    assert captured["body"]["reasoning_effort"] == "high"
    assert captured["body"]["parallel_tool_calls"] is False


def test_knobs_omitted_by_default():
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    tools = [ToolSpec(name="echo", description="d", input_schema={"type": "object"})]
    prov.chat([{"role": "user", "content": "hi"}], tools=tools)
    body = captured["body"]
    assert "reasoning_effort" not in body
    assert "parallel_tool_calls" not in body
    assert "stream_options" not in body  # non-streaming request


def test_proxy_cache_control_marks_system_as_content_parts():
    wire = LiteLLMProvider._to_wire([{"role": "user", "content": "q"}], "SYS", cache_system=True)
    assert wire[0] == {
        "role": "system",
        "content": [{"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}],
    }
    # Off by default: plain string, byte-identical to before.
    assert LiteLLMProvider._to_wire([], "SYS")[0] == {"role": "system", "content": "SYS"}


# --------------------------------------------------------------- streaming usage

def test_stream_requests_usage_and_parses_final_usage_chunk():
    chunks = [
        {"choices": [{"delta": {"content": "Hi"}}]},
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
        # include_usage final chunk: empty choices, usage only — must not crash.
        {"choices": [], "usage": {"prompt_tokens": 900, "completion_tokens": 5,
                                  "prompt_tokens_details": {"cached_tokens": 800}}},
    ]
    body = "".join(f"data: {json.dumps(c)}\n\n" for c in chunks) + "data: [DONE]\n\n"
    captured = {}

    def handler(request: httpx.Request) -> httpx.Response:
        captured["body"] = json.loads(request.content)
        return httpx.Response(200, content=body.encode(), headers={"content-type": "text/event-stream"})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    result = prov.chat([{"role": "user", "content": "hi"}], on_stream=lambda k, d: None)
    assert captured["body"]["stream_options"] == {"include_usage": True}
    assert result.text == "Hi" and result.stop_reason == "stop"


# --------------------------------------------------------------- strip-and-retry on 400

def test_strip_rejected_removes_named_optional_params():
    payload = {"model": "m", "messages": [], "max_tokens": 100,
               "stream_options": {"include_usage": True}, "reasoning_effort": "high"}
    assert _strip_rejected(payload, "unknown parameter: stream_options") == "stream_options"
    assert "stream_options" not in payload
    assert _strip_rejected(payload, "max_tokens must be at least 1, got -86016") == "max_tokens"
    assert "max_tokens" not in payload
    assert _strip_rejected(payload, "some unrelated validation error") is None
    assert payload["reasoning_effort"] == "high"  # untouched — not named by the error


def test_strip_rejected_removes_message_level_reasoning_and_cache_parts():
    payload = {
        "model": "m",
        "messages": [
            {"role": "system", "content": [
                {"type": "text", "text": "SYS", "cache_control": {"type": "ephemeral"}}]},
            {"role": "assistant", "content": None, "reasoning_content": "cot"},
        ],
    }
    assert _strip_rejected(payload, "reasoning_content is not permitted") == "reasoning_content"
    assert "reasoning_content" not in payload["messages"][1]
    assert _strip_rejected(payload, "extra field cache_control") == "cache_control"
    assert payload["messages"][0]["content"] == "SYS"  # flattened back to a string


def test_400_naming_optional_param_is_stripped_and_retried(monkeypatch):
    monkeypatch.setattr(litellm_provider.time, "sleep", lambda *_: None)
    bodies = []

    def handler(request: httpx.Request) -> httpx.Response:
        bodies.append(json.loads(request.content))
        if len(bodies) == 1:
            return httpx.Response(400, json={"error": "unsupported parameter: reasoning_effort"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    prov = LiteLLMProvider(_cfg(reasoning_effort="high"), transport=httpx.MockTransport(handler))
    result = prov.chat([{"role": "user", "content": "hi"}])
    assert result.text == "ok"
    assert "reasoning_effort" in bodies[0] and "reasoning_effort" not in bodies[1]


# --------------------------------------------------------------- Retry-After

def test_retry_delay_honors_retry_after_within_cap():
    assert _retry_delay(0) == 0.5
    assert _retry_delay(1) == 1.0
    assert _retry_delay(0, "7") == 7.0            # server's ask wins when larger
    assert _retry_delay(4, "1") == 8.0            # backoff wins when larger
    assert _retry_delay(0, "9999") == 30.0        # capped
    assert _retry_delay(0, "Wed, 21 Oct") == 0.5  # HTTP-date form ignored


def test_retry_after_header_used_on_429(monkeypatch):
    sleeps = []
    monkeypatch.setattr(litellm_provider.time, "sleep", lambda s: sleeps.append(s))
    statuses = [429, 200]

    def handler(request: httpx.Request) -> httpx.Response:
        code = statuses.pop(0)
        if code != 200:
            return httpx.Response(code, json={"error": "slow down"}, headers={"retry-after": "3"})
        return httpx.Response(200, json={"choices": [{"message": {"content": "ok"}}]})

    prov = LiteLLMProvider(_cfg(), transport=httpx.MockTransport(handler))
    assert prov.chat([{"role": "user", "content": "hi"}]).text == "ok"
    assert sleeps == [3.0]
