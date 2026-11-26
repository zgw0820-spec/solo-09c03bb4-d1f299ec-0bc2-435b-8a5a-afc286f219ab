"""Hand-computed tests for the diagnostic-frame reservation engine."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.booking import (  # noqa: E402
    RESERVATION_PERIODS,
    DrillStore,
    Interval,
    earliest_free_start,
    place_diagnostic_frame,
)
from app.simulator import FrameSpec, GateWindow, VerifyConfig  # noqa: E402


def make_cfg(**kw) -> VerifyConfig:
    defaults = dict(
        period_us=1000,
        deadline_us=1000,
        offset_min_us=0,
        offset_max_us=0,
        hop1_gate=GateWindow(0, 900),
        hop2_gate=GateWindow(0, 900),
        frames=(
            FrameSpec(1, 0, 100, 100, frame_id="cam-trigger"),
        ),
    )
    defaults.update(kw)
    return VerifyConfig(**defaults)


def open_drill(cfg: VerifyConfig | None = None, delta: int = 0):
    st = DrillStore()
    outcome = st.create_drill(cfg or make_cfg(), delta)
    assert outcome["created"] is True, outcome
    return st, outcome["drill"]


# ------------------------------------------------------------- Interval math

def test_intervals_that_merely_touch_do_not_overlap():
    assert Interval(0, 10).overlaps(Interval(10, 20)) is False
    assert Interval(0, 10).overlaps(Interval(5, 15)) is True
    assert Interval(5, 15).overlaps(Interval(0, 10)) is True
    assert Interval(0, 10).overlaps(Interval(11, 20)) is False


# ------------------------------------------------------- earliest_free_start

def test_earliest_free_start_respects_gate_and_blockers():
    # blocker starts before `now` so the first gate-legal start is inside it.
    blocked = [Interval(0, 120)]
    # gate [0,900] per period, blocker 0..120 -> jump to 120
    assert earliest_free_start(0, 30, 0, 900, 1000, blocked, 8000) == 120
    # a request whose fit is spoiled by the close waits for the next window
    blocked2 = [Interval(850, 870)]
    assert earliest_free_start(800, 100, 0, 900, 1000, blocked2, 8000) == 1000
    # horizon cut-off
    assert (
        earliest_free_start(7950, 100, 0, 900, 1000, [], 8000) is None
    )
    # longer than window -> impossible
    assert earliest_free_start(0, 901, 0, 900, 1000, [], 8000) is None


def test_earliest_free_start_skips_chain_of_blockers():
    # contiguous blockers from the gate open force the start past all of them
    blocked = [Interval(0, 10), Interval(10, 20), Interval(20, 40)]
    assert earliest_free_start(0, 5, 0, 900, 1000, blocked, 8000) == 40


# ------------------------------------------------------------- drill creation

def test_drill_creation_rejects_failing_configuration():
    st = DrillStore()
    # hop2 window 150 long cannot fit a 100us frame after hop1 finishes at 100
    # under deadline 300: it is deferred to the next window and misses.
    bad = make_cfg(
        deadline_us=300,
        hop2_gate=GateWindow(0, 150),
        frames=(FrameSpec(1, 0, 100, 100),),
    )
    outcome = st.create_drill(bad, 0)
    assert outcome["created"] is False
    assert outcome["verification"]["verdict"] == "deadline_miss"
    assert st.list_drills() == []


def test_drill_seeds_trigger_frame_occupancy_for_eight_periods():
    st, drill = open_drill()
    assert drill.horizon_us == 8 * 1000
    # trigger frame: hop1 [k*1000, k*1000+100), hop2 [k*1000+100, +200)
    assert len(drill.hop1_blocked) == 8
    assert len(drill.hop2_blocked) == 8
    assert drill.hop1_blocked[0] == Interval(0, 100)
    assert drill.hop2_blocked[0] == Interval(100, 200)
    assert drill.hop1_blocked[-1] == Interval(7000, 7100)
    assert drill.hop2_blocked[-1] == Interval(7100, 7200)


def test_drill_requires_single_fixed_offset_at_api_level():
    from app.schemas import DrillCreateRequest

    payload = {
        "period_us": 1000,
        "clock_offset_range_us": [-1, 1],
        "hop1_gate": {"open_us": 0, "close_us": 900},
        "hop2_gate": {"open_us": 0, "close_us": 900},
        "frames": [{"enqueue_offset_us": 0, "hop1_tx_us": 10, "hop2_tx_us": 10}],
    }
    with pytest.raises(ValueError):
        DrillCreateRequest(**payload)


# ------------------------------------------------------------- placement

def test_earliest_placement_skips_trigger_frame_on_both_hops():
    st, drill = open_drill()
    # diagnostic 30us per hop, no wait limit issue.
    outcome = st.reserve(drill.drill_id, 0, 900, 30, 30, 500, "diag-1")
    assert outcome["available"] is True
    r = outcome["reservation"]
    # hop1 cannot use 0..100 (trigger) -> 100..130; hop2 then 130..160, but
    # trigger occupies hop2 100..200 -> 200..230.
    assert (r.hop1.start, r.hop1.finish) == (100, 130)
    assert (r.hop2.start, r.hop2.finish) == (200, 230)
    assert r.relay_wait_us == 70
    assert r.period_no == 1


def test_reservation_occupies_both_hops_and_blocks_the_next_one():
    st, drill = open_drill()
    first = st.reserve(drill.drill_id, 100, 900, 30, 30, 500)
    assert first["available"] is True
    assert (first["reservation"].hop1.start,
            first["reservation"].hop1.finish) == (100, 130)

    second = st.reserve(drill.drill_id, 100, 900, 30, 30, 500)
    assert second["available"] is True
    r2 = second["reservation"]
    # hop1 must start after the first reservation's hop1 interval ends at 130
    assert r2.hop1.start >= 130
    assert (r2.hop1.start, r2.hop1.finish) == (130, 160)
    # hop2 blocked 100..200 (trigger) and 200..230 (first diag) -> 230..260
    assert (r2.hop2.start, r2.hop2.finish) == (230, 260)
    assert len(drill.reservations) == 2


def test_relay_wait_limit_violation_returns_no_slot():
    st, drill = open_drill()
    # Hop 1 blocked until 300 and hop 2 blocked until 400.  With a loose
    # finish bound the engine slides s1 forward (f1=400 -> wait 0); binding
    # the finish time to 430 removes that slack: the only placements are
    # f1 in [330, 400] with wait 400-f1, i.e. a minimum wait of 0 at f1=400
    # (s1=370).  A cap below 0 is impossible to express, so instead force the
    # minimum wait up by also blocking hop 1 between 370 and 400.
    drill.hop1_blocked.append(Interval(100, 300))
    drill.hop1_blocked.append(Interval(370, 400))
    drill.hop1_blocked.sort(key=lambda iv: iv.start)
    drill.hop2_blocked.append(Interval(200, 400))
    drill.hop2_blocked.sort(key=lambda iv: iv.start)

    # f1 <= 370 now; earliest hop-2 start 400 -> minimum wait 30.
    outcome = st.reserve(drill.drill_id, 100, 430, 30, 30, max_relay_wait_us=29)
    assert outcome["available"] is False
    assert outcome["reason"] == "relay_wait_limit_exceeded"
    assert len(drill.reservations) == 0  # nothing occupied on failure

    # the boundary value 30 succeeds
    ok = st.reserve(drill.drill_id, 100, 430, 30, 30, max_relay_wait_us=30)
    assert ok["available"] is True
    r = ok["reservation"]
    assert (r.hop1.start, r.hop1.finish) == (340, 370)
    assert (r.hop2.start, r.hop2.finish) == (400, 430)
    assert r.relay_wait_us == 30


def test_latest_finish_violation_returns_no_slot():
    st, drill = open_drill()
    # earliest delivery (from s1=100) finishes at 230; demand 229.
    outcome = st.reserve(drill.drill_id, 100, 229, 30, 30, 500)
    assert outcome["available"] is False
    assert outcome["reason"] in {
        "hop2_latest_finish_unreachable",
        "hop2_no_available_slot",
    }
    # and the boundary value 230 works
    ok = st.reserve(drill.drill_id, 100, 230, 30, 30, 500)
    assert ok["available"] is True
    assert ok["reservation"].hop2.finish == 230


def test_latest_finish_unreachable_reason_with_open_horizon():
    # With generous limits the horizon never binds, so a tight finish bound
    # reports the dedicated reason rather than hop2_no_available_slot.
    st, drill = open_drill()
    outcome = st.reserve(drill.drill_id, 100, 205, 30, 30, 500)
    assert outcome["available"] is False
    assert outcome["reason"] == "hop2_latest_finish_unreachable"


def test_earliest_start_constraint_is_honored():
    st, drill = open_drill()
    outcome = st.reserve(drill.drill_id, 250, 900, 30, 30, 500)
    assert outcome["available"] is True
    r = outcome["reservation"]
    assert r.hop1.start >= 250
    assert (r.hop1.start, r.hop1.finish) == (250, 280)
    # hop2 trigger 100..200 already past -> 280..310
    assert (r.hop2.start, r.hop2.finish) == (280, 310)


def test_relay_wait_violation_at_early_candidate_keeps_scanning():
    # The relay wait is not monotone in s1: an injected hop-2 blocker forces a
    # 70us wait on the earliest hop-1 candidate, but a later hop-1 start past
    # the blocker ends with wait 0.  The engine must keep scanning instead of
    # giving up after the first candidate.
    st, drill = open_drill()
    # Hop 1 free from 130 on; hop 2 blocked [130, 230).
    #   s1=130 -> f1=160, hop2 starts 230 -> wait 70 (over a 40us cap)
    #   s1=160 -> f1=190, hop2 starts 230 -> wait 40 (exactly the cap)
    drill.hop2_blocked.append(Interval(130, 230))
    drill.hop2_blocked.sort(key=lambda iv: iv.start)

    # A cap of 40 rejects the earliest candidate (wait 70) but the scan finds
    # s1=160 with wait 40; without the continue-on-violation logic this would
    # wrongly come back as unavailable.
    outcome = st.reserve(drill.drill_id, 100, 900, 30, 30, max_relay_wait_us=40)
    assert outcome["available"] is True, outcome
    r = outcome["reservation"]
    assert (r.hop1.start, r.hop1.finish) == (160, 190)
    assert (r.hop2.start, r.hop2.finish) == (230, 260)
    assert r.relay_wait_us == 40

    # With no wait slack at all (cap 0) the scan continues to s1=200, where
    # f1=230 meets the hop-2 opening exactly -> wait 0.
    st2, drill2 = open_drill()
    drill2.hop2_blocked.append(Interval(130, 230))
    drill2.hop2_blocked.sort(key=lambda iv: iv.start)
    zero_wait = st2.reserve(
        drill2.drill_id, 100, 900, 30, 30, max_relay_wait_us=0
    )
    assert zero_wait["available"] is True
    zr = zero_wait["reservation"]
    assert zr.relay_wait_us == 0
    assert (zr.hop1.start, zr.hop1.finish) == (200, 230)


def test_frame_longer_than_gate_window_rejected_without_side_effect():
    st, drill = open_drill()
    outcome = st.reserve(drill.drill_id, 0, 8000, 901, 10, 100)
    assert outcome["available"] is False
    assert outcome["reason"] == "frame_longer_than_gate_window"
    assert len(drill.reservations) == 0


def test_horizon_bounds_eight_periods_and_finish_at_horizon_allowed():
    st, drill = open_drill()
    # last period trigger: hop1 7000..7100, hop2 7100..7200.
    # A zero-duration frame at 7900 fits gate [7000,7900): starting exactly at
    # close is allowed for tx=0; hop2 likewise, finishing at the horizon edge.
    ok = st.reserve(drill.drill_id, 7900, 8000, 0, 0, 100)
    assert ok["available"] is True, ok
    r = ok["reservation"]
    assert r.hop1.start == 7900 and r.hop1.finish == 7900
    assert r.hop2.start == 7900 and r.hop2.finish == 7900
    assert r.relay_wait_us == 0
    # anything demanding a finish beyond the horizon is refused up front
    bad = st.reserve(drill.drill_id, 0, 8001, 10, 10, 100)
    assert bad["available"] is False
    assert bad["reason"] == "reservation_horizon_exceeded"


def test_no_slot_when_every_period_is_full_on_hop1():
    cfg = make_cfg(
        hop1_gate=GateWindow(0, 900),
        hop2_gate=GateWindow(0, 900),
        # trigger fills 800us of every hop-1 window
        frames=(FrameSpec(1, 0, 800, 10, frame_id="cam"),),
    )
    st, drill = open_drill(cfg)
    # only 100us remain per window; a 101us frame never fits in 8 periods
    outcome = st.reserve(drill.drill_id, 0, 8000, 101, 10, 500)
    assert outcome["available"] is False
    assert outcome["reason"] == "hop1_no_available_slot"


def test_place_is_side_effect_free():
    st, drill = open_drill()
    before = (len(drill.hop1_blocked), len(drill.hop2_blocked))
    result = place_diagnostic_frame(drill, 100, 900, 30, 30, 500)
    assert result["available"] is True
    assert (len(drill.hop1_blocked), len(drill.hop2_blocked)) == before


def test_clock_offset_shifts_hop2_window_and_local_timestamps():
    # hop2 local gate [40, 980]; delta=16 -> unified [24, 964].
    cfg = make_cfg(
        hop2_gate=GateWindow(40, 980),
        frames=(FrameSpec(1, 0, 100, 100, frame_id="cam"),),
    )
    st, drill = open_drill(cfg, delta=16)
    # trigger hop2 unified 100..200 -> local 116..216
    assert drill.hop2_blocked[0] == Interval(100, 200)
    outcome = st.reserve(drill.drill_id, 200, 900, 30, 30, 500)
    assert outcome["available"] is True
    r = outcome["reservation"]
    body = r.as_dict(16)
    assert body["hop2"]["start_timeline_us"] == r.hop2.start
    assert body["hop2"]["start_local_us"] == r.hop2.start + 16
    assert body["hop1"]["start_local_us"] == r.hop1.start


def test_reservation_uses_later_periods_when_early_ones_are_taken():
    st, drill = open_drill()
    # fill period 1 hop-1 gaps after the trigger: trigger 0..100; reserve the
    # rest of window 100..900 with one 800us frame (hop2 stays free later).
    big = st.reserve(drill.drill_id, 100, 7900, 800, 10, 7000)
    assert big["available"] is True, big
    assert big["reservation"].hop1 == Interval(100, 900)
    # next hop-1 request can no longer use period 1 -> period 2 at 1100
    nxt = st.reserve(drill.drill_id, 0, 7900, 30, 30, 500)
    assert nxt["available"] is True
    assert nxt["reservation"].hop1.start == 1100
    assert nxt["reservation"].period_no == 2


# ------------------------------------------------------------- concurrency

def test_concurrent_requests_never_overcommit_either_hop():
    st, drill = open_drill()
    results: list[dict] = []
    errors: list[BaseException] = []

    def worker() -> None:
        try:
            results.append(st.reserve(drill.drill_id, 100, 7900, 30, 30, 500))
        except BaseException as exc:  # pragma: no cover - test failure path
            errors.append(exc)

    threads = [threading.Thread(target=worker) for _ in range(20)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []

    accepted = [r for r in results if r["available"]]
    rejected = [r for r in results if not r["available"]]
    # Period-1 hop-1 window outside the trigger has room for exactly
    # (900-100)/30 = 26.6 -> 26 back-to-back 30us frames; later periods also
    # exist, so every request should be placeable, but intervals must never
    # overlap regardless of accept/reject counts.
    assert len(accepted) + len(rejected) == 20
    for hop in (drill.hop1_blocked, drill.hop2_blocked):
        ordered = sorted(hop, key=lambda iv: iv.start)
        for a, b in zip(ordered, ordered[1:]):
            assert a.finish <= b.start
    # the accepted reservations themselves are pairwise disjoint on each hop
    diag = [r["reservation"] for r in accepted]
    for i, ri in enumerate(diag):
        for rj in diag[i + 1:]:
            assert not ri.hop1.overlaps(rj.hop1)
            assert not ri.hop2.overlaps(rj.hop2)


def test_concurrent_requests_for_single_remaining_slot_only_one_wins():
    # Arrange exactly one remaining 30us hop-1 slot in the whole horizon:
    # the trigger takes 870us of every 900us hop-1 window (30us tail left),
    # while hop 2 stays generous so only hop 1 is the bottleneck.  The
    # configuration still verifies because the trigger's small hop-2 tx fits
    # its next-period window well within the deadline.
    cfg = make_cfg(
        hop1_gate=GateWindow(0, 900),
        hop2_gate=GateWindow(0, 999),
        frames=(FrameSpec(1, 0, 870, 10, frame_id="cam"),),
    )
    st, drill = open_drill(cfg)
    # trigger per period: hop1 [kT, kT+870); hop2 [kT+870, kT+880).
    # Free hop-1 tail per period is exactly [kT+870, kT+900), 30us wide.
    # Occupy the tail of the first seven periods.
    for k in range(RESERVATION_PERIODS - 1):
        out = st.reserve(
            drill.drill_id,
            earliest_start_us=k * 1000 + 870,
            latest_finish_us=8000,
            hop1_tx_us=30,
            hop2_tx_us=1,
            max_relay_wait_us=7000,
        )
        assert out["available"], out
    # Exactly the period-8 tail remains.
    remaining = [iv for iv in drill.hop1_blocked if iv.start >= 7000]
    assert Interval(7000, 7870) in remaining
    barrier = threading.Barrier(4)
    outcomes: list[dict] = []

    def worker() -> None:
        barrier.wait()
        outcomes.append(
            st.reserve(drill.drill_id, 0, 8000, 30, 1, 7000)
        )

    threads = [threading.Thread(target=worker) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    wins = [o for o in outcomes if o["available"]]
    losses = [o for o in outcomes if not o["available"]]
    assert len(wins) == 1
    assert len(losses) == 3
    assert all(o["reason"] == "hop1_no_available_slot" for o in losses)
    assert wins[0]["reservation"].hop1 == Interval(7870, 7900)


# ------------------------------------------------------------- snapshot

def test_snapshot_reports_all_reservations_and_occupancy():
    st, drill = open_drill()
    st.reserve(drill.drill_id, 100, 900, 30, 30, 500, frame_id="diag")
    snap = st.snapshot(drill.drill_id)
    assert snap["reservation_count"] == 1
    assert snap["reservations"][0]["frame_id"] == "diag"
    # 8 trigger intervals + 1 reservation on each hop
    assert len(snap["occupied_hop1_us"]) == 9
    assert len(snap["occupied_hop2_us"]) == 9
    assert st.snapshot("does-not-exist") is None
