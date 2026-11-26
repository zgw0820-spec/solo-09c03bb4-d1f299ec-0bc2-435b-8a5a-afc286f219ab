"""HTTP-level tests using FastAPI's TestClient."""

from __future__ import annotations

import sys
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.main import app  # noqa: E402

client = TestClient(app)

GOOD_PAYLOAD = {
    "period_us": 1000,
    "clock_offset_range_us": [-5, 5],
    "hop1_gate": {"open_us": 0, "close_us": 900},
    "hop2_gate": {"open_us": 20, "close_us": 920},
    "frames": [
        {"id": "cam-1", "enqueue_offset_us": 0,
         "hop1_tx_us": 120, "hop2_tx_us": 150},
        {"id": "cam-2", "enqueue_offset_us": 250,
         "hop1_tx_us": 80, "hop2_tx_us": 90},
    ],
}


def test_health():
    resp = client.get("/health")
    assert resp.status_code == 200
    assert resp.json() == {"status": "ok"}


def test_root_reports_service_info():
    resp = client.get("/")
    assert resp.status_code == 200
    body = resp.json()
    assert body["verify_endpoint"] == "/api/v1/verify"
    assert body["docs"] == "/docs"


def test_verify_happy_path_over_range():
    resp = client.post("/api/v1/verify", json=GOOD_PAYLOAD)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["offset_count"] == 11
    assert [r["offset_us"] for r in body["results"]] == list(range(-5, 6))
    assert body["request_summary"]["frame_count"] == 2
    for r in body["results"]:
        assert r["verdict"] in {"pass", "deadline_miss", "inconclusive"}


def test_short_route_alias_and_single_offset():
    payload = dict(GOOD_PAYLOAD)
    payload.pop("clock_offset_range_us")
    payload["clock_offset_us"] = 3
    resp = client.post("/verify", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["offset_count"] == 1
    assert body["results"][0]["offset_us"] == 3


def test_snake_short_aliases_accepted():
    payload = {
        "period": 100,
        "deadline": 100,
        "offset_range_us": [0, 0],
        "hop1_gate": {"open": 0, "close": 90},
        "hop2_gate": {"open": 0, "close": 90},
        "frames": [{"id": "f", "enqueue_us": 0, "tx1_us": 5, "tx2_us": 5}],
    }
    resp = client.post("/api/v1/verify", json=payload)
    assert resp.status_code == 200, resp.text
    assert resp.json()["all_offsets_pass"] is True


def test_trace_flag():
    payload = dict(GOOD_PAYLOAD)
    payload["include_trace"] = True
    resp = client.post("/api/v1/verify", json=payload)
    body = resp.json()
    assert "trace" in body["results"][0]
    assert body["results"][0]["trace"]


@pytest.mark.parametrize(
    "mutation, fragment",
    [
        ({"period_us": 1001}, "period_us"),
        ({"period_us": 0}, "period_us"),
        ({"clock_offset_range_us": [-21, 0]}, "-20"),
        ({"clock_offset_range_us": [0, 21]}, "-20"),
        ({"deadline_us": 1001}, "deadline"),
    ],
)
def test_invalid_scalar_values_rejected(mutation, fragment):
    payload = dict(GOOD_PAYLOAD)
    payload.update(mutation)
    resp = client.post("/api/v1/verify", json=payload)
    assert resp.status_code == 422
    assert fragment in resp.text


def test_too_many_frames_rejected():
    payload = dict(GOOD_PAYLOAD)
    payload["frames"] = [
        {"enqueue_offset_us": i, "hop1_tx_us": 1, "hop2_tx_us": 1}
        for i in range(5)
    ]
    resp = client.post("/api/v1/verify", json=payload)
    assert resp.status_code == 422


def test_gate_must_not_cross_period_and_shorter_than_period():
    payload = dict(GOOD_PAYLOAD)
    payload["hop1_gate"] = {"open_us": 800, "close_us": 1200}
    assert client.post("/api/v1/verify", json=payload).status_code == 422

    payload["hop1_gate"] = {"open_us": 0, "close_us": 1000}  # length == period
    assert client.post("/api/v1/verify", json=payload).status_code == 422


def test_enqueue_offset_must_be_inside_period():
    payload = dict(GOOD_PAYLOAD)
    payload["frames"] = [
        {"enqueue_offset_us": 1000, "hop1_tx_us": 1, "hop2_tx_us": 1}
    ]
    assert client.post("/api/v1/verify", json=payload).status_code == 422


def test_chinese_keys_and_nested_gates_accepted():
    payload = {
        "周期": 100,
        "时限": 100,
        "偏差范围": {"min": -1, "max": 1},
        "gates": {
            "hop1": {"开门时刻": 0, "关门时刻": 90},
            "hop2": {"open": 0, "end": 90},
        },
        "帧": [
            {"编号": "cam", "首跳入队时刻": 0, "一跳发送耗时": 5, "二跳发送耗时": 5}
        ],
    }
    resp = client.post("/api/v1/verify", json=payload)
    assert resp.status_code == 200, resp.text
    body = resp.json()
    assert body["offset_count"] == 3
    assert body["request_summary"]["frame_count"] == 1


def test_gate1_gate2_and_tx1_tx2_aliases():
    payload = {
        "period": 50,
        "offset_range": [0, 0],
        "gate1": {"start": 0, "end": 45},
        "gate2": {"start": 2, "end": 47},
        "frames": [{"enqueue_time_us": 1, "tx1": 4, "tx2": 4}],
    }
    resp = client.post("/api/v1/verify", json=payload)
    assert resp.status_code == 200, resp.text


def test_miss_payload_shape_end_to_end():
    payload = {
        "period_us": 1000,
        "deadline_us": 300,
        "clock_offset_us": 0,
        "hop1_gate": {"open_us": 0, "close_us": 400},
        "hop2_gate": {"open_us": 0, "close_us": 150},
        "frames": [
            {"id": "cam-trigger", "enqueue_offset_us": 0,
             "hop1_tx_us": 100, "hop2_tx_us": 100}
        ],
    }
    body = client.post("/api/v1/verify", json=payload).json()
    assert body["all_offsets_pass"] is False
    m = body["results"][0]["first_miss"]
    assert m["frame_no"] == 1
    assert m["period_no"] == 1
    assert m["hop1"]["finish_at"]["timeline_us"] == 100
    assert m["hop2"]["enqueue_at"]["local_us"] == 100
    assert m["hop2"]["start_at"] is None
