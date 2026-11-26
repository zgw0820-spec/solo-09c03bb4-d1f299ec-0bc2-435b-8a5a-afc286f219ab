"""Two-hop TSN gate delivery verification engine.

The engine is deliberately free of any web framework so it can be unit tested
directly.  All times are integer microseconds on a *unified timeline* whose
zero is the start of repetition period 0 on the hop-1 clock.

Clock model
-----------
Let ``delta = clock2 - clock1`` be the fixed offset between the second-hop
local clock and the first-hop local clock.  We let the unified timeline equal
the hop-1 clock, therefore:

* hop-1 gate phase ``[open1, close1]`` repeats at ``k*T + open1``;
* hop-2 local gate phase ``[open2, close2]`` corresponds to unified phase
  ``open2 - delta`` and repeats at ``k*T + open2 - delta``;
* a unified instant ``t`` reads ``t + delta`` on the hop-2 clock.

Scheduling model
----------------
There is one FIFO frame sequence: instances are ordered by first-hop enqueue
instant, ties broken by submission order.  Hop 1 serves that sequence; hop 2
receives a frame exactly when hop 1 finishes it, so hop 2 preserves the same
order.  A frame starts at the earliest integer instant ``s >= now`` such that
``s + tx <= close`` of one upcoming gate window (finishing exactly at close is
allowed).  If a transmission duration is longer than every window, the frame
can never start under the whole-frame-in-window rule.

"Always on time" is decided rigorously by periodic-state detection.  Starting
from empty queues, the per-period finish residues form a component-wise
non-decreasing integer sequence; once a residue pair repeats, the whole future
schedule repeats shifted by whole periods, so no later miss can appear.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional

OFFSET_BOUND_US = 20
MAX_PERIOD_HARD_CAP = 100_000


@dataclass(frozen=True)
class GateWindow:
    """One non-period-crossing gate-open window per period, in local clock."""

    open_us: int
    close_us: int

    @property
    def length_us(self) -> int:
        return self.close_us - self.open_us


@dataclass(frozen=True)
class FrameSpec:
    number: int  # 1-based submission number
    enqueue_offset_us: int
    hop1_tx_us: int
    hop2_tx_us: int
    frame_id: Optional[str] = None


@dataclass(frozen=True)
class VerifyConfig:
    period_us: int
    deadline_us: int
    offset_min_us: int
    offset_max_us: int
    hop1_gate: GateWindow
    hop2_gate: GateWindow
    frames: tuple[FrameSpec, ...]


def earliest_gate_start(
    now: int,
    tx_us: int,
    open_phase_us: int,
    window_len_us: int,
    period_us: int,
) -> Optional[int]:
    """Earliest integer ``s >= now`` with ``s + tx`` inside one gate window.

    Windows on the unified timeline are
    ``[open_phase + k*T, open_phase + k*T + window_len]`` for every integer k.
    Returns ``None`` when ``tx_us`` can never fit a window.
    """
    if tx_us < 0:
        raise ValueError("transmission duration must be non-negative")
    if tx_us > window_len_us:
        return None

    # k of the window whose open is the last one <= now.
    k = (now - open_phase_us) // period_us
    for kk in (k, k + 1):
        opening = open_phase_us + kk * period_us
        closing = opening + window_len_us
        start = max(now, opening)
        if start + tx_us <= closing:
            return start
    # tx <= window_len guarantees the next opening works, so this is unreachable.
    return None


def _event_value(
    t: Optional[int], deadline_us: int, clock_shift_us: int
) -> Optional[dict[str, int]]:
    """Report an event only when it has occurred by the deadline instant."""
    if t is None or t > deadline_us:
        return None
    return {"timeline_us": t, "local_us": t + clock_shift_us}


def _planned_event(t: Optional[int], clock_shift_us: int) -> Optional[dict[str, int]]:
    if t is None:
        return None
    return {"timeline_us": t, "local_us": t + clock_shift_us}


def evaluate_offset(
    cfg: VerifyConfig, delta_us: int, include_trace: bool = False
) -> dict[str, Any]:
    """Run the continuous simulation for one fixed clock offset."""
    period = cfg.period_us
    deadline_rel = cfg.deadline_us

    # Fixed within-period service order: enqueue offset, then submission order.
    order = sorted(
        range(len(cfg.frames)),
        key=lambda i: (cfg.frames[i].enqueue_offset_us, i),
    )

    hop1_open = cfg.hop1_gate.open_us
    hop1_len = cfg.hop1_gate.length_us
    # Hop-2 local phase converted to unified timeline.
    hop2_open = cfg.hop2_gate.open_us - delta_us
    hop2_len = cfg.hop2_gate.length_us

    f1_prev: Optional[int] = None
    f2_prev: Optional[int] = None

    # residue pair -> period index where first seen
    seen_states: dict[tuple[int, int], int] = {}
    trace: list[dict[str, Any]] = []
    trace_truncated = False
    trace_frame_cap = 2000

    result: dict[str, Any]

    for k in range(MAX_PERIOD_HARD_CAP):
        miss_report: Optional[dict[str, Any]] = None

        for original_index in order:
            fr = cfg.frames[original_index]
            arrival = k * period + fr.enqueue_offset_us
            deadline_abs = arrival + deadline_rel

            # ---- Hop 1 -------------------------------------------------
            hop1_now = arrival if f1_prev is None else max(arrival, f1_prev)
            s1 = earliest_gate_start(
                hop1_now, fr.hop1_tx_us, hop1_open, hop1_len, period
            )
            f1: Optional[int] = None if s1 is None else s1 + fr.hop1_tx_us

            # ---- Hop 2 (enqueued exactly when hop 1 finishes) ----------
            enq2: Optional[int] = f1
            s2: Optional[int] = None
            f2: Optional[int] = None
            if f1 is not None:
                hop2_now = f1 if f2_prev is None else max(f1, f2_prev)
                s2 = earliest_gate_start(
                    hop2_now, fr.hop2_tx_us, hop2_open, hop2_len, period
                )
                if s2 is not None:
                    f2 = s2 + fr.hop2_tx_us

            on_time = f2 is not None and f2 <= deadline_abs

            if include_trace:
                if len(trace) < trace_frame_cap:
                    trace.append(
                        {
                            "period_index": k,
                            "period_no": k + 1,
                            "frame_index": original_index,
                            "frame_no": fr.number,
                            "frame_id": fr.frame_id,
                            "arrival_timeline_us": arrival,
                            "deadline_timeline_us": deadline_abs,
                            "hop1_start_timeline_us": s1,
                            "hop1_finish_timeline_us": f1,
                            "hop2_enqueue_timeline_us": enq2,
                            "hop2_start_timeline_us": s2,
                            "hop2_finish_timeline_us": f2,
                            "on_time": on_time,
                        }
                    )
                else:
                    trace_truncated = True

            if not on_time:
                if fr.hop1_tx_us > hop1_len:
                    reason = "hop1_frame_longer_than_gate_window"
                elif f1 is None:
                    reason = "hop1_no_feasible_gate_slot"
                elif fr.hop2_tx_us > hop2_len:
                    reason = "hop2_frame_longer_than_gate_window"
                elif f2 is None:
                    reason = "hop2_no_feasible_gate_slot"
                else:
                    reason = "delivery_after_deadline"

                miss_report = {
                    "frame_no": fr.number,
                    "frame_index": original_index,
                    "frame_id": fr.frame_id,
                    "period_index": k,
                    "period_no": k + 1,
                    "reason": reason,
                    "deadline": {
                        "timeline_us": deadline_abs,
                        "hop1_local_us": deadline_abs,
                        "hop2_local_us": deadline_abs + delta_us,
                    },
                    "clocks_at_deadline": {
                        "hop1_local_us": deadline_abs,
                        "hop2_local_us": deadline_abs + delta_us,
                    },
                    # Only events that have actually occurred by the deadline
                    # carry a value; future / impossible events are null.
                    "hop1": {
                        "enqueue_at": _event_value(arrival, deadline_abs, 0),
                        "start_at": _event_value(s1, deadline_abs, 0),
                        "finish_at": _event_value(f1, deadline_abs, 0),
                    },
                    "hop2": {
                        "enqueue_at": _event_value(enq2, deadline_abs, delta_us),
                        "start_at": _event_value(s2, deadline_abs, delta_us),
                        "finish_at": _event_value(f2, deadline_abs, delta_us),
                    },
                    # Full computed placement on the unified timeline, even
                    # when it lies after the deadline (null = impossible).
                    "planned": {
                        "hop1": {
                            "enqueue": _planned_event(arrival, 0),
                            "start": _planned_event(s1, 0),
                            "finish": _planned_event(f1, 0),
                        },
                        "hop2": {
                            "enqueue": _planned_event(enq2, delta_us),
                            "start": _planned_event(s2, delta_us),
                            "finish": _planned_event(f2, delta_us),
                        },
                        "delivery_finish_timeline_us": f2,
                    },
                    "late_by_us": (f2 - deadline_abs) if f2 is not None else None,
                }
                break

            # This frame is fully delivered on time: advance both servers.
            f1_prev = f1
            f2_prev = f2

        if miss_report is not None:
            result = {
                "offset_us": delta_us,
                "verdict": "deadline_miss",
                "always_on_time": False,
                "periods_simulated": k + 1,
                "first_miss": miss_report,
                "cycle": None,
            }
            if include_trace:
                result["trace"] = trace
                result["trace_truncated"] = trace_truncated
            return result

        # Period complete: residue of the final frame vs this period's zero.
        assert f1_prev is not None and f2_prev is not None
        residue = (f1_prev - k * period, f2_prev - k * period)

        if residue in seen_states:
            k0 = seen_states[residue]
            result = {
                "offset_us": delta_us,
                "verdict": "pass",
                "always_on_time": True,
                "periods_simulated": k + 1,
                "cycle": {
                    "first_seen_period_index": k0,
                    "first_seen_period_no": k0 + 1,
                    "repeated_period_index": k,
                    "repeated_period_no": k + 1,
                    "cycle_length_periods": k - k0,
                },
                "steady_residue_us": {
                    "hop1_last_finish_phase": residue[0],
                    "hop2_last_finish_phase": residue[1],
                },
                "first_miss": None,
            }
            if include_trace:
                result["trace"] = trace
                result["trace_truncated"] = trace_truncated
            return result

        seen_states[residue] = k

    # Defensive: unreachable under the integer bounded-residue argument while
    # every frame meets its deadline.
    result = {
        "offset_us": delta_us,
        "verdict": "inconclusive",
        "always_on_time": None,
        "periods_simulated": MAX_PERIOD_HARD_CAP,
        "first_miss": None,
        "cycle": None,
        "warning": "safety simulation cap reached without a repeated state",
    }
    if include_trace:
        result["trace"] = trace
        result["trace_truncated"] = True
    return result


def verify(cfg: VerifyConfig, include_trace: bool = False) -> dict[str, Any]:
    """Evaluate every integer offset in the configured range."""
    offsets = list(range(cfg.offset_min_us, cfg.offset_max_us + 1))
    results = [evaluate_offset(cfg, d, include_trace=include_trace) for d in offsets]

    miss_offsets = [r["offset_us"] for r in results if r["verdict"] == "deadline_miss"]
    pass_offsets = [r["offset_us"] for r in results if r["verdict"] == "pass"]
    inconclusive_offsets = [
        r["offset_us"] for r in results if r["verdict"] == "inconclusive"
    ]

    return {
        "all_offsets_pass": not miss_offsets and not inconclusive_offsets,
        "offset_count": len(offsets),
        "pass_count": len(pass_offsets),
        "miss_count": len(miss_offsets),
        "inconclusive_count": len(inconclusive_offsets),
        "pass_offsets_us": pass_offsets,
        "miss_offsets_us": miss_offsets,
        "inconclusive_offsets_us": inconclusive_offsets,
        "results": results,
    }
