"""Build stage3_modelling/demo/itherm/ from a real job bundle."""
import argparse, json, shutil
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--job", required=True)
    ap.add_argument("--out", default=str(ROOT / "demo" / "itherm"))
    a = ap.parse_args()
    job = Path(a.job); out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    for f in ("results.json", "interpretation.json"):
        shutil.copy2(job / f, out / f)
    res = json.loads((job / "results.json").read_text())
    for s in res.get("selected_designs", []):
        ddir = job / "designs" / s["design_id"]
        od = out / "designs" / s["design_id"]
        od.mkdir(parents=True, exist_ok=True)
        for f in (f"{s['design_id']}.stl",):
            p = ddir / f
            if p.exists(): shutil.copy2(p, od / f)
    sess = (res.get("interpretation", {}).get("sessions") or [{}])[0]
    (out / "demo_source.json").write_text(json.dumps({
        "job_id": res.get("job_id"), "created_at": res.get("created_at"),
        "devin_session_url": sess.get("url"),
        "inputs": "tests/fixtures/A_canonical.md + figure-derived power map CSV",
        "engine_version": res.get("engine_version"),
        "bundled_at": datetime.now(timezone.utc).isoformat(),
    }, indent=2))
    size = sum(p.stat().st_size for p in out.rglob("*") if p.is_file())
    print(f"demo bundle: {out}  ({size/1e6:.1f} MB, "
          f"{sum(1 for _ in out.rglob('*') if _.is_file())} files)")

if __name__ == "__main__":
    main()
