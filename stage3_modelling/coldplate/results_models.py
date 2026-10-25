"""Pydantic models for the results bundle (contract: docs/API_CONTRACT.md)."""

from __future__ import annotations

from typing import Any, Optional

from pydantic import BaseModel, ConfigDict


class FlexModel(BaseModel):
    model_config = ConfigDict(extra="allow")


class FileRef(FlexModel):
    url: Optional[str] = None
    path: str = ""
    bytes: int = 0
    sha256: str = ""
    convention: Optional[str] = None


class CandidateRecord(FlexModel):
    candidate_id: str
    design_id: str
    is_baseline: bool = False
    params: dict = {}
    metrics: dict = {}
    feasible: bool = False
    violations: list = []
    targets_met: dict = {}
    worst_case: dict = {}
    pareto: dict = {}
    selected: bool = False
    role: Optional[str] = None
    status_label: str = "infeasible"
    files: Optional[dict[str, FileRef]] = None
    cad_error: Optional[str] = None


class SelectedDesign(CandidateRecord):
    role_label: Optional[str] = None
    sweep: Optional[dict] = None
    acceptance: Optional[dict] = None
    files: Optional[dict[str, FileRef]] = None
    cad_check: Optional[dict] = None


class ManifestEntry(FlexModel):
    path: str
    role: str
    media_type: str
    bytes: int
    sha256: str
    design_id: Optional[str] = None


class ResultsDoc(FlexModel):
    schema_version: str = "coldplate-results/1"
    job_id: str
    engine_version: str
    created_at: str
    completed_at: Optional[str] = None
    status: str
    label: str
    inputs: dict = {}
    interpretation: dict = {}
    normalized_inputs: Optional[dict] = None
    assumptions: list = []
    requirement_conflicts: list = []
    simulation: dict = {}
    objectives: Optional[dict] = None
    constraints: list = []
    candidates: list[CandidateRecord] = []
    rejected_candidates: list = []
    pareto: dict = {}
    optimizer: dict = {}
    selected_designs: list[SelectedDesign] = []
    warnings: list = []
    links: dict = {}


class ManifestDoc(FlexModel):
    schema_version: str = "coldplate-manifest/1"
    job_id: str
    files: list[ManifestEntry] = []
