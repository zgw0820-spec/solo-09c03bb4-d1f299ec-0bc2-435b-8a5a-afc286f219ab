"""Property cross-checks: analytic engine vs independent tick oracle."""

from __future__ import annotations

import random
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from app.simulator import FrameSpec, GateWindow, VerifyConfig, evaluate_offset  # noqa
from tick_oracle import oracle_first_miss, tick_simulate  # noqa


def make_config(rng: random.Random) -> VerifyConfig:
    T = rng.choice([8, 10, 12, 20, 25])
    n = rng.randint(1, 4)
    frames = []
    for i in range(1, n + 1):
        frames.append(
            FrameSpec(
                number=i,
                enqueue_offset_us=rng.randint(0, T - 1),
                hop1_tx_us=rng.randint(1, T // 2 + 2),
                hop2_tx_us=rng.randint(1, T // 2 + 2),
            )
        )
    o1 = rng.randint(0, T - 2)
    c1 = rng.randint(o1 + 1, T)
    o2 = rng.randint(0, T - 2)
    c2 = rng.randint(o2 + 1, T)
    D = rng.randint(max(1, T // 2), T)
    return VerifyConfig(
        period_us=T,
        deadline_us=D,
        offset_min_us=0,
        offset_max_us=0,
        hop1_gate=GateWindow(o1, c1),
        hop2_gate=GateWindow(o2, c2),
        frames=tuple(frames),
    )


def _planned_events_match(cfg, delta, result, periods=6):
    """Compare analytic trace against tick oracle for several periods."""
    ev = tick_simulate(cfg, delta, periods)
    r = evaluate_offset(cfg, delta, include_trace=True)
    for row in r["trace"]:
        key = (row["period_index"], row["frame_index"])
        if key not in ev:
            continue
        o = ev[key]
        assert row["hop1_start_timeline_us"] == o["s1"], (key, row, o)
        assert row["hop1_finish_timeline_us"] == o["f1"], (key, row, o)
        assert row["hop2_enqueue_timeline_us"] == o["enq2"], (key, row, o)
        assert row["hop2_start_timeline_us"] == o["s2"], (key, row, o)
        assert row["hop2_finish_timeline_us"] == o["f2"], (key, row, o)


@pytest.mark.parametrize("seed", list(range(120)))
def test_engine_matches_tick_oracle_on_random_configs(seed):
    rng = random.Random(seed)
    cfg = make_config(rng)
    for delta in (-20, -7, 0, 7, 20):
        result = evaluate_offset(cfg, delta)
        oracle_miss = oracle_first_miss(cfg, delta, periods=300)

        if result["verdict"] == "pass":
            assert oracle_miss is None, (
                f"seed {seed} delta {delta}: engine passed but oracle found "
                f"{oracle_miss}"
            )
            _planned_events_match(cfg, delta, result)
        else:
            assert result["verdict"] == "deadline_miss"
            assert oracle_miss is not None, (
                f"seed {seed} delta {delta}: engine reported miss but oracle "
                "found none"
            )
            m = result["first_miss"]
            assert m["period_index"] == oracle_miss["period_index"], seed
            assert m["frame_index"] == oracle_miss["frame_index"], seed
            assert m["deadline"]["timeline_us"] == oracle_miss["deadline"], seed

            oe = oracle_miss["events"]
            planned = m["planned"]
            p_s1 = planned["hop1"]["start"]
            p_f1 = planned["hop1"]["finish"]
            p_s2 = planned["hop2"]["start"]
            p_f2 = planned["hop2"]["finish"]
            assert (p_s1["timeline_us"] if p_s1 else None) == oe["s1"]
            assert (p_f1["timeline_us"] if p_f1 else None) == oe["f1"]
            assert (p_s2["timeline_us"] if p_s2 else None) == oe["s2"]
            assert (p_f2["timeline_us"] if p_f2 else None) == oe["f2"]


def test_pass_verdict_holds_for_long_oracle_horizon():
    # A few fixed seeds that happen to pass must stay good over 500 periods.
    for seed in range(40):
        rng = random.Random(1000 + seed)
        cfg = make_config(rng)
        result = evaluate_offset(cfg, 0)
        if result["verdict"] == "pass":
            assert oracle_first_miss(cfg, 0, periods=500) is None


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-v"]))
