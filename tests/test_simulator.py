"""Hand-computed semantic tests for the verification engine."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.simulator import (  # noqa: E402
    FrameSpec,
    GateWindow,
    VerifyConfig,
    earliest_gate_start,
    evaluate_offset,
    verify,
)


def cfg(**kw) -> VerifyConfig:
    defaults = dict(
        period_us=1000,
        deadline_us=1000,
        offset_min_us=0,
        offset_max_us=0,
        hop1_gate=GateWindow(0, 900),
        hop2_gate=GateWindow(0, 900),
        frames=(FrameSpec(1, 0, 100, 100),),
    )
    defaults.update(kw)
    return VerifyConfig(**defaults)


# ---------------------------------------------------------------- gate math

def test_earliest_gate_start_basic_and_boundary():
    # window [0, 900], duration 100
    assert earliest_gate_start(0, 100, 0, 900, 1000) == 0
    assert earliest_gate_start(500, 100, 0, 900, 1000) == 500
    # finishing exactly at close is allowed (start 800 + 100 == 900)
    assert earliest_gate_start(800, 100, 0, 900, 1000) == 800
    # one microsecond later cannot fit -> next period open
    assert earliest_gate_start(801, 100, 0, 900, 1000) == 1000
    # duration exactly window length fits only at opening
    assert earliest_gate_start(50, 900, 0, 900, 1000) == 1000
    # longer than every window -> impossible
    assert earliest_gate_start(0, 901, 0, 900, 1000) is None


def test_shifted_hop2_phase():
    # delta = 10, local open 20 -> unified open phase 10
    assert earliest_gate_start(5, 10, 20 - 10, 60, 100) == 10


# ---------------------------------------------------------------- happy path

def test_simple_pass_immediate_fixed_point():
    r = evaluate_offset(cfg(), 0, include_trace=True)
    assert r["verdict"] == "pass"
    assert r["always_on_time"] is True
    # period 0 residue repeats at period 1 -> cycle length 1
    assert r["cycle"]["cycle_length_periods"] == 1
    assert r["cycle"]["first_seen_period_no"] == 1
    assert r["cycle"]["repeated_period_no"] == 2
    e0 = r["trace"][0]
    assert (e0["hop1_start_timeline_us"], e0["hop1_finish_timeline_us"]) == (0, 100)
    assert (e0["hop2_start_timeline_us"], e0["hop2_finish_timeline_us"]) == (100, 200)


def test_finish_exactly_at_gate_close_allowed():
    c = cfg(
        hop1_gate=GateWindow(0, 100),   # d1 = 100 exactly
        hop2_gate=GateWindow(0, 200),
        frames=(FrameSpec(1, 0, 100, 100),),
    )
    r = evaluate_offset(c, 0)
    assert r["verdict"] == "pass"


def test_delivery_exactly_at_deadline_is_on_time():
    c = cfg(
        period_us=10,
        deadline_us=5,
        hop1_gate=GateWindow(0, 9),
        hop2_gate=GateWindow(0, 9),
        frames=(FrameSpec(1, 0, 2, 3),),  # 0->2 hop1, hop2 2->5 == deadline
    )
    r = evaluate_offset(c, 0, include_trace=True)
    assert r["verdict"] == "pass"
    assert r["trace"][0]["hop2_finish_timeline_us"] == 5


# ---------------------------------------------------------------- miss cases

def test_hop2_defers_to_next_window_and_reports_snapshot():
    c = cfg(
        deadline_us=300,
        hop1_gate=GateWindow(0, 400),
        hop2_gate=GateWindow(0, 150),
        frames=(FrameSpec(1, 0, 100, 100),),
    )
    r = evaluate_offset(c, 0)
    assert r["verdict"] == "deadline_miss"
    m = r["first_miss"]
    assert m["frame_no"] == 1
    assert m["period_no"] == 1
    assert m["reason"] == "delivery_after_deadline"
    assert m["late_by_us"] == 800  # hop2 finish 1100 vs deadline 300

    # at deadline 300: hop1 all happened; hop2 enqueue at 100 happened,
    # but start (1000) / finish (1100) have not.
    assert m["hop1"]["enqueue_at"]["timeline_us"] == 0
    assert m["hop1"]["start_at"]["timeline_us"] == 0
    assert m["hop1"]["finish_at"]["timeline_us"] == 100
    assert m["hop2"]["enqueue_at"]["timeline_us"] == 100
    assert m["hop2"]["start_at"] is None
    assert m["hop2"]["finish_at"] is None
    assert m["planned"]["hop2"]["start"]["timeline_us"] == 1000
    assert m["planned"]["hop2"]["finish"]["timeline_us"] == 1100


def test_event_exactly_at_deadline_counts_as_occurred():
    # T=10: second frame starts hop1 exactly at deadline 10.
    c = cfg(
        period_us=10,
        deadline_us=10,
        hop1_gate=GateWindow(0, 9),
        hop2_gate=GateWindow(0, 9),
        frames=(
            FrameSpec(1, 0, 5, 0),
            FrameSpec(2, 0, 5, 0),
        ),
    )
    r = evaluate_offset(c, 0)
    # frame 2: 5->10 does not fit [0,9], deferred to 10->15, misses dl=10
    assert r["verdict"] == "deadline_miss"
    m = r["first_miss"]
    assert m["frame_no"] == 2
    assert m["period_no"] == 1
    assert m["hop1"]["enqueue_at"]["timeline_us"] == 0
    # start scheduled at 10 == deadline -> occurred
    assert m["hop1"]["start_at"]["timeline_us"] == 10
    assert m["hop1"]["finish_at"] is None  # 15 > 10
    assert m["hop2"]["enqueue_at"] is None


def test_frame_longer_than_window_never_starts():
    c = cfg(
        hop1_gate=GateWindow(0, 50),
        frames=(FrameSpec(1, 0, 51, 10),),
    )
    r = evaluate_offset(c, 0)
    m = r["first_miss"]
    assert m["reason"] == "hop1_frame_longer_than_gate_window"
    assert m["hop1"]["enqueue_at"] is not None
    assert m["hop1"]["start_at"] is None
    assert m["hop1"]["finish_at"] is None
    assert m["hop2"]["enqueue_at"] is None


def test_hop2_too_long_reports_hop1_done_hop2_never_starts():
    c = cfg(
        hop2_gate=GateWindow(0, 40),
        frames=(FrameSpec(1, 0, 10, 41),),
    )
    r = evaluate_offset(c, 0)
    m = r["first_miss"]
    assert m["reason"] == "hop2_frame_longer_than_gate_window"
    assert m["hop1"]["finish_at"]["timeline_us"] == 10
    assert m["hop2"]["enqueue_at"]["timeline_us"] == 10
    assert m["hop2"]["start_at"] is None


# ---------------------------------------------------------------- ordering

def test_same_enqueue_tie_break_is_submission_order():
    c = cfg(
        frames=(
            FrameSpec(1, 0, 3, 0),
            FrameSpec(2, 0, 3, 0),
        ),
    )
    r = evaluate_offset(c, 0, include_trace=True)
    t = r["trace"]
    assert [x["frame_no"] for x in t[:2]] == [1, 2]
    assert (t[0]["hop1_start_timeline_us"], t[0]["hop1_finish_timeline_us"]) == (0, 3)
    assert (t[1]["hop1_start_timeline_us"], t[1]["hop1_finish_timeline_us"]) == (3, 6)


def test_within_period_order_follows_offset_not_submission_index():
    c = cfg(
        frames=(
            FrameSpec(1, 8, 1, 0),
            FrameSpec(2, 2, 1, 0),
        ),
    )
    r = evaluate_offset(c, 0, include_trace=True)
    first_period = [x for x in r["trace"] if x["period_index"] == 0]
    assert [x["frame_no"] for x in first_period] == [2, 1]
    assert first_period[0]["arrival_timeline_us"] == 2
    assert first_period[1]["arrival_timeline_us"] == 8


# ---------------------------------------------------------------- clock offset

def test_offset_changes_hop2_local_timestamps_only():
    c = cfg(
        period_us=100,
        deadline_us=100,
        hop1_gate=GateWindow(0, 90),
        hop2_gate=GateWindow(20, 80),  # unified [10, 70] at delta=10
        frames=(FrameSpec(1, 0, 5, 10),),
    )
    r = evaluate_offset(c, 10)
    m_or_pass = r
    assert r["verdict"] == "pass"
    # inspect via a forced miss snapshot instead for local conversion:
    c2 = cfg(
        period_us=100,
        deadline_us=15,
        hop1_gate=GateWindow(0, 90),
        hop2_gate=GateWindow(20, 80),
        frames=(FrameSpec(1, 0, 5, 10),),
    )
    r2 = evaluate_offset(c2, 10)
    m = r2["first_miss"]
    # unified hop2 start 10 -> local 20
    assert m["hop2"]["start_at"]["timeline_us"] == 10
    assert m["hop2"]["start_at"]["local_us"] == 20
    assert m["deadline"]["hop2_local_us"] == 25  # 15 + delta


# ---------------------------------------------------------------- range verify

def test_verify_iterates_every_integer_offset_in_range():
    c = cfg(offset_min_us=-2, offset_max_us=2)
    rep = verify(c)
    assert [r["offset_us"] for r in rep["results"]] == [-2, -1, 0, 1, 2]
    assert rep["offset_count"] == 5
    assert rep["all_offsets_pass"] is True
    assert rep["miss_offsets_us"] == []


def test_miss_builds_up_over_periods():
    # Frozen from a search and cross-checked with the independent tick oracle:
    # first miss is frame 3 in period 3, deadline 300, delivery planned 340.
    c = cfg(
        period_us=100,
        deadline_us=100,
        hop1_gate=GateWindow(15, 89),
        hop2_gate=GateWindow(16, 95),
        frames=(
            FrameSpec(1, 99, 22, 33),
            FrameSpec(2, 42, 11, 14),
            FrameSpec(3, 0, 12, 24),
            FrameSpec(4, 56, 29, 12),
        ),
    )
    r = evaluate_offset(c, 0)
    assert r["verdict"] == "deadline_miss"
    m = r["first_miss"]
    assert (m["period_no"], m["frame_no"]) == (3, 3)
    assert m["deadline"]["timeline_us"] == 300
    assert m["late_by_us"] == 40
    assert m["hop1"]["start_at"]["timeline_us"] == 237
    assert m["hop1"]["finish_at"]["timeline_us"] == 249
    assert m["hop2"]["enqueue_at"]["timeline_us"] == 249
    assert m["hop2"]["start_at"] is None  # planned 316 > deadline 300
    assert m["planned"]["hop2"]["start"]["timeline_us"] == 316
    assert m["planned"]["hop2"]["finish"]["timeline_us"] == 340


def test_verify_collects_miss_offsets():
    # hop2 window [0, 150] local cannot fit after hop1 finish 100 for d2=100;
    # every offset in a small range still misses with deadline 300.
    c = cfg(
        deadline_us=300,
        offset_min_us=-3,
        offset_max_us=3,
        hop2_gate=GateWindow(0, 150),
        frames=(FrameSpec(1, 0, 100, 100),),
    )
    rep = verify(c)
    assert rep["miss_count"] == 7
    assert rep["all_offsets_pass"] is False
    assert len(rep["miss_offsets_us"]) == 7


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
