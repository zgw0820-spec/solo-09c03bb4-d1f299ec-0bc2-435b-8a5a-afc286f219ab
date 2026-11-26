"""HTTP-level tests for the drill / reservation API."""

from __future__ import annotations

import sys
import threading
from pathlib import Path

from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

client = TestClient(app)

DRILL_PAYLOAD = {
    "period_us": 1000,
    "clock_offset_us": 0,
    "hop1_gate": {"open_us": 0, "close_us": 900},
    "hop2_gate": {"open_us": 0, "close_us": 900},
    "frames": [
        {"id": "cam-trigger", "enqueue_offset_us": 0,
         "hop1_tx_us": 100, "hop2_tx_us": 100}
    ],
}


def _create_drill(payload=None):
    resp = client.post("/api/v1/drills", json=payload or DRILL_PAYLOAD)
    assert resp.status_code == 200, resp.text
    return resp.json()


def test_drill_creation_happy_path_and_trigger_schedule():
    body = _create_drill()
    assert body["clock_offset_us"] == 0
    assert body["reservation_periods"] == 8
    assert body["horizon_us"] == 8000
    assert body["verification"]["verdict"] == "pass"
    # 8 periods x 1 trigger frame
    assert len(body["trigger_schedule"]) == 8
    first = body["trigger_schedule"][0]
    assert first["hop1"] == {"start_timeline_us": 0, "finish_timeline_us": 100}
    assert first["hop2"]["start_timeline_us"] == 100
    assert body["trigger_schedule"][-1]["period_no"] == 8


def test_drill_creation_requires_single_fixed_offset():
    payload = dict(DRILL_PAYLOAD)
    payload.pop("clock_offset_us")
    payload["clock_offset_range_us"] = [-1, 1]
    resp = client.post("/api/v1/drills", json=payload)
    assert resp.status_code == 422
    assert "fixed clock offset" in resp.text


def test_drill_creation_rejects_failing_configuration():
    payload = {
        "period_us": 1000,
        "deadline_us": 300,
        "clock_offset_us": 0,
        "hop1_gate": {"open_us": 0, "close_us": 500},
        "hop2_gate": {"open_us": 0, "close_us": 150},
        "frames": [
            {"id": "cam", "enqueue_offset_us": 0,
             "hop1_tx_us": 100, "hop2_tx_us": 100}
        ],
    }
    resp = client.post("/api/v1/drills", json=payload)
    assert resp.status_code == 422
    detail = resp.json()["detail"]
    assert detail["error"] == "configuration_verification_failed"
    assert detail["verification"]["verdict"] == "deadline_miss"


def test_drill_listing_and_snapshot():
    drill = _create_drill()
    listing = client.get("/api/v1/drills").json()
    assert any(d["drill_id"] == drill["drill_id"] for d in listing["drills"])

    snap = client.get(f"/api/v1/drills/{drill['drill_id']}").json()
    assert snap["reservation_count"] == 0
    # 8 trigger intervals pre-occupied on each hop
    assert len(snap["occupied_hop1_us"]) == 8
    assert len(snap["occupied_hop2_us"]) == 8

    assert client.get("/api/v1/drills/nope").status_code == 404


def test_reservation_happy_path_returns_unified_timestamps():
    drill = _create_drill()
    resp = client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations",
        json={
            "frame_id": "diag-1",
            "hop1_earliest_start_us": 0,
            "hop2_latest_finish_us": 900,
            "hop1_tx_us": 30,
            "hop2_tx_us": 30,
            "max_relay_wait_us": 500,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    r = body["reservation"]
    assert r["frame_id"] == "diag-1"
    assert r["period_no"] == 1
    assert r["hop1"]["start_timeline_us"] == 100
    assert r["hop1"]["finish_timeline_us"] == 130
    assert r["hop2"]["start_timeline_us"] == 200
    assert r["hop2"]["finish_timeline_us"] == 230
    assert r["relay_wait_us"] == 70

    listing = client.get(
        f"/api/v1/drills/{drill['drill_id']}/reservations"
    ).json()
    assert listing["reservation_count"] == 1


def test_reservation_no_slot_is_200_with_reason_and_occupies_nothing():
    drill = _create_drill()
    # A finish bound before the earliest possible delivery (230) leaves no
    # feasible hop-2 placement inside the horizon.
    resp = client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations",
        json={
            "hop1_earliest_start_us": 100,
            "hop2_latest_finish_us": 229,
            "hop1_tx_us": 30,
            "hop2_tx_us": 30,
            "max_relay_wait_us": 500,
        },
    )
    assert resp.status_code == 200
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] in {
        "hop2_latest_finish_unreachable",
        "hop2_no_available_slot",
    }
    assert body["reservation"] is None

    snap = client.get(f"/api/v1/drills/{drill['drill_id']}").json()
    assert snap["reservation_count"] == 0


def test_reservation_latest_finish_reason_with_open_horizon():
    drill = _create_drill()
    resp = client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations",
        json={
            "hop1_earliest_start_us": 100,
            "hop2_latest_finish_us": 205,
            "hop1_tx_us": 30,
            "hop2_tx_us": 30,
            "max_relay_wait_us": 500,
        },
    )
    body = resp.json()
    assert body["available"] is False
    assert body["reason"] == "hop2_latest_finish_unreachable"


def test_reservation_unknown_drill_404():
    resp = client.post(
        "/api/v1/drills/nope/reservations",
        json={
            "hop1_earliest_start_us": 0,
            "hop2_latest_finish_us": 900,
            "hop1_tx_us": 10,
            "hop2_tx_us": 10,
            "max_relay_wait_us": 100,
        },
    )
    assert resp.status_code == 404


def test_reservation_validation_errors():
    drill = _create_drill()
    base = {
        "hop1_earliest_start_us": 100,
        "hop2_latest_finish_us": 900,
        "hop1_tx_us": 30,
        "hop2_tx_us": 30,
        "max_relay_wait_us": 500,
    }
    # negative duration
    bad = dict(base, hop1_tx_us=-1)
    assert client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations", json=bad
    ).status_code == 422
    # latest finish not after earliest start
    bad = dict(base, hop2_latest_finish_us=100)
    assert client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations", json=bad
    ).status_code == 422
    # missing field
    bad = dict(base)
    del bad["max_relay_wait_us"]
    assert client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations", json=bad
    ).status_code == 422


def test_chinese_reservation_keys_accepted():
    drill = _create_drill()
    resp = client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations",
        json={
            "编号": "诊断帧",
            "首跳最早起发时刻": 100,
            "二跳最晚发完时刻": 900,
            "一跳发送耗时": 30,
            "二跳发送耗时": 30,
            "中转等待上限": 500,
        },
    )
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["available"] is True
    assert body["reservation"]["frame_id"] == "诊断帧"


def test_clock_offset_propagates_to_local_timestamps():
    payload = {
        "period_us": 1000,
        "clock_offset_us": 16,
        "hop1_gate": {"open_us": 0, "close_us": 900},
        "hop2_gate": {"open_us": 40, "close_us": 980},
        "frames": [
            {"id": "cam", "enqueue_offset_us": 0,
             "hop1_tx_us": 100, "hop2_tx_us": 100}
        ],
    }
    drill = _create_drill(payload)
    resp = client.post(
        f"/api/v1/drills/{drill['drill_id']}/reservations",
        json={
            "hop1_earliest_start_us": 200,
            "hop2_latest_finish_us": 900,
            "hop1_tx_us": 30,
            "hop2_tx_us": 30,
            "max_relay_wait_us": 500,
        },
    )
    body = resp.json()
    assert body["available"] is True, body
    r = body["reservation"]
    assert r["hop2"]["start_local_us"] == r["hop2"]["start_timeline_us"] + 16
    assert r["hop1"]["start_local_us"] == r["hop1"]["start_timeline_us"]


def test_concurrent_http_requests_keep_intervals_disjoint():
    drill = _create_drill()
    url = f"/api/v1/drills/{drill['drill_id']}/reservations"
    payload = {
        "hop1_earliest_start_us": 100,
        "hop2_latest_finish_us": 7900,
        "hop1_tx_us": 30,
        "hop2_tx_us": 30,
        "max_relay_wait_us": 500,
    }
    outcomes: list[dict] = []
    barrier = threading.Barrier(8)

    def worker():
        barrier.wait()
        outcomes.append(client.post(url, json=payload).json())

    threads = [threading.Thread(target=worker) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert len(outcomes) == 8
    assert all(o["available"] for o in outcomes)
    snap = client.get(f"/api/v1/drills/{drill['drill_id']}").json()
    assert snap["reservation_count"] == 8
    for key in ("occupied_hop1_us", "occupied_hop2_us"):
        intervals = sorted(snap[key])
        for a, b in zip(intervals, intervals[1:]):
            assert a[1] <= b[0]
