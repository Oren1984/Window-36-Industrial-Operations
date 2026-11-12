from datetime import UTC, datetime

from window36.domain.routes import LegSpec, evaluate_route
from window36.domain.timeutil import local_str


def T(d, h, m=0):
    return datetime(2026, 2, d, h, m, tzinfo=UTC)


ROAD = [LegSpec(1, "road", "Hessenfeld", "Valmont", 10, 3100, scheduled_departure=T(20, 9))]
AIR = [
    LegSpec(1, "road", "Plant", "SNN", 1.5, 600, scheduled_departure=T(20, 7, 30)),
    LegSpec(
        2, "air", "SNN", "CDG", 2, 7300, handoff_buffer_h=0.5, cutoff_at=T(20, 10), scheduled_departure=T(20, 11, 30)
    ),
    LegSpec(3, "road", "CDG", "Valmont", 3, 1000, handoff_buffer_h=2),
]
SEA = [
    LegSpec(1, "road", "Plant", "Rosslare", 3.5, 450, scheduled_departure=T(20, 8)),
    LegSpec(
        2,
        "sea",
        "Rosslare",
        "Cherbourg",
        19,
        1400,
        handoff_buffer_h=1,
        cutoff_at=T(20, 14),
        scheduled_departure=T(20, 16),
    ),
    LegSpec(3, "road", "Cherbourg", "Valmont", 6, 500, handoff_buffer_h=1.5),
]
FR = ("Europe/Paris", "06:00", "22:00")


def test_road_route_arrival():
    r = evaluate_route(ROAD, T(20, 3), *FR)
    assert r.feasible and r.arrival == T(20, 19) and r.delivered_at == T(20, 19)
    assert r.cost_eur == 3100


def test_road_pickup_delay_shifts_arrival():
    delayed = [LegSpec(1, "road", "H", "V", 10, 3100, scheduled_departure=T(20, 9), delay_h=16)]
    r = evaluate_route(delayed, T(20, 3), *FR)
    assert r.arrival == T(21, 11)


def test_air_route_with_handoffs_and_cutoff():
    r = evaluate_route(AIR, T(20, 3), *FR)
    assert r.feasible
    assert [leg.departs_at for leg in r.legs] == [T(20, 7, 30), T(20, 11, 30), T(20, 15, 30)]
    assert r.arrival == T(20, 18, 30) and r.cost_eur == 8900


def test_missed_air_cutoff_reports_reason():
    late = [LegSpec(1, "road", "Plant", "SNN", 1.5, 600, scheduled_departure=T(20, 7, 30), delay_h=2.5)] + AIR[1:]
    r = evaluate_route(late, T(20, 3), *FR)
    assert not r.feasible
    assert r.legs[1].status == "missed_cutoff" and r.legs[2].status == "not_reached"
    assert "after cutoff" in r.reasons[0] and "10:00 UTC" in r.reasons[0]


def test_late_dispatch_misses_cutoff():
    assert evaluate_route(AIR, T(20, 8), *FR).feasible  # ready at 10:00 exactly: cutoff met
    r = evaluate_route(AIR, T(20, 8, 30), *FR)  # authorization too late for the feeder truck
    assert not r.feasible


def test_sea_route_arrives_next_evening():
    r = evaluate_route(SEA, T(20, 3), *FR)
    assert r.feasible and r.arrival == T(21, 18, 30)


def test_arrival_outside_receiving_hours_waits_until_open():
    night = [LegSpec(1, "road", "A", "B", 2, 1, scheduled_departure=T(20, 21, 30))]
    r = evaluate_route(night, T(20, 3), *FR)
    assert r.arrival == T(20, 23, 30)  # 00:30 Paris time
    assert r.delivered_at == T(21, 5)  # waits for 06:00 Paris


def test_site_time_zones_display():
    assert local_str(T(20, 6), "Europe/Paris").endswith("07:00 CET")
    assert local_str(T(20, 6), "Europe/Dublin").endswith("06:00 GMT")
    assert local_str(T(20, 6), "Europe/Berlin").endswith("07:00 CET")
    summer = datetime(2026, 7, 1, 6, tzinfo=UTC)
    assert local_str(summer, "Europe/Paris").endswith("08:00 CEST")
