from datetime import UTC, datetime

import pytest

from window36.domain.scheduling import CrewSpec, SchedulingError, TaskSpec, schedule, topo_order
from window36.domain.timeutil import daily_windows


def T(d, h, m=0):
    return datetime(2026, 2, d, h, m, tzinfo=UTC)


TASKS = [
    TaskSpec("T1", "LOTO", 2, "mech", False),
    TaskSpec("T2", "Drain", 4, "mech", False, ("T1",)),
    TaskSpec("T3", "Rollers", 6, "mech", False, ("T1",)),
    TaskSpec("T4", "Sensors", 4, "elec", False, ("T1",)),
    TaskSpec("T5", "Remove", 3, "mech", True, ("T2",)),
    TaskSpec("T6", "Install", 4, "mech", True, ("T5",)),
    TaskSpec("T7", "Align", 3, "mech", True, ("T6",)),
    TaskSpec("T8", "QA run", 3, "qa", False, ("T3", "T4", "T7")),
    TaskSpec("T9", "Handover", 1, "mech", False, ("T8",)),
]
CREWS = [
    CrewSpec("mech", 2, "00:00", "24:00"),
    CrewSpec("elec", 1, "00:00", "24:00"),
    CrewSpec("qa", 1, "07:00", "23:00"),
]


def by_code(res):
    return {t.code: t for t in res.tasks}


def test_dependencies_parallel_crews_and_part_arrival():
    res = schedule(TASKS, CREWS, "Europe/Paris", T(20, 6), part_available_at=T(20, 20))
    s = by_code(res)
    assert s["T1"].start == T(20, 6) and s["T1"].finish == T(20, 8)
    assert s["T2"].start == s["T3"].start == s["T4"].start == T(20, 8)  # two mech crews + elec
    assert s["T5"].start == T(20, 20) and s["T5"].binding == "part"
    assert s["T7"].finish == T(21, 6)
    assert s["T8"].start == T(21, 6)  # QA inspector works 07:00–23:00 Paris
    assert res.finish == T(21, 10)
    assert res.critical_path == ["PART", "T5", "T6", "T7", "T8", "T9"]


def test_qa_calendar_forces_wait_overnight():
    res = schedule(TASKS, CREWS, "Europe/Paris", T(20, 6), part_available_at=T(21, 12))
    s = by_code(res)
    assert s["T7"].finish == T(21, 22)
    assert s["T8"].start == T(22, 6) and s["T8"].binding == "calendar"
    assert res.critical_path == ["PART", "T5", "T6", "T7", "T8", "T9"]  # traced through the calendar wait


def test_reduced_scope_without_part():
    reduced = [
        TaskSpec("T1", "LOTO", 2, "mech", False),
        TaskSpec("T3", "Rollers", 6, "mech", False, ("T1",)),
        TaskSpec("T4", "Sensors", 4, "elec", False, ("T1",)),
        TaskSpec("T8", "QA", 3, "qa", False, ("T3", "T4")),
        TaskSpec("T9", "Hand", 1, "mech", False, ("T8",)),
    ]
    res = schedule(reduced, CREWS, "Europe/Paris", T(20, 6), None)
    assert res.finish == T(20, 18) and not res.errors


def test_part_task_without_part_is_an_error():
    res = schedule(TASKS, CREWS, "Europe/Paris", T(20, 6), None)
    assert res.errors and "needs the replacement part" in res.errors[0]


def test_missing_skill_is_an_error():
    res = schedule(TASKS, CREWS[:2], "Europe/Paris", T(20, 6), T(20, 7))
    assert any("skill 'qa'" in e for e in res.errors)


def test_cycle_detection():
    with pytest.raises(SchedulingError, match="cycle"):
        topo_order([TaskSpec("A", "a", 1, "mech", False, ("B",)), TaskSpec("B", "b", 1, "mech", False, ("A",))])


def test_daily_windows_respect_local_time_and_dst():
    w = daily_windows(
        "Europe/Paris", "07:00", "23:00", datetime(2026, 3, 28, tzinfo=UTC), datetime(2026, 3, 30, tzinfo=UTC)
    )
    starts = [s for s, _ in w]
    assert datetime(2026, 3, 28, 6, tzinfo=UTC) in starts  # CET
    assert datetime(2026, 3, 29, 5, tzinfo=UTC) in starts  # CEST after the switch
