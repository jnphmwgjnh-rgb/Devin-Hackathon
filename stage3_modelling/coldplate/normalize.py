"""Normalisation: RequirementsExtraction -> ColdPlateSpec.

Converts every reported quantity to canonical units, verifies verbatim
evidence, rejects assumptions on essential inputs, applies the labelled
engine assumptions (water properties, placements, manifold openings, ...),
and classifies the outcome as ok / needs_input / unsupported / invalid.
"""

from __future__ import annotations

import math
import os
import re
from dataclasses import dataclass, field
from typing import Literal, Mapping, Optional

from coldplate.evidence import evidence_found, value_in_evidence
from coldplate.extraction_schema import (
    CANONICAL_UNITS,
    NumberList,
    Quantity,
    RequirementsExtraction,
    Text,
)
from coldplate.power_map import Footprint, GridPowerMap, UniformPowerMap
from coldplate.spec import (
    AcceptanceSpec,
    AssumptionRec,
    BaselineSpec,
    ColdPlateSpec,
    CoolantSpec,
    DieRegionSpec,
    FinRegionSpec,
    FlowSpec,
    HeatSourceSpec,
    ListVal,
    ManufacturingSpec,
    ObjectiveSpec,
    PlateSpec,
    PowerMapSpec,
    PressureSpec,
    TempLimitSpec,
    TextVal,
    TimSpec,
    Val,
)
from coldplate.units import UnitError, convert

Status = Literal["ok", "needs_input", "unsupported", "invalid"]

ESSENTIAL_PATHS = {
    "plate.length_x",
    "plate.width_y",
    "plate.thickness",
    "fin_region.length_x",
    "fin_region.width_y",
    "fin_region.height",
    "coolant.flow_rate",
    "coolant.inlet_temperature",
}

UNSUPPORTED_ARRANGEMENTS = {"u_turn", "serpentine", "radial", "other"}

FLOW_AXIS_NOTE = (
    "flow axis not stated; assumed coolant crosses the fin region along x "
    "(inlets on the x-min/x-max edges)"
)
MANIFOLD_NOTE = (
    "manifold opening widths not specified; default proportions from the "
    "ITherm competition flow-path figure (11.1 mm edge inlets / 6.0 mm center "
    "outlet on a 52.15 mm fin region)"
)
_INLET_EDGE_FRAC = 0.212
_OUTLET_CENTER_FRAC = 0.115


@dataclass
class NormalizationResult:
    status: Status
    spec: Optional[ColdPlateSpec] = None
    blocking_errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    missing_essential: list[str] = field(default_factory=list)
    missing_optional: list[str] = field(default_factory=list)
    unsupported_assumptions: list[str] = field(default_factory=list)
    evidence_misses: list[str] = field(default_factory=list)


@dataclass
class _Ctx:
    document: str
    verify_evidence: bool = True
    blocking: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    misses: list[str] = field(default_factory=list)
    missing_ess: list[str] = field(default_factory=list)
    missing_opt: list[str] = field(default_factory=list)
    unsup: list[str] = field(default_factory=list)
    assumptions: list[AssumptionRec] = field(default_factory=list)

    def miss(self, path: str, essential: bool) -> None:
        (self.missing_ess if essential else self.missing_opt).append(path)


def _orig(value, unit) -> Optional[str]:
    if value is None:
        return None
    return f"{value} {unit}".strip() if unit else str(value)


def _map_source(src: str) -> str:
    return src if src in ("input", "input_flagged_assumption", "assumed") else "missing"


def _evidence_check(ctx: _Ctx, path: str, essential: bool, detail: str) -> None:
    if not ctx.verify_evidence:
        ctx.misses.append(f"{path}: {detail}")
        warning = "evidence quoted by your AI, not verifiable against the diagram"
        if warning not in ctx.warnings:
            ctx.warnings.append(warning)
        return
    if essential:
        ctx.blocking.append(f"{path}: {detail}")
    else:
        ctx.misses.append(f"{path}: {detail}")
        ctx.warnings.append(f"{path}: {detail}")


def _quantity(
    ctx: _Ctx, q: Optional[Quantity], path: str, kind: str, *, essential: bool = False
) -> Val:
    """Convert a reported Quantity to a canonical Val, recording all diagnostics."""
    canon_unit = CANONICAL_UNITS[kind]
    if q is None or q.value is None or q.source == "missing":
        ctx.miss(path, essential)
        return Val(unit=canon_unit, source="missing", note=(q.note if q else None))
    if q.source == "assumed" and essential:
        ctx.unsup.append(
            f"{path}: essential input reported as 'assumed' (value {q.value!r} {q.unit})"
        )
        ctx.blocking.append(
            f"{path}: essential value was assumed (got {q.value} {q.unit}); it must "
            f"come from the document — report it 'missing' or with document evidence"
        )
        ctx.miss(path, True)
        return Val(
            unit=canon_unit,
            source="missing",
            note="assumed value rejected: essential inputs cannot be assumed",
            original=_orig(q.value, q.unit),
        )
    try:
        canon = convert(q.value, q.unit, kind)
    except UnitError:
        ctx.blocking.append(
            f"{path}: unit {q.unit!r} not convertible as {kind}"
        )
        return Val(
            unit=canon_unit,
            source=_map_source(q.source),
            note="unit conversion failed",
            original=_orig(q.value, q.unit),
        )
    if q.source in ("input", "input_flagged_assumption"):
        if not evidence_found(q.evidence, ctx.document):
            _evidence_check(
                ctx, path, essential,
                f"evidence quote not found in document: {q.evidence!r}",
            )
        elif not value_in_evidence(q.value, q.evidence):
            _evidence_check(
                ctx, path, essential,
                f"reported value {q.value} does not appear in the evidence quote",
            )
    if q.source == "assumed":
        ctx.assumptions.append(
            AssumptionRec(
                field=path,
                statement=q.note or f"value {q.value} {q.unit} assumed by Devin",
                origin="devin",
            )
        )
    return Val(
        value=canon,
        unit=canon_unit,
        source=_map_source(q.source),
        evidence=q.evidence,
        note=q.note,
        original=_orig(q.value, q.unit),
    )


def _numberlist(
    ctx: _Ctx, nl: Optional[NumberList], path: str, kind: str, *, essential: bool = False
) -> ListVal:
    canon_unit = CANONICAL_UNITS[kind]
    if nl is None or not nl.values or nl.source == "missing":
        ctx.miss(path, essential)
        return ListVal(unit=canon_unit, source="missing", note=(nl.note if nl else None))
    out: list[float] = []
    bad = False
    for v in nl.values:
        try:
            out.append(convert(v, nl.unit, kind))
        except UnitError:
            bad = True
    if bad:
        ctx.blocking.append(f"{path}: unit {nl.unit!r} not convertible as {kind}")
        return ListVal(unit=canon_unit, source=_map_source(nl.source), note="unit conversion failed")
    if nl.source in ("input", "input_flagged_assumption"):
        if not evidence_found(nl.evidence, ctx.document):
            _evidence_check(
                ctx, path, essential,
                f"evidence quote not found in document: {nl.evidence!r}",
            )
        else:
            for v in nl.values:
                if not value_in_evidence(v, nl.evidence):
                    _evidence_check(
                        ctx, path, essential,
                        f"reported value {v} does not appear in the evidence quote",
                    )
    if nl.source == "assumed":
        ctx.assumptions.append(
            AssumptionRec(field=path, statement=nl.note or "list assumed by Devin", origin="devin")
        )
    return ListVal(
        values=out,
        unit=canon_unit,
        source=_map_source(nl.source),
        evidence=nl.evidence,
        note=nl.note,
        original=f"{nl.values} {nl.unit}".strip(),
    )


def _text(
    ctx: _Ctx, t: Optional[Text], path: str, *, essential: bool = False
) -> TextVal:
    if t is None or t.value is None or t.source == "missing":
        ctx.miss(path, essential)
        return TextVal(source="missing", note=(t.note if t else None))
    if t.source in ("input", "input_flagged_assumption") and not evidence_found(
        t.evidence, ctx.document
    ):
        _evidence_check(
            ctx, path, essential,
            f"evidence quote not found in document: {t.evidence!r}",
        )
    if t.source == "assumed":
        ctx.assumptions.append(
            AssumptionRec(field=path, statement=t.note or f"text {t.value!r} assumed", origin="devin")
        )
    return TextVal(
        value=t.value,
        source=_map_source(t.source),
        evidence=t.evidence,
        note=t.note,
        original=t.value,
    )


# ---------------------------------------------------------------------------
# physics helpers
# ---------------------------------------------------------------------------


def _water_density(T_C: float) -> float:
    """Thiesen-Scheel-Diesselhorst equation, kg/m^3, T in degC."""
    return 1000.0 * (
        1.0 - (T_C + 288.9414) / (508929.2 * (T_C + 68.12963)) * (T_C - 3.9863) ** 2
    )


def _water_viscosity(T_C: float) -> float:
    """Vogel correlation, Pa*s."""
    return 2.414e-5 * 10.0 ** (247.8 / (T_C + 133.15))


def _water_cp(T_C: float) -> float:  # noqa: ARG001 - constant in this model
    return 4180.0


def _water_k(T_C: float) -> float:
    tau = (T_C + 273.15) / 298.15
    return 0.6065 * (-1.48445 + 4.12292 * tau - 1.63866 * tau**2)


_WATER_PROPS = {
    "density": ("density_kg_m3", _water_density, "kg/m^3"),
    "specific_heat": ("cp_J_kgK", _water_cp, "J/(kg*K)"),
    "dynamic_viscosity": ("viscosity_Pa_s", _water_viscosity, "Pa*s"),
    "thermal_conductivity": ("k_W_mK", _water_k, "W/(m*K)"),
}


def _placement_origin(
    ctx: _Ctx,
    placement: TextVal,
    region_w: Optional[float],
    region_h: Optional[float],
    plate_w: float,
    plate_h: float,
    what: str,
) -> list[float]:
    """Lower-left corner of a region in the plate frame."""
    if region_w is None or region_h is None:
        return [0.0, 0.0]
    centered = [(plate_w - region_w) / 2.0, (plate_h - region_h) / 2.0]
    txt = (placement.value or "").lower()
    if placement.source == "missing" or not txt:
        ctx.assumptions.append(
            AssumptionRec(
                field=f"{what}.placement",
                statement=f"{what} placement not stated; assumed centered on the plate",
                origin="engine",
            )
        )
        return centered
    if "center" in txt:
        return centered
    if "lower" in txt and ("left" in txt or "corner" in txt):
        return [0.0, 0.0]
    ctx.warnings.append(
        f"{what}.placement {placement.value!r} not understood; assumed centered on the plate"
    )
    ctx.assumptions.append(
        AssumptionRec(
            field=f"{what}.placement",
            statement=f"{what} placement {placement.value!r} unparsed; assumed centered on the plate",
            origin="engine",
        )
    )
    return centered


def _dim_positive(ctx: _Ctx, path: str, v: Optional[float]) -> None:
    if v is not None and v <= 0:
        ctx.blocking.append(f"{path}: dimension must be > 0 (got {v})")


# ---------------------------------------------------------------------------
# main entry
# ---------------------------------------------------------------------------


def normalize_extraction(
    extraction: RequirementsExtraction,
    *,
    document: str,
    reference_files: Mapping[str, bytes],
    verify_evidence: bool = True,
) -> NormalizationResult:
    ctx = _Ctx(document=document, verify_evidence=verify_evidence)
    ex = extraction

    # -- unsupported arrangement short-circuits the whole parse -------------
    if ex.flow is not None and ex.flow.arrangement in UNSUPPORTED_ARRANGEMENTS:
        return NormalizationResult(
            status="unsupported",
            spec=None,
            blocking_errors=[
                f"flow.arrangement={ex.flow.arrangement!r} is not supported by the "
                f"engine (supported: straight, split_edges_to_center, split_center_to_edges)"
            ],
        )

    spec = ColdPlateSpec(document_title=ex.document_title)
    spec.coordinate_frame = _text(ctx, ex.coordinate_frame, "coordinate_frame")

    # -- plate ---------------------------------------------------------------
    spec.plate = PlateSpec(
        length_x_mm=_quantity(ctx, ex.plate.length_x, "plate.length_x", "length", essential=True),
        width_y_mm=_quantity(ctx, ex.plate.width_y, "plate.width_y", "length", essential=True),
        thickness_mm=_quantity(ctx, ex.plate.thickness, "plate.thickness", "length", essential=True),
        k_W_mK=_quantity(ctx, ex.plate.thermal_conductivity, "plate.thermal_conductivity", "thermal_conductivity"),
        material=_text(ctx, ex.plate.material, "plate.material"),
    )
    for p in ("plate.length_x", "plate.width_y", "plate.thickness"):
        _dim_positive(ctx, p, getattr(spec.plate, {
            "plate.length_x": "length_x_mm", "plate.width_y": "width_y_mm",
            "plate.thickness": "thickness_mm"}[p]).value)

    # solid conductivity assumption from the material text
    mat = (spec.plate.material.value or "").lower()
    if spec.plate.k_W_mK.value is None:
        if "copper" in mat or re.search(r"\bcu\b", mat) or re.search(r"\bc1\d{3}\b", mat):
            spec.plate.k_W_mK = Val(
                value=391.0, unit="W/(m*K)", source="assumed",
                note="solid conductivity assumed for copper (k=391 W/m·K)",
            )
            ctx.assumptions.append(AssumptionRec(
                field="plate.thermal_conductivity",
                statement="k not stated; assumed 391 W/m·K for copper", origin="engine"))
            ctx.missing_opt = [m for m in ctx.missing_opt if m != "plate.thermal_conductivity"]
        elif "alumin" in mat:
            spec.plate.k_W_mK = Val(
                value=205.0, unit="W/(m*K)", source="assumed",
                note="solid conductivity assumed for aluminium (k=205 W/m·K)",
            )
            ctx.assumptions.append(AssumptionRec(
                field="plate.thermal_conductivity",
                statement="k not stated; assumed 205 W/m·K for aluminium", origin="engine"))
            ctx.missing_opt = [m for m in ctx.missing_opt if m != "plate.thermal_conductivity"]
        else:
            ctx.missing_ess.append("plate.thermal_conductivity")

    # -- die / fin region placement ------------------------------------------
    spec.die_region = DieRegionSpec(
        width_x_mm=_quantity(ctx, ex.die_region.width_x, "die_region.width_x", "length"),
        height_y_mm=_quantity(ctx, ex.die_region.height_y, "die_region.height_y", "length"),
        placement=_text(ctx, ex.die_region.placement, "die_region.placement"),
    )
    spec.fin_region = FinRegionSpec(
        length_x_mm=_quantity(ctx, ex.fin_region.length_x, "fin_region.length_x", "length", essential=True),
        width_y_mm=_quantity(ctx, ex.fin_region.width_y, "fin_region.width_y", "length", essential=True),
        height_mm=_quantity(ctx, ex.fin_region.height, "fin_region.height", "length", essential=True),
        placement=_text(ctx, ex.fin_region.placement, "fin_region.placement"),
    )
    for p, v in (
        ("die_region.width_x", spec.die_region.width_x_mm.value),
        ("die_region.height_y", spec.die_region.height_y_mm.value),
        ("fin_region.length_x", spec.fin_region.length_x_mm.value),
        ("fin_region.width_y", spec.fin_region.width_y_mm.value),
        ("fin_region.height", spec.fin_region.height_mm.value),
    ):
        _dim_positive(ctx, p, v)

    pw = spec.plate.length_x_mm.value or 0.0
    ph = spec.plate.width_y_mm.value or 0.0
    spec.fin_region.origin_plate_mm = _placement_origin(
        ctx, spec.fin_region.placement, spec.fin_region.length_x_mm.value,
        spec.fin_region.width_y_mm.value, pw, ph, "fin_region",
    )
    spec.die_region.origin_plate_mm = _placement_origin(
        ctx, spec.die_region.placement, spec.die_region.width_x_mm.value,
        spec.die_region.height_y_mm.value, pw, ph, "die_region",
    )

    # fin region must lie inside the plate
    if all(v is not None for v in (
        spec.fin_region.length_x_mm.value, spec.fin_region.width_y_mm.value)) and pw and ph:
        fx0, fy0 = spec.fin_region.origin_plate_mm
        fx1 = fx0 + spec.fin_region.length_x_mm.value
        fy1 = fy0 + spec.fin_region.width_y_mm.value
        if fx0 < -1e-9 or fy0 < -1e-9 or fx1 > pw + 1e-9 or fy1 > ph + 1e-9:
            ctx.blocking.append(
                f"fin_region: region [{fx0},{fy0}]-[{fx1},{fy1}] mm lies outside the "
                f"plate ({pw} x {ph} mm)"
            )

    # -- heat-source frame ----------------------------------------------------
    cf_txt = (spec.coordinate_frame.value or "").lower()
    die_known = (
        spec.die_region.width_x_mm.value is not None
        and spec.die_region.height_y_mm.value is not None
    )
    if "plate" in cf_txt:
        frame_ox, frame_oy = 0.0, 0.0
    elif die_known:
        frame_ox, frame_oy = spec.die_region.origin_plate_mm
    else:
        frame_ox = frame_oy = None  # decided after sources parsed (bbox centered)

    # -- heat sources ---------------------------------------------------------
    raw_sources = ex.heat_sources or []
    if not raw_sources:
        ctx.missing_ess.append("heat_sources")
    out_sources: list[HeatSourceSpec] = []
    for i, h in enumerate(raw_sources):
        name = h.name or f"source_{i}"
        base = f"heat_sources[{name}]"
        required_null = [
            f for f in ("x0", "y0", "width", "height", "power") if getattr(h, f) is None
        ]
        if h.source == "assumed":
            ctx.unsup.append(
                f"{base}: heat source reported as 'assumed' — geometry/power must "
                f"come from the document"
            )
            ctx.blocking.append(
                f"{base}: heat source assumed by Devin; provide document evidence"
            )
            for f in required_null or ["x0"]:
                ctx.missing_ess.append(f"{base}.{f}")
            continue
        if required_null:
            for f in required_null:
                ctx.missing_ess.append(f"{base}.{f}")
        # evidence: every numeric field must appear in the row quote
        if h.source in ("input", "input_flagged_assumption"):
            if not evidence_found(h.evidence, ctx.document):
                _evidence_check(
                    ctx, f"{base}.evidence", True,
                    f"row quote not found in document: {h.evidence!r}",
                )
            else:
                for f in ("x0", "y0", "width", "height", "power"):
                    v = getattr(h, f)
                    if v is not None and not value_in_evidence(v, h.evidence):
                        _evidence_check(
                            ctx, f"{base}.{f}", True,
                            f"value {v} does not appear in the row quote",
                        )
        # unit conversion (as-written units)
        lunit, punit = h.length_unit, h.power_unit
        conv = {}
        for f in ("x0", "y0", "width", "height"):
            v = getattr(h, f)
            if v is None:
                conv[f] = None
                continue
            try:
                conv[f] = convert(v, lunit, "length")
            except UnitError:
                ctx.blocking.append(f"{base}.{f}: unit {lunit!r} not convertible as length")
                conv[f] = None
        if h.power is None:
            conv["power"] = None
        else:
            try:
                conv["power"] = convert(h.power, punit, "power")
            except UnitError:
                ctx.blocking.append(f"{base}.power: unit {punit!r} not convertible as power")
                conv["power"] = None
        out_sources.append(HeatSourceSpec(
            name=name, type=h.type,
            x0_mm=conv["x0"], y0_mm=conv["y0"],
            width_mm=conv["width"], height_mm=conv["height"],
            power_W=conv["power"],
            source=_map_source(h.source), evidence=h.evidence, note=h.note,
        ))

    if frame_ox is None:
        # bbox-centered fallback (labelled assumption)
        complete = [
            s for s in out_sources
            if None not in (s.x0_mm, s.y0_mm, s.width_mm, s.height_mm)
        ]
        if complete and pw and ph:
            bx0 = min(s.x0_mm for s in complete)
            by0 = min(s.y0_mm for s in complete)
            bx1 = max(s.x0_mm + s.width_mm for s in complete)
            by1 = max(s.y0_mm + s.height_mm for s in complete)
            frame_ox = (pw - (bx1 - bx0)) / 2.0 - bx0
            frame_oy = (ph - (by1 - by0)) / 2.0 - by0
            ctx.assumptions.append(AssumptionRec(
                field="coordinate_frame",
                statement="coordinate frame not tied to the plate and die region "
                          "unknown; assumed the heat-source bounding box is centered "
                          "on the plate",
                origin="engine",
            ))
        else:
            frame_ox, frame_oy = 0.0, 0.0
            ctx.warnings.append(
                "heat-source coordinate frame unresolved (no plate reference, no "
                "die region); using document coordinates as plate coordinates"
            )
    for s in out_sources:
        if s.x0_mm is not None:
            s.x0_plate_mm = frame_ox + s.x0_mm
        if s.y0_mm is not None:
            s.y0_plate_mm = frame_oy + s.y0_mm
    spec.heat_sources = out_sources

    # sources must lie within the plate; outside fin region only warns
    fx0, fy0 = spec.fin_region.origin_plate_mm
    fw = spec.fin_region.length_x_mm.value
    fh = spec.fin_region.width_y_mm.value
    for s in out_sources:
        if None in (s.x0_plate_mm, s.y0_plate_mm, s.width_mm, s.height_mm):
            continue
        x1 = s.x0_plate_mm + s.width_mm
        y1 = s.y0_plate_mm + s.height_mm
        if pw and ph and (
            s.x0_plate_mm < -1e-9 or s.y0_plate_mm < -1e-9
            or x1 > pw + 1e-9 or y1 > ph + 1e-9
        ):
            ctx.blocking.append(
                f"heat_sources[{s.name}]: footprint [{s.x0_plate_mm},{s.y0_plate_mm}]-"
                f"[{x1},{y1}] mm lies outside the plate ({pw} x {ph} mm); likely an "
                f"extraction or frame error"
            )
        elif fw and fh and (
            s.x0_plate_mm < fx0 - 1e-9 or s.y0_plate_mm < fy0 - 1e-9
            or x1 > fx0 + fw + 1e-9 or y1 > fy0 + fh + 1e-9
        ):
            ctx.warnings.append(
                f"heat_sources[{s.name}]: footprint lies outside the fin region "
                f"[{fx0},{fy0}]-[{fx0 + fw},{fy0 + fh}]"
            )
        _dim_positive(ctx, f"heat_sources[{s.name}].width", s.width_mm)
        _dim_positive(ctx, f"heat_sources[{s.name}].height", s.height_mm)

    powers = [s.power_W for s in out_sources if s.power_W is not None]
    spec.total_power_W = sum(powers) if out_sources else None

    spec.stated_total_power_W = _quantity(
        ctx, ex.stated_total_power, "stated_total_power", "power"
    )
    if (
        spec.stated_total_power_W.value is not None
        and spec.total_power_W is not None
        and spec.stated_total_power_W.value != 0
    ):
        rel = abs(spec.total_power_W - spec.stated_total_power_W.value) / abs(
            spec.stated_total_power_W.value
        )
        if rel > 0.005:
            ctx.warnings.append(
                f"sum of heat-source powers {spec.total_power_W:.3f} W differs from "
                f"stated total {spec.stated_total_power_W.value:.3f} W by {rel:.2%}; "
                f"the source sum is used"
            )

    # -- power map ------------------------------------------------------------
    spec.power_map = PowerMapSpec(
        distribution=ex.power_map.distribution,
        file_name=ex.power_map.file_name,
        grid_pitch_mm=_quantity(ctx, ex.power_map.grid_pitch, "power_map.grid_pitch", "length"),
        grid_origin=_text(ctx, ex.power_map.grid_origin, "power_map.grid_origin"),
        stated_peak_flux_W_m2=_quantity(
            ctx, ex.power_map.stated_peak_flux, "power_map.stated_peak_flux", "heat_flux"
        ),
        uniform_components=list(ex.power_map.uniform_components or []),
        applied="uniform",
    )
    footprints = tuple(
        Footprint(
            name=s.name, type=s.type,
            x0=s.x0_plate_mm, y0=s.y0_plate_mm,
            x1=s.x0_plate_mm + s.width_mm, y1=s.y0_plate_mm + s.height_mm,
            power_W=s.power_W or 0.0,
        )
        for s in out_sources
        if None not in (s.x0_plate_mm, s.y0_plate_mm, s.width_mm, s.height_mm)
    )
    pmap_obj = UniformPowerMap(footprints)
    pm = ex.power_map
    if pm.distribution == "non_uniform_file" and pm.file_name:
        want_base = os.path.basename(pm.file_name).lower()
        match = next(
            (v for k, v in (reference_files or {}).items()
             if os.path.basename(str(k)).lower() == want_base),
            None,
        )
        if match is not None:
            try:
                pitch = spec.power_map.grid_pitch_mm.value or 0.5
                if spec.power_map.grid_pitch_mm.value is None:
                    ctx.warnings.append("power_map.grid_pitch missing; assumed 0.5 mm")
                row0 = "top" if "top" in (spec.power_map.grid_origin.value or "").lower() else "bottom"
                grid = GridPowerMap.from_csv(
                    match,
                    pitch_mm=pitch,
                    origin_plate_mm=(frame_ox, frame_oy),
                    row0=row0,
                    units=pm.file_units or "W/mm^2",
                    sources=footprints,
                    file_name=pm.file_name,
                )
                spec.power_map.applied = "file"
                spec.power_map.description = f"file map {pm.file_name} ({grid.shape[1]}x{grid.shape[0]} cells @ {pitch} mm)"
                pmap_obj = grid
                # per-component rescale vs stated powers
                stated = {s.name: s.power_W for s in footprints}
                ctx.warnings.extend(grid.rescale_components(stated))
                # file overrides a uniform_components statement
                if spec.power_map.uniform_components:
                    hit = [u for u in spec.power_map.uniform_components
                           if any(u.lower() in s.name.lower() or u.lower() == s.type.lower()
                                  for s in footprints)]
                    if hit:
                        ctx.warnings.append(
                            f"power map file overrides the document's uniform-flux "
                            f"statement for {hit}"
                        )
                # map power in cells touching no footprint
                _attr, outside = grid.attributed_powers()
                if outside > 1e-6:
                    ctx.warnings.append(
                        f"power map file places {outside:.2f} W outside "
                        f"every heat-source footprint"
                    )
                # file peak vs stated peak
                if spec.power_map.stated_peak_flux_W_m2.value:
                    file_peak = float(grid.values_W_mm2.max()) * 1e6
                    stated_peak = spec.power_map.stated_peak_flux_W_m2.value
                    if stated_peak and abs(file_peak - stated_peak) / stated_peak > 0.10:
                        ctx.warnings.append(
                            f"power map file peak {file_peak:.3e} W/m^2 differs from "
                            f"stated peak {stated_peak:.3e} W/m^2 by >10%"
                        )
            except (ValueError, UnitError) as e:
                ctx.warnings.append(
                    f"power map file {pm.file_name} could not be loaded ({e}); "
                    f"uniform flux per component used"
                )
        else:
            ctx.warnings.append(
                f"power map file {pm.file_name} referenced but not provided; "
                f"uniform flux per component used"
            )
    spec.attach_power_map(pmap_obj)

    # -- temperature limits ----------------------------------------------------
    seen_types: set[str] = set()
    for tl in ex.temperature_limits or []:
        ct = tl.component_type
        seen_types.add(ct)
        entry = TempLimitSpec(
            component_type=ct,
            hard_C=_quantity(ctx, tl.hard_max, f"temperature_limits[{ct}].hard_max", "temperature"),
            target_C=_quantity(ctx, tl.target_max, f"temperature_limits[{ct}].target_max", "temperature"),
            stretch_C=_quantity(ctx, tl.stretch_max, f"temperature_limits[{ct}].stretch_max", "temperature"),
        )
        spec.temperature_limits.append(entry)
        h, t, s = entry.hard_C.value, entry.target_C.value, entry.stretch_C.value
        if h is not None and t is not None and h < t:
            ctx.warnings.append(
                f"temperature_limits[{ct}]: hard limit {h} < target {t}"
            )
        if t is not None and s is not None and t < s:
            ctx.warnings.append(
                f"temperature_limits[{ct}]: target {t} < stretch {s}"
            )

    # critical component: extraction, else lowest hard limit
    spec.critical_component_type = _text(
        ctx, ex.critical_component, "critical_component"
    )
    if spec.critical_component_type.value is None:
        candidates = [
            (t.component_type, t.hard_C.value)
            for t in spec.temperature_limits
            if t.hard_C.value is not None
        ]
        if candidates:
            worst = min(candidates, key=lambda kv: kv[1])[0]
            spec.critical_component_type = TextVal(
                value=worst, source="derived",
                note="lowest hard temperature limit among temperature_limits",
            )
            ctx.missing_opt = [m for m in ctx.missing_opt if m != "critical_component"]
    required_limit_types = {
        source.type for source in ex.heat_sources or [] if source.type
    }
    if spec.critical_component_type.value:
        required_limit_types.add(spec.critical_component_type.value)
    for required_type in required_limit_types:
        if not any(
            t.component_type.casefold() == required_type.casefold()
            and t.hard_C.value is not None
            for t in spec.temperature_limits
        ):
            ctx.missing_ess.append(
                f"temperature_limits[{required_type}].hard_max"
            )

    # -- coolant ----------------------------------------------------------------
    spec.coolant = CoolantSpec(
        fluid=_text(ctx, ex.coolant.fluid, "coolant.fluid"),
        density_kg_m3=_quantity(ctx, ex.coolant.density, "coolant.density", "density"),
        cp_J_kgK=_quantity(ctx, ex.coolant.specific_heat, "coolant.specific_heat", "specific_heat"),
        viscosity_Pa_s=_quantity(ctx, ex.coolant.dynamic_viscosity, "coolant.dynamic_viscosity", "viscosity"),
        k_W_mK=_quantity(ctx, ex.coolant.thermal_conductivity, "coolant.thermal_conductivity", "thermal_conductivity"),
        inlet_temperature_C=_quantity(ctx, ex.coolant.inlet_temperature, "coolant.inlet_temperature", "temperature", essential=True),
        inlet_temperature_sweep_C=_numberlist(ctx, ex.coolant.inlet_temperature_sweep, "coolant.inlet_temperature_sweep", "temperature"),
        flow_rate_Lpm=_quantity(ctx, ex.coolant.flow_rate, "coolant.flow_rate", "flow_rate", essential=True),
        flow_rate_sweep_Lpm=_numberlist(ctx, ex.coolant.flow_rate_sweep, "coolant.flow_rate_sweep", "flow_rate"),
        max_outlet_rise_K=_quantity(ctx, ex.coolant.max_outlet_temperature_rise, "coolant.max_outlet_temperature_rise", "temperature_difference"),
    )
    fluid_txt = (spec.coolant.fluid.value or "").lower()
    if "water" in fluid_txt:
        T = spec.coolant.inlet_temperature_C.value
        T_fill = T if T is not None else 25.0
        for field_name, (attr, fn, unit) in _WATER_PROPS.items():
            val = getattr(spec.coolant, attr)
            if val.value is None:
                filled = fn(T_fill)
                setattr(spec.coolant, attr, Val(
                    value=filled, unit=unit, source="assumed",
                    note=f"water property at {T_fill} °C from standard correlation"
                    + ("" if T is not None else " (inlet temperature unknown; 25 °C used)"),
                ))
                ctx.assumptions.append(AssumptionRec(
                    field=f"coolant.{field_name}",
                    statement=f"not stated; filled from water correlation at {T_fill} °C",
                    origin="engine",
                ))
                ctx.missing_opt = [m for m in ctx.missing_opt if m != f"coolant.{field_name}"]

    # -- flow -------------------------------------------------------------------
    arrangement = ex.flow.arrangement
    flow_src = ex.flow.source
    if arrangement == "unspecified":
        spec.flow.arrangement = "straight"
        ctx.assumptions.append(AssumptionRec(
            field="flow.arrangement",
            statement="flow arrangement not stated; assumed straight",
            origin="engine",
        ))
    else:
        spec.flow.arrangement = arrangement
    axis = ex.flow.flow_axis
    if axis == "unspecified":
        spec.flow.flow_axis = "x"
        ctx.assumptions.append(AssumptionRec(
            field="flow.flow_axis", statement=FLOW_AXIS_NOTE, origin="engine"))
    else:
        spec.flow.flow_axis = axis
    spec.flow.inlet = _text(ctx, ex.flow.inlet, "flow.inlet")
    spec.flow.outlet = _text(ctx, ex.flow.outlet, "flow.outlet")
    if flow_src == "assumed":
        ctx.assumptions.append(AssumptionRec(
            field="flow", statement=ex.flow.note or "flow path assumed", origin="devin"))

    # manifold openings: always engine_default
    axis_len = (
        spec.fin_region.length_x_mm.value
        if spec.flow.flow_axis == "x"
        else spec.fin_region.width_y_mm.value
    )
    if axis_len:
        if spec.flow.arrangement == "split_center_to_edges":
            inlet_f, outlet_f = _OUTLET_CENTER_FRAC, _INLET_EDGE_FRAC
        elif spec.flow.arrangement == "split_edges_to_center":
            inlet_f, outlet_f = _INLET_EDGE_FRAC, _OUTLET_CENTER_FRAC
        else:  # straight
            inlet_f = outlet_f = _INLET_EDGE_FRAC
        spec.flow.inlet_opening_mm = Val(
            value=round(inlet_f * axis_len, 4), unit="mm",
            source="engine_default", note=MANIFOLD_NOTE,
        )
        spec.flow.outlet_opening_mm = Val(
            value=round(outlet_f * axis_len, 4), unit="mm",
            source="engine_default", note=MANIFOLD_NOTE,
        )

    # -- remaining sections -----------------------------------------------------
    spec.manufacturing = ManufacturingSpec(
        min_feature_mm=_quantity(ctx, ex.manufacturing.min_feature, "manufacturing.min_feature", "length"),
        max_feature_mm=_quantity(ctx, ex.manufacturing.max_feature, "manufacturing.max_feature", "length"),
        xy_resolution_mm=_quantity(ctx, ex.manufacturing.xy_resolution, "manufacturing.xy_resolution", "length"),
        z_resolution_mm=_quantity(ctx, ex.manufacturing.z_resolution, "manufacturing.z_resolution", "length"),
        min_overhang_angle_deg=_quantity(ctx, ex.manufacturing.min_overhang_angle, "manufacturing.min_overhang_angle", "angle"),
        process=_text(ctx, ex.manufacturing.process, "manufacturing.process"),
    )
    spec.baseline = BaselineSpec(
        fin_type=_text(ctx, ex.baseline_design.fin_type, "baseline_design.fin_type"),
        fin_thickness_mm=_quantity(ctx, ex.baseline_design.fin_thickness, "baseline_design.fin_thickness", "length"),
        channel_width_mm=_quantity(ctx, ex.baseline_design.channel_width, "baseline_design.channel_width", "length"),
        fin_height_mm=_quantity(ctx, ex.baseline_design.fin_height, "baseline_design.fin_height", "length"),
    )
    spec.tim = TimSpec(
        r_m2K_W=_quantity(ctx, ex.tim.area_resistance, "tim.area_resistance", "area_thermal_resistance"),
        thickness_mm=_quantity(ctx, ex.tim.thickness, "tim.thickness", "length"),
    )
    if spec.tim.r_m2K_W.value is None:
        spec.tim.r_m2K_W = Val(
            value=0.0, unit="m^2*K/W", source="engine_default",
            note="no TIM specified; R'' = 0",
        )
        ctx.missing_opt = [m for m in ctx.missing_opt if m != "tim.area_resistance"]
        ctx.assumptions.append(AssumptionRec(
            field="tim.area_resistance",
            statement="TIM absent; R'' taken as 0", origin="engine"))
    spec.pressure = PressureSpec(
        hard_Pa=_quantity(ctx, ex.pressure_drop.hard_limit, "pressure_drop.hard_limit", "pressure"),
        target_Pa=_quantity(ctx, ex.pressure_drop.design_target, "pressure_drop.design_target", "pressure"),
        stretch_Pa=_quantity(ctx, ex.pressure_drop.stretch_goal, "pressure_drop.stretch_goal", "pressure"),
        acceptance_Pa=_quantity(ctx, ex.pressure_drop.acceptance_limit, "pressure_drop.acceptance_limit", "pressure"),
        acceptance_condition=_text(ctx, ex.pressure_drop.acceptance_condition, "pressure_drop.acceptance_condition"),
    )
    spec.acceptance = AcceptanceSpec(
        temperature_margin_K=_quantity(ctx, ex.acceptance.temperature_margin, "acceptance.temperature_margin", "temperature_difference"),
        worst_case_condition=_text(ctx, ex.acceptance.worst_case_condition, "acceptance.worst_case_condition"),
    )
    spec.objectives = [
        ObjectiveSpec(
            metric=o.metric, direction=o.direction, description=o.description,
            source=_map_source(o.source), evidence=o.evidence,
        )
        for o in ex.objectives or []
    ]
    for o, ospec in zip(ex.objectives or [], spec.objectives):
        if o.source in ("input", "input_flagged_assumption") and not evidence_found(
            o.evidence, ctx.document
        ):
            ctx.misses.append(f"objectives[{ospec.metric}]: evidence not found")

    # -- Devin-declared assumptions & missing fields -----------------------------
    for a in ex.assumptions or []:
        spec.assumptions.append(AssumptionRec(
            field=a.field, statement=a.statement,
            origin="document" if a.source == "input_flagged_assumption" else "devin",
        ))
    # Devin's missing_fields are reported, not trusted: the engine's own checks
    # decide missing_essential. Essential-looking entries the engine does not
    # require produce a warning instead of stalling the pipeline.
    from coldplate.spec import ReportedMissing

    engine_missing = set(ctx.missing_ess)
    for m in ex.missing_fields or []:
        spec.reported_missing.append(ReportedMissing(
            field=m.field, reason=m.reason, essential=bool(m.essential)
        ))
        if m.essential and m.field not in engine_missing:
            ctx.warnings.append(
                f"Devin reported '{m.field}' missing (essential); not required by the engine"
            )

    spec.assumptions.extend(ctx.assumptions)
    spec.warnings = list(dict.fromkeys(ctx.warnings))
    spec.missing_essential = list(dict.fromkeys(ctx.missing_ess))
    spec.missing_optional = list(dict.fromkeys(ctx.missing_opt))

    # -- status ------------------------------------------------------------------
    if spec.missing_essential:
        status: Status = "needs_input"
    elif ctx.blocking:
        status = "invalid"
    else:
        status = "ok"
    return NormalizationResult(
        status=status,
        spec=spec if status in ("ok", "needs_input") else None,
        blocking_errors=ctx.blocking,
        warnings=spec.warnings,
        missing_essential=spec.missing_essential,
        missing_optional=spec.missing_optional,
        unsupported_assumptions=ctx.unsup,
        evidence_misses=ctx.misses,
    )
