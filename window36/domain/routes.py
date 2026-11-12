"""Deterministic multimodal route evaluation.

Each leg departs at max(ready time, scheduled departure + delay). A leg with a cutoff
requires the shipment to be ready at the terminal no later than the cutoff. After the last
leg the shipment waits for the destination's receiving hours.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime

from window36.domain.timeutil import daily_windows, hours, next_open, utc_str


@dataclass(frozen=True)
class LegSpec:
    seq: int
    mode: str
    origin: str
    destination: str
    duration_h: float
    cost_eur: float
    handoff_buffer_h: float = 0.0
    scheduled_departure: datetime | None = None
    cutoff_at: datetime | None = None
    delay_h: float = 0.0


@dataclass
class LegResult:
    seq: int
    mode: str
    origin: str
    destination: str
    ready_at: datetime
    departs_at: datetime | None
    arrives_at: datetime | None
    cutoff_at: datetime | None
    delay_h: float
    status: str  # ok | missed_cutoff | not_reached
    reason: str = ""


@dataclass
class RouteResult:
    feasible: bool
    arrival: datetime | None
    delivered_at: datetime | None  # after waiting for receiving hours
    cost_eur: float
    legs: list[LegResult] = field(default_factory=list)
    reasons: list[str] = field(default_factory=list)


def evaluate_route(
    legs: list[LegSpec],
    earliest_dispatch: datetime,
    receiving_tz: str,
    receiving_open: str,
    receiving_close: str,
) -> RouteResult:
    legs = sorted(legs, key=lambda leg: leg.seq)
    t = earliest_dispatch
    results: list[LegResult] = []
    reasons: list[str] = []
    feasible = True
    cost = sum(leg.cost_eur for leg in legs)
    for leg in legs:
        if not feasible:
            results.append(
                LegResult(
                    leg.seq,
                    leg.mode,
                    leg.origin,
                    leg.destination,
                    t,
                    None,
                    None,
                    leg.cutoff_at,
                    leg.delay_h,
                    "not_reached",
                )
            )
            continue
        ready = t + hours(leg.handoff_buffer_h)
        if leg.cutoff_at is not None and ready > leg.cutoff_at:
            feasible = False
            reason = (
                f"Leg {leg.seq} ({leg.mode} {leg.origin} → {leg.destination}): shipment ready "
                f"{utc_str(ready)}, after cutoff {utc_str(leg.cutoff_at)}"
            )
            reasons.append(reason)
            results.append(
                LegResult(
                    leg.seq,
                    leg.mode,
                    leg.origin,
                    leg.destination,
                    ready,
                    None,
                    None,
                    leg.cutoff_at,
                    leg.delay_h,
                    "missed_cutoff",
                    reason,
                )
            )
            continue
        depart = ready
        if leg.scheduled_departure is not None:
            depart = max(ready, leg.scheduled_departure + hours(leg.delay_h))
        elif leg.delay_h:
            depart = ready + hours(leg.delay_h)
        arrive = depart + hours(leg.duration_h)
        results.append(
            LegResult(
                leg.seq, leg.mode, leg.origin, leg.destination, ready, depart, arrive, leg.cutoff_at, leg.delay_h, "ok"
            )
        )
        t = arrive
    if not feasible:
        return RouteResult(False, None, None, cost, results, reasons)
    windows = daily_windows(receiving_tz, receiving_open, receiving_close, t, t + hours(72))
    delivered = next_open(t, windows)
    if delivered is None:
        return RouteResult(False, t, None, cost, results, ["No receiving window found"])
    return RouteResult(True, t, delivered, cost, results, [])
