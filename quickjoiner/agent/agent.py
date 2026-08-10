"""Tool-calling agent loop over the configured LLM provider."""

from __future__ import annotations

import json
from typing import Callable

from quickjoiner.llm.base import AgentTool, LLMProvider, Message, TokenUsage

MAX_TOOL_ROUNDS = 10

EventCallback = Callable[[str, str], None]
# Event types emitted to on_event:
#   "thinking"   - model reasoning delta (streamed)
#   "delta"      - answer text delta (streamed)
#   "tool_call"  - the agent is invoking a tool. JSON: {"id", "name", "args"} — the
#                  arguments are what make a trace readable ("searched for X", "read
#                  URL Y") rather than a bare list of function names.
#   "tool_result"- that call came back. JSON: {"id", "name", "ok", "summary", "chars"}.
#                  `summary` is a bounded PREVIEW, never the whole output: a single
#                  result can be 24k chars and there is no reason to push that down
#                  the wire twice. `chars` states the true size so a truncated preview
#                  is never mistaken for the whole answer.
#   "sources"    - JSON list of citable refs newly seen in a tool result
#                  ({"label", "uri", "kind"?, "score"?, "snippet"}). The tools already
#                  hand the MODEL a uri per hit; this is the same information reaching
#                  the CLIENT, so a citation of a readable title can resolve to the page
#                  it came from instead of being unlinkable. Additive per turn — a client
#                  merges each batch into what it already has.
#   "candidates" - JSON list of validated multi-angle candidate answers (plan 06 §C);
#                  only fires when the final text carried a valid ```candidates block

# Bounds for what rides the event stream. These govern DISPLAY only — the model still
# receives the full (or `_cap`-limited) tool output, so nothing here can change an answer.
_ARG_PREVIEW_CHARS = 400
_RESULT_PREVIEW_CHARS = 800


def _preview_args(args: dict) -> dict:
    """Bound each argument value for display. A `remember` fact or a long WIQL query
    can be arbitrarily large, and the trace only ever renders a line or two of it."""
    out = {}
    for key, value in (args or {}).items():
        text = value if isinstance(value, str) else json.dumps(value, default=str)
        if len(text) > _ARG_PREVIEW_CHARS:
            text = text[:_ARG_PREVIEW_CHARS] + "…"
        out[key] = text
    return out


class OnboardingAgent:
    def __init__(
        self,
        provider: LLMProvider,
        tools: list[AgentTool],
        system: str,
        tool_result_max_chars: int = 24000,
        score_ledger: dict[str, float] | None = None,
        uncapped_tools: frozenset[str] = frozenset(),
    ):
        self._provider = provider
        # Sorted by name so the tool-spec list is byte-stable across requests and
        # processes — prompt caching (explicit Anthropic cache_control, automatic
        # prefix caching on OpenAI-compatible backends, llama.cpp KV-cache reuse)
        # is a prefix match over tools -> system -> messages, and a reordered tool
        # list silently invalidates all of it.
        self._tools = {t.spec.name: t for t in sorted(tools, key=lambda t: t.spec.name)}
        self._system = system
        # Cap a single live tool result before feeding it back to the model, so an
        # unbounded connector tool (e.g. the whole Octopus dashboard) can't overflow
        # the context window and make the provider reject the next turn.
        self._tool_result_max_chars = tool_result_max_chars
        # Tools exempt from that cap: ones that already bound themselves to a small,
        # fixed shape (a relationship count, a hub sample, a handful of path chains)
        # with NO model-controllable size parameter, and that state their own
        # truncation explicitly in the text they return ("truncated at N — there are
        # more..."). Capping their output a second time by raw character count can
        # slice a complete, honestly-labelled result mid-list and silently drop real
        # data with no signal beyond a generic "[tool output truncated]" — the
        # user-reported "you missed some teams" bug, where graph_relations returned a
        # correct, untruncated 334-row list that the outer cap then chopped anyway.
        self._uncapped_tools = uncapped_tools
        # Per-request evidence-ref -> confidence map the graph tools populate as they
        # score chains; candidate answers read their displayed confidence from HERE
        # (server-computed), never from the model's self-reported number.
        self._score_ledger = score_ledger
        # What the most recent ask() cost: token usage summed over every round of the
        # turn, and how many model rounds it took. An agent is built per request, so
        # this is per-turn state, not shared. Read by `qj bench`; nothing depends on it,
        # and a provider that reports no usage simply leaves the counts at zero.
        self.last_usage = TokenUsage()
        self.last_rounds = 0

    def ask(
        self,
        question: str,
        history: list[Message] | None = None,
        on_event: EventCallback | None = None,
    ) -> tuple[str, list[Message]]:
        """Run one user turn to completion. Returns (answer, updated history)."""
        messages: list[Message] = list(history or [])
        messages.append({"role": "user", "content": question})
        specs = [t.spec for t in self._tools.values()]
        self.last_usage, self.last_rounds = TokenUsage(), 0
        # Refs already announced this turn, so repeated searches don't re-send them.
        self._seen_refs: set[tuple[str, str]] = set()

        on_stream = None
        if on_event:
            on_stream = lambda kind, delta: on_event("delta" if kind == "text" else "thinking", delta)

        for _ in range(MAX_TOOL_ROUNDS):
            result = self._provider.chat(
                messages, system=self._system, tools=specs, on_stream=on_stream
            )
            self._account(result)
            if not result.tool_calls:
                if result.text and result.text.strip():
                    messages.append({"role": "assistant", "content": result.text})
                    return self._finalize(result.text, messages, on_event), messages
                # No tool call AND no text — a reasoning model (gpt-oss) that emitted only a
                # thinking channel, or an empty completion. Returning this blank is the "qj
                # ended with no response" bug; fall through to a final tool-free turn that
                # forces a real answer or an honest refusal, never nothing.
                break

            assistant: Message = {
                "role": "assistant", "content": result.text, "tool_calls": result.tool_calls,
            }
            if result.thinking_blocks:
                # Signed thinking blocks must ride along so the provider can echo
                # them back on the next round of this tool-use turn.
                assistant["thinking_blocks"] = result.thinking_blocks
            if result.thinking:
                # Harmony-format reasoning models (gpt-oss) want the chain of thought
                # that produced a tool call passed back until the turn completes; the
                # LiteLLM provider re-emits it as reasoning_content on tool-call turns.
                # Live-turn plumbing only — sessions strip it on persist.
                assistant["reasoning"] = result.thinking
            messages.append(assistant)
            for call in result.tool_calls:
                if on_event:
                    on_event("tool_call", json.dumps(
                        {"id": call.id, "name": call.name, "args": _preview_args(call.input)}))
                tool = self._tools.get(call.name)
                ok = True
                if tool is None:
                    ok = False
                    output = f"Error: unknown tool {call.name!r}"
                else:
                    try:
                        output = tool.run(**call.input)
                    except Exception as exc:
                        ok = False
                        output = f"Error running {call.name}: {exc}"
                output = self._cap(output, call.name)
                if on_event:
                    on_event("tool_result", json.dumps({
                        "id": call.id, "name": call.name, "ok": ok,
                        "summary": output[:_RESULT_PREVIEW_CHARS],
                        "chars": len(output),
                    }))
                    self._emit_sources(output, on_event)
                messages.append(
                    {
                        "role": "tool",
                        "tool_call_id": call.id,
                        "name": call.name,
                        "content": output,
                    }
                )

        # Tool-call budget exhausted (often a model looping on searches for something
        # that isn't in memory). Make one final tool-free turn so it must answer — or
        # properly refuse ("I haven't learned that yet") — from what it has gathered,
        # instead of emitting an unhelpful canned message.
        try:
            final = self._provider.chat(messages, system=self._system, tools=None, on_stream=on_stream)
            self._account(final)
            if final.text and final.text.strip():
                messages.append({"role": "assistant", "content": final.text})
                return self._finalize(final.text, messages, on_event), messages
        except Exception:
            pass
        fallback = "I wasn't able to finish answering that from the sources I have."
        messages.append({"role": "assistant", "content": fallback})
        return fallback, messages

    def _emit_sources(self, output: str, on_event: EventCallback) -> None:
        """Forward citable refs this tool result introduced, so the client can turn a
        cited title into a link. Only the NEW ones each time — the same document is
        returned by round after round of searching, and re-sending it would grow the
        stream quadratically for no gain. Best-effort: a parse failure costs link
        resolution, never the answer, so it can never propagate."""
        try:
            from quickjoiner.agent.refs import parse_source_refs
            from quickjoiner.agent.weblinks import citable_link

            fresh = []
            for ref in parse_source_refs(output):
                key = (ref["label"], ref["uri"])
                if key in self._seen_refs:
                    continue
                self._seen_refs.add(key)
                # `uri` is identity and stays as the tools reported it; `link` is where a
                # citation should actually open — different things for a cloned repo file,
                # and absent entirely for a local path or a distilled conversation.
                link = citable_link(ref["uri"])
                if link:
                    ref["link"] = link
                fresh.append(ref)
            if fresh:
                on_event("sources", json.dumps(fresh))
        except Exception:
            pass

    def _account(self, result) -> None:
        """Add one round's token usage to this turn's running total (bench visibility)."""
        u = result.usage
        self.last_rounds += 1
        self.last_usage = TokenUsage(
            prompt=self.last_usage.prompt + u.prompt,
            completion=self.last_usage.completion + u.completion,
            cached=self.last_usage.cached + u.cached,
            cache_write=self.last_usage.cache_write + u.cache_write,
        )

    def _finalize(self, text: str, messages: list[Message],
                  on_event: EventCallback | None) -> str:
        """Post-process the final assistant turn (plan 06 §C): if it carries a valid
        ```candidates block whose candidates all resolve to citations actually
        returned by tools this turn, emit a `candidates` event and return the prose
        with the block stripped. Anything else — no block, all-invalid block, no
        event channel to deliver on, or ANY exception — returns the text untouched:
        content is never lost, and nothing here can escape into the SSE stream.
        History (already appended by the caller) keeps the model's full raw text."""
        try:
            from quickjoiner.agent.candidates import (
                attach_confidence, filter_resolvable, known_refs, parse_candidates,
            )

            prose, cands = parse_candidates(text)
            if not cands or on_event is None:
                return text
            survivors = filter_resolvable(cands, known_refs(messages))
            if not survivors:
                return text
            survivors = attach_confidence(survivors, known_refs(messages),
                                          self._score_ledger or {})
            on_event("candidates", json.dumps([
                {"rank": c.rank, "summary": c.summary, "confidence": c.confidence,
                 "sources": list(c.sources)}
                for c in survivors
            ]))
            return prose
        except Exception:
            return text

    def _cap(self, output: str, tool_name: str) -> str:
        if tool_name in self._uncapped_tools:
            return output
        limit = self._tool_result_max_chars
        if limit and len(output) > limit:
            return output[:limit] + "\n…[tool output truncated]"
        return output
