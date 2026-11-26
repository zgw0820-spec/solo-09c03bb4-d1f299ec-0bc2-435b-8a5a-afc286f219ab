"""HTTP request/response schemas and conversion to the engine config."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict, Field, model_validator
from pydantic.aliases import AliasChoices

from .simulator import OFFSET_BOUND_US, FrameSpec, GateWindow, VerifyConfig

# Best-effort compatibility for callers using the Chinese spec vocabulary or
# slightly different nesting.  Canonical English fields always win.
_TOP_KEY_MAP = {
    "周期": "period_us",
    "重复周期": "period_us",
    "cycle": "period_us",
    "interval_us": "period_us",
    "period_duration_us": "period_us",
    "时限": "deadline_us",
    "送达时限": "deadline_us",
    "deadline_rel_us": "deadline_us",
    "偏差": "clock_offset_us",
    "时钟偏差": "clock_offset_us",
    "偏差范围": "clock_offset_range_us",
    "时钟偏差范围": "clock_offset_range_us",
    "offset_range": "clock_offset_range_us",
    "offset_range_us": "clock_offset_range_us",
    "一跳门窗": "hop1_gate",
    "首跳门窗": "hop1_gate",
    "gate1": "hop1_gate",
    "window1": "hop1_gate",
    "hop1_window_us": "hop1_gate",
    "二跳门窗": "hop2_gate",
    "第二跳门窗": "hop2_gate",
    "gate2": "hop2_gate",
    "window2": "hop2_gate",
    "hop2_window_us": "hop2_gate",
    "帧": "frames",
    "帧配置": "frames",
    "相机帧": "frames",
    "frame_list": "frames",
    "frame_configs": "frames",
}

_GATE_KEY_MAP = {
    "开门": "open_us",
    "开门时刻": "open_us",
    "open": "open_us",
    "start": "open_us",
    "start_us": "open_us",
    "关门": "close_us",
    "关门时刻": "close_us",
    "close": "close_us",
    "end": "close_us",
    "end_us": "close_us",
}

_FRAME_KEY_MAP = {
    "编号": "frame_id",
    "名称": "frame_id",
    "入队时刻": "enqueue_offset_us",
    "首跳入队时刻": "enqueue_offset_us",
    "入队偏移": "enqueue_offset_us",
    "enqueue_time_us": "enqueue_offset_us",
    "enqueue_at_us": "enqueue_offset_us",
    "arrival_time_us": "enqueue_offset_us",
    "arrival_offset_us": "enqueue_offset_us",
    "一跳耗时": "hop1_tx_us",
    "首跳耗时": "hop1_tx_us",
    "一跳发送耗时": "hop1_tx_us",
    "tx1": "hop1_tx_us",
    "duration1_us": "hop1_tx_us",
    "send1_us": "hop1_tx_us",
    "二跳耗时": "hop2_tx_us",
    "第二跳耗时": "hop2_tx_us",
    "二跳发送耗时": "hop2_tx_us",
    "tx2": "hop2_tx_us",
    "duration2_us": "hop2_tx_us",
    "send2_us": "hop2_tx_us",
}


def _remap(d: Any, mapping: dict[str, str]) -> Any:
    if not isinstance(d, dict):
        return d
    out = dict(d)
    for old, new in mapping.items():
        if old in out and new not in out:
            out[new] = out.pop(old)
    return out


def normalize_payload(raw: Any) -> Any:
    """Apply additive key/nesting compatibility before Pydantic validation."""
    if not isinstance(raw, dict):
        return raw
    data = dict(raw)

    # Accept {"gates": {"hop1": ..., "hop2": ...}} / {"gates": [g1, g2]}.
    gates = data.pop("gates", None) or data.pop("gate_windows", None)
    if isinstance(gates, dict):
        if "hop1_gate" not in data and "hop1" in gates:
            data["hop1_gate"] = gates["hop1"]
        if "hop2_gate" not in data and "hop2" in gates:
            data["hop2_gate"] = gates["hop2"]
    elif isinstance(gates, (list, tuple)) and len(gates) == 2:
        data.setdefault("hop1_gate", gates[0])
        data.setdefault("hop2_gate", gates[1])

    data = _remap(data, _TOP_KEY_MAP)

    if isinstance(data.get("hop1_gate"), dict):
        data["hop1_gate"] = _remap(data["hop1_gate"], _GATE_KEY_MAP)
    if isinstance(data.get("hop2_gate"), dict):
        data["hop2_gate"] = _remap(data["hop2_gate"], _GATE_KEY_MAP)

    if isinstance(data.get("frames"), list):
        data["frames"] = [_remap(f, _FRAME_KEY_MAP) for f in data["frames"]]

    # offset range may arrive as a dict {"min": .., "max": ..} or "lo..hi".
    rng = data.get("clock_offset_range_us")
    if isinstance(rng, dict):
        data["clock_offset_range_us"] = (
            rng.get("min", rng.get("lo")),
            rng.get("max", rng.get("hi")),
        )

    return data



class GateWindowIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    open_us: int = Field(
        ...,
        description="Gate open offset within the period, local clock (us).",
        validation_alias=AliasChoices(
            "open_us", "open", "start_us", "open_at_us", "open_at", "start"
        ),
    )
    close_us: int = Field(
        ...,
        description=(
            "Gate close offset within the period, local clock (us). A frame "
            "may finish exactly at close."
        ),
        validation_alias=AliasChoices(
            "close_us", "close", "end_us", "close_at_us", "close_at", "end"
        ),
    )


class FrameIn(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    frame_id: Optional[str] = Field(
        default=None,
        description="Optional caller-defined identifier (echoed in reports).",
        validation_alias=AliasChoices("frame_id", "id", "name"),
    )
    enqueue_offset_us: int = Field(
        ...,
        ge=0,
        description="First-hop enqueue offset inside one period (us).",
        validation_alias=AliasChoices(
            "enqueue_offset_us", "enqueue_us", "offset_us", "enqueue", "arrival_us"
        ),
    )
    hop1_tx_us: int = Field(
        ...,
        ge=0,
        description="Transmission duration on hop 1 (us).",
        validation_alias=AliasChoices(
            "hop1_tx_us",
            "hop1_duration_us",
            "tx1_us",
            "duration_hop1_us",
            "first_hop_tx_us",
        ),
    )
    hop2_tx_us: int = Field(
        ...,
        ge=0,
        description="Transmission duration on hop 2 (us).",
        validation_alias=AliasChoices(
            "hop2_tx_us",
            "hop2_duration_us",
            "tx2_us",
            "duration_hop2_us",
            "second_hop_tx_us",
        ),
    )


class VerifyRequest(BaseModel):
    model_config = ConfigDict(populate_by_name=True, extra="ignore")

    @model_validator(mode="before")
    @classmethod
    def _normalize_compat(cls, data: Any) -> Any:
        return normalize_payload(data)

    period_us: int = Field(
        ...,
        ge=1,
        le=1000,
        description="Repetition period in integer microseconds (max 1000).",
        validation_alias=AliasChoices(
            "period_us", "period", "periodUs", "cycle_us",
            "period_duration_us", "cycle_time_us",
        ),
    )
    deadline_us: Optional[int] = Field(
        default=None,
        ge=1,
        description=(
            "Unified delivery deadline measured from this frame's first-hop "
            "enqueue. Defaults to the period; must be <= period_us."
        ),
        validation_alias=AliasChoices("deadline_us", "deadline", "deadlineUs"),
    )
    offset_min_us: int = Field(
        default=0,
        ge=-OFFSET_BOUND_US,
        le=OFFSET_BOUND_US,
        description="Minimum clock2-minus-clock1 offset to test (us, >= -20).",
        validation_alias=AliasChoices(
            "offset_min_us", "clock_offset_min_us", "delta_min_us"
        ),
    )
    offset_max_us: int = Field(
        default=0,
        ge=-OFFSET_BOUND_US,
        le=OFFSET_BOUND_US,
        description="Maximum clock2-minus-clock1 offset to test (us, <= 20).",
        validation_alias=AliasChoices(
            "offset_max_us", "clock_offset_max_us", "delta_max_us"
        ),
    )
    clock_offset_range_us: Optional[tuple[int, int]] = Field(
        default=None,
        description=(
            "Shorthand for [min, max] clock2-minus-clock1 offset in us; "
            "every integer offset in the inclusive range is evaluated."
        ),
        validation_alias=AliasChoices(
            "clock_offset_range_us", "offset_range_us", "delta_range"
        ),
    )
    clock_offset_us: Optional[int] = Field(
        default=None,
        ge=-OFFSET_BOUND_US,
        le=OFFSET_BOUND_US,
        description="Shorthand for a single fixed offset (sets min = max).",
        validation_alias=AliasChoices("clock_offset_us", "offset_us", "delta_us"),
    )
    hop1_gate: GateWindowIn = Field(
        ...,
        description="Hop-1 gate-open window in hop-1 local clock.",
        validation_alias=AliasChoices("hop1_gate", "gate_hop1", "hop1_window"),
    )
    hop2_gate: GateWindowIn = Field(
        ...,
        description="Hop-2 gate-open window in hop-2 local clock.",
        validation_alias=AliasChoices("hop2_gate", "gate_hop2", "hop2_window"),
    )
    frames: list[FrameIn] = Field(
        ...,
        min_length=1,
        max_length=4,
        description="1..4 frames; submission order is the tie-break order.",
        validation_alias=AliasChoices(
            "frames", "frame_configs", "frame_list", "configs"
        ),
    )
    include_trace: bool = Field(
        default=False,
        description="Include per-frame scheduled timestamps for debugging.",
        validation_alias=AliasChoices("include_trace", "trace", "with_trace"),
    )

    @model_validator(mode="after")
    def _check_semantics(self) -> "VerifyRequest":
        explicit = self.model_fields_set
        if self.clock_offset_range_us is not None and not (
            "offset_min_us" in explicit or "offset_max_us" in explicit
        ):
            lo, hi = self.clock_offset_range_us
            if not (-OFFSET_BOUND_US <= lo <= OFFSET_BOUND_US):
                raise ValueError("offset range min must be within [-20, 20] us")
            if not (-OFFSET_BOUND_US <= hi <= OFFSET_BOUND_US):
                raise ValueError("offset range max must be within [-20, 20] us")
            self.offset_min_us = lo
            self.offset_max_us = hi
        if self.clock_offset_us is not None and not (
            "offset_min_us" in explicit or "offset_max_us" in explicit
        ):
            self.offset_min_us = self.clock_offset_us
            self.offset_max_us = self.clock_offset_us

        if self.deadline_us is None:
            self.deadline_us = self.period_us
        if self.deadline_us > self.period_us:
            raise ValueError("deadline_us must be <= period_us (at most one period)")
        if self.offset_min_us > self.offset_max_us:
            raise ValueError("offset_min_us must be <= offset_max_us")

        for label, gate in (("hop1_gate", self.hop1_gate), ("hop2_gate", self.hop2_gate)):
            if not (0 <= gate.open_us < gate.close_us <= self.period_us):
                raise ValueError(
                    f"{label}: require 0 <= open_us < close_us <= period_us"
                )
            if gate.close_us - gate.open_us >= self.period_us:
                raise ValueError(f"{label}: gate window must be shorter than period")

        for idx, fr in enumerate(self.frames, start=1):
            if fr.enqueue_offset_us >= self.period_us:
                raise ValueError(
                    f"frames[{idx - 1}]: enqueue_offset_us must be within "
                    "[0, period_us)"
                )
        return self

    def to_config(self) -> VerifyConfig:
        return VerifyConfig(
            period_us=self.period_us,
            deadline_us=self.deadline_us or self.period_us,
            offset_min_us=self.offset_min_us,
            offset_max_us=self.offset_max_us,
            hop1_gate=GateWindow(self.hop1_gate.open_us, self.hop1_gate.close_us),
            hop2_gate=GateWindow(self.hop2_gate.open_us, self.hop2_gate.close_us),
            frames=tuple(
                FrameSpec(
                    number=i,
                    enqueue_offset_us=fr.enqueue_offset_us,
                    hop1_tx_us=fr.hop1_tx_us,
                    hop2_tx_us=fr.hop2_tx_us,
                    frame_id=fr.frame_id,
                )
                for i, fr in enumerate(self.frames, start=1)
            ),
        )
