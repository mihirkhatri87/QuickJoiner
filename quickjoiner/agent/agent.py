"""Tool-calling agent loop over the configured LLM provider."""

from __future__ import annotations

import json
from typing import Callable

from quickjoiner.llm.base import AgentTool, LLMProvider, Message

MAX_TOOL_ROUNDS = 10

EventCallback = Callable[[str, str], None]
# Event types emitted to on_event:
#   "thinking"   - model reasoning delta (streamed)
#   "delta"      - answer text delta (streamed)
#   "tool_call"  - the agent is invoking a tool (detail = tool name)
#   "candidates" - JSON list of validated multi-angle candidate answers (plan 06 §C);
#                  only fires when the final text carried a valid ```candidates block


class OnboardingAgent:
    def __init__(
        self,
        provider: LLMProvider,
        tools: list[AgentTool],
        system: str,
        tool_result_max_chars: int = 24000,
        score_ledger: dict[str, float] | None = None,
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
        # Per-request evidence-ref -> confidence map the graph tools populate as they
        # score chains; candidate answers read their displayed confidence from HERE
        # (server-computed), never from the model's self-reported number.
        self._score_ledger = score_ledger

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

        on_stream = None
        if on_event:
            on_stream = lambda kind, delta: on_event("delta" if kind == "text" else "thinking", delta)

        for _ in range(MAX_TOOL_ROUNDS):
            result = self._provider.chat(
                messages, system=self._system, tools=specs, on_stream=on_stream
            )
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
                    on_event("tool_call", call.name)
                tool = self._tools.get(call.name)
                if tool is None:
                    output = f"Error: unknown tool {call.name!r}"
                else:
                    try:
                        output = tool.run(**call.input)
                    except Exception as exc:
                        output = f"Error running {call.name}: {exc}"
                output = self._cap(output)
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
            if final.text and final.text.strip():
                messages.append({"role": "assistant", "content": final.text})
                return self._finalize(final.text, messages, on_event), messages
        except Exception:
            pass
        fallback = "I wasn't able to finish answering that from the sources I have."
        messages.append({"role": "assistant", "content": fallback})
        return fallback, messages

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

    def _cap(self, output: str) -> str:
        limit = self._tool_result_max_chars
        if limit and len(output) > limit:
            return output[:limit] + "\n…[tool output truncated]"
        return output
