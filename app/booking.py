"""Diagnostic-frame reservation engine.

A *drill* (演练) reuses the two-hop TSN configuration and one fixed clock
offset of the verification service.  After the configuration is verified, the
network manager may reserve up to the first ``RESERVATION_PERIODS`` repetition
periods (counted from empty queues) for diagnostic frames.

Every reservation request carries, all on the hop-1 clock (the unified
timeline):

* ``hop1_earliest_start_us``  a: earliest instant hop 1 may start;
* ``hop2_latest_finish_us``   b: latest instant hop 2 may finish;
* ``hop1_tx_us`` / ``hop2_tx_us``: the two transmission durations;
* ``max_relay_wait_us``       w: maximum time between hop-1 finish and
  hop-2 start (the transit wait limit).

Placement rules (all integer microseconds):

1. Both hops must send the *whole* frame inside one local gate window
   (finishing exactly at gate close is allowed).
2. The send interval of either hop must not overlap a camera-trigger frame's
   send interval or another reservation's send interval on the *same* hop
   (intervals that merely touch, ``[s, f)`` and ``[f, ...)``, do not overlap).
3. Hop 2 may start only after hop 1 has finished: ``s2 >= f1``.
4. The relay wait must stay within its limit: ``s2 - f1 <= w``.
5. ``s1 >= a`` and ``f2 <= b``, and both hops have to lie within the
   reservation horizon ``[0, 8*T)``.

The engine returns the placement with the *earliest delivery* (smallest
``f2``), breaking ties by the earliest ``s1``.  If no placement satisfies
every rule, no slot is returned and nothing is occupied.
"""

from __future__ import annotations

import threading
import uuid
from dataclasses import dataclass, field
from typing import Any, Optional

from .simulator import (
    GateWindow,
    VerifyConfig,
    earliest_gate_start,
    evaluate_offset,
    simulate_periods,
)

RESERVATION_PERIODS = 8

# Machine-readable reasons carried in a no-slot response.
REASON_HOP1_NO_SLOT = "hop1_no_available_slot"
REASON_HOP2_NO_SLOT = "hop2_no_available_slot"
REASON_RELAY_WAIT_EXCEEDED = "relay_wait_limit_exceeded"
REASON_LATEST_FINISH_UNREACHABLE = "hop2_latest_finish_unreachable"
REASON_HORIZON_EXCEEDED = "reservation_horizon_exceeded"
REASON_FRAME_LONGER_THAN_GATE = "frame_longer_than_gate_window"


@dataclass(frozen=True)
class Interval:
    """A half-open send interval ``[start, finish)`` on the unified timeline."""

    start: int
    finish: int

    def overlaps(self, other: "Interval") -> bool:
        return self.start < other.finish and other.start < self.finish


@dataclass
class Reservation:
    reservation_id: str
    frame_id: Optional[str]
    hop1: Interval
    hop2: Interval
    hop1_tx_us: int
    hop2_tx_us: int
    max_relay_wait_us: int
    earliest_start_us: int
    latest_finish_us: int
    period_no: int
    relay_wait_us: int

    def as_dict(self, delta_us: int) -> dict[str, Any]:
        return {
            "reservation_id": self.reservation_id,
            "frame_id": self.frame_id,
            "period_no": self.period_no,
            "hop1": {
                "start_timeline_us": self.hop1.start,
                "finish_timeline_us": self.hop1.finish,
                "start_local_us": self.hop1.start,
                "finish_local_us": self.hop1.finish,
            },
            "hop2": {
                "start_timeline_us": self.hop2.start,
                "finish_timeline_us": self.hop2.finish,
                "start_local_us": self.hop2.start + delta_us,
                "finish_local_us": self.hop2.finish + delta_us,
            },
            "relay_wait_us": self.relay_wait_us,
            "request_constraints": {
                "hop1_earliest_start_us": self.earliest_start_us,
                "hop2_latest_finish_us": self.latest_finish_us,
                "max_relay_wait_us": self.max_relay_wait_us,
                "hop1_tx_us": self.hop1_tx_us,
                "hop2_tx_us": self.hop2_tx_us,
            },
        }


@dataclass
class Drill:
    drill_id: str
    config: VerifyConfig
    clock_offset_us: int
    horizon_us: int
    hop1_blocked: list[Interval] = field(default_factory=list)
    hop2_blocked: list[Interval] = field(default_factory=list)
    reservations: list[Reservation] = field(default_factory=list)

    @property
    def period_us(self) -> int:
        return self.config.period_us


def _intervals_overlap_with_any(iv: Interval, blocked: list[Interval]) -> bool:
    return any(iv.overlaps(b) for b in blocked)


def earliest_free_start(
    now: int,
    tx_us: int,
    open_phase_us: int,
    window_len_us: int,
    period_us: int,
    blocked: list[Interval],
    horizon: int,
) -> Optional[int]:
    """Earliest gate-legal, non-blocked start ``s >= now`` with ``s+tx < horizon``.

    Combines the gate-window rule of :func:`earliest_gate_start` with the
    same-hop occupancy rule (trigger frames and existing reservations).
    Returns ``None`` when no start exists inside the horizon.
    """
    if tx_us < 0:
        raise ValueError("transmission duration must be non-negative")
    if tx_us > window_len_us:
        return None

    t = now
    # Each iteration either returns or jumps past a concrete blocker, so the
    # loop is bounded by the number of blocked intervals plus the period count.
    while True:
        s = earliest_gate_start(t, tx_us, open_phase_us, window_len_us, period_us)
        if s is None:
            return None
        if s + tx_us > horizon:
            return None
        candidate = Interval(s, s + tx_us)
        hit = next((b for b in blocked if candidate.overlaps(b)), None)
        if hit is None:
            return s
        # Try again from the end of the conflicting interval.
        t = hit.finish


def place_diagnostic_frame(
    drill: Drill,
    earliest_start_us: int,
    latest_finish_us: int,
    hop1_tx_us: int,
    hop2_tx_us: int,
    max_relay_wait_us: int,
) -> dict[str, Any]:
    """Find the earliest-delivery feasible placement without occupying it.

    The function is side-effect free; the caller (holding the drill lock)
    commits the returned intervals.
    """
    cfg = drill.config
    period = cfg.period_us
    horizon = drill.horizon_us
    delta = drill.clock_offset_us

    hop1_open = cfg.hop1_gate.open_us
    hop1_len = cfg.hop1_gate.length_us
    hop2_open = cfg.hop2_gate.open_us - delta
    hop2_len = cfg.hop2_gate.length_us

    def no_slot(reason: str, detail: str) -> dict[str, Any]:
        return {
            "available": False,
            "reason": reason,
            "detail": detail,
            "placement": None,
        }

    if hop1_tx_us > hop1_len or hop2_tx_us > hop2_len:
        return no_slot(
            REASON_FRAME_LONGER_THAN_GATE,
            "a transmission duration is longer than every gate window",
        )
    if earliest_start_us < 0 or earliest_start_us >= horizon:
        return no_slot(
            REASON_HORIZON_EXCEEDED,
            f"earliest start must lie inside [0, {horizon})",
        )
    if latest_finish_us <= earliest_start_us or latest_finish_us > horizon:
        return no_slot(
            REASON_HORIZON_EXCEEDED,
            f"latest finish must lie inside ({earliest_start_us}, {horizon}]",
        )

    s1 = earliest_free_start(
        earliest_start_us,
        hop1_tx_us,
        hop1_open,
        hop1_len,
        period,
        drill.hop1_blocked,
        horizon,
    )

    # Failure reasons seen while scanning, reported if no candidate works.
    # Neither the relay wait nor f2 is monotone in s1 in general (blockers can
    # make an early candidate wait and a later one immediate), so a failed
    # candidate only disqualifies itself; the scan keeps going.
    saw_wait_violation = False
    saw_finish_violation = False
    saw_no_hop2_slot = False
    wait_detail = ""

    while s1 is not None:
        f1 = s1 + hop1_tx_us

        # Hop 2: not before hop 1 finishes; the relay wait is capped at w.
        s2_lower = f1
        s2_upper = f1 + max_relay_wait_us
        s2 = earliest_free_start(
            s2_lower,
            hop2_tx_us,
            hop2_open,
            hop2_len,
            period,
            drill.hop2_blocked,
            horizon,
        )

        if s2 is None:
            # No hop-2 slot inside the horizon from this f1 onward; the lower
            # bound only moves right for later s1, so the scan cannot recover.
            saw_no_hop2_slot = True
            break

        f2 = s2 + hop2_tx_us

        if s2 <= s2_upper and f2 <= latest_finish_us:
            # Feasible.  Hop-1 candidates are scanned in ascending order and
            # earliest_free_start is monotone in its lower bound, so this is
            # the globally earliest-delivery placement (earliest s1 on ties).
            return {
                "available": True,
                "reason": None,
                "placement": {
                    "hop1_start": s1,
                    "hop1_finish": f1,
                    "hop2_start": s2,
                    "hop2_finish": f2,
                    "relay_wait": s2 - f1,
                },
            }

        if f2 > latest_finish_us:
            saw_finish_violation = True
        else:
            # f2 within the finish bound but s2 past the relay-wait cap.
            saw_wait_violation = True
            wait_detail = (
                f"earliest hop-2 start is {s2 - f1} us after hop-1 finish, "
                f"limit is {max_relay_wait_us} us"
            )

        next_s1 = earliest_free_start(
            s1 + 1,
            hop1_tx_us,
            hop1_open,
            hop1_len,
            period,
            drill.hop1_blocked,
            horizon,
        )
        if next_s1 is None or next_s1 == s1:
            break
        s1 = next_s1

    # Reason priority: the relay constraint is the most specific failure, then
    # an over-tight finish bound; horizon exhaustion and a missing hop-1 slot
    # are the structural fallbacks.
    if saw_wait_violation:
        return no_slot(REASON_RELAY_WAIT_EXCEEDED, wait_detail)
    if saw_finish_violation:
        return no_slot(
            REASON_LATEST_FINISH_UNREACHABLE,
            "no placement finishes within hop2_latest_finish_us",
        )
    if saw_no_hop2_slot:
        return no_slot(
            REASON_HOP2_NO_SLOT,
            "no gate-legal, unoccupied hop-2 slot exists inside the horizon",
        )
    return no_slot(
        REASON_HOP1_NO_SLOT,
        "no gate-legal, unoccupied hop-1 slot exists inside the horizon",
    )


class DrillStore:
    """Thread-safe in-memory collection of drills and reservations.

    A single re-entrant lock per store makes "check both hops then occupy"
    atomic, so concurrent reservation requests can never interleave and
    over-commit either hop.
    """

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._drills: dict[str, Drill] = {}

    # ---------------------------------------------------------------- drills
    def create_drill(self, cfg: VerifyConfig, delta_us: int) -> dict[str, Any]:
        """Verify the configuration and open a drill with trigger occupancy."""
        with self._lock:
            report = evaluate_offset(cfg, delta_us)
            if report["verdict"] != "pass":
                return {"created": False, "verification": report, "drill": None}

            trace = simulate_periods(cfg, delta_us, RESERVATION_PERIODS)
            horizon = RESERVATION_PERIODS * cfg.period_us
            hop1_blocked: list[Interval] = []
            hop2_blocked: list[Interval] = []
            trigger_schedule: list[dict[str, Any]] = []
            for row in trace:
                entry: dict[str, Any] = {
                    "period_no": row["period_no"],
                    "frame_no": row["frame_no"],
                    "frame_id": row["frame_id"],
                    "hop1": None,
                    "hop2": None,
                }
                if row["hop1_start_timeline_us"] is not None:
                    iv1 = Interval(
                        row["hop1_start_timeline_us"],
                        row["hop1_finish_timeline_us"],
                    )
                    if iv1.finish <= horizon:
                        hop1_blocked.append(iv1)
                        entry["hop1"] = {
                            "start_timeline_us": iv1.start,
                            "finish_timeline_us": iv1.finish,
                        }
                if row["hop2_start_timeline_us"] is not None:
                    iv2 = Interval(
                        row["hop2_start_timeline_us"],
                        row["hop2_finish_timeline_us"],
                    )
                    if iv2.finish <= horizon:
                        hop2_blocked.append(iv2)
                        entry["hop2"] = {
                            "start_timeline_us": iv2.start,
                            "finish_timeline_us": iv2.finish,
                            "local_start_us": iv2.start + delta_us,
                            "local_finish_us": iv2.finish + delta_us,
                        }
                trigger_schedule.append(entry)

            drill_id = uuid.uuid4().hex
            drill = Drill(
                drill_id=drill_id,
                config=cfg,
                clock_offset_us=delta_us,
                horizon_us=horizon,
                hop1_blocked=sorted(hop1_blocked, key=lambda iv: iv.start),
                hop2_blocked=sorted(hop2_blocked, key=lambda iv: iv.start),
            )
            self._drills[drill_id] = drill
            return {
                "created": True,
                "verification": report,
                "drill": drill,
                "trigger_schedule": trigger_schedule,
            }

    def get(self, drill_id: str) -> Optional[Drill]:
        with self._lock:
            return self._drills.get(drill_id)

    def list_drills(self) -> list[Drill]:
        with self._lock:
            return list(self._drills.values())

    # ----------------------------------------------------------- reservation
    def reserve(
        self,
        drill_id: str,
        earliest_start_us: int,
        latest_finish_us: int,
        hop1_tx_us: int,
        hop2_tx_us: int,
        max_relay_wait_us: int,
        frame_id: Optional[str] = None,
    ) -> Optional[dict[str, Any]]:
        """Atomically place and commit one reservation; ``None`` = unknown id."""
        with self._lock:
            drill = self._drills.get(drill_id)
            if drill is None:
                return None

            outcome = place_diagnostic_frame(
                drill,
                earliest_start_us=earliest_start_us,
                latest_finish_us=latest_finish_us,
                hop1_tx_us=hop1_tx_us,
                hop2_tx_us=hop2_tx_us,
                max_relay_wait_us=max_relay_wait_us,
            )
            if not outcome["available"]:
                return outcome

            p = outcome["placement"]
            iv1 = Interval(p["hop1_start"], p["hop1_finish"])
            iv2 = Interval(p["hop2_start"], p["hop2_finish"])

            # Re-check under the lock: both hops must still be free (defensive
            # invariant; nothing releases the lock between search and commit).
            if _intervals_overlap_with_any(
                iv1, drill.hop1_blocked
            ) or _intervals_overlap_with_any(iv2, drill.hop2_blocked):
                return {
                    "available": False,
                    "reason": "concurrent_occupation",
                    "detail": "the slot was taken by a concurrent request",
                    "placement": None,
                }

            reservation = Reservation(
                reservation_id=uuid.uuid4().hex,
                frame_id=frame_id,
                hop1=iv1,
                hop2=iv2,
                hop1_tx_us=hop1_tx_us,
                hop2_tx_us=hop2_tx_us,
                max_relay_wait_us=max_relay_wait_us,
                earliest_start_us=earliest_start_us,
                latest_finish_us=latest_finish_us,
                period_no=(p["hop1_start"] // drill.period_us) + 1,
                relay_wait_us=p["relay_wait"],
            )
            drill.reservations.append(reservation)
            drill.hop1_blocked.append(iv1)
            drill.hop2_blocked.append(iv2)
            drill.hop1_blocked.sort(key=lambda iv: iv.start)
            drill.hop2_blocked.sort(key=lambda iv: iv.start)

            return {
                "available": True,
                "reason": None,
                "reservation": reservation,
            }

    def snapshot(self, drill_id: str) -> Optional[dict[str, Any]]:
        with self._lock:
            drill = self._drills.get(drill_id)
            if drill is None:
                return None
            return {
                "drill_id": drill.drill_id,
                "clock_offset_us": drill.clock_offset_us,
                "period_us": drill.period_us,
                "reservation_periods": RESERVATION_PERIODS,
                "horizon_us": drill.horizon_us,
                "reservation_count": len(drill.reservations),
                "reservations": [
                    r.as_dict(drill.clock_offset_us) for r in drill.reservations
                ],
                "occupied_hop1_us": [
                    [iv.start, iv.finish] for iv in drill.hop1_blocked
                ],
                "occupied_hop2_us": [
                    [iv.start, iv.finish] for iv in drill.hop2_blocked
                ],
            }


# Module-level store used by the FastAPI routes.  One process, in-memory state.
store = DrillStore()
