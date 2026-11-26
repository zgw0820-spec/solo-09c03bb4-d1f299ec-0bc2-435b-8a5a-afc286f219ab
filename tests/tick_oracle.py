"""Independent integer-tick oracle used only by the test-suite.

It models two FIFO queues/servers tick by tick, deliberately written in a
different style from app.simulator so cross-checking has value.
"""

from __future__ import annotations

from collections import deque
from typing import Any, Optional


def gate_allows_start(
    t: int, tx: int, open_phase: int, win_len: int, period: int
) -> bool:
    k = (t - open_phase) // period
    for kk in (k - 1, k, k + 1):
        lo = open_phase + kk * period
        hi = lo + win_len
        if lo <= t and t + tx <= hi:
            return True
    return False


def tick_simulate(cfg, delta: int, periods: int) -> dict[tuple[int, int], dict]:
    """Return recorded event times for instances (period_index, frame_index)."""
    T = cfg.period_us
    hop2_open = cfg.hop2_gate.open_us - delta
    hop2_len = cfg.hop2_gate.length_us

    # global arrival order
    instances: list[tuple[int, int, int]] = []  # arr, k, frame_index
    for k in range(periods):
        for i, fr in enumerate(cfg.frames):
            instances.append((k * T + fr.enqueue_offset_us, k, i))
    instances.sort(key=lambda x: (x[0], x[2]))

    horizon = periods * T + 5 * T + 100
    by_time: dict[int, list[tuple[int, int]]] = {}
    for arr, k, i in instances:
        by_time.setdefault(arr, []).append((k, i))
    for t in by_time:
        by_time[t].sort(key=lambda ki: ki[1])

    q1: deque[tuple[int, int]] = deque()
    q2: deque[tuple[int, int]] = deque()
    busy1: Optional[tuple[tuple[int, int], int]] = None
    busy2: Optional[tuple[tuple[int, int], int]] = None
    ev: dict[tuple[int, int], dict[str, Optional[int]]] = {}

    def rec(key):
        return ev.setdefault(
            key, {"enq1": None, "s1": None, "f1": None,
                  "enq2": None, "s2": None, "f2": None}
        )

    for t in range(horizon + 1):
        # arrivals first
        for key in by_time.get(t, []):
            q1.append(key)
            rec(key)["enq1"] = t

        # completions free servers and (hop1) enqueue hop2
        if busy1 is not None and busy1[1] == t:
            key = busy1[0]
            rec(key)["f1"] = t
            q2.append(key)
            rec(key)["enq2"] = t
            busy1 = None
        if busy2 is not None and busy2[1] == t:
            rec(busy2[0])["f2"] = t
            busy2 = None

        # start hop2 (after hop1 completion at same tick)
        if busy2 is None and q2:
            key = q2[0]
            i = key[1]
            d = cfg.frames[i].hop2_tx_us
            if gate_allows_start(t, d, hop2_open, hop2_len, T):
                q2.popleft()
                rec(key)["s2"] = t
                busy2 = (key, t + d)

        # start hop1
        if busy1 is None and q1:
            key = q1[0]
            i = key[1]
            d = cfg.frames[i].hop1_tx_us
            if gate_allows_start(t, d, cfg.hop1_gate.open_us,
                                 cfg.hop1_gate.length_us, T):
                q1.popleft()
                rec(key)["s1"] = t
                busy1 = (key, t + d)

    return ev


def oracle_first_miss(cfg, delta: int, periods: int = 400) -> Optional[dict[str, Any]]:
    ev = tick_simulate(cfg, delta, periods)
    T = cfg.period_us
    D = cfg.deadline_us
    misses = []
    for (k, i), e in ev.items():
        fr = cfg.frames[i]
        arr = k * T + fr.enqueue_offset_us
        dl = arr + D
        if e["f2"] is None or e["f2"] > dl:
            misses.append((dl, k, i, e))
    if not misses:
        return None
    misses.sort(key=lambda x: (x[0], x[1], x[2]))
    dl, k, i, e = misses[0]
    return {"deadline": dl, "period_index": k, "frame_index": i, "events": e}
