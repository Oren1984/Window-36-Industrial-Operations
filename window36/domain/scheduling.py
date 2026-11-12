"""Transparent deterministic list scheduling.

Tasks are placed in dependency order (ties broken by code). Each task starts at the earliest
instant that satisfies: window start, all predecessors finished, part available (if it needs the
replacement part), and a crew of the required skill free for the full duration inside its
local working hours. No solver is needed for the seeded constraints.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from window36.domain.timeutil import daily_windows, hours


@dataclass(frozen=True)
class TaskSpec:
    code: str
    name: str
    duration_h: float
    skill: str
    requires_part: bool
    depends_on: tuple[str, ...] = ()


@dataclass(frozen=True)
class CrewSpec:
    skill: str
    headcount: int
    daily_start: str
    daily_end: str


@dataclass
class ScheduledTask:
    code: str
    name: str
    skill: str
    start: datetime
    finish: datetime
    crew: int
    binding: str  # what determined the start: window | dep:<code> | part | crew | calendar
    requires_part: bool
    depends_on: tuple[str, ...] = ()


@dataclass
class ScheduleResult:
    tasks: list[ScheduledTask] = field(default_factory=list)
    finish: datetime | None = None
    critical_path: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)


class SchedulingError(ValueError):
    pass


def topo_order(tasks: list[TaskSpec]) -> list[TaskSpec]:
    by_code = {t.code: t for t in tasks}
    for t in tasks:
        for d in t.depends_on:
            if d not in by_code:
                raise SchedulingError(f"Task {t.code} depends on unknown task {d}")
    done: set[str] = set()
    order: list[TaskSpec] = []
    remaining = sorted(tasks, key=lambda t: t.code)
    while remaining:
        ready = [t for t in remaining if all(d in done for d in t.depends_on)]
        if not ready:
            raise SchedulingError("Dependency cycle among tasks: " + ", ".join(t.code for t in remaining))
        nxt = ready[0]
        order.append(nxt)
        done.add(nxt.code)
        remaining.remove(nxt)
    return order


def _fit(
    earliest: datetime,
    duration_h: float,
    availability: list[tuple[datetime, datetime]],
    busy: list[tuple[datetime, datetime]],
) -> datetime | None:
    dur = hours(duration_h)
    for win_start, win_end in availability:
        if win_end <= earliest:
            continue
        cand = max(earliest, win_start)
        moved = True
        while moved:
            moved = False
            for b_start, b_end in busy:
                if cand < b_end and cand + dur > b_start:
                    cand = b_end
                    moved = True
        if cand + dur <= win_end:
            return cand
    return None


def schedule(
    tasks: list[TaskSpec],
    crews: list[CrewSpec],
    site_tz: str,
    window_start: datetime,
    part_available_at: datetime | None,
    horizon_h: float = 24 * 10,
) -> ScheduleResult:
    result = ScheduleResult()
    try:
        order = topo_order(tasks)
    except SchedulingError as exc:
        result.errors.append(str(exc))
        return result
    horizon_end = window_start + hours(horizon_h)
    crew_map = {c.skill: c for c in crews}
    availability = {
        c.skill: daily_windows(site_tz, c.daily_start, c.daily_end, window_start, horizon_end) for c in crews
    }
    busy: dict[tuple[str, int], list[tuple[datetime, datetime]]] = {}
    placed: dict[str, ScheduledTask] = {}
    for t in order:
        if t.skill not in crew_map or crew_map[t.skill].headcount < 1:
            result.errors.append(f"No staff with skill '{t.skill}' for task {t.code}")
            return result
        if t.requires_part and part_available_at is None:
            result.errors.append(f"Task {t.code} needs the replacement part, which is not available")
            return result
        earliest, binding = window_start, "window"
        for d in t.depends_on:
            if placed[d].finish > earliest:
                earliest, binding = placed[d].finish, f"dep:{d}"
        if t.requires_part and part_available_at > earliest:
            earliest, binding = part_available_at, "part"
        best: tuple[datetime, int] | None = None
        for unit in range(crew_map[t.skill].headcount):
            start = _fit(earliest, t.duration_h, availability[t.skill], busy.get((t.skill, unit), []))
            if start is not None and (best is None or start < best[0]):
                best = (start, unit)
        if best is None:
            result.errors.append(f"No {t.skill} crew slot for task {t.code} within the planning horizon")
            return result
        start, unit = best
        if start > earliest:
            in_window = any(s <= earliest and earliest + hours(t.duration_h) <= e for s, e in availability[t.skill])
            binding = "crew" if in_window else "calendar"
        finish = start + hours(t.duration_h)
        busy.setdefault((t.skill, unit), []).append((start, finish))
        placed[t.code] = ScheduledTask(
            t.code, t.name, t.skill, start, finish, unit + 1, binding, t.requires_part, t.depends_on
        )
    result.tasks = sorted(placed.values(), key=lambda s: (s.start, s.code))
    result.finish = max(s.finish for s in placed.values()) if placed else window_start
    result.critical_path = critical_path(placed)
    return result


def critical_path(placed: dict[str, ScheduledTask]) -> list[str]:
    """Trace back from the last-finishing task through the constraint that bound each start."""
    if not placed:
        return []
    current = max(placed.values(), key=lambda s: (s.finish, s.code))
    path = [current.code]
    while True:
        if current.binding.startswith("dep:"):
            current = placed[current.binding[4:]]
        elif current.binding in ("crew", "calendar") and current.depends_on:
            # Waited for a crew or working hours: the chain continues through the latest predecessor.
            current = max((placed[d] for d in current.depends_on), key=lambda s: (s.finish, s.code))
        else:
            break
        path.append(current.code)
    if current.binding == "part":
        path.append("PART")
    return list(reversed(path))
