"""FastAPI service: two-hop TSN delivery verification and diagnostic
frame reservation drills."""

from __future__ import annotations

from fastapi import FastAPI, HTTPException

from .booking import RESERVATION_PERIODS, store
from .schemas import DrillCreateRequest, ReservationRequest, VerifyRequest
from .simulator import verify

app = FastAPI(
    title="TSN Two-Hop Delivery Verifier & Reservation Service",
    version="1.1.0",
    description=(
        "Before changing a production-line gate table, verify that periodic "
        "camera-trigger frames can always be delivered on time across two TSN "
        "hops, and reserve idle windows for diagnostic frames without moving "
        "the camera-trigger schedule. See /docs for the request schemas and "
        "/health for liveness."
    ),
)


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    return {
        "service": "tsn-two-hop-verifier",
        "version": "1.1.0",
        "docs": "/docs",
        "health": "/health",
        "verify_endpoint": "/api/v1/verify",
        "drills_endpoint": "/api/v1/drills",
        "method": "POST",
    }


@app.get("/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


@app.post("/api/v1/verify")
def verify_v1(req: VerifyRequest) -> dict:
    """Evaluate all integer clock offsets in the configured range."""
    cfg = req.to_config()
    report = verify(cfg, include_trace=req.include_trace)
    report["request_summary"] = {
        "period_us": cfg.period_us,
        "deadline_us": cfg.deadline_us,
        "offset_range_us": [cfg.offset_min_us, cfg.offset_max_us],
        "frame_count": len(cfg.frames),
        "hop1_gate_us": [cfg.hop1_gate.open_us, cfg.hop1_gate.close_us],
        "hop2_gate_us": [cfg.hop2_gate.open_us, cfg.hop2_gate.close_us],
    }
    return report


# Convenience alias for callers that prefer a short path.
app.add_api_route("/verify", verify_v1, methods=["POST"], include_in_schema=False)


# --------------------------------------------------------------------- drills
@app.post("/api/v1/drills")
def create_drill(req: DrillCreateRequest) -> dict:
    """Create a reservation drill from the two-hop config and one fixed offset.

    The configuration must pass verification for that offset; otherwise the
    drill is rejected with the first-miss report.  On success the first eight
    periods (counted from empty queues) are reservable, and the camera-trigger
    send intervals inside that horizon become the initial occupancy.
    """
    cfg = req.to_config()
    delta = req.offset_min_us
    outcome = store.create_drill(cfg, delta)
    if not outcome["created"]:
        raise HTTPException(
            status_code=422,
            detail={
                "error": "configuration_verification_failed",
                "message": (
                    "the camera-trigger configuration does not always meet its "
                    "deadline at the requested fixed clock offset; no drill "
                    "was created"
                ),
                "verification": outcome["verification"],
            },
        )

    drill = outcome["drill"]
    return {
        "drill_id": drill.drill_id,
        "clock_offset_us": delta,
        "period_us": cfg.period_us,
        "reservation_periods": RESERVATION_PERIODS,
        "horizon_us": drill.horizon_us,
        "verification": {
            "verdict": outcome["verification"]["verdict"],
            "always_on_time": outcome["verification"]["always_on_time"],
            "cycle": outcome["verification"].get("cycle"),
        },
        "config_summary": {
            "deadline_us": cfg.deadline_us,
            "hop1_gate_us": [cfg.hop1_gate.open_us, cfg.hop1_gate.close_us],
            "hop2_gate_us": [cfg.hop2_gate.open_us, cfg.hop2_gate.close_us],
            "frame_count": len(cfg.frames),
        },
        "trigger_schedule": outcome["trigger_schedule"],
        "reservation_endpoint": f"/api/v1/drills/{drill.drill_id}/reservations",
    }


@app.get("/api/v1/drills")
def list_drills() -> dict:
    drills = [
        {
            "drill_id": d.drill_id,
            "clock_offset_us": d.clock_offset_us,
            "period_us": d.period_us,
            "horizon_us": d.horizon_us,
            "reservation_count": len(d.reservations),
        }
        for d in store.list_drills()
    ]
    return {"drill_count": len(drills), "drills": drills}


@app.get("/api/v1/drills/{drill_id}")
def get_drill(drill_id: str) -> dict:
    snap = store.snapshot(drill_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="drill not found")
    return snap


@app.post("/api/v1/drills/{drill_id}/reservations")
def create_reservation(drill_id: str, req: ReservationRequest) -> dict:
    """Reserve one diagnostic frame inside an existing drill.

    Placement is atomic across both hops: a concurrent request can only take
    the slot if both hops remain free; otherwise the caller gets
    ``available=false`` and no interval is occupied.
    """
    if store.get(drill_id) is None:
        raise HTTPException(status_code=404, detail="drill not found")

    outcome = store.reserve(
        drill_id,
        earliest_start_us=req.hop1_earliest_start_us,
        latest_finish_us=req.hop2_latest_finish_us,
        hop1_tx_us=req.hop1_tx_us,
        hop2_tx_us=req.hop2_tx_us,
        max_relay_wait_us=req.max_relay_wait_us,
        frame_id=req.frame_id,
    )
    if outcome is None:  # race with deletion is impossible today; defensive
        raise HTTPException(status_code=404, detail="drill not found")

    if not outcome["available"]:
        return {
            "drill_id": drill_id,
            "available": False,
            "reason": outcome["reason"],
            "detail": outcome["detail"],
            "reservation": None,
        }

    r = outcome["reservation"]
    drill = store.get(drill_id)
    return {
        "drill_id": drill_id,
        "available": True,
        "reason": None,
        "reservation": r.as_dict(drill.clock_offset_us),
    }


@app.get("/api/v1/drills/{drill_id}/reservations")
def list_reservations(drill_id: str) -> dict:
    snap = store.snapshot(drill_id)
    if snap is None:
        raise HTTPException(status_code=404, detail="drill not found")
    return {
        "drill_id": drill_id,
        "reservation_count": snap["reservation_count"],
        "reservations": snap["reservations"],
    }
