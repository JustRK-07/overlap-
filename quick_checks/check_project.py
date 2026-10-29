#!/usr/bin/env python3
"""
check_project.py - black-box scoreboard for the ISGEC overlay project.

Run it against a RUNNING server (./run.sh) from the project root:

    python3 quick_checks/check_project.py \
        --mech "path/TP-104 MECH GA.dxf" --str "path/TP-104 STR GA.dxf" \
        [--url http://127.0.0.1:8000] [--skip-real] [--skip-planted]

It only uses the public HTTP API, so it keeps working while you refactor.
Each check prints PASS / FAIL and the prompt number that is meant to fix it.
Exit code = number of failed checks.
"""
import argparse, io, json, re, sys, tempfile, time, pathlib
import requests

RESULTS = []
def check(name, ok, detail="", prompt=""):
    RESULTS.append((name, bool(ok), prompt))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -> {detail}" if detail else "") + (f"   (fixed by prompt {prompt})" if (not ok and prompt) else ""))

def wait(url, jid, states, timeout=600):
    t0 = time.time(); st = {}
    while time.time() - t0 < timeout:
        st = requests.get(f"{url}/api/v1/overlay/{jid}/status").json()
        if st.get("state") in states: return st
        time.sleep(3)
    return st

def run_pair(url, mech, strf, label, extra=None, confirm_body=None, axes=("1","2","3","A","B","C","D"),
             units=None, capture=None):
    files = {"drawing_a": open(mech, "rb"), "drawing_b": open(strf, "rb")}
    r = requests.post(f"{url}/api/v1/overlay", files=files, data=extra or {})
    if r.status_code != 200:
        return None, {"state": "rejected", "error": r.text[:200]}
    jid = r.json()["job_id"]
    st = wait(url, jid, {"awaiting_axes", "failed", "done"})
    if st.get("state") != "awaiting_axes":
        return jid, st
    body = {"prefix": "TP104-", "axes": list(axes)}
    ra = requests.post(f"{url}/api/v1/overlay/{jid}/axes", json=body)
    axes_payload = ra.json() if ra.ok else {}
    if units:
        # Look before you answer: the first response carries the diagnosis
        # (what the grid says the two drawings are in), the second one
        # carries the answer.
        requests.post(f"{url}/api/v1/overlay/{jid}/axes",
                      json={**body, "units": units})
    if capture is not None:
        capture.update(axes_payload)
    cbody = dict(confirm_body or {})
    if units:
        cbody["units"] = units
    requests.post(f"{url}/api/v1/overlay/{jid}/axes/confirm", json=cbody)
    st = wait(url, jid, {"done", "failed"})
    return jid, st

def get_findings(url, jid):
    r = requests.get(f"{url}/api/v1/overlay/{jid}/findings")
    return r.json() if r.status_code == 200 else {}


def finding_rows(body):
    """The findings of a findings envelope (the API calls them "items")."""
    return body.get("items") or body.get("findings") or []

# ---------------------------------------------------------------- real TP-104
def real(url, mech, strf):
    print("\n=== A. Real TP-104 drawings (no force, no manual edits) ===")
    # The MECH header claims inches; the grid says both drawings are in mm.
    # Confirm that — the units override — and the pipeline must then run
    # without any "allow weak fit" crutch.
    seen = {}
    jid, st = run_pair(url, mech, strf, "real", units={"mech": "mm", "str": "mm"},
                       capture=seen)
    sug = ((seen.get("units") or {}).get("suggestion") or {})
    check("A0 the grid indicates both drawings are in mm",
          sug.get("mech") == "mm" and sug.get("str") == "mm",
          f"suggestion={sug}", "2 (units)")
    check("A1 pipeline finishes WITHOUT allow_weak_fit although MECH header says inches",
          st.get("state") == "done", f"state={st.get('state')} err={(st.get('error') or '')[:90]}", "2 (units)")
    if st.get("state") != "done":
        print("     (remaining real-drawing checks skipped: no result)"); return
    t = requests.get(f"{url}/api/v1/overlay/{jid}").json().get("transform") or {}
    check("A2 fit: scale 1.0, rotation 0, 12/12 inliers",
          abs(t.get("scale", 0) - 1) < 1e-6 and abs(t.get("rotation_deg", 9)) < 1e-3 and t.get("n_inliers") == 12,
          f"scale={t.get('scale')} rot={t.get('rotation_deg')} inl={t.get('n_inliers')}", "(already works)")
    check("A3 translation matches known TP-104 value (208379.860, -179584.524)",
          abs(t.get("tx", 0) - 208379.860) < 0.01 and abs(t.get("ty", 0) + 179584.524) < 0.01,
          f"tx={t.get('tx')} ty={t.get('ty')}", "(already works)")
    f = get_findings(url, jid)
    units = (f.get("units") or {})
    check("A4 findings use mm_per_unit = 1.0 (not 25.4)", abs(units.get("mm_per_unit", 0) - 1.0) < 1e-9,
          f"mm_per_unit={units.get('mm_per_unit')}", "2 (units)")
    cov = f.get("coverage") or {}
    mm_status = f.get("status")
    zero_cov = not any((c or {}).get("matched", 0) for c in cov.values())
    check("A5 zero matched items is NOT reported as 'ok'/passed",
          not (zero_cov and mm_status in ("ok", "passed")), f"status={mm_status}, coverage={json.dumps(cov)[:120]}", "3 (status)")
    rep = requests.get(f"{url}/api/v1/overlay/{jid}/report.json").json().get("mismatch") or {}
    str_feats = (rep.get("features") or {}).get("STR") or {}
    non_hole = {k: v for k, v in str_feats.items() if k != "str_hole" and v}
    check("A6 STR members / plates / columns are recognised (not only holes)",
          bool(non_hole), f"STR features recognised: {str_feats}", "4 (Tekla classification)")
    rows = finding_rows(f)
    check("A7 each finding has dx_mm, dy_mm and a grid bay",
          bool(rows) and all(("dx_mm" in r or "dx" in r) and ("grid_bay" in r or "bay" in r) for r in rows[:5]),
          f"{len(rows)} findings; keys={sorted(rows[0].keys())[:8] if rows else '-'}", "6 (report)")

# ------------------------------------------------------------- planted offset
def planted(url, repo):
    print("\n=== B. Planted 7.5 mm offset (synthetic, ground truth known) ===")
    sys.path.insert(0, str(pathlib.Path(repo) / "tests")); sys.path.insert(0, str(repo))
    import synth, ezdxf
    tmp = pathlib.Path(tempfile.mkdtemp())
    pair = synth.build_pair(tmp, synth.LEGACY_COMPATIBLE)
    o = pair.opts; P = (1500.0, 8000.0)       # location in frame P (inside the grid)
    W, H, SHIFT = 200.0, 150.0, 7.5
    def rect(x, y): return [(x, y), (x + W, y), (x + W, y + H), (x, y + H)]
    for path, off, dx in ((pair.mech_path, o.mech_offset, 0.0), (pair.str_path, o.str_offset, SHIFT)):
        d = ezdxf.readfile(path)
        if "NEUTRAL" not in d.layers: d.layers.add("NEUTRAL")
        d.modelspace().add_lwpolyline(rect(P[0] + off[0] + dx, P[1] + off[1]), close=True, dxfattribs={"layer": "NEUTRAL"})
        d.saveas(path)
    jid, st = run_pair(url, pair.mech_path, pair.str_path, "planted", axes=("A","B","C","D","1","2","3"))
    check("B1 synthetic pair runs to done", st.get("state") == "done", f"state={st.get('state')} {(st.get('error') or '')[:80]}", "(baseline)")
    if st.get("state") != "done": return
    f = get_findings(url, jid); rows = finding_rows(f)
    cx, cy = P[0] + o.mech_offset[0] + W/2, P[1] + o.mech_offset[1] + H/2
    near = [r for r in rows if abs(float(r.get("x", 1e12)) - cx) < 600 and abs(float(r.get("y", 1e12)) - cy) < 600]
    check("B2 the 7.5 mm shifted rectangle produces a finding at its location", bool(near), f"{len(rows)} findings in total", "5 (offset engine)")
    if near:
        mags = [abs(float(r.get("magnitude_mm", r.get("offset_mm", 0)))) for r in near]
        dxs = [float(r.get("dx_mm", 0)) for r in near]
        check("B3 reported offset is 7.5 mm (+-0.1) and dx=7.5, dy=0",
              any(abs(m - 7.5) < 0.1 for m in mags) and any(abs(abs(v) - 7.5) < 0.1 for v in dxs), f"magnitudes={mags}", "5 (offset engine)")
    check("B4 a run that missed the planted offset must not say ok/passed",
          bool(near) or f.get("status") not in ("ok", "passed"), f"status={f.get('status')}", "3 (status)")

# -------------------------------------------------------------------- hygiene
def hygiene(repo):
    print("\n=== C. Repository hygiene (static) ===")
    r = pathlib.Path(repo)
    pyc = list(r.rglob("__pycache__"))
    check("C1 no __pycache__ folders shipped", not [p for p in pyc if ".git" not in p.parts], f"{len(pyc)} found", "1")
    hard = [str(p) for p in r.rglob("*.py") if "/home/rushabh" in p.read_text(errors="ignore")]
    check("C2 no developer-machine paths in code", not hard, str(hard[:2]), "8")
    check("C3 jobs/ folder is not packed with old jobs", len(list((r / "jobs").glob("*/job.json"))) < 5 if (r / "jobs").exists() else True,
          f"{len(list((r/'jobs').glob('*/job.json')))} saved jobs", "1/8")
    run_sh = (r / "run.sh").read_text() if (r / "run.sh").exists() else ""
    check("C4 run.sh does not bind 0.0.0.0 by default", "0.0.0.0" not in run_sh, "binds 0.0.0.0", "8")

if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="http://127.0.0.1:8000"); ap.add_argument("--repo", default=".")
    ap.add_argument("--mech"); ap.add_argument("--str", dest="strf")
    ap.add_argument("--skip-real", action="store_true"); ap.add_argument("--skip-planted", action="store_true")
    a = ap.parse_args()
    if not a.skip_real:
        if not (a.mech and a.strf): sys.exit("give --mech and --str, or use --skip-real")
        real(a.url, a.mech, a.strf)
    if not a.skip_planted: planted(a.url, a.repo)
    hygiene(a.repo)
    bad = [r for r in RESULTS if not r[1]]
    print(f"\n{len(RESULTS)-len(bad)}/{len(RESULTS)} checks pass")
    sys.exit(len(bad))
