"""LiteLLM (OpenAI-compatible) provider: URL construction, neutral<->wire translation,
streamed tool-call accumulation, lenient argument parsing, and end-to-end request/response
via httpx.MockTransport (no live proxy needed).
"""

from __future__ import annotations

import json

import httpx

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import ToolCall, ToolSpec
from quickjoiner.llm.litellm_provider import (
    LiteLLMProvider,
    _parse_arguments,
    _sse_json,
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
