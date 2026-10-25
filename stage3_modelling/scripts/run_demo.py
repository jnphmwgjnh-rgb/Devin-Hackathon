"""CLI demo client for the running coldplate API.

Usage: python scripts/run_demo.py [--input MD] [--reference FILE]...
          [--parser devin|recorded] [--no-cache] [--api URL] [--export-sample]
"""

import argparse
import base64
import json
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _post(url, payload):
    req = urllib.request.Request(
        url, data=json.dumps(payload).encode(), method="POST",
        headers={"Content-Type": "application/json"})
    return json.loads(urllib.request.urlopen(req).read())


def _get(url):
    return json.loads(urllib.request.urlopen(url).read())


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", default=str(ROOT / "tests/fixtures/A_canonical.md"))
    ap.add_argument("--reference", action="append", default=None)
    ap.add_argument("--parser", choices=["devin", "recorded"], default="devin")
    ap.add_argument("--prompt-version", default="v2")
    ap.add_argument("--no-cache", action="store_true")
    ap.add_argument("--api", default="http://127.0.0.1:8000")
    ap.add_argument("--export-sample", action="store_true")
    ap.add_argument("--pop-size", type=int, default=24)
    ap.add_argument("--n-gen", type=int, default=8)
    ap.add_argument("--grid-mm", type=float, default=0.5)
    a = ap.parse_args()

    doc_path = Path(a.input)
    doc = doc_path.read_text(encoding="utf-8")
    refs = a.reference or [str(ROOT / "tests/fixtures/reference_files/power_map_W_per_mm2.csv")]
    ref_files = [
        {"name": Path(r).name,
         "content_base64": base64.b64encode(Path(r).read_bytes()).decode()}
        for r in refs
    ]

    options = {"parser": a.parser, "prompt_version": a.prompt_version,
               "use_cache": not a.no_cache, "pop_size": a.pop_size,
               "n_gen": a.n_gen,
               "grid_mm": a.grid_mm, "sweep": True, "export_cad": True}
    if a.parser == "recorded":
        rec = (ROOT / "tests/fixtures/devin_recordings"
               / f"{doc_path.stem}.{a.prompt_version}.json")
        if not rec.exists():
            sys.exit(f"no recording for {doc_path.stem}.{a.prompt_version}")
        options["recording"] = json.loads(rec.read_text(encoding="utf-8"))

    job = _post(f"{a.api}/api/optimize", {
        "requirements_markdown": doc,
        "reference_files": ref_files,
        "options": options,
    })
    jid = job["job_id"]
    print(f"job {jid} submitted")

    while True:
        j = _get(f"{a.api}/api/jobs/{jid}")
        print(f"  {j['status']:10} {j['stage']:10} {j['progress']:.0%}  {j['message']}")
        if j["status"] in ("completed", "failed", "needs_input", "unsupported"):
            break
        time.sleep(3)

    res = _get(f"{a.api}/api/results/{jid}")
    interp = res.get("interpretation", {})
    print("\n== interpretation ==")
    print("  provider:", interp.get("provider_label"))
    for s in interp.get("sessions", []):
        print(f"  session {s.get('session_id')} {s.get('url')} acus={s.get('acus_consumed')}")
    print("  cached:", interp.get("cached"), "| devin_calls:", interp.get("devin_calls"))

    print("\n== requirement conflicts ==")
    for c in res.get("requirement_conflicts", []):
        print("  -", c)

    print("\n== selected designs ==")
    print(f"{'role':<18}{'status':<14}{'t':<6}{'widths':<28}{'dP kPa':<9}{'HBM C':<8}{'GPU C':<8}files")
    for s in res.get("selected_designs", []):
        m, p = s["metrics"], s["params"]
        w = "/".join(str(v) for v in p.get("channel_width_by_zone_mm", {}).values())
        print(f"{(s['role'] or ''):<18}{s['status_label']:<14}{p['fin_thickness_mm']:<6}{w:<28}"
              f"{m['pressure_drop_kPa']:<9.3f}{m['hbm_max_C'] or 0:<8.1f}{m['gpu_max_C'] or 0:<8.1f}"
              f"{','.join(s.get('files', {}).keys())}")

    print(f"\nbundle: runs/jobs/{jid}/  ({a.api}/api/results/{jid}/bundle.zip)")

    if a.export_sample:
        out = ROOT / "frontend/public/sample-results.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(res, indent=2))
        print("wrote", out)


if __name__ == "__main__":
    main()
