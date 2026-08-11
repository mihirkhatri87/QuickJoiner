"""AppContext.build_agent's system-prompt composition: date grounding + skills/session
framing stack correctly and in the right order relative to each other."""

from __future__ import annotations

import re
from datetime import datetime, timezone

import pytest

from tests.conftest import FakeEmbedder


def _build_ctx(tmp_path, monkeypatch):
    import quickjoiner.app as app_module

    monkeypatch.setattr(app_module, "create_embedder", lambda cfg: FakeEmbedder())
    ctx = app_module.build_context(tmp_path / "ws")
    monkeypatch.setattr(ctx, "build_provider", lambda *a, **k: object())
    monkeypatch.setattr(ctx, "connector_tools", lambda sources=None: [])
    return ctx


def test_system_prompt_states_the_real_current_date(tmp_path, monkeypatch):
    """Without this the agent has no ground truth for "today" and invents one — observed
    live, a single tool-calling turn resolving "Friday" produced four different guesses."""
    ctx = _build_ctx(tmp_path, monkeypatch)
    agent = ctx.build_agent()
    now = datetime.now(timezone.utc)
    assert now.strftime("%Y-%m-%d") in agent._system
    assert now.strftime("%A") in agent._system


def test_current_date_line_carries_no_time_of_day():
    """Date-only is deliberate: Anthropic prompt caching hashes the whole system+tools
    prefix as one unit, so a per-minute clock would miss cache on every follow-up turn
    of every conversation. A per-day grain costs at most one miss every 24h."""
    from quickjoiner.app import _current_date_line

    line = _current_date_line()
    assert not re.search(r"\d{1,2}:\d{2}", line)  # no HH:MM — the whole point of the test


def test_extra_system_still_lands_after_the_date_and_skills_framing(tmp_path, monkeypatch):
    ctx = _build_ctx(tmp_path, monkeypatch)
    agent = ctx.build_agent(extra_system="PROJECT: onboarding sprint planning")
    assert agent._system.index("Current date:") < agent._system.index(
        "PROJECT: onboarding sprint planning")


def test_the_date_line_points_at_the_tool_rather_than_inviting_arithmetic(tmp_path, monkeypatch):
    """Knowing today fixes the anchor, not the arithmetic — and the arithmetic is where
    the silent off-by-a-week errors are."""
    ctx = _build_ctx(tmp_path, monkeypatch)
    assert "resolve_dates" in ctx.build_agent()._system


def test_resolve_dates_is_offered_and_honours_the_workspace_timezone(tmp_path, monkeypatch):
    from quickjoiner.agent.dates import load_timezone

    ctx = _build_ctx(tmp_path, monkeypatch)
    ctx.config.chat.timezone = "America/Chicago"
    agent = ctx.build_agent()
    assert "resolve_dates" in agent._tools

    out = agent._tools["resolve_dates"].fn(phrase="yesterday")
    if load_timezone("America/Chicago") is timezone.utc:
        pytest.skip("no tz database on this host")
    assert "America/Chicago" in out
    assert "T05:00:00.000Z" in out or "T06:00:00.000Z" in out   # CDT / CST offset


def test_an_unresolvable_phrase_tells_the_agent_to_ask_rather_than_guess(tmp_path, monkeypatch):
    ctx = _build_ctx(tmp_path, monkeypatch)
    out = ctx.build_agent()._tools["resolve_dates"].fn(phrase="a while back")
    assert "Do not guess" in out
