"""Relative-date resolution: the arithmetic a model gets right *most* of the time.

These pin the CONVENTIONS, not just the parsing — the failure mode this module exists to
prevent is a plausible-looking window off by a day or a week, which returns real rows from
the wrong period and cannot be detected downstream.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from quickjoiner.agent.dates import (
    MAX_ROLLING_DAYS, describe, load_timezone, resolve,
)

# Monday 2026-08-10 12:00Z. The prior Friday is 2026-08-07 — the day the live debugging
# session was actually asking about.
MONDAY = datetime(2026, 8, 10, 12, 0, tzinfo=timezone.utc)
FRIDAY = datetime(2026, 8, 7, 12, 0, tzinfo=timezone.utc)


def _days(phrase, now=MONDAY, tz=timezone.utc):
    r = resolve(phrase, now, tz)
    assert r is not None, f"{phrase!r} did not resolve"
    return r.start.date().isoformat(), r.end.date().isoformat()


# ------------------------------------------------------------------ named weekdays

def test_last_friday_from_a_monday_is_the_friday_three_days_back():
    assert _days("last friday") == ("2026-08-07", "2026-08-08")


def test_last_friday_ON_a_friday_is_seven_days_ago_not_today():
    """The single most likely off-by-a-week error, and it reads perfectly plausibly."""
    assert _days("last friday", now=FRIDAY) == ("2026-07-31", "2026-08-01")


def test_a_bare_weekday_includes_today_and_says_so():
    r = resolve("friday", FRIDAY, timezone.utc)
    assert r.start.date().isoformat() == "2026-08-07"
    assert "today included" in r.assumption


def test_this_friday_from_a_monday_is_forward_and_flagged_as_future():
    r = resolve("this friday", MONDAY, timezone.utc)
    assert r.start.date().isoformat() == "2026-08-14"
    assert r.is_future(MONDAY)
    assert "future" in describe("this friday", MONDAY)


@pytest.mark.parametrize("phrase, expected", [
    ("last monday", "2026-08-03"),      # today is Monday -> a week back
    ("last sunday", "2026-08-09"),      # yesterday
    ("last wed", "2026-08-05"),         # abbreviation
    ("next friday", "2026-08-14"),
])
def test_weekday_variants(phrase, expected):
    assert _days(phrase)[0] == expected


# ------------------------------------------------------------------ calendar vs rolling

def test_last_week_is_the_previous_calendar_week_and_names_the_alternative():
    r = resolve("last week", MONDAY, timezone.utc)
    assert (r.start.date().isoformat(), r.end.date().isoformat()) == ("2026-08-03", "2026-08-10")
    assert "CALENDAR" in r.assumption and "last 7 days" in r.assumption


def test_last_7_days_is_rolling_from_now_not_a_calendar_week():
    """The distinction the assumption text exists to make explicit."""
    r = resolve("last 7 days", MONDAY, timezone.utc)
    assert r.end == MONDAY                      # ends NOW, not at a midnight
    assert r.start == MONDAY - timedelta(days=7)
    assert "rolling" in r.assumption.lower()


def test_last_month_is_the_previous_calendar_month():
    assert _days("last month") == ("2026-07-01", "2026-08-01")


def test_last_30_days_is_rolling_and_differs_from_last_month():
    rolling = resolve("last 30 days", MONDAY, timezone.utc)
    calendar = resolve("last month", MONDAY, timezone.utc)
    assert rolling.start != calendar.start      # they are genuinely different windows


@pytest.mark.parametrize("phrase", ["past two weeks", "last 2 weeks", "previous 14 days"])
def test_number_words_and_synonyms_all_parse(phrase):
    assert resolve(phrase, MONDAY, timezone.utc) is not None


def test_three_days_ago_is_that_whole_day_not_a_rolling_72_hours():
    assert _days("3 days ago") == ("2026-08-07", "2026-08-08")


def test_last_hour_is_rolling_even_with_no_number():
    r = resolve("the last hour", MONDAY, timezone.utc)
    assert r.start == MONDAY - timedelta(hours=1) and r.end == MONDAY


# ------------------------------------------------------------------ weekends

def test_this_weekend_asked_midweek_means_the_one_just_gone():
    """A question about what happened cannot mean a window that has not happened."""
    r = resolve("this weekend", MONDAY, timezone.utc)
    assert (r.start.date().isoformat(), r.end.date().isoformat()) == ("2026-08-08", "2026-08-10")
    assert "BACKWARD" in r.assumption


def test_on_a_monday_this_and_last_weekend_are_the_same_one_just_gone():
    """Not a bug — it is how people speak. On a Monday both phrases mean the weekend that
    just ended, and resolving them differently would be the surprising behaviour."""
    assert _days("last weekend") == ("2026-08-08", "2026-08-10")
    assert _days("this weekend") == ("2026-08-08", "2026-08-10")


def test_asked_DURING_a_weekend_the_two_phrases_separate():
    sunday = datetime(2026, 8, 9, 12, tzinfo=timezone.utc)
    assert _days("this weekend", now=sunday) == ("2026-08-08", "2026-08-10")   # in progress
    assert _days("last weekend", now=sunday) == ("2026-08-01", "2026-08-03")   # the one before


def test_a_weekend_runs_saturday_to_monday_midnight():
    r = resolve("last weekend", MONDAY, timezone.utc)
    assert (r.end - r.start) == timedelta(days=2)


# ------------------------------------------------------------------ explicit dates

def test_an_iso_date_is_taken_literally_with_no_assumption():
    r = resolve("on 2026-08-07", MONDAY, timezone.utc)
    assert (r.start.date().isoformat(), r.end.date().isoformat()) == ("2026-08-07", "2026-08-08")
    assert r.assumption == ""


def test_two_iso_dates_are_an_inclusive_span():
    assert _days("between 2026-08-03 and 2026-08-05") == ("2026-08-03", "2026-08-06")


def test_a_reversed_span_is_ordered_rather_than_rejected():
    assert _days("2026-08-05 to 2026-08-03") == ("2026-08-03", "2026-08-06")


def test_an_impossible_date_resolves_to_nothing_rather_than_a_neighbouring_day():
    assert resolve("2026-02-30", MONDAY, timezone.utc) is None


# ------------------------------------------------------------------ timezone

def test_a_local_day_is_not_a_utc_day():
    """The reason this takes a timezone at all: a US-Central Friday starts at 05:00Z, and
    resolving it in UTC silently drops five hours of a working evening."""
    chicago = load_timezone("America/Chicago")
    if chicago is timezone.utc:
        pytest.skip("no tz database on this host")
    r = resolve("last friday", MONDAY, chicago)
    assert r.start.isoformat() == "2026-08-07T05:00:00+00:00"
    assert r.end.isoformat() == "2026-08-08T05:00:00+00:00"


def test_an_unknown_timezone_degrades_to_utc_rather_than_raising():
    assert load_timezone("Mars/Olympus_Mons") is timezone.utc
    assert "used UTC instead" in describe("last friday", MONDAY, "Mars/Olympus_Mons")


def test_a_dst_day_is_still_a_whole_local_day():
    """US DST ends 2026-11-01, making that local day 25 hours. Adding timedelta(days=1)
    to a UTC instant would end the window an hour early and drop real events."""
    chicago = load_timezone("America/Chicago")
    if chicago is timezone.utc:
        pytest.skip("no tz database on this host")
    r = resolve("2026-11-01", datetime(2026, 11, 3, 12, tzinfo=timezone.utc), chicago)
    assert (r.end - r.start) == timedelta(hours=25)


# ------------------------------------------------------------------ refusal & bounds

@pytest.mark.parametrize("phrase", ["", "   ", "recently", "a while back", "soonish", None])
def test_an_unparseable_phrase_returns_nothing_rather_than_a_guess(phrase):
    """Inventing a window for a vague phrase is exactly the guessing this removes."""
    assert resolve(phrase, MONDAY, timezone.utc) is None


def test_describe_tells_the_agent_not_to_guess_when_it_cannot_resolve():
    text = describe("sometime recently", MONDAY)
    assert "Do not guess" in text


def test_an_absurd_window_is_refused_rather_than_scanning_every_index():
    assert resolve(f"last {MAX_ROLLING_DAYS + 500} days", MONDAY, timezone.utc) is None


def test_describe_offers_the_minutes_back_form_some_log_tools_need():
    """grafana/datadog/dynatrace take minutes-back-from-now, not a range. Supplying it
    here closes that seam once, rather than making the model redo the arithmetic."""
    text = describe("last friday", MONDAY)
    assert "minutes=" in text
    assert "sweeps up everything since" in text   # honest that the window is wider


def test_a_window_ending_now_needs_no_widening_caveat():
    text = describe("last 30 days", MONDAY)
    assert "minutes=" in text
    assert "sweeps up everything since" not in text


def test_describe_emits_paste_ready_iso_and_surfaces_the_assumption():
    text = describe("last week", MONDAY)
    assert "2026-08-03T00:00:00.000Z" in text
    assert "2026-08-10T00:00:00.000Z" in text
    assert "ASSUMPTION:" in text and "State this assumption" in text
