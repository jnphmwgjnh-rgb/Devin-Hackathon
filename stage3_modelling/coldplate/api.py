"""FastAPI backend (contract: docs/API_CONTRACT.md).

Run: uvicorn coldplate.api:app --port 8000
"""

from __future__ import annotations

import io
import json
import os
import re
import zipfile
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel, Field, ValidationError

from coldplate import __version__ as ENGINE_VERSION
from coldplate.jobs import JobManager
from coldplate.results_models import ResultsDoc

RUNS_DIR = Path(os.environ.get(
    "COLDPLATE_RUNS_DIR",
    str(Path(__file__).resolve().parent.parent / "runs"),
))
jobs = JobManager(RUNS_DIR)

app = FastAPI(title="coldplate-engine")

_origins = os.environ.get("COLDPLATE_CORS_ORIGINS")
if _origins:
    app.add_middleware(CORSMiddleware, allow_origins=_origins.split(","),
                       allow_methods=["*"], allow_headers=["*"])
else:
    app.add_middleware(CORSMiddleware,
                       allow_origin_regex=r"http://(localhost|127\.0\.0\.1)(:\d+)?",
                       allow_methods=["*"], allow_headers=["*"])


def err(status: int, code: str, message: str, details: Optional[dict] = None):
    return JSONResponse(status_code=status,
                        content={"error": {"code": code, "message": message,
                                           "details": details or {}}})


class Options(BaseModel):
    parser: str = "devin"
    prompt_version: str = "v2"
    use_cache: bool = True
    pop_size: int = Field(24, ge=4, le=200)
    n_gen: int = Field(8, ge=1, le=200)
    seed: int = 42
    grid_mm: float = Field(0.5, ge=0.25, le=2.0)
    sweep: bool = True
    export_cad: bool = True
    recording: Optional[dict] = None
    manual: Optional[dict] = None


class OptimizeBody(BaseModel):
    requirements_markdown: str
    reference_files: list = []
    options: Options = Options()


@app.exception_handler(Exception)
async def _unhandled(request: Request, exc: Exception):
    return err(500, "internal_error", str(exc))


_MAX_DOC_BYTES = 2 * 1024 * 1024
_MAX_REF_BYTES = 20 * 1024 * 1024
_MAX_REF_FILES = 10
_SAFE_NAME = re.compile(r"^[^/\\]+$")


def _ref_name(raw: str):
    """Reduce a supplied filename to a safe basename."""
    name = Path(str(raw).replace("\\", "/")).name.strip()
    if name in ("", ".", ".."):
        raise HTTPException(422, detail={"error": {
            "code": "invalid_reference_name",
            "message": f"unsafe reference filename {raw!r}", "details": {}}})
    return name


@app.post("/api/optimize")
async def optimize(request: Request):
    ctype = request.headers.get("content-type", "")
    if ctype.startswith("multipart/form-data"):
        form = await request.form()
        req = form.get("requirements")
        if req is None:
            return err(422, "missing_requirements", "multipart field 'requirements' is required")
        raw_doc = (await req.read()) if hasattr(req, "read") else str(req).encode()
        if len(raw_doc) > _MAX_DOC_BYTES:
            return err(413, "requirements_too_large",
                       "requirements document exceeds 2 MB",
                       {"limit_bytes": _MAX_DOC_BYTES})
        try:
            doc = raw_doc.decode("utf-8")
        except UnicodeDecodeError:
            return err(422, "invalid_requirements_encoding",
                       "requirements document is not valid UTF-8")
        ref_items = [v for k, v in form.multi_items()
                     if k == "reference_files" and isinstance(v, UploadFile)]
        if len(ref_items) > _MAX_REF_FILES:
            return err(413, "too_many_reference_files",
                       f"at most {_MAX_REF_FILES} reference files")
        ref = {}
        try:
            for val in ref_items:
                data = await val.read()
                if len(data) > _MAX_REF_BYTES:
                    return err(413, "reference_file_too_large",
                               f"{val.filename} exceeds 20 MB",
                               {"limit_bytes": _MAX_REF_BYTES})
                ref[_ref_name(val.filename)] = data
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content=e.detail)
        try:
            options = Options.model_validate_json(form.get("options") or "{}")
        except ValidationError as e:
            return err(422, "invalid_options", "options failed validation",
                       {"errors": e.errors()})
    else:
        try:
            body = OptimizeBody.model_validate(await request.json())
        except ValidationError as e:
            return err(422, "invalid_body", "request failed validation",
                       {"errors": e.errors()})
        if len(body.requirements_markdown.encode("utf-8")) > _MAX_DOC_BYTES:
            return err(413, "requirements_too_large",
                       "requirements document exceeds 2 MB",
                       {"limit_bytes": _MAX_DOC_BYTES})
        if len(body.reference_files) > _MAX_REF_FILES:
            return err(413, "too_many_reference_files",
                       f"at most {_MAX_REF_FILES} reference files")
        doc = body.requirements_markdown
        import base64
        ref = {}
        try:
            for f in body.reference_files:
                name = _ref_name(f["name"])
                try:
                    data = base64.b64decode(f["content_base64"])
                except Exception:
                    return err(422, "invalid_reference_file",
                               f"bad base64 in reference file {f.get('name')}")
                if len(data) > _MAX_REF_BYTES:
                    return err(413, "reference_file_too_large",
                               f"{name} exceeds 20 MB",
                               {"limit_bytes": _MAX_REF_BYTES})
                ref[name] = data
        except HTTPException as e:
            return JSONResponse(status_code=e.status_code, content=e.detail)
        options = body.options
    job = jobs.submit(doc, ref, options.model_dump(exclude_none=True))
    return JSONResponse(status_code=202, content={
        "job_id": job.job_id, "status": "queued",
        "links": {"job": f"/api/jobs/{job.job_id}",
                  "results": f"/api/results/{job.job_id}"},
    })


@app.get("/api/jobs/{job_id}")
def job_status(job_id: str):
    job = jobs.get(job_id)
    if job is None:
        # try loading finished job from disk (same validation as _job_dir)
        if ".." in job_id or "/" in job_id or "\\" in job_id:
            return err(400, "bad_job_id", "invalid job id")
        jf = RUNS_DIR / "jobs" / job_id / "job.json"
        if jf.exists():
            return json.loads(jf.read_text())
        return err(404, "job_not_found", f"no job {job_id}")
    return job.to_dict()


def _job_dir(job_id: str) -> Path:
    if ".." in job_id or "/" in job_id or "\\" in job_id:
        raise HTTPException(400)
    d = RUNS_DIR / "jobs" / job_id
    if not d.is_dir():
        raise HTTPException(404, detail={"error": {"code": "job_not_found",
                                                 "message": f"no job {job_id}", "details": {}}})
    return d


@app.get("/api/results/{job_id}")
def results(job_id: str):
    d = _job_dir(job_id)
    job = jobs.get(job_id)
    if job is not None and job.status not in ("completed", "needs_input", "unsupported", "failed"):
        return err(409, "job_running", "results not ready yet")
    f = d / "results.json"
    if not f.exists():
        return err(404, "results_not_found", f"no results for {job_id}")
    return json.loads(f.read_text())


@app.get("/api/results/{job_id}/manifest")
def manifest(job_id: str):
    d = _job_dir(job_id)
    f = d / "manifest.json"
    if not f.exists():
        return err(404, "manifest_not_found", f"no manifest for {job_id}")
    return json.loads(f.read_text())


@app.get("/api/results/{job_id}/candidates.csv")
def candidates_csv(job_id: str):
    d = _job_dir(job_id)
    f = d / "candidates.csv"
    if not f.exists():
        return err(404, "not_found", "no candidates.csv")
    return FileResponse(f, media_type="text/csv", filename="candidates.csv")


def _manifest_paths(job_id: str) -> set:
    f = _job_dir(job_id) / "manifest.json"
    if not f.exists():
        return set()
    return {e["path"] for e in json.loads(f.read_text()).get("files", [])}


@app.get("/api/results/{job_id}/files/{path:path}")
def bundle_file(job_id: str, path: str):
    d = _job_dir(job_id)
    if ".." in path or path.startswith("/") or "\\" in path:
        return err(400, "bad_path", "traversal rejected")
    allowed = _manifest_paths(job_id)
    if path not in allowed:
        return err(404, "not_in_manifest", f"{path} is not a bundle file")
    return FileResponse(d / path)


@app.get("/api/results/{job_id}/bundle.zip")
def bundle_zip(job_id: str):
    d = _job_dir(job_id)
    allowed = _manifest_paths(job_id)
    if not allowed:
        return err(404, "no_bundle", "no manifest/bundle")
    mem = io.BytesIO()
    with zipfile.ZipFile(mem, "w", zipfile.ZIP_DEFLATED) as z:
        for rel in sorted(allowed):
            p = d / rel
            if p.exists():
                z.write(p, rel)
    mem.seek(0)
    return StreamingResponse(mem, media_type="application/zip",
                             headers={"Content-Disposition":
                                      f"attachment; filename={job_id}.zip"})


_DESIGN_ID = re.compile(r"^[A-Za-z0-9_-]+$")


def _design_doc(design_id: str) -> tuple[Path, dict]:
    if not _DESIGN_ID.match(design_id):
        raise HTTPException(400, detail={"error": {"code": "bad_design_id",
                                                 "message": "invalid design id",
                                                 "details": {}}})
    job_id = design_id.rsplit("-", 1)[0]
    d = _job_dir(job_id)
    f = d / "designs" / design_id / "design.json"
    if not f.exists():
        raise HTTPException(404, detail={"error": {"code": "design_not_found",
                                                 "message": design_id, "details": {}}})
    return d / "designs" / design_id, json.loads(f.read_text())


@app.get("/api/designs/{design_id}")
def design(design_id: str):
    _, doc = _design_doc(design_id)
    return doc


@app.get("/api/designs/{design_id}/cad")
def design_cad(design_id: str, format: str = "step"):
    ddir, _ = _design_doc(design_id)
    if format not in {"step", "stl", "glb"}:
        return err(422, "bad_format", "format must be step|stl|glb")
    name = f"{design_id}.{format}"
    f = ddir / name
    if not f.exists():
        return err(404, "not_found", f"no {format} for {design_id}")
    return FileResponse(f)


@app.get("/api/designs/{design_id}/fields")
def design_fields(design_id: str):
    ddir, _ = _design_doc(design_id)
    f = ddir / "fields.json"
    if not f.exists():
        return err(404, "not_found", "no fields")
    return json.loads(f.read_text())


@app.get("/api/designs/{design_id}/geometry")
def design_geometry(design_id: str):
    ddir, _ = _design_doc(design_id)
    f = ddir / "geometry.json"
    if not f.exists():
        return err(404, "not_found", "no geometry")
    return json.loads(f.read_text())


@app.get("/api/schema/results")
def results_schema():
    return ResultsDoc.model_json_schema()


@app.get("/api/health")
def health():
    devin_ok, api_v = False, None
    try:
        from coldplate.devin_client import DevinSettings
        s = DevinSettings.from_env()
        devin_ok, api_v = True, s.api_version
    except Exception:
        pass
    return {"status": "ok", "engine_version": ENGINE_VERSION,
            "devin_configured": devin_ok, "api_version": api_v}
