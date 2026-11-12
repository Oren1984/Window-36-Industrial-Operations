"""UTC storage, site-local display."""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from zoneinfo import ZoneInfo


def utc(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        raise ValueError("naive datetime not allowed")
    return dt.astimezone(UTC)


def hours(h: float) -> timedelta:
    return timedelta(hours=h)


def parse_hhmm(value: str) -> tuple[int, int]:
    hh, mm = value.split(":")
    return int(hh), int(mm)


def local_str(dt: datetime | None, tz_name: str, with_tz: bool = True) -> str:
    if dt is None:
        return "—"
    local = dt.astimezone(ZoneInfo(tz_name))
    text = local.strftime("%a %d %b %H:%M")
    return f"{text} {local.tzname()}" if with_tz else text


def utc_str(dt: datetime | None) -> str:
    if dt is None:
        return "—"
    return dt.astimezone(UTC).strftime("%a %d %b %H:%M UTC")


def daily_windows(
    tz_name: str, start_hhmm: str, end_hhmm: str, frm: datetime, to: datetime
) -> list[tuple[datetime, datetime]]:
    """Recurring local daily windows expressed in UTC between frm and to, merged when contiguous."""
    tz = ZoneInfo(tz_name)
    sh, sm = parse_hhmm(start_hhmm)
    eh, em = parse_hhmm(end_hhmm)
    day = frm.astimezone(tz).date() - timedelta(days=1)
    last = to.astimezone(tz).date() + timedelta(days=1)
    windows: list[tuple[datetime, datetime]] = []
    while day <= last:
        start = datetime.combine(day, time(sh, sm), tz)
        if eh == 24:
            end = datetime.combine(day + timedelta(days=1), time(0, 0), tz)
        else:
            end = datetime.combine(day, time(eh, em), tz)
        if end > start:
            s, e = start.astimezone(UTC), end.astimezone(UTC)
            if windows and windows[-1][1] >= s:
                windows[-1] = (windows[-1][0], max(windows[-1][1], e))
            else:
                windows.append((s, e))
        day += timedelta(days=1)
    return windows


def next_open(dt: datetime, windows: list[tuple[datetime, datetime]]) -> datetime | None:
    """Earliest instant >= dt inside one of the windows."""
    for s, e in windows:
        if dt < e:
            return max(dt, s)
    return None
