#!/usr/bin/env python3
"""smoke_test.py — End-to-end smoke test for overlay-app.

Usage (server must be running):
  python3 tests/smoke_test.py

What it does:
  1. POSTs two cleaned DXFs from ISGEC/dxf/ + strategy A
  2. Polls /status until state != running/awaiting_axes
  3. At awaiting_axes, POSTs the convention (TP104-, A-D 1-3)
  4. Continues polling until state = "done"
  5. Downloads the final DXF
  6. Verifies the file is a valid DXF (≥ 1 MB)
  7. Reports PASS/FAIL with timing

Environment:
  - Server must be running on http://127.0.0.1:8000 (default)
  - Or set OVERLAY_URL=http://localhost:8000 to override
"""

from __future__ import annotations

import os
import sys
import time
from pathlib import Path

import requests

API = os.environ.get("OVERLAY_URL", "http://127.0.0.1:8765")
ISGEC = Path(os.environ.get("ISGEC_DIR",
    "/home/rushabh/Desktop/Rushabh New Laptop Files/desktop/Rushabh/edi_sem_5/ISGEC"))
TIMEOUT_SEC = 180

MECH = ISGEC / "dxf" / "TP-104 MECH GA_clean3.dxf"
STR = ISGEC / "dxf" / "TP-104 STR GA_clean3.dxf"


def green(s): return f"\033[32m{s}\033[0m"
def red(s):   return f"\033[31m{s}\033[0m"
def dim(s):   return f"\033[2m{s}\033[0m"


def main() -> int:
    if not MECH.exists() or not STR.exists():
        print(red(f"FAIL: missing test fixtures at {MECH} or {STR}"))
        return 1
    if not requests.get(f"{API}/healthz").ok:
        print(red(f"FAIL: server not reachable at {API} (run ./run.sh first)"))
        return 1

    print(dim(f"  → POST /api/v1/overlay"))
    started = time.time()
    with open(MECH, "rb") as fa, open(STR, "rb") as fb:
        r = requests.post(
            f"{API}/api/v1/overlay",
            data={"strategy": "A"},
            files={"drawing_a": (MECH.name, fa), "drawing_b": (STR.name, fb)},
            timeout=30,
        )
    r.raise_for_status()
    job_id = r.json()["job_id"]
    print(f"    job_id = {job_id}")

    # Poll until awaiting_axes (stage 5)
    print(dim(f"  → Polling until /axes is reachable..."))
    deadline = time.time() + 60
    while time.time() < deadline:
        s = requests.get(f"{API}/api/v1/overlay/{job_id}/status").json()
        if s["state"] in ("awaiting_axes", "done", "failed"):
            break
        time.sleep(0.5)
    else:
        print(red("FAIL: timed out waiting for awaiting_axes state"))
        return 1

    if s["state"] == "failed":
        print(red(f"FAIL: pipeline failed before stage 5: {s.get('error')}"))
        return 1
    print(f"    reached state={s['state']} step={s['current_step']} in {time.time()-started:.1f}s")

    # Verify axes suggestions are reachable
    sugs = requests.get(f"{API}/api/v1/overlay/{job_id}/axes").json()
    print(dim(f"  → Got {len(sugs.get('suggestions', []))} convention suggestion(s)"))

    # Submit convention
    print(dim(f"  → POST /axes with TP104- prefix, A-D + 1-3 axes"))
    r = requests.post(
        f"{API}/api/v1/overlay/{job_id}/axes",
        json={"prefix": "TP104-", "axes": ["A", "B", "C", "D", "1", "2", "3"]},
        timeout=10,
    )
    r.raise_for_status()
    j = r.json()
    if not j.get("all_matched"):
        print(red(f"FAIL: convention didn't match (a={j['a_found']}/b={j['b_found']})"))
        return 1

    # Confirm → resumes stages 6-9
    requests.post(f"{API}/api/v1/overlay/{job_id}/axes/confirm", timeout=5)

    # Poll until done
    print(dim(f"  → Polling until done..."))
    fit_done = False
    while time.time() - started < TIMEOUT_SEC:
        s = requests.get(f"{API}/api/v1/overlay/{job_id}/status").json()
        if s["current_step"] == 6 and not fit_done:
            print(f"    fit started ({s['message'][:60]})")
            fit_done = True
        if s["state"] == "done":
            break
        if s["state"] == "failed":
            print(red(f"FAIL: {s['message']} — {s.get('error')}"))
            return 1
        time.sleep(0.5)

    if s["state"] != "done":
        print(red(f"FAIL: timed out (state={s['state']} after {time.time()-started:.0f}s)"))
        return 1
    elapsed = time.time() - started
    print(f"    pipeline DONE in {elapsed:.1f}s")

    # Download the DXF
    print(dim(f"  → GET /download"))
    r = requests.get(f"{API}/api/v1/overlay/{job_id}/download", timeout=60)
    r.raise_for_status()
    dxf_out = Path("/tmp/superimposed_smoke.dxf")
    dxf_out.write_bytes(r.content)
    size_mb = dxf_out.stat().st_size / 1e6
    print(f"    downloaded {size_mb:.1f} MB")

    if dxf_out.stat().st_size < 1_000_000:
        print(red(f"FAIL: output too small ({size_mb:.1f} MB)"))
        return 1

    # Verify it's a valid DXF
    import ezdxf
    try:
        d = ezdxf.readfile(str(dxf_out))
        ent_count = sum(1 for _ in d.modelspace())
    except Exception as e:
        print(red(f"FAIL: downloaded file is not a valid DXF: {e}"))
        return 1

    if ent_count < 10:
        print(red(f"FAIL: modelspace has only {ent_count} entities"))
        return 1

    # Verify the transform was fitted
    job_full = requests.get(f"{API}/api/v1/overlay/{job_id}").json()
    if not job_full.get("transform"):
        print(red("FAIL: no transform in job state"))
        return 1
    t = job_full["transform"]
    print(f"    transform: x_m={t['scale_x']:.4f}·x_s+{t['tx']:.2f}, "
          f"y_m={t['scale_y']:.4f}·y_s+{t['ty']:.2f}, "
          f"max_residual={t['max_residual']:.2e}")

    print()
    print(green(f"  ✓ SMOKE TEST PASSED"))
    print(f"    elapsed: {elapsed:.1f}s")
    print(f"    output:  {dxf_out} ({size_mb:.1f} MB)")
    print(f"    strategy: A (preserve original colors)")
    print(f"    entities: {ent_count} modelspace")
    return 0


if __name__ == "__main__":
    sys.exit(main())
