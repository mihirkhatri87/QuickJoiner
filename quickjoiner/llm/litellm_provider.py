"""LiteLLM provider — speaks the OpenAI Chat Completions wire format over HTTP.

LiteLLM's proxy server exposes an OpenAI-compatible `/chat/completions` endpoint
that fronts 100+ model backends (OpenAI, Anthropic, Azure, Bedrock, vLLM, local…),
so pointing this provider at a LiteLLM proxy `base_url` unlocks any of them through
one config. It also works with any other OpenAI-compatible endpoint.

Supports SSE token streaming (on_stream), tool calling, and reasoning passthrough
(OpenAI-compatible endpoints that emit `reasoning_content` — e.g. via LiteLLM's
normalization — surface it on the "thinking" stream).

gpt-oss / reasoning-model enhancements:
- **Reasoning round-trip**: Harmony-format models (gpt-oss) expect the chain of
  thought that produced a tool call to be passed back (`reasoning_content` on the
  assistant message) until the turn completes; assistant history messages carrying
  a `reasoning` field are re-emitted that way. The agent attaches it on tool-call
  rounds; sessions strip it on persist (live-turn plumbing, not stored state).
- **`llm.reasoning_effort`** ("low"|"medium"|"high") is forwarded when set.
- **Usage visibility**: `stream_options.include_usage` is requested on streams and
  usage (incl. `prompt_tokens_details.cached_tokens` — the automatic-prefix-cache
  hit counter on vLLM-style backends) is logged at DEBUG on every response.
- **Strip-and-retry on 400**: optional params some backends reject
  (`stream_options`, `reasoning_effort`, `parallel_tool_calls`, message-level
  `reasoning_content`, content-part `cache_control`) — and a `max_tokens` the
  server computes as impossible — are removed and the request retried once each,
  so an enhancement can never hard-break a stricter backend.

The wire translation lives in pure module-level functions (`_to_wire`,
`accumulate_delta`, `_tool_calls_from_*`, `_strip_rejected`, `_retry_delay`) so it
is unit-tested without a live server; `httpx` I/O is done through an injectable
transport for MockTransport tests. One pooled `httpx.Client` is reused across
requests, so a 10-round tool loop pays one TCP/TLS handshake, not ten.
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any

import httpx

from quickjoiner.config import LLMConfig
from quickjoiner.llm.base import (
    ChatResult, LLMProvider, Message, StreamCallback, TokenUsage, ToolCall, ToolSpec,
)

logger = logging.getLogger(__name__)

# Transient HTTP statuses worth retrying. 5xx/429 are the usual gateway blips; 401 is
# included because some fronting proxies (e.g. an overloaded internal LiteLLM/model
# broker) intermittently reject a *valid* static key under load — observed live as the
# same key alternating 200/401 within seconds. Retries are capped, so a genuinely bad
# key still surfaces its 401, just after a few bounded backoffs.
_RETRY_STATUSES = frozenset({401, 429, 500, 502, 503, 504})
_MAX_ATTEMPTS = 4
_BACKOFF_BASE = 0.5  # seconds; exponential: 0.5, 1.0, 2.0 …
_RETRY_AFTER_CAP = 30.0  # never honor a Retry-After longer than this

# Top-level payload keys a stricter backend may reject; strippable on a 400 that
# names them. max_tokens is here for the observed "max_tokens must be at least 1,
# got -86016" failure mode: an overlong prompt makes the proxy compute a negative
# completion budget — dropping the key lets the server pick its own default.
_OPTIONAL_KEYS = ("stream_options", "reasoning_effort", "parallel_tool_calls", "max_tokens")


def _parse_arguments(raw: Any) -> dict[str, Any]:
    """OpenAI tool-call arguments arrive as a JSON string (or already a dict in some
    proxies). Parse leniently — a malformed/partial payload yields an empty dict rather
    than crashing the tool loop."""
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except (json.JSONDecodeError, TypeError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _tool_calls_from_message(message: dict[str, Any]) -> list[ToolCall]:
    """Build ToolCalls from a non-streamed assistant message."""
    calls: list[ToolCall] = []
    for i, tc in enumerate(message.get("tool_calls") or []):
        fn = tc.get("function") or {}
        name = fn.get("name")
        if not name:
            continue
        calls.append(
            ToolCall(id=tc.get("id") or f"call_{i}", name=name, input=_parse_arguments(fn.get("arguments")))
        )
    return calls


def _tool_calls_from_acc(slots: dict[int, dict[str, Any]]) -> list[ToolCall]:
    """Build ToolCalls from streamed deltas accumulated by index."""
    calls: list[ToolCall] = []
    for idx, slot in sorted(slots.items()):
        if not slot.get("name"):
            continue
        calls.append(
            ToolCall(id=slot.get("id") or f"call_{idx}", name=slot["name"], input=_parse_arguments(slot.get("arguments")))
        )
    return calls


def accumulate_delta(delta: dict[str, Any], acc: dict[str, Any], on_stream: StreamCallback | None) -> None:
    """Fold one streamed choice `delta` into the accumulator (pure; testable).

    Tool calls stream as fragments keyed by `index`: the id/name land in the first
    fragment and `arguments` is concatenated across fragments, so we merge by index.
    """
    content = delta.get("content")
    if content:
        acc["text"] += content
        if on_stream:
            on_stream("text", content)
    reasoning = delta.get("reasoning_content")
    if reasoning:
        acc["thinking"] += reasoning
        if on_stream:
            on_stream("thinking", reasoning)
    for tc in delta.get("tool_calls") or []:
        idx = tc.get("index", 0)
        slot = acc["tool_calls"].setdefault(idx, {"id": None, "name": None, "arguments": ""})
        if tc.get("id"):
            slot["id"] = tc["id"]
        fn = tc.get("function") or {}
        if fn.get("name"):
            slot["name"] = fn["name"]
        if fn.get("arguments"):
            slot["arguments"] += fn["arguments"]


def _sse_json(line: str) -> dict[str, Any] | None:
    """Decode one Server-Sent-Events line into JSON, skipping keep-alives and the
    terminal `data: [DONE]` sentinel."""
    line = line.strip()
    if not line.startswith("data:"):
        return None
    data = line[len("data:"):].strip()
    if not data or data == "[DONE]":
        return None
    try:
        return json.loads(data)
    except json.JSONDecodeError:
        return None


def _strip_rejected(payload: dict[str, Any], error_body: str) -> str | None:
    """On a 400, remove the optional enhancement the error names (pure; testable).

    Returns the name of what was stripped so the caller can retry, or None when the
    400 is a genuine client error we must surface. One param is removed per call;
    payload keys shrink monotonically, so retries are bounded by _MAX_ATTEMPTS.
    """
    for key in _OPTIONAL_KEYS:
        if key in payload and key in error_body:
            payload.pop(key)
            return key
    messages = payload.get("messages") or []
    # Message-level reasoning round-trip (Harmony): strip from every assistant entry.
    if "reasoning_content" in error_body and any("reasoning_content" in m for m in messages):
        for m in messages:
            m.pop("reasoning_content", None)
        return "reasoning_content"
    # Content-part cache markers: flatten parts lists back to plain strings.
    if "cache_control" in error_body:
        flattened = False
        for m in messages:
            if isinstance(m.get("content"), list):
                m["content"] = "".join(p.get("text", "") for p in m["content"])
                flattened = True
        if flattened:
            return "cache_control"
    return None


def _retry_delay(attempt: int, retry_after: str | None = None) -> float:
    """Exponential backoff, raised to the server's Retry-After when it's a larger,
    parseable number of seconds (HTTP-date forms are ignored), capped for sanity."""
    delay = _BACKOFF_BASE * (2 ** attempt)
    if retry_after:
        try:
            delay = max(delay, min(float(retry_after), _RETRY_AFTER_CAP))
        except ValueError:
            pass
    return delay


def _token_usage(usage: dict[str, Any] | None) -> TokenUsage:
    """Map an OpenAI-compatible usage block onto the neutral TokenUsage (pure).

    cached = prompt_tokens_details.cached_tokens — the automatic-prefix-cache hit
    counter on vLLM-style backends; if it stays 0 across a tool loop the prompt prefix
    is not byte-stable (or the server has caching off). Absent/garbage fields count as
    0 rather than raising: usage is observability, and must never fail a real answer.
    """
    if not usage:
        return TokenUsage()

    def _int(value: Any) -> int:
        try:
            return int(value or 0)
        except (TypeError, ValueError):
            return 0

    details = usage.get("prompt_tokens_details") or {}
    return TokenUsage(
        prompt=_int(usage.get("prompt_tokens")),
        completion=_int(usage.get("completion_tokens")),
        cached=_int(details.get("cached_tokens")),
    )


def _log_usage(usage: dict[str, Any] | None) -> TokenUsage:
    """DEBUG-log token usage and return it in neutral form."""
    tokens = _token_usage(usage)
    if usage:
        logger.debug(
            "litellm usage: prompt=%s completion=%s cached=%s",
            tokens.prompt, tokens.completion, tokens.cached,
        )
    return tokens


class LiteLLMProvider(LLMProvider):
    name = "litellm"

    def __init__(self, config: LLMConfig, transport: httpx.BaseTransport | None = None):
        base = config.base_url.rstrip("/")
        # Accept a bare proxy URL, a `/v1` base, or a full endpoint interchangeably.
        self._url = base if base.endswith("/chat/completions") else base + "/chat/completions"
        self._model = config.resolved_model()
        self._max_tokens = config.max_tokens
        self._reasoning_effort = config.reasoning_effort
        self._parallel_tool_calls = config.parallel_tool_calls
        self._cache_control = config.proxy_cache_control
        key = os.environ.get(config.api_key_env) if config.api_key_env else None
        self._headers = {"Authorization": f"Bearer {key}"} if key else {}
        # One pooled client for the provider's lifetime (httpx.Client is thread-safe):
        # a 10-round tool loop pays one TCP/TLS handshake instead of ten.
        self._http = (
            httpx.Client(transport=transport, timeout=300.0)
            if transport is not None
            else httpx.Client(timeout=300.0)
        )

    def chat(
        self,
        messages: list[Message],
        system: str | None = None,
        tools: list[ToolSpec] | None = None,
        on_stream: StreamCallback | None = None,
    ) -> ChatResult:
        payload: dict[str, Any] = {
            "model": self._model,
            "messages": self._to_wire(messages, system, cache_system=self._cache_control),
            "max_tokens": self._max_tokens,
            "stream": on_stream is not None,
        }
        if on_stream is not None:
            # Ask for the final usage chunk so cache hits/token spend are observable.
            payload["stream_options"] = {"include_usage": True}
        if self._reasoning_effort:
            payload["reasoning_effort"] = self._reasoning_effort
        if tools:
            payload["tools"] = [
                {
                    "type": "function",
                    "function": {
                        "name": t.name,
                        "description": t.description,
                        "parameters": t.input_schema,
                    },
                }
                for t in tools
            ]
            if self._parallel_tool_calls is not None:
                payload["parallel_tool_calls"] = self._parallel_tool_calls
        if on_stream is not None:
            return self._chat_stream(payload, on_stream)
        return self._chat_once(payload)

    def _post_json(self, payload: dict[str, Any]) -> dict[str, Any]:
        """POST the (non-streamed) request, retrying transient failures with backoff
        and stripping optional params a stricter backend rejects with a 400."""
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                resp = self._http.post(self._url, json=payload, headers=self._headers)
            except httpx.TransportError:
                if last:
                    raise
                time.sleep(_retry_delay(attempt))
                continue
            if resp.status_code in _RETRY_STATUSES and not last:
                time.sleep(_retry_delay(attempt, resp.headers.get("retry-after")))
                continue
            if resp.status_code == 400 and not last:
                stripped = _strip_rejected(payload, resp.text)
                if stripped:
                    logger.warning("backend rejected %r; retrying without it", stripped)
                    continue  # immediate retry — not a transient fault
            resp.raise_for_status()
            return resp.json()
        raise RuntimeError("unreachable")  # loop always returns or raises

    def _chat_once(self, payload: dict[str, Any]) -> ChatResult:
        data = self._post_json(payload)
        tokens = _log_usage(data.get("usage"))
        choice = (data.get("choices") or [{}])[0]
        message = choice.get("message") or {}
        return ChatResult(
            text=message.get("content") or "",
            tool_calls=_tool_calls_from_message(message),
            thinking=message.get("reasoning_content") or "",
            stop_reason=choice.get("finish_reason"),
            usage=tokens,
        )

    def _chat_stream(self, payload: dict[str, Any], on_stream: StreamCallback) -> ChatResult:
        acc: dict[str, Any] = {"text": "", "thinking": "", "tool_calls": {}}
        finish: str | None = None
        usage: dict[str, Any] | None = None
        # Retry the connection + status check only; once tokens start flowing we are
        # committed to this response and propagate any mid-stream error.
        for attempt in range(_MAX_ATTEMPTS):
            last = attempt == _MAX_ATTEMPTS - 1
            try:
                with self._http.stream("POST", self._url, json=payload, headers=self._headers) as resp:
                    if resp.status_code in _RETRY_STATUSES and not last:
                        resp.read()  # drain so the connection can be reused/closed cleanly
                        time.sleep(_retry_delay(attempt, resp.headers.get("retry-after")))
                        continue
                    if resp.status_code == 400 and not last:
                        resp.read()
                        stripped = _strip_rejected(payload, resp.text)
                        if stripped:
                            logger.warning("backend rejected %r; retrying without it", stripped)
                            continue
                    resp.raise_for_status()
                    for line in resp.iter_lines():
                        data = _sse_json(line)
                        if data is None:
                            continue
                        if data.get("usage"):
                            usage = data["usage"]  # include_usage final chunk
                        choice = (data.get("choices") or [{}])[0]
                        accumulate_delta(choice.get("delta") or {}, acc, on_stream)
                        if choice.get("finish_reason"):
                            finish = choice["finish_reason"]
            except httpx.TransportError:
                # If tokens already reached the caller, retrying would duplicate the
                # visible stream — we are committed, so propagate.
                committed = acc["text"] or acc["thinking"] or acc["tool_calls"]
                if last or committed:
                    raise
                time.sleep(_retry_delay(attempt))
                continue
            break
        return ChatResult(
            text=acc["text"],
            tool_calls=_tool_calls_from_acc(acc["tool_calls"]),
            thinking=acc["thinking"],
            stop_reason=finish,
            usage=_log_usage(usage),
        )

    @staticmethod
    def _to_wire(
        messages: list[Message], system: str | None, cache_system: bool = False
    ) -> list[dict[str, Any]]:
        wire: list[dict[str, Any]] = []
        if system:
            if cache_system:
                # Anthropic-style cache marker as a content-part extra; LiteLLM
                # forwards it when the proxy routes to Claude, most OpenAI-compatible
                # servers ignore it, and a strict backend's 400 is strip-and-retried.
                wire.append({
                    "role": "system",
                    "content": [
                        {"type": "text", "text": system, "cache_control": {"type": "ephemeral"}}
                    ],
                })
            else:
                wire.append({"role": "system", "content": system})
        for m in messages:
            role = m["role"]
            if role == "assistant":
                entry: dict[str, Any] = {"role": "assistant", "content": m.get("content") or ""}
                if m.get("tool_calls"):
                    entry["tool_calls"] = [
                        {
                            "id": tc.id,
                            "type": "function",
                            "function": {"name": tc.name, "arguments": json.dumps(tc.input)},
                        }
                        for tc in m["tool_calls"]
                    ]
                    # OpenAI permits (and prefers) null content alongside tool calls.
                    if not entry["content"]:
                        entry["content"] = None
                    # Harmony round-trip: gpt-oss-family models want the chain of
                    # thought that produced a tool call passed back until the turn
                    # completes. Only tool-call turns carry it — prior finished
                    # turns' reasoning is dropped by the chat template anyway.
                    if m.get("reasoning"):
                        entry["reasoning_content"] = m["reasoning"]
                wire.append(entry)
            elif role == "tool":
                # tool_call_id must match an id from the preceding assistant turn.
                wire.append(
                    {"role": "tool", "tool_call_id": m.get("tool_call_id", ""), "content": m["content"]}
                )
            else:
                wire.append({"role": "user", "content": m["content"]})
        return wire
