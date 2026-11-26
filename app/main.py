"""FastAPI service for two-hop TSN gate delivery verification."""

from __future__ import annotations

from fastapi import FastAPI

from .schemas import VerifyRequest
from .simulator import verify

app = FastAPI(
    title="TSN Two-Hop Delivery Verifier",
    version="1.0.0",
    description=(
        "Before changing a production-line gate table, verify that periodic "
        "camera-trigger frames can always be delivered on time across two TSN "
        "hops for every fixed clock2-minus-clock1 offset in the submitted "
        "range. See /docs for the request schema and /health for liveness."
    ),
)


@app.get("/", include_in_schema=False)
def root() -> dict[str, object]:
    return {
        "service": "tsn-two-hop-verifier",
        "version": "1.0.0",
        "docs": "/docs",
        "health": "/health",
        "verify_endpoint": "/api/v1/verify",
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
