"""Relative dates, resolved deterministically instead of guessed.

Telling the agent today's date (see `app._current_date_line`) fixes the *anchor*. It does
not fix the arithmetic, and the arithmetic is where the silent errors live: "last Friday"
on a Friday, whether "last week" is the previous calendar week or a rolling seven days,
whether "this weekend" on a Tuesday means the one just gone or the one coming. A model
asked to work those out in prose gets them right most of the time, which is the worst
possible rate — a wrong window returns real, well-formed, confidently-cited rows from the
wrong day, and nothing downstream can detect it.

So this module does the arithmetic, and every answer carries three things:

  * an exact half-open UTC range, `[start, end)`, ready to paste into a query;
  * a **label** naming what it resolved to in words ("Friday 2026-08-07"), so the agent
    can state the window it actually searched rather than echoing the user's phrase back;
  * an **assumption**, non-empty exactly when the phrase was genuinely ambiguous and a
    convention had to be applied. Stating it is the point: the user can correct a
    convention they can see, and cannot correct one they cannot.

Conventions, all deliberate:

  * Days are **calendar days in the caller's timezone**, converted to UTC. A US-Central
    "Friday" is 05:00Z Friday to 05:00Z Saturday; resolving it in UTC would silently drop
    five hours of a working evening.
  * Weeks start **Monday** (ISO 8601).
  * "last week"/"last month" are the previous **calendar** period. "last 7 days"/"last 30
    days" are **rolling** from now. These are different windows and both readings are
    common, so the calendar ones say so.
  * "last Friday" is the most recent Friday **strictly before today** — on a Friday it is
    seven days ago, not today. A bare "Friday" includes today.
  * "this weekend" is **retrospective when the current week's weekend has not finished**:
    asked on a Tuesday it means the weekend just gone, because that is what a question
    about what happened means. "this Friday" is left literal and flagged when future,
    because that phrase is habitually forward-looking.

Pure and side-effect-free: `now` and `tz` are arguments, so every case is testable without
touching the clock.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone

#: How far back a rolling window may reach. A typo ("last 9999 days") should not become a
#: query that scans every index a log platform has.
MAX_ROLLING_DAYS = 3650

_WEEKDAYS = {
    "monday": 0, "mon": 0,
    "tuesday": 1, "tue": 1, "tues": 1,
    "wednesday": 2, "wed": 2,
    "thursday": 3, "thu": 3, "thur": 3, "thurs": 3,
    "friday": 4, "fri": 4,
    "saturday": 5, "sat": 5,
    "sunday": 6, "sun": 6,
}

# Small number words, because people write "the last two weeks" as often as "2".
_NUMBER_WORDS = {
    "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11, "twelve": 12,
    "fourteen": 14, "fifteen": 15, "twenty": 20, "thirty": 30, "sixty": 60, "ninety": 90,
}

_UNITS = {
    "minute": "minutes", "min": "minutes", "mins": "minutes", "minutes": "minutes",
    "hour": "hours", "hr": "hours", "hrs": "hours", "hours": "hours",
    "day": "days", "days": "days",
    "week": "weeks", "weeks": "weeks",
    "month": "months", "months": "months",
    "year": "years", "years": "years",
}

_ISO_DATE = re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b")


@dataclass(frozen=True)
class DateRange:
    """A half-open UTC window `[start, end)` plus what it means and what was assumed."""
    start: datetime
    end: datetime
    label: str
    assumption: str = ""

    @property
    def start_iso(self) -> str:
        return self.start.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    @property
    def end_iso(self) -> str:
        return self.end.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")

    def is_future(self, now: datetime) -> bool:
        """Wholly ahead of `now` — a retrospective query against it must come back empty,
        which is worth saying up front rather than reporting as 'nothing happened'."""
        return self.start > now


def load_timezone(name: str):
    """The caller's timezone, or UTC when it cannot be honoured.

    Never raises. `zoneinfo` needs a tz database, which Windows does not ship — the
    `tzdata` package supplies it and is declared, but a stripped environment should
    degrade to UTC (and say so via `resolve`'s assumption) rather than lose date
    resolution altogether.
    """
    if not name or name.upper() == "UTC":
        return timezone.utc
    try:
        from zoneinfo import ZoneInfo

        return ZoneInfo(name)
    except Exception:
        return timezone.utc


def _local_day(moment: datetime, tz) -> datetime:
    """Midnight starting the calendar day `moment` falls in, in `tz`."""
    local = moment.astimezone(tz)
    return datetime.combine(local.date(), time.min, tzinfo=tz)


def _span(start_local: datetime, days: int = 1, tz=timezone.utc) -> tuple[datetime, datetime]:
    """`days` calendar days from a local midnight, as UTC instants.

    Rebuilt from the DATE rather than by adding 24h, so a DST transition still yields a
    whole local day — the 23- and 25-hour days are exactly where a naive `+timedelta(1)`
    loses or double-counts an hour.
    """
    end_date = (start_local + timedelta(days=days)).date()
    end_local = datetime.combine(end_date, time.min, tzinfo=tz)
    return start_local.astimezone(timezone.utc), end_local.astimezone(timezone.utc)


def _add_months(moment: datetime, count: int) -> datetime:
    """Calendar-month arithmetic, clamping the day (31 Jan minus one month is 28/29 Feb)."""
    month_index = moment.month - 1 + count
    year = moment.year + month_index // 12
    month = month_index % 12 + 1
    day = min(moment.day, [31, 29 if year % 4 == 0 and (year % 100 or year % 400 == 0) else 28,
                           31, 30, 31, 30, 31, 31, 30, 31, 30, 31][month - 1])
    return moment.replace(year=year, month=month, day=day)


def _number(token: str) -> int | None:
    token = token.strip().lower()
    if token.isdigit():
        return int(token)
    return _NUMBER_WORDS.get(token)


def _normalize(phrase: str) -> str:
    text = (phrase or "").lower().strip()
    text = re.sub(r"[?!.,;:]+$", "", text)
    text = re.sub(r"\b(on|in|the|during|over|for|from)\b", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def _rolling(now: datetime, count: int, unit: str) -> DateRange | None:
    """A window ending NOW and reaching `count` units back."""
    if count < 1:
        return None
    if unit == "minutes":
        start = now - timedelta(minutes=count)
    elif unit == "hours":
        start = now - timedelta(hours=count)
    elif unit == "days":
        start = now - timedelta(days=count)
    elif unit == "weeks":
        start = now - timedelta(weeks=count)
    elif unit == "months":
        start = _add_months(now, -count)
    elif unit == "years":
        start = _add_months(now, -12 * count)
    else:
        return None
    if (now - start).days > MAX_ROLLING_DAYS:
        return None
    plural = unit if count != 1 else unit.rstrip("s")
    return DateRange(start, now, f"the last {count} {plural}, ending now",
                     "A rolling window measured back from the present moment, not a "
                     "calendar period.")


def resolve(phrase: str, now: datetime, tz=timezone.utc) -> DateRange | None:
    """One phrase -> a window, or None when it names no date this module understands.

    None is deliberate and is not a failure to paper over: inventing a window for an
    unparseable phrase is exactly the guessing this module exists to remove.
    """
    text = _normalize(phrase)
    if not text:
        return None
    now = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    today = _local_day(now, tz)

    # --- explicit dates win: nothing about them is ambiguous -------------------
    stamps = _ISO_DATE.findall(text)
    if stamps:
        try:
            first = datetime(int(stamps[0][0]), int(stamps[0][1]), int(stamps[0][2]), tzinfo=tz)
        except ValueError:
            return None
        if len(stamps) >= 2:
            try:
                last = datetime(int(stamps[1][0]), int(stamps[1][1]), int(stamps[1][2]), tzinfo=tz)
            except ValueError:
                return None
            if last < first:
                first, last = last, first
            start, end = _span(first, (last - first).days + 1, tz)
            return DateRange(start, end, f"{first.date()} to {last.date()} inclusive",
                             "Both endpoints treated as whole days; the end date is included.")
        start, end = _span(first, 1, tz)
        return DateRange(start, end, str(first.date()))

    # --- fixed single days -----------------------------------------------------
    if re.search(r"\btoday\b", text):
        start, end = _span(today, 1, tz)
        return DateRange(start, end, f"today, {today.date()}")
    if re.search(r"\byesterday\b", text):
        day = today - timedelta(days=1)
        start, end = _span(_local_day(day, tz), 1, tz)
        return DateRange(start, end, f"yesterday, {day.date()}")
    if re.search(r"\btomorrow\b", text):
        day = today + timedelta(days=1)
        start, end = _span(_local_day(day, tz), 1, tz)
        return DateRange(start, end, f"tomorrow, {day.date()}")

    # --- rolling windows: "last 30 days", "past two weeks", "3 days ago" --------
    rolling = re.search(
        r"\b(?:last|past|previous|latest)\s+(\d+|[a-z]+)\s+([a-z]+)\b", text)
    if rolling:
        count, unit = _number(rolling.group(1)), _UNITS.get(rolling.group(2))
        if count and unit:
            return _rolling(now, count, unit)

    ago = re.search(r"\b(\d+|[a-z]+)\s+([a-z]+)\s+ago\b", text)
    if ago:
        count, unit = _number(ago.group(1)), _UNITS.get(ago.group(2))
        if count and unit:
            if unit in ("minutes", "hours"):
                return _rolling(now, count, unit)
            if unit == "days":  # a day named by counting back is that whole day
                day = _local_day(today - timedelta(days=count), tz)
                start, end = _span(day, 1, tz)
                return DateRange(start, end, f"{day.date()}, {count} day"
                                             f"{'s' if count != 1 else ''} ago")
            return _rolling(now, count, unit)

    # "last hour" / "past week" with no number
    bare = re.search(r"\b(?:last|past|previous)\s+([a-z]+)\b", text)
    bare_unit = _UNITS.get(bare.group(1)) if bare else None
    if bare_unit in ("minutes", "hours"):
        return _rolling(now, 1, bare_unit)

    # --- weekends --------------------------------------------------------------
    if re.search(r"\bweekend\b", text):
        saturday = _local_day(today - timedelta(days=(today.weekday() - 5) % 7), tz)
        if saturday > today:            # only happens transiently; normalise backwards
            saturday -= timedelta(days=7)
        this_week_saturday = _local_day(today + timedelta(days=(5 - today.weekday())), tz)
        assumption = "A weekend is Saturday 00:00 to Monday 00:00."
        if re.search(r"\blast\b", text):
            # "last weekend" = the one before the most recent, when the most recent has
            # already been referred to as "this weekend"; simplest consistent reading is
            # the most recent COMPLETED weekend before the current week's.
            target = this_week_saturday - timedelta(days=7)
            assumption += " 'last weekend' = the weekend of the previous week."
        elif re.search(r"\bnext\b", text):
            target = this_week_saturday + timedelta(days=7)
        else:
            target = this_week_saturday
            if target + timedelta(days=2) > today + timedelta(days=1):
                # This week's weekend has not happened yet, and a question about what
                # happened cannot mean a future window.
                target = this_week_saturday - timedelta(days=7)
                assumption += (" 'this weekend' resolved BACKWARD to the most recent one, "
                               "since the current week's has not happened yet.")
        start, end = _span(_local_day(target, tz), 2, tz)
        return DateRange(start, end,
                         f"the weekend of {target.date()} (Sat) to "
                         f"{(target + timedelta(days=1)).date()} (Sun)", assumption)

    # --- calendar periods ------------------------------------------------------
    if re.search(r"\bweek\b", text):
        monday = _local_day(today - timedelta(days=today.weekday()), tz)
        if re.search(r"\blast\b|\bprevious\b", text):
            monday -= timedelta(days=7)
            start, end = _span(_local_day(monday, tz), 7, tz)
            return DateRange(start, end,
                             f"the week of {monday.date()} (Mon) to "
                             f"{(monday + timedelta(days=6)).date()} (Sun)",
                             "The previous CALENDAR week, Monday-start. If a rolling "
                             "seven days back from now was meant, ask for 'last 7 days'.")
        if re.search(r"\bnext\b", text):
            monday += timedelta(days=7)
        start, end = _span(_local_day(monday, tz), 7, tz)
        return DateRange(start, end,
                         f"the week of {monday.date()} (Mon) to "
                         f"{(monday + timedelta(days=6)).date()} (Sun)",
                         "The current CALENDAR week, Monday-start — it includes days "
                         "that have not happened yet.")

    if re.search(r"\bmonth\b", text):
        first = _local_day(today.replace(day=1), tz)
        if re.search(r"\blast\b|\bprevious\b", text):
            first = _local_day(_add_months(first, -1), tz)
            nxt = _add_months(first, 1)
            start, end = _span(first, (nxt.date() - first.date()).days, tz)
            return DateRange(start, end, f"{first.strftime('%B %Y')}",
                             "The previous CALENDAR month. If a rolling 30 days was "
                             "meant, ask for 'last 30 days'.")
        nxt = _add_months(first, 1)
        start, end = _span(first, (nxt.date() - first.date()).days, tz)
        return DateRange(start, end, f"{first.strftime('%B %Y')} (the current month)",
                         "The current CALENDAR month, including days still to come.")

    # --- named weekdays --------------------------------------------------------
    match = re.search(r"\b(" + "|".join(_WEEKDAYS) + r")\b", text)
    if match:
        target = _WEEKDAYS[match.group(1)]
        name = match.group(1).capitalize()
        if re.search(r"\blast\b|\bprevious\b", text):
            # STRICTLY before today: on a Friday, "last Friday" is seven days ago.
            back = (today.weekday() - target) % 7 or 7
            day = today - timedelta(days=back)
            assumption = (f"'last {name}' = the most recent {name} before today. On a "
                          f"{name} that is seven days ago, not today.")
        elif re.search(r"\bnext\b", text):
            forward = (target - today.weekday()) % 7 or 7
            day = today + timedelta(days=forward)
            assumption = ""
        elif re.search(r"\bthis\b", text):
            day = today + timedelta(days=target - today.weekday())
            assumption = f"'this {name}' = the {name} of the current Monday-start week."
        else:
            back = (today.weekday() - target) % 7   # bare weekday includes today
            day = today - timedelta(days=back)
            assumption = (f"A bare '{name}' = the most recent {name}, today included."
                          if back == 0 else "")
        start, end = _span(_local_day(day, tz), 1, tz)
        return DateRange(start, end, f"{name} {day.date()}", assumption)

    return None


def describe(phrase: str, now: datetime, tz_name: str = "UTC") -> str:
    """The agent-facing rendering: the window, what it means, and what was assumed."""
    tz = load_timezone(tz_name)
    effective = "UTC" if tz is timezone.utc else tz_name
    now_utc = now.astimezone(timezone.utc) if now.tzinfo else now.replace(tzinfo=timezone.utc)
    result = resolve(phrase, now, tz)
    if result is None:
        return (
            f"Could not resolve {phrase!r} to a date range. Say it as a calendar date "
            "(2026-08-07), a rolling window ('last 30 days'), a named day ('last "
            "Friday'), or a period ('last week', 'this weekend') — or ask the user which "
            "dates they mean. Do not guess a window."
        )
    lines = [
        f"{phrase!r} = {result.label}",
        f"  start (inclusive): {result.start_iso}",
        f"  end   (exclusive): {result.end_iso}",
        f"  calendar days interpreted in: {effective}",
    ]
    if effective != tz_name:
        lines.append(f"  NOTE: timezone {tz_name!r} is unavailable here; used UTC instead.")
    # Some live log tools (grafana/datadog/dynatrace) take "how many minutes back from
    # now", not an absolute range. Giving the equivalent here closes that seam in ONE
    # place instead of making the model redo the arithmetic this module exists to remove —
    # and states plainly that the window is wider, since minutes-back always runs to now.
    minutes_back = int((now_utc - result.start).total_seconds() // 60)
    if 0 < minutes_back <= MAX_ROLLING_DAYS * 24 * 60:
        lines.append(f"  for a tool that takes minutes-back-from-now: minutes={minutes_back}")
        if result.end < now_utc - timedelta(minutes=1):
            lines.append("    (that also sweeps up everything since the window ended — "
                         "filter the results by timestamp, or say the range you meant.)")
    if result.assumption:
        lines.append(f"  ASSUMPTION: {result.assumption}")
        lines.append("  State this assumption in your answer so the user can correct it.")
    if result.is_future(now_utc):
        lines.append("  WARNING: this window is entirely in the future — a search over it "
                     "will be empty for reasons that have nothing to do with the data.")
    return "\n".join(lines)
